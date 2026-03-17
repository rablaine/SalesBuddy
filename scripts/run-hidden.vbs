' Silent launcher for scheduled tasks.
' Runs a PowerShell script with no visible window.
'
' Usage (from Task Scheduler):
'   wscript.exe "C:\path\to\run-hidden.vbs" "C:\path\to\script.ps1"

If WScript.Arguments.Count = 0 Then
    WScript.Quit 1
End If

Dim shell, scriptPath, command
Set shell = CreateObject("WScript.Shell")
scriptPath = WScript.Arguments(0)
command = "powershell.exe -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File """ & scriptPath & """"
shell.Run command, 0, True
