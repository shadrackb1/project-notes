@echo off
REM Foreground launcher for the LiteLLM proxy. run it detached (wscript or
REM Start-Process -WindowStyle Hidden); it never returns while the proxy lives.
REM
REM   %1 = stdout log file
REM   %2 = worker count
REM
REM This exists because `Start-Process -RedirectStandardOutput` from PowerShell
REM makes the child share the parent's stdout pipe. When the parent is itself
REM piped (the watchdog runs `powershell ... | Out-Null`), the parent then waits
REM for the proxy to exit, which is forever: the watchdog appeared to hang and
REM logged "restart did not bring the proxy back". Redirection happens here, in
REM cmd, so the parent's handles stay clean.

setlocal
if "%~1"=="" (
    echo usage: run-proxy.bat ^<logfile^> ^<workers^>
    exit /b 2
)
REM Kept for compatibility with callers that still pass a worker count.
set WORKERS=%~2
if "%WORKERS%"=="" set WORKERS=1

REM Load environment variables from .env file
for /f "usebackq tokens=1* delims==" %%a in ("%~dp0.env") do (
    if not "%%a"=="" if not "%%a:~0,1%"=="#" (
        set "%%a=%%b"
    )
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set LITELLM_EXE=%LITELLM_EXE:C:\Users\ADMIN\AppData\Roaming\Python\Python314\Scripts\=%
if "%LITELLM_EXE%"=="" set LITELLM_EXE=%LOCALAPPDATA%\Python\Python314\Scripts\litellm.exe
set CONFIG=%~dp0config.yaml

REM Uvicorn, one process. Granian was faster in a benchmark but it cannot run
REM more than one worker on Windows (its own warning), and under a burst of
REM subagent requests it reset connections mid-body, which LiteLLM logged as
REM ClientDisconnect and answered with "Missing required parameter: messages".
REM It also hung before binding the port on two restarts, leaving a zero-byte
REM log and no listener. Uvicorn is single-process and handles the same load.
"%LITELLM_EXE%" --config "%CONFIG%" --port 8788 --host 127.0.0.1 > "%~1" 2>&1
