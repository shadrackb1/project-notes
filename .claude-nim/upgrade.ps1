# ---------------------------------------------------------------------------
# Controlled upgrade with rollback.
#
# The failure mode this prevents: a background `pip install --upgrade litellm`
# (or a reinstall) changes a dependency, the proxy stops starting, and the next
# Claude Code session fails with "Connection refused" and no clue why. Here an
# upgrade is only kept if the proxy comes back AND a real tool call succeeds;
# otherwise the previous version is reinstalled automatically.
#
#   powershell -File upgrade.ps1                     # latest litellm
#   powershell -File upgrade.ps1 -Version 1.106.0    # exact version
#   powershell -File upgrade.ps1 -Package mcp -Version 2.3.0
#   powershell -File upgrade.ps1 -Check              # report, change nothing
#
# Exit: 0 = at target and healthy, 1 = rollback happened or still broken
# ---------------------------------------------------------------------------

param(
    [string]$Package = 'litellm',
    [string]$Version = '',
    [switch]$Check
)

$ErrorActionPreference = 'Continue'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$python = $env:NIM_PYTHON_EXE
if (-not $python) { $python = 'C:\Python314\python.exe' }
if (-not (Test-Path $python)) { $python = 'python' }
$log = Join-Path $nimDir 'upgrade.log'

function Write-Log($message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $message
}

function Get-Version($name) {
    & $python -c "import importlib.metadata as m; print(m.version('$name'))" 2>$null
}

function Restart-Proxy {
    Get-CimInstance Win32_Process -Filter "Name='litellm.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { & taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
    Start-Sleep -Seconds 3
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $nimDir 'start-proxy.ps1') | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-Stack {
    $out = & $python (Join-Path $nimDir 'doctor.py') --smoke 2>&1 | Out-String
    Write-Log ($out.Trim() -replace "`r?`n", ' | ')
    return ($LASTEXITCODE -eq 0)
}

$current = Get-Version $Package
if (-not $current) { Write-Log "package not installed: $Package"; exit 1 }
Write-Log "$Package $current installed"

if ($Check) {
    $latest = (& $python -m pip index versions $Package 2>$null | Select-Object -First 1)
    Write-Log "latest reported by pip: $latest"
    exit 0
}

$target = if ($Version) { $Version } else { '' }
if ($target -and $target -eq $current) {
    Write-Log "already at $target; verifying the stack instead"
    if (Test-Stack) { exit 0 } else { exit 1 }
}

Write-Log "installing $Package $target"
$spec = if ($target) { "$Package==$target" } else { "--upgrade $Package" }
$install = & $python -m pip install --quiet $spec.Split(' ') 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Log "install failed: $($install.Trim() -replace "`r?`n", ' | ')"
    exit 1
}

$new = Get-Version $Package
Write-Log "$Package is now $new"

$ready = Restart-Proxy
if (-not $ready) { Write-Log 'proxy did not become ready after the upgrade' }

if ($ready -and (Test-Stack)) {
    Write-Log "upgrade to $new verified"
    & $python (Join-Path $nimDir 'doctor.py') --write-lock | Out-Null
    exit 0
}

Write-Log "upgrade to $new failed verification; rolling back to $current"
& $python -m pip install --quiet "$Package==$current" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Log "ROLLBACK INSTALL FAILED; reinstall by hand: $python -m pip install $Package==$current"
    exit 1
}
Restart-Proxy | Out-Null
if (Test-Stack) {
    Write-Log "rolled back to $current and healthy"
} else {
    Write-Log "still unhealthy after rollback; run doctor.py and read litellm-*.log"
}
exit 1
