@echo off
REM NoteHelper Stop - kills the running server on the configured PORT
REM Auto-elevates (admin) only if PORT < 1024 (e.g. port 80)
cd /d "%~dp0"

set PORT=5000
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="PORT" set PORT=%%b
    )
)

set "STOP_CMD=-ExecutionPolicy Bypass -Command \"$p=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if($p){$p|ForEach-Object{Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue};Write-Host '  Server on port %PORT% stopped.' -ForegroundColor Green}else{Write-Host '  No server running on port %PORT%.' -ForegroundColor Yellow};Start-Sleep -Seconds 1\""

if %PORT% LSS 1024 (
    powershell -Command "Start-Process powershell -ArgumentList '%STOP_CMD%' -Verb RunAs"
) else (
    powershell %STOP_CMD%
)
