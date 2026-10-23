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
. (Join-Path $PSScriptRoot 'Resolve-DbPath.ps1')

# Always pin Azure CLI state to an environment-specific path under USERPROFILE.
# Production uses %USERPROFILE%\SalesBuddy\.azure, development uses
# %USERPROFILE%\SalesBuddyDev\.azure, and all other values default to production.
function Initialize-IsolatedAzCliContext {
    $baseUserProfile = $env:USERPROFILE
    if (-not $baseUserProfile) {
        $baseUserProfile = [Environment]::GetFolderPath('UserProfile')
    }
    if (-not $baseUserProfile) {
        throw "Unable to resolve user profile path for Sales Buddy runtime context."
    }

    $flaskEnv = 'production'
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*FLASK_ENV\s*=\s*(.+?)\s*$') {
                $flaskEnv = $Matches[1].Trim().Trim('"').Trim("'")
                break
            }
        }
    }

    switch -Regex ($flaskEnv) {
        '^(?i)production$' { $profileFolder = 'SalesBuddy'; break }
        '^(?i)development$' { $profileFolder = 'SalesBuddyDev'; break }
        default { $profileFolder = 'SalesBuddy' }
    }
    $salesBuddyHome = Join-Path $baseUserProfile $profileFolder
    $azureConfigDir = Join-Path $salesBuddyHome '.azure'
    $defaultAzureConfigDir = Join-Path $baseUserProfile '.azure'

    if (-not (Test-Path $salesBuddyHome)) {
        New-Item -Path $salesBuddyHome -ItemType Directory -Force | Out-Null
    }

    # First run bootstrap: if the env-specific .azure folder does not exist yet,
    # migrate existing default Azure CLI state automatically when available.
    # Also retry migration if target exists but is empty (failed copy recovery).
    $azureDirEmpty = @(Get-ChildItem -Path $azureConfigDir -Force -ErrorAction SilentlyContinue).Count -eq 0
    if (-not (Test-Path $azureConfigDir) -or $azureDirEmpty) {
        if (Test-Path $defaultAzureConfigDir) {
            try {
                Copy-Item -Path $defaultAzureConfigDir -Destination $salesBuddyHome -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Host "  [WARNING] Could not migrate Azure CLI config to ${azureConfigDir}: $($_.Exception.Message)" -ForegroundColor Yellow
                New-Item -Path $azureConfigDir -ItemType Directory -Force | Out-Null
            }
        } else {
            New-Item -Path $azureConfigDir -ItemType Directory -Force | Out-Null
            $script:AzCliContextFresh = $true
        }
    }

    $env:SALESBUDDY_HOME = $salesBuddyHome
    $env:AZURE_CONFIG_DIR = $azureConfigDir

    # Disable the Windows broker (WAM) inside this isolated config dir.
    # With the broker enabled (az CLI default since 2.61), refresh tokens live
    # in WAM keyed by {client_id, account} and are SHARED across every
    # AZURE_CONFIG_DIR on the machine -- defeating the isolation. An `az logout`
    # in any other shell flips the broker account to Status_AccountUnusable
    # and Sales Buddy starts returning 401 on every MSX/gateway call.
    # File-backed MSAL cache (DPAPI-encrypted, lives in msal_token_cache.bin
    # inside this dir) is per-AZURE_CONFIG_DIR and gives us real isolation.
    $configFile = Join-Path $azureConfigDir 'config'
    $needsBrokerWrite = $true
    if (Test-Path $configFile) {
        $existing = Get-Content $configFile -Raw -ErrorAction SilentlyContinue
        if ($existing -match '(?im)^\s*enable_broker_on_windows\s*=\s*false\s*$') {
            $needsBrokerWrite = $false
        }
    }
    if ($needsBrokerWrite) {
        try {
            $lines = @()
            if (Test-Path $configFile) {
                $lines = @(Get-Content $configFile -ErrorAction Stop)
            }
            # Strip any existing enable_broker_on_windows line
            $lines = $lines | Where-Object { $_ -notmatch '(?i)^\s*enable_broker_on_windows\s*=' }
            # Ensure a [core] section exists, then append the setting under it
            $hasCore = $lines | Where-Object { $_ -match '(?i)^\s*\[core\]\s*$' }
            if (-not $hasCore) {
                if ($lines.Count -gt 0 -and $lines[-1].Trim() -ne '') { $lines += '' }
                $lines += '[core]'
                $lines += 'enable_broker_on_windows = false'
            } else {
                # Insert the setting immediately after the [core] header
                $out = New-Object System.Collections.Generic.List[string]
                $inserted = $false
                foreach ($line in $lines) {
                    $out.Add($line)
                    if (-not $inserted -and $line -match '(?i)^\s*\[core\]\s*$') {
                        $out.Add('enable_broker_on_windows = false')
                        $inserted = $true
                    }
                }
                $lines = $out.ToArray()
            }
            Set-Content -Path $configFile -Value $lines -Encoding ASCII -ErrorAction Stop
        } catch {
            Write-Host "  [WARNING] Could not disable Az CLI broker in ${configFile}: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    # Expose the resolved environment label for the banner
    switch -Regex ($flaskEnv) {
        '^(?i)production$' { $script:ResolvedEnvLabel = 'Production'; break }
        '^(?i)development$' { $script:ResolvedEnvLabel = 'Development'; break }
        default { $script:ResolvedEnvLabel = 'Production' }
    }
}

try {
    Initialize-IsolatedAzCliContext
} catch {
    # Deferred: error is reported after the banner in Main
    $script:AzCliContextError = $_
}

# ==============================================================================
# Helper Functions
# ==============================================================================

# Check if winget is available
$HasWinget = $false
try { if (Get-Command winget -ErrorAction SilentlyContinue) { $HasWinget = $true } } catch {}

# Wait for interactive use (skipped when -Force for non-interactive updates)
function Wait-WithMessage {
    param([string]$Message = "Press any key to close...", [string]$Color = "Gray")
    if ($Force) { return }
    # Skip ReadKey when there's no usable console (auto-start via wscript). Blocking
    # here would keep this script alive holding handles for the waitress subprocess.
    try {
        $null = $Host.UI.RawUI.KeyAvailable
    } catch {
        return
    }
    Write-Host "`n$Message" -ForegroundColor $Color
    try {
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    } catch {
        return
    }
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
    # All candidates must resolve to 'OneDrive - Microsoft' to ensure we pick
    # the Microsoft corporate account, not a partner tenant.
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

# Store OneDrive path in the database (user_preferences table)
function Set-OneDrivePathInDb {
    param([string]$Path)
    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { return }
    $dbPath = Resolve-SalesBuddyDbPath -RepoRoot $RepoRoot
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
    $dbPath = Resolve-SalesBuddyDbPath -RepoRoot $RepoRoot
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

# Recursively kill a process and all its descendants. ExcludePid protects one
# process (e.g. the update process that is itself driving this stop) so it
# doesn't terminate itself while tearing down the tree.
function Stop-ProcessTree {
    param([int]$ProcessId, [int]$ExcludePid = 0)
    if ($ProcessId -le 0 -or $ProcessId -eq $ExcludePid) { return }
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-ProcessTree -ProcessId $_.ProcessId -ExcludePid $ExcludePid }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# Stop the whole app: the supervisor and its children (web + worker), plus any
# process still listening on the port (covers a monolithic/unsupervised launch).
# Excludes the current process ($PID) so an in-flight update driving this stop
# doesn't kill itself.
function Stop-Server {
    param([int]$Port)
    Write-Host "  Stopping server on port $Port..." -ForegroundColor Yellow

    # 1. Kill the supervisor tree first so it can't respawn the web child mid-stop.
    $installPattern = [regex]::Escape($RepoRoot) + '([\\/]|["\s]|$)'
    $supervisors = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $installPattern -and
            $_.CommandLine -match 'app\.supervisor' -and $_.ProcessId -ne $PID }
    foreach ($sup in $supervisors) {
        Stop-ProcessTree -ProcessId $sup.ProcessId -ExcludePid $PID
    }

    # 2. Kill whatever still listens on the port (monolithic waitress, orphans).
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $procIds = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($p in $procIds) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction Stop
            if (-not $owner.CommandLine -or $owner.CommandLine -notmatch $installPattern) {
                throw "Port $Port belongs to another application. Upgrade stopped without changing files."
            }
            Stop-ProcessTree -ProcessId $p -ExcludePid $PID
        }
    }
    # Coordinators and diagnostic senders use the same venv but are not backend children.
    $backendPattern = '(?:app\.supervisor|app\.worker|waitress-serve\.exe|' +
        '\s-m\s+(?:waitress|flask)(?:\s|$))'
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='waitress-serve.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $installPattern -and
            $_.CommandLine -match $backendPattern -and
            $_.ProcessId -ne $PID } |
        ForEach-Object { Stop-ProcessTree -ProcessId $_.ProcessId -ExcludePid $PID }

    # 3. Verify the port is free.
    $retries = 0
    while ($retries -lt 5) {
        Start-Sleep -Seconds 1
        $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $still) { return }
        $retries++
    }
    throw "The backend did not stop. No upgrade files have been changed."
}

# Register salesbuddy:// URL protocol handler (idempotent, HKCU - no admin needed)
# Allows the offline page to start the server via a browser link.
function Register-ProtocolHandler {
    $regPath = 'HKCU:\Software\Classes\salesbuddy'
    $vbsPath = Join-Path $RepoRoot 'scripts\run-hidden.vbs'
    $scriptPath = Join-Path $RepoRoot 'scripts\server.ps1'
    $command = "wscript.exe `"$vbsPath`" `"$scriptPath`""

    try {
        New-Item -Path "$regPath\shell\open\command" -Force | Out-Null
        # Friendly names so the browser dialog says "Sales Buddy" not "Microsoft Windows Based Script Host"
        Set-ItemProperty -Path $regPath -Name '(Default)' -Value 'URL:Sales Buddy'
        New-ItemProperty -Path $regPath -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $regPath -Name 'FriendlyTypeName' -Value 'Sales Buddy' -PropertyType String -Force | Out-Null

        # Application metadata - controls the name shown in the browser's "Open?" prompt
        New-Item -Path "$regPath\Application" -Force | Out-Null
        Set-ItemProperty -Path "$regPath\Application" -Name 'ApplicationName' -Value 'Sales Buddy'
        Set-ItemProperty -Path "$regPath\Application" -Name 'ApplicationDescription' -Value 'Start the Sales Buddy server'

        Set-ItemProperty -Path "$regPath\shell\open\command" -Name '(Default)' -Value $command
    } catch {
        Write-Host "  [WARNING] Could not register salesbuddy:// protocol: $_" -ForegroundColor Yellow
    }
}

# Resolve the directory used for runtime server logs.
function Get-ServerLogDir {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
    $dir = Join-Path $base 'SalesBuddy\logs'
    if (-not (Test-Path $dir)) {
        New-Item -Path $dir -ItemType Directory -Force | Out-Null
    }
    return $dir
}

# Rotate server.log -> server.log.1 when it exceeds the size cap. Keeps one old
# file so total disk use is bounded at ~2x the cap.
function Invoke-ServerLogRotation {
    param([string]$LogPath, [int]$MaxBytes = 5MB)
    if (-not (Test-Path $LogPath)) { return }
    try {
        $size = (Get-Item $LogPath -ErrorAction Stop).Length
    } catch { return }
    if ($size -lt $MaxBytes) { return }
    $rolled = "$LogPath.1"
    try {
        if (Test-Path $rolled) { Remove-Item $rolled -Force -ErrorAction Stop }
        Move-Item $LogPath $rolled -Force -ErrorAction Stop
    } catch {
        # If the live log is locked by a previous server still shutting down,
        # truncate in place as a fallback so it can't grow without bound.
        try { Set-Content -Path $LogPath -Value '' -Force } catch {}
    }
}

# Start the supervisor (which spawns web + worker) in a hidden window.
function Start-Server {
    param([int]$Port)
    $python = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }
    $stateFile = Join-Path $RepoRoot 'data\upgrade-state.json'
    if (Test-Path $stateFile) {
        $upgrade = Get-Content $stateFile -Raw | ConvertFrom-Json
        if ($upgrade.notification_pending) {
            & $python (Join-Path $RepoRoot 'scripts\upgrade.py') launch-recovery --repo $RepoRoot
            if ($LASTEXITCODE -ne 0) { throw 'The pending recovery notice could not be opened.' }
            throw 'Automatic recovery is handling startup. Dismiss its notice when restoration finishes.'
        }
    }
    $serverArgs = @('-m', 'app.supervisor')

    Write-Host "  Starting supervisor (web + worker) on port $Port..." -ForegroundColor Yellow

    $logDir = Get-ServerLogDir
    $logPath = Join-Path $logDir 'server.log'
    Invoke-ServerLogRotation -LogPath $logPath

    # Use ProcessStartInfo to explicitly pass environment variables (e.g., AZURE_CONFIG_DIR)
    # so the supervisor and the children it spawns inherit the correct az config path.
    # Redirect stdout/stderr via cmd.exe to a rotated log file. Redirecting through
    # ProcessStartInfo pipes without a reader deadlocks once the pipe buffer fills
    # (~4-64 KB), which is why headless auto-start used to wedge after a while.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $env:ComSpec
    $argLine = ($serverArgs -join ' ')
    $psi.Arguments = '/d /c ""' + $python + '" ' + $argLine + ' >> "' + $logPath + '" 2>&1"'
    $psi.WorkingDirectory = $RepoRoot
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    # Copy current environment and set the vars the children rely on.
    foreach ($key in [System.Environment]::GetEnvironmentVariables([System.EnvironmentVariableTarget]::Process).Keys) {
        $psi.EnvironmentVariables[$key] = [System.Environment]::GetEnvironmentVariable($key, [System.EnvironmentVariableTarget]::Process)
    }
    $psi.EnvironmentVariables['AZURE_CONFIG_DIR'] = $env:AZURE_CONFIG_DIR
    $psi.EnvironmentVariables['SALESBUDDY_HOME'] = $env:SALESBUDDY_HOME
    $psi.EnvironmentVariables['PORT'] = "$Port"
    $bootId = [guid]::NewGuid().ToString()
    $psi.EnvironmentVariables['SALESBUDDY_BOOT_ID'] = $bootId

    [System.Diagnostics.Process]::Start($psi) | Out-Null

    $coordinator = Join-Path $RepoRoot 'scripts\upgrade.py'
    & $python $coordinator wait-ready --repo $RepoRoot --boot-id $bootId --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Backend startup failed. Open Sales Buddy for recovery and diagnostic export. Logs: $logPath"
    }
    & $python $coordinator accept --repo $RepoRoot --boot-id $bootId --port $Port
    if ($LASTEXITCODE -ne 0) { throw "The update could not be accepted as ready." }
    Write-Host "  [OK] Web server and worker ready at http://localhost:$Port" -ForegroundColor Green
    Write-Host "  Logs: $logPath" -ForegroundColor DarkGray
}

function Invoke-SafeUpdate {
    param([int]$Port)
    $python = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    $coordinator = Join-Path $RepoRoot 'scripts\upgrade.py'
    if (-not (Test-Path $coordinator)) {
        throw 'The upgrade coordinator is missing. Repair the installation before updating.'
    }
    Stop-Server -Port $Port
    & git fetch origin main
    if ($LASTEXITCODE -ne 0) { throw 'Fetching the update failed; no application files changed.' }
    $desktop = Join-Path $RepoRoot 'electron-dist\Sales Buddy.exe'
    $shellFiles = & git diff --name-only HEAD origin/main -- electron
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine whether the desktop shell needs rebuilding.' }
    $shellRebuild = (Test-Path $desktop) -and [bool]$shellFiles
    $prepareArgs = @('prepare', '--repo', $RepoRoot, '--target', 'origin/main')
    if ($shellRebuild) { $prepareArgs += '--shell-rebuild' }
    & $python $coordinator @prepareArgs
    if ($LASTEXITCODE -ne 0) { throw 'Recovery preparation failed. The update was stopped.' }
    $recovery = Get-Content (Join-Path $RepoRoot 'data\upgrade-state.json') -Raw | ConvertFrom-Json
    $recoveryPoint = Join-Path (Split-Path $recovery.database -Parent) `
        "upgrade-recovery\$($recovery.attempt_id)"
    $recoveryPython = Join-Path $recoveryPoint 'runtime\Scripts\python.exe'
    $recoveryRunner = Join-Path $recoveryPoint 'recover.py'
    try {
        & $python $coordinator apply --repo $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'Update validation failed.'
        }
        if (Test-Path $desktop) {
            & (Join-Path $RepoRoot 'scripts\migrate-to-electron.ps1') -SkipPull `
                -Rebuild:$shellRebuild -RecoveryOwnedByCaller
            if ($LASTEXITCODE -ne 0) {
                throw 'The desktop update did not become ready.'
            }
        } else {
            Start-Server -Port $Port
        }
    } catch {
        Write-Host "  Update failed. Starting saved-version recovery..." -ForegroundColor Red
        Stop-Server -Port $Port
        if ((Get-FileHash $recoveryRunner -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            $recovery.runner_sha256 -or
            (Get-FileHash (Join-Path $recoveryPoint 'db_paths.py') -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            $recovery.backup_helper_sha256) {
            throw 'The saved recovery entry points failed verification. Nothing was restored.'
        }
        & $recoveryPython $recoveryRunner launch-recovery --repo $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'Automatic recovery could not start. Do not use this installation; retain its logs.'
        }
        throw 'Update failed. Automatic recovery has started and diagnostics will be queued.'
    }
}

# Install/update pip dependencies
function Install-Dependencies {
    Write-Host "  Installing dependencies..." -ForegroundColor Yellow
    Write-Host "  (First run may take a minute or two)" -ForegroundColor Gray
    $pipExe = Join-Path $RepoRoot 'venv\Scripts\pip.exe'
    $reqFile = Join-Path $RepoRoot 'requirements.txt'

    # Run pip directly with --quiet. Suppress stderr (2>$null) because pip
    # writes [notice] upgrade nags there.
    & $pipExe install -r $reqFile --quiet

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  pip exited with code $LASTEXITCODE" -ForegroundColor Red
        return $false
    }
    return $true
}

# ==============================================================================
# Main
# ==============================================================================

Write-Host ""
Write-Host "  Sales Buddy" -ForegroundColor Cyan
Write-Host "  ==========" -ForegroundColor Cyan
Write-Host ""
if ($script:AzCliContextError) {
    $err = $script:AzCliContextError
    $pos = $err.InvocationInfo
    $location = if ($pos -and $pos.ScriptName) {
        "$($pos.ScriptName):$($pos.ScriptLineNumber)"
    } else { 'unknown' }
    Write-Host "  [FAILURE] Config directory setup failed" -ForegroundColor Red
    Write-Host "            Error: $($err.Exception.Message)" -ForegroundColor Red
    Write-Host "            Location: $location" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Azure CLI isolation is not active. The server will start but az commands" -ForegroundColor Yellow
    Write-Host "  may use the default Azure CLI config (~\.azure) instead of the isolated one." -ForegroundColor Yellow
    Write-Host ""
} elseif ($script:AzCliContextFresh) {
    Write-Host "  [WARNING] No existing Az CLI context detected, new Az Login required" -ForegroundColor Yellow
} else {
    Write-Host "  [OK] Config directory and environment variables set as $script:ResolvedEnvLabel" -ForegroundColor Green
}

# -StopOnly: Read port from .env, stop the server, and exit immediately.
# Skips all prereq checks (Python, Git, venv, etc.) for instant execution.
if ($StopOnly) {
    $envConfig = Read-EnvFile
    $Port = if ($envConfig['PORT']) { [int]$envConfig['PORT'] } else { 5151 }
    try {
        Stop-Server -Port $Port
        Write-Host "  [OK] Backend stopped." -ForegroundColor Green
    } catch {
        Write-Host "  [ERROR] $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
    Start-Sleep -Seconds 1
    exit 0
}

# -- Step 1: Check Python -----------------------------------------------------
function Test-PythonOk {
    param([string]$PythonExe = 'python')
    try {
        $ver = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
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
    # Check NuGet-installed Python (MSI installer puts it here)
    $nugetPython = Join-Path $env:LOCALAPPDATA 'python\python.exe'
    if (Test-Path $nugetPython) {
        $pyVersion = Test-PythonOk -PythonExe $nugetPython
        if ($pyVersion) {
            Write-Host "  [OK] Python $pyVersion (NuGet)" -ForegroundColor Green
            # Prepend to PATH so venv creation and pip use this Python
            $nugetDir = Join-Path $env:LOCALAPPDATA 'python'
            $env:Path = "$nugetDir;$(Join-Path $nugetDir 'Scripts');$env:Path"
            $pythonOk = $true
        }
    }
}

if (-not $pythonOk) {
    if ($null -eq $pyVersion) {
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
    Wait-WithMessage "Press any key to close..." "Red"
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
$createdVenv = $false
if (-not (Test-Path (Join-Path $RepoRoot 'venv\Scripts\python.exe'))) {
    Write-Host "  [SETUP] Creating virtual environment..." -ForegroundColor Yellow
    & python -m venv (Join-Path $RepoRoot 'venv')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Failed to create virtual environment." -ForegroundColor Red
        Wait-WithMessage "Press any key to close..." "Red"
        exit 1
    }
    Write-Host "  [OK] Virtual environment created." -ForegroundColor Green
    $createdVenv = $true
} else {
    Write-Host "  [OK] Virtual environment found." -ForegroundColor Green
}

# -- Step 6: Install dependencies ---------------------------------------------
$dependencyCheck = Join-Path $RepoRoot 'venv\Scripts\python.exe'
if (-not $createdVenv) {
    & $dependencyCheck -c "import flask, sqlalchemy, dotenv"
    $missingDependencies = $LASTEXITCODE -ne 0
}
if ($createdVenv) {
    if (-not (Install-Dependencies)) {
        Write-Host "  [ERROR] Failed to install dependencies." -ForegroundColor Red
        Wait-WithMessage "Press any key to close..." "Red"
        exit 1
    }
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

        Write-Host "  [OK] Automatic backups configured (database + notes)." -ForegroundColor Green
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
    $vbsLauncher = Join-Path $PSScriptRoot 'run-hidden.vbs'
    # wscript + VBS launcher avoids the brief console flash that
    # powershell.exe -WindowStyle Hidden still produces.
    $asAction = New-ScheduledTaskAction `
        -Execute 'wscript.exe' `
        -Argument "`"$vbsLauncher`" `"$serverScript`"" `
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

# ==============================================================================
# Decision Logic
# ==============================================================================

# Both launchers share the same verified snapshot and migration gates.
if ($Force -or $hasUpdates) {
    Write-Host ""
    Write-Host "  Updating..." -ForegroundColor Cyan

    try {
        if (-not $isGitRepo) { throw 'A healthy Git installation is required to update safely.' }
        Invoke-SafeUpdate -Port $Port
    } catch {
        Write-Host "  [ERROR] $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  No automatic database rollback was performed." -ForegroundColor Yellow
        Wait-WithMessage "UPDATE FAILED - press any key to close..." "Red"
        exit 1
    }

    Start-Server -Port $Port
    Write-Host ""
    Write-Host "  Update complete!" -ForegroundColor Green
    Wait-WithMessage "Press any key to close..."
    exit 0
}

# Smart mode: do whatever makes sense

if ($serverRunning -and -not $hasUpdates) {
    Write-Host ""
    Write-Host "  Server is already running on port $Port and up to date." -ForegroundColor Green
    Wait-WithMessage "Press any key to close..."
    exit 0
}

# Register protocol handler (idempotent)
Register-ProtocolHandler

# Start server
try {
    if ($missingDependencies -and -not (Install-Dependencies)) {
        throw 'Required dependencies could not be repaired.'
    }
    Start-Server -Port $Port
} catch {
    Write-Host "  [ERROR] $($_.Exception.Message)" -ForegroundColor Red
    Wait-WithMessage "STARTUP FAILED - press any key to close..." "Red"
    exit 1
}
Write-Host ""
Write-Host "  Sales Buddy is running! Open in your browser:" -ForegroundColor Green
Write-Host ""
Write-Host "  http://localhost:$Port" -ForegroundColor Cyan
Write-Host ""
Wait-WithMessage "Press any key to close this window..."
