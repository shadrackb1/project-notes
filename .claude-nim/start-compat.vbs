' Start the Claude Code compatibility shim (:8791) with no visible window.
' Safe to run repeatedly: compat.py exits cleanly if the port is already taken.
'
' Every path is literal. The previous version passed '$env:NIM_PYTHON_EXE'
' (single-quoted, so PowerShell never expanded it) to Start-Process. The shim
' was therefore never launched, and after a reboot every claude request died
' with ECONNREFUSED on 127.0.0.1:8791.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Python314\python.exe"" ""C:\Users\ADMIN\.claude-nim\compat.py""", 0, False
