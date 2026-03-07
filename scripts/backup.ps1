# NoteHelper - Database Backup Script
# Copies the database to OneDrive with daily/weekly/monthly rotation.
#
# Usage:
#   .\scripts\backup.ps1              Run a backup now
#   .\scripts\backup.ps1 -Setup       Set up scheduled task + configure OneDrive path
#   .\scripts\backup.ps1 -Remove      Remove the scheduled task
#   .\scripts\backup.ps1 -Status      Show backup status and recent backups
#
# Configuration lives in data/backup_config.json. Defaults:
#   - 7 daily backups, 4 weekly backups, 3 monthly backups
#   - Backups go to OneDrive - Microsoft/Backups/NoteHelper/
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
$ConfigFile = Join-Path $DataDir 'backup_config.json'
$DbFile = Join-Path $DataDir 'notehelper.db'
$TaskName = 'NoteHelper-DailyBackup'

# ==============================================================================
# Helper Functions
# ==============================================================================

function Get-BackupConfig {
    <#
    .SYNOPSIS
    Load backup configuration from JSON, creating defaults if missing.
    #>
    $defaults = @{
        enabled = $false
        onedrive_path = ''
        backup_dir = ''
        retention = @{
            daily = 7
            weekly = 4
            monthly = 3
        }
        last_backup = $null
        task_registered = $false
    }

    if (Test-Path $ConfigFile) {
        try {
            $config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
            # Merge with defaults for any missing keys
            $result = @{}
            foreach ($key in $defaults.Keys) {
                if ($null -ne $config.$key) {
                    if ($key -eq 'retention') {
                        $result[$key] = @{
                            daily = if ($config.retention.daily) { $config.retention.daily } else { 7 }
                            weekly = if ($config.retention.weekly) { $config.retention.weekly } else { 4 }
                            monthly = if ($config.retention.monthly) { $config.retention.monthly } else { 3 }
                        }
                    } else {
                        $result[$key] = $config.$key
                    }
                } else {
                    $result[$key] = $defaults[$key]
                }
            }
            return $result
        } catch {
            Write-Host "  [WARNING] Could not parse backup_config.json, using defaults." -ForegroundColor Yellow
        }
    }
    return $defaults
}

function Save-BackupConfig {
    param([hashtable]$Config)
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
    $Config | ConvertTo-Json -Depth 3 | Set-Content $ConfigFile -Encoding UTF8
}

function Find-OneDrivePath {
    <#
    .SYNOPSIS
    Detect the corporate (business) OneDrive folder path.
    Only returns OneDrive for Business paths, not personal OneDrive.
    Checks: $env:OneDriveCommercial > registry Business1 > folder scan (business names only)
    #>
    # Priority 1: OneDriveCommercial env var (always corporate)
    if ($env:OneDriveCommercial -and (Test-Path $env:OneDriveCommercial)) {
        return $env:OneDriveCommercial
    }

    # Priority 2: Registry Business1 account (always corporate)
    try {
        $regPath = "HKCU:\Software\Microsoft\OneDrive\Accounts\Business1"
        if (Test-Path $regPath) {
            $folder = (Get-ItemProperty $regPath -ErrorAction SilentlyContinue).UserFolder
            if ($folder -and (Test-Path $folder)) { return $folder }
        }
    } catch {}

    # Priority 3: Scan user profile for the Microsoft corporate OneDrive folder
    # Employees may have multiple OneDrive for Business accounts; we only want Microsoft.
    $candidates = Get-ChildItem $env:USERPROFILE -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'OneDrive - Microsoft' }
    if ($candidates) {
        return ($candidates | Sort-Object { $_.Name.Length } -Descending |
            Select-Object -First 1).FullName
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
tables = {'call_logs': 'Call Logs', 'customers': 'Customers', 'sellers': 'Sellers',
          'customer_revenue_data': 'Revenue Records', 'milestones': 'Milestones',
          'opportunities': 'Opportunities'}
for table, label in tables.items():
    try:
        c.execute(f'SELECT COUNT(*) FROM {table}')
        stats[label] = c.fetchone()[0]
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

    return Get-ChildItem $BackupDir -Filter 'notehelper_*.db' -File |
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
    $config = Get-BackupConfig

    if (-not $config.backup_dir) {
        Write-Host "  [ERROR] Backups not configured. Run: .\scripts\backup.ps1 -Setup" -ForegroundColor Red
        return $false
    }

    if (-not (Test-Path $DbFile)) {
        Write-Host "  [ERROR] Database not found: $DbFile" -ForegroundColor Red
        return $false
    }

    $backupDir = $config.backup_dir
    if (-not (Test-Path $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    }

    $timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $backupFile = Join-Path $backupDir "notehelper_$timestamp.db"

    try {
        Copy-Item $DbFile $backupFile -Force
        $size = (Get-Item $backupFile).Length
        $sizeMB = [math]::Round($size / 1MB, 1)

        if (-not $Silent) {
            Write-Host "  [OK] Backup saved: $backupFile ($sizeMB MB)" -ForegroundColor Green
        }

        # Update config with last backup time
        $config.last_backup = (Get-Date).ToString('o')
        Save-BackupConfig $config

        # Apply retention policy
        Remove-OldBackups -BackupDir $backupDir `
            -KeepDaily $config.retention.daily `
            -KeepWeekly $config.retention.weekly `
            -KeepMonthly $config.retention.monthly

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
    Write-Host "  NoteHelper Backup Setup" -ForegroundColor Cyan
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

    $defaultBackupDir = Join-Path $onedrivePath 'Backups\NoteHelper'
    Write-Host "  Detected OneDrive: $onedrivePath" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Backups will be saved to:" -ForegroundColor White
    Write-Host "  $defaultBackupDir" -ForegroundColor Cyan
    Write-Host ""
    $confirm = Read-Host "  Is this correct? (Y/n, or enter a different full path)"

    if ($confirm -eq 'n' -or $confirm -eq 'N') {
        Write-Host "  Setup cancelled." -ForegroundColor Yellow
        exit 0
    } elseif ($confirm -and $confirm -ne 'Y' -and $confirm -ne 'y' -and $confirm -ne '') {
        # User entered a custom path
        $defaultBackupDir = $confirm.Trim('"').Trim("'")
    }

    # Create the backup directory
    if (-not (Test-Path $defaultBackupDir)) {
        New-Item -ItemType Directory -Path $defaultBackupDir -Force | Out-Null
        Write-Host "  [OK] Created backup folder." -ForegroundColor Green
    }

    # Save config
    $config = Get-BackupConfig
    $config.enabled = $true
    $config.onedrive_path = $onedrivePath
    $config.backup_dir = $defaultBackupDir
    Save-BackupConfig $config

    # Register scheduled task
    Write-Host ""
    Write-Host "  Setting up daily automatic backup..." -ForegroundColor Yellow

    $scriptPath = Join-Path $PSScriptRoot 'backup.ps1'
    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Silent" `
        -WorkingDirectory $RepoRoot

    # Run daily at 2 AM
    $trigger = New-ScheduledTaskTrigger -Daily -At '11:00AM'

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    # Remove existing task if present
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    # Try S4U first (runs whether logged in or not, but requires admin)
    # Fall back to Interactive (runs only when logged in, no admin needed)
    $registered = $false
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Daily backup of NoteHelper database to OneDrive' `
            -ErrorAction Stop | Out-Null
        $registered = $true
        Write-Host "  [OK] Scheduled task '$TaskName' registered." -ForegroundColor Green
        Write-Host "       Runs daily at 11:00 AM (even when logged out)." -ForegroundColor Gray
    } catch {
        # S4U requires admin -- fall back to Interactive
        try {
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Principal $principal `
                -Settings $settings `
                -Description 'Daily backup of NoteHelper database to OneDrive' `
                -ErrorAction Stop | Out-Null
            $registered = $true
            Write-Host "  [OK] Scheduled task '$TaskName' registered." -ForegroundColor Green
            Write-Host "       Runs daily at 11:00 AM (while you're logged in)." -ForegroundColor Gray
        } catch {
            Write-Host "  [WARNING] Could not register scheduled task: $_" -ForegroundColor Yellow
            Write-Host "            You can still run backups manually with backup.bat" -ForegroundColor Gray
        }
    }

    if ($registered) {
        $config.task_registered = $true
        Save-BackupConfig $config
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
    Write-Host "  Removing NoteHelper backup scheduled task..." -ForegroundColor Yellow
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  [OK] Scheduled task removed." -ForegroundColor Green
    } catch {
        Write-Host "  [WARNING] Task not found or already removed." -ForegroundColor Yellow
    }

    $config = Get-BackupConfig
    $config.task_registered = $false
    $config.enabled = $false
    Save-BackupConfig $config

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
    Write-Host "  NoteHelper Backup Status" -ForegroundColor Cyan
    Write-Host "  ========================" -ForegroundColor Cyan
    Write-Host ""

    $config = Get-BackupConfig

    if (-not $config.enabled) {
        Write-Host "  Backups: NOT CONFIGURED" -ForegroundColor Yellow
        Write-Host "  Run: .\scripts\backup.ps1 -Setup" -ForegroundColor Gray
        Write-Host ""
        exit 0
    }

    Write-Host "  Backup folder: $($config.backup_dir)" -ForegroundColor White
    Write-Host "  Scheduled task: $(if ($config.task_registered) { 'Active (daily at 2 AM)' } else { 'Not registered' })" -ForegroundColor White
    Write-Host "  Last backup: $(if ($config.last_backup) { [datetime]::Parse($config.last_backup).ToString('yyyy-MM-dd h:mm tt') } else { 'Never' })" -ForegroundColor White
    Write-Host "  Retention: $($config.retention.daily) daily, $($config.retention.weekly) weekly, $($config.retention.monthly) monthly" -ForegroundColor White
    Write-Host ""

    $backups = Get-BackupFiles -BackupDir $config.backup_dir
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
Write-Host "  NoteHelper Backup" -ForegroundColor Cyan
Write-Host "  =================" -ForegroundColor Cyan
Write-Host ""

# If not configured, offer to run setup instead of just erroring
$config = Get-BackupConfig
if (-not $config.backup_dir -and -not $Silent) {
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

$success = Run-Backup

if (-not $Silent) {
    Write-Host ""
    if (-not $success) {
        Read-Host "  Press Enter to close"
    }
}

exit $(if ($success) { 0 } else { 1 })
