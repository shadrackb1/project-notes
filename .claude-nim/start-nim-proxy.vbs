' Start every layer of the Claude Code stack, silently, at login.
'
' Previous version relied on `shell.Run(..., 0, False) = 0` to decide whether
' the proxy was already healthy. With bWaitOnReturn = False, Run always returns
' 0 immediately, so that branch always won and the proxy was never started:
' only the watchdog came up, and the first `claude` after a reboot failed until
' the watchdog noticed (up to a minute later). This version waits for each step
' and returns the real exit code.
'
' Layers:
'   1. LiteLLM proxy  :8788   (start-proxy.ps1, idempotent, waits for health)
'   2. compat shim    :8791   (compat.py, idempotent: exits if the port is taken)
'   3. watchdog               (named mutex: extra copies exit immediately)
'   4. maintenance keeper      (runs maintenance.ps1 every 10 minutes)
'
' Every path is literal: the Startup folder runs with a minimal PATH.

Option Explicit

Dim shell, powershell, wscriptExe, base

Set shell = CreateObject("WScript.Shell")
base = "C:\Users\ADMIN\.claude-nim"
powershell = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
wscriptExe = "C:\Windows\System32\wscript.exe"

If Not CreateObject("Scripting.FileSystemObject").FileExists(powershell) Then
    WScript.Echo "ERROR: powershell.exe not found at " & powershell
    WScript.Quit 1
End If

' 1. Proxy. start-proxy.ps1 exits 0 when the proxy answers, and waits up to
'    120s when it has to start it, so the wait here is real work, not a sleep.
Dim proxyExit
proxyExit = shell.Run( _
    """" & powershell & """ -NoProfile -ExecutionPolicy Bypass -File """ & base & "\start-proxy.ps1""", _
    0, True)

' A non-zero exit is not fatal for the rest of the stack: the shim and the
' watchdog still get started so that the next `claude` can recover on its own.
If proxyExit <> 0 Then
    shell.Run """" & powershell & """ -NoProfile -Command ""Add-Content -Path '" & base & "\heal.log' -Value 'login: start-proxy.ps1 did not report ready'""", 0, True
End If

' 2. Compatibility shim.
shell.Run """" & wscriptExe & """ """ & base & "\start-compat.vbs""", 0, False

' 3. Watchdog (single instance via named mutex).
shell.Run """" & wscriptExe & """ """ & base & "\start-watchdog.vbs""", 0, False

' 4. Maintenance keeper: verifies settings/config, raises dead layers, prunes
'    logs, and reports through a toast when something needs a human.
shell.Run """" & wscriptExe & """ """ & base & "\start-keeper.vbs""", 0, False

WScript.Quit proxyExit
