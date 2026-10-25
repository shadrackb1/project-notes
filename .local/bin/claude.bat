@echo off
REM Wrapper for Claude Code to use NVIDIA NIM models via local LiteLLM proxy (port 8788)
REM Proxy config: C:\Users\ADMIN\.claude-nim\config.yaml

REM Point at the compat shim (:8791), not the raw proxy: the shim strips the
REM fields NIM rejects and guarantees every turn comes back with text.
set ANTHROPIC_BASE_URL=http://localhost:8791
set ANTHROPIC_API_KEY=not-used

REM Master key for the LiteLLM proxy (stored in user env via setx)
if "%LITELLM_MASTER_KEY%"=="" (
    for /f "tokens=1,2*" %%A in ('reg query "HKCU\Environment" -v LITELLM_MASTER_KEY 2^>nul') do set LITELLM_MASTER_KEY=%%C
)
if "%LITELLM_MASTER_KEY%"=="" (
    echo Error: LITELLM_MASTER_KEY is not set in user environment.
    exit /b 1
)
set ANTHROPIC_AUTH_TOKEN=%LITELLM_MASTER_KEY%

if "%NVIDIA_API_KEY%"=="" (
    echo Error: NVIDIA_API_KEY environment variable is not set.
    echo Please set it via 'setx NVIDIA_API_KEY nvapi-...' and restart this terminal.
    exit /b 1
)

REM Model aliases -> verified-working NVIDIA NIM models. Claude Code only
REM enables thinking budgets, effort scaling and fine-grained tool streaming
REM for ids it recognises, so use the Claude-shaped names.
set ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-5.5
set ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-5.5
set ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-5.5
set ANTHROPIC_DEFAULT_FABLE_MODEL=claude-fable-5.1
set CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5.5
set ANTHROPIC_CUSTOM_MODEL_OPTION=claude-opus-5.5

REM Skip proxy bootstrap for informational commands
set SKIP_PROXY=false
for %%a in (%*) do (
    if "%%a"=="--version" set SKIP_PROXY=true
    if "%%a"=="-v" set SKIP_PROXY=true
    if "%%a"=="--help" set SKIP_PROXY=true
    if "%%a"=="-h" set SKIP_PROXY=true
)
if "%SKIP_PROXY%"=="true" (
    "C:\Users\ADMIN\.local\bin\claude.exe" %*
    exit /b %ERRORLEVEL%
)

REM Start proxy if not already running
curl -s --max-time 2 http://localhost:8788/health/liveliness >nul 2>&1
if %ERRORLEVEL% EQU 0 goto SHIM_CHECK

echo Starting NVIDIA NIM LiteLLM proxy on :8788 ...
REM Through the same script login uses. Starting litellm.exe directly here
REM skipped the config and settings guards, the environment loaded from .env
REM (including REPEATED_STREAMING_CHUNK_LIMIT, without which a long session
REM dies mid-stream), and the stale-process cleanup that stops two proxies
REM fighting over the port. start-proxy.ps1 is idempotent and waits for
REM readiness itself, so its exit code is the real signal.
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\ADMIN\.claude-nim\start-proxy.ps1" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Error: LiteLLM proxy did not become ready. See C:\Users\ADMIN\.claude-nim\litellm-*.log
    exit /b 1
)
echo Proxy ready.
goto PROXY_READY

:SHIM_CHECK
REM The compat shim must be up too: it is what the client talks to.
curl -s --max-time 2 http://localhost:8791/health/liveliness >nul 2>&1
if %ERRORLEVEL% EQU 0 goto PROXY_READY
REM Absolute interpreter through the same launcher login uses: bare `python`
REM depends on PATH, which is minimal in a fresh shell.
start "" /b cmd /c "wscript.exe \"C:\Users\ADMIN\.claude-nim\start-compat.vbs\""
set TIMEOUT=30
set ELAPSED=0
:SHIM_WAIT
if %ELAPSED% GEQ %TIMEOUT% (
    echo Error: compat shim did not become ready. See C:\Users\ADMIN\.claude-nim\compat.log
    exit /b 1
)
curl -s --max-time 2 http://localhost:8791/health/liveliness >nul 2>&1
if %ERRORLEVEL% EQU 0 goto PROXY_READY
timeout /t 1 /nobreak >nul
set /a ELAPSED+=1
goto SHIM_WAIT

:PROXY_READY
"C:\Users\ADMIN\.local\bin\claude.exe" %*
exit /b %ERRORLEVEL%
