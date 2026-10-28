# Find what is eating C: when no process shows the writes.
#
# Written 2026-10-28, after free space fell from 3.2 GB to 0.62 GB in minutes
# while per-process write counters showed almost nothing. Two things can grow
# like that without appearing in any per-process counter or folder listing:
#
#   * Volume Shadow Copy (System Restore) storage - C:\System Volume Information
#   * The NTFS change journal ($UsnJrnl) - grows with file churn, invisible
#
# This only reports. It deletes nothing; the decisions are separate and need to
# be explicit.
#
# Run elevated: powershell -NoProfile -ExecutionPolicy Bypass -File disk-forensics.ps1

$ErrorActionPreference = 'SilentlyContinue'

Write-Host ("free: {0:N2} GB" -f ((Get-PSDrive C).Free / 1GB)) -ForegroundColor Yellow

Write-Host "`n--- shadow copy storage" -ForegroundColor Cyan
$storage = Get-CimInstance Win32_ShadowStorage
if ($storage) {
    foreach ($s in $storage) {
        Write-Host ("  used {0:N2} GB   max {1:N2} GB" -f ($s.UsedSpace / 1GB), ($s.MaxSpace / 1GB))
    }
} else {
    Write-Host '  no shadow storage reported'
}
$shadows = @(Get-CimInstance Win32_ShadowCopy)
Write-Host ("  restore points: {0}" -f $shadows.Count)

Write-Host "`n--- NTFS change journal" -ForegroundColor Cyan
& fsutil usn queryjournal C: 2>&1 | ForEach-Object { Write-Host "  $_" }

Write-Host "`n--- largest files written in the last 60 minutes" -ForegroundColor Cyan
$roots = @('C:\Windows', 'C:\ProgramData', 'C:\Users\ADMIN\AppData', 'C:\Recovery', 'C:\Users\ADMIN\Desktop', 'C:\Users\ADMIN\Documents')
foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-60) -and $_.Length -gt 100MB } |
        Sort-Object Length -Descending | Select-Object -First 5 |
        ForEach-Object {
            Write-Host ("  {0,8:N0} MB  {1}  {2}" -f ($_.Length / 1MB), $_.LastWriteTime.ToString('HH:mm'), $_.FullName)
        }
}

Write-Host "`n--- top-level folder sizes (C:)" -ForegroundColor Cyan
Get-ChildItem C:\ -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $sum = (Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum
    if ($sum -gt 1GB) {
        [pscustomobject]@{ GB = [math]::Round($sum / 1GB, 2); Path = $_.FullName }
    }
} | Sort-Object GB -Descending | ForEach-Object { Write-Host ("  {0,7:N2} GB  {1}" -f $_.GB, $_.Path) }

Write-Host ("`nfree after: {0:N2} GB" -f ((Get-PSDrive C).Free / 1GB)) -ForegroundColor Yellow
