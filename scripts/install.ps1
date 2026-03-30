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
        if (Get-Command $Command -ErrorAction SilentlyContinue) {
            # Python and git have Windows Store stubs in WindowsApps that exist
            # as aliases but don't actually work. Verify the command runs.
            if ($Command -eq 'python') {
                $ver = & python --version 2>&1
                return ($LASTEXITCODE -eq 0 -and $ver -match 'Python \d')
            }
            return $true
        }
    } catch {}
    return $false
}

function Find-Winget {
    # MSI custom actions run in limited PATH contexts where WindowsApps isn't included.
    # Check common locations before concluding winget is missing.
    if (Test-CommandExists 'winget') { return $true }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'),
        'C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_*\winget.exe'
    )
    foreach ($candidate in $candidates) {
        $found = Get-Item $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            Write-Log "Found winget at $($found.FullName), adding to PATH."
            $dir = Split-Path $found.FullName
            $env:Path += ";$dir"
            return $true
        }
    }
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
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $result = winget install $PackageId --silent --accept-package-agreements --accept-source-agreements 2>&1
        if ($LASTEXITCODE -eq 0) {
            Refresh-Path
            Write-Log "$Name installed successfully."
            return $true
        }
        # Exit code -1978335189 means "already installed" in winget
        if ($LASTEXITCODE -eq -1978335189) {
            Write-Log "$Name already installed (winget confirmed)."
            Refresh-Path
            return $true
        }
        # Exit code -1978334974 wraps MSI error 1618 (another install in progress).
        # Wait for the mutex to release and retry.
        if ($LASTEXITCODE -eq -1978334974 -and $attempt -lt $maxAttempts) {
            Write-Log "$Name install hit MSI mutex (another install in progress). Waiting 15s before retry ($attempt/$maxAttempts)..." 'WARN'
            Start-Sleep -Seconds 15
            continue
        }
        Write-Log "Failed to install $Name (exit code: $LASTEXITCODE). Output: $result" 'ERROR'
        return $false
    }
    return $false
}

function Get-PortFromEnv {
    $envFile = Join-Path $InstallDir '.env'
    if (Test-Path $envFile) {
        $lines = Get-Content $envFile
        foreach ($line in $lines) {
            if ($line -match '^\s*PORT\s*=\s*(\d+)') {
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

# -- Step 1: Install winget if missing ----------------------------------------
if (-not $SkipPrereqs -and -not (Find-Winget)) {
    Write-Log "winget not found. Installing from GitHub..."
    try {
        # Ensure TLS 1.2 is enabled (fresh Windows VMs may default to older TLS)
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

        # winget requires the VCLibs dependency and the UI.Xaml framework on clean Windows
        $vcLibsUrl = 'https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx'
        $vcLibsPath = Join-Path $env:TEMP 'VCLibs.appx'
        Write-Log "Downloading VCLibs..."
        Invoke-WebRequest -Uri $vcLibsUrl -OutFile $vcLibsPath -UseBasicParsing
        Add-AppxPackage -Path $vcLibsPath -ErrorAction SilentlyContinue

        # Download UI.Xaml from official NuGet (.nupkg is a zip containing the .appx)
        $xamlNupkgUrl = 'https://www.nuget.org/api/v2/package/Microsoft.UI.Xaml/2.8.6'
        $xamlNupkgPath = Join-Path $env:TEMP 'Microsoft.UI.Xaml.2.8.6.nupkg.zip'
        $xamlExtractDir = Join-Path $env:TEMP 'UIXaml-nupkg'
        Write-Log "Downloading UI.Xaml from NuGet..."
        Invoke-WebRequest -Uri $xamlNupkgUrl -OutFile $xamlNupkgPath -UseBasicParsing
        if (Test-Path $xamlExtractDir) { Remove-Item $xamlExtractDir -Recurse -Force }
        Expand-Archive -Path $xamlNupkgPath -DestinationPath $xamlExtractDir -Force
        $xamlAppx = Join-Path $xamlExtractDir 'tools\AppX\x64\Release\Microsoft.UI.Xaml.2.8.appx'
        if (Test-Path $xamlAppx) {
            Write-Log "Installing UI.Xaml from NuGet package..."
            Add-AppxPackage -Path $xamlAppx -ErrorAction SilentlyContinue
        } else {
            Write-Log "UI.Xaml .appx not found in NuGet package (expected at $xamlAppx). Continuing without it." 'WARN'
        }

        # Get the latest winget release URL from GitHub API
        Write-Log "Downloading winget..."
        $releaseInfo = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/winget-cli/releases/latest' -UseBasicParsing
        $msixUrl = ($releaseInfo.assets | Where-Object { $_.name -match '\.msixbundle$' }).browser_download_url
        $licenseUrl = ($releaseInfo.assets | Where-Object { $_.name -match 'License.*\.xml$' }).browser_download_url

        $msixPath = Join-Path $env:TEMP 'winget.msixbundle'
        Invoke-WebRequest -Uri $msixUrl -OutFile $msixPath -UseBasicParsing

        if ($licenseUrl) {
            $licensePath = Join-Path $env:TEMP 'winget-license.xml'
            Invoke-WebRequest -Uri $licenseUrl -OutFile $licensePath -UseBasicParsing
            # Add-AppxProvisionedPackage requires admin/SYSTEM - try it but don't let it
            # abort the whole block if we're running as a normal user (MSI Impersonate=yes).
            try {
                Add-AppxProvisionedPackage -Online -PackagePath $msixPath -LicensePath $licensePath -ErrorAction Stop
                Write-Log "winget provisioned system-wide."
            } catch {
                Write-Log "Provisioned install failed (expected if not admin): $($_.Exception.Message)" 'WARN'
                Write-Log "Falling back to per-user Add-AppxPackage..."
            }
        }
        Add-AppxPackage -Path $msixPath -ErrorAction SilentlyContinue

        Refresh-Path
        # winget may be available via the WindowsApps path that isn't in PATH yet
        $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
        if ($env:Path -notlike "*$windowsApps*") {
            $env:Path += ";$windowsApps"
        }

        if (Test-CommandExists 'winget') {
            Write-Log "winget installed successfully."
        } else {
            Write-Log "winget installation completed but command not found yet. Continuing..." 'WARN'
        }
    } catch {
        Write-Log "Failed to install winget: $_" 'WARN'
        Write-Log "Will check if prerequisites are already installed." 'WARN'
    }
}

# -- Step 2: Install prerequisites (no MSI installers - avoids mutex) ----------
if (-not $SkipPrereqs) {

    # -- Git via winget (Inno Setup EXE, not MSI - safe during our custom action)
    if (-not (Test-CommandExists 'winget')) {
        Write-Log "winget not available. Skipping winget-based installs." 'WARN'
    } elseif (-not (Test-CommandExists 'git')) {
        Install-Prereq -Name 'Git' -PackageId 'Git.Git' -TestCommand 'git'
        Refresh-Path
    } else {
        Write-Log "Git already installed, skipping."
    }

    # -- Python via NuGet zip extraction (MSI-free) ----------------------------
    if (-not (Test-CommandExists 'python')) {
        Write-Log "Installing Python via NuGet package (avoids MSI mutex)..."
        try {
            $pythonInstallDir = Join-Path $env:LOCALAPPDATA 'python'
            $pythonNupkgUrl = 'https://www.nuget.org/api/v2/package/python/3.13.2'
            $pythonNupkgPath = Join-Path $env:TEMP 'python-3.13.2.nupkg.zip'
            $pythonExtractDir = Join-Path $env:TEMP 'python-nupkg'

            Write-Log "Downloading Python 3.13.2 from NuGet..."
            Invoke-WebRequest -Uri $pythonNupkgUrl -OutFile $pythonNupkgPath -UseBasicParsing

            Write-Log "Extracting Python..."
            if (Test-Path $pythonExtractDir) { Remove-Item $pythonExtractDir -Recurse -Force }
            Expand-Archive -Path $pythonNupkgPath -DestinationPath $pythonExtractDir -Force

            # NuGet python package has the full CPython in tools/
            $pythonToolsDir = Join-Path $pythonExtractDir 'tools'
            if (Test-Path (Join-Path $pythonToolsDir 'python.exe')) {
                if (Test-Path $pythonInstallDir) { Remove-Item $pythonInstallDir -Recurse -Force }
                Move-Item $pythonToolsDir $pythonInstallDir -Force

                # Add python and Scripts to user PATH permanently
                $pythonScriptsDir = Join-Path $pythonInstallDir 'Scripts'
                $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                $pathsToAdd = @($pythonInstallDir, $pythonScriptsDir)
                foreach ($p in $pathsToAdd) {
                    if ($userPath -notlike "*$p*") {
                        # Prepend so our Python beats the WindowsApps Store stub
                        $userPath = "$p;$userPath"
                    }
                    # Prepend so our real Python beats the WindowsApps Store stub
                    if ($env:Path -notlike "*$p*") {
                        $env:Path = "$p;$env:Path"
                    }
                }
                [Environment]::SetEnvironmentVariable('Path', $userPath, 'User')

                # Bootstrap pip (NuGet package may not have it ready)
                Write-Log "Bootstrapping pip..."
                & (Join-Path $pythonInstallDir 'python.exe') -m ensurepip --upgrade 2>&1 | ForEach-Object { Write-Log "  [pip] $_" }

                Write-Log "Python 3.13.2 installed to $pythonInstallDir."
            } else {
                Write-Log "Python tools/ directory not found in NuGet package." 'ERROR'
            }
        } catch {
            Write-Log "Failed to install Python via NuGet: $_" 'ERROR'
        }
    } else {
        Write-Log "Python already installed, skipping."
    }

    # Verify critical commands
    Refresh-Path
    # Prepend our install dirs so they beat the WindowsApps Store aliases.
    # Duplicates are harmless - simpler than remove-and-prepend which risks
    # accidentally dropping entries.
    $localPython = Join-Path $env:LOCALAPPDATA 'python'
    $localPythonScripts = Join-Path $localPython 'Scripts'
    $localNode = Join-Path $env:LOCALAPPDATA 'nodejs'
    $prepend = @($localPython, $localPythonScripts, $localNode) | Where-Object { Test-Path $_ }
    if ($prepend) {
        $env:Path = ($prepend -join ';') + ';' + $env:Path
        Write-Log "PATH prepended with: $($prepend -join ', ')"
    }
    foreach ($cmd in @('git', 'python')) {
        if (-not (Test-CommandExists $cmd)) {
            Write-Log "$cmd is required but not found on PATH. Please install it manually." 'ERROR'
            exit 1
        }
    }
    Write-Log "Required commands (git, python) verified."

    # -- Azure CLI via pip (no MSI) --------------------------------------------
    if (-not (Test-CommandExists 'az')) {
        Write-Log "Installing Azure CLI via pip (this may take several minutes)..."
        $pyExe = Join-Path $env:LOCALAPPDATA 'python\python.exe'
        if (-not (Test-Path $pyExe)) { $pyExe = 'python' }
        & $pyExe -m pip install azure-cli --quiet 2>&1 | ForEach-Object { Write-Log "  [az] $_" }
        Refresh-Path
        if (Test-CommandExists 'az') {
            Write-Log "Azure CLI installed successfully."
        } else {
            # pip puts az.cmd in Python's Scripts dir - add it explicitly
            $pyDir = Split-Path (Get-Command python -ErrorAction SilentlyContinue).Source
            $pyScripts = Join-Path $pyDir 'Scripts'
            if (Test-Path (Join-Path $pyScripts 'az.cmd')) {
                $env:Path += ";$pyScripts"
                Write-Log "Azure CLI installed (added $pyScripts to PATH)."
            } else {
                Write-Log "Azure CLI pip install completed but 'az' not found." 'WARN'
            }
        }
    } else {
        Write-Log "Azure CLI already installed, skipping."
    }

    # -- Node.js via zip extraction (no MSI) -----------------------------------
    if (-not (Test-CommandExists 'node')) {
        Write-Log "Installing Node.js via zip extraction..."
        try {
            $nodeInstallDir = Join-Path $env:LOCALAPPDATA 'nodejs'
            $nodeVersions = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -UseBasicParsing
            $latestLts = ($nodeVersions | Where-Object { $_.lts -and $_.lts -ne $false }) | Select-Object -First 1
            $nodeVersion = $latestLts.version
            $nodeZipUrl = "https://nodejs.org/dist/$nodeVersion/node-$nodeVersion-win-x64.zip"
            $nodeZipPath = Join-Path $env:TEMP "node-$nodeVersion-win-x64.zip"

            Write-Log "Downloading Node.js $nodeVersion..."
            Invoke-WebRequest -Uri $nodeZipUrl -OutFile $nodeZipPath -UseBasicParsing

            Write-Log "Extracting Node.js..."
            $nodeExtractDir = Join-Path $env:TEMP 'nodejs-extract'
            if (Test-Path $nodeExtractDir) { Remove-Item $nodeExtractDir -Recurse -Force }
            Expand-Archive -Path $nodeZipPath -DestinationPath $nodeExtractDir -Force

            $innerDir = Get-ChildItem $nodeExtractDir -Directory | Select-Object -First 1
            if ($innerDir) {
                if (Test-Path $nodeInstallDir) { Remove-Item $nodeInstallDir -Recurse -Force }
                Move-Item $innerDir.FullName $nodeInstallDir -Force

                $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                if ($userPath -notlike "*$nodeInstallDir*") {
                    [Environment]::SetEnvironmentVariable('Path', "$userPath;$nodeInstallDir", 'User')
                }
                $env:Path = "$nodeInstallDir;$env:Path"
                Write-Log "Node.js $nodeVersion installed to $nodeInstallDir."
            } else {
                Write-Log "Node.js zip extraction failed - no inner directory found." 'WARN'
            }
        } catch {
            Write-Log "Failed to install Node.js via zip: $_" 'WARN'
        }
    } else {
        Write-Log "Node.js already installed, skipping."
    }
}

# -- Step 3: Clone the repository --------------------------------------------
# Disable Git Credential Manager to prevent login popups (public repo, no auth needed).
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'

if (Test-Path (Join-Path $InstallDir '.git')) {
    Write-Log "Repository already exists at $InstallDir, pulling latest."
    Push-Location $InstallDir
    # Use fetch + reset instead of pull to handle dirty working trees from
    # previous installs (untracked scripts, modified files, etc.).
    git -c credential.helper= fetch origin 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git reset --hard origin/main 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git clean -fd 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    Pop-Location
} elseif (Test-Path $InstallDir) {
    # Directory exists but isn't a git repo (MSI installed bootstrap files here).
    # Initialize a repo in-place and pull rather than moving the directory.
    Write-Log "Directory exists at $InstallDir, initializing git repository..."
    Refresh-Path
    Push-Location $InstallDir
    git init 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git remote add origin $RepoUrl 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
    git -c credential.helper= fetch origin 2>&1 | ForEach-Object { Write-Log "  [git] $_" }
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
    $cloneOutput = git -c credential.helper= clone $RepoUrl $InstallDir 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Failed to clone repository. Output: $cloneOutput" 'ERROR'
        exit 2
    }
    Write-Log "Repository cloned successfully."
}

# -- Step 4: Environment setup (venv, pip, .env, migrations) ------------------
# We do this inline rather than delegating to server.ps1 because the background
# process would lose our current PATH (which has newly-installed tools).
Refresh-Path

# Resolve the actual Python executable - must use full path to avoid the
# Windows Store alias in WindowsApps which intercepts bare "python" commands.
$pythonExe = Join-Path $env:LOCALAPPDATA 'python\python.exe'
if (-not (Test-Path $pythonExe)) {
    # Fall back to PATH-based lookup
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $pythonExe = $pythonCmd.Source }
}
Write-Log "Using Python at: $pythonExe"

# 4a: Create venv
$venvPython = Join-Path $InstallDir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Log "Creating virtual environment..."
    & $pythonExe -m venv (Join-Path $InstallDir 'venv') 2>&1 | ForEach-Object { Write-Log "  [venv] $_" }
    if (-not (Test-Path $venvPython)) {
        Write-Log "Failed to create virtual environment." 'ERROR'
        exit 3
    }
    Write-Log "Virtual environment created."
} else {
    Write-Log "Virtual environment already exists."
}

# 4b: pip install
$pipExe = Join-Path $InstallDir 'venv\Scripts\pip.exe'
$reqFile = Join-Path $InstallDir 'requirements.txt'
if (Test-Path $reqFile) {
    Write-Log "Installing Python dependencies (this may take a minute)..."
    & $pipExe install -r $reqFile --quiet 2>&1 | ForEach-Object { Write-Log "  [pip] $_" }
    Write-Log "Dependencies installed (exit code: $LASTEXITCODE)."
}

# 4c: Create .env if missing
$envFile = Join-Path $InstallDir '.env'
if (-not (Test-Path $envFile)) {
    $exampleFile = Join-Path $InstallDir '.env.example'
    if (Test-Path $exampleFile) {
        Write-Log "Creating .env from .env.example..."
        Copy-Item $exampleFile $envFile
        $secretKey = & $venvPython -c "import secrets; print(secrets.token_hex(32))" 2>$null
        if ($secretKey) {
            $content = Get-Content $envFile -Raw
            $content = $content.Replace('your-secret-key-here-change-in-production', $secretKey)
            Set-Content $envFile $content -NoNewline
        }
        Write-Log ".env created."
    }
}

# 4d: Run migrations
Write-Log "Running database migrations..."
Push-Location $InstallDir
$migrationOutput = & $venvPython -c "from app import create_app, db; from app.migrations import run_migrations; app = create_app(); app.app_context().push(); run_migrations(db)" 2>&1
$migrationOutput | ForEach-Object { Write-Log "  [migrate] $_" }
Write-Log "Migrations complete (exit code: $LASTEXITCODE)."
Pop-Location

# 4e: Start the server (background - waitress in a hidden window)
$waitressExe = Join-Path $InstallDir 'venv\Scripts\waitress-serve.exe'
if (Test-Path $waitressExe) {
    Write-Log "Starting server on port $DefaultPort..."
    Start-Process -FilePath $waitressExe `
        -ArgumentList '--host=0.0.0.0', "--port=$DefaultPort", '--call', 'app:create_app' `
        -WorkingDirectory $InstallDir `
        -WindowStyle Hidden
    Write-Log "Server launched in background."
} else {
    Write-Log "waitress-serve.exe not found. Server not started. User can run start.bat." 'WARN'
}

# -- Step 5: Create shortcuts -------------------------------------------------
$Port = Get-PortFromEnv
$AppUrl = "http://localhost:$Port"
# Icon is installed by MSI into the install dir root; fall back to cloned repo copy
$MsiInstallDir = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
$IconPath = Join-Path $MsiInstallDir 'icon.ico'
if (-not (Test-Path $IconPath)) {
    $IconPath = Join-Path $InstallDir 'static\icon.ico'
}
Write-Log "Icon path: $IconPath (exists: $(Test-Path $IconPath))"

if ($Shortcuts) {
    Write-Log "Creating Start Menu shortcuts..."
    if (-not (Test-Path $StartMenuFolder)) {
        New-Item -ItemType Directory -Path $StartMenuFolder -Force | Out-Null
    }

    # Main app shortcut - uses explorer.exe to open URL (supports custom icons)
    New-Shortcut `
        -Path (Join-Path $StartMenuFolder "$AppName.lnk") `
        -TargetPath 'explorer.exe' `
        -Arguments $AppUrl `
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
        -TargetPath 'explorer.exe' `
        -Arguments $AppUrl `
        -IconLocation "$IconPath,0" `
        -Description 'Open Sales Buddy in your browser'
    Write-Log "Desktop shortcut created."
}

# -- Step 6: Launch browser ---------------------------------------------------
if ($LaunchBrowser) {
    Write-Log "Launching browser to $AppUrl..."
    # Start-Process with a URL fails in MSI custom action context (no shell associations).
    # Use explorer.exe which always handles URLs.
    Start-Process 'explorer.exe' $AppUrl
}

# -- Done --------------------------------------------------------------------
Write-Log "=== Sales Buddy Installation Complete ==="
exit 0
