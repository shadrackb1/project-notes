# ---------------------------------------------------------------------------
# Keeps the Claude Code proxy alive.
#
# Runs at login, checks the health endpoint every 60s, and restarts the proxy
# through start-proxy.ps1 when it stops answering. A named mutex makes the
# script single-instance: a second copy (from the scheduled-task safety net)
# exits immediately instead of fighting over port 8788.
#
# Log: C:\Users\ADMIN\.claude-nim\watchdog.log
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Continue'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }

# Overridable so the recovery path can be exercised against a stand-in server
# without touching the real proxy.
$log = $env:CLAUDE_PROXY_WATCHDOG_LOG
if (-not $log) { $log = Join-Path $nimDir 'watchdog.log' }
$probe = $env:CLAUDE_PROXY_HEALTH_URL
if (-not $probe) { $probe = 'http://127.0.0.1:8788/health/liveliness' }
$starter = $env:CLAUDE_PROXY_STARTER
if (-not $starter) { $starter = Join-Path $nimDir 'start-proxy.ps1' }
$interval = 60
if ($env:CLAUDE_PROXY_CHECK_SECONDS) { $interval = [int]$env:CLAUDE_PROXY_CHECK_SECONDS }
# Optional command-based health probe (exit 0 = healthy). Lets the control loop
# be tested without depending on real sockets; production uses the HTTP probe.
$healthCmd = $env:CLAUDE_PROXY_HEALTH_CMD
$grace = 20
if ($env:CLAUDE_PROXY_RESTART_GRACE_SECONDS) { $grace = [int]$env:CLAUDE_PROXY_RESTART_GRACE_SECONDS }
$mutexName = $env:CLAUDE_PROXY_MUTEX
if (-not $mutexName) { $mutexName = 'Global\ClaudeCodeProxyWatchdog' }

function Write-Log($message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    # Keep the log from growing without bound.
    $info = Get-Item $log -ErrorAction SilentlyContinue
    if ($info -and $info.Length -gt 512KB) {
        $tail = Get-Content $log -Tail 500
        Set-Content -Path $log -Value $tail -Encoding utf8
    }
}

function Test-Proxy {
    if ($healthCmd) {
        & cmd /c $healthCmd 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    try {
        $null = Invoke-RestMethod -Uri $probe -TimeoutSec 6
        return $true
    } catch {
        return $false
    }
}

# The compatibility shim on :8791 is what the client now points at (it strips
# fast mode and re-maps model ids the proxy does not serve), so it has to be
# watched too: without it every request fails at the socket level.
$shimScript = Join-Path $nimDir 'compat.py'
$shimProbe = 'http://127.0.0.1:8791/health/liveliness'

function Test-Shim {
    try {
        $null = Invoke-RestMethod -Uri $shimProbe -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

function Start-Shim {
    # Launched through wscript so the shim is not a child of this process: a
    # direct Start-Process copy dies with the watchdog if the watchdog is
    # killed from outside.
    $starter = Join-Path $nimDir 'start-compat.vbs'
    if (Test-Path $starter) {
        Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $starter + '"') -WindowStyle Hidden
    } else {
        # Absolute path: the watchdog runs with a minimal PATH, where `python`
        # does not resolve and the shim silently fails to start.
        $python = $env:NIM_PYTHON_EXE
        if (-not $python) { $python = 'C:\Python314\python.exe' }
        if (-not (Test-Path $python)) { $python = 'C:\Python314\python.exe' }
        Start-Process -FilePath $python -ArgumentList $shimScript -WindowStyle Hidden
    }
}

# Single instance. Global\ so it is visible across sessions.
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    Write-Log 'watchdog already running; exiting'
    exit 0
}

Write-Log 'watchdog started'
$failures = 0
$shimFailures = 0

while ($true) {
    $healthy = Test-Proxy
    if (-not $healthy) {
        # Confirm before acting. A single missed probe is usually the probe
        # itself: PowerShell's startup and a busy proxy can both exceed the
        # timeout. Restarting on it killed in-flight turns, which is exactly
        # what a long session must not have happen.
        Start-Sleep -Seconds 8
        $healthy = Test-Proxy
    }

    if ($healthy) {
        if ($failures -gt 0) { Write-Log 'proxy healthy again'; $failures = 0 }
    } else {
        $failures++
        Write-Log "proxy not answering (failure $failures); restarting"
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $starter | Out-Null
        } catch {
            Write-Log "start-proxy.ps1 threw: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds $grace
        if (Test-Proxy) {
            Write-Log 'proxy recovered'
            $failures = 0
        } else {
            Write-Log 'restart did not bring the proxy back'
            # A restart that fails with an empty log means the proxy never
            # reached startup; a restart that fails with output means it
            # crashed or the port was still held. Both used to look identical
            # from the log, so a real crash was invisible.
            $latest = Get-ChildItem -LiteralPath $nimDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($latest) {
                if ($latest.Length -eq 0) {
                    Write-Log "  $($latest.Name) is empty: the proxy never finished starting"
                } else {
                    Get-Content -LiteralPath $latest.FullName -Tail 5 -ErrorAction SilentlyContinue |
                        ForEach-Object { Write-Log "  $_" }
                }
            }
        }
    }

    if (Test-Shim) {
        if ($shimFailures -gt 0) { Write-Log 'shim healthy again'; $shimFailures = 0 }
    } else {
        $shimFailures++
        Write-Log "shim not answering (failure $shimFailures); restarting"
        try {
            Start-Shim
        } catch {
            Write-Log "starting the shim threw: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 5
        if (Test-Shim) {
            Write-Log 'shim recovered'
            $shimFailures = 0
        } else {
            Write-Log 'restart did not bring the shim back'
        }
    }

    Start-Sleep -Seconds $interval
}
