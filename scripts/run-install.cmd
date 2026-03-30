@echo off
REM MSI custom action wrapper - calls install.ps1 with full logging.
REM %~dp0 = this batch file's directory (e.g. C:\Users\X\AppData\Local\SalesBuddy\scripts\)
set "LOGFILE=%TEMP%\SalesBuddy-Install.log"

echo [%date% %time%] === MSI Install Action Started === >> "%LOGFILE%"
echo Script dir: %~dp0 >> "%LOGFILE%"
echo Listing scripts directory: >> "%LOGFILE%"
dir "%~dp0" >> "%LOGFILE%" 2>&1
echo. >> "%LOGFILE%"

echo [%date% %time%] Launching PowerShell... >> "%LOGFILE%"
REM Don't redirect stdout - Write-Log already writes to the log file via Add-Content,
REM and Write-Host output should be visible in the terminal so the user sees progress.
REM Only redirect stderr to the log for diagnostics.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Shortcuts -DesktopShortcut -LaunchBrowser 2>> "%LOGFILE%"
set "PSEXIT=%ERRORLEVEL%"

echo [%date% %time%] PowerShell exit code: %PSEXIT% >> "%LOGFILE%"
exit %PSEXIT%
