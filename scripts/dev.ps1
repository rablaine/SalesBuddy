# Sales Buddy - Dev Server Control
#
# Reliable start/stop for the local development Flask server. Fixes the
# Windows + Werkzeug-reloader problem where Ctrl+C leaves orphaned python
# processes still holding the dev database (which then blocks DB swaps and
# eats the port).
#
# Usage:
#   .\scripts\dev.ps1            Start (auto-kills any stragglers first)
#   .\scripts\dev.ps1 start      Same as above
#   .\scripts\dev.ps1 stop       Kill ALL dev flask processes for this repo
#   .\scripts\dev.ps1 restart    Stop then start
#   .\scripts\dev.ps1 status     Show running dev flask processes
#   .\scripts\dev.ps1 start -Port 5050
#
# Notes:
#   - Targets ONLY Flask processes launched from THIS repo's venv, so it never
#     touches the installed production app (waitress on 5151) or other repos.
#   - Sets FLASK_ENV=development and points Azure CLI at the dev-isolated
#     config dir (%USERPROFILE%\SalesBuddyDev\.azure) so MSX calls use the
#     dev sign-in. Both env vars are restored to their previous values when
#     flask exits, so the caller's terminal is left untouched (important: the
#     dev config dir must stay on the corp tenant, and leaking it would break
#     later `az` work such as MSI code signing in the same shell).
#   - 'start' runs in the foreground so you see logs; Ctrl+C to stop normally,
#     and if a straggler survives, the next 'start' (or 'stop') clears it.

param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [int]$Port = 5000
)

# Allow dash-prefixed action switches (e.g. `dev.ps1 -Stop`) in addition to the
# positional form (`dev.ps1 stop`). Without these, a token like `-Stop` doesn't
# bind to $Action, silently falls through to the default 'start', and the
# server gets relaunched instead of stopped. A switch wins over the positional
# default; if several are passed, the most "destructive" one wins.
if ($Stop) { $Action = 'stop' }
elseif ($Restart) { $Action = 'restart' }
elseif ($Status) { $Action = 'status' }
elseif ($Start) { $Action = 'start' }

$RepoRoot = Split-Path $PSScriptRoot -Parent
$VenvFlask = Join-Path $RepoRoot 'venv\Scripts\flask.exe'
$VenvPython = Join-Path $RepoRoot 'venv\Scripts\python.exe'

function Get-DevFlask {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            if (-not $_.CommandLine) { return $false }

            $usesRepoVenv = (
                $_.CommandLine -like "*$VenvFlask*" -or
                $_.CommandLine -like "*$VenvPython*"
            )
            $runsFlask = (
                $_.CommandLine -like '*flask.exe*run*' -or
                $_.CommandLine -like '*-m flask*run*' -or
                $_.CommandLine -like '*-m app.worker*'
            )
            $usesRepoVenv -and $runsFlask
        }
}

function Stop-DevProcessFamily([int[]]$RootIds) {
    if (-not $RootIds.Count) { return }
    $ownedIds = @($RootIds)
    $processes = @(Get-CimInstance Win32_Process)
    do {
        $children = @($processes | Where-Object {
            $ownedIds -contains $_.ParentProcessId -and
            $ownedIds -notcontains $_.ProcessId
        })
        $ownedIds += @($children.ProcessId)
    } while ($children.Count -gt 0)
    foreach ($process in ($processes | Where-Object {
        $ownedIds -contains $_.ProcessId
    } | Sort-Object CreationDate)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-DevFlask {
    $procs = @(Get-DevFlask)
    if ($procs.Count -eq 0) {
        Write-Host "No dev flask processes running." -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) {
        Write-Host "Stopping dev flask PID $($p.ProcessId)" -ForegroundColor Yellow
    }
    Stop-DevProcessFamily -RootIds @($procs.ProcessId)
    Start-Sleep -Milliseconds 400
    $left = @(Get-DevFlask).Count
    if ($left -eq 0) {
        Write-Host "Stopped $($procs.Count) process(es)." -ForegroundColor Green
    }
    else {
        Write-Host "Warning: $left process(es) still alive." -ForegroundColor Red
    }
}

function Show-Status {
    $procs = @(Get-DevFlask)
    if ($procs.Count -eq 0) {
        Write-Host "Dev flask: not running." -ForegroundColor DarkGray
        return
    }
    Write-Host "Dev flask: $($procs.Count) process(es)" -ForegroundColor Green
    $procs | Select-Object ProcessId, CommandLine | Format-List
}

function Start-DevFlask {
    if (-not (Test-Path $VenvFlask)) {
        throw "flask.exe not found at $VenvFlask. Create the venv and install requirements first."
    }

    # Clean slate: clear any stragglers from a previous Ctrl+C.
    Stop-DevFlask

    Set-Location $RepoRoot
    & (Join-Path $RepoRoot 'venv\Scripts\Activate.ps1')

    # Point Flask + Azure CLI at the dev-isolated config for the duration of
    # this run ONLY. These are process-session env vars, so we must restore the
    # caller's originals when flask exits - otherwise every later `az` command in
    # this terminal silently targets SalesBuddyDev\.azure (which must stay signed
    # into the corp tenant), clobbering unrelated az work like MSI code signing.
    $prevFlaskEnv = $env:FLASK_ENV
    $prevAzureConfigDir = $env:AZURE_CONFIG_DIR
    $prevSupervised = $env:SALESBUDDY_SUPERVISED
    $prevDevStack = $env:SALESBUDDY_DEV_STACK
    $prevBootId = $env:SALESBUDDY_BOOT_ID

    $env:FLASK_ENV = 'development'
    $env:AZURE_CONFIG_DIR = Join-Path $env:USERPROFILE 'SalesBuddyDev\.azure'
    $env:SALESBUDDY_SUPERVISED = '1'
    $env:SALESBUDDY_DEV_STACK = '1'
    Remove-Item Env:\SALESBUDDY_BOOT_ID -ErrorAction SilentlyContinue

    Write-Host "Starting dev flask on port $Port (FLASK_ENV=development)" -ForegroundColor Cyan
    Write-Host "Azure config dir: $env:AZURE_CONFIG_DIR" -ForegroundColor DarkGray
    Write-Host "Ctrl+C to stop (or run: .\scripts\dev.ps1 stop)" -ForegroundColor DarkGray
    Write-Host ""

    $worker = $null
    $web = $null
    try {
        $worker = Start-Process -FilePath $VenvPython -ArgumentList '-m', 'app.worker' `
            -WorkingDirectory $RepoRoot -PassThru -NoNewWindow
        Write-Host "Started durable worker PID $($worker.Id)" -ForegroundColor Cyan
        $web = Start-Process -FilePath $VenvPython `
            -ArgumentList '-m', 'flask', 'run', '--port', "$Port" `
            -WorkingDirectory $RepoRoot -PassThru -NoNewWindow
        while (-not $web.WaitForExit(1000)) {
            if ($worker.HasExited) {
                Write-Warning "Durable worker exited ($($worker.ExitCode)); restarting in 5 seconds."
                Start-Sleep -Seconds 5
                if ($web.HasExited) { break }
                $worker = Start-Process -FilePath $VenvPython -ArgumentList '-m', 'app.worker' `
                    -WorkingDirectory $RepoRoot -PassThru -NoNewWindow
                Write-Host "Restarted durable worker PID $($worker.Id)" -ForegroundColor Cyan
            }
        }
    }
    finally {
        $ownedIds = @(@($web, $worker) | Where-Object { $null -ne $_ } |
            ForEach-Object { $_.Id })
        Stop-DevProcessFamily -RootIds $ownedIds
        # Restore (or remove, if they weren't set before) so the caller's shell
        # is left exactly as we found it.
        if ($null -eq $prevFlaskEnv) { Remove-Item Env:\FLASK_ENV -ErrorAction SilentlyContinue }
        else { $env:FLASK_ENV = $prevFlaskEnv }

        if ($null -eq $prevAzureConfigDir) { Remove-Item Env:\AZURE_CONFIG_DIR -ErrorAction SilentlyContinue }
        else { $env:AZURE_CONFIG_DIR = $prevAzureConfigDir }
        if ($null -eq $prevSupervised) { Remove-Item Env:\SALESBUDDY_SUPERVISED -ErrorAction SilentlyContinue }
        else { $env:SALESBUDDY_SUPERVISED = $prevSupervised }
        if ($null -eq $prevDevStack) { Remove-Item Env:\SALESBUDDY_DEV_STACK -ErrorAction SilentlyContinue }
        else { $env:SALESBUDDY_DEV_STACK = $prevDevStack }
        if ($null -eq $prevBootId) { Remove-Item Env:\SALESBUDDY_BOOT_ID -ErrorAction SilentlyContinue }
        else { $env:SALESBUDDY_BOOT_ID = $prevBootId }
    }
}

switch ($Action) {
    'start' { Start-DevFlask }
    'stop' { Stop-DevFlask }
    'restart' { Stop-DevFlask; Start-DevFlask }
    'status' { Show-Status }
}
