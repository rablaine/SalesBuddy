"""Exercise the real historical migration with isolated SQLite and local-only VM test payloads."""
import hashlib
import inspect
import json
import shutil
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from scripts import build_upgrade_vm_kit as builder
from scripts import vm_legacy_migration_smoke as legacy
from scripts import vm_upgrade_smoke as smoke


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    """Create the original two-note fixture using the real offline initialization pipeline."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    database = tmp_path / 'vm.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    return smoke.seed_database(repo, tmp_path / 'initial.db')


def test_real_historical_failure_then_fixed_baseline_repairs_same_database(
    fixture_db, tmp_path, monkeypatch,
):
    """The omitted mapping causes a real rebuild FK error, then fixed initialization recovers."""
    from app import migrations
    from app.upgrade_rehearsal import initialize_offline
    from scripts.upgrade import diagnostic_error

    database = Path(fixture_db['database'])
    backup = tmp_path / 'before.db'
    proof = legacy.seed_legacy(database, fixture_db, backup)
    legacy.verify_task(database, proof, legacy=True)
    smoke.verify_records(fixture_db)
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute('SELECT COUNT(*) FROM msx_tasks').fetchone() == (0,)
        assert saved.execute('PRAGMA foreign_key_check').fetchall() == []
    fixed = migrations._migrate_owned_task_inventory
    namespace = dict(vars(migrations))
    old_source = legacy.pre_fix_source(inspect.getsource(fixed))
    exec(compile(old_source, migrations.__file__, 'exec'), namespace)
    monkeypatch.setattr(migrations, '_migrate_owned_task_inventory',
                        namespace['_migrate_owned_task_inventory'])
    with pytest.raises(RuntimeError, match='owned-tasks migration failed') as failure:
        initialize_offline(database)
    diagnostic = diagnostic_error(failure.value)
    assert diagnostic['category'] == 'migration_foreign_keys'
    assert diagnostic['parent_tables'] == ['_old_call_logs']
    assert any(frame['function'] == '_rebuild_sqlite_table' for frame in diagnostic['frames'])
    legacy.verify_task(database, proof, legacy=True)
    smoke.verify_records(fixture_db)
    monkeypatch.setattr(migrations, '_migrate_owned_task_inventory', fixed)
    assert initialize_offline(database)['success'] is True
    legacy.verify_task(database, proof, legacy=False)
    assert initialize_offline(database)['success'] is True
    smoke.verify_records(fixture_db)


@pytest.mark.parametrize('table', ['customers', 'notes', 'milestones', 'msx_tasks'])
def test_seed_refuses_extra_nonfixture_data_without_modifying_schema(
    fixture_db, tmp_path, table,
):
    """An edited or no-longer-disposable fixture cannot silently enter the destructive test."""
    database = Path(fixture_db['database'])
    with closing(sqlite3.connect(database)) as connection, connection:
        if table == 'customers':
            connection.execute(
                "INSERT INTO customers (name, tpid, created_at) "
                "SELECT 'Extra', -999, created_at FROM customers LIMIT 1",
            )
        elif table == 'notes':
            connection.execute(
                'INSERT INTO notes (content, customer_id, call_date, created_at, updated_at) '
                "SELECT 'extra', customer_id, call_date, created_at, updated_at FROM notes LIMIT 1",
            )
        elif table == 'milestones':
            connection.execute(
                "INSERT INTO milestones (url, created_at, updated_at, on_my_team) "
                "VALUES ('test', '2026-10-22', '2026-10-22', 0)",
            )
        else:
            connection.execute(
                'INSERT INTO msx_tasks (msx_task_id, subject, task_category, duration_minutes, '
                "is_hva, is_current_owner, created_at) VALUES ('extra','extra',0,60,0,1,'2026-10-22')",
            )
        before = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(RuntimeError, match='Only the original synthetic'):
        legacy.seed_legacy(database, fixture_db, tmp_path / 'backup.db')
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name",
        ).fetchall() == before


def test_backup_failure_prevents_every_fixture_write(fixture_db, tmp_path, monkeypatch):
    """A failed safety backup cannot be followed by task inserts or DDL."""
    from app import db_paths

    monkeypatch.setattr(db_paths, 'backup_database', lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match='no schema was changed'):
        legacy.seed_legacy(Path(fixture_db['database']), fixture_db, tmp_path / 'backup.db')
    with closing(sqlite3.connect(fixture_db['database'])) as connection:
        assert connection.execute('SELECT COUNT(*) FROM msx_tasks').fetchone() == (0,)
        assert connection.execute('SELECT COUNT(*) FROM milestones').fetchone() == (0,)


def test_fixture_transaction_rolls_back_unexpected_schema(fixture_db, tmp_path):
    """A schema guard error leaves all original records and definitions unchanged."""
    database = Path(fixture_db['database'])
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE unexpected_links (task_id REFERENCES msx_tasks(id))')
    with pytest.raises(RuntimeError, match='Unexpected inbound'):
        legacy.seed_legacy(database, fixture_db, tmp_path / 'backup.db')
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM milestones').fetchone() == (0,)
        assert connection.execute('SELECT COUNT(*) FROM msx_tasks').fetchone() == (0,)
    smoke.verify_records(fixture_db)


def test_pre_fix_candidate_changes_exactly_the_historical_mapping():
    """The candidate retains every other byte of product migration logic."""
    source = (Path(__file__).parents[1] / 'app' / 'migrations.py').read_text(encoding='utf-8')
    old = legacy.pre_fix_source(source)
    assert old == source.replace(legacy.FIX_LINE, '', 1)
    for invalid in (old, source + legacy.FIX_LINE):
        with pytest.raises(RuntimeError, match='missing or ambiguous'):
            legacy.pre_fix_source(invalid)


def test_addon_zip_complete_and_fenced_to_existing_kit(tmp_path):
    """The transferable deliverable contains checksummed helpers and no shell, DB or credentials."""
    repo = Path(__file__).parents[1]
    original = tmp_path / 'original.zip'
    with zipfile.ZipFile(original, 'w') as archive:
        archive.writestr('kit-manifest.json', json.dumps({'kit_id': 'original-test-kit'}))
    output = tmp_path / 'legacy.zip'
    builder.build_legacy_migration_addon(repo, original, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            'vm_legacy_migration_smoke.py', 'vm_upgrade_smoke.py', 'legacy-migration-addon.json',
            'Legacy-VM-Test.ps1', 'LEGACY-TEST-START-HERE.txt', '6-TEST-LEGACY-MIGRATION.cmd',
            '7-CHECK-LEGACY-ROLLBACK.cmd', '8-RESTART-AND-CHECK-LEGACY.cmd',
        }
        manifest = json.loads(archive.read('legacy-migration-addon.json'))
        assert manifest['kit_id'] == 'original-test-kit'
        for name, checksum in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == checksum
        assert archive.read('vm_legacy_migration_smoke.py') == (
            repo / 'scripts' / 'vm_legacy_migration_smoke.py'
        ).read_bytes()
        assert 'step 5 passed' in archive.read('LEGACY-TEST-START-HERE.txt').decode()
        script = tmp_path / 'Legacy-VM-Test.ps1'
        script.write_bytes(archive.read('Legacy-VM-Test.ps1'))
    command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def recovered_case(fixture_db, tmp_path, monkeypatch):
    """Build genuine legacy snapshots plus the product's actual sanitized report shape."""
    from app.db_paths import backup_database
    from app.upgrade_rehearsal import initialize_offline
    from scripts import upgrade

    database = Path(fixture_db['database'])
    proof = legacy.seed_legacy(database, fixture_db, tmp_path / 'before.db')
    repo = tmp_path / 'installed'
    (repo / 'data').mkdir(parents=True)
    shell = repo / 'electron-dist' / 'resources' / 'app.asar'
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b'known-good desktop')
    attempt = 'a' * 32
    point = database.parent / 'upgrade-recovery' / attempt
    (point / 'trial').mkdir(parents=True)
    (point / 'failed-state').mkdir()
    assert backup_database(point / 'database.db', src=database)
    assert backup_database(point / 'failed-state' / 'database.db', src=database)
    diagnostic = {
        'category': 'migration_foreign_keys', 'parent_tables': ['_old_call_logs'],
        'type': 'RuntimeError', 'violation_count': 1,
        'frames': [{'file': 'migrations.py', 'function': '_rebuild_sqlite_table', 'line': 600}],
    }
    upgrade.atomic_json(point / 'trial' / 'rehearsal-result.json',
                        {'success': False, 'error': diagnostic})
    current = {
        'attempt_id': attempt, 'target_commit': 'b' * 40, 'previous_commit': 'c' * 40,
        'phase': 'restored', 'notification_pending': False,
        'acknowledged_at': '2026-10-22T10:01:00+00:00',
        'report_queued_at': '2026-10-22T10:00:00+00:00',
        'original_failure': {'failed_phase': 'rehearsing', 'error': diagnostic},
    }
    marker = {
        'schema_version': 1, 'kit_id': 'test', 'repo': str(repo), 'stage': 'requested',
        'previous_attempt': 'd' * 32, 'previous_boot': 'original-boot',
        'target': current['target_commit'], 'baseline': current['previous_commit'],
        'shell_sha256': smoke.digest(shell), **proof,
    }
    upgrade.atomic_json(repo / 'data' / legacy.MARKER, marker)
    report_path = database.parent / 'diagnostic-outbox' / (attempt + '-report.json')
    upgrade.atomic_json(report_path, {
        'report_id': attempt, 'payload': {'report': upgrade.failure_report(repo, current)},
    })
    assert initialize_offline(database)['success']
    fixture = {**fixture_db, 'good': current['previous_commit'], 'kit_id': 'test'}
    monkeypatch.setattr(upgrade, 'load_state', lambda path: current)
    monkeypatch.setattr(smoke, 'run', lambda path, *args: current['previous_commit'])
    monkeypatch.setattr(legacy, 'healthy_boot', lambda path, revision: 'restored-boot')
    return repo, fixture, current, point, report_path


def test_checker_accepts_real_report_shape_and_requires_another_boot(recovered_case, monkeypatch):
    """The actual report uses phase, not failed_phase; restart cannot reuse the recovered boot."""
    repo, fixture, current, point, report = recovered_case
    legacy.check(repo, fixture)
    with pytest.raises(RuntimeError, match='new desktop/worker boot'):
        legacy.check(repo, fixture, restart=True)
    monkeypatch.setattr(legacy, 'healthy_boot', lambda path, revision: 'second-boot')
    legacy.check(repo, fixture, restart=True)


@pytest.mark.parametrize('damage', [
    'wrong-phase', 'old-attempt', 'live-migration', 'missing-report', 'late-report',
    'wrong-report-phase', 'wrong-stack', 'changed-note', 'missing-task', 'old-boot',
])
def test_checker_rejects_incomplete_or_stale_proof(recovered_case, damage, monkeypatch):
    """No PASS from a different failure, old startup proof, lost data or late/missing reporting."""
    from scripts import upgrade

    repo, fixture, current, point, report_path = recovered_case
    if damage == 'wrong-phase':
        current['original_failure']['failed_phase'] = 'startup'
    elif damage == 'old-attempt':
        current['attempt_id'] = 'd' * 32
    elif damage == 'live-migration':
        upgrade.atomic_json(point / 'live-result.json', {'success': False})
    elif damage == 'missing-report':
        report_path.unlink()
    elif damage == 'late-report':
        current['report_queued_at'] = '2026-10-22T10:02:00+00:00'
    elif damage == 'wrong-report-phase':
        report = upgrade.read_json(report_path)
        report['payload']['report']['upgrade']['phase'] = 'startup'
        upgrade.atomic_json(report_path, report)
    elif damage == 'wrong-stack':
        trial_path = point / 'trial' / 'rehearsal-result.json'
        trial = upgrade.read_json(trial_path)
        trial['error']['frames'] = []
        upgrade.atomic_json(trial_path, trial)
    elif damage in {'changed-note', 'missing-task'}:
        with closing(sqlite3.connect(fixture['database'])) as connection, connection:
            if damage == 'changed-note':
                connection.execute("UPDATE notes SET content='unexpected'")
            else:
                connection.execute('DELETE FROM msx_tasks')
    else:
        monkeypatch.setattr(legacy, 'healthy_boot', lambda path, revision: 'original-boot')
    with pytest.raises(RuntimeError):
        legacy.check(repo, fixture)


def test_start_uses_real_local_git_candidate_and_desktop_request(fixture_db, tmp_path, monkeypatch):
    """The add-on changes only the historical mapping and requests the packaged desktop watcher."""
    from scripts import upgrade

    repo = tmp_path / 'installed'
    (repo / 'app').mkdir(parents=True)
    (repo / 'data').mkdir()
    shell = repo / 'electron-dist' / 'resources' / 'app.asar'
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b'test desktop')
    migrations = repo / 'app' / 'migrations.py'
    shutil.copy2(Path(__file__).parents[1] / 'app' / 'migrations.py', migrations)
    smoke.run(repo, 'init', '-b', 'main')
    baseline = smoke.commit_files(repo, ['app/migrations.py'], 'test: good baseline',
                                  tmp_path / 'paths.txt')
    work = tmp_path / 'vm-session'
    work.mkdir()
    remote = work / 'updates.git'
    smoke.run(repo, 'clone', '--bare', str(repo), str(remote))
    fixture = {**fixture_db, 'good': baseline, 'work': str(work), 'remote': str(remote),
               'kit_id': 'isolated'}
    monkeypatch.setattr(smoke, 'check', lambda *args, **kwargs: None)
    monkeypatch.setattr(upgrade, 'load_state', lambda path: {'attempt_id': 'a' * 32})
    monkeypatch.setattr(legacy, 'healthy_boot', lambda path, revision: 'initial-boot')
    legacy.start(repo, tmp_path, fixture)
    marker = upgrade.read_json(repo / 'data' / legacy.MARKER)
    target = marker['target']
    assert target != baseline
    assert smoke.run(repo, 'rev-parse', 'HEAD') == baseline
    assert smoke.run(repo, '--git-dir', str(remote), 'rev-parse', 'refs/heads/main') == target
    assert smoke.run(repo, 'diff', '--name-only', baseline, target) == 'app/migrations.py'
    candidate = subprocess.run(
        ['git', 'show', f'{target}:app/migrations.py'], cwd=repo, check=True,
        capture_output=True, encoding='utf-8', timeout=30,
    ).stdout
    assert candidate == legacy.pre_fix_source(migrations.read_text(encoding='utf-8'))
    assert (repo / 'data' / 'electron-update.request').read_text().strip() == 'update'
    legacy.verify_task(Path(fixture['database']), marker, legacy=True)
    smoke.verify_records(fixture)
    with pytest.raises(RuntimeError, match='already started'):
        legacy.start(repo, tmp_path, fixture)
