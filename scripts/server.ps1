# Sales Buddy - Server Management Script
# Handles first-run setup, starting the server, and pulling updates.
#
# Usage:
#   .\scripts\server.ps1            Normal start (bootstrap if needed, update if available)
#   .\scripts\server.ps1 -Force     Full update cycle (stop, backup, pull, install, migrate, restart)
#   .\scripts\server.ps1 -StopOnly  Stop the running server and exit (skips all prereq checks)
#
# Entry points:
#   start.bat               Double-click launcher (calls this script)
#   update.bat              Admin-elevated update (calls this script with -Force)
#   stop.bat                Stop the server (calls this script with -StopOnly)

param(
    [switch]$Force,    # Force full update cycle regardless of current state
    [switch]$StopOnly  # Stop the running server and exit
)

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

# ==============================================================================
# Helper Functions
# ==============================================================================

# Check if winget is available
$HasWinget = $false
try { if (Get-Command winget -ErrorAction SilentlyContinue) { $HasWinget = $true } } catch {}

# Pause for interactive use (skipped when -Force for non-interactive updates)
function Pause-WithMessage {
    param([string]$Message = "Press any key to close...", [string]$Color = "Gray")
    if ($Force) { return }
    Write-Host "`n$Message" -ForegroundColor $Color
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

# Prompt to install something via winget (skipped when -Force)
function Install-WithWinget {
    param(
        [string]$Name,
        [string]$PackageId,
        [string]$ManualUrl
    )
    if ($Force) { return $false }
    if ($HasWinget) {
        $response = Read-Host "         Install $Name automatically via winget? (Y/n)"
        if ($response -eq '' -or $response -eq 'Y' -or $response -eq 'y') {
            Write-Host ""
            Write-Host "  [SETUP] Installing $Name via winget..." -ForegroundColor Yellow
            winget install $PackageId --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  [OK] $Name installed." -ForegroundColor Green
                # Refresh PATH so the current session can find the new binary
                $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
                $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                $env:Path = "$machinePath;$userPath"
                return $true
            } else {
                Write-Host "  [ERROR] Installation failed." -ForegroundColor Red
            }
        }
    }
    if ($ManualUrl) {
        Write-Host "         Install manually from: $ManualUrl" -ForegroundColor Yellow
    }
    return $false
}

# Detect OneDrive for Business folder (excludes personal OneDrive)
function Find-OneDriveBusinessPath {
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

# Store OneDrive path in the database (user_preferences table)
function Set-OneDrivePathInDb {
    param([string]$Path)
    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { return }
    $dbPath = Join-Path $RepoRoot 'data\salesbuddy.db'
    $script = @"
import sqlite3, sys
db, path = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute('UPDATE user_preferences SET onedrive_path = ? WHERE id = (SELECT id FROM user_preferences LIMIT 1)', (path,))
conn.commit()
conn.close()
"@
    & $pythonExe -c $script $dbPath $Path 2>$null
}

# Read OneDrive path from the database
function Get-OneDrivePathFromDb {
    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { return '' }
    $dbPath = Join-Path $RepoRoot 'data\salesbuddy.db'
    $script = @"
import sqlite3, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    c = conn.cursor()
    c.execute('SELECT onedrive_path FROM user_preferences LIMIT 1')
    row = c.fetchone()
    conn.close()
    print(row[0] if row and row[0] else '')
except Exception:
    print('')
"@
    try {
        $result = & $pythonExe -c $script $dbPath 2>$null
        return $result.Trim()
    } catch {}
    return ''
}

# Read .env file into a hashtable
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

# Check if something is listening on a port
function Test-ServerRunning {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conn
}

# Kill whatever is listening on a port (and its process tree)
function Stop-Server {
    param([int]$Port)
    Write-Host "  Stopping server on port $Port..." -ForegroundColor Yellow
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $procIds = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($p in $procIds) {
            # Kill the process and any children (waitress workers)
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" -ErrorAction SilentlyContinue |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
        # Wait and verify the port is free
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

# Start waitress in a hidden window (no lingering console)
function Start-Server {
    param([int]$Port)
    $waitress = Join-Path $RepoRoot 'venv\Scripts\waitress-serve.exe'
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

# One-time rename from legacy database filename
function Migrate-DatabaseName {
    $oldDb = Join-Path $RepoRoot 'data\notehelper.db'
    $newDb = Join-Path $RepoRoot 'data\salesbuddy.db'
    if ((Test-Path $oldDb) -and -not (Test-Path $newDb)) {
        Move-Item $oldDb $newDb
        Write-Host "  [OK] Renamed database: notehelper.db -> salesbuddy.db" -ForegroundColor Green
    }

    # Remove legacy DATABASE_URL from .env (the app default is correct)
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        $lines = Get-Content $envFile
        $filtered = $lines | Where-Object { $_ -notmatch '^\s*DATABASE_URL\s*=' }
        if ($filtered.Count -ne $lines.Count) {
            Set-Content $envFile $filtered
            Write-Host "  [OK] Removed DATABASE_URL from .env (using app default)" -ForegroundColor Green
        }
    }
}

# Backup the database
function Backup-Database {
    $dbFile = Join-Path $RepoRoot 'data\salesbuddy.db'
    if (Test-Path $dbFile) {
        $timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
        $backupFile = Join-Path $RepoRoot "data\salesbuddy_backup_$timestamp.db"
        Copy-Item $dbFile $backupFile
        Write-Host "  [OK] Database backed up." -ForegroundColor Green
    }
}

# Pull latest from git (handles stashing dirty files)
function Pull-Updates {
    $dirty = git status --porcelain
    if ($dirty) {
        Write-Host "  Stashing local changes..." -ForegroundColor Gray
        git stash --quiet 2>$null
    }

    Write-Host "  Pulling latest changes..." -ForegroundColor Yellow
    $pullOutput = git pull origin main 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] git pull failed!" -ForegroundColor Red
        Write-Host "  $pullOutput" -ForegroundColor Red
        if ($dirty) { git stash pop --quiet 2>$null }
        return $false
    }
    Write-Host "  $pullOutput"

    if ($dirty) {
        git stash pop --quiet 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Stashed changes conflict with upstream - dropped." -ForegroundColor Yellow
            git stash drop --quiet 2>$null
        }
    }
    return $true
}

# Install/update pip dependencies
function Install-Dependencies {
    Write-Host "  Installing dependencies..." -ForegroundColor Yellow
    Write-Host "  (First run may take a minute or two)" -ForegroundColor Gray
    $pipExe = Join-Path $RepoRoot 'venv\Scripts\pip.exe'
    $reqFile = Join-Path $RepoRoot 'requirements.txt'

    # Run pip directly with --quiet. Suppress stderr (2>$null) because pip
    # writes [notice] upgrade nags there.
    & $pipExe install -r $reqFile --quiet 2>$null

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  pip exited with code $LASTEXITCODE" -ForegroundColor Red
        return $false
    }
    return $true
}

# Run database migrations
function Run-Migrations {
    Write-Host "  Running migrations..." -ForegroundColor Yellow
    & (Join-Path $RepoRoot 'venv\Scripts\python.exe') -c "from app import create_app, db; from app.migrations import run_migrations; app = create_app(); app.app_context().push(); run_migrations(db)"
    return $LASTEXITCODE -eq 0
}

# ==============================================================================
# Main
# ==============================================================================

Write-Host ""
Write-Host "  Sales Buddy" -ForegroundColor Cyan
Write-Host "  ==========" -ForegroundColor Cyan
Write-Host ""

# -StopOnly: Read port from .env, stop the server, and exit immediately.
# Skips all prereq checks (Python, Git, venv, etc.) for instant execution.
if ($StopOnly) {
    $envConfig = Read-EnvFile
    $Port = if ($envConfig['PORT']) { [int]$envConfig['PORT'] } else { 5151 }
    if (Test-ServerRunning -Port $Port) {
        Stop-Server -Port $Port
        if (-not (Test-ServerRunning -Port $Port)) {
            Write-Host "  [OK] Server on port $Port stopped." -ForegroundColor Green
        } else {
            Write-Host "  [ERROR] Failed to stop server on port $Port." -ForegroundColor Red
        }
    } else {
        Write-Host "  No server running on port $Port." -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 1
    exit 0
}

# -- Step 1: Check Python -----------------------------------------------------
function Test-PythonOk {
    try {
        $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split('.')
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 13) {
                return $ver
            }
        }
    } catch {}
    return $null
}

$pythonOk = $false
$pyVersion = Test-PythonOk
if ($pyVersion) {
    Write-Host "  [OK] Python $pyVersion" -ForegroundColor Green
    $pythonOk = $true
} else {
    if ($pyVersion -eq $null) {
        Write-Host "  [ERROR] Python not found." -ForegroundColor Red
    } else {
        Write-Host "  [ERROR] Python found, but 3.13+ is required." -ForegroundColor Red
    }
    Write-Host ""
    $installed = Install-WithWinget -Name "Python 3.14" -PackageId "Python.Python.3.14" -ManualUrl "https://www.python.org/downloads/"
    if ($installed) {
        # Re-check with refreshed PATH
        $pyVersion = Test-PythonOk
        if ($pyVersion) {
            Write-Host "  [OK] Python $pyVersion" -ForegroundColor Green
            $pythonOk = $true
        }
    }
}

if (-not $pythonOk) {
    Pause-WithMessage "Press any key to close..." "Red"
    exit 1
}

# -- Step 2: Check Git ---------------------------------------------------------
$hasGit = $false
try { if (Get-Command git -ErrorAction SilentlyContinue) { $hasGit = $true } } catch {}
if ($hasGit) {
    $gitVersion = & git --version 2>$null
    Write-Host "  [OK] $gitVersion" -ForegroundColor Green
} else {
    Write-Host "  [WARNING] Git not found." -ForegroundColor Yellow
    Write-Host "            Required for pulling updates." -ForegroundColor Gray
    Write-Host ""
    Install-WithWinget -Name "Git" -PackageId "Git.Git" -ManualUrl "https://git-scm.com/downloads"
    # Re-check after potential install
    try { if (Get-Command git -ErrorAction SilentlyContinue) { $hasGit = $true } } catch {}
    if (-not $hasGit) {
        Write-Host "            Sales Buddy will still run, but you won't be able to pull updates." -ForegroundColor Gray
        Write-Host ""
    }
}

# -- Step 3: Check Azure CLI (optional) ---------------------------------------
$hasAz = $false
try { if (Get-Command az -ErrorAction SilentlyContinue) { $hasAz = $true } } catch {}
if ($hasAz) {
    Write-Host "  [OK] Azure CLI found." -ForegroundColor Green
} else {
    Write-Host "  [WARNING] Azure CLI (az) not found." -ForegroundColor Yellow
    Write-Host "            Required for MSX imports, milestone sync, and AI features." -ForegroundColor Gray
    Write-Host ""
    Install-WithWinget -Name "Azure CLI" -PackageId "Microsoft.AzureCLI" -ManualUrl "https://aka.ms/installazurecliwindows"
    # Re-check after potential install
    try { if (Get-Command az -ErrorAction SilentlyContinue) { $hasAz = $true } } catch {}
    if (-not $hasAz) {
        Write-Host "            Sales Buddy will still run, but Azure features won't work." -ForegroundColor Gray
        Write-Host ""
    }
}

# -- Step 4: Check Node.js (optional) -----------------------------------------
$hasNode = $false
try { if (Get-Command node -ErrorAction SilentlyContinue) { $hasNode = $true } } catch {}
if ($hasNode) {
    $nodeVersion = & node -v 2>$null
    Write-Host "  [OK] Node.js $nodeVersion" -ForegroundColor Green
} else {
    Write-Host "  [WARNING] Node.js not found." -ForegroundColor Yellow
    Write-Host "            Required for WorkIQ meeting import (auto-fill from meetings)." -ForegroundColor Gray
    Write-Host ""
    Install-WithWinget -Name "Node.js LTS" -PackageId "OpenJS.NodeJS.LTS" -ManualUrl "https://nodejs.org/"
    # Re-check after potential install
    try { if (Get-Command node -ErrorAction SilentlyContinue) { $hasNode = $true } } catch {}
    if (-not $hasNode) {
        Write-Host "            Sales Buddy will still run, but meeting import won't work." -ForegroundColor Gray
        Write-Host ""
    }
}

# -- Step 5: Create venv if missing -------------------------------------------
if (-not (Test-Path (Join-Path $RepoRoot 'venv\Scripts\python.exe'))) {
    Write-Host "  [SETUP] Creating virtual environment..." -ForegroundColor Yellow
    & python -m venv (Join-Path $RepoRoot 'venv')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Failed to create virtual environment." -ForegroundColor Red
        Pause-WithMessage "Press any key to close..." "Red"
        exit 1
    }
    Write-Host "  [OK] Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "  [OK] Virtual environment found." -ForegroundColor Green
}

# -- Step 6: Install dependencies ---------------------------------------------
if (-not (Install-Dependencies)) {
    Write-Host "  [ERROR] Failed to install dependencies." -ForegroundColor Red
    Pause-WithMessage "Press any key to close..." "Red"
    exit 1
}
Write-Host "  [OK] Dependencies installed." -ForegroundColor Green

# -- Step 7: Create .env if missing -------------------------------------------
$envFile = Join-Path $RepoRoot '.env'
if (-not (Test-Path $envFile)) {
    $exampleFile = Join-Path $RepoRoot '.env.example'
    if (Test-Path $exampleFile) {
        Write-Host "  [SETUP] Creating .env from .env.example..." -ForegroundColor Yellow
        Copy-Item $exampleFile $envFile
        # Generate a random SECRET_KEY
        $secretKey = & (Join-Path $RepoRoot 'venv\Scripts\python.exe') -c "import secrets; print(secrets.token_hex(32))"
        $content = Get-Content $envFile -Raw
        $content = $content.Replace('your-secret-key-here-change-in-production', $secretKey)
        Set-Content $envFile $content -NoNewline
        Write-Host "  [OK] .env created with generated SECRET_KEY." -ForegroundColor Green
        Write-Host "        Edit .env to add Azure credentials if needed." -ForegroundColor Gray
    } else {
        Write-Host "  [WARNING] No .env.example found. Create .env manually." -ForegroundColor Yellow
    }
}

# -- Step 8: Read config ------------------------------------------------------
$envConfig = Read-EnvFile
$Port = if ($envConfig['PORT']) { [int]$envConfig['PORT'] } else { 5151 }
Write-Host "  [OK] Port: $Port" -ForegroundColor Green

# -- Step 9: Check current state -----------------------------------------------
$serverRunning = Test-ServerRunning -Port $Port

# -- Step 10: Check for git updates -------------------------------------------
$hasUpdates = $false

# Only check for updates if git is installed AND this is a git repo
$isGitRepo = $false
if ($hasGit) {
    try {
        git rev-parse --git-dir 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $isGitRepo = $true }
    } catch {}
}

if ($isGitRepo) {
    Write-Host "  Checking for updates..." -ForegroundColor Gray
    git fetch origin main --quiet 2>$null
    $localCommit = git rev-parse HEAD 2>$null
    $remoteCommit = git rev-parse origin/main 2>$null
    if ($localCommit -and $remoteCommit -and $localCommit -ne $remoteCommit) {
        $hasUpdates = $true
        $behindCount = git rev-list --count HEAD..origin/main 2>$null
        Write-Host "  [UPDATE] $behindCount commit(s) behind origin/main" -ForegroundColor Yellow
    } else {
        Write-Host "  [OK] Up to date." -ForegroundColor Green
    }
} else {
    Write-Host "  [INFO] Git not available - update checking disabled." -ForegroundColor Gray
}

# -- Step 11: Check/configure backups ------------------------------------------
# Read OneDrive path from DB. If empty, try runtime detection and store it.
$backupOneDrive = Get-OneDrivePathFromDb

if ($backupOneDrive) {
    $backupDir = Join-Path $backupOneDrive 'Backups\SalesBuddy'
    Write-Host "  [OK] Backups configured -> $backupDir" -ForegroundColor Green
} elseif (-not $Force) {
    # First run or DB has no path - auto-detect OneDrive for Business
    $businessOneDrive = Find-OneDriveBusinessPath
    if ($businessOneDrive) {
        $backupDir = Join-Path $businessOneDrive 'Backups\SalesBuddy'
        Write-Host ""
        Write-Host "  Detected OneDrive for Business: $businessOneDrive" -ForegroundColor Green
        Write-Host "  Configuring automatic backups -> $backupDir" -ForegroundColor Yellow

        # Create the backup directory
        if (-not (Test-Path $backupDir)) {
            New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        }

        # Store OneDrive path in the database
        Set-OneDrivePathInDb -Path $businessOneDrive
        $backupOneDrive = $businessOneDrive

        Write-Host "  [OK] Automatic backups configured (database + call logs)." -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "  [WARNING] No OneDrive for Business detected." -ForegroundColor Yellow
        Write-Host "            Automatic backups could not be configured." -ForegroundColor Gray
        Write-Host "            Install OneDrive and sign in with your work account," -ForegroundColor Gray
        Write-Host "            then run backup.bat to set up backups." -ForegroundColor Gray
    }
} else {
    Write-Host "  [INFO] Backups not configured. Run backup.bat to set up." -ForegroundColor Gray
}

# -- Step 12: Ensure scheduled tasks are registered ----------------------------
# Both tasks are registered idempotently on every start. If a task was disabled
# by the user via the admin panel, we leave it disabled - we only create missing
# tasks, never re-enable or re-create existing ones.

# 12a: SalesBuddy-DailyBackup (only if OneDrive path is known)
$BackupTaskName = 'SalesBuddy-DailyBackup'
$existingBackupTask = Get-ScheduledTask -TaskName $BackupTaskName -ErrorAction SilentlyContinue

if (-not $existingBackupTask -and $backupOneDrive) {
    Write-Host ""
    Write-Host "  Registering daily backup task..." -ForegroundColor Yellow

    $backupScript = Join-Path $PSScriptRoot 'backup.ps1'
    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$backupScript`" -Silent" `
        -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At '11:00AM'
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    # Try SYSTEM first (reliable on Entra ID cloud-joined machines, but needs admin).
    # Fall back to Interactive if not elevated.
    # NEVER use S4U - it silently fails on Entra-only accounts.
    $taskRegistered = $false
    try {
        $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask `
            -TaskName $BackupTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings `
            -Description 'Daily backup of Sales Buddy database to OneDrive' `
            -ErrorAction Stop | Out-Null
        $taskRegistered = $true
    } catch {
        try {
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask `
                -TaskName $BackupTaskName -Action $action -Trigger $trigger `
                -Principal $principal -Settings $settings `
                -Description 'Daily backup of Sales Buddy database to OneDrive' `
                -ErrorAction Stop | Out-Null
            $taskRegistered = $true
        } catch {
            Write-Host "  [WARNING] Could not register backup task: $_" -ForegroundColor Yellow
        }
    }

    if ($taskRegistered) {
        Write-Host "  [OK] Daily backups scheduled at 11:00 AM." -ForegroundColor Green
    }
} elseif ($existingBackupTask) {
    Write-Host "  [OK] Daily backup task registered." -ForegroundColor Green
}

# 12b: SalesBuddy-AutoStart
$AutoStartTaskName = 'SalesBuddy-AutoStart'
$existingAutoStart = Get-ScheduledTask -TaskName $AutoStartTaskName -ErrorAction SilentlyContinue

if (-not $existingAutoStart) {
    Write-Host ""
    Write-Host "  Registering auto-start on login..." -ForegroundColor Yellow

    $serverScript = Join-Path $PSScriptRoot 'server.ps1'
    $asAction = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$serverScript`"" `
        -WorkingDirectory $RepoRoot
    $asTrigger = New-ScheduledTaskTrigger -AtLogOn
    $asTrigger.UserId = $env:USERNAME
    $asSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

    # Use Interactive logon type directly. The AtLogOn trigger guarantees the
    # user is logged in, so Interactive always works. S4U is unreliable on
    # Entra ID / cloud-joined machines (no stored local credentials).
    $asRegistered = $false
    try {
        $asPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $AutoStartTaskName -Action $asAction -Trigger $asTrigger `
            -Principal $asPrincipal -Settings $asSettings `
            -Description 'Start Sales Buddy web server automatically at login' `
            -ErrorAction Stop | Out-Null
        $asRegistered = $true
    } catch {
        Write-Host "  [WARNING] Could not register auto-start task: $_" -ForegroundColor Yellow
        Write-Host "            You can still start manually with start.bat." -ForegroundColor Gray
    }

    if ($asRegistered) {
        Write-Host "  [OK] Sales Buddy will start automatically at login." -ForegroundColor Green
    }
} elseif ($existingAutoStart) {
    Write-Host "  [OK] Auto-start at login registered." -ForegroundColor Green
}

# 12c: SalesBuddy-MilestoneSync (Mon/Wed/Fri at a random time between 9:30 AM and 4:30 PM)
$MilestoneSyncTaskName = 'SalesBuddy-MilestoneSync'
$existingMilestoneSync = Get-ScheduledTask -TaskName $MilestoneSyncTaskName -ErrorAction SilentlyContinue

if (-not $existingMilestoneSync) {
    Write-Host ""
    Write-Host "  Registering milestone sync task..." -ForegroundColor Yellow

    # Pick a random 5-minute slot between 9:30 AM and 4:30 PM (84 slots)
    # so that many users' syncs are spread throughout the day.
    $slotIndex = Get-Random -Minimum 0 -Maximum 84
    $syncTime = (Get-Date '9:30AM').AddMinutes($slotIndex * 5)
    $syncTimeStr = $syncTime.ToString('h:mmtt').ToLower()

    $syncScript = Join-Path $PSScriptRoot 'milestone-sync.ps1'
    $hiddenLauncher = Join-Path $PSScriptRoot 'run-hidden.vbs'
    $msAction = New-ScheduledTaskAction `
        -Execute 'wscript.exe' `
        -Argument "`"$hiddenLauncher`" `"$syncScript`"" `
        -WorkingDirectory $RepoRoot
    $msTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Wednesday,Friday -At $syncTime
    $msSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

    $msRegistered = $false
    try {
        $msPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $MilestoneSyncTaskName -Action $msAction -Trigger $msTrigger `
            -Principal $msPrincipal -Settings $msSettings `
            -Description "Milestone sync from MSX (Mon/Wed/Fri at $syncTimeStr when Sales Buddy is running)" `
            -ErrorAction Stop | Out-Null
        $msRegistered = $true
    } catch {
        Write-Host "  [WARNING] Could not register milestone sync task: $_" -ForegroundColor Yellow
    }

    if ($msRegistered) {
        Write-Host "  [OK] Milestone sync scheduled Mon/Wed/Fri at $syncTimeStr." -ForegroundColor Green
    }
} elseif ($existingMilestoneSync) {
    Write-Host "  [OK] Milestone sync task registered." -ForegroundColor Green
}

# ==============================================================================
# Decision Logic
# ==============================================================================

# -Force: Full update cycle (used by update.bat)
if ($Force) {
    Write-Host ""
    Write-Host "  Updating..." -ForegroundColor Cyan

    if ($serverRunning) { Stop-Server -Port $Port }
    Migrate-DatabaseName
    Backup-Database

    if ($isGitRepo) {
        if (-not (Pull-Updates)) {
            Write-Host "  Restarting server with current code..." -ForegroundColor Yellow
            Start-Server -Port $Port
            Pause-WithMessage "UPDATE FAILED - press any key to close..." "Red"
            exit 1
        }
    }

    if (-not (Install-Dependencies)) {
        Write-Host "  [ERROR] pip install failed!" -ForegroundColor Red
        Start-Server -Port $Port
        Pause-WithMessage "UPDATE FAILED - press any key to close..." "Red"
        exit 1
    }

    if (-not (Run-Migrations)) {
        Write-Host "  [ERROR] Migrations failed!" -ForegroundColor Red
        Start-Server -Port $Port
        Pause-WithMessage "UPDATE FAILED - press any key to close..." "Red"
        exit 1
    }

    Start-Server -Port $Port
    Write-Host ""
    Write-Host "  Update complete!" -ForegroundColor Green
    Pause-WithMessage "Press any key to close..."
    exit 0
}

# Smart mode: do whatever makes sense

if ($serverRunning -and -not $hasUpdates) {
    Write-Host ""
    Write-Host "  Server is already running on port $Port and up to date." -ForegroundColor Green
    Pause-WithMessage "Press any key to close..."
    exit 0
}

if ($hasUpdates) {
    Write-Host ""
    Write-Host "  Applying updates..." -ForegroundColor Cyan

    if ($serverRunning) { Stop-Server -Port $Port }
    Migrate-DatabaseName
    Backup-Database

    if (-not (Pull-Updates)) {
        Write-Host "  Starting server with current code..." -ForegroundColor Yellow
        Start-Server -Port $Port
        Pause-WithMessage "UPDATE FAILED - press any key to close..." "Red"
        exit 1
    }

    if (-not (Install-Dependencies)) {
        Write-Host "  [ERROR] pip install failed!" -ForegroundColor Red
    }

    if (-not (Run-Migrations)) {
        Write-Host "  [ERROR] Migrations failed!" -ForegroundColor Red
    }
}

# Start server
Start-Server -Port $Port
Write-Host ""
Write-Host "  Sales Buddy is running! Open in your browser:" -ForegroundColor Green
Write-Host ""
Write-Host "  http://localhost:$Port" -ForegroundColor Cyan
Write-Host ""
Pause-WithMessage "Press any key to close this window..."
