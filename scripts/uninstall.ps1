# NoteHelper - Uninstall Script
# Removes all NoteHelper scheduled tasks and stops the running server.
# Does NOT delete the app files, database, or OneDrive backups.
#
# Usage:
#   uninstall.bat                Double-click to run
#   .\scripts\uninstall.ps1     Run directly from PowerShell

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

Write-Host ""
Write-Host "  NoteHelper Uninstall" -ForegroundColor Cyan
Write-Host "  ====================" -ForegroundColor Cyan
Write-Host ""

# -- Stop the running server ---------------------------------------------------
# Read port from .env
$Port = 5151
$envFile = Join-Path $RepoRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*PORT\s*=\s*(\d+)') { $Port = [int]$Matches[1] }
    }
}

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "  Stopping server on port $Port..." -ForegroundColor Yellow
    $procIds = @($conn | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($p in $procIds) {
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $still) {
        Write-Host "  [OK] Server stopped." -ForegroundColor Green
    } else {
        Write-Host "  [WARNING] Server may still be running on port $Port." -ForegroundColor Yellow
    }
} else {
    Write-Host "  [OK] No server running on port $Port." -ForegroundColor Green
}

# -- Remove scheduled tasks ----------------------------------------------------
$taskNames = @('NoteHelper-AutoStart', 'NoteHelper-DailyBackup')
$removedAny = $false

foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Write-Host "  [OK] Removed scheduled task: $taskName" -ForegroundColor Green
            $removedAny = $true
        } catch {
            Write-Host "  [WARNING] Could not remove task '$taskName': $_" -ForegroundColor Yellow
            Write-Host "            Open Task Scheduler and remove it manually." -ForegroundColor Gray
        }
    } else {
        Write-Host "  [OK] No task found: $taskName (already removed)" -ForegroundColor Green
    }
}

# -- Summary -------------------------------------------------------------------
Write-Host ""
Write-Host "  Uninstall complete." -ForegroundColor Green
Write-Host ""
Write-Host "  What was removed:" -ForegroundColor Gray
Write-Host "    - Server process (stopped)" -ForegroundColor Gray
Write-Host "    - Scheduled tasks (NoteHelper-AutoStart, NoteHelper-DailyBackup)" -ForegroundColor Gray
Write-Host ""
Write-Host "  What was NOT removed:" -ForegroundColor Gray
Write-Host "    - App files (this folder: $RepoRoot)" -ForegroundColor Gray
Write-Host "    - Database (data\notehelper.db)" -ForegroundColor Gray
Write-Host "    - OneDrive backups" -ForegroundColor Gray
Write-Host ""
Write-Host "  To fully remove NoteHelper, delete this folder:" -ForegroundColor Gray
Write-Host "    $RepoRoot" -ForegroundColor Yellow
Write-Host ""

Write-Host "Press any key to close..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
