# Sales Buddy - Uninstall Script
# Removes all Sales Buddy scheduled tasks, shortcuts, and stops the running server.
# Optionally removes app files (prompts user unless -Silent).
#
# Usage:
#   uninstall.bat                       Double-click to run (interactive)
#   .\scripts\uninstall.ps1             Run directly from PowerShell (interactive)
#   .\scripts\uninstall.ps1 -Silent     MSI uninstall mode (no prompts, removes everything)

param(
    [switch]$Silent    # Skip prompts, remove everything (used by MSI uninstaller)
)

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

Write-Host ""
Write-Host "  Sales Buddy Uninstall" -ForegroundColor Cyan
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
$taskNames = @('SalesBuddy-AutoStart', 'SalesBuddy-DailyBackup')
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

# -- Remove shortcuts ----------------------------------------------------------
$StartMenuFolder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Sales Buddy'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sales Buddy.lnk'

if (Test-Path $StartMenuFolder) {
    Remove-Item $StartMenuFolder -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  [OK] Removed Start Menu shortcuts." -ForegroundColor Green
} else {
    Write-Host "  [OK] No Start Menu shortcuts found." -ForegroundColor Green
}

if (Test-Path $DesktopShortcut) {
    Remove-Item $DesktopShortcut -Force -ErrorAction SilentlyContinue
    Write-Host "  [OK] Removed desktop shortcut." -ForegroundColor Green
} else {
    Write-Host "  [OK] No desktop shortcut found." -ForegroundColor Green
}

# -- Remove app files (optional) -----------------------------------------------
# When called with -Silent (MSI uninstall), skip file removal entirely.
# The MSI's RemoveFiles action handles deleting the install directory.
# We only need to handle the git-cloned repo at %LOCALAPPDATA%\SalesBuddy.
$removeFiles = $false
if ($Silent) {
    # MSI handles file removal. Backup the database so the user doesn't lose data.
    $clonedRepo = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
    $dbFile = Join-Path $clonedRepo 'data\salesbuddy.db'
    if (Test-Path $dbFile) {
        $dbBackup = Join-Path $env:TEMP "salesbuddy-uninstall-$(Get-Date -Format 'yyyyMMdd-HHmmss').db"
        Copy-Item $dbFile $dbBackup -ErrorAction SilentlyContinue
        Write-Host "  [OK] Database backed up to $dbBackup" -ForegroundColor Green
    }
    # Remove the cloned repo (separate from the MSI install directory)
    # Use cmd rmdir which is orders of magnitude faster than Remove-Item -Recurse
    if ((Test-Path $clonedRepo) -and ($clonedRepo -ne $RepoRoot)) {
        Write-Host "  Removing app files (this may take a moment)..." -ForegroundColor Yellow
        cmd /c "rmdir /s /q `"$clonedRepo`"" 2>$null
        if (-not (Test-Path $clonedRepo)) {
            Write-Host "  [OK] Cloned repository removed." -ForegroundColor Green
        } else {
            Write-Host "  [WARNING] Some files could not be removed (may be in use)." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host ""
    $response = Read-Host "  Delete app files at $RepoRoot? Your database will be preserved. (y/N)"
    if ($response -eq 'y' -or $response -eq 'Y') {
        $removeFiles = $true
    }
}

if ($removeFiles) {
    # Preserve the database by copying it to a temp location first
    $dbFile = Join-Path $RepoRoot 'data\salesbuddy.db'
    $dbBackup = $null
    if (Test-Path $dbFile) {
        $dbBackup = Join-Path $env:TEMP "salesbuddy-uninstall-$(Get-Date -Format 'yyyyMMdd-HHmmss').db"
        Copy-Item $dbFile $dbBackup -ErrorAction SilentlyContinue
    }

    # Remove the app directory
    # Use cmd rmdir which is orders of magnitude faster than Remove-Item -Recurse
    if (Test-Path $RepoRoot) {
        Write-Host "  Removing app files (this may take a moment)..." -ForegroundColor Yellow
        cmd /c "rmdir /s /q `"$RepoRoot`"" 2>$null
        if (-not (Test-Path $RepoRoot)) {
            Write-Host "  [OK] App files removed." -ForegroundColor Green
        } else {
            Write-Host "  [WARNING] Some files could not be removed (may be in use)." -ForegroundColor Yellow
            Write-Host "            Close all Sales Buddy windows and delete manually:" -ForegroundColor Gray
            Write-Host "            $RepoRoot" -ForegroundColor Yellow
        }
    }

    # Tell user where the database backup is
    if ($dbBackup -and (Test-Path $dbBackup)) {
        Write-Host ""
        Write-Host "  Your database was saved to:" -ForegroundColor Cyan
        Write-Host "    $dbBackup" -ForegroundColor Yellow
        Write-Host "  You can restore it by copying it to a new installation's data\ folder." -ForegroundColor Gray
    }
}

# -- Summary -------------------------------------------------------------------
Write-Host ""
Write-Host "  Uninstall complete." -ForegroundColor Green
Write-Host ""
Write-Host "  What was removed:" -ForegroundColor Gray
Write-Host "    - Server process (stopped)" -ForegroundColor Gray
Write-Host "    - Scheduled tasks (SalesBuddy-AutoStart, SalesBuddy-DailyBackup)" -ForegroundColor Gray
Write-Host "    - Start Menu and desktop shortcuts" -ForegroundColor Gray
if ($removeFiles) {
    Write-Host "    - App files" -ForegroundColor Gray
}
Write-Host ""
Write-Host "  What was NOT removed:" -ForegroundColor Gray
if (-not $removeFiles) {
    Write-Host "    - App files (this folder: $RepoRoot)" -ForegroundColor Gray
    Write-Host "    - Database (data\salesbuddy.db)" -ForegroundColor Gray
}
Write-Host "    - OneDrive backups" -ForegroundColor Gray
Write-Host ""

if (-not $Silent -and [Environment]::UserInteractive -and $Host.Name -ne 'Default Host') {
    Write-Host "Press any key to close..." -ForegroundColor Gray
    try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch {}
}
