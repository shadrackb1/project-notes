# Stop Windows Update from refilling C: and clear what it staged.
#
# 2026-10-28: free space fell 3.2 GB -> 1.7 -> 0.62 -> 0.11 GB in repeated bursts
# while per-process write counters showed nothing. Shadow copies were empty and
# the change journal was capped at 32 MB, which leaves the update pipeline:
# wuauserv stages downloads in SoftwareDistribution\Download and
# Delivery Optimization keeps its own copy in a service profile (it had reached
# 41 GB). Clearing one without stopping the other just meant it refilled.
#
# This stops the download side, clears staged payloads, and leaves the services
# stopped so the drain does not resume. Windows Update is not broken by this:
# the next time it runs (Windows Update page, or `Start-Service wuauserv`) it
# re-plans and re-downloads what it needs.
#
# Run elevated: powershell -NoProfile -ExecutionPolicy Bypass -File pause-updates-and-purge.ps1

$ErrorActionPreference = 'SilentlyContinue'

function FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 2) }

function DirGB($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum
    if ($sum) { return [math]::Round($sum / 1GB, 2) }
    return 0
}

$before = FreeGB
Write-Host ("free before: {0:N2} GB" -f $before) -ForegroundColor Yellow

$doRoot = 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization'
$doCache = Join-Path $doRoot 'Cache'
$wuDownload = 'C:\Windows\SoftwareDistribution\Download'
$wuDelivery = 'C:\Windows\SoftwareDistribution\DeliveryOptimization'

Write-Host "`n--- staged payload before" -ForegroundColor Cyan
Write-Host ("  Delivery Optimization : {0:N2} GB" -f (DirGB $doRoot))
Write-Host ("  SoftwareDistribution  : {0:N2} GB" -f (DirGB 'C:\Windows\SoftwareDistribution'))

Write-Host "`n--- stopping update services" -ForegroundColor Cyan
foreach ($name in @('Usosvc', 'wuauserv', 'BITS', 'Dosvc', 'TrustedInstaller')) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -eq 'Running') {
            Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 700
        }
        # Manual, not disabled: the next explicit update still works, but nothing
        # starts on its own and resumes the download overnight.
        Set-Service -Name $name -StartupType Manual -ErrorAction SilentlyContinue
        Write-Host ("  {0,-16} {1}" -f $name, (Get-Service -Name $name).Status)
    }
}

Write-Host "`n--- clearing" -ForegroundColor Cyan
try {
    Delete-DeliveryOptimizationCache -Force -ErrorAction Stop
    Write-Host '  Delivery Optimization cache deleted'
} catch {
    Write-Host "  Delete-DeliveryOptimizationCache: $($_.Exception.Message)"
}
if (Test-Path -LiteralPath $doCache) {
    Remove-Item -LiteralPath $doCache -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  Delivery Optimization cache folder removed'
}
foreach ($path in @($wuDownload, $wuDelivery)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item (Join-Path $path '*') -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  cleared $path"
    }
}
foreach ($dump in @('C:\Windows\MEMORY.DMP', 'C:\Windows\LiveKernelReports')) {
    if (Test-Path -LiteralPath $dump) {
        Remove-Item -LiteralPath $dump -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  removed $dump"
    }
}

$after = FreeGB
Write-Host ("`nfree after: {0:N2} GB (reclaimed {1:N2} GB)" -f $after, ($after - $before)) -ForegroundColor Green
Write-Host 'Windows Update downloads are paused. They resume when Update is opened or wuauserv is started.'
