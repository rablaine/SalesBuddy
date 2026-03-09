# NoteHelper - Database Restore Script
# Interactive terminal UI to browse backups and restore one.
#
# Usage:
#   .\scripts\restore.ps1              Interactive menu to pick and restore a backup
#
# What it does:
#   1. Lists available backups with date, size, and database stats
#   2. Lets you pick one to restore
#   3. Stops the server (if running)
#   4. Backs up the current database as a safety net
#   5. Copies the selected backup over the current database
#   6. Restarts the server
#
# Entry point:
#   restore.bat             Double-click launcher (auto-elevates if needed)

$RepoRoot = Split-Path $PSScriptRoot -Parent
$DataDir = Join-Path $RepoRoot 'data'
$ConfigFile = Join-Path $DataDir 'backup_config.json'
$DbFile = Join-Path $DataDir 'notehelper.db'

Set-Location $RepoRoot

# ==============================================================================
# Helper Functions
# ==============================================================================

function Read-EnvFile {
    $config = @{}
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$') {
                $config[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $config
}

function Test-ServerRunning {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conn
}

function Stop-Server {
    param([int]$Port)
    Write-Host "  Stopping server on port $Port..." -ForegroundColor Yellow
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $procIds = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($p in $procIds) {
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
        $retries = 0
        while ($retries -lt 5) {
            Start-Sleep -Seconds 1
            $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if (-not $still) { return }
            $still | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
            $retries++
        }
    }
}

function Start-Server {
    param([int]$Port)
    $waitress = Join-Path $RepoRoot 'venv\Scripts\waitress-serve.exe'
    if (-not (Test-Path $waitress)) {
        Write-Host "  [WARNING] Waitress not found. Start manually with start.bat" -ForegroundColor Yellow
        return
    }
    $serverArgs = @('--host=0.0.0.0', "--port=$Port", '--call', 'app:create_app')
    Write-Host "  Starting server on port $Port..." -ForegroundColor Yellow
    Start-Process -FilePath $waitress -ArgumentList $serverArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (Test-ServerRunning -Port $Port) {
        Write-Host "  [OK] Server running at http://localhost:$Port" -ForegroundColor Green
    } else {
        Write-Host "  Server may still be starting..." -ForegroundColor Yellow
    }
}

function Get-DbStats {
    param([string]$DbPath)

    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }

    $script = @"
import sqlite3, json, sys
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
        if ($result) { return ($result | ConvertFrom-Json) }
    } catch {}
    return $null
}

function Get-BackupConfig {
    if (Test-Path $ConfigFile) {
        try { return Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch {}
    }
    return $null
}

function Get-AllBackupFiles {
    <#
    .SYNOPSIS
    Find all backups from OneDrive backup dir AND local data/ dir.
    #>
    $files = @()

    # OneDrive backups
    $config = Get-BackupConfig
    if ($config -and $config.backup_dir -and (Test-Path $config.backup_dir)) {
        $files += Get-ChildItem $config.backup_dir -Filter 'notehelper_*.db' -File |
            ForEach-Object { $_ | Add-Member -NotePropertyName Source -NotePropertyValue 'OneDrive' -PassThru }
    }

    # Local data/ backups (from deploy/update cycle)
    if (Test-Path $DataDir) {
        $localFiles = Get-ChildItem $DataDir -Filter 'notehelper_backup_*.db' -File |
            ForEach-Object { $_ | Add-Member -NotePropertyName Source -NotePropertyValue 'Local' -PassThru }
        $files += $localFiles
    }

    # Deduplicate by name (prefer OneDrive copy) and sort newest first
    $seen = @{}
    $unique = @()
    foreach ($f in ($files | Sort-Object LastWriteTime -Descending)) {
        if (-not $seen.ContainsKey($f.Name)) {
            $seen[$f.Name] = $true
            $unique += $f
        }
    }

    return $unique
}

# ==============================================================================
# Main: Interactive Restore
# ==============================================================================

Write-Host ""
Write-Host "  NoteHelper Restore" -ForegroundColor Cyan
Write-Host "  ==================" -ForegroundColor Cyan
Write-Host ""

# Find all backups
$backups = Get-AllBackupFiles

if ($backups.Count -eq 0) {
    Write-Host "  No backups found." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Looked in:" -ForegroundColor Gray
    $config = Get-BackupConfig
    if ($config -and $config.backup_dir) {
        Write-Host "    - $($config.backup_dir) (OneDrive)" -ForegroundColor Gray
    }
    Write-Host "    - $DataDir (local)" -ForegroundColor Gray
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 0
}

# Display backup list with stats
Write-Host "  Available backups:" -ForegroundColor White
Write-Host ""
Write-Host ("  {0,-4} {1,-22} {2,-9} {3,-8} {4}" -f '#', 'Date', 'Size', 'Source', 'Contents')
Write-Host ("  {0,-4} {1,-22} {2,-9} {3,-8} {4}" -f '---', '----', '----', '------', '--------')

$displayBackups = $backups | Select-Object -First 20

for ($i = 0; $i -lt $displayBackups.Count; $i++) {
    $b = $displayBackups[$i]
    $sizeMB = "{0:N1} MB" -f ($b.Length / 1MB)
    $date = $b.LastWriteTime.ToString('yyyy-MM-dd  h:mm tt')

    # Get stats for this backup
    $stats = Get-DbStats -DbPath $b.FullName
    if ($stats) {
        $parts = @()
        if ($stats.'Customers') { $parts += "$($stats.'Customers') accts" }
        if ($stats.'Call Logs') { $parts += "$($stats.'Call Logs') logs" }
        if ($stats.'Sellers') { $parts += "$($stats.'Sellers') sellers" }
        if ($stats.'Revenue Records') { $parts += "$($stats.'Revenue Records') rev" }
        if ($stats.'Milestones') { $parts += "$($stats.'Milestones') ms" }
        $statsStr = $parts -join ', '
    } else {
        $statsStr = '(could not read)'
    }

    $num = $i + 1
    Write-Host ("  {0,-4} {1,-22} {2,-9} {3,-8} {4}" -f "[$num]", $date, $sizeMB, $b.Source, $statsStr)
}

if ($backups.Count -gt 20) {
    Write-Host ""
    Write-Host "  (showing 20 of $($backups.Count) backups)" -ForegroundColor Gray
}

# Also show current database stats for comparison
Write-Host ""
if (Test-Path $DbFile) {
    $currentStats = Get-DbStats -DbPath $DbFile
    $currentSize = "{0:N1} MB" -f ((Get-Item $DbFile).Length / 1MB)
    if ($currentStats) {
        $parts = @()
        if ($currentStats.'Customers') { $parts += "$($currentStats.'Customers') accts" }
        if ($currentStats.'Call Logs') { $parts += "$($currentStats.'Call Logs') logs" }
        if ($currentStats.'Sellers') { $parts += "$($currentStats.'Sellers') sellers" }
        if ($currentStats.'Revenue Records') { $parts += "$($currentStats.'Revenue Records') rev" }
        if ($currentStats.'Milestones') { $parts += "$($currentStats.'Milestones') ms" }
        $currentStatsStr = $parts -join ', '
    } else {
        $currentStatsStr = ''
    }
    Write-Host "  Current database: $currentSize - $currentStatsStr" -ForegroundColor White
}

# Prompt for selection
Write-Host ""
$selection = Read-Host "  Enter backup number to restore (or 'q' to quit)"

if ($selection -eq 'q' -or $selection -eq 'Q' -or $selection -eq '') {
    Write-Host "  Cancelled." -ForegroundColor Yellow
    exit 0
}

$idx = 0
if (-not [int]::TryParse($selection, [ref]$idx) -or $idx -lt 1 -or $idx -gt $displayBackups.Count) {
    Write-Host "  Invalid selection." -ForegroundColor Red
    Read-Host "  Press Enter to close"
    exit 1
}

$selectedBackup = $displayBackups[$idx - 1]
$selectedDate = $selectedBackup.LastWriteTime.ToString('yyyy-MM-dd h:mm tt')

Write-Host ""
Write-Host "  You selected: $($selectedBackup.Name)" -ForegroundColor White
Write-Host "  Date: $selectedDate" -ForegroundColor White
Write-Host ""
Write-Host "  This will:" -ForegroundColor Yellow
Write-Host "    1. Stop the server (if running)" -ForegroundColor Gray
Write-Host "    2. Back up the current database (safety net)" -ForegroundColor Gray
Write-Host "    3. Replace the database with the selected backup" -ForegroundColor Gray
Write-Host "    4. Restart the server" -ForegroundColor Gray
Write-Host ""
$confirm = Read-Host "  Are you sure? (y/N)"

if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host "  Cancelled." -ForegroundColor Yellow
    exit 0
}

# Execute restore
Write-Host ""

# Step 1: Stop server
$envConfig = Read-EnvFile
$Port = if ($envConfig['PORT']) { [int]$envConfig['PORT'] } else { 5151 }

if (Test-ServerRunning -Port $Port) {
    Stop-Server -Port $Port
    if (Test-ServerRunning -Port $Port) {
        Write-Host "  [ERROR] Could not stop server on port $Port." -ForegroundColor Red
        Write-Host "  Stop it manually and try again." -ForegroundColor Gray
        Read-Host "  Press Enter to close"
        exit 1
    }
    Write-Host "  [OK] Server stopped." -ForegroundColor Green
} else {
    Write-Host "  Server not running (OK)." -ForegroundColor Gray
}

# Step 2: Safety backup of current database
if (Test-Path $DbFile) {
    $timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
    $safetyBackup = Join-Path $DataDir "notehelper_pre_restore_$timestamp.db"
    Copy-Item $DbFile $safetyBackup -Force
    Write-Host "  [OK] Current database backed up to: $($safetyBackup | Split-Path -Leaf)" -ForegroundColor Green
}

# Step 3: Copy backup over current database
try {
    Copy-Item $selectedBackup.FullName $DbFile -Force
    Write-Host "  [OK] Database restored from: $($selectedBackup.Name)" -ForegroundColor Green
} catch {
    Write-Host "  [ERROR] Failed to copy backup: $_" -ForegroundColor Red
    Write-Host "  Your pre-restore backup is at: $safetyBackup" -ForegroundColor Yellow
    Read-Host "  Press Enter to close"
    exit 1
}

# Step 4: Restart server
Write-Host ""
Start-Server -Port $Port

Write-Host ""
Write-Host "  Restore complete!" -ForegroundColor Green
Write-Host "  Database restored from backup dated: $selectedDate" -ForegroundColor White
Write-Host ""
Read-Host "  Press Enter to close"
