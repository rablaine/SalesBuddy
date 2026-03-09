@echo off
REM NoteHelper Launcher - double-click to start
REM Auto-elevates (admin) only if PORT in .env is below 1024 (e.g. port 80)
cd /d "%~dp0"

set PORT=5151
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="PORT" set PORT=%%b
    )
)

if %PORT% LSS 1024 (
    powershell -Command "Start-Process powershell -ArgumentList '-ExecutionPolicy Bypass -File \"%~dp0scripts\server.ps1\"' -Verb RunAs"
) else (
    powershell -ExecutionPolicy Bypass -File "%~dp0scripts\server.ps1"
)
