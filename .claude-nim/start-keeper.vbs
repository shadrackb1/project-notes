' Start the maintenance keeper with no visible window.
' Safe to run repeatedly: keeper.ps1 holds a named mutex, so extra copies exit.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\ADMIN\.claude-nim\keeper.ps1""", 0, False
