# ---------------------------------------------------------------------------
# Self-heal + housekeeping for the Claude and Codex stacks that run this proxy.
# Keep both side by side; never remove the Claude Code layer to make room for Codex.
#
# Safe to run as often as you like: every action checks before it acts, and the
# launchers it calls are idempotent. Scheduled every 10 minutes by
# register-tasks.ps1, which is what turns "it broke while I was away" into a
# line in maintenance.log instead of a lost afternoon.
#
# What it does, in order:
#   1. raise the stack if a layer is down (Claude proxy, shim, watchdog; Codex proxy, shim, watchdog)
#   2. re-assert config.yaml, settings.json and the Claude Desktop gateway profile
#   3. run doctor.py and record the result
#   4. prune logs so the directory cannot grow without bound
#   5. toast once when the stack is unhealthy, not once per run
#
# Exit codes: 0 = healthy, 1 = problems remain
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Continue'

$claudeDir = $env:CLAUDE_PROXY_DIR
if (-not $claudeDir) { $claudeDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) '.claude-nim' }
$python = $env:NIM_PYTHON_EXE
if (-not $python) { $python = 'C:\Python314\python.exe' }
if (-not (Test-Path $python)) { $python = 'python' }

$log = Join-Path $claudeDir 'maintenance.log'
$state = Join-Path $claudeDir 'maintenance.state'
$health = Join-Path $claudeDir 'health.json'
$claudeProxyProbe = 'http://127.0.0.1:8788/health/liveliness'
$claudeShimProbe = 'http://127.0.0.1:8791/health/liveliness'

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
        $nui = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced')
        $isDark = ($nui.GetValue('AppsUseLightTheme', 1) -eq 0)
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class ToastIcons {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SHGetStockIconInfo(int siid, int flags, IntPtr pInfo);
}
"@
        $siid = 3
        $icon.ShowBalloonTip(10000, 'Claude Code stack', $message, [System.Windows.Forms.ToolTipIcon]::Warning)

        Start-Sleep -Seconds 1
        $icon.Dispose()

        $codexIcon = New-Object System.Windows.Forms.NotifyIcon
        $codexIcon.Icon = [System.Drawing.SystemIcons]::Information
        $codexIcon.Visible = $true
        $codexIcon.ShowBalloonTip(10000, 'Codex stack', $message, [System.Windows.Forms.ToolTipIcon]::Information)

        Start-Sleep -Seconds 1
        $codexIcon.Dispose()


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

# A watchdog process is not a working watchdog. On 2026-10-28 one copy sat
# blocked forever inside a PowerShell pipeline (its restart piped start-proxy
# into Out-Null, so the child inherited the pipe and the parent waited for the
# proxy to exit). This script saw the process, deferred to it, and logged
# "watchdog owns the restart" every ten minutes for 25 minutes while the proxy
# stayed down and both Claude Code and Codex failed. The watchdog now writes a
# heartbeat file each cycle; only a recent one proves the loop is still turning.
$watchdogFresh = $false
if ($watchdog) {
    $heartbeat = Join-Path $claudeDir 'watchdog.heartbeat'
    $hb = Get-Item -LiteralPath $heartbeat -ErrorAction SilentlyContinue
    if ($hb) {
        $quietMinutes = ((Get-Date) - $hb.LastWriteTime).TotalMinutes
        if ($quietMinutes -lt 4) {
            $watchdogFresh = $true
        } else {
            Write-Log ("watchdog process exists but has not heartbeat for {0:N0} min; treating it as stuck" -f $quietMinutes)
        }
    } else {
        Write-Log 'watchdog process exists but wrote no heartbeat; treating it as stuck'
    }
}

$claudeProxyUp = Test-Url $claudeProxyProbe
$claudeShimUp = Test-Url $claudeShimProbe

if (-not $claudeProxyUp -and -not $watchdogFresh) {
    Write-Log 'claude proxy down and no working watchdog: starting the stack'
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $claudeDir 'start-proxy.ps1') | Out-Null
    } catch {
        Write-Log "start-proxy.ps1 threw: $($_.Exception.Message)"
    }
    $claudeProxyUp = Test-Url $claudeProxyProbe
} elseif (-not $claudeProxyUp) {
    Write-Log 'claude proxy not answering; watchdog owns the restart, leaving it alone'
}

if (-not $claudeShimUp) {
    Write-Log 'claude shim not answering: starting it'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $claudeDir 'start-compat.vbs') + '"') -WindowStyle Hidden
    Start-Sleep -Seconds 3
    $claudeShimUp = Test-Url $claudeShimProbe
}

# The standby proxy on 8789 is what the shim falls back to when the primary
# wedges. The watchdog keeps it up; this is the second pair of hands for the
# case where the watchdog itself is being replaced in this same run.
$standbyProbe = 'http://127.0.0.1:8789/health/liveliness'
$standbyUp = Test-Url $standbyProbe
if (-not $standbyUp) {
    Write-Log 'standby proxy not answering: starting it'
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $claudeDir 'start-proxy2.ps1') *> (Join-Path $claudeDir 'start-proxy2.out.log')
    } catch {
        Write-Log "start-proxy2.ps1 threw: $($_.Exception.Message)"
    }
    $standbyUp = Test-Url $standbyProbe
}

if (-not $watchdog -or -not $watchdogFresh) {
    if ($watchdog -and -not $watchdogFresh) {
        # Killing it is safe: the proxy is started above when the watchdog is not
        # trusted, and the new copy takes over the same mutex.
        Write-Log 'replacing the stuck watchdog'
        try {
            Stop-Process -Id $watchdog.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Log "could not stop the stuck watchdog: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 2
    }
    Write-Log 'claude watchdog not running: starting it'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $claudeDir 'start-watchdog.vbs') + '"') -WindowStyle Hidden
}

# --- codex stack -----------------------------------------------------------
# Codex is a second *client* of this proxy, not a second proxy. Both clients
# share config.yaml, the model catalog and port 8788; there is no .codex-nim.
# Codex keeps no guard scripts of its own, so the only thing to verify is its
# client config: a stale base_url or a default model the catalog dropped is
# exactly the failure that looks like "Codex stopped working" with no error in
# the app.
$masterKey = $env:LITELLM_MASTER_KEY
if (-not $masterKey) { $masterKey = [Environment]::GetEnvironmentVariable('LITELLM_MASTER_KEY', 'User') }

$codexConfig = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\config.toml'
$codexOk = $true
if (-not (Test-Path $codexConfig)) {
    Write-Log 'codex config missing: ~/.codex/config.toml not found'
    $codexOk = $false
} else {
    $codexText = Get-Content -LiteralPath $codexConfig -Raw
    $codexModel = [regex]::Match($codexText, '(?m)^\s*model\s*=\s*"([^"]+)"').Groups[1].Value
    $codexProvider = [regex]::Match($codexText, '(?m)^\s*model_provider\s*=\s*"([^"]+)"').Groups[1].Value
    if (-not $codexModel -or -not $codexProvider) {
        Write-Log 'codex config is missing model or model_provider'
        $codexOk = $false
    } elseif ($codexText -notmatch 'http://127\.0\.0\.1:(8788|8791)/v1') {
        # 8791 is the shim in front of both proxy instances, and is the better
        # target: it fails over. 8788 is accepted for a config that predates
        # the standby, but not anything else (a stray public base_url would
        # silently send the user's traffic somewhere else).
        Write-Log "codex provider '$codexProvider' points at neither the shim on :8791 nor the proxy on :8788"
        $codexOk = $false
    } elseif ($claudeProxyUp) {
        try {
            $headers = @{ Authorization = "Bearer $masterKey" }
            $served = (Invoke-RestMethod -Uri 'http://127.0.0.1:8788/v1/models' -TimeoutSec 8 -Headers $headers).data.id
            if ($served -notcontains $codexModel) {
                Write-Log "codex default model '$codexModel' is not served by the proxy"
                $codexOk = $false
            }
        } catch {
            Write-Log "could not read the model catalog for the codex check: $($_.Exception.Message)"
            $codexOk = $false
        }
    }
}

# Both stacks are idempotent: they restore the known-good state only when the
# live file has drifted, and they say so. git-guard.py belongs here too: it
# re-asserts GIT_CONFIG_GLOBAL and credential.interactive, without which git
# in a shell whose HOME points elsewhere falls back to Git Credential Manager
# and hangs on every push.
foreach ($guard in @('config-guard.py', 'settings-guard.py', 'git-guard.py')) {
    $path = Join-Path $claudeDir $guard
    if (-not (Test-Path $path)) { continue }
    $out = & $python $path 2>&1 | Out-String
    if ($out.Trim()) { Write-Log ("{0}: {1}" -f $guard, ($out.Trim() -replace "`r?`n", ' | ')) }
}

# --- Claude Desktop (third-party inference) --------------------------------
# The Desktop app reads its gateway profile once, at launch, from
# %LOCALAPPDATA%\Claude-3p\configLibrary. An upgrade or a profile reset drops
# that entry and the app falls back to the claude.ai sign-in screen, which
# reads as "the models disappeared". The same pass also re-adds the MCP
# servers from ~/.claude.json and any skill missing from ~/.claude/skills, so
# a profile reset cannot leave Desktop with no tools or no skills. The script
# is silent when everything is already correct and reads the key from HKCU
# itself.
$desktop3p = Join-Path $claudeDir 'desktop-3p.py'
if (Test-Path $desktop3p) {
    $out = & $python $desktop3p ensure --quiet 2>&1 | Out-String
    if ($out.Trim()) { Write-Log ("desktop-3p: " + ($out.Trim() -replace "`r?`n", ' | ')) }
}

# --- python3 alias ---------------------------------------------------------
# Claude often runs `python3`, but python.org installs only `python.exe` and
# Windows has no python3 alias. Keep a copy on the user PATH. A hardlink to
# C:\Python314 is denied by Windows, so refresh the copy when Python changes.
$pyHome = 'C:\Python314\python.exe'
$py3 = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.local\bin\python3.exe'
if ((Test-Path $pyHome) -and (Test-Path (Split-Path $py3))) {
    $refresh = -not (Test-Path $py3)
    if (-not $refresh) {
        $refresh = (Get-Item $pyHome).LastWriteTimeUtc -gt (Get-Item $py3).LastWriteTimeUtc
    }
    if ($refresh) {
        try {
            Copy-Item -LiteralPath $pyHome -Destination $py3 -Force
            Write-Log "refreshed python3.exe from $pyHome"
        } catch {
            Write-Log "could not refresh python3.exe: $($_.Exception.Message)"
        }
    }
}

$doctorOut = & $python (Join-Path $claudeDir 'doctor.py') 2>&1 | Out-String
$doctorExit = $LASTEXITCODE
if ($doctorExit -ne 0) {
    Write-Log ("doctor found problems: {0}" -f ($doctorOut.Trim() -replace "`r?`n", ' | '))
}

# --- Housekeeping ----------------------------------------------------------
# The proxy writes a fresh log per start; keep the newest 20 and drop anything
# older than two weeks so the folder stays readable.
$cutoff = (Get-Date).AddDays(-14)
Get-ChildItem -LiteralPath $claudeDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $claudeDir -Filter 'litellm-*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 20 |
    Remove-Item -Force -ErrorAction SilentlyContinue

# compat.log rotates at 5 MB inside the shim; this catches the case where the
# shim was not restarted for weeks.
$compatLog = Join-Path $claudeDir 'compat.log'
$info = Get-Item $compatLog -ErrorAction SilentlyContinue
if ($info -and $info.Length -gt 4MB) {
    Set-Content -Path $compatLog -Value (Get-Content $compatLog -Tail 4000) -Encoding utf8
    Write-Log 'trimmed compat.log to its last 4000 lines'
}

# --- Disk guard ------------------------------------------------------------
# 2026-10-28: the volume sat at 0.62 GB free because the Windows Delivery
# Optimization cache had grown to 41 GB. Every write then failed somewhere else
# — Chromium pages, npm installs, proxy logs — so the symptom looked like a
# dozen unrelated bugs. This pass reports the state; reclaim-space.ps1 does the
# clearing and needs elevation because the largest item lives in a service
# profile. Nothing here deletes anything.
$diskFreeGB = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
if ($diskFreeGB -lt 5) {
    Write-Log ("low disk: {0:N2} GB free; run reclaim-space.ps1 elevated" -f $diskFreeGB)
    $doCache = 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization'
    if (Test-Path -LiteralPath $doCache) {
        $doSize = (Get-ChildItem -LiteralPath $doCache -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
        if ($doSize) {
            Write-Log ("  Delivery Optimization cache is {0:N2} GB" -f ($doSize / 1GB))
        }
    }
}

# The standby is part of healthy: with one instance the stack still serves, but
# it has no failover, and that is the state that turned a restart into a
# 25-minute outage. Report it rather than hide it.
$healthy = (($doctorExit -eq 0) -and $codexOk -and $standbyUp)
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

Write-Log ($(if ($healthy) { 'healthy' } else { 'unhealthy' }) + " (claude proxy=$claudeProxyUp standby=$standbyUp shim=$claudeShimUp watchdog=$([bool]$watchdog) codex=$codexOk disk=${diskFreeGB}GB)")
exit $(if ($healthy) { 0 } else { 1 })
