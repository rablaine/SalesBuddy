' Silent launcher for scheduled tasks.
' Runs a PowerShell script with no visible window (no console flash).
'
' Usage (from Task Scheduler):
'   wscript.exe "C:\path\to\run-hidden.vbs" "C:\path\to\script.ps1" [extra args...]

If WScript.Arguments.Count = 0 Then
    WScript.Quit 1
End If

Dim shell, scriptPath, extraArgs, command, exitCode, i
Set shell = CreateObject("WScript.Shell")
scriptPath = WScript.Arguments(0)

extraArgs = ""
For i = 1 To WScript.Arguments.Count - 1
    extraArgs = extraArgs & " """ & WScript.Arguments(i) & """"
Next

command = "powershell.exe -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File """ & scriptPath & """" & extraArgs
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
