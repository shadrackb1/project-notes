# Start the SECOND Claude Code proxy instance (port 8789).
#
# Two instances run the same config.yaml on adjacent ports and the compat shim
# on :8791 fails over between them, so one wedged or restarting proxy no longer
# stops Claude Code and Codex.
#
# This is deliberately a separate script from start-proxy.ps1 rather than a
# -Port parameter: that script's stale-process sweep kills every litellm it can
# match, which would take the healthy instance down with the broken one. Here
# only processes belonging to 8789 are touched.
#
# Exit codes: 0 = ready, 1 = failed

$ErrorActionPreference = 'Stop'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$port = 8789
$probe = "http://127.0.0.1:$port/health/liveliness"

function Test-Proxy2 {
    try {
        $r = Invoke-RestMethod -Uri $probe -TimeoutSec 3
        return ($null -ne $r)
    } catch {
        return $false
    }
}

if (Test-Proxy2) { exit 0 }

# --- stop only this instance's leftovers -----------------------------------
# A hard-killed litellm can leave a worker behind that keeps the port bound
# while no main process exists. Matching on the command line (and never on the
# port owner alone, which can be a worker) keeps the primary untouched.
try {
    $staggers = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'litellm' -and $_.CommandLine -match "--port $port\b" }
    foreach ($p in $staggers) {
        Write-Host "Stopping stale standby proxy process $($p.ProcessId)."
        & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
    }
    if ($staggers) { Start-Sleep -Seconds 2 }
} catch {
    Write-Host "could not inspect standby processes: $($_.Exception.Message)"
}

foreach ($name in @('LITELLM_MASTER_KEY', 'NVIDIA_API_KEY', 'NVIDIA_API_KEY_2')) {
    if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
        $val = [Environment]::GetEnvironmentVariable($name, 'User')
        if ($val) { [Environment]::SetEnvironmentVariable($name, $val, 'Process') }
    }
}

$launcher = Join-Path $nimDir 'run-proxy.bat'
if (-not (Test-Path $launcher)) {
    Write-Error "run-proxy.bat is missing from $nimDir"
    exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $nimDir "litellm-standby-$stamp.log"

$child = Start-Process -FilePath $launcher `
    -ArgumentList @("`"$log`"", '1', "$port") `
    -WindowStyle Hidden -PassThru

# Bounded wait, abandoned as soon as the child exits. See start-proxy.ps1 for
# why the old flat 120s window was worse than useless: it made a crash at
# second 8 look like a hang for two minutes.
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Proxy2) { exit 0 }
    if ($child -and $child.HasExited) {
        Write-Host "Standby proxy exited after $($i + 1)s without binding :$port."
        break
    }
}

Write-Host "Standby proxy did not become ready. Last lines of ${log}:"
Get-Content -LiteralPath $log -Tail 25 -ErrorAction SilentlyContinue | Write-Host
if (Select-String -LiteralPath $log -Pattern 'UnicodeEncodeError' -Quiet -ErrorAction SilentlyContinue) {
    Write-Host 'Cause: startup crashed on the Unicode banner (stdout is not UTF-8).'
}
exit 1
