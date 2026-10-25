# ---------------------------------------------------------------------------
# Self-heal + housekeeping for the Claude Code stack.
#
# Safe to run as often as you like: every action checks before it acts, and the
# launchers it calls are idempotent. Scheduled every 10 minutes by
# register-tasks.ps1, which is what turns "it broke while I was away" into a
# line in maintenance.log instead of a lost afternoon.
#
# What it does, in order:
#   1. raise the stack if a layer is down (proxy, shim, watchdog)
#   2. re-assert config.yaml and settings.json guards
#   3. run doctor.py and record the result
#   4. prune logs so the directory cannot grow without bound
#   5. toast once when the stack is unhealthy, not once per run
#
# Exit codes: 0 = healthy, 1 = problems remain
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Continue'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$python = $env:NIM_PYTHON_EXE
if (-not $python) { $python = 'C:\Python314\python.exe' }
if (-not (Test-Path $python)) { $python = 'python' }

$log = Join-Path $nimDir 'maintenance.log'
$state = Join-Path $nimDir 'maintenance.state'
$health = Join-Path $nimDir 'health.json'
$proxyProbe = 'http://127.0.0.1:8788/health/liveliness'
$shimProbe = 'http://127.0.0.1:8791/health/liveliness'

function Write-Log($message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    $info = Get-Item $log -ErrorAction SilentlyContinue
    if ($info -and $info.Length -gt 512KB) {
        Set-Content -Path $log -Value (Get-Content $log -Tail 500) -Encoding utf8
    }
}

function Test-Url($url) {
    try {
        $null = Invoke-RestMethod -Uri $url -TimeoutSec 6
        return $true
    } catch {
        return $false
    }
}

function Show-Toast($message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        Add-Type -AssemblyName System.Drawing | Out-Null
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $icon.Visible = $true
        $icon.ShowBalloonTip(10000, 'Claude Code stack', $message, [System.Windows.Forms.ToolTipIcon]::Warning)
        Start-Sleep -Seconds 6
        $icon.Dispose()
    } catch {
        # A toast is a courtesy; never let it fail the maintenance run.
    }
}

# Two supervisors exist on purpose (the keeper and the scheduled task), which
# means both can fire in the same minute. Guards and doctor both rewrite files,
# so overlapping runs could make one of them read a half-written config and
# "repair" a file that was never broken. Only one run may be in flight.
$mutex = New-Object System.Threading.Mutex($false, 'Local\ClaudeNimMaintenance')
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    Write-Log 'another maintenance run is in progress; skipping this one'
    exit 0
}

# A scheduled task that keeps firing while the proxy is mid-restart would
# fight the watchdog. The watchdog owns the restart decision while it is alive;
# this script only starts the things that are missing entirely.
$watchdog = $null
try {
    $watchdog = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'watchdog\.ps1' } |
        Select-Object -First 1
} catch {
    Write-Log "could not inspect processes: $($_.Exception.Message)"
}

$proxyUp = Test-Url $proxyProbe
$shimUp = Test-Url $shimProbe

if (-not $proxyUp -and -not $watchdog) {
    Write-Log 'proxy down and no watchdog: starting the stack'
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $nimDir 'start-proxy.ps1') | Out-Null
    } catch {
        Write-Log "start-proxy.ps1 threw: $($_.Exception.Message)"
    }
    $proxyUp = Test-Url $proxyProbe
} elseif (-not $proxyUp) {
    Write-Log 'proxy not answering; watchdog owns the restart, leaving it alone'
}

if (-not $shimUp) {
    Write-Log 'shim not answering: starting it'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $nimDir 'start-compat.vbs') + '"') -WindowStyle Hidden
    Start-Sleep -Seconds 3
    $shimUp = Test-Url $shimProbe
}

if (-not $watchdog) {
    Write-Log 'watchdog not running: starting it'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $nimDir 'start-watchdog.vbs') + '"') -WindowStyle Hidden
}

# Both guards are idempotent: they restore the known-good state only when the
# live file has drifted, and they say so.
foreach ($guard in @('config-guard.py', 'settings-guard.py')) {
    $path = Join-Path $nimDir $guard
    if (-not (Test-Path $path)) { continue }
    $out = & $python $path 2>&1 | Out-String
    if ($out.Trim()) { Write-Log ("{0}: {1}" -f $guard, ($out.Trim() -replace "`r?`n", ' | ')) }
}

$doctorOut = & $python (Join-Path $nimDir 'doctor.py') 2>&1 | Out-String
$doctorExit = $LASTEXITCODE
if ($doctorExit -ne 0) {
    Write-Log ("doctor found problems: {0}" -f ($doctorOut.Trim() -replace "`r?`n", ' | '))
}

# --- Housekeeping ----------------------------------------------------------
# The proxy writes a fresh log per start; keep the newest 20 and drop anything
# older than two weeks so the folder stays readable.
$cutoff = (Get-Date).AddDays(-14)
Get-ChildItem -LiteralPath $nimDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $nimDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 20 |
    Remove-Item -Force -ErrorAction SilentlyContinue

# compat.log rotates at 5 MB inside the shim; this catches the case where the
# shim was not restarted for weeks.
$compatLog = Join-Path $nimDir 'compat.log'
$info = Get-Item $compatLog -ErrorAction SilentlyContinue
if ($info -and $info.Length -gt 4MB) {
    Set-Content -Path $compatLog -Value (Get-Content $compatLog -Tail 4000) -Encoding utf8
    Write-Log 'trimmed compat.log to its last 4000 lines'
}

$healthy = ($doctorExit -eq 0)
$previous = 'unknown'
if (Test-Path $state) { $previous = (Get-Content $state -Raw).Trim() }
Set-Content -Path $state -Value ($(if ($healthy) { 'healthy' } else { 'unhealthy' })) -Encoding utf8

if (-not $healthy -and $previous -ne 'unhealthy') {
    $detail = 'see maintenance.log and health.json'
    if (Test-Path $health) {
        try {
            $report = Get-Content $health -Raw | ConvertFrom-Json
            $failed = @($report.checks | Where-Object { -not $_.ok } | ForEach-Object { $_.check })
            if ($failed.Count -gt 0) { $detail = 'failing: ' + ($failed -join ', ') }
        } catch {
            # keep the generic detail
        }
    }
    Show-Toast ("Claude Code stack needs attention - " + $detail)
}

Write-Log ($(if ($healthy) { 'healthy' } else { 'unhealthy' }) + " (proxy=$proxyUp shim=$shimUp watchdog=$([bool]$watchdog))")
exit $(if ($healthy) { 0 } else { 1 })
