# Sales Buddy - Database Backup Script
# Copies the database to OneDrive with daily/weekly/monthly rotation.
#
# Usage:
#   .\scripts\backup.ps1              Run a backup now
#   .\scripts\backup.ps1 -Setup       Set up scheduled task + configure OneDrive path
#   .\scripts\backup.ps1 -Remove      Remove the scheduled task
#   .\scripts\backup.ps1 -Status      Show backup status and recent backups
#
# Configuration lives in the database (user_preferences table). Defaults:
#   - 7 daily backups, 4 weekly backups, 3 monthly backups
#   - Backups go to OneDrive - Microsoft/Backups/SalesBuddy/
#
# Entry points:
#   backup.bat              Double-click to run a backup now
#   restore.bat             Double-click to restore from a backup

param(
    [switch]$Setup,     # Configure OneDrive path and register scheduled task
    [switch]$Remove,    # Remove the scheduled task
    [switch]$Status,    # Show backup status
    [switch]$Silent     # Suppress interactive prompts (for scheduled task execution)
)

$RepoRoot = Split-Path $PSScriptRoot -Parent
$DataDir = Join-Path $RepoRoot 'data'
$DbFile = Join-Path $DataDir 'salesbuddy.db'
$TaskName = 'SalesBuddy-DailyBackup'

# ==============================================================================
# Helper Functions
# ==============================================================================

function Get-PythonExe {
    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }
    return $pythonExe
}

function Get-BackupPrefs {
    <#
    .SYNOPSIS
    Read backup preferences from the database via Python.
    Returns a hashtable with onedrive_path, retention (daily/weekly/monthly).
    #>
    $pythonExe = Get-PythonExe
    $script = @"
import sqlite3, json, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    c = conn.cursor()
    c.execute('SELECT onedrive_path, backup_retention_daily, backup_retention_weekly, backup_retention_monthly FROM user_preferences LIMIT 1')
    row = c.fetchone()
    conn.close()
    if row:
        print(json.dumps({'onedrive_path': row[0] or '', 'retention': {'daily': row[1] or 7, 'weekly': row[2] or 4, 'monthly': row[3] or 3}}))
    else:
        print(json.dumps({'onedrive_path': '', 'retention': {'daily': 7, 'weekly': 4, 'monthly': 3}}))
except Exception:
    print(json.dumps({'onedrive_path': '', 'retention': {'daily': 7, 'weekly': 4, 'monthly': 3}}))
"@
    try {
        $result = & $pythonExe -c $script $DbFile 2>$null
        if ($result) { return ($result | ConvertFrom-Json) }
    } catch {}
    return @{ onedrive_path = ''; retention = @{ daily = 7; weekly = 4; monthly = 3 } }
}

function Set-OneDrivePath {
    <#
    .SYNOPSIS
    Store the OneDrive path in the database.
    #>
    param([string]$Path)
    $pythonExe = Get-PythonExe
    $escapedPath = $Path -replace "'", "''"
    $script = @"
import sqlite3, sys
db = sys.argv[1]
path = sys.argv[2]
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute('UPDATE user_preferences SET onedrive_path = ? WHERE id = (SELECT id FROM user_preferences LIMIT 1)', (path,))
if c.rowcount == 0:
    c.execute('INSERT INTO user_preferences (onedrive_path, dark_mode, customer_view_grouped, customer_sort_by, topic_sort_by_calls, territory_view_accounts, show_customers_without_calls, first_run_modal_dismissed, guided_tour_completed, fy_transition_active, fy_sync_complete, backup_retention_daily, backup_retention_weekly, backup_retention_monthly) VALUES (?, 1, 0, ''alphabetical'', 0, 0, 1, 0, 0, 0, 0, 7, 4, 3)', (path,))
conn.commit()
conn.close()
"@
    & $pythonExe -c $script $DbFile $Path 2>$null
}

function Find-OneDrivePath {
    <#
    .SYNOPSIS
    Detect the corporate (business) OneDrive folder path.
    Only returns OneDrive for Business paths, not personal OneDrive.
    Checks: $env:OneDriveCommercial > registry Business1 > folder scan (business names only)
    All candidates must resolve to a folder named 'OneDrive - Microsoft' to ensure
    we pick the Microsoft corporate account, not a partner tenant.
    #>
    $expectedName = 'OneDrive - Microsoft'

    # Priority 1: OneDriveCommercial env var (always corporate)
    if ($env:OneDriveCommercial -and (Test-Path $env:OneDriveCommercial)) {
        if ((Split-Path $env:OneDriveCommercial -Leaf) -eq $expectedName) {
            return $env:OneDriveCommercial
        }
    }

    # Priority 2: Registry Business1 account (always corporate)
    try {
        $regPath = "HKCU:\Software\Microsoft\OneDrive\Accounts\Business1"
        if (Test-Path $regPath) {
            $folder = (Get-ItemProperty $regPath -ErrorAction SilentlyContinue).UserFolder
            if ($folder -and (Test-Path $folder) -and (Split-Path $folder -Leaf) -eq $expectedName) {
                return $folder
            }
        }
    } catch {}

    # Priority 3: Scan user profile for the Microsoft corporate OneDrive folder
    # Employees may have multiple OneDrive for Business accounts; we only want Microsoft.
    $candidates = Get-ChildItem $env:USERPROFILE -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $expectedName }
    if ($candidates) {
        return ($candidates | Select-Object -First 1).FullName
    }

    return $null
}

function Get-DbStats {
    <#
    .SYNOPSIS
    Open a SQLite database read-only and return record counts for key tables.
    #>
    param([string]$DbPath)

    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) {
        # Fallback: try system python
        $pythonExe = 'python'
    }

    $script = @"
import sqlite3, json, os, sys
db = sys.argv[1]
conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
c = conn.cursor()
stats = {}
tables = {'customers': 'Customers',
          'notes': 'Notes', 'milestones': 'Milestones'}
for table, label in tables.items():
    try:
        c.execute(f'SELECT COUNT(*) FROM {table}')
        stats[label] = c.fetchone()[0]
    except:
        pass
# Fallback: old backups still have call_logs (renamed to notes)
if 'Notes' not in stats:
    try:
        c.execute('SELECT COUNT(*) FROM call_logs')
        stats['Notes'] = c.fetchone()[0]
    except:
        pass
# Active engagements count
try:
    c.execute("SELECT COUNT(*) FROM engagements WHERE status = 'Active'")
    stats['Active Engagements'] = c.fetchone()[0]
except:
    pass
conn.close()
print(json.dumps(stats))
"@

    try {
        $result = & $pythonExe -c $script $DbPath 2>$null
        if ($result) {
            return ($result | ConvertFrom-Json)
        }
    } catch {}
    return $null
}

function Get-BackupFiles {
    <#
    .SYNOPSIS
    List all backup files in the backup directory, sorted newest first.
    #>
    param([string]$BackupDir)

    if (-not $BackupDir -or -not (Test-Path $BackupDir)) { return @() }

    return Get-ChildItem $BackupDir -Filter 'salesbuddy_*.db' -File |
        Sort-Object LastWriteTime -Descending
}

function Remove-OldBackups {
    <#
    .SYNOPSIS
    Apply retention policy: keep N daily, N weekly, N monthly backups.
    Daily = any backup. Weekly = most recent per ISO week. Monthly = most recent per month.
    #>
    param(
        [string]$BackupDir,
        [int]$KeepDaily = 7,
        [int]$KeepWeekly = 4,
        [int]$KeepMonthly = 3
    )

    $allFiles = Get-BackupFiles -BackupDir $BackupDir
    if ($allFiles.Count -eq 0) { return }

    $keep = @{}  # Track files to keep by full path

    # Keep the N most recent as "daily"
    $dailyKeep = $allFiles | Select-Object -First $KeepDaily
    foreach ($f in $dailyKeep) { $keep[$f.FullName] = 'daily' }

    # Weekly: group by ISO year-week, keep most recent from each, up to N weeks
    $weekGroups = $allFiles | Group-Object {
        $d = $_.LastWriteTime
        $cal = [System.Globalization.CultureInfo]::InvariantCulture.Calendar
        $week = $cal.GetWeekOfYear($d, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [DayOfWeek]::Monday)
        "{0}-W{1:D2}" -f $d.Year, $week
    } | Select-Object -First $KeepWeekly

    foreach ($g in $weekGroups) {
        $newest = $g.Group | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $keep[$newest.FullName] = 'weekly'
    }

    # Monthly: group by year-month, keep most recent from each, up to N months
    $monthGroups = $allFiles | Group-Object {
        $_.LastWriteTime.ToString('yyyy-MM')
    } | Select-Object -First $KeepMonthly

    foreach ($g in $monthGroups) {
        $newest = $g.Group | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $keep[$newest.FullName] = 'monthly'
    }

    # Delete everything not in the keep set
    $deleted = 0
    foreach ($f in $allFiles) {
        if (-not $keep.ContainsKey($f.FullName)) {
            Remove-Item $f.FullName -Force
            $deleted++
        }
    }

    if ($deleted -gt 0 -and -not $Silent) {
        Write-Host "  Cleaned up $deleted old backup(s)." -ForegroundColor Gray
    }
}

function Run-Backup {
    <#
    .SYNOPSIS
    Copy the database to the backup directory with timestamp, then apply retention.
    #>
    $prefs = Get-BackupPrefs

    if (-not $prefs.onedrive_path) {
        # Fallback: try runtime detection
        $detected = Find-OneDrivePath
        if ($detected) {
            $prefs.onedrive_path = $detected
        } else {
            Write-Host "  [ERROR] Backups not configured. Run: .\scripts\backup.ps1 -Setup" -ForegroundColor Red
            return $false
        }
    }

    if (-not (Test-Path $DbFile)) {
        Write-Host "  [ERROR] Database not found: $DbFile" -ForegroundColor Red
        return $false
    }

    $backupDir = Join-Path $prefs.onedrive_path 'Backups\SalesBuddy'
    if (-not (Test-Path $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    }

    $timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $backupFile = Join-Path $backupDir "salesbuddy_$timestamp.db"

    try {
        Copy-Item $DbFile $backupFile -Force
        # Copy-Item preserves the source file's LastWriteTime, so every backup
        # would show the same modified timestamp. Touch it so the backup file's
        # modification time reflects when the backup was actually taken.
        (Get-Item $backupFile).LastWriteTime = Get-Date
        $size = (Get-Item $backupFile).Length
        $sizeMB = [math]::Round($size / 1MB, 1)

        if (-not $Silent) {
            Write-Host "  [OK] Backup saved: $backupFile ($sizeMB MB)" -ForegroundColor Green
        }

        # Apply retention policy
        Remove-OldBackups -BackupDir $backupDir `
            -KeepDaily $prefs.retention.daily `
            -KeepWeekly $prefs.retention.weekly `
            -KeepMonthly $prefs.retention.monthly

        return $true
    } catch {
        Write-Host "  [ERROR] Backup failed: $_" -ForegroundColor Red
        return $false
    }
}

# ==============================================================================
# -Setup: Configure backup location and register scheduled task
# ==============================================================================

if ($Setup) {
    Write-Host ""
    Write-Host "  Sales Buddy Backup Setup" -ForegroundColor Cyan
    Write-Host "  =======================" -ForegroundColor Cyan
    Write-Host ""

    # Detect OneDrive
    $onedrivePath = Find-OneDrivePath
    if (-not $onedrivePath) {
        Write-Host "  [ERROR] Could not find OneDrive folder." -ForegroundColor Red
        Write-Host "  Make sure OneDrive is installed and signed in." -ForegroundColor Gray
        Write-Host ""
        Read-Host "  Press Enter to close"
        exit 1
    }

    $defaultBackupDir = Join-Path $onedrivePath 'Backups\SalesBuddy'
    Write-Host "  Detected OneDrive: $onedrivePath" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Backups will be saved to:" -ForegroundColor White
    Write-Host "  $defaultBackupDir" -ForegroundColor Cyan
    Write-Host ""
    $confirm = Read-Host "  Continue? (Y/n)"

    if ($confirm -eq 'n' -or $confirm -eq 'N') {
        Write-Host "  Setup cancelled." -ForegroundColor Yellow
        exit 0
    }

    # Create the backup directory
    if (-not (Test-Path $defaultBackupDir)) {
        New-Item -ItemType Directory -Path $defaultBackupDir -Force | Out-Null
        Write-Host "  [OK] Created backup folder." -ForegroundColor Green
    }

    # Save OneDrive path to database
    Set-OneDrivePath -Path $onedrivePath

    # Register scheduled task
    Write-Host ""
    Write-Host "  Setting up daily automatic backup..." -ForegroundColor Yellow

    $scriptPath = Join-Path $PSScriptRoot 'backup.ps1'
    $vbsLauncher = Join-Path $PSScriptRoot 'run-hidden.vbs'
    # Use wscript + VBS launcher so the powershell console window never flashes.
    # (powershell.exe -WindowStyle Hidden still shows a brief console flash;
    #  wscript starts the process with no console at all.)
    $action = New-ScheduledTaskAction `
        -Execute 'wscript.exe' `
        -Argument "`"$vbsLauncher`" `"$scriptPath`" -Silent" `
        -WorkingDirectory $RepoRoot

    # Run daily at 11:00 AM
    $trigger = New-ScheduledTaskTrigger -Daily -At '11:00AM'

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    # Remove existing task if present
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    # Try SYSTEM first (reliable on Entra ID cloud-joined machines, but needs admin).
    # Fall back to Interactive if not elevated. Interactive works when the user is
    # logged in, and StartWhenAvailable catches up on missed runs.
    # NEVER use S4U - it silently fails on Entra-only accounts (no creds/profile).
    $registered = $false
    try {
        $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Daily backup of Sales Buddy database to OneDrive' `
            -ErrorAction Stop | Out-Null
        $registered = $true
        Write-Host "  [OK] Scheduled task '$TaskName' registered (runs as SYSTEM)." -ForegroundColor Green
        Write-Host "       Runs daily at 11:00 AM." -ForegroundColor Gray
    } catch {
        # SYSTEM requires admin — fall back to Interactive (runs when logged in)
        try {
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Principal $principal `
                -Settings $settings `
                -Description 'Daily backup of Sales Buddy database to OneDrive' `
                -ErrorAction Stop | Out-Null
            $registered = $true
            Write-Host "  [OK] Scheduled task '$TaskName' registered (runs while logged in)." -ForegroundColor Green
            Write-Host "       Runs daily at 11:00 AM. Tip: run setup as Admin for SYSTEM mode." -ForegroundColor Gray
        } catch {
            Write-Host "  [WARNING] Could not register scheduled task: $_" -ForegroundColor Yellow
            Write-Host "            You can still run backups manually with backup.bat" -ForegroundColor Gray
        }
    }

    # Run first backup now
    Write-Host ""
    Write-Host "  Running first backup..." -ForegroundColor Yellow
    $success = Run-Backup
    if ($success) {
        Write-Host ""
        Write-Host "  Backup setup complete!" -ForegroundColor Green
        Write-Host ""
        Write-Host "  Your backups live here:" -ForegroundColor White
        Write-Host "  $defaultBackupDir" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  This folder syncs to OneDrive automatically." -ForegroundColor Gray
        Write-Host "  You can access your backups from any device" -ForegroundColor Gray
        Write-Host "  signed into your Microsoft account." -ForegroundColor Gray
    }

    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 0
}

# ==============================================================================
# -Remove: Unregister the scheduled task
# ==============================================================================

if ($Remove) {
    Write-Host ""
    Write-Host "  Removing Sales Buddy backup scheduled task..." -ForegroundColor Yellow
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  [OK] Scheduled task removed." -ForegroundColor Green
    } catch {
        Write-Host "  [WARNING] Task not found or already removed." -ForegroundColor Yellow
    }

    # Clear OneDrive path from database
    Set-OneDrivePath -Path ''

    Write-Host "  Existing backups in OneDrive have NOT been deleted." -ForegroundColor Gray
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 0
}

# ==============================================================================
# -Status: Show backup information
# ==============================================================================

if ($Status) {
    Write-Host ""
    Write-Host "  Sales Buddy Backup Status" -ForegroundColor Cyan
    Write-Host "  ========================" -ForegroundColor Cyan
    Write-Host ""

    $prefs = Get-BackupPrefs

    if (-not $prefs.onedrive_path) {
        Write-Host "  Backups: NOT CONFIGURED" -ForegroundColor Yellow
        Write-Host "  Run: .\scripts\backup.ps1 -Setup" -ForegroundColor Gray
        Write-Host ""
        exit 0
    }

    $backupStatusDir = Join-Path $prefs.onedrive_path 'Backups\SalesBuddy'
    Write-Host "  Backup folder: $backupStatusDir" -ForegroundColor White

    # Live-check the scheduled task in Windows Task Scheduler
    $liveTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($liveTask) {
        $taskInfo = $liveTask | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
        $nextRun = if ($taskInfo -and $taskInfo.NextRunTime) { $taskInfo.NextRunTime.ToString('yyyy-MM-dd h:mm tt') } else { 'Unknown' }
        Write-Host "  Scheduled task: Active (next run: $nextRun)" -ForegroundColor Green
    } else {
        Write-Host "  Scheduled task: Not registered" -ForegroundColor Yellow
        Write-Host "                  Run start.bat to register, or .\scripts\backup.ps1 -Setup" -ForegroundColor Gray
    }

    # Deduce last backup from newest file
    $backups = Get-BackupFiles -BackupDir $backupStatusDir
    $lastBackupStr = 'Never'
    if ($backups.Count -gt 0) {
        $lastBackupStr = $backups[0].LastWriteTime.ToString('yyyy-MM-dd h:mm tt')
    }
    Write-Host "  Last backup: $lastBackupStr" -ForegroundColor White
    Write-Host "  Retention: $($prefs.retention.daily) daily, $($prefs.retention.weekly) weekly, $($prefs.retention.monthly) monthly" -ForegroundColor White
    Write-Host ""

    if ($backups.Count -eq 0) {
        Write-Host "  No backups found." -ForegroundColor Yellow
    } else {
        Write-Host "  Recent backups:" -ForegroundColor White
        $backups | Select-Object -First 10 | ForEach-Object {
            $sizeMB = [math]::Round($_.Length / 1MB, 1)
            $date = $_.LastWriteTime.ToString('yyyy-MM-dd h:mm tt')
            Write-Host "    $date  $sizeMB MB  $($_.Name)" -ForegroundColor Gray
        }
        Write-Host ""
        Write-Host "  Total: $($backups.Count) backup(s)" -ForegroundColor White
    }

    Write-Host ""
    exit 0
}

# ==============================================================================
# Default: Run a backup now
# ==============================================================================

Write-Host ""
Write-Host "  Sales Buddy Backup" -ForegroundColor Cyan
Write-Host "  =================" -ForegroundColor Cyan
Write-Host ""

# If not configured, offer to run setup instead of just erroring
$prefs = Get-BackupPrefs
if (-not $prefs.onedrive_path -and -not $Silent) {
    # Try runtime detection before prompting
    $detected = Find-OneDrivePath
    if (-not $detected) {
        Write-Host "  Backups are not configured yet." -ForegroundColor Yellow
        Write-Host ""
        $response = Read-Host "  Would you like to set up automatic backups now? (Y/n)"
        if ($response -eq '' -or $response -eq 'Y' -or $response -eq 'y') {
            & $PSCommandPath -Setup
            exit $LASTEXITCODE
        } else {
            Write-Host "  [SKIP] Run with -Setup when you're ready." -ForegroundColor Gray
            Write-Host ""
            Read-Host "  Press Enter to close"
            exit 0
        }
    }
}

$success = Run-Backup

if (-not $Silent) {
    Write-Host ""
    if (-not $success) {
        Read-Host "  Press Enter to close"
    }
}

exit $(if ($success) { 0 } else { 1 })
