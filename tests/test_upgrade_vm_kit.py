"""Isolated tests for the transferable VM kit; never run its setup on a real installation."""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from scripts import build_upgrade_vm_kit as builder
from scripts import vm_upgrade_smoke as smoke


@pytest.fixture
def source_repo(tmp_path):
    """Create a tiny local Git source repository containing no actual installation data."""
    repo = tmp_path / 'source'
    repo.mkdir()
    smoke.run(repo, 'init', '-b', 'main')
    names = [
        '.gitignore', 'README.md', 'electron/status.html', 'app/supervisor.py',
        smoke.VERSION_FILE, 'scripts/vm_upgrade_smoke.py', 'requirements.txt',
    ]
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('baseline\n', encoding='utf-8')
    (repo / '.gitignore').write_text('.env\ndata/\n.azure/\nvenv/\n', encoding='utf-8')
    smoke.commit_files(repo, names, 'test: isolated VM kit source', tmp_path / 'paths.txt')
    for name in builder.NEW_RUNTIME_FILES:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# new safety runtime\n', encoding='utf-8')
    return repo


def test_source_snapshot_includes_untracked_runtime_not_secrets(source_repo):
    """Capture current safety source without blindly including untracked or ignored files."""
    (source_repo / '.env').write_text('SECRET_KEY=do-not-export', encoding='utf-8')
    for name in ('data/salesbuddy.db', '.azure/accessTokens.json', 'venv/credential.txt'):
        path = source_repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'private VM-independent developer data')
    names = builder.source_files(source_repo)
    assert set(builder.NEW_RUNTIME_FILES) <= set(names)
    assert '.env' not in names
    assert not any(name.startswith(('data/', '.azure/', 'venv/')) for name in names)


@pytest.mark.parametrize('name', ['private.db', 'capture.har', 'secret.pem', 'output.log'])
def test_source_snapshot_rejects_tracked_private_payload(source_repo, name):
    """Even accidentally tracked database, traffic, log or key files cannot enter the kit."""
    (source_repo / name).write_bytes(b'private')
    smoke.run(source_repo, 'add', name)
    with pytest.raises(RuntimeError, match='sensitive source'):
        builder.source_files(source_repo)


def test_zip_contains_launchers_complete_sources_and_verified_shell(source_repo, tmp_path, monkeypatch):
    """Build and verify the actual ZIP structure, checksums and runnable action names."""
    shell = tmp_path / 'shell'
    (shell / 'resources').mkdir(parents=True)
    (shell / 'Sales Buddy.exe').write_bytes(b'MZ isolated test executable')
    (shell / 'icudtl.dat').write_bytes(b'isolated ICU')
    (shell / 'resources' / 'app.asar').write_bytes(b'isolated archive')
    monkeypatch.setattr(builder, 'verify_shell', lambda repo, folder: None)
    output = tmp_path / 'kit.zip'
    builder.build_kit(source_repo, shell, output)
    kit = tmp_path / 'extracted'
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert all(name + '.cmd' in archive.namelist() for name in builder.ACTIONS)
        assert 'VM-Test.ps1' in archive.namelist()
        with zipfile.ZipFile(io.BytesIO(archive.read('payload/source.zip'))) as source:
            assert set(builder.NEW_RUNTIME_FILES) <= set(source.namelist())
        archive.extractall(kit)
    manifest = smoke.validate_payload(kit)
    assert manifest['schema_version'] == 1
    (kit / 'payload' / 'shell' / 'resources' / 'app.asar').write_bytes(b'corrupted')
    with pytest.raises(RuntimeError, match='verification failed'):
        smoke.validate_payload(kit)


@pytest.mark.parametrize('name', ['../escape.py', '.git/config', '.env', 'nested/.env', 'C:/escape.py'])
def test_extract_source_refuses_unsafe_paths(tmp_path, name):
    """Source extraction cannot overwrite outside the install, Git config or environment."""
    source = tmp_path / 'source.zip'
    repo = tmp_path / 'install'
    repo.mkdir()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(name, 'unsafe')
    with pytest.raises(RuntimeError, match='Unsafe source'):
        smoke.extract_source(source, repo)
    assert not list(repo.iterdir())


def test_install_guard_rejects_developer_checkout(tmp_path, monkeypatch):
    """A caller cannot aim this tool at the developer checkout or an arbitrary directory."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'profile'))
    with pytest.raises(RuntimeError, match='only operates'):
        smoke.validate_install(tmp_path / 'developer-checkout')


@pytest.mark.parametrize('table', ['customers', 'notes', 'call_logs', 'engagements', 'milestones'])
def test_freshness_guard_rejects_existing_work_before_source_changes(tmp_path, monkeypatch, table):
    """Read the real installed resolver, but constrain its result to an isolated test database."""
    repo = tmp_path / 'installed'
    (repo / 'app').mkdir(parents=True)
    shutil.copy2(Path(__file__).parents[1] / 'app' / 'db_paths.py', repo / 'app' / 'db_paths.py')
    database = tmp_path / 'isolated.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        connection.execute(f'INSERT INTO "{table}" VALUES (1)')
    with pytest.raises(RuntimeError, match='fresh VM'):
        smoke.require_fresh_database(repo)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(f'SELECT id FROM "{table}"').fetchall() == [(1,)]


def test_seed_real_schema_and_verify_exact_records_offline(tmp_path, monkeypatch):
    """Exercise the actual offline schema path and synthetic data using only a temporary DB."""
    from app.db_paths import backup_database
    from app.upgrade_rehearsal import initialize_offline

    database = tmp_path / 'isolated.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    initialize_offline(database)
    fixture = smoke.seed_database(tmp_path, tmp_path / 'initial.db')
    assert (tmp_path / 'initial.db').is_file()
    smoke.verify_records(fixture)
    assert backup_database(tmp_path / 'seeded.db', src=database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE vm_update_canary (id INTEGER PRIMARY KEY)')
    with pytest.raises(RuntimeError, match='not rolled back'):
        smoke.verify_records(fixture)


def test_targets_leave_baseline_clean_and_change_desktop_payload(source_repo, tmp_path):
    """Real local Git targets exercise a shell-source difference and do not alter baseline HEAD."""
    baseline = smoke.run(source_repo, 'rev-parse', 'HEAD')
    work = tmp_path / 'session'
    work.mkdir()
    broken = smoke.create_target(source_repo, work, baseline, broken=True)
    good = smoke.create_target(source_repo, work, baseline, broken=False)
    assert smoke.run(source_repo, 'rev-parse', 'HEAD') == baseline
    assert not smoke.run(source_repo, 'diff', '--name-only')
    assert smoke.run(source_repo, 'show', f'{broken}:app/supervisor.py') == (
        smoke.BROKEN_SUPERVISOR.strip())
    assert 'VM smoke-test desktop: broken' in smoke.run(
        source_repo, 'show', f'{broken}:electron/status.html')
    assert 'VM smoke-test desktop: good' in smoke.run(
        source_repo, 'show', f'{good}:electron/status.html')
    assert smoke.TRAILER in smoke.run(source_repo, 'show', '-s', '--format=%B', broken)
    smoke.run(source_repo, 'merge-base', '--is-ancestor', baseline, broken)
    smoke.run(source_repo, 'merge-base', '--is-ancestor', baseline, good)


def test_intentional_fault_commits_canary_and_records_actual_shell(tmp_path):
    """Execute the actual fault payload inside an isolated stub application, never the VM app."""
    repo = tmp_path / 'fault'
    work = tmp_path / 'session'
    for directory in ('app', 'scripts', 'data', 'electron-dist/resources'):
        (repo / directory).mkdir(parents=True)
    work.mkdir()
    for name in ('app/__init__.py', 'scripts/__init__.py'):
        (repo / name).write_text('', encoding='utf-8')
    (repo / 'app' / 'supervisor.py').write_text(smoke.BROKEN_SUPERVISOR, encoding='utf-8')
    (repo / 'data' / smoke.MARKER).write_text(json.dumps({'work': str(work)}), encoding='utf-8')
    (repo / 'electron-dist' / 'resources' / 'app.asar').write_bytes(b'new shell')
    (repo / 'scripts' / 'upgrade.py').write_text(
        "import json\n"
        "def atomic_json(path, data): path.write_text(json.dumps(data), encoding='utf-8')\n"
        "def database_path(repo): return repo / 'isolated.db'\n"
        "def diagnostic_error(error): return {'type': type(error).__name__}\n"
        "def startup_path(repo): return repo / 'data' / 'startup-state.json'\n"
        "def load_state(repo): return {'attempt_id': 'test-attempt', 'target_commit': 'test-target'}\n"
        "def save_state(repo, state, phase, **fields):\n"
        "    atomic_json(repo / 'data' / 'upgrade-state.json', dict(state, phase=phase, **fields))\n",
        encoding='utf-8',
    )
    result = subprocess.run(
        [sys.executable, '-m', 'app.supervisor'], cwd=repo,
        env={**os.environ, 'SALESBUDDY_BOOT_ID': 'vm-fault-test'},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert 'Intentional VM-only startup failure after database mutation' in result.stderr
    with closing(sqlite3.connect(repo / 'isolated.db')) as connection:
        assert connection.execute('SELECT id FROM vm_update_canary').fetchall() == [(1,)]
    reached = json.loads((work / 'fault-reached.json').read_text(encoding='utf-8'))
    assert reached == {
        'attempt_id': 'test-attempt', 'target_commit': 'test-target',
        'canary_committed': True,
        'shell_sha256': smoke.digest(repo / 'electron-dist' / 'resources' / 'app.asar'),
    }
    startup = json.loads((repo / 'data' / 'startup-state.json').read_text(encoding='utf-8'))
    assert startup['boot_id'] == 'vm-fault-test'
    assert startup['state'] == 'failed'


def test_setup_local_origin_update_request_and_non_proxy_result_checks(source_repo, tmp_path, monkeypatch):
    """Exercise setup and launch requests with real Git/SQLite and only a mocked desktop/HTTP peer."""
    from app.db_paths import backup_database
    from scripts import upgrade
    from scripts import repair_vm_readiness

    shutil.copy2(Path(__file__).parents[1] / 'app' / 'db_paths.py',
                 source_repo / 'app' / 'db_paths.py')
    smoke.commit_files(source_repo, ['app/db_paths.py'], 'test: isolated resolver',
                       tmp_path / 'resolver-paths.txt')
    profile = tmp_path / 'profile'
    profile.mkdir()
    monkeypatch.setenv('LOCALAPPDATA', str(profile))
    installed = profile / 'SalesBuddy'
    smoke.run(source_repo, 'clone', str(source_repo), str(installed))
    interpreter = installed / 'venv' / 'Scripts' / 'python.exe'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b'isolated interpreter guard fixture')
    kit = tmp_path / 'kit'
    (kit / 'payload' / 'shell' / 'resources').mkdir(parents=True)
    (kit / 'payload' / 'shell' / 'resources' / 'app.asar').write_bytes(b'baseline shell')
    with zipfile.ZipFile(kit / 'payload' / 'source.zip', 'w') as archive:
        for name in builder.source_files(source_repo):
            archive.write(source_repo / name, name)
    (kit / 'kit-manifest.json').write_text(json.dumps({
        'schema_version': 1, 'kit_id': 'isolated',
        'files': {
            'payload/source.zip': smoke.digest(kit / 'payload' / 'source.zip'),
            'payload/shell/resources/app.asar': smoke.digest(
                kit / 'payload' / 'shell' / 'resources' / 'app.asar'),
        },
    }), encoding='utf-8')
    database = tmp_path / 'isolated.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            'CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);'
            'CREATE TABLE notes (id INTEGER PRIMARY KEY, customer_id INTEGER, content TEXT);')

    def seed(repo, backup):
        """Substitute minimal fixture schema while the separate seed test covers real ORM setup."""
        assert repo == installed
        assert backup_database(backup, src=database)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO customers VALUES (1, 'VM TEST ONLY')")
            connection.executemany('INSERT INTO notes VALUES (?, 1, ?)', enumerate(smoke.NOTE_TEXT, 1))
        return {'database': str(database), 'customer_id': 1, 'note_ids': [1, 2]}

    monkeypatch.setattr(smoke, 'seed_database', seed)
    smoke.setup(installed, kit)
    state = smoke.session(installed, kit)
    old_baseline = state['baseline']
    original = tmp_path / 'original.zip'
    with zipfile.ZipFile(original, 'w') as archive:
        archive.write(kit / 'kit-manifest.json', 'kit-manifest.json')
        archive.write(kit / 'payload' / 'source.zip', 'payload/source.zip')
    fixed_repo = tmp_path / 'fixed-source'
    (fixed_repo / 'app').mkdir(parents=True)
    (fixed_repo / 'scripts').mkdir()
    (fixed_repo / 'app' / 'supervisor.py').write_text('# Fixed PID startup\n', encoding='utf-8')
    shutil.copy2(Path(__file__).parents[1] / 'scripts' / 'repair_vm_readiness.py',
                 fixed_repo / 'scripts' / 'repair_vm_readiness.py')
    repair_zip = tmp_path / 'repair.zip'
    builder.build_readiness_repair(fixed_repo, original, repair_zip)
    with zipfile.ZipFile(repair_zip) as archive:
        assert archive.testzip() is None
        archive.extractall(kit)
    repair_vm_readiness.repair(installed, kit)
    state = smoke.session(installed, kit)
    assert state['baseline'] != old_baseline
    smoke.run(installed, 'merge-base', '--is-ancestor', old_baseline, state['baseline'])
    assert smoke.run(installed, '--git-dir', state['remote'], 'rev-parse', 'main') == state['baseline']
    assert '# Fixed PID startup' in smoke.run(installed, 'show', f'{state["good"]}:app/supervisor.py')
    assert (Path(state['readiness_repair']) / 'previous-supervisor.py').is_file()
    with pytest.raises(RuntimeError, match='already changed'):
        repair_vm_readiness.validate_repair(installed, kit)
    assert smoke.run(installed, 'rev-parse', 'HEAD') == state['baseline']
    assert Path(smoke.run(installed, 'remote', 'get-url', 'origin')) == Path(state['remote'])
    assert (Path(state['work']) / 'initial-database.db').is_file()
    health = {'status': 'healthy', 'worker_ready': True, 'commit_full': state['baseline']}
    monkeypatch.setattr(
        smoke.urllib.request, 'urlopen',
        lambda *args, **kwargs: io.BytesIO(json.dumps(health).encode('utf-8')),
    )
    smoke.request_update(installed, state, good=False)
    assert smoke.run(installed, '--git-dir', state['remote'], 'rev-parse', 'main') == state['broken']
    assert (installed / 'data' / 'electron-update.request').read_text(encoding='utf-8') == 'rebuild'

    shell = installed / 'electron-dist' / 'resources' / 'app.asar'
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b'baseline shell')
    result = {
        'phase': 'restored', 'attempt_id': 'completed-attempt',
        'notification_pending': False, 'needs_shell_rebuild': True, 'shell_existed': True,
        'report_queued_at': 'queued', 'acknowledged_at': 'acknowledged',
        'original_failure': {'failed_phase': 'startup'}, 'target_commit': state['broken'],
    }
    monkeypatch.setattr(upgrade, 'load_state', lambda repo: result)
    proof = {
        'attempt_id': 'completed-attempt', 'target_commit': state['broken'],
        'canary_committed': True, 'shell_sha256': 'different rebuilt shell',
    }
    reached = Path(state['work']) / 'fault-reached.json'
    with pytest.raises(RuntimeError, match='intended startup fault was not reached'):
        smoke.check(installed, state, good=False)
    reached.write_text(json.dumps(proof), encoding='utf-8')
    smoke.check(installed, state, good=False)
    proof['attempt_id'] = 'stale-attempt'
    reached.write_text(json.dumps(proof), encoding='utf-8')
    with pytest.raises(RuntimeError, match='fault was not reached'):
        smoke.check(installed, state, good=False)
    proof['attempt_id'] = 'completed-attempt'
    reached.write_text(json.dumps(proof), encoding='utf-8')
    result['notification_pending'] = True
    with pytest.raises(RuntimeError, match='Finish recovery'):
        smoke.request_update(installed, state, good=True)
    result['notification_pending'] = False
    smoke.request_update(installed, state, good=True)
    assert smoke.run(installed, '--git-dir', state['remote'], 'rev-parse', 'main') == state['good']
    smoke.run(installed, 'merge', '--ff-only', state['good'])
    result.update(phase='ready', target_commit=state['good'])
    with pytest.raises(RuntimeError, match='did not replace'):
        smoke.check(installed, state, good=True)
    shell.write_bytes(b'updated good shell')
    smoke.check(installed, state, good=True)


@pytest.mark.parametrize('payload', [
    builder.POWERSHELL, builder.REPAIR_POWERSHELL, builder.SHELL_REPAIR_POWERSHELL,
])
def test_kit_powershell_parses_and_keeps_vm_guard_and_actual_handoff(tmp_path, payload):
    """Parse the exact delivered PowerShell, not a separately maintained approximation."""
    script = tmp_path / 'VM-Test.ps1'
    script.write_text(payload, encoding='utf-8')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-Command',
         '$tokens=$null; $errors=$null; '
         '[System.Management.Automation.Language.Parser]::ParseFile('
         f"'{str(script).replace(chr(39), chr(39) * 2)}', [ref]$tokens, [ref]$errors) > $null; "
         'if ($errors.Count) { $errors | Format-List; exit 1 }'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Get-CimInstance Win32_ComputerSystem' in payload
    assert '-ElectronSource' in builder.POWERSHELL
    assert '-SkipPull -NoLaunch' in builder.POWERSHELL
    assert 'Stop-Process' not in builder.POWERSHELL
    assert 'Set-ExecutionPolicy' not in builder.POWERSHELL


@pytest.fixture
def shell_repair_case(source_repo, tmp_path, monkeypatch):
    """Model a restored VM test with real Git and local files but no installed applications."""
    from scripts import repair_vm_shell_readiness as repair
    from scripts import upgrade

    repo = source_repo
    kit = tmp_path / 'kit'
    work = kit / 'vm-session'
    work.mkdir(parents=True)
    old_shell = repo / 'electron-dist' / 'resources' / 'app.asar'
    old_shell.parent.mkdir(parents=True)
    old_shell.write_bytes(b'original desktop')
    names = [
        'electron/main.js', 'electron/upgrade-safety.js',
        'scripts/migrate-to-electron.ps1', 'scripts/upgrade.py', 'scripts/server.ps1',
    ]
    hashes = {}
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'old source\n')
        payload = kit / 'shell-readiness-repair' / name
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b'fixed source\n')
        hashes[name] = {'old_sha256': smoke.digest(path), 'new_sha256': smoke.digest(payload)}
    baseline = smoke.commit_files(repo, names, 'test: baseline sources', tmp_path / 'files.txt')
    remote = work / 'updates.git'
    smoke.run(repo, 'clone', '--bare', str(repo), str(remote))
    helper = kit / 'vm_upgrade_smoke.py'
    helper.write_bytes(b'old checker\n')
    replacement = kit / 'shell-readiness-repair' / 'vm_upgrade_smoke.py'
    replacement.write_bytes(b'fixed checker\n')
    smoke.write_json(kit / 'kit-manifest.json', {
        'schema_version': 1, 'kit_id': 'test-kit',
        'files': {'vm_upgrade_smoke.py': smoke.digest(helper)},
    })
    smoke.write_json(kit / 'shell-readiness-repair.json', {
        'schema_version': 1, 'kit_id': 'test-kit', 'files': hashes,
        'helper_sha256': smoke.digest(replacement),
    })
    state = {
        'kit_id': 'test-kit', 'repo': str(repo), 'baseline': baseline,
        'broken': 'b' * 40, 'good': 'c' * 40, 'work': str(work), 'remote': str(remote),
        'baseline_shell_sha256': smoke.digest(old_shell),
    }
    smoke.run(repo, 'remote', 'add', 'origin', str(remote))
    result = {
        'phase': 'restored', 'target_commit': state['broken'], 'restored_at': 'restored',
        'acknowledged_at': 'dismissed', 'notification_pending': False,
    }
    marker = repo / 'data' / smoke.MARKER
    marker.parent.mkdir(parents=True)
    smoke.write_json(marker, state)
    (work / 'old-attempt-evidence.txt').write_text('retain me')
    monkeypatch.setattr(smoke, 'validate_install', lambda _: None)
    monkeypatch.setattr(smoke, 'verify_records', lambda _: None)
    monkeypatch.setattr(upgrade, 'load_state', lambda _: result)
    return repair, repo, kit, state, result


@pytest.mark.parametrize('invalid', ['pending', 'wrong-kit', 'changed-source', 'reached-fault'])
def test_shell_repair_refuses_unsafe_or_unrelated_attempts(shell_repair_case, invalid):
    """The transfer patch cannot overwrite a pending recovery, changed source or another kit."""
    repair, repo, kit, state, result = shell_repair_case
    repair.validate_repair(repo, kit)
    if invalid == 'pending':
        result['notification_pending'] = True
    elif invalid == 'wrong-kit':
        manifest = json.loads((kit / 'shell-readiness-repair.json').read_text())
        manifest['kit_id'] = 'different-kit'
        smoke.write_json(kit / 'shell-readiness-repair.json', manifest)
    elif invalid == 'changed-source':
        (repo / 'electron' / 'main.js').write_text('changed\n')
    else:
        (Path(state['work']) / 'fault-reached.json').write_text('{}')
    with pytest.raises(RuntimeError):
        repair.validate_repair(repo, kit)


@pytest.mark.parametrize('phase', ['restored', 'failed'])
def test_shell_repair_regenerates_real_local_targets_and_retains_evidence(
    shell_repair_case, monkeypatch, phase,
):
    """Exercise the actual repair and real Git history; mock only Windows build/staging."""
    repair, repo, kit, previous, result = shell_repair_case
    from scripts import upgrade

    result['phase'] = phase
    monkeypatch.setattr(upgrade, 'save_state',
                        lambda _repo, state, value, **fields: state.update(phase=value, **fields))
    original_run = subprocess.run
    commands = []

    def run(argv, **kwargs):
        """Model platform build tools without downloading or starting a desktop."""
        if argv[0] != 'powershell.exe':
            return original_run(argv, **kwargs)
        commands.append(argv)
        if '-NoLaunch' in argv:
            (repo / 'electron-dist' / 'resources' / 'app.asar').write_bytes(b'fixed desktop')
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    repair.repair(repo, kit)
    state = json.loads((repo / 'data' / smoke.MARKER).read_text())
    assert state['baseline'] != previous['baseline']
    assert smoke.run(repo, 'rev-parse', 'HEAD') == state['baseline']
    assert smoke.run(repo, '--git-dir', state['remote'], 'rev-parse', 'main') == state['baseline']
    assert (Path(previous['work']) / 'old-attempt-evidence.txt').read_text() == 'retain me'
    assert (kit / 'vm_upgrade_smoke.py').read_bytes() == b'fixed checker\n'
    smoke.validate_payload(kit)
    assert len(commands) == 2
    assert '-SkipInstall' in commands[0] and '-NoLaunch' in commands[1]
    assert state['baseline_shell_sha256'] != previous['baseline_shell_sha256']
    assert result['phase'] == 'restored'
    evidence = Path(state['shell_readiness_repair']) / 'previous-upgrade-state.json'
    assert json.loads(evidence.read_text())['phase'] == phase


def test_interrupted_shell_staging_resumes_without_reinstall_or_overwriting_backup(
    shell_repair_case, monkeypatch,
):
    """Resume the precise VM failure: source committed and desktop staged, parent terminated."""
    repair, repo, kit, previous, _result = shell_repair_case
    manifest_path = kit / 'shell-readiness-repair.json'
    updated_manifest = json.loads(manifest_path.read_text())
    first_patch = json.loads(manifest_path.read_text())
    first_patch['files'].pop('scripts/server.ps1')
    smoke.write_json(manifest_path, first_patch)
    original_run = subprocess.run
    staging = []

    def run(argv, **kwargs):
        """Model the first parent's interrupted exit, while its staging child finishes."""
        if argv[0] != 'powershell.exe':
            return original_run(argv, **kwargs)
        if '-NoLaunch' in argv:
            (repo / 'electron-dist' / 'resources' / 'app.asar').write_bytes(b'fixed desktop')
            staging.append(True)
            return subprocess.CompletedProcess(argv, 1 if len(staging) == 1 else 0)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='could not be staged'):
        repair.repair(repo, kit)
    assert (repo / 'scripts' / 'server.ps1').read_bytes() == b'old source\n'
    smoke.write_json(manifest_path, updated_manifest)
    _state, manifest = repair.validate_repair(repo, kit)
    backup = Path(manifest['resume_work']) / 'previous-upgrade-state.json'
    original_backup = backup.read_bytes()
    old_marker = (repo / 'data' / smoke.MARKER).read_bytes()
    assert json.loads(old_marker)['baseline'] == previous['baseline']
    repair.repair(repo, kit)
    repaired = json.loads((repo / 'data' / smoke.MARKER).read_text())
    assert repaired['baseline'] != previous['baseline']
    assert backup.read_bytes() == original_backup
    assert repaired['shell_readiness_repair'] == manifest['resume_work']
    assert len(staging) == 2
    assert (repo / 'scripts' / 'server.ps1').read_bytes() == b'fixed source\n'
    smoke.validate_payload(kit)


def test_migration_success_does_not_inherit_robocopy_success_exit_code(tmp_path):
    """Robocopy's normal exit 1 must not make the VM setup report a failed staging operation."""
    repo = tmp_path / 'install'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'scripts' / 'migrate-to-electron.ps1').write_text(
        "param([switch]$SkipPull, [switch]$NoLaunch, [switch]$NoAutoStart, "
        "[string]$ElectronSource)\n"
        "& cmd.exe /c 'exit 1'\nWrite-Host 'Staging completed successfully'\n", encoding='utf-8',
    )
    handoff = builder.POWERSHELL.rsplit("if ($Action -eq 'setup') {", 1)[1].split(
        '    Start-Process', 1)[0]
    quoted_repo = str(repo).replace("'", "''")
    runner = tmp_path / 'handoff.ps1'
    runner.write_text(f"$ErrorActionPreference = 'Stop'\n$repo='{quoted_repo}'\n$kit=$repo\n{handoff}",
                      encoding='utf-8')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(runner)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Staging completed successfully' in result.stdout
