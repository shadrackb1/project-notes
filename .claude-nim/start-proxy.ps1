# Start the Claude Code LiteLLM proxy as a fully detached process.
#
# PowerShell's Start-Process creates the proxy outside the calling shell's
# console, so it keeps running after the terminal closes. That is what stopped
# the "Connection refused" errors: `nohup ... &` from Git Bash died with the
# shell that launched it.
#
# Exit codes:  0 = ready, 1 = failed to start

$ErrorActionPreference = 'Stop'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$litellm = $env:LITELLM_EXE
if (-not $litellm) { $litellm = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Python\Python314\Scripts\litellm.exe' }
$config = Join-Path $nimDir 'config.yaml'
$log = Join-Path $nimDir 'litellm.log'
$err = Join-Path $nimDir 'litellm.err.log'
$probe = 'http://127.0.0.1:8788/health/liveliness'

function Test-Proxy {
    try {
        $r = Invoke-RestMethod -Uri $probe -TimeoutSec 2
        return ($null -ne $r)
    } catch {
        return $false
    }
}

# --- Config guard ----------------------------------------------------------
# Validated before the "already running" shortcut, because an empty fallback
# list is legal YAML that silently disables failover. When that happened, every
# 429 from NVIDIA ended the whole Claude Code turn instead of retrying on
# another model. The guard restores the known-good snapshot and reports what
# it found; a repair forces a reload so the running proxy stops using the bad
# config.
$configRepaired = $false
$guardPython = $env:NIM_PYTHON_EXE
if (-not $guardPython) { $guardPython = 'python' }
if (-not (Test-Path $guardPython)) { $guardPython = 'python' }
$guard = Join-Path $nimDir 'config-guard.py'
if (Test-Path $guard) {
    # $ErrorActionPreference is 'Stop' in this script; a native command that
    # writes anything to stderr would otherwise abort the run mid-guard and
    # leave the bad config in place, so relax it for the duration of the call.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $guardOutput = & $guardPython $guard 2>&1
        $guardExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($guardOutput) { Write-Host ($guardOutput -join "`n") }
    if ($guardExit -eq 1) {
        $configRepaired = $true
        Write-Host 'Config guard repaired an invalid config; reloading the proxy.'
    } elseif ($guardExit -eq 2) {
        Write-Host 'Config guard: fallbacks are still invalid; rate limits will end turns.'
    }
}

# --- Settings guard --------------------------------------------------------
# ~/.claude/settings.json carries the settings without which this setup looks
# broken but still runs: the auto-mode classifier opt-out (otherwise every
# auto-mode turn parks on "this session isn't eligible"), gateway model
# discovery (otherwise /model offers six entries), and the fast-mode
# entitlements. A settings reset, a reinstall or a crashed writer can drop any
# of them, so they are restored on every start, before the proxy shortcut.
$settingsGuard = Join-Path $nimDir 'settings-guard.py'
if (Test-Path $settingsGuard) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $guardPython $settingsGuard 2>&1 | ForEach-Object { Write-Host $_ }
    } catch {
        Write-Host "settings guard could not run: $($_.Exception.Message)"
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

if ((Test-Proxy) -and (-not $configRepaired)) { exit 0 }

# --- Clear stale or duplicate proxies --------------------------------------
# Two failure modes made "restart" look successful when it was not:
#
#   1. The socket is reported as owned by a granian WORKER. Killing that PID
#      leaves the parent alive, and granian immediately spawns a replacement.
#   2. After a hard kill, a worker can outlive its parent. It keeps :8788
#      bound while no litellm main process exists, and every restart then
#      starts a second proxy that cannot serve.
#
# Either way two proxies share one port, each with a different config, and the
# symptom is "my change did nothing" or random 502s. So identify litellm by
# its command line (never by port owner), kill the main's tree, and also kill
# workers whose parent no longer exists.
function Stop-StaleProxy {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Select-Object ProcessId, ParentProcessId, CommandLine
    $live = @{}
    foreach ($p in $procs) { $live[[int]$p.ProcessId] = $true }

    $targets = @()
    foreach ($p in $procs) {
        $cmd = $p.CommandLine
        if (-not $cmd) { continue }
        if ($cmd -match 'litellm\.exe' -and $cmd -match '--config') {
            $targets += $p.ProcessId
            continue
        }
        if ($cmd -match 'spawn_main\(parent_pid=(\d+)') {
            $parent = [int]$Matches[1]
            if (-not $live.ContainsKey($parent)) { $targets += $p.ProcessId }
        }
    }

    foreach ($target in ($targets | Select-Object -Unique)) {
        Write-Host "Stopping stale proxy process $target."
        & taskkill /PID $target /T /F 2>$null | Out-Null
    }

    if ($targets) {
        for ($i = 0; $i -lt 12; $i++) {
            Start-Sleep -Milliseconds 500
            $held = Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
            if (-not $held) { break }
        }
    }

    # Each start now writes its own log pair, so prune old ones instead of
    # letting the directory grow forever (6 starts kept).
    Get-ChildItem -LiteralPath $nimDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 12 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# --- One starter at a time -------------------------------------------------
# Login can launch this script three ways at once (Startup folder, HKCU Run
# entry, scheduled task). Without this, each copy sees an unhealthy proxy
# during the other's warmup, kills it in Stop-StaleProxy, and starts again:
# a kill/restart loop for as long as the launchers overlap. The loser now
# waits for the winner instead of competing.
$startMutex = New-Object System.Threading.Mutex($false, 'Local\ClaudeNimProxyStart')
$startOwned = $false
try {
    $startOwned = $startMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $startOwned = $true
}
if (-not $startOwned) {
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Proxy) { exit 0 }
    }
    Write-Error 'another launcher is starting the proxy, but it is not ready after 120s'
    exit 1
}

Stop-StaleProxy

# --- Ensure the Python 'mcp' package is at least 2.2 (litellm >=1.106 needs it) ---
# litellm 1.106+ imports CacheableResult from mcp.types, which only exists in
# mcp >= 2.2. Without it, litellm crashes with:
#   ImportError: cannot import name 'CacheableResult' from 'mcp.types'
$python = $env:NIM_PYTHON_EXE
if (-not $python) { $python = 'python' }
# Exit-code based, with stderr discarded. Checking the TEXT of the output made
# every start log a bogus "Traceback" (any stderr record tripped the Stop
# preference) and then attempt a pip upgrade that is slow enough to look like a
# hung restart.
& $python -c "import mcp.types,sys; sys.exit(0 if hasattr(mcp.types,'CacheableResult') else 3)" 2>$null
if ($LASTEXITCODE -eq 3) {
    Write-Host "mcp package is missing CacheableResult; upgrading to >=2.2..."
    & $python -m pip install --quiet --upgrade "mcp>=2.2.0,<3" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "mcp upgrade failed; the proxy may still start with the installed version."
    }
} elseif ($LASTEXITCODE -ne 0) {
    Write-Host "mcp import check could not run (exit $LASTEXITCODE); continuing."
}

# Pull the keys the config expects into this process, then hand them to the
# child. setx values live in HKCU and are not inherited by older shells.
foreach ($name in @('LITELLM_MASTER_KEY', 'NVIDIA_API_KEY')) {
    if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
        $val = [Environment]::GetEnvironmentVariable($name, 'User')
        if ($val) { [Environment]::SetEnvironmentVariable($name, $val, 'Process') }
    }
}

if (-not $env:LITELLM_MASTER_KEY) {
    Write-Error 'LITELLM_MASTER_KEY is not set. Run: setx LITELLM_MASTER_KEY sk-...'
    exit 1
}

# Set here as well as in .env: LiteLLM reads this constant at import time, and
# a value that only the .bat supplies is invisible when the proxy is started
# any other way. 500 identical chunks still catches a runaway loop while
# tolerating the template-token repetition that killed long sessions.
if (-not $env:REPEATED_STREAMING_CHUNK_LIMIT) {
    $env:REPEATED_STREAMING_CHUNK_LIMIT = '500'
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# One uvicorn process. Granian is limited to a single worker on Windows (its
# own warning) and reset connections mid-body under a burst of subagent
# requests, which surfaced as ClientDisconnect in LiteLLM and "API error" in
# Claude Code. Uvicorn handles the same concurrency without those resets.
$workers = 1

# Fresh log files per start. Reusing one path lets a previous instance's open
# handle block Start-Process, which turned a restart into a hang instead of a
# clean failure.
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $nimDir "litellm-$stamp.log"
$err = $log

# Started through run-proxy.bat so redirection happens in cmd, not in this
# script. With `Start-Process -RedirectStandardOutput` the proxy shares this
# process's stdout handle, and whenever this script is itself piped (the
# watchdog does exactly that) the parent then blocks until the proxy exits,
# i.e. forever. That is what made automatic restarts silently stop working.
$launcher = Join-Path $nimDir 'run-proxy.bat'
Start-Process -FilePath $launcher `
    -ArgumentList @("`"$log`"", "$workers") `
    -WindowStyle Hidden | Out-Null

# Multiple workers load the catalog independently, so allow a generous window.
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Proxy) { exit 0 }
}

Write-Host "Proxy did not become ready in time. Last lines of ${err}:"
Get-Content -LiteralPath $err -Tail 25 -ErrorAction SilentlyContinue | Write-Host
Write-Error "Proxy did not become ready; the next 'claude' will report a connection error until it does."
exit 1
