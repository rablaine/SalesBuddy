@echo off
REM MSI custom action wrapper - calls uninstall.ps1 with full logging.
REM Save the script path before changing directory (cmd needs to not lock the install dir).
set "SCRIPTPATH=%~dp0uninstall.ps1"
set "LOGFILE=%TEMP%\SalesBuddy-Uninstall.log"

echo [%date% %time%] === MSI Uninstall Action Started === >> "%LOGFILE%"
echo Script dir: %~dp0 >> "%LOGFILE%"

REM Change to TEMP so cmd.exe doesn't hold a lock on the install directory.
cd /d "%TEMP%"

echo [%date% %time%] Launching PowerShell... >> "%LOGFILE%"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPTPATH%" -Silent 2>> "%LOGFILE%"
set "PSEXIT=%ERRORLEVEL%"

echo [%date% %time%] PowerShell exit code: %PSEXIT% >> "%LOGFILE%"
exit %PSEXIT%
