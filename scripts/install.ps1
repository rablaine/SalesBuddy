# Sales Buddy - MSI Installer Bootstrap Script
# Called as a custom action by the MSI installer during the progress page.
# Runs silently (no terminal windows, no user prompts).
#
# Usage:
#   .\scripts\install.ps1                              Full install (all steps)
#   .\scripts\install.ps1 -Shortcuts -DesktopShortcut  Create shortcuts after install
#   .\scripts\install.ps1 -SkipPrereqs                 Skip prereq installation (dev/testing)
#
# Exit codes:
#   0  Success
#   1  Fatal error (prerequisites failed)
#   2  Clone/setup failed
#   3  Server setup failed

param(
    [switch]$Shortcuts,         # Create Start Menu shortcuts
    [switch]$DesktopShortcut,   # Create desktop shortcut
    [switch]$SkipPrereqs,       # Skip prerequisite installation (for testing)
    [switch]$LaunchBrowser      # Open browser after install
)

# ==============================================================================
# Configuration
# ==============================================================================

$InstallDir = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
$RepoUrl = 'https://github.com/rablaine/SalesBuddy.git'
$DefaultPort = 5151
$AppName = 'Sales Buddy'
$StartMenuFolder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Sales Buddy'

# ==============================================================================
# Logging
# ==============================================================================

$LogFile = Join-Path $env:TEMP 'SalesBuddy-Install.log'

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$timestamp] [$Level] $Message"
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
    # Also write to stdout for MSI progress capture
    Write-Host $line
}

# ==============================================================================
# Helpers
# ==============================================================================

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machinePath;$userPath"
}

function Test-CommandExists {
    param([string]$Command)
    try {
        if (Get-Command $Command -ErrorAction SilentlyContinue) { return $true }
    } catch {}
    return $false
}

function Install-Prereq {
    param(
        [string]$Name,
        [string]$PackageId,
        [string]$TestCommand
    )
    if (Test-CommandExists $TestCommand) {
        Write-Log "$Name already installed, skipping."
        return $true
    }
    Write-Log "Installing $Name..."
    $result = winget install $PackageId --silent --accept-package-agreements --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0) {
        Refresh-Path
        Write-Log "$Name installed successfully."
        return $true
    } else {
        # Exit code -1978335189 means "already installed" in winget
        if ($LASTEXITCODE -eq -1978335189) {
            Write-Log "$Name already installed (winget confirmed)."
            Refresh-Path
            return $true
        }
        Write-Log "Failed to install $Name (exit code: $LASTEXITCODE). Output: $result" 'ERROR'
        return $false
    }
}

function Get-PortFromEnv {
    $envFile = Join-Path $InstallDir '.env'
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*PORT\s*=\s*(\d+)') {
                return [int]$Matches[1]
            }
        }
    }
    return $DefaultPort
}

function New-Shortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$Arguments = '',
        [string]$WorkingDirectory = '',
        [string]$IconLocation = '',
        [string]$Description = ''
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $TargetPath
    if ($Arguments) { $shortcut.Arguments = $Arguments }
    if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
    if ($IconLocation) { $shortcut.IconLocation = $IconLocation }
    if ($Description) { $shortcut.Description = $Description }
    $shortcut.Save()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($shell) | Out-Null
}

# ==============================================================================
# Main Install Sequence
# ==============================================================================

Write-Log "=== Sales Buddy Installation Starting ==="
Write-Log "Install directory: $InstallDir"

# -- Step 1: Check winget is available ----------------------------------------
if (-not $SkipPrereqs) {
    if (-not (Test-CommandExists 'winget')) {
        Write-Log "winget not found. Cannot install prerequisites." 'ERROR'
        Write-Log "winget is included with Windows 10 1709+ and Windows 11." 'ERROR'
        exit 1
    }
    Write-Log "winget found."
}

# -- Step 2: Install prerequisites silently -----------------------------------
if (-not $SkipPrereqs) {
    $prereqs = @(
        @{ Name = 'Git';       PackageId = 'Git.Git';            TestCommand = 'git' },
        @{ Name = 'Python';    PackageId = 'Python.Python.3.13'; TestCommand = 'python' },
        @{ Name = 'Azure CLI'; PackageId = 'Microsoft.AzureCLI'; TestCommand = 'az' },
        @{ Name = 'Node.js';   PackageId = 'OpenJS.NodeJS.LTS';  TestCommand = 'node' }
    )

    foreach ($prereq in $prereqs) {
        $ok = Install-Prereq -Name $prereq.Name -PackageId $prereq.PackageId -TestCommand $prereq.TestCommand
        if (-not $ok -and ($prereq.Name -eq 'Git' -or $prereq.Name -eq 'Python')) {
            Write-Log "$($prereq.Name) is required and could not be installed. Aborting." 'ERROR'
            exit 1
        }
    }
    Write-Log "Prerequisites complete."
}

# -- Step 3: Clone the repository --------------------------------------------
if (Test-Path (Join-Path $InstallDir '.git')) {
    Write-Log "Repository already exists at $InstallDir, pulling latest."
    Push-Location $InstallDir
    $pullOutput = git pull --ff-only 2>&1
    $pullOutput | ForEach-Object { Write-Log "  [git] $_" }
    Pop-Location
} elseif (Test-Path $InstallDir) {
    # Directory exists but isn't a git repo (MSI installed bootstrap files here).
    # Initialize a repo in-place and pull rather than moving the directory.
    Write-Log "Directory exists at $InstallDir, initializing git repository..."
    Refresh-Path
    Push-Location $InstallDir
    git init 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git remote add origin $RepoUrl 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git fetch origin 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git checkout -f -B main origin/main 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    Pop-Location
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Failed to initialize repository." 'ERROR'
        exit 2
    }
    Write-Log "Repository initialized."
} else {
    Write-Log "Cloning repository..."
    Refresh-Path
    $cloneOutput = git clone $RepoUrl $InstallDir 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Failed to clone repository. Output: $cloneOutput" 'ERROR'
        exit 2
    }
    Write-Log "Repository cloned successfully."
}

# -- Step 4: Run server.ps1 for environment setup ----------------------------
# server.ps1 -Force handles: venv creation, pip install, .env generation,
# database migrations, scheduled task registration, and server start.
# -Force skips interactive prompts and pauses.
Write-Log "Running server setup..."
$serverScript = Join-Path $InstallDir 'scripts\server.ps1'
if (-not (Test-Path $serverScript)) {
    Write-Log "server.ps1 not found at $serverScript" 'ERROR'
    exit 3
}

$serverOutput = & powershell.exe -ExecutionPolicy Bypass -NonInteractive -File $serverScript -Force 2>&1
$serverExit = $LASTEXITCODE
$serverOutput | ForEach-Object { Write-Log "  [server.ps1] $_" }

if ($serverExit -ne 0) {
    Write-Log "server.ps1 exited with code $serverExit" 'WARN'
    # Non-fatal: server.ps1 may exit non-zero but still have set everything up
}
Write-Log "Server setup complete."

# -- Step 5: Create shortcuts -------------------------------------------------
$Port = Get-PortFromEnv
$AppUrl = "http://localhost:$Port"
$IconPath = Join-Path $InstallDir 'static\icon.ico'

if ($Shortcuts) {
    Write-Log "Creating Start Menu shortcuts..."
    if (-not (Test-Path $StartMenuFolder)) {
        New-Item -ItemType Directory -Path $StartMenuFolder -Force | Out-Null
    }

    # Main app shortcut - opens browser to localhost
    New-Shortcut `
        -Path (Join-Path $StartMenuFolder "$AppName.lnk") `
        -TargetPath $AppUrl `
        -IconLocation "$IconPath,0" `
        -Description 'Open Sales Buddy in your browser'

    # Start Server shortcut
    New-Shortcut `
        -Path (Join-Path $StartMenuFolder 'Start Server.lnk') `
        -TargetPath (Join-Path $InstallDir 'start.bat') `
        -WorkingDirectory $InstallDir `
        -IconLocation "$IconPath,0" `
        -Description 'Start the Sales Buddy server'

    # Stop Server shortcut
    New-Shortcut `
        -Path (Join-Path $StartMenuFolder 'Stop Server.lnk') `
        -TargetPath (Join-Path $InstallDir 'stop.bat') `
        -WorkingDirectory $InstallDir `
        -Description 'Stop the Sales Buddy server'

    # Update shortcut
    New-Shortcut `
        -Path (Join-Path $StartMenuFolder 'Update.lnk') `
        -TargetPath (Join-Path $InstallDir 'update.bat') `
        -WorkingDirectory $InstallDir `
        -IconLocation "$IconPath,0" `
        -Description 'Update Sales Buddy to the latest version'

    Write-Log "Start Menu shortcuts created."
}

if ($DesktopShortcut) {
    Write-Log "Creating desktop shortcut..."
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    New-Shortcut `
        -Path (Join-Path $desktopPath "$AppName.lnk") `
        -TargetPath $AppUrl `
        -IconLocation "$IconPath,0" `
        -Description 'Open Sales Buddy in your browser'
    Write-Log "Desktop shortcut created."
}

# -- Step 6: Launch browser ---------------------------------------------------
if ($LaunchBrowser) {
    Write-Log "Launching browser to $AppUrl..."
    Start-Process $AppUrl
}

# -- Done --------------------------------------------------------------------
Write-Log "=== Sales Buddy Installation Complete ==="
exit 0
