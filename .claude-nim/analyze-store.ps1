# Report what Windows itself can give back, without changing anything.
#
# Two reclaimable items that never show up as "files you can delete":
#   * WinSxS superseded component backups - DISM reports what is reclaimable
#   * hiberfil.sys - sized to installed RAM, only useful if hibernation is used
#
# Read-only: this prints numbers so the decision can be made with them.
# Run elevated: powershell -NoProfile -ExecutionPolicy Bypass -File analyze-store.ps1

$ErrorActionPreference = 'SilentlyContinue'

Write-Host ("free: {0:N2} GB" -f ((Get-PSDrive C).Free / 1GB)) -ForegroundColor Yellow

Write-Host "`n--- component store (DISM)" -ForegroundColor Cyan
$out = & dism.exe /Online /Cleanup-Image /AnalyzeComponentStore 2>&1
$out | Where-Object { $_ -match 'Size|Reclaimable|Recommend|Actual|Shared|Backups|Cache|Component Store' } |
    ForEach-Object { Write-Host "  $_" }

Write-Host "`n--- hibernation" -ForegroundColor Cyan
& powercfg.exe /a 2>&1 | Select-Object -First 6 | ForEach-Object { Write-Host "  $_" }
$hib = Get-Item -LiteralPath 'C:\hiberfil.sys' -Force -ErrorAction SilentlyContinue
if ($hib) {
    Write-Host ("  hiberfil.sys: {0:N2} GB" -f ($hib.Length / 1GB))
} else {
    Write-Host '  hiberfil.sys: not present'
}

Write-Host "`n--- page file" -ForegroundColor Cyan
Get-CimInstance Win32_PageFileUsage | ForEach-Object {
    Write-Host ("  {0}: {1} MB current, {2} MB peak" -f $_.Name, $_.CurrentUsage, $_.PeakUsage)
}

Write-Host "`n--- winsxs size" -ForegroundColor Cyan
$w = (Get-ChildItem 'C:\Windows\WinSxS' -Recurse -File -Force -ErrorAction SilentlyContinue |
    Measure-Object Length -Sum).Sum
Write-Host ("  {0:N2} GB on disk (hard links make this larger than the real cost)" -f ($w / 1GB))

Write-Host ("`nfree after: {0:N2} GB" -f ((Get-PSDrive C).Free / 1GB)) -ForegroundColor Yellow
