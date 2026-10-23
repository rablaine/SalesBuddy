"""Exercise updater PowerShell functions without launching or changing an installation."""
import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell entry points')


def run_powershell(script: str, **environment: str) -> dict:
    """Run an isolated mocked function harness and decode its structured result."""
    completed = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        env={**os.environ, **environment}, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize('filename', ['server.ps1', 'migrate-to-electron.ps1', 'dev.ps1'])
def test_upgrade_scripts_parse_without_executing_their_entry_points(filename):
    """Syntax validation never executes prerequisite setup, app shutdown or updates."""
    result = run_powershell("""
        $tokens = $null; $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $env:SOURCE_FILE, [ref]$tokens, [ref]$errors) | Out-Null
        @{ errors = @($errors | ForEach-Object { $_.Message }) } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / filename))
    assert result['errors'] == []


@pytest.mark.parametrize('failure', ['', 'stop', 'fetch', 'diff', 'prepare', 'apply', 'ready'])
def test_both_cli_update_modes_use_fail_closed_shared_pipeline(failure):
    """Run the actual function with mocked commands, stopping at the first failed prerequisite."""
    result = run_powershell(r"""
        $ErrorActionPreference = 'Stop'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $env:SOURCE_FILE, [ref]$tokens, [ref]$errors)
        $definition = $ast.Find({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Invoke-SafeUpdate'
        }, $true)
        Invoke-Expression $definition.Extent.Text
        $RepoRoot = 'C:\isolated-fixture'
        $script:events = [System.Collections.Generic.List[string]]::new()
        function Write-Host { }
        function Join-Path {
            param($Path, $ChildPath)
            if ($ChildPath -in @('venv\Scripts\python.exe', 'runtime\Scripts\python.exe')) {
                return 'Invoke-FixturePython'
            }
            return "$Path\$ChildPath"
        }
        function Get-Content {
            param($Path, [switch]$Raw)
            return (@{ database = 'C:\isolated-data\salesbuddy.db'; attempt_id = 'a' * 32;
                runner_sha256 = 'a' * 64; backup_helper_sha256 = 'a' * 64 } | ConvertTo-Json)
        }
        function Get-FileHash {
            param($Path, $Algorithm)
            return @{ Hash = 'a' * 64 }
        }
        function Test-Path {
            param($Path)
            return $Path -notlike '*Sales Buddy.exe'
        }
        function Stop-Server {
            param($Port)
            $script:events.Add('stop')
            if ($env:FAIL_STEP -eq 'stop') { throw 'Fixture shutdown refused' }
        }
        function git {
            $step = $args[0]
            $script:events.Add($step)
            $global:LASTEXITCODE = [int]($env:FAIL_STEP -eq $step)
        }
        function Invoke-FixturePython {
            $step = $args[1]
            $script:events.Add($step)
            $global:LASTEXITCODE = [int]($env:FAIL_STEP -eq $step)
        }
        function Start-Server {
            param($Port)
            $script:events.Add('ready')
            if ($env:FAIL_STEP -eq 'ready') { throw 'Fixture readiness refused' }
        }
        $failed = $false
        try { Invoke-SafeUpdate -Port 54321 } catch { $failed = $true }
        @{ events = @($script:events); failed = $failed } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / 'server.ps1'), FAIL_STEP=failure)
    sequence = ['stop', 'fetch', 'diff', 'prepare', 'apply', 'ready']
    expected = sequence[:sequence.index(failure) + 1] if failure else sequence
    if failure in {'apply', 'ready'}:
        expected += ['stop', 'launch-recovery']
    assert result['events'] == expected
    assert result['failed'] is bool(failure)


def test_shutdown_refuses_an_unrelated_port_owner():
    """Stopping this install cannot kill another app or assume a busy port is safe."""
    result = run_powershell(r"""
        $ErrorActionPreference = 'Stop'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $env:SOURCE_FILE, [ref]$tokens, [ref]$errors)
        $definition = $ast.Find({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Stop-Server'
        }, $true)
        Invoke-Expression $definition.Extent.Text
        $RepoRoot = 'C:\isolated-fixture'
        $script:killed = [System.Collections.Generic.List[int]]::new()
        function Write-Host { }
        function Get-CimInstance {
            param($ClassName, $Filter, $ErrorAction)
            if ($Filter -eq 'ProcessId=20') {
                return [pscustomobject]@{ ProcessId = 20; CommandLine = 'C:\another-app\python.exe' }
            }
            return @(
                [pscustomobject]@{
                    ProcessId = 10
                    CommandLine = 'C:\isolated-fixture\venv\Scripts\python.exe -m app.supervisor'
                },
                [pscustomobject]@{
                    ProcessId = 20
                    CommandLine = 'C:\another-app\python.exe -m app.supervisor'
                }
            )
        }
        function Get-NetTCPConnection {
            param($LocalPort, $State, $ErrorAction)
            return [pscustomobject]@{ OwningProcess = 20 }
        }
        function Stop-ProcessTree {
            param($ProcessId, $ExcludePid)
            $script:killed.Add($ProcessId)
        }
        $failed = $false
        try { Stop-Server -Port 54321 } catch { $failed = $true }
        @{ killed = @($script:killed); failed = $failed } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / 'server.ps1'))
    assert result == {'killed': [10], 'failed': True}


def test_shell_migration_validates_backend_before_build_and_never_masks_failure():
    """Restaging cannot build stale source or advertise success without actual readiness."""
    source = (ROOT / 'scripts' / 'migrate-to-electron.ps1').read_text(encoding='utf-8')
    assert source.index('& $serverScript -StopOnly') < source.index('coordinator prepare')
    assert source.index('coordinator apply') < source.index('& $buildScript -SkipSign')
    assert 'git pull skipped/failed (continuing' not in source
    assert 'coordinator wait-ready' in source
    assert 'startup readiness still required' in source
    assert '$savedPython $runner fail --repo $repoRoot --phase shell' in source
    assert '$savedPython $runner launch-recovery --repo $repoRoot' in source


def test_cli_start_checks_both_children_and_never_uses_the_full_app_factory():
    """A listening TCP socket or migration-only Flask factory cannot complete an update."""
    source = (ROOT / 'scripts' / 'server.ps1').read_text(encoding='utf-8')
    assert 'from app import create_app' not in source
    assert "git stash" not in source
    assert "['SALESBUDDY_BOOT_ID'] = $bootId" in source
    assert 'coordinator wait-ready --repo $RepoRoot --boot-id $bootId' in source
    assert 'coordinator accept --repo $RepoRoot --boot-id $bootId' in source
    assert 'if ($Force -or $hasUpdates)' in source


@pytest.mark.parametrize('launch_exit', [0, 1])
def test_electron_owned_migration_does_not_run_a_second_readiness_or_recovery_coordinator(launch_exit):
    """Execute the actual launch section and refuse even one competing readiness poll."""
    result = run_powershell(r"""
        $ErrorActionPreference = 'Stop'
        $source = Get-Content -LiteralPath $env:SOURCE_FILE -Raw
        $launch = $source.Substring($source.IndexOf('# --- 7. Launch Electron now ---'))
        $NoLaunch = $false
        $RecoveryOwnedByCaller = $true
        $repoRoot = 'C:\isolated-fixture'
        $destExe = 'C:\isolated-fixture\electron-dist\Sales Buddy.exe'
        $venvPy = 'Invoke-FixturePython'
        $coordinator = 'isolated-upgrade.py'
        $script:launches = 0
        $script:messages = [System.Collections.Generic.List[string]]::new()
        function Start-Process { throw 'A desktop must not inherit this console' }
        function Invoke-FixturePython {
            if ($args[1] -ne 'launch-desktop' -or $args[2] -ne '--repo' -or
                $args[3] -ne $repoRoot) { throw 'Unexpected independent launch arguments' }
            $script:launches++
            $global:LASTEXITCODE = [int]$env:LAUNCH_EXIT
        }
        function Test-Path { return $false }
        function Start-Sleep { throw 'A second coordinator waited for the failed desktop' }
        function Write-Host { $script:messages.Add([string]$args[0]) }
        $errorText = $null
        try { Invoke-Expression $launch } catch { $errorText = $_.Exception.Message }
        @{ launches = $script:launches; error = $errorText;
           messages = @($script:messages) } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / 'migrate-to-electron.ps1'), LAUNCH_EXIT=str(launch_exit))
    assert result['error'] == (
        'The desktop could not be launched independently.' if launch_exit else None
    )
    assert result['launches'] == 1
    assert any('readiness and recovery' in message for message in result['messages'])
    assert not any('web and worker ready' in message for message in result['messages'])


@pytest.mark.parametrize('launch_exit', [0, 1])
def test_migration_owned_launch_preserves_fresh_identity_and_readiness_checks(launch_exit):
    """Launch independently without accepting a stale boot or hiding launch failures."""
    result = run_powershell(r"""
        $ErrorActionPreference = 'Stop'
        $source = Get-Content -LiteralPath $env:SOURCE_FILE -Raw
        $launch = $source.Substring($source.IndexOf('# --- 7. Launch Electron now ---'))
        $NoLaunch = $false
        $RecoveryOwnedByCaller = $false
        $repoRoot = 'C:\isolated-fixture'
        $venvPy = 'Invoke-FixturePython'
        $coordinator = 'isolated-upgrade.py'
        $script:events = [System.Collections.Generic.List[string]]::new()
        $script:stateReads = 0
        function Start-Process { throw 'A desktop must not inherit this console' }
        function Test-Path { return $true }
        function Get-Content {
            param($Path, [switch]$Raw, $ErrorAction)
            if ($Path.EndsWith('startup-state.json')) {
                $script:stateReads++
                if ($script:stateReads -eq 1) {
                    $script:events.Add('read-old')
                    return '{"boot_id":"old-boot"}'
                }
                $script:events.Add('read-new')
                return '{"boot_id":"new-boot"}'
            }
            return 'PORT=54321'
        }
        function Invoke-FixturePython {
            $script:events.Add([string]$args[1])
            if ($args[1] -eq 'launch-desktop') {
                $global:LASTEXITCODE = [int]$env:LAUNCH_EXIT
            } elseif ($args[1] -eq 'wait-ready' -and $args[5] -eq 'new-boot' -and
                      $args[7] -eq '54321') {
                $global:LASTEXITCODE = 0
            } else { throw 'Unexpected launcher or readiness arguments' }
        }
        function Start-Sleep { throw 'The fresh identity should be immediately available' }
        function Write-Host { }
        $errorText = $null
        try { Invoke-Expression $launch } catch { $errorText = $_.Exception.Message }
        @{ events = @($script:events); error = $errorText } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / 'migrate-to-electron.ps1'), LAUNCH_EXIT=str(launch_exit))
    assert result['error'] == (
        'The desktop could not be launched independently.' if launch_exit else None
    )
    assert result['events'] == (
        ['read-old', 'launch-desktop'] if launch_exit else
        ['read-old', 'launch-desktop', 'read-new', 'wait-ready']
    )


def test_backend_shutdown_does_not_kill_repair_or_diagnostic_python_processes():
    """Execute the real shutdown cleanup with the exact VM repair parent process shape."""
    result = run_powershell(r"""
        $ErrorActionPreference = 'Stop'
        $tokens=$null; $errors=$null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $env:SOURCE_FILE, [ref]$tokens, [ref]$errors)
        $definition = $ast.Find({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Stop-Server'
        }, $true)
        Invoke-Expression $definition.Extent.Text
        $RepoRoot = 'C:\isolated-fixture'
        $script:killed = [System.Collections.Generic.List[int]]::new()
        function Write-Host { }
        function Start-Sleep { }
        function Get-NetTCPConnection { return $null }
        function Get-CimInstance {
            param($ClassName, $Filter, $ErrorAction)
            if ($Filter -eq "Name='python.exe'") { return @() }
            return @(
                [pscustomobject]@{ ProcessId=101; CommandLine=
                    '"C:\isolated-fixture\venv\Scripts\python.exe" repair_vm_shell_readiness.py --repo C:\isolated-fixture' },
                [pscustomobject]@{ ProcessId=102; CommandLine=
                    'C:\frozen\runtime\Scripts\python.exe C:\frozen\recover.py send-reports --repo C:\isolated-fixture' },
                [pscustomobject]@{ ProcessId=103; CommandLine=
                    '"C:\isolated-fixture\venv\Scripts\python.exe" -m app.worker' },
                [pscustomobject]@{ ProcessId=104; CommandLine=
                    '"C:\isolated-fixture\venv\Scripts\python.exe" -m waitress app:create_app' }
            )
        }
        function Stop-ProcessTree {
            param($ProcessId, $ExcludePid)
            $script:killed.Add($ProcessId)
        }
        Stop-Server -Port 54321
        @{ killed = @($script:killed) } | ConvertTo-Json -Compress
    """, SOURCE_FILE=str(ROOT / 'scripts' / 'server.ps1'))
    assert result['killed'] == [103, 104]
