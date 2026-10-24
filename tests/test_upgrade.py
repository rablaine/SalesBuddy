"""Offline upgrade safety, matched recovery and strict readiness regressions."""
import copy
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db_paths import backup_database
from scripts import upgrade


OLD = 'a' * 40
NEW = 'b' * 40


@pytest.mark.parametrize('fails', [False, True])
def test_upgrade_stage_timing_reports_success_or_failure(monkeypatch, caplog, fails):
    """Timing logs measure monotonic elapsed work and never hide a failed operation."""
    clock = [100.0]
    monkeypatch.setattr(upgrade.time, 'monotonic', lambda: clock[0])

    def run_stage():
        """Advance a deterministic clock inside the measured operation."""
        with upgrade._timed_stage('database migration rehearsal'):
            clock[0] += 1.25
            if fails:
                raise RuntimeError('private failure details')

    with caplog.at_level('INFO', logger='scripts.upgrade'):
        if fails:
            with pytest.raises(RuntimeError, match='private failure details'):
                run_stage()
        else:
            run_stage()
    outcome = 'failed' if fails else 'completed'
    assert 'Upgrade stage started: database migration rehearsal' in caplog.text
    assert f'Upgrade stage {outcome}: database migration rehearsal seconds=1.250' in caplog.text
    assert 'private failure details' not in caplog.text
    if fails:
        assert 'Upgrade stage completed' not in caplog.text


def test_tree_digest_preserves_existing_manifest_checksums(tmp_path):
    """Faster enumeration must keep the exact checksum format for older recovery runners."""
    for name in ('a.txt', 'a/nested.txt', 'Z.data', 'empty', '__pycache__/generated.pyc'):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'' if name == 'empty' else name.encode())
    expected = upgrade.hashlib.sha256()
    for path in sorted(tmp_path.rglob('*')):
        if path.is_file():
            expected.update(str(path.relative_to(tmp_path)).encode('utf-8'))
            expected.update(b'\0')
            expected.update(upgrade._digest(path).encode('ascii'))
    assert upgrade._tree_digest(tmp_path) == expected.hexdigest()
    assert upgrade._tree_digest(tmp_path, ignore_cache=True) != expected.hexdigest()


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Model an isolated installation with real files and SQLite, never user data."""
    repo = tmp_path / 'install'
    repo.mkdir()
    (repo / 'app').mkdir()
    shutil.copy2(Path(__file__).parents[1] / 'app' / 'db_paths.py', repo / 'app' / 'db_paths.py')
    python = upgrade._python(repo / 'venv')
    python.parent.mkdir(parents=True)
    python.write_bytes(b'old interpreter')
    (repo / 'venv' / 'package.txt').write_text('old dependencies')
    (repo / 'electron-dist').mkdir()
    (repo / 'electron-dist' / 'Sales Buddy.exe').write_bytes(b'old shell')
    (repo / 'source.py').write_text('old source')
    database = tmp_path / 'storage' / 'salesbuddy.db'
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE notes (id INTEGER PRIMARY KEY, customer_id INTEGER, content TEXT,
                                FOREIGN KEY (customer_id) REFERENCES customers(id));
            INSERT INTO customers VALUES (11, 'Private fixture customer');
            INSERT INTO notes VALUES (21, 11, 'Private fixture note');
            PRAGMA user_version=1;
        """)
    module = SimpleNamespace(resolve_db_path=lambda: database, backup_database=backup_database)
    real_db_module = upgrade._db_module
    real_sender = upgrade.start_report_delivery
    monkeypatch.setattr(upgrade, '_db_module', lambda _: module)
    control = {'revision': OLD, 'dirty': False, 'requirements': False, 'fail': None, 'calls': []}

    def run(_repo, argv, **_kwargs):
        """Emulate checked commands while exercising actual snapshots and filesystem swaps."""
        control['calls'].append(argv)
        if 'venv' in argv and '-m' in argv:
            target = upgrade._python(Path(argv[-1]))
            target.parent.mkdir(parents=True)
            target.write_bytes(b'isolated interpreter')
        if 'freeze' in argv:
            return 'Flask==3.1.0\nSQLAlchemy==2.0.0\npython-dotenv==1.0.0'
        if argv[:2] == ['git', 'show']:
            return 'Flask==3.1.0\nSQLAlchemy==2.0.0\npython-dotenv==1.0.0'
        if argv[:3] == ['git', 'rev-parse', 'HEAD']:
            return control['revision']
        if argv[:2] == ['git', 'status']:
            return ' M source.py' if control['dirty'] else ''
        if argv[:3] == ['git', 'rev-parse', '--verify']:
            return NEW
        if argv[:2] == ['git', 'archive']:
            output = next(value.split('=', 1)[1] for value in argv if value.startswith('--output='))
            Path(output).write_bytes(('code archive ' + control['revision']).encode())
        if argv[:2] == ['git', 'merge']:
            control['revision'] = NEW
            (repo / 'source.py').write_text('new source')
        if argv[:2] == ['git', 'reset']:
            control['revision'] = argv[-1]
            (repo / 'source.py').write_text('old source')
        if argv[:2] == ['git', 'diff']:
            return 'requirements.txt' if control['requirements'] else ''
        if '--result' in argv:
            phase = 'migrating' if '--live' in argv else 'rehearsing'
            result = Path(argv[argv.index('--result') + 1])
            if control['fail'] == phase:
                upgrade.atomic_json(result, {'success': False, 'error': {
                    'type': 'RuntimeError', 'category': 'migration_foreign_keys',
                    'violation_count': 1, 'parent_tables': ['_old_call_logs'], 'frames': [],
                }})
                raise subprocess.CalledProcessError(1, argv, stderr='private exception text')
            upgrade.atomic_json(result, {'success': True, 'repeated': '--live' not in argv})
            if phase == 'migrating':
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute('PRAGMA user_version=2')
        if 'install' in argv and 'pip' in argv and 'venv.restore-' in argv[0]:
            (Path(argv[0]).parent.parent / 'package.txt').write_text('old dependencies')
        if control['fail'] == 'dependencies' and 'install' in argv and 'pip' in argv:
            raise subprocess.CalledProcessError(1, argv, stderr='private installation details')
        return ''

    monkeypatch.setattr(upgrade, '_run', run)
    monkeypatch.setattr(upgrade, 'start_report_delivery', lambda *_: None)
    monkeypatch.setattr(upgrade, 'recovery_contact_email', lambda: None)
    return SimpleNamespace(repo=repo, database=database, control=control,
                           real_db_module=real_db_module, real_sender=real_sender)


@pytest.mark.parametrize('fails', [False, True])
def test_update_pipeline_logs_measured_safety_stages(installation, caplog, fails):
    """Actual update stages emit timing logs, including terminal rehearsal failure."""
    fixture = installation
    with caplog.at_level('INFO', logger='scripts.upgrade'):
        upgrade.prepare(fixture.repo)
        if fails:
            fixture.control['fail'] = 'rehearsing'
            with pytest.raises(upgrade.RehearsalError):
                upgrade.apply_update(fixture.repo)
        else:
            upgrade.apply_update(fixture.repo)
    for stage in (
        'application recovery snapshot', 'desktop shell recovery snapshot',
        'database recovery snapshot', 'recovery snapshot verification',
    ):
        assert f'Upgrade stage completed: {stage} seconds=' in caplog.text
    if fails:
        assert 'Upgrade stage failed: database migration rehearsal seconds=' in caplog.text
        assert 'Upgrade stage completed: database migration rehearsal' not in caplog.text
        assert 'Upgrade stage started: live database initialization' not in caplog.text
    else:
        for stage in ('database migration rehearsal', 'live database initialization'):
            assert f'Upgrade stage completed: {stage} seconds=' in caplog.text


def make_legacy_point(fixture) -> dict:
    """Model an existing full-runtime recovery point without changing production defaults."""
    state = upgrade.prepare(fixture.repo)
    point = upgrade._point(fixture.repo, state)
    shutil.rmtree(point / 'runtime')
    shutil.copytree(fixture.repo / 'venv', point / 'runtime')
    state.pop('runtime_mode')
    state.pop('runtime_ignores_cache')
    state['runtime_sha256'] = upgrade._tree_digest(point / 'runtime')
    upgrade.save_state(fixture.repo, state, 'prepared')
    return state


def test_prepare_freezes_a_verified_matched_point(installation):
    """All old application components are frozen before any mutation is admitted."""
    fixture = installation
    original = fixture.database.read_bytes()
    state = upgrade.prepare(fixture.repo, shell_rebuild=True)
    point = upgrade._point(fixture.repo, state)
    assert state['phase'] == 'prepared'
    assert state['recovery_ready'] is True
    assert state['changes_started'] is False
    assert state['needs_shell_rebuild'] is True
    assert state['previous_commit'] == OLD
    assert fixture.control['revision'] == OLD
    assert fixture.database.read_bytes() == original
    for name, key in (
        ('application.zip', 'code_sha256'), ('database.db', 'database_sha256'),
        ('recover.py', 'runner_sha256'), ('db_paths.py', 'backup_helper_sha256'),
    ):
        assert upgrade._digest(point / name) == state[key]
    assert upgrade._tree_digest(point / 'runtime', ignore_cache=True) == state['runtime_sha256']
    assert upgrade._tree_digest(point / 'shell') == state['shell_sha256']
    with closing(sqlite3.connect(point / 'database.db')) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT content FROM notes').fetchone()[0] == 'Private fixture note'


def test_prepare_folds_uncheckpointed_wal_without_writing_to_the_source(installation):
    """The verified snapshot includes committed WAL rows without changing source bytes."""
    fixture = installation
    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO notes VALUES (22, 11, 'Committed WAL fixture')")
        connection.commit()
        before = fixture.database.read_bytes()
        wal = Path(str(fixture.database) + '-wal')
        before_wal = wal.read_bytes()
        state = upgrade.prepare(fixture.repo)
        assert fixture.database.read_bytes() == before
        assert wal.read_bytes() == before_wal
        with closing(sqlite3.connect(
            upgrade._point(fixture.repo, state) / 'database.db',
        )) as backup:
            assert backup.execute('SELECT id FROM notes ORDER BY id').fetchall() == [(21,), (22,)]


def test_normal_updates_never_scan_copy_or_install_the_live_environment(
    installation, monkeypatch,
):
    """The regular update path is independent of installed environment size."""
    fixture = installation
    digest = upgrade._tree_digest
    copytree = shutil.copytree

    def checked_digest(path, **kwargs):
        """Reject expensive traversal of the installed packages."""
        assert Path(path) != fixture.repo / 'venv'
        return digest(path, **kwargs)

    def checked_copy(source, target, *args, **kwargs):
        """Reject copying installed dependencies."""
        assert Path(source) != fixture.repo / 'venv'
        return copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(upgrade, '_tree_digest', checked_digest)
    monkeypatch.setattr(shutil, 'copytree', checked_copy)
    first = upgrade.prepare(fixture.repo)
    first_point = upgrade._point(fixture.repo, first)
    upgrade.save_state(fixture.repo, first, 'ready', accepted_at=upgrade.utc_timestamp())
    with closing(sqlite3.connect(fixture.database)) as connection, connection:
        connection.execute("INSERT INTO notes VALUES (22, 11, 'Newly saved note')")
    second = upgrade.prepare(fixture.repo)
    second_point = upgrade._point(fixture.repo, second)
    assert second['runtime_mode'] == 'rebuild-on-recovery'
    assert not (second_point / 'runtime' / 'package.txt').exists()
    with closing(sqlite3.connect(second_point / 'database.db')) as connection:
        assert connection.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 2
    with closing(sqlite3.connect(first_point / 'database.db')) as connection:
        assert connection.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 1
    upgrade.apply_update(fixture.repo)
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert (fixture.repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
    assert not any('pip' in call for call in fixture.control['calls'])
    assert not (fixture.repo / f'venv.failed-{second["attempt_id"]}').exists()


def test_package_changes_are_recorded_before_pip_and_rebuilt_only_during_recovery(
    installation, monkeypatch,
):
    """A partial pip installation cannot destroy the previous exact dependency record."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    fixture.control.update(requirements=True, fail='dependencies')
    original = upgrade._run

    def inspect_install(repo, argv, **kwargs):
        """Prove crash-recovery state is already durable at the moment pip starts."""
        if 'install' in argv and argv[0] == str(upgrade._python(fixture.repo / 'venv')):
            saved = upgrade.load_state(repo)
            assert saved['dependencies_attempted'] is True
            assert upgrade._digest(
                upgrade._point(repo, saved) / 'requirements-recovery.txt',
            ) == saved['dependencies_sha256']
        return original(repo, argv, **kwargs)

    monkeypatch.setattr(upgrade, '_run', inspect_install)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.apply_update(fixture.repo)
    state = upgrade.load_state(fixture.repo)
    point = upgrade._point(fixture.repo, state)
    assert state['dependencies_attempted'] is True
    assert 'Flask==3.1.0' in (point / 'requirements-recovery.txt').read_text()
    assert upgrade._digest(point / 'requirements-recovery.txt') == state['dependencies_sha256']
    calls = fixture.control['calls']
    assert next(i for i, call in enumerate(calls) if 'freeze' in call) < next(
        i for i, call in enumerate(calls) if 'install' in call)
    fixture.control['fail'] = None
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert (fixture.repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
    assert any('install' in call and 'venv.restore-' in call[0] for call in calls)


def test_retry_never_overwrites_the_pre_install_dependency_record(installation):
    """Retrying a partial install retains versions from before the first pip attempt."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    fixture.control.update(requirements=True, fail='dependencies')
    for _ in range(2):
        with pytest.raises(subprocess.CalledProcessError):
            upgrade.apply_update(fixture.repo)
    assert sum('freeze' in call for call in fixture.control['calls']) == 1
    assert upgrade.load_state(fixture.repo)['dependencies_attempted'] is True


def test_dependency_capture_failure_cannot_start_package_installation(installation, monkeypatch):
    """An unavailable pip inventory leaves packages untouched and recovery usable."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    fixture.control['requirements'] = True
    original = upgrade._run

    def missing_inventory(repo, argv, **kwargs):
        """Model pip failing before it has permission to change installed packages."""
        if 'freeze' in argv:
            raise subprocess.CalledProcessError(1, argv, stderr='pip inventory unavailable')
        return original(repo, argv, **kwargs)

    monkeypatch.setattr(upgrade, '_run', missing_inventory)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.apply_update(fixture.repo)
    assert not upgrade.load_state(fixture.repo).get('dependencies_attempted')
    assert not any('install' in call for call in fixture.control['calls'])
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'


def test_tampered_dependency_record_blocks_restore_before_any_mutation(installation):
    """Recovery must not install from a changed package record."""
    fixture = installation
    state = upgrade.prepare(fixture.repo)
    fixture.control['requirements'] = True
    upgrade.apply_update(fixture.repo)
    point = upgrade._point(fixture.repo, state)
    (point / 'requirements-recovery.txt').write_text('unexpected-package==1.0')
    with pytest.raises(RuntimeError, match='dependency record failed verification'):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert fixture.control['revision'] == NEW
    assert not (point / 'failed-state').exists()


@pytest.mark.parametrize('problem', ['missing-package', 'unrelated-error', 'startup-package-error'])
def test_code_only_recovery_repairs_only_diagnosed_dependency_failures(
    installation, monkeypatch, problem,
):
    """Missing packages justify repair; unrelated process failures must remain visible."""
    fixture = installation
    state = upgrade.prepare(fixture.repo)
    upgrade.apply_update(fixture.repo)
    if problem == 'startup-package-error':
        upgrade.save_state(fixture.repo, state, 'failed', startup_failure={
            'error': {'category': 'dependency_missing'},
        })
    original = upgrade._run

    def fail_import(repo, argv, **kwargs):
        """Model a package import failure without making the independent launcher fail."""
        if (argv[0] == str(upgrade._python(fixture.repo / 'venv')) and '-c' in argv
                and problem != 'startup-package-error'):
            message = ('ModuleNotFoundError: No module named flask' if problem ==
                       'missing-package' else 'PermissionError: interpreter blocked')
            raise subprocess.CalledProcessError(1, argv, stderr=message)
        return original(repo, argv, **kwargs)

    monkeypatch.setattr(upgrade, '_run', fail_import)
    if problem == 'unrelated-error':
        with pytest.raises(subprocess.CalledProcessError):
            upgrade.restore(fixture.repo, confirmed=True, launch=False)
        assert not any('pip' in call for call in fixture.control['calls'])
        assert fixture.control['revision'] == NEW
    else:
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
        assert any(call[:2] == ['git', 'show'] for call in fixture.control['calls'])
        assert any('install' in call for call in fixture.control['calls'])
        assert fixture.control['revision'] == OLD


def test_failed_package_rebuild_preserves_live_environment_and_can_retry(installation):
    """A failed download cannot replace the live environment or claim completed recovery."""
    fixture = installation
    initial = upgrade.prepare(fixture.repo)
    fixture.control['requirements'] = True
    upgrade.apply_update(fixture.repo)
    fixture.control['fail'] = 'dependencies'
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['phase'] == 'failed'
    assert fixture.control['revision'] == NEW
    assert not (fixture.repo / f'venv.failed-{initial["attempt_id"]}').exists()
    assert (fixture.repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
    fixture.control['fail'] = None
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'


def test_legacy_full_environment_recovery_remains_supported(installation):
    """Already-created recovery points still restore their original complete runtime."""
    fixture = installation
    state = make_legacy_point(fixture)
    upgrade.apply_update(fixture.repo)
    (fixture.repo / 'venv' / 'package.txt').write_text('damaged legacy environment')
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert (fixture.repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
    assert not any('pip' in call for call in fixture.control['calls'])
    assert upgrade.load_state(fixture.repo)['attempt_id'] == state['attempt_id']


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop environment recovery')
def test_recovery_rebuilds_with_real_pip_from_recorded_versions_offline(
    installation, monkeypatch, tmp_path,
):
    """Exercise real venv creation and pip without downloading packages or changing user data."""
    fixture = installation
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    for distribution, version, module in (
        ('Flask', '3.1.0', 'flask'), ('SQLAlchemy', '2.0.0', 'sqlalchemy'),
        ('python_dotenv', '1.0.0', 'dotenv'),
    ):
        metadata = f'{distribution}-{version}.dist-info'
        files = {
            f'{module}/__init__.py': f"__version__ = '{version}'\n",
            f'{metadata}/METADATA': (
                f'Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n'
            ),
            f'{metadata}/WHEEL': (
                'Wheel-Version: 1.0\nGenerator: isolated-upgrade-test\n'
                'Root-Is-Purelib: true\nTag: py3-none-any\n'
            ),
        }
        files[f'{metadata}/RECORD'] = ''.join(f'{name},,\n' for name in files)
        with zipfile.ZipFile(wheels / f'{distribution}-{version}-py3-none-any.whl', 'w') as wheel:
            for name, content in files.items():
                wheel.writestr(name, content)
    monkeypatch.setenv('PIP_NO_INDEX', '1')
    monkeypatch.setenv('PIP_FIND_LINKS', str(wheels))
    mocked = upgrade._run

    def run(repo, argv, **kwargs):
        """Use actual offline Python tooling only for bootstrap and staged repair."""
        if (('-m' in argv and 'venv' in argv)
                or ('pip' in argv and 'venv.restore-' in argv[0])):
            return subprocess.run(
                argv, cwd=repo, capture_output=True, text=True, check=True, timeout=120,
                **kwargs,
            ).stdout.strip()
        return mocked(repo, argv, **kwargs)

    monkeypatch.setattr(upgrade, '_run', run)
    state = upgrade.prepare(fixture.repo)
    fixture.control['requirements'] = True
    upgrade.apply_update(fixture.repo)
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    result = subprocess.run(
        [str(upgrade._python(fixture.repo / 'venv')), '-I', '-c',
         'import flask, sqlalchemy, dotenv; '
         'print(flask.__version__, sqlalchemy.__version__, dotenv.__version__)'],
        capture_output=True, text=True, check=True, timeout=30,
    )
    assert result.stdout.strip() == '3.1.0 2.0.0 1.0.0'
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'
    assert (fixture.repo / f'venv.failed-{state["attempt_id"]}' / 'package.txt').exists()


def test_generated_launcher_bytecode_does_not_break_recovery(installation):
    """The small launcher can regenerate disposable Python startup caches."""
    fixture = installation
    first = upgrade.prepare(fixture.repo)
    first_point = upgrade._point(fixture.repo, first)
    frozen_cache = first_point / 'runtime' / '__pycache__'
    frozen_cache.mkdir()
    (frozen_cache / 'startup.pyc').write_bytes(b'generated during Python startup')
    upgrade.verify_recovery(fixture.repo)


def test_legacy_runtime_snapshot_checksums_remain_strict(installation):
    """Old manifests retain the original checksum rules, including bytecode files."""
    fixture = installation
    state = upgrade.prepare(fixture.repo)
    del state['runtime_ignores_cache']
    upgrade.save_state(fixture.repo, state, 'prepared')
    cache = upgrade._point(fixture.repo, state) / 'runtime' / '__pycache__'
    cache.mkdir()
    (cache / 'unexpected.pyc').write_bytes(b'unexpected addition to an old-format snapshot')
    with pytest.raises(RuntimeError, match='runtime verification failed'):
        upgrade.verify_recovery(fixture.repo)


def test_pruning_old_launchers_does_not_break_newer_recovery(installation):
    """Each point owns its small independent recovery launcher."""
    fixture = installation
    points = []
    for index in range(4):
        state = upgrade.prepare(fixture.repo)
        points.append(upgrade._point(fixture.repo, state))
        upgrade.save_state(fixture.repo, state, 'ready', accepted_at=str(index))
        upgrade._prune_successful_points(fixture.repo, state)
    assert not points[0].exists() and not points[1].exists()
    assert points[2].exists() and points[3].exists()
    assert upgrade._python(points[3] / 'runtime').exists()
    upgrade.verify_recovery(fixture.repo)


@pytest.mark.parametrize('failure', ['dirty', 'space', 'backup'])
def test_preparation_failure_never_changes_live_state(installation, monkeypatch, failure):
    """Refused/partial recovery preparation cannot become an update or usable restore point."""
    fixture = installation
    original = fixture.database.read_bytes()
    if failure == 'dirty':
        fixture.control['dirty'] = True
    elif failure == 'space':
        monkeypatch.setattr(shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    else:
        monkeypatch.setattr(upgrade, '_snapshot_database',
                            lambda *_: (_ for _ in ()).throw(RuntimeError('Backup failed')))
    with pytest.raises(RuntimeError):
        upgrade.prepare(fixture.repo)
    assert fixture.control['revision'] == OLD
    assert fixture.database.read_bytes() == original
    state = upgrade.load_state(fixture.repo)
    if state:
        assert state['phase'] == 'failed' and not state['recovery_ready']
        assert not (upgrade._point(fixture.repo, state) / 'runtime').exists()


def test_retry_keeps_the_original_recovery_point(installation):
    """Repeated preparations and failed applications never snapshot the broken version as safe."""
    fixture = installation
    first = upgrade.prepare(fixture.repo)
    assert upgrade.prepare(fixture.repo)['attempt_id'] == first['attempt_id']
    fixture.control['fail'] = 'rehearsing'
    with pytest.raises(upgrade.RehearsalError):
        upgrade.apply_update(fixture.repo)
    resumed = upgrade.prepare(fixture.repo, shell_rebuild=True)
    assert resumed['attempt_id'] == first['attempt_id']
    assert resumed['previous_commit'] == OLD
    assert resumed['needs_shell_rebuild'] is True


@pytest.mark.parametrize('failure', ['dependencies', 'rehearsing', 'migrating'])
def test_update_failure_is_durable_and_recoverable(installation, failure):
    """Errors stop the pipeline and retain actionable, privacy-safe failure metadata."""
    fixture = installation
    original = fixture.database.read_bytes()
    upgrade.prepare(fixture.repo)
    fixture.control.update(fail=failure, requirements=True)
    with pytest.raises((subprocess.CalledProcessError, upgrade.RehearsalError)):
        upgrade.apply_update(fixture.repo)
    state = upgrade.load_state(fixture.repo)
    assert state['phase'] == 'failed' and state['failed_phase'] == failure
    assert state['recovery_ready'] and state['changes_started']
    assert fixture.database.read_bytes() == original
    if failure != 'migrating':
        assert not any('--live' in call for call in fixture.control['calls'])
    if failure != 'dependencies':
        assert state['error']['parent_tables'] == ['_old_call_logs']
    diagnostic = json.dumps(state['error'])
    assert 'Private fixture' not in diagnostic
    assert 'private exception' not in diagnostic
    assert str(fixture.database) not in diagnostic


def test_apply_waits_for_independent_readiness_before_success(installation):
    """Passing migrations means starting, not update complete; unchanged requirements skip pip."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    state = upgrade.apply_update(fixture.repo)
    assert state['phase'] == 'starting'
    assert not any('pip' in call for call in fixture.control['calls'])
    with closing(sqlite3.connect(fixture.database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2


@pytest.mark.parametrize('artifact', [
    'database.db', 'application.zip', 'runtime/package.txt', 'shell/Sales Buddy.exe',
    'recover.py', 'db_paths.py',
])
def test_recovery_rejects_tampered_artifacts_before_mutation(installation, artifact):
    """No component is swapped unless the entire matched restore point verifies."""
    fixture = installation
    state = upgrade.prepare(fixture.repo)
    point = upgrade._point(fixture.repo, state)
    (point / Path(artifact)).write_bytes(b'corrupt artifact')
    with pytest.raises(RuntimeError):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    with pytest.raises(RuntimeError):
        upgrade.apply_update(fixture.repo)
    assert fixture.control['revision'] == OLD
    assert not any(call[:2] == ['git', 'merge'] for call in fixture.control['calls'])
    assert not (point / 'failed-state').exists()


def test_recovery_requires_explicit_confirmation(installation):
    """An available recovery point is not authorization to replace user data."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    with pytest.raises(PermissionError):
        upgrade.restore(fixture.repo, launch=False)
    assert fixture.control['revision'] == OLD


def test_restore_recovers_all_components_and_preserves_failed_data(installation):
    """Restoration returns code, runtime, shell and SQLite together, retaining later notes."""
    fixture = installation
    initial = upgrade.prepare(fixture.repo)
    fixture.control['requirements'] = True
    upgrade.apply_update(fixture.repo)
    (fixture.repo / 'venv' / 'package.txt').write_text('new dependencies')
    (fixture.repo / 'electron-dist' / 'Sales Buddy.exe').write_bytes(b'new shell')
    with closing(sqlite3.connect(fixture.database)) as connection, connection:
        connection.execute("INSERT INTO notes VALUES (22, 11, 'Post-snapshot fixture note')")
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert fixture.control['revision'] == OLD
    assert (fixture.repo / 'source.py').read_text() == 'old source'
    assert (fixture.repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
    assert (fixture.repo / 'electron-dist' / 'Sales Buddy.exe').read_bytes() == b'old shell'
    with closing(sqlite3.connect(fixture.database)) as connection:
        assert connection.execute('SELECT id FROM notes').fetchall() == [(21,)]
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
    failed = upgrade._point(fixture.repo, initial) / 'failed-state'
    with closing(sqlite3.connect(failed / 'database.db')) as connection:
        assert connection.execute('SELECT id FROM notes').fetchall() == [(21,), (22,)]
    assert (fixture.repo / f'venv.failed-{initial["attempt_id"]}' / 'package.txt').read_text(
    ) == 'new dependencies'
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'


def test_restore_preserves_a_corrupted_failed_database_without_requiring_it_to_verify(
    installation,
):
    """A damaged new database cannot prevent restoring the already verified old snapshot."""
    fixture = installation
    initial = upgrade.prepare(fixture.repo)
    upgrade.apply_update(fixture.repo)
    fixture.database.write_bytes(b'Damaged failed database for a recovery regression')
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    failed = upgrade._point(fixture.repo, initial) / 'failed-state'
    assert (failed / 'database.db').read_bytes().startswith(b'Damaged failed database')
    with closing(sqlite3.connect(fixture.database)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT id FROM notes').fetchall() == [(21,)]


@pytest.mark.parametrize('tamper_staging', [False, True])
def test_interrupted_restore_can_resume_without_overwriting_failed_evidence(
    installation, monkeypatch, tamper_staging,
):
    """An interruption after moving the failed database does not strand recovery."""
    fixture = installation
    initial = upgrade.prepare(fixture.repo)
    upgrade.apply_update(fixture.repo)
    replace = os.replace
    failed_once = False

    def interrupt(source, target):
        """Fail the first final database swap, after all failed-state preservation."""
        nonlocal failed_once
        if Path(source).name == 'restore-database.db' and not failed_once:
            failed_once = True
            raise OSError('Interrupted final database swap')
        return replace(source, target)

    monkeypatch.setattr(os, 'replace', interrupt)
    with pytest.raises(OSError):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['failed_phase'] == 'restoring'
    if tamper_staging:
        (upgrade._point(fixture.repo, initial) / 'restore-database.db').write_bytes(
            b'Interrupted staging must not become the restored database',
        )
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'
    assert (upgrade._point(fixture.repo, initial) / 'failed-state' / 'database.db').exists()
    with closing(sqlite3.connect(fixture.database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.mark.parametrize('component', ['runtime', 'shell'])
def test_interrupted_staging_is_recreated_from_verified_artifacts(
    installation, monkeypatch, component,
):
    """A partially copied staging folder is repairable without replacing the recovery point."""
    fixture = installation
    make_legacy_point(fixture)
    upgrade.apply_update(fixture.repo)
    copytree = shutil.copytree
    failed_once = False
    prefix = 'venv.restore-' if component == 'runtime' else 'electron-dist.restore-'

    def interrupt(source, target, *args, **kwargs):
        """Leave a real partial staging directory on the first copy attempt."""
        nonlocal failed_once
        if Path(target).name.startswith(prefix) and not failed_once:
            failed_once = True
            Path(target).mkdir()
            (Path(target) / 'partial.txt').write_text('incomplete staging')
            raise OSError('Interrupted component copy')
        return copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copytree', interrupt)
    with pytest.raises(OSError):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert fixture.control['revision'] == NEW
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert fixture.control['revision'] == OLD
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'


def test_database_staging_can_resume_before_its_checksum_was_published(
    installation, monkeypatch,
):
    """A crash between the staged backup and its durable checksum cannot strand recovery."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    upgrade.apply_update(fixture.repo)
    save = upgrade.save_state
    failed_once = False

    def interrupt(repo, state, phase, **fields):
        """Interrupt exactly before the staged database checksum reaches durable state."""
        nonlocal failed_once
        if 'restore_database_sha256' in fields and not failed_once:
            failed_once = True
            raise OSError('Interrupted recovery checksum publication')
        return save(repo, state, phase, **fields)

    monkeypatch.setattr(upgrade, 'save_state', interrupt)
    with pytest.raises(OSError):
        upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert 'restore_database_sha256' not in upgrade.load_state(fixture.repo)
    upgrade.restore(fixture.repo, confirmed=True, launch=False)
    assert upgrade.load_state(fixture.repo)['phase'] == 'restored'
    with closing(sqlite3.connect(fixture.database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.fixture
def ready_pair():
    """Provide current ready metadata with distinct, positively identified children."""
    now = datetime.now(timezone.utc)
    status = {
        'schema_version': 1, 'state': 'ready', 'phase': 'ready', 'boot_id': 'new-boot',
        'expected_commit': NEW, 'started_at': (now - timedelta(seconds=10)).isoformat(),
        'updated_at': now.isoformat(),
        'children': [
            {'name': 'web', 'pid': 111, 'ready': True},
            {'name': 'worker', 'pid': 222, 'ready': True},
        ],
    }
    health = {
        'status': 'healthy', 'database': 'connected', 'boot_id': 'new-boot',
        'commit_full': NEW, 'worker': 'alive', 'worker_boot_id': 'new-boot',
        'worker_ready': True, 'web_pid': 111, 'worker_pid': 222,
        'worker_heartbeat_at': now.isoformat(),
    }
    return status, health, now


def test_readiness_accepts_only_the_final_two_phase_pair(ready_pair):
    """The final healthy web and activated worker can complete startup."""
    status, health, now = ready_pair
    assert upgrade.readiness_matches(status, health, 'new-boot', NEW, now=now)


@pytest.mark.parametrize(('document', 'key', 'value'), [
    ('status', 'state', 'starting'), ('status', 'phase', 'activating'),
    ('status', 'boot_id', 'old-boot'), ('status', 'expected_commit', OLD),
    ('status', 'children', []), ('status', 'updated_at', 'invalid'),
    ('health', 'boot_id', 'old-boot'), ('health', 'commit_full', OLD),
    ('health', 'worker_boot_id', 'old-boot'), ('health', 'worker_ready', False),
    ('health', 'worker_ready', None), ('health', 'worker', 'stale'),
    ('health', 'database', 'disconnected'), ('health', 'web_pid', 999),
    ('health', 'worker_pid', 999),
])
def test_readiness_rejects_stale_or_incomplete_identity(ready_pair, document, key, value):
    """An HTTP 200 response alone never establishes fresh web-and-worker readiness."""
    status, health, now = copy.deepcopy(ready_pair)
    (status if document == 'status' else health)[key] = value
    assert not upgrade.readiness_matches(status, health, 'new-boot', NEW, now=now)


@pytest.mark.parametrize(('status_age', 'worker_age'), [(31, 0), (0, 91), (-1, 0), (0, -1)])
def test_readiness_enforces_exact_freshness_bounds(ready_pair, status_age, worker_age):
    """Clock skew and stale durable/worker files cannot satisfy readiness."""
    status, health, now = ready_pair
    status['started_at'] = (now - timedelta(seconds=120)).isoformat()
    status['updated_at'] = (now - timedelta(seconds=status_age)).isoformat()
    health['worker_heartbeat_at'] = (now - timedelta(seconds=worker_age)).isoformat()
    assert not upgrade.readiness_matches(status, health, 'new-boot', NEW, now=now)


def test_accept_checks_health_before_marking_the_update_complete(
    installation, ready_pair, monkeypatch,
):
    """Only matching, independent HTTP health plus final supervisor state accepts an update."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    upgrade.apply_update(fixture.repo)
    status, health, _ = ready_pair
    upgrade.atomic_json(upgrade.startup_path(fixture.repo), status)
    monkeypatch.setattr(upgrade.urllib.request, 'urlopen',
                        lambda *_args, **_kwargs: io.BytesIO(json.dumps(health).encode()))
    upgrade.accept(fixture.repo, 'new-boot', 54321)
    assert upgrade.load_state(fixture.repo)['phase'] == 'ready'


def test_terminal_startup_failure_does_not_poll_forever(installation):
    """A durable failure for this boot exits before attempting HTTP or another restart."""
    fixture = installation
    upgrade.atomic_json(upgrade.startup_path(fixture.repo),
                        {'state': 'failed', 'boot_id': 'new-boot'})
    with pytest.raises(RuntimeError, match='bounded'):
        upgrade.wait_ready(fixture.repo, 'new-boot', 54321)


def test_incomplete_update_blocks_live_startup(installation):
    """A prepared but not yet applied update is a maintenance gate, not permission to migrate."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    with pytest.raises(RuntimeError, match='unfinished'):
        upgrade.startup_preflight(fixture.repo)


def test_upgrade_commands_are_serialized_and_the_lock_is_released(
    installation, monkeypatch,
):
    """A competing command cannot mutate the install, and exiting releases the OS lock."""
    fixture = installation
    clock = iter([0, 0, 6])
    monkeypatch.setattr(upgrade.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(upgrade.time, 'sleep', lambda _seconds: None)
    with upgrade.upgrade_lock(fixture.repo):
        with pytest.raises(RuntimeError, match='Another upgrade'):
            with upgrade.upgrade_lock(fixture.repo):
                pytest.fail('A second writer acquired the same installation lock')
    monkeypatch.undo()
    with upgrade.upgrade_lock(fixture.repo):
        assert (fixture.repo / 'data' / 'upgrade.lock').exists()


def test_rehearsal_retry_cannot_reuse_a_stale_success_result(installation):
    """A previous result is removed before an isolated migration subprocess can fail."""
    fixture = installation
    trial = fixture.database.parent / 'trial'
    trial.mkdir()
    upgrade.atomic_json(trial / 'rehearsal-result.json', {'success': True})
    fixture.control['fail'] = 'rehearsing'
    with pytest.raises(upgrade.RehearsalError):
        upgrade.rehearse_database(fixture.repo, fixture.database, trial)
    assert json.loads((trial / 'rehearsal-result.json').read_text())['success'] is False


@pytest.mark.skipif(os.name != 'nt', reason='The desktop uses a relocatable Windows venv')
def test_real_git_and_frozen_runtime_recover_a_broken_install(tmp_path, monkeypatch, request):
    """Restore code and data through the stdlib launcher without replacing healthy packages."""
    root = Path(__file__).parents[1]
    repo = tmp_path / 'real-install'
    repo.mkdir()
    database = tmp_path / 'isolated-data' / 'salesbuddy.db'

    def cleanup_owned_files() -> None:
        """Remove only this test's large isolated copies, including read-only Git objects."""
        def remove_readonly(function, path, error) -> None:
            """Retry deleting a read-only file created by the fixture's local Git history."""
            if isinstance(error, PermissionError) and Path(path).is_file():
                os.chmod(path, 0o600)
                function(path)
            else:
                raise error

        for owned in (repo, database.parent):
            if owned.exists():
                shutil.rmtree(owned, onexc=remove_readonly)

    request.addfinalizer(cleanup_owned_files)
    shutil.copytree(root / 'app', repo / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    (repo / 'scripts').mkdir()
    shutil.copy2(root / 'scripts' / 'upgrade.py', repo / 'scripts' / 'upgrade.py')
    shutil.copy2(root / 'requirements.txt', repo / 'requirements.txt')
    (repo / '.gitignore').write_text('venv/\nvenv.*/\nelectron-dist*/\ndata/\n__pycache__/\n')
    subprocess.run(
        [sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages',
         str(repo / 'venv')], check=True, capture_output=True, text=True,
    )
    (repo / 'venv' / 'Lib' / 'site-packages' / 'fixture-dependencies.pth').write_text(
        str(Path(sys.prefix) / 'Lib' / 'site-packages'), encoding='utf-8',
    )
    (repo / 'venv' / 'runtime-version.txt').write_text('old runtime')
    (repo / 'electron-dist').mkdir()
    (repo / 'electron-dist' / 'Sales Buddy.exe').write_bytes(b'old isolated test shell')
    database.parent.mkdir()
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    monkeypatch.setenv('TESTING', 'true')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    from app.upgrade_rehearsal import initialize_offline

    initialize_offline(database)

    def git(*arguments):
        """Use an isolated local Git history; no fetches, pushes or external systems."""
        return subprocess.run(
            ['git', *arguments], cwd=repo, check=True, capture_output=True, text=True,
            timeout=60,
        ).stdout.strip()

    git('init')
    git('config', 'user.name', 'Upgrade Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    git('add', '.')
    git('commit', '-m', 'Old isolated application')
    previous = git('rev-parse', 'HEAD')
    (repo / 'upgrade_version.txt').write_text('new isolated application')
    git('add', 'upgrade_version.txt')
    git('commit', '-m', 'New isolated application')
    target = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', target)
    git('checkout', '--detach', previous)
    started = time.monotonic()
    state = upgrade.prepare(repo, shell_rebuild=True)
    print(f'Code-only recovery preparation: {time.monotonic() - started:.2f} seconds')
    point = upgrade._point(repo, state)
    assert state['runtime_mode'] == 'rebuild-on-recovery'
    assert not (point / 'runtime' / 'runtime-version.txt').exists()
    assert not list((point / 'runtime').rglob('flask'))
    original_interpreter = upgrade._python(repo / 'venv').stat().st_ino
    applied = upgrade.apply_update(repo)
    assert applied['phase'] == 'starting' and git('rev-parse', 'HEAD') == target
    (repo / 'electron-dist' / 'Sales Buddy.exe').write_bytes(b'failed new isolated test shell')
    (repo / 'app' / 'db_paths.py').write_text('This is deliberately invalid Python.')
    (repo / 'scripts' / 'upgrade.py').write_text('This is deliberately invalid Python too.')
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE post_snapshot_change (id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO post_snapshot_change VALUES (71)')

    recovery_code = """
import json
import runpy
import sys
from pathlib import Path
namespace = runpy.run_path(sys.argv[1])
scope = namespace['recover_update'].__globals__
def notice(repo):
    state = scope['load_state'](repo)
    assert state['phase'] == 'restored' and state['notification_pending']
    scope['dismiss_recovery'](repo)
    return True
scope['recovery_notice'] = notice
scope['launch_workspace'] = lambda repo: None
assert namespace['recover_update'](Path(sys.argv[2]))
"""
    recovered = subprocess.run(
        [str(upgrade._python(point / 'runtime')), '-c', recovery_code,
         str(point / 'recover.py'), str(repo)],
        cwd=repo, env={
            **{key: value for key, value in os.environ.items()
               if key != 'PYTHONDONTWRITEBYTECODE'},
            'SALESBUDDY_TELEMETRY_OPT_OUT': 'true',
        },
        capture_output=True, text=True, timeout=240,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert upgrade._tree_digest(point / 'runtime', ignore_cache=True) == state['runtime_sha256']
    assert git('rev-parse', 'HEAD') == previous
    assert (repo / 'venv' / 'runtime-version.txt').read_text() == 'old runtime'
    assert upgrade._python(repo / 'venv').stat().st_ino == original_interpreter
    assert not (repo / f'venv.failed-{state["attempt_id"]}').exists()
    assert (repo / 'electron-dist' / 'Sales Buddy.exe').read_bytes() == b'old isolated test shell'
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='post_snapshot_change'",
        ).fetchone()
    with closing(sqlite3.connect(point / 'failed-state' / 'database.db')) as connection:
        assert connection.execute('SELECT id FROM post_snapshot_change').fetchall() == [(71,)]
    assert 'deliberately invalid Python' in (
        point / 'failed-state' / 'tracked-changes.patch'
    ).read_text()
    assert upgrade.load_state(repo)['phase'] == 'restored'
    assert upgrade.load_state(repo)['notification_pending'] is False
    assert (database.parent / 'diagnostic-outbox' / f'{state["attempt_id"]}-report.json').is_file()


def _failed_update(fixture) -> dict:
    """Leave a changed isolated installation with a genuine recorded migration failure."""
    upgrade.prepare(fixture.repo)
    fixture.control['fail'] = 'migrating'
    with pytest.raises(upgrade.RehearsalError):
        upgrade.apply_update(fixture.repo)
    return upgrade.load_state(fixture.repo)


def _dismiss_notice(repo: Path) -> bool:
    """Model dismissal after restoration, without export or network delivery."""
    upgrade.dismiss_recovery(repo)
    return True


def test_automatic_recovery_restores_before_notice_and_launches_only_after_acknowledgment(
    installation, monkeypatch,
):
    """The recovered workspace opens after dismissal without requiring a report download."""
    fixture = installation
    failure = _failed_update(fixture)
    launches = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: launches.append(args))

    def notice(repo):
        """Inspect the actual restored components before authorizing a return to work."""
        state = upgrade.load_state(repo)
        assert state['phase'] == 'restored'
        assert state['notification_pending'] is True
        assert state['original_failure']['error'] == failure['error']
        assert fixture.control['revision'] == OLD
        assert (repo / 'venv' / 'package.txt').read_text() == 'old dependencies'
        assert (repo / 'electron-dist' / 'Sales Buddy.exe').read_bytes() == b'old shell'
        with closing(sqlite3.connect(fixture.database)) as connection:
            assert connection.execute('PRAGMA user_version').fetchone() == (1,)
        assert not launches
        return _dismiss_notice(repo)

    monkeypatch.setattr(upgrade, 'recovery_notice', notice)
    assert upgrade.recover_update(fixture.repo) is True
    state = upgrade.load_state(fixture.repo)
    assert state['notification_pending'] is False
    assert state['acknowledged_at'] and state['report_queued_at']
    assert not state.get('report_saved_at')
    assert len(launches) == 1


def test_interrupted_notice_survives_restart_without_repeating_restore(installation, monkeypatch):
    """A killed notice cannot restore twice, but a later successful dismissal releases it."""
    fixture = installation
    _failed_update(fixture)
    launches = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: launches.append(args))
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    assert upgrade.recover_update(fixture.repo) is False
    assert upgrade.load_state(fixture.repo)['notification_pending'] is True
    with pytest.raises(RuntimeError, match='recovery notice'):
        upgrade.startup_preflight(fixture.repo)
    with pytest.raises(RuntimeError, match='Acknowledge'):
        upgrade.prepare(fixture.repo)
    monkeypatch.setattr(upgrade, 'restore', lambda *args, **kwargs: pytest.fail('Restored twice'))
    monkeypatch.setattr(upgrade, 'recovery_notice', _dismiss_notice)
    assert upgrade.recover_update(fixture.repo) is True
    assert len(launches) == 1


def test_dismissal_needs_neither_export_nor_delivery(installation, monkeypatch):
    """A restored app is usable even if diagnostics cannot be uploaded."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    upgrade.recover_update(fixture.repo)
    state = upgrade.load_state(fixture.repo)
    upgrade.dismiss_recovery(fixture.repo)
    assert upgrade.load_state(fixture.repo)['notification_pending'] is False
    item = upgrade.read_json(upgrade._report_directory(fixture.repo) /
                             f'{state["attempt_id"]}-report.json')
    assert item['status'] == 'queued'
    assert not (fixture.repo / 'exported-failure.json').exists()


def test_failed_automatic_restore_never_opens_the_workspace(installation, monkeypatch):
    """A damaged snapshot retains the failure and offers export without claiming recovery."""
    fixture = installation
    _failed_update(fixture)
    state = upgrade.load_state(fixture.repo)
    (upgrade._point(fixture.repo, state) / 'database.db').write_bytes(b'corrupt snapshot')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Launched'))

    def notice(repo):
        """Recovery failure keeps both the original update cause and recovery error."""
        current = upgrade.load_state(repo)
        assert current['phase'] == 'failed'
        assert current['failed_phase'] == 'restoring'
        assert current['notification_pending'] is True
        assert current['original_failure']['failed_phase'] == 'migrating'
        return False

    monkeypatch.setattr(upgrade, 'recovery_notice', notice)
    assert upgrade.recover_update(fixture.repo) is False
    assert fixture.control['revision'] == NEW


def test_failure_report_reconstructs_only_safe_original_diagnostics(installation, monkeypatch):
    """Reports identify the failing release and code locations but never contain private fields."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    upgrade.recover_update(fixture.repo)
    state = upgrade.load_state(fixture.repo)
    secret = 'PRIVATE_CUSTOMER_AND_TOKEN_123'
    state['original_failure']['error'].update(
        message=secret, sql=secret, frames=[
            {'file': secret, 'line': 12},
            {'file': 'upgrade_rehearsal.py', 'line': 43, 'function': secret},
        ], parent_tables=['notes', secret],
    )
    state.update(repo=secret, database=secret, logs=secret)
    report = upgrade.failure_report(fixture.repo, state)
    assert secret not in json.dumps(report)
    assert report['previous_commit'] == OLD
    assert report['target_commit'] == NEW
    assert report['upgrade']['phase'] == 'migrating'
    assert report['upgrade']['error']['frames'] == [{'file': 'upgrade_rehearsal.py', 'line': 43}]
    assert report['upgrade']['error']['parent_tables'] == ['notes', 'other']


def test_automatic_recovery_refuses_unchanged_and_already_acknowledged_installs(
    installation, monkeypatch,
):
    """Neither a pristine preparation nor a completed recovery can trigger destructive replay."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    with pytest.raises(RuntimeError, match='No changed upgrade'):
        upgrade.recover_update(fixture.repo)
    fixture.control['fail'] = 'migrating'
    with pytest.raises(upgrade.RehearsalError):
        upgrade.apply_update(fixture.repo)
    monkeypatch.setattr(upgrade, 'recovery_notice', _dismiss_notice)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: None)
    upgrade.recover_update(fixture.repo)
    monkeypatch.setattr(upgrade, 'launch_workspace', lambda _: pytest.fail('Launched twice'))
    assert upgrade.recover_update(fixture.repo) is True


def test_notice_success_without_persisted_acknowledgment_cannot_launch(installation, monkeypatch):
    """An IPC/UI success-shaped return cannot bypass the durable acknowledgment state."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: True)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Launched'))
    with pytest.raises(RuntimeError, match='requires dismissing'):
        upgrade.recover_update(fixture.repo)


def test_recovery_handoff_does_not_import_the_broken_installed_dependencies(
    installation, monkeypatch,
):
    """The bootstrap can reach the frozen interpreter even when installed dotenv cannot import."""
    fixture = installation
    _failed_update(fixture)
    state = upgrade.load_state(fixture.repo)
    point = upgrade._point(fixture.repo, state)
    monkeypatch.setattr(upgrade, '_db_module',
                        lambda _: pytest.fail('Imported installed dependencies'))
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(args[0]))
    upgrade.launch_recovery(fixture.repo, parent_pid=12345)
    assert calls == [[
        'cmd.exe', '/d', '/c', 'start', '', '/min', 'cmd.exe', '/d', '/c',
        str(point / 'launch-recovery.cmd'),
    ]]
    command = (point / 'launch-recovery.cmd').read_text()
    assert str(upgrade._python(point / 'runtime')) in command
    assert 'recover --repo' in command
    assert '--parent-pid 12345' in command
    assert 'restore --confirmed' not in command
    assert 'if %errorlevel% equ 2 exit /b 0' in command


def test_duplicate_recovery_command_joins_the_existing_notice(installation, monkeypatch):
    """Installer and shell failure detection cannot produce concurrent restore screens."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.setattr(sys, 'argv', ['upgrade.py', 'recover', '--repo', str(fixture.repo)])
    monkeypatch.setattr(upgrade, '_recover_update',
                        lambda _: pytest.fail('A second recovery started'))
    with upgrade.upgrade_lock(fixture.repo, name='recovery-notice.lock'):
        assert upgrade.main() == 0


def test_accepted_updates_cannot_be_recovered_by_an_unrelated_startup_failure(installation):
    """A later startup problem must not silently rewind a previously accepted database."""
    fixture = installation
    upgrade.prepare(fixture.repo)
    state = upgrade.apply_update(fixture.repo)
    upgrade.save_state(fixture.repo, state, 'ready')
    with pytest.raises(RuntimeError, match='accepted update'):
        upgrade.recover_update(fixture.repo)
    assert fixture.control['revision'] == NEW
    assert not upgrade.load_state(fixture.repo).get('notification_pending')


@pytest.mark.skipif(os.name != 'nt', reason='The recovery notice uses Windows Forms')
@pytest.mark.parametrize('outcome', ['cancel', 'continue', 'contact', 'failed_restore'])
def test_real_offline_notice_allows_dismissal_without_manual_export(
    installation, monkeypatch, outcome,
):
    """Execute the native dark form: export is optional and only failed restoration is blocked."""
    fixture = installation
    _failed_update(fixture)
    real_notice = upgrade.recovery_notice
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    upgrade.recover_update(fixture.repo)
    state = upgrade.load_state(fixture.repo)
    if outcome == 'failed_restore':
        upgrade.save_state(fixture.repo, state, 'failed', failed_phase='restoring')
    monkeypatch.setattr(upgrade, 'recovery_notice', real_notice)
    monkeypatch.setattr(upgrade, 'reporting_enabled', lambda: True)
    if outcome == 'contact':
        monkeypatch.setattr(upgrade, 'recovery_contact_email', lambda: 'test.contact@example.com')
    real_run = subprocess.run
    destination = fixture.repo / 'native-export.json'

    def automate_form(argv, **kwargs):
        """Exercise real click handlers without an interactive Save dialog or app launch."""
        script = upgrade.base64.b64decode(argv[-1]).decode('utf-16-le')
        result = 'Cancel'
        fake_dialog = (
            "$dialog = [pscustomobject]@{ Title=''; Filter=''; FileName='' }\n"
            "$dialog | Add-Member ScriptMethod ShowDialog { param($owner) "
            f"$this.FileName=$env:NOTICE_EXPORT; return [Windows.Forms.DialogResult]::{result}"
            " }\n$dialog | Add-Member ScriptMethod Dispose { }"
        )
        script = script.replace('$dialog = New-Object System.Windows.Forms.SaveFileDialog',
                                fake_dialog)
        automate = r"""
$form.Show()
[Windows.Forms.Application]::DoEvents()
if ($save.ForeColor.R -lt 200) { throw 'Save label is not readable' }
if ($contact.Text -notmatch 'Microsoft email') { throw 'Contact wording is incorrect' }
if ($env:NOTICE_OUTCOME -eq 'contact') {
    if ($email.Text -ne 'Microsoft email: test.contact@example.com') { throw 'Email not displayed' }
} elseif ($env:NOTICE_OUTCOME -ne 'failed_restore') {
    if ($email.Text -notmatch 'Microsoft email unavailable') { throw 'Missing email not explained' }
}
$save.PerformClick()
if ($env:NOTICE_OUTCOME -eq 'failed_restore') {
    if ($continue.Visible -or $contact.Visible) { throw 'Failed restore exposed Continue or contact' }
} else {
    if (-not $continue.Enabled) { throw 'Canceled export blocked Continue' }
    if ($env:NOTICE_OUTCOME -eq 'contact') { $contact.Checked = $true }
    if ($env:NOTICE_OUTCOME -ne 'cancel') { $continue.PerformClick() }
}
$form.Close()
"""
        script = script.replace('$form.ShowDialog() | Out-Null', automate)
        encoded = upgrade.base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        assert len(encoded) < 30000
        completed = real_run(
            [*argv[:-1], encoded], capture_output=True, text=True, timeout=30,
            env={**os.environ, 'NOTICE_EXPORT': str(destination), 'NOTICE_OUTCOME': outcome},
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return completed

    monkeypatch.setattr(subprocess, 'run', automate_form)
    continued = outcome != 'failed_restore'
    assert upgrade.recovery_notice(fixture.repo) is continued
    assert upgrade.load_state(fixture.repo)['notification_pending'] is not continued
    assert not destination.exists()
    assert (upgrade._report_directory(fixture.repo) /
            f'{state["attempt_id"]}-followup.json').exists() is (outcome == 'contact')
    assert not (upgrade._point(fixture.repo, state) / 'notice-settings.json').exists()


def test_initial_report_is_immutable_and_queued_before_any_restore_or_contact(
    installation, monkeypatch,
):
    """Optional contact and restoration cannot delay or replace the original failure."""
    fixture = installation
    failure = _failed_update(fixture)
    path = upgrade._report_directory(fixture.repo) / f'{failure["attempt_id"]}-report.json'
    before = path.read_bytes()
    event = json.loads(before)
    assert event['payload']['report']['recovery'] == 'incomplete'
    assert not any(key in json.dumps(event) for key in ('Private fixture', 'access_token'))
    calls = []
    monkeypatch.setattr(upgrade, 'start_report_delivery', lambda *_: calls.append('send'))
    real_restore = upgrade.restore

    def restore(repo, **kwargs):
        """Observe the ordering at the exact filesystem mutation boundary."""
        assert calls == ['send']
        assert path.read_bytes() == before
        calls.append('restore')
        real_restore(repo, **kwargs)

    monkeypatch.setattr(upgrade, 'restore', restore)
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    assert upgrade.recover_update(fixture.repo) is False
    assert calls == ['send', 'restore']
    upgrade.queue_failure_report(fixture.repo, upgrade.load_state(fixture.repo))
    assert path.read_bytes() == before


def test_followup_is_independent_opt_in_without_a_client_email(installation, monkeypatch):
    """A second event references the original ID, leaving server-side identity to APIM."""
    fixture = installation
    failure = _failed_update(fixture)
    monkeypatch.setattr(upgrade, 'recovery_notice', lambda _: False)
    monkeypatch.setattr(upgrade, 'reporting_enabled', lambda: True)
    upgrade.recover_update(fixture.repo)
    path = upgrade._report_directory(fixture.repo) / f'{failure["attempt_id"]}-report.json'
    original = path.read_bytes()
    upgrade.dismiss_recovery(fixture.repo, contact=True)
    state = upgrade.load_state(fixture.repo)
    followup = upgrade.read_json(
        upgrade._report_directory(fixture.repo) / f'{failure["attempt_id"]}-followup.json')
    assert followup['payload'] == {'schema_version': 1, 'report_id': failure['attempt_id']}
    assert path.read_bytes() == original
    assert state['contact_requested'] is True
    assert state['notification_pending'] is False


@pytest.mark.parametrize('receipt', [
    'accepted', 'wrong-id', 'missing-status', 'offline', 'no-auth', 'missing-contact',
    'quota', 'server-error', 'bad-shape', 'wrong-status',
])
def test_report_delivery_requires_real_matching_receipt_and_retries_independently(
    installation, monkeypatch, receipt,
):
    """Only durable server acceptance means sent; auth/network errors leave a bounded retry."""
    fixture = installation
    failure = _failed_update(fixture)
    monkeypatch.setattr(upgrade, 'reporting_enabled', lambda: True)
    calls = []

    def token():
        """No credential acquisition or actual networking is permitted in this fixture."""
        if receipt == 'no-auth':
            raise RuntimeError('Not signed in')
        return 'test-token'

    def post(request, timeout):
        """Return the exact deployed service receipt contract."""
        calls.append((request, timeout))
        if receipt == 'offline':
            raise OSError('offline')
        if receipt in ('missing-contact', 'quota', 'server-error'):
            code = {'missing-contact': 422, 'quota': 429, 'server-error': 503}[receipt]
            raise upgrade.urllib.error.HTTPError(
                request.full_url, code, 'test', {'Retry-After': '3600'}, io.BytesIO(b'{}'))
        result = {'report_id': failure['attempt_id'], 'status': 'accepted'}
        if receipt == 'wrong-id':
            result['report_id'] = 'f' * 32
        if receipt == 'missing-status':
            result.pop('status')
        if receipt == 'bad-shape':
            result = []
        response = io.BytesIO(json.dumps(result).encode())
        response.status = 200 if receipt == 'wrong-status' else 202
        return response

    monkeypatch.setattr(upgrade, '_report_token', token)
    monkeypatch.setattr(upgrade.urllib.request, 'urlopen', post)
    upgrade.send_pending_reports(fixture.repo)
    path = upgrade._report_directory(fixture.repo) / f'{failure["attempt_id"]}-report.json'
    item = upgrade.read_json(path)
    expected = ('sent' if receipt == 'accepted'
                else 'rejected' if receipt == 'missing-contact' else 'queued')
    assert item['status'] == expected
    assert len(calls) == (0 if receipt == 'no-auth' else 1)
    if calls:
        assert calls[0][0].full_url == upgrade.DIAGNOSTICS_URL + '/reports'
        assert calls[0][1] == 8
    if receipt != 'accepted':
        assert item['attempts'] == 1
        assert item['retry_at'] > time.time()
        if receipt in ('quota', 'server-error'):
            assert item['retry_at'] > time.time() + 3500
    upgrade.send_pending_reports(fixture.repo)
    assert len(calls) == (0 if receipt == 'no-auth' else 1)


def test_reporting_privacy_optout_does_not_prevent_restoration_or_dismissal(
    installation, monkeypatch,
):
    """Disabled telemetry means no automatic network call or follow-up opt-in requirement."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.setenv('SALESBUDDY_TELEMETRY_OPT_OUT', 'true')
    monkeypatch.setattr(upgrade, '_report_token', lambda: pytest.fail('Acquired credentials'))
    monkeypatch.setattr(upgrade, 'recovery_notice', _dismiss_notice)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_args, **_kwargs: None)
    upgrade.send_pending_reports(fixture.repo)
    assert upgrade.recover_update(fixture.repo) is True
    assert upgrade.load_state(fixture.repo)['notification_pending'] is False


def test_diagnostic_write_failure_does_not_prevent_matched_restore(installation, monkeypatch):
    """Reporting is not another prerequisite capable of breaking the rollback itself."""
    fixture = installation
    _failed_update(fixture)

    def fail(*_args):
        """Model a separate diagnostic-directory permission error."""
        raise PermissionError('Cannot save diagnostics')

    monkeypatch.setattr(upgrade, 'queue_failure_report', fail)
    monkeypatch.setattr(upgrade, 'recovery_notice', _dismiss_notice)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_args, **_kwargs: None)
    assert upgrade.recover_update(fixture.repo) is True
    assert fixture.control['revision'] == OLD


def test_frozen_report_sender_cannot_modify_verified_runtime(installation, monkeypatch):
    """Background imports must not add bytecode to the frozen snapshot during verification."""
    fixture = installation
    failure = _failed_update(fixture)
    calls = []
    monkeypatch.setattr(upgrade, 'reporting_enabled', lambda: True)
    monkeypatch.setattr(upgrade, '_point', lambda *_: fixture.database.parent / 'point')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    fixture.real_sender(fixture.repo, failure)
    assert calls[0][1]['env']['PYTHONDONTWRITEBYTECODE'] == '1'
    assert 'send-reports' in calls[0][0][0]
    if os.name == 'nt':
        assert calls[0][0][0][:5] == ['cmd.exe', '/c', 'start', '', '/min']


def test_sender_loads_installed_dotenv_before_checking_privacy(installation, monkeypatch):
    """A frozen standalone sender honors opt-out even before the app has bootstrapped dotenv."""
    fixture = installation
    _failed_update(fixture)
    monkeypatch.delenv('SALESBUDDY_TELEMETRY_OPT_OUT', raising=False)
    (fixture.repo / '.env').write_text('SALESBUDDY_TELEMETRY_OPT_OUT=true\n')

    def load(repo):
        """Use the real dotenv/bootstrap loader with isolated database resolution."""
        module = fixture.real_db_module(repo)
        module.resolve_db_path = lambda: fixture.database
        return module

    monkeypatch.setattr(upgrade, '_db_module', load)
    monkeypatch.setattr(upgrade, '_report_token', lambda: pytest.fail('Opt-out ignored'))
    upgrade.send_pending_reports(fixture.repo)
    assert upgrade.reporting_enabled() is False


@pytest.mark.parametrize('corporate', [True, False])
def test_reporting_uses_existing_isolated_noninteractive_corporate_signin(
    installation, monkeypatch, corporate,
):
    """Do not acquire default-shell credentials or open an interactive sign-in during recovery."""
    monkeypatch.delenv('AZURE_CONFIG_DIR', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    calls = []
    claims = {'tid': upgrade.CORPORATE_TENANT if corporate else 'other-tenant'}
    encoded = upgrade.base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    token = f'test.{encoded}.signature'
    monkeypatch.setattr(upgrade.shutil, 'which', lambda _: 'az')

    def run(argv, **kwargs):
        """Observe the CLI contract without accessing credentials or network."""
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=token)

    monkeypatch.setattr(subprocess, 'run', run)
    if corporate:
        assert upgrade._report_token() == token
    else:
        with pytest.raises(RuntimeError, match='corporate sign-in'):
            upgrade._report_token()
    argv, kwargs = calls[0]
    assert argv[1:3] == ['account', 'get-access-token']
    assert argv[argv.index('--tenant') + 1] == upgrade.CORPORATE_TENANT
    assert kwargs['timeout'] == 12
    assert kwargs['env']['AZURE_CONFIG_DIR'] == str(Path.home() / 'SalesBuddy' / '.azure')


@pytest.mark.parametrize('mode', ['enabled', 'disabled', 'delivery-error'])
def test_existing_telemetry_loop_retries_outbox_without_blocking_other_telemetry(
    installation, monkeypatch, mode,
):
    """Startup reuses the existing daemon for durable retry, including failure isolation."""
    from app.services import telemetry_shipper

    class LoopFinished(Exception):
        """Stop the modeled daemon after exactly one iteration."""

    fixture = installation
    calls = []
    targets = []
    sleeps = []

    def sleep(_seconds):
        """Advance once without real waits or actual background threads."""
        if sleeps:
            raise LoopFinished
        sleeps.append(True)

    def deliver(repo):
        """Model either a retry or an explicitly surfaced storage error."""
        assert repo == fixture.repo
        calls.append('diagnostics')
        if mode == 'delivery-error':
            raise PermissionError('outbox unavailable')

    def thread(*, target, **kwargs):
        """Capture the existing worker contract without launching anything."""
        assert kwargs['daemon'] is True
        targets.append(target)
        return SimpleNamespace(start=lambda: None, is_alive=lambda: True)

    monkeypatch.setattr(telemetry_shipper, '_flush_thread', None)
    monkeypatch.setattr(telemetry_shipper, 'is_telemetry_enabled', lambda: mode != 'disabled')
    monkeypatch.setattr(telemetry_shipper, 'get_instance_id', lambda: 'test')
    monkeypatch.setattr(telemetry_shipper, 'queue_install_profile', lambda _: None)
    monkeypatch.setattr(telemetry_shipper, 'flush_buffer', lambda: calls.append('telemetry'))
    monkeypatch.setattr(telemetry_shipper.threading, 'Thread', thread)
    monkeypatch.setattr(telemetry_shipper.time, 'sleep', sleep)
    monkeypatch.setattr(upgrade, 'send_pending_reports', deliver)
    telemetry_shipper.start_flush_thread(SimpleNamespace(
        root_path=str(fixture.repo / 'app'), config={},
    ))
    if mode == 'disabled':
        assert targets == []
        return
    with pytest.raises(LoopFinished):
        targets[0]()
    assert calls == ['diagnostics', 'telemetry']


@pytest.mark.parametrize(('claims', 'expected'), [
    ({'preferred_username': 'preferred@example.com', 'upn': 'other@example.com'},
     'preferred@example.com'),
    ({'preferred_username': 'not-an-email', 'upn': 'upn@example.com'}, 'upn@example.com'),
    ({'email': 'guest@other.example'}, 'guest@other.example'),
    ({'unique_name': 'unique@example.com'}, 'unique@example.com'),
    ({'email': 'bad@example.com\r\ninjected'}, None),
    ({'email': 'x' * 250 + '@example.com'}, None),
    ({}, None),
])
def test_recovery_email_display_matches_apim_contact_claim_selection(
    monkeypatch, claims, expected,
):
    """Display only the same usable address claims accepted by the authenticated backend."""
    encoded = upgrade.base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    monkeypatch.setattr(upgrade, '_report_token', lambda: f'test.{encoded}.signature')
    assert upgrade.recovery_contact_email() == expected


def test_recovery_email_unavailable_does_not_require_authentication(monkeypatch):
    """Signed-out and offline machines can still dismiss recovery without a contact address."""
    def token():
        """Model unavailable noninteractive corporate authentication."""
        raise RuntimeError('Not signed in')

    monkeypatch.setattr(upgrade, '_report_token', token)
    assert upgrade.recovery_contact_email() is None
