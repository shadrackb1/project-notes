' Start the Claude Code proxy watchdog with no visible window.
' Safe to run repeatedly: watchdog.ps1 holds a named mutex, so extra copies exit.
'
' PowerShell does not expand $env:APPDATA inside a -File argument, so the old
' version asked for a file literally named "$env:APPDATA\.claude-nim\...".
' The watchdog never started, which left the proxy and the compat shim with no
' supervisor after a reboot. Both paths below are absolute for the same reason.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\ADMIN\.claude-nim\watchdog.ps1""", 0, False
