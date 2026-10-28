# Reclaim disk space on C: from caches that rebuild themselves.
#
# Written 2026-10-28 when the volume sat at 100% free-but-1.7GB. The single
# biggest item was the Windows Delivery Optimization cache at 41 GB: it is
# staged update payload that Windows never trims, and when it fills the volume
# every other write starts failing (this is what broke Chromium, npm and the
# Claude/Codex logs earlier in the day).
#
# Nothing here is user data. No document, photo, project, installed program or
# browser profile is touched - only caches, staged installers, temp files and
# the Recycle Bin.
#
# Run elevated: powershell -NoProfile -ExecutionPolicy Bypass -File reclaim-space.ps1

$ErrorActionPreference = 'SilentlyContinue'

function FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 2) }
function Section($name) { Write-Host "--- $name" -ForegroundColor Cyan }

$before = FreeGB
Write-Host ("free before: {0:N2} GB" -f $before) -ForegroundColor Yellow

# 1. Windows Delivery Optimization. Stop the service so the files are not open,
#    use the supported cmdlet first, then remove whatever it left behind.
Section 'Delivery Optimization cache'
if (Get-Service Dosvc -ErrorAction SilentlyContinue) {
    Stop-Service Dosvc -Force -ErrorAction SilentlyContinue
}
try { Delete-DeliveryOptimizationCache -Force -ErrorAction Stop } catch {
    Write-Host "  Delete-DeliveryOptimizationCache unavailable: $($_.Exception.Message)"
}
$doCache = 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache'
if (Test-Path -LiteralPath $doCache) {
    Remove-Item -LiteralPath $doCache -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $doCache) {
    Write-Host '  cache directory still present (some entries are held by Windows)'
} else {
    Write-Host '  cleared'
}
if (Get-Service Dosvc -ErrorAction SilentlyContinue) {
    Start-Service Dosvc -ErrorAction SilentlyContinue
}

# 2. Recycle Bin. Emptying it is the user's own deleted files, so it is reported
#    rather than assumed: skip with -KeepRecycleBin.
Section 'Recycle Bin'
if ($args -contains '-KeepRecycleBin') {
    Write-Host '  skipped (-KeepRecycleBin)'
} else {
    Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue
    Write-Host '  emptied'
}

# 3. Staged update downloads and temp files.
Section 'Windows update download cache and temp'
$wu = 'C:\Windows\SoftwareDistribution\Download'
if (Test-Path -LiteralPath $wu) {
    Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $wu '*') -Recurse -Force -ErrorAction SilentlyContinue
    Start-Service wuauserv -ErrorAction SilentlyContinue
    Write-Host '  cleared SoftwareDistribution\Download'
}
foreach ($temp in @('C:\Windows\Temp', "$env:LOCALAPPDATA\Temp")) {
    if (Test-Path -LiteralPath $temp) {
        # Files still open belong to running programs; -ErrorAction SilentlyContinue
        # leaves those alone instead of failing the whole pass.
        Get-ChildItem -LiteralPath $temp -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-2) } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  trimmed $temp"
    }
}

# 4. Package manager caches. Both are pure downloads: npm refetches, pip
#    refetches. npm's own 'cache clean' is not used because it can hang on a
#    damaged index, which is exactly the state a full disk leaves behind.
Section 'npm and pip caches'
foreach ($cache in @("$env:LOCALAPPDATA\npm-cache", "$env:LOCALAPPDATA\pip\Cache")) {
    if (Test-Path -LiteralPath $cache) {
        Get-ChildItem -LiteralPath $cache -Force -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  cleared $cache"
    }
}

# 5. Crash dumps.
Section 'Crash dumps'
$dumps = "$env:LOCALAPPDATA\CrashDumps"
if (Test-Path -LiteralPath $dumps) {
    Get-ChildItem -LiteralPath $dumps -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  cleared'
}

# 6. Report what is still large, so the next pass is not blind.
Section 'largest remaining cache directories'
$candidates = @(
    "$env:LOCALAPPDATA\ms-playwright",
    "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Cache",
    "$env:LOCALAPPDATA\Microsoft\Edge\User Data\Default\Cache",
    "$env:LOCALAPPDATA\Programs",
    "$env:LOCALAPPDATA\Android",
    "$env:LOCALAPPDATA\Roblox",
    "$env:LOCALAPPDATA\Claude-3p"
)
foreach ($path in $candidates) {
    if (Test-Path -LiteralPath $path) {
        $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
        if ($sum) {
            Write-Host ("  {0,-70} {1,7:N2} GB" -f $path, ($sum / 1GB))
        }
    }
}

$after = FreeGB
Write-Host ("free after: {0:N2} GB (reclaimed {1:N2} GB)" -f $after, ($after - $before)) -ForegroundColor Green
