# ---------------------------------------------------------------------------
# The keeper: a hidden, single-instance loop that runs maintenance.ps1 every
# 10 minutes for as long as the session lasts.
#
# This is the layer that replaces "remember to check on it". The watchdog
# watches the proxy; nothing watched the watchdog, and nothing verified that
# settings.json still had the settings the whole setup depends on. Both now
# happen here.
#
# Design notes that matter:
#   * the named mutex is Local\ (no admin needed) so login cannot start two.
#   * maintenance runs in a CHILD powershell, because maintenance.ps1 ends in
#     `exit` and would otherwise terminate this loop on its first run.
#   * the loop never exits on error. A broken run is a log line, not a dead
#     keeper, so the next ten minutes still get their attempt.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Continue'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$maintenance = Join-Path $nimDir 'maintenance.ps1'
$log = Join-Path $nimDir 'keeper.log'
$powershell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

function Write-Log($message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    $info = Get-Item $log -ErrorAction SilentlyContinue
    if ($info -and $info.Length -gt 256KB) {
        Set-Content -Path $log -Value (Get-Content $log -Tail 400) -Encoding utf8
    }
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\ClaudeNimKeeper')
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    Write-Log 'keeper already running; exiting'
    exit 0
}

Write-Log 'keeper started'
$interval = 600
if ($env:CLAUDE_KEEPER_SECONDS) { $interval = [int]$env:CLAUDE_KEEPER_SECONDS }

while ($true) {
    try {
        & $powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $maintenance 2>&1 | Out-Null
        Write-Log ("maintenance exit {0}" -f $LASTEXITCODE)
    } catch {
        Write-Log ("maintenance threw: {0}" -f $_.Exception.Message)
    }
    Start-Sleep -Seconds $interval
}
