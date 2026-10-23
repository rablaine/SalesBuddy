"""Build a local transferable VM test kit without publishing code or copying user data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

NEW_RUNTIME_FILES = (
    'app/upgrade_rehearsal.py', 'electron/upgrade-safety.js',
    'installer/SalesBuddy.CustomActions/UpgradeSafety.cs', 'scripts/upgrade.py',
)
ACTIONS = {
    '1-SETUP-VM': 'setup',
    '2-TEST-FAILED-UPDATE': 'fail',
    '3-CHECK-ROLLBACK': 'check-rollback',
    '4-TEST-GOOD-UPDATE': 'good',
    '5-CHECK-GOOD-UPDATE': 'check-good',
}
POWERSHELL = r"""param(
    [ValidateSet('setup','fail','check-rollback','good','check-good')]
    [string]$Action
)
$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
$repo = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
$system = Get-CimInstance Win32_ComputerSystem
if ($system.Model -notmatch 'Virtual Machine|VMware|VirtualBox|KVM') {
    throw 'This kit is only for the disposable test VM, never your normal Sales Buddy laptop.'
}
if ($Action -eq 'setup') {
    Write-Host 'DISPOSABLE VM ONLY. This installs test code, creates fake records and redirects Git to a LOCAL test origin.'
    if ((Read-Host 'Type VM to confirm this is your disposable test machine') -cne 'VM') {
        throw 'Setup canceled. No application files changed.'
    }
}
$activate = Join-Path $repo 'venv\Scripts\Activate.ps1'
$python = Join-Path $repo 'venv\Scripts\python.exe'
if (-not (Test-Path $activate)) { throw 'Install Sales Buddy in this VM first.' }
& $activate
$helper = Join-Path $kit 'vm_upgrade_smoke.py'
if ($Action -eq 'setup') {
    # Validate the complete kit before stopping the existing application.
    & $python -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from vm_upgrade_smoke import validate_payload, validate_install; validate_payload(Path(sys.argv[1])); validate_install(Path(sys.argv[2]))' $kit $repo
    if ($LASTEXITCODE -ne 0) { throw 'VM kit validation failed. Nothing was installed.' }
    $sentinel = Join-Path $repo 'data\shutdown.request'
    New-Item (Split-Path $sentinel -Parent) -ItemType Directory -Force | Out-Null
    Set-Content -LiteralPath $sentinel -Value 'VM smoke-test setup'
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $shells = @(Get-CimInstance Win32_Process -Filter "Name='Sales Buddy.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase) })
        if (-not $shells.Count) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($shells.Count) { throw 'Quit Sales Buddy from the VM tray, then run setup again.' }
    & (Join-Path $repo 'scripts\server.ps1') -StopOnly
    if ($LASTEXITCODE -ne 0) { throw 'The VM backend could not be stopped safely.' }
}
& $python $helper $Action --repo $repo --kit $kit --confirm-vm
if ($LASTEXITCODE -ne 0) { throw 'The VM test stopped. Read the error above; do not continue blindly.' }
if ($Action -eq 'setup') {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $repo 'scripts\migrate-to-electron.ps1') -SkipPull -NoLaunch `
        -NoAutoStart -ElectronSource (Join-Path $kit 'payload\shell')
    if ($LASTEXITCODE -ne 0) { throw 'The VM desktop safety build could not be staged.' }
    Start-Process (Join-Path $repo 'electron-dist\Sales Buddy.exe') `
        -WorkingDirectory $repo -ArgumentList '--force-dark-mode'
    Write-Host 'Wait for the desktop to open. Look for customer VM TEST ONLY - Upgrade Safety and its two notes.'
    Write-Host 'Then run 2-TEST-FAILED-UPDATE.cmd from this same extracted folder.'
}
"""
INSTRUCTIONS = """SALES BUDDY - DISPOSABLE AZURE VM UPDATE TEST

Do NOT run this on your normal PC. No Azure snapshot or corporate sign-in is needed.
Internet access to public npm/Electron downloads is needed for the shell-rebuild tests.
The regular Sales Buddy release must already be installed in the VM.

1. Copy this ZIP into the VM and EXTRACT ALL files to a folder you will keep.
   Do not run launchers from inside the ZIP and do not move the folder after setup.
2. Double-click 1-SETUP-VM.cmd. Type VM when asked.
   This installs the safety build, bypasses onboarding only in this VM database,
   creates a clearly labeled fake customer and two notes, and opens the real desktop.
   Initial database/environment backups stay in the extracted folder's vm-session directory.
3. Once the desktop opens, double-click 2-TEST-FAILED-UPDATE.cmd.
   It requests a real update, including rebuilding and replacing the desktop executable.
   The test version intentionally fails at startup AFTER adding a database canary.
   Recovery should restore the previous app, runtime, desktop and database.
4. On the recovery notice:
   - Confirm the notice says diagnostics were sent or queued, not both.
   - Continue must work without saving a report; optional export is only a fallback.
   - Optional contact requests must not delay or replace initial failure reporting.
5. Double-click 3-CHECK-ROLLBACK.cmd. It must print PASS for both notes, their customer
   links, database integrity, removal of the failed-version canary and diagnostic queue receipt.
6. Double-click 4-TEST-GOOD-UPDATE.cmd. Wait for the actual desktop to restart successfully.
7. Double-click 5-CHECK-GOOD-UPDATE.cmd. It must print PASS.

No code is pushed or published. This VM's Git origin is deliberately redirected to a LOCAL
test repository inside the extracted kit folder. Do not change it back during these tests.
This tests updates FROM the new safety version. Installing that first version is a separate
bootstrap step, not a claim that older releases already have its safeguards.

These are unsigned test desktop files. This kit does NOT disable Windows security or antivirus.
If Windows blocks them, retain the error details rather than changing security settings.
This kit tests the real desktop update/rebuild/recovery flow, not the MSI upgrade UI.

If something fails, stop and share the exact error and report ID.
Without corporate sign-in, diagnostics stay queued; optional JSON export remains available.
Do not delete the VM's database, upgrade-recovery directory or vm-session folder.
"""

LEGACY_POWERSHELL = r"""param(
    [ValidateSet('start','check','restart')]
    [string]$Action
)
$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
$repo = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
$system = Get-CimInstance Win32_ComputerSystem
if ($system.Model -notmatch 'Virtual Machine|VMware|VirtualBox|KVM') {
    throw 'This add-on is only for the disposable test VM, never your normal PC.'
}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
& (Join-Path $repo 'venv\Scripts\Activate.ps1')
$python = Join-Path $repo 'venv\Scripts\python.exe'
$helper = Join-Path $kit 'vm_legacy_migration_smoke.py'
if ($Action -eq 'restart') {
    & $python $helper check --repo $repo --kit $kit --confirm-vm
    if ($LASTEXITCODE -ne 0) { throw 'Legacy verification failed; the app was not stopped.' }
    Set-Content -LiteralPath (Join-Path $repo 'data\shutdown.request') -Value 'VM legacy restart'
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $shells = @(Get-CimInstance Win32_Process -Filter "Name='Sales Buddy.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(
                $repo + '\', [StringComparison]::OrdinalIgnoreCase) })
        if (-not $shells.Count) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($shells.Count) { throw 'The VM desktop did not quit; no process was forcibly killed.' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $repo 'scripts\server.ps1') -StopOnly
    if ($LASTEXITCODE -ne 0) { throw 'The VM backend could not be stopped safely.' }
    Start-Process (Join-Path $repo 'electron-dist\Sales Buddy.exe') `
        -WorkingDirectory $repo -ArgumentList '--force-dark-mode'
    $deadline = (Get-Date).AddSeconds(180)
    do {
        Start-Sleep -Seconds 2
        $healthy = $false
        try {
            $settings = Get-Content -LiteralPath (Join-Path $repo '.env')
            $portLine = @($settings | Where-Object { $_ -match '^\s*PORT\s*=\s*["'']?(\d+)' })
            $port = 5151
            if ($portLine.Count -eq 1 -and $portLine[0] -match '=\s*["'']?(\d+)') {
                $port = [int]$Matches[1]
            }
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 5
            $proof = Get-Content -LiteralPath (Join-Path $repo 'data\vm-legacy-migration-test.json') -Raw |
                ConvertFrom-Json
            $healthy = $health.status -eq 'healthy' -and $health.worker_ready -eq $true -and
                $health.boot_id -ne $proof.restart_boot
        } catch [System.Net.WebException] {
            # A closed listener during the explicitly requested restart is expected.
        }
        if ($healthy) { break }
    } while ((Get-Date) -lt $deadline)
    if (-not $healthy) { throw 'The VM did not become ready after restart. Retain its diagnostics.' }
    & $python $helper check-restart --repo $repo --kit $kit --confirm-vm
} else {
    & $python $helper $Action --repo $repo --kit $kit --confirm-vm
}
if ($LASTEXITCODE -ne 0) {
    throw 'The legacy VM test stopped. Retain the exact error and report ID; do not continue blindly.'
}
"""

REPAIR_POWERSHELL = r"""$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
$repo = Join-Path $env:LOCALAPPDATA 'SalesBuddy'
$system = Get-CimInstance Win32_ComputerSystem
if ($system.Model -notmatch 'Virtual Machine|VMware|VirtualBox|KVM') {
    throw 'This repair is only for the disposable VM with the original test kit.'
}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
$activate = Join-Path $repo 'venv\Scripts\Activate.ps1'
$python = Join-Path $repo 'venv\Scripts\python.exe'
& $activate
$helper = Join-Path $kit 'repair_vm_readiness.py'
& $python $helper --repo $repo --kit $kit --check
if ($LASTEXITCODE -ne 0) { throw 'Repair validation failed. No application files changed.' }
Set-Content -LiteralPath (Join-Path $repo 'data\shutdown.request') -Value 'VM readiness repair'
$deadline = (Get-Date).AddSeconds(20)
do {
    $shells = @(Get-CimInstance Win32_Process -Filter "Name='Sales Buddy.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($repo + '\', [StringComparison]::OrdinalIgnoreCase) })
    if (-not $shells.Count) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
if ($shells.Count) { throw 'Quit Sales Buddy from the VM tray, then run FIX-VM.cmd again.' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $repo 'scripts\server.ps1') -StopOnly
if ($LASTEXITCODE -ne 0) { throw 'The VM backend could not be stopped safely.' }
$rootPattern = [regex]::Escape($repo)
$backends = @(Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $rootPattern -and
        $_.CommandLine -match '(app\.supervisor|app\.worker|waitress)' })
if ($backends.Count) { throw 'A VM backend is still running. No repair files were written.' }
& $python $helper --repo $repo --kit $kit
if ($LASTEXITCODE -ne 0) { throw 'The VM repair stopped. Preserve the error above.' }
Start-Process (Join-Path $repo 'electron-dist\Sales Buddy.exe') `
    -WorkingDirectory $repo -ArgumentList '--force-dark-mode'
Write-Host 'Sales Buddy is reopening with corrected process identity checks.'
Write-Host 'After the workspace opens, continue with 2-TEST-FAILED-UPDATE.cmd.'
"""
SHELL_REPAIR_POWERSHELL = REPAIR_POWERSHELL.replace(
    'repair_vm_readiness.py', 'repair_vm_shell_readiness.py',
).replace('FIX-VM.cmd', 'FIX-VM-SHELL.cmd').replace(
    'with corrected process identity checks', 'with corrected packaged-shell startup checks',
)


def digest(path: Path) -> str:
    """Hash one payload without loading large binaries into memory."""
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def source_files(repo: Path) -> list[str]:
    """Include tracked source and explicit new runtime files, never ignored/private data."""
    result = subprocess.run(
        ['git', 'ls-files', '-z'], cwd=repo, capture_output=True, check=True, timeout=30,
    )
    names = set(result.stdout.decode('utf-8').rstrip('\0').split('\0')) | set(NEW_RUNTIME_FILES)
    for name in names:
        path = (repo / name).resolve()
        if (not path.is_relative_to(repo.resolve()) or not path.is_file()
                or (repo / name).is_symlink() or path.name == '.env'
                or path.suffix.lower() in {
                    '.pfx', '.p12', '.key', '.pem', '.db', '.sqlite', '.sqlite3', '.log', '.har',
                }
                or {'.git', '.azure', 'venv', 'node_modules'} & set(Path(name).parts)):
            raise RuntimeError(f'Refusing missing, linked or sensitive source: {name}')
    return sorted(names)


def verify_shell(repo: Path, shell: Path) -> None:
    """Require a real packaged executable and exact current bundled source bytes."""
    for name in ('Sales Buddy.exe', 'icudtl.dat', 'resources/app.asar'):
        if not (shell / name).is_file():
            raise RuntimeError(f'The packaged desktop is incomplete: {name}')
    with (shell / 'Sales Buddy.exe').open('rb') as handle:
        if handle.read(2) != b'MZ':
            raise RuntimeError('The shell is not a Windows executable')
    code = """
const fs = require('fs'), path = require('path'), asar = require('@electron/asar');
const manifest = require('./package.json');
for (const file of manifest.build.files.filter(name => /\\.(js|css|html|ps1)$/.test(name))) {
  if (!asar.extractFile(process.argv[1], file).equals(fs.readFileSync(path.resolve(file)))) {
    throw new Error('Stale packaged source: ' + file);
  }
}
"""
    subprocess.run(
        ['node', '-e', code, str(shell / 'resources' / 'app.asar')],
        cwd=repo / 'electron', check=True, timeout=30,
    )


def build_kit(repo: Path, shell: Path, output: Path) -> None:
    """Create one self-contained ZIP; do not change the working tree, remotes or branches."""
    verify_shell(repo, shell)
    names = source_files(repo)
    helper = repo / 'scripts' / 'vm_upgrade_smoke.py'
    with tempfile.TemporaryDirectory(prefix='salesbuddy-vm-kit-') as temporary:
        source = Path(temporary) / 'source.zip'
        with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                archive.write(repo / name, name)
        files = {'payload/source.zip': digest(source), 'vm_upgrade_smoke.py': digest(helper)}
        shell_files = sorted(path for path in shell.rglob('*') if path.is_file())
        for path in shell_files:
            if path.is_symlink():
                raise RuntimeError('The desktop payload contains an unexpected symbolic link')
            files['payload/shell/' + path.relative_to(shell).as_posix()] = digest(path)
        manifest = {'schema_version': 1, 'kit_id': uuid.uuid4().hex, 'files': files}
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_suffix('.partial')
        try:
            with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.write(source, 'payload/source.zip')
                archive.write(helper, 'vm_upgrade_smoke.py')
                for path in shell_files:
                    archive.write(path, 'payload/shell/' + path.relative_to(shell).as_posix())
                archive.writestr('kit-manifest.json', json.dumps(manifest, indent=2))
                archive.writestr('VM-Test.ps1', POWERSHELL)
                archive.writestr('START-HERE.txt', INSTRUCTIONS)
                for name, action in ACTIONS.items():
                    archive.writestr(name + '.cmd',
                                     '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
                                     f'-File "%~dp0VM-Test.ps1" -Action {action}\r\npause\r\n')
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
    print(f'VM kit: {output}')
    print(f'Size: {output.stat().st_size / (1024 * 1024):.1f} MiB')
    print(f'SHA256: {digest(output)}')


def build_readiness_repair(repo: Path, original_kit: Path, output: Path) -> None:
    """Make a small, kit-fenced repair ZIP without repackaging or transferring Electron again."""
    patch = repo / 'app' / 'supervisor.py'
    with zipfile.ZipFile(original_kit) as archive:
        original_manifest = json.loads(archive.read('kit-manifest.json'))
        with zipfile.ZipFile(io.BytesIO(archive.read('payload/source.zip'))) as source:
            old_hash = hashlib.sha256(source.read('app/supervisor.py')).hexdigest()
    manifest = {
        'schema_version': 1, 'kit_id': original_manifest['kit_id'],
        'old_supervisor_sha256': old_hash, 'new_supervisor_sha256': digest(patch),
    }
    if manifest['old_supervisor_sha256'] == manifest['new_supervisor_sha256']:
        raise RuntimeError('This kit already contains the readiness fix')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(patch, 'readiness-repair/supervisor.py')
        archive.write(repo / 'scripts' / 'repair_vm_readiness.py', 'repair_vm_readiness.py')
        archive.writestr('readiness-repair.json', json.dumps(manifest, indent=2))
        archive.writestr('FIX-VM.ps1', REPAIR_POWERSHELL)
        archive.writestr(
            'FIX-VM.cmd', '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
            '-File "%~dp0FIX-VM.ps1"\r\npause\r\n')
        archive.writestr(
            'REPAIR-READ-ME.txt',
            'Extract this ZIP INTO the folder where you originally extracted the VM test kit.\n'
            'FIX-VM.cmd must sit next to 2-TEST-FAILED-UPDATE.cmd and kit-manifest.json.\n'
            'Double-click FIX-VM.cmd. It closes the VM app, repairs startup and reopens it.\n'
            'Both fake notes are preserved. No sign-in, reinstall or new full kit is needed.\n'
            'After the workspace opens, continue with 2-TEST-FAILED-UPDATE.cmd.\n',
        )
    print(f'VM readiness repair: {output} ({output.stat().st_size:,} bytes)')
    print(f'SHA256: {digest(output)}')


def build_shell_readiness_repair(repo: Path, original_kit: Path, output: Path) -> None:
    """Package a small source-fenced repair for the already-restored recovery-reporting VM."""
    names = (
        'electron/main.js', 'electron/upgrade-safety.js',
        'scripts/migrate-to-electron.ps1', 'scripts/upgrade.py', 'scripts/server.ps1',
    )
    with zipfile.ZipFile(original_kit) as original:
        kit_id = json.loads(original.read('kit-manifest.json'))['kit_id']
        with zipfile.ZipFile(io.BytesIO(original.read('payload/source.zip'))) as source:
            files = {
                name: {
                    'old_sha256': hashlib.sha256(
                        source.read(name).replace(b'\r\n', b'\n')).hexdigest(),
                    'new_sha256': digest(repo / name),
                } for name in names
            }
    manifest = {
        'schema_version': 1, 'kit_id': kit_id, 'files': files,
        'helper_sha256': digest(repo / 'scripts' / 'vm_upgrade_smoke.py'),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(repo / name, 'shell-readiness-repair/' + name)
        archive.write(repo / 'scripts' / 'vm_upgrade_smoke.py',
                      'shell-readiness-repair/vm_upgrade_smoke.py')
        archive.write(repo / 'scripts' / 'repair_vm_shell_readiness.py',
                      'repair_vm_shell_readiness.py')
        archive.writestr('shell-readiness-repair.json', json.dumps(manifest, indent=2))
        archive.writestr('FIX-VM-SHELL.ps1', SHELL_REPAIR_POWERSHELL)
        archive.writestr(
            'FIX-VM-SHELL.cmd', '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
            '-File "%~dp0FIX-VM-SHELL.ps1"\r\npause\r\n',
        )
        archive.writestr(
            'SHELL-FIX-START-HERE.txt',
            'For the existing recovery-reporting VM kit only. No reinstall needed.\n'
            'First finish the restored-version notice with Continue working.\n'
            'If the previous patch stopped after staging, this resumes its saved repair.\n'
            'Extract ALL patch files into the SAME folder as 2-TEST-FAILED-UPDATE.cmd.\n'
            'Run FIX-VM-SHELL.cmd. It validates the original kit and source before changing files,\n'
            'closes the VM app, rebuilds the fixed desktop and regenerates local test updates.\n'
            'Both notes and the previous failed attempt/report are retained.\n'
            'After Sales Buddy opens, run step 2 again, Continue working, then step 3.\n'
            'Do not run step 1 again. Ignore normal update notifications during the test.\n',
        )
    print(f'VM shell repair: {output} ({output.stat().st_size:,} bytes)')
    print(f'SHA256: {digest(output)}')


def build_legacy_migration_addon(repo: Path, original_kit: Path, output: Path) -> None:
    """Package a small legacy failure test for the VM that completed the good update."""
    with zipfile.ZipFile(original_kit) as original:
        kit_id = json.loads(original.read('kit-manifest.json'))['kit_id']
    helpers = ('vm_legacy_migration_smoke.py', 'vm_upgrade_smoke.py')
    manifest = {
        'schema_version': 1, 'kit_id': kit_id,
        'files': {name: digest(repo / 'scripts' / name) for name in helpers},
    }
    actions = {
        '6-TEST-LEGACY-MIGRATION': 'start', '7-CHECK-LEGACY-ROLLBACK': 'check',
        '8-RESTART-AND-CHECK-LEGACY': 'restart',
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in helpers:
            archive.write(repo / 'scripts' / name, name)
        archive.writestr('legacy-migration-addon.json', json.dumps(manifest, indent=2))
        archive.writestr('Legacy-VM-Test.ps1', LEGACY_POWERSHELL)
        for name, action in actions.items():
            archive.writestr(
                name + '.cmd', '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
                f'-File "%~dp0Legacy-VM-Test.ps1" -Action {action}\r\npause\r\n',
            )
        archive.writestr(
            'LEGACY-TEST-START-HERE.txt',
            'DISPOSABLE VM ONLY, starting after step 5 passed. No reinstall or sign-in needed.\n'
            'Extract ALL add-on files into the ORIGINAL recovery-reporting VM kit folder,\n'
            'alongside kit-manifest.json and 5-CHECK-GOOD-UPDATE.cmd. Replace the helper if asked.\n'
            'Keep Sales Buddy open and wait until its workspace is ready.\n\n'
            '6. Run 6-TEST-LEGACY-MIGRATION.cmd once. It backs up the VM database, adds a synthetic\n'
            '   milestone/task linked to your first test note, and recreates the historical task\n'
            '   foreign key to missing _old_call_logs, with milestone_id still NOT NULL.\n'
            '   It requests a real desktop update whose ONLY code change removes the historical\n'
            '   one-line repair. This is a real SQLite migration failure, not a raised test error.\n'
            '   Rehearsal should fail BEFORE live migration or the candidate can start.\n'
            '   Wait for restoration, choose Continue working, and wait for the workspace.\n'
            '7. Run 7-CHECK-LEGACY-ROLLBACK.cmd. All checks must print PASS. This checks the new\n'
            '   attempt, actual migration stack, original legacy snapshot, untouched legacy live\n'
            '   DB at failure, restored version, queued diagnostics, two notes, repaired task\n'
            '   links, and live web/worker readiness.\n'
            '8. Run 8-RESTART-AND-CHECK-LEGACY.cmd. It closes/reopens the desktop and verifies a\n'
            '   NEW healthy boot with the SAME recovered attempt, proving no migration loop.\n\n'
            'This recreates the known legacy schema/code path, not your tester\'s private DB.\n'
            'The fixed saved version should repair the legacy task table when it starts again.\n'
            'Only the disposable fixture DB is eligible; extra notes/customers/tasks stop setup.\n'
            'No downloads, corporate login, push, publish, MSI install or shell rebuild needed.\n'
            'Ignore normal update notifications. Do NOT run steps 1-5 or step 6 again.\n'
            'If anything fails, STOP and share the exact output and report ID.\n'
            'Keep the original kit folder, vm-session folder, backups and upgrade-recovery data.\n',
        )
    print(f'Legacy migration add-on: {output} ({output.stat().st_size:,} bytes)')
    print(f'SHA256: {digest(output)}')


def main() -> None:
    """Package a separately built shell, without installing, signing or deploying anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build_kit(Path(__file__).resolve().parents[1], args.shell.resolve(), args.output.resolve())


if __name__ == '__main__':
    main()
