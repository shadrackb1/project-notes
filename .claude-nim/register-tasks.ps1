# ---------------------------------------------------------------------------
# Register the two Windows scheduled tasks that keep this stack alive.
#
#   ClaudeNim-Stack        at logon        start proxy + shim + watchdog
#   ClaudeNim-Maintenance  every 10 min    maintenance.ps1 (self-heal + doctor)
#
# Why tasks as well as the Startup folder: a Startup entry runs once, has no
# restart policy, and is silently skipped if Explorer is slow or the entry is
# removed by a cleanup tool. A task has restart-on-failure and can run again
# after logon. The launchers are idempotent (health probe first, named mutex
# for the watchdog), so the two paths cannot start duplicate processes.
#
# Run once. The HKCU autostart entry needs no rights; the scheduled tasks do
# (some Windows builds refuse task creation for standard users), so a denial
# there is reported and skipped rather than treated as a failure of the setup.
#   powershell -NoProfile -ExecutionPolicy Bypass -File register-tasks.ps1
#   ... -Check        show the current registration
#   ... -Unregister   remove the tasks and the autostart entry
# ---------------------------------------------------------------------------

param(
    [switch]$Check,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$nimDir = $env:CLAUDE_PROXY_DIR
if (-not $nimDir) { $nimDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$wscript = 'C:\Windows\System32\wscript.exe'
$powershell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

$stackTask = 'ClaudeNim-Stack'
$maintenanceTask = 'ClaudeNim-Maintenance'

if ($Check) {
    $run = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).ClaudeNimStack
    "autostart (HKCU Run): {0}" -f $(if ($run) { $run } else { 'NOT SET' })
    Get-ScheduledTask -TaskName $stackTask, $maintenanceTask -ErrorAction SilentlyContinue |
        ForEach-Object {
            $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
            "{0,-24} {1,-10} last={2} result={3} next={4}" -f `
                $_.TaskName, $_.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime
        }
    exit 0
}

if ($Unregister) {
    Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ClaudeNimStack' -ErrorAction SilentlyContinue
    Write-Host 'removed autostart entry'
    foreach ($name in @($stackTask, $maintenanceTask)) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed $name"
    }
    exit 0
}

# --- Autostart (no elevation) ----------------------------------------------
# A second logon trigger beside the Startup-folder shortcut. Both run the same
# idempotent launcher, so a double start costs nothing and a Startup folder
# cleared by a cleanup tool no longer matters.
$launcher = '"' + $wscript + '" "' + (Join-Path $nimDir 'start-nim-proxy.vbs') + '"'
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
    -Name 'ClaudeNimStack' -Value $launcher -Type String
Write-Host "autostart entry set: $launcher"

foreach ($path in @($wscript, $powershell, (Join-Path $nimDir 'start-nim-proxy.vbs'),
        (Join-Path $nimDir 'maintenance.ps1'))) {
    if (-not (Test-Path $path)) { throw "missing required file: $path" }
}

# Restart-on-failure plus "run as soon as possible after a missed schedule" is
# the difference between a task that heals and one that merely logs that it
# wanted to run while the machine was asleep.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$settings.Hidden = $true

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

# --- Stack -----------------------------------------------------------------
$stackAction = New-ScheduledTaskAction `
    -Execute $wscript `
    -Argument ('"' + (Join-Path $nimDir 'start-nim-proxy.vbs') + '"') `
    -WorkingDirectory $nimDir
$stackTrigger = New-ScheduledTaskTrigger -AtLogOn
$stackTrigger.Delay = 'PT20S'

Register-ScheduledTask -TaskName $stackTask `
    -Action $stackAction `
    -Trigger $stackTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Start the Claude Code proxy, compat shim and watchdog at logon.' `
    -Force -ErrorAction SilentlyContinue | Out-Null
if ($?) {
    Write-Host "registered $stackTask"
} else {
    $tasksBlocked = $true
}

# --- Maintenance -----------------------------------------------------------
$maintAction = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
        (Join-Path $nimDir 'maintenance.ps1') + '"') `
    -WorkingDirectory $nimDir

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$logonTrigger.Delay = 'PT2M'

$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName $maintenanceTask `
    -Action $maintAction `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description 'Self-heal and verify the Claude Code stack every 10 minutes.' `
    -Force -ErrorAction SilentlyContinue | Out-Null
if ($?) {
    Write-Host "registered $maintenanceTask"
    Start-ScheduledTask -TaskName $maintenanceTask
    Write-Host 'ran maintenance once; see maintenance.log'
} else {
    $tasksBlocked = $true
}

if ($tasksBlocked) {
    Write-Host ''
    Write-Host 'Scheduled tasks were refused (needs an elevated shell on this build).'
    Write-Host 'Nothing is broken: the startup launcher also starts keeper.ps1, which'
    Write-Host 'runs the same maintenance every 10 minutes.'
    Write-Host 'To add the task layer anyway, run this file from an Administrator shell.'
    exit 2
}
