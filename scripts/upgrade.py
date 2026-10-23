"""Offline upgrade coordinator, automatic recovery and durable diagnostic delivery.

Keep this module independent of Flask so a copied recovery runner still works
when the installed application's dependencies or migrations cannot load.
"""
from __future__ import annotations

import argparse
import ast
import base64
import errno
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import urllib.error
import uuid
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
COMMAND_TIMEOUT = 900
PENDING_PHASES = {
    'preparing', 'prepared', 'code', 'dependencies', 'rehearsing', 'migrating',
    'starting', 'failed', 'restoring',
}
CORE_TABLES = ('customers', 'notes', 'milestones', 'msx_tasks', 'engagements')
SAFE_TABLES = set(CORE_TABLES) | {
    '_old_call_logs', 'call_logs_backup', '_old_milestones',
    'manager_initiative_items', 'initiative_discussed_points',
}
DIAGNOSTICS_URL = 'https://apim-notehelper.azure-api.net/diagnostics'
CORPORATE_TENANT = '72f988bf-86f1-41af-91ab-2d7cd011db47'
REPORT_RETRY_SECONDS = 300


class RehearsalError(RuntimeError):
    """Carry only the sanitized failure produced by the isolated migration process."""

    def __init__(self, details: dict) -> None:
        super().__init__('Database upgrade rehearsal failed')
        self.upgrade_diagnostic = details


class UpgradeBusyError(RuntimeError):
    """Identify an already-owned operation without confusing it with an I/O failure."""

    def __init__(self, name: str) -> None:
        super().__init__('Another upgrade operation is still running')
        self.lock_name = name


def utc_timestamp() -> str:
    """Return an explicit UTC timestamp for durable upgrade state."""
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: dict) -> None:
    """Publish complete state or fail explicitly without replacing the last good state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def startup_path(repo: Path) -> Path:
    """Return the shell-readable status path, independent of the database."""
    return repo / 'data' / 'startup-state.json'


def read_json(path: Path) -> dict | None:
    """Read bounded durable state; only a genuinely absent file means no state."""
    if not path.exists():
        return None
    if path.stat().st_size > 262144:
        raise RuntimeError('Upgrade state exceeds its size limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Upgrade state must be an object')
    return value


@contextmanager
def upgrade_lock(
    repo: Path, *, name: str = 'upgrade.lock', timeout: float = 5,
) -> Generator[None, None, None]:
    """Serialize offline upgrade commands without leaving a stale lock after a crash."""
    lock = repo / 'data' / name
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise UpgradeBusyError(name) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _assert_quiescent(repo: Path) -> None:
    """Refuse mutation while an installation-scoped Python process still owns files."""
    if os.name != 'nt':
        return
    root = re.escape(str(repo.resolve())).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='python.exe' OR Name='pythonw.exe' OR Name='waitress-serve.exe'\" | "
        f"Where-Object {{ $_.ProcessId -ne {os.getpid()} -and $_.CommandLine -match "
        f"'{root}(?:[\\\\/]|[\"\\s]|$)' -and $_.CommandLine -notmatch "
        "'(?:upgrade(?:-[a-f0-9]{32})?\\.py|recover\\.py|scripts\\.upgrade)' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    if _run(repo, ['powershell', '-NoProfile', '-NonInteractive', '-Command', command]):
        raise RuntimeError('Application processes are still running; offline upgrade stopped')


def diagnostic_error(error: BaseException) -> dict:
    """Describe code failures without exception messages, row IDs or record contents."""
    result = {'type': type(error).__name__ if re.fullmatch(
        r'[A-Za-z_]{1,64}', type(error).__name__) else 'Error',
        'category': 'operation_failed', 'frames': []}
    seen = set()
    current = error
    while current and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        message = str(current).lower()
        if isinstance(current, subprocess.CalledProcessError) and (
            'ModuleNotFoundError' in (current.stderr or '')
        ):
            result.update(type='ModuleNotFoundError', category='dependency_missing')
        for text, category in (
            ('foreign key', 'migration_foreign_keys'), ('database is locked', 'database_locked'),
            ('backup', 'backup_failed'), ('space', 'disk_space'), ('permission', 'permission'),
            ('timed out', 'timeout'), ('rehearsal', 'rehearsal_failed'),
        ):
            if text in message:
                result['category'] = category
                break
        if isinstance(current, subprocess.CalledProcessError):
            result['exit_code'] = current.returncode
        if 'foreign key violations:' in str(current):
            try:
                violations = ast.literal_eval(str(current).split('violations:', 1)[1].strip())
                if isinstance(violations, list):
                    result['violation_count'] = len(violations)
                    result['parent_tables'] = sorted({
                        row[2] if row[2] in SAFE_TABLES else 'other'
                        for row in violations if isinstance(row, tuple) and len(row) == 4
                    })
            except (SyntaxError, ValueError):
                logger.warning('Could not parse foreign-key diagnostic metadata')
        for frame in traceback.extract_tb(current.__traceback__):
            filename = Path(frame.filename).name
            if (filename.endswith('.py') and re.fullmatch(r'[\w.-]{1,80}', filename)
                    and re.fullmatch(r'[\w<>]{1,80}', frame.name)):
                result['frames'].append(
                    {'file': filename, 'function': frame.name, 'line': frame.lineno})
        current = current.__cause__ or current.__context__
    result['frames'] = result['frames'][-16:]
    return result


def _run(repo: Path, argv: list[str], **kwargs) -> str:
    """Run one bounded checked command; never continue on an unsuccessful step."""
    completed = subprocess.run(
        argv, cwd=repo, check=True, capture_output=True, text=True,
        timeout=COMMAND_TIMEOUT, **kwargs,
    )
    return completed.stdout.strip()


def commit(repo: Path) -> str:
    """Read and validate the complete checked-out revision."""
    value = _run(repo, ['git', 'rev-parse', 'HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40,64}', value):
        raise RuntimeError('Could not identify the installed revision')
    return value


def _db_module(repo: Path):
    """Load the existing path/backup implementation without importing the Flask app."""
    from dotenv import load_dotenv

    load_dotenv(repo / '.env')
    frozen = Path(__file__).with_name('db_paths.py')
    source = frozen if Path(__file__).name == 'recover.py' else repo / 'app' / 'db_paths.py'
    spec = importlib.util.spec_from_file_location('_upgrade_db_paths', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('Database backup implementation is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._repo_root = lambda: repo
    return module


def database_path(repo: Path) -> Path:
    """Resolve the actual configured database using the shared application resolver."""
    return _db_module(repo).resolve_db_path().resolve()


def _digest(path: Path) -> str:
    """Hash a restore artifact without loading a large database into memory."""
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _tree_digest(directory: Path) -> str:
    """Verify snapshot file names and bytes, including empty-file boundaries."""
    result = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Recovery snapshots cannot contain symbolic links')
        if path.is_file():
            result.update(str(path.relative_to(directory)).encode('utf-8'))
            result.update(b'\0')
            result.update(_digest(path).encode('ascii'))
    return result.hexdigest()


def _point(repo: Path, state: dict) -> Path:
    """Resolve only this installation's named recovery directory."""
    attempt = state.get('attempt_id', '')
    if not re.fullmatch(r'[0-9a-f]{32}', attempt):
        raise ValueError('Invalid recovery attempt identifier')
    if Path(state.get('repo', '')).resolve() != repo.resolve():
        raise ValueError('Recovery state belongs to another installation')
    database = database_path(repo)
    if Path(state.get('database', '')).resolve() != database:
        raise ValueError('Recovery state belongs to another database')
    return database.parent / 'upgrade-recovery' / attempt


def load_state(repo: Path) -> dict | None:
    """Read an installation-fenced recovery manifest."""
    state = read_json(repo / 'data' / 'upgrade-state.json')
    if state:
        point = _point(repo, state)
        saved = read_json(point / 'manifest.json')
        if not saved or saved.get('attempt_id') != state['attempt_id']:
            raise RuntimeError('Recovery manifest is missing or inconsistent')
        if _point(repo, saved) != point or saved.get('schema_version') != 1:
            raise RuntimeError('Recovery manifest failed installation validation')
        return saved
    return None


def save_state(repo: Path, state: dict, phase: str, **fields) -> None:
    """Persist the recovery record outside the install before publishing its shell mirror."""
    state.update(fields, phase=phase, updated_at=utc_timestamp())
    if phase == 'failed':
        state['ever_failed'] = True
    atomic_json(_point(repo, state) / 'manifest.json', state)
    atomic_json(repo / 'data' / 'upgrade-state.json', state)


def _snapshot_database(repo: Path, source: Path, target: Path) -> None:
    """Require the existing WAL-safe, integrity-checked snapshot implementation."""
    if not _db_module(repo).backup_database(target, src=source):
        raise RuntimeError('Verified database backup failed')


def _python(directory: Path) -> Path:
    """Find the interpreter belonging to one frozen runtime."""
    return directory / ('Scripts' if os.name == 'nt' else 'bin') / (
        'python.exe' if os.name == 'nt' else 'python')


def _discard_incomplete_snapshot(point: Path) -> None:
    """Remove only owned partial artifacts when application mutation never began."""
    for name in ('runtime', 'shell'):
        path = point / name
        if path.exists():
            if path.is_symlink():
                raise RuntimeError('An incomplete snapshot contains an unexpected symbolic link')
            shutil.rmtree(path)
    for name in ('application.zip', 'database.db', 'database.db.partial',
                 'recover.py', 'db_paths.py'):
        (point / name).unlink(missing_ok=True)


def prepare(repo: Path, target: str = 'origin/main', *, shell_rebuild: bool = False) -> dict:
    """Freeze code, runtime, desktop shell and DB before modifying an installed version."""
    _assert_quiescent(repo)
    revision = commit(repo)
    if _run(repo, ['git', 'status', '--porcelain', '--untracked-files=no']):
        raise RuntimeError('Tracked application files have local changes; update stopped')
    incoming = _run(repo, ['git', 'rev-parse', '--verify', f'{target}^{{commit}}'])
    if not re.fullmatch(r'[0-9a-f]{40,64}', incoming):
        raise ValueError('Invalid incoming revision')
    _run(repo, ['git', 'merge-base', '--is-ancestor', revision, incoming])
    previous = load_state(repo)
    if previous and previous.get('notification_pending'):
        raise RuntimeError('Acknowledge the failed update before preparing another update')
    if previous and previous.get('phase') in PENDING_PHASES:
        if previous.get('changes_started') and not previous.get('recovery_ready'):
            raise RuntimeError('An unfinished update has no verified recovery point')
        if not previous.get('recovery_ready') and not previous.get('changes_started'):
            _discard_incomplete_snapshot(_point(repo, previous))
    if previous and previous.get('phase') in PENDING_PHASES and previous.get('recovery_ready'):
        if not previous.get('recovery_ready') or revision not in {
            previous['previous_commit'], previous['target_commit'], previous.get('applied_commit'),
        }:
            raise RuntimeError('An unfinished update must be recovered before updating again')
        if previous.get('phase') == 'restoring' or previous.get('failed_phase') == 'restoring':
            raise RuntimeError('Interrupted recovery must finish before another update')
        save_state(repo, previous, 'prepared', target_commit=incoming, error=None,
                   needs_shell_rebuild=shell_rebuild or previous.get('needs_shell_rebuild', False))
        return previous

    database = database_path(repo)
    state = {
        'schema_version': 1, 'attempt_id': uuid.uuid4().hex, 'repo': str(repo.resolve()),
        'database': str(database), 'database_existed': database.exists(),
        'previous_commit': revision, 'target_commit': incoming, 'applied_commit': revision,
        'created_at': utc_timestamp(), 'recovery_ready': False, 'changes_started': False,
        'needs_shell_rebuild': shell_rebuild,
    }
    point = _point(repo, state)
    point.mkdir(parents=True, mode=0o700)
    save_state(repo, state, 'preparing')
    try:
        runtime = repo / 'venv'
        shell = repo / 'electron-dist'
        required = sum(path.stat().st_size for folder in (runtime, shell)
                       if folder.exists() for path in folder.rglob('*') if path.is_file())
        if shutil.disk_usage(repo).free < required + 64 * 1024 * 1024:
            raise RuntimeError('Not enough disk space to stage a recovery runtime')
        required *= 2
        required += (database.stat().st_size * 3 if database.exists() else 0) + 64 * 1024 * 1024
        if shutil.disk_usage(point).free < required:
            raise RuntimeError('Not enough free disk space for a complete recovery snapshot')
        if not _python(runtime).exists():
            raise RuntimeError('The installed Python runtime cannot be backed up')
        _run(repo, ['git', 'archive', '--format=zip', f'--output={point / "application.zip"}',
                    revision])
        state['code_sha256'] = _digest(point / 'application.zip')
        shutil.copytree(runtime, point / 'runtime')
        _run(repo, [str(_python(point / 'runtime')), '-c', 'import flask, sqlalchemy, dotenv'],
             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
        state['runtime_sha256'] = _tree_digest(point / 'runtime')
        state['shell_existed'] = shell.exists()
        if shell.exists():
            shutil.copytree(shell, point / 'shell')
            state['shell_sha256'] = _tree_digest(point / 'shell')
        if database.exists():
            _snapshot_database(repo, database, point / 'database.db')
            state['database_sha256'] = _digest(point / 'database.db')
        shutil.copy2(Path(__file__), point / 'recover.py')
        shutil.copy2(repo / 'app' / 'db_paths.py', point / 'db_paths.py')
        state['runner_sha256'] = _digest(point / 'recover.py')
        state['backup_helper_sha256'] = _digest(point / 'db_paths.py')
        save_state(repo, state, 'prepared', recovery_ready=True)
        return state
    except Exception as error:
        save_state(repo, state, 'failed', error=diagnostic_error(error))
        try:
            _discard_incomplete_snapshot(point)
        except (OSError, RuntimeError):
            logger.exception('Could not remove an incomplete recovery snapshot')
        raise


def rehearse_database(repo: Path, source: Path, directory: Path) -> dict:
    """Run the real shared schema initializer twice against a private database snapshot."""
    directory.mkdir(parents=True, exist_ok=True)
    trial = directory / 'rehearsal.db'
    for path in (trial, Path(str(trial) + '-wal'), Path(str(trial) + '-shm')):
        path.unlink(missing_ok=True)
    result_path = directory / 'rehearsal-result.json'
    result_path.unlink(missing_ok=True)
    if source.exists():
        _snapshot_database(repo, source, trial)
    environment = {**os.environ, 'TESTING': 'true', 'SALESBUDDY_SUPERVISED': '1'}
    try:
        _run(repo, [str(_python(repo / 'venv')), '-m', 'app.upgrade_rehearsal',
                    '--database', str(trial), '--result', str(result_path)], env=environment)
    except subprocess.CalledProcessError:
        result = read_json(result_path)
        if result and result.get('error'):
            raise RehearsalError(result['error'])
        raise
    result = read_json(result_path)
    if not result or result.get('success') is not True:
        raise RuntimeError('Database upgrade rehearsal did not produce a successful result')
    return result


def apply_update(repo: Path) -> dict:
    """Apply code/dependencies, rehearse, then migrate live, leaving failures recoverable."""
    _assert_quiescent(repo)
    state = load_state(repo)
    if not state or not state.get('recovery_ready') or state['phase'] not in {
        'prepared', 'failed',
    }:
        raise RuntimeError('A complete recovery snapshot is required before updating')
    if state.get('notification_pending'):
        raise RuntimeError('Acknowledge recovery before applying another update')
    point = _point(repo, state)
    try:
        verify_recovery(repo)
        if commit(repo) not in {
            state['previous_commit'], state['target_commit'], state.get('applied_commit'),
        }:
            raise RuntimeError('Installed code changed outside the pending update')
        save_state(repo, state, 'code', changes_started=True)
        _run(repo, ['git', 'merge', '--ff-only', state['target_commit']])
        save_state(repo, state, 'dependencies', applied_commit=commit(repo))
        requirements_changed = _run(repo, [
            'git', 'diff', '--name-only', state['previous_commit'], state['target_commit'],
            '--', 'requirements.txt',
        ])
        if requirements_changed or state.get('repair_dependencies'):
            _run(repo, [str(_python(repo / 'venv')), '-m', 'pip', 'install',
                        '-r', 'requirements.txt'])
        save_state(repo, state, 'rehearsing')
        result = rehearse_database(repo, database_path(repo), point / 'trial')
        save_state(repo, state, 'migrating', rehearsal=result)
        environment = {**os.environ, 'TESTING': 'true', 'SALESBUDDY_SUPERVISED': '1'}
        result_path = point / 'live-result.json'
        result_path.unlink(missing_ok=True)
        try:
            _run(repo, [str(_python(repo / 'venv')), '-m', 'app.upgrade_rehearsal',
                        '--database', str(database_path(repo)), '--result', str(result_path),
                        '--live'], env=environment)
        except subprocess.CalledProcessError:
            result = read_json(result_path)
            if result and result.get('error'):
                raise RehearsalError(result['error'])
            raise
        live_result = read_json(result_path)
        if not live_result or live_result.get('success') is not True:
            raise RuntimeError('Live migration did not produce a successful result')
        save_state(repo, state, 'starting', error=None)
        return state
    except Exception as error:
        details = getattr(error, 'upgrade_diagnostic', diagnostic_error(error))
        failed_phase = state['phase']
        save_state(repo, state, 'failed', failed_phase=failed_phase, error=details,
                   repair_dependencies=details.get('category') == 'dependency_missing')
        report_update_failure(repo, state)
        raise


def _schema_fingerprint(database: Path) -> str | None:
    """Fingerprint schema only, never business values, to fence a readiness cache."""
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name",
        ).fetchall()
    return hashlib.sha256(json.dumps(schema).encode('utf-8')).hexdigest()


def mark_startup_ready(repo: Path) -> None:
    """Cache only a schema/version pair that really reached web-and-worker readiness."""
    atomic_json(repo / 'data' / 'schema-ready.json', {
        'commit': commit(repo), 'schema': _schema_fingerprint(database_path(repo)),
        'verified_at': utc_timestamp(),
    })


def startup_preflight(repo: Path) -> None:
    """Protect manual pulls and first boots too, without touching the working DB."""
    state = load_state(repo)
    if state and state.get('notification_pending'):
        raise RuntimeError('Dismiss the completed recovery notice before startup')
    if state and state['phase'] in PENDING_PHASES and (
        state.get('changes_started') or state['phase'] not in {'failed'}
    ):
        if state['phase'] == 'starting' and state['target_commit'] == commit(repo):
            return
        raise RuntimeError('An unfinished upgrade needs recovery before startup')
    database = database_path(repo)
    previous = read_json(repo / 'data' / 'schema-ready.json')
    if previous and previous.get('commit') == commit(repo) and previous.get(
        'schema') == _schema_fingerprint(database):
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='upgrade-rehearsal-', dir=database.parent) as folder:
        rehearse_database(repo, database, Path(folder))


def readiness_matches(status: dict, health: dict, boot_id: str, revision: str, *,
                      now: datetime | None = None) -> bool:
    """Match the final web/worker identities and freshness against this exact launch."""
    if not isinstance(status, dict) or not isinstance(health, dict):
        return False
    if not (
        status.get('schema_version') == 1 and status.get('state') == 'ready'
        and status.get('phase') == 'ready' and status.get('boot_id') == boot_id
        and status.get('expected_commit') == revision
        and health.get('boot_id') == boot_id and health.get('commit_full') == revision
        and health.get('status') == 'healthy' and health.get('database') == 'connected'
        and health.get('worker') == 'alive' and health.get('worker_boot_id') == boot_id
        and health.get('worker_ready') is True
    ):
        return False
    children = status.get('children')
    if not isinstance(children, list) or len(children) != 2:
        return False
    for role in ('web', 'worker'):
        matching = [child for child in children if isinstance(child, dict)
                    and child.get('name') == role]
        if len(matching) != 1:
            return False
        child = matching[0]
        pid = child.get('pid')
        if (child.get('ready') is not True or type(pid) is not int or pid <= 0
                or health.get(f'{role}_pid') != pid):
            return False
    try:
        started = datetime.fromisoformat(status['started_at'])
        updated = datetime.fromisoformat(status['updated_at'])
        heartbeat = datetime.fromisoformat(health['worker_heartbeat_at'])
        if any(value.tzinfo is None for value in (started, updated, heartbeat)):
            return False
        current = now or datetime.now(timezone.utc)
        return (
            started <= updated and started <= heartbeat
            and 0 <= (current - updated).total_seconds() <= 30
            and 0 <= (current - heartbeat).total_seconds() <= 90
        )
    except (KeyError, TypeError, ValueError):
        logger.warning('Startup readiness contained invalid timestamp metadata')
        return False


def wait_ready(repo: Path, boot_id: str, port: int, timeout: float = 720) -> dict:
    """Require the new supervisor, web revision and worker identity, not an open TCP port."""
    expected = commit(repo)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = read_json(startup_path(repo))
        if status and status.get('boot_id') == boot_id:
            if status.get('state') == 'failed':
                raise RuntimeError('The backend stopped after bounded startup failures')
            if status.get('state') == 'ready' and status.get('expected_commit') == expected:
                try:
                    with urllib.request.urlopen(
                        f'http://127.0.0.1:{port}/health', timeout=3,
                    ) as response:
                        health = json.load(response)
                except (OSError, ValueError):
                    logger.warning('Waiting for the new backend health response')
                else:
                    if readiness_matches(status, health, boot_id, expected):
                        return status
        time.sleep(0.5)
    raise TimeoutError('Timed out waiting for the new web and worker processes')


def accept(repo: Path, boot_id: str, port: int) -> None:
    """Mark an update complete only after independently proving full readiness."""
    wait_ready(repo, boot_id, port, timeout=10)
    state = load_state(repo)
    if state and state['phase'] == 'starting':
        if state['target_commit'] != commit(repo):
            raise RuntimeError('The ready revision is not the pending upgrade')
        save_state(repo, state, 'ready', accepted_at=utc_timestamp())
        try:
            _prune_successful_points(repo, state)
        except (OSError, ValueError, RuntimeError):
            logger.exception('Recovery retention could not finish; the accepted update remains ready')


def _prune_successful_points(repo: Path, current: dict) -> None:
    """Keep two successful restore points; never prune failed/recovered evidence."""
    root = _point(repo, current).parent
    successful = []
    for point in root.iterdir():
        if not point.is_dir() or point.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', point.name):
            continue
        candidate = read_json(point / 'manifest.json')
        if (candidate and candidate.get('phase') == 'ready' and not candidate.get('ever_failed')
                and _point(repo, candidate) == point):
            successful.append((candidate.get('accepted_at', ''), point))
    for _, point in sorted(successful, reverse=True)[2:]:
        if point != _point(repo, current):
            try:
                shutil.rmtree(point)
            except OSError:
                logger.exception('Could not prune a successful historical recovery point')


def verify_recovery(repo: Path) -> dict:
    """Verify every frozen component and interpreter before offering or applying recovery."""
    state = load_state(repo)
    if not state or not state.get('recovery_ready'):
        raise RuntimeError('No complete recovery point is available')
    point = _point(repo, state)
    if state.get('phase') == 'restored' and not state.get('notification_pending'):
        raise RuntimeError('This recovery point has already been restored')
    for file, checksum in [
        ('application.zip', 'code_sha256'), ('database.db', 'database_sha256'),
        ('recover.py', 'runner_sha256'), ('db_paths.py', 'backup_helper_sha256'),
    ]:
        if file == 'database.db' and not state['database_existed']:
            continue
        if _digest(point / file) != state[checksum]:
            raise RuntimeError('Recovery backup verification failed')
    if _tree_digest(point / 'runtime') != state['runtime_sha256']:
        raise RuntimeError('Recovery runtime verification failed')
    if state.get('shell_existed') and _tree_digest(point / 'shell') != state['shell_sha256']:
        raise RuntimeError('Recovery desktop shell verification failed')
    _run(repo, [str(_python(point / 'runtime')), '-c', 'import flask, sqlalchemy'],
         env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    _run(repo, ['git', 'cat-file', '-e', f'{state["previous_commit"]}^{{commit}}'])
    return state


def _stage_recovery_tree(source: Path, target: Path, checksum: str) -> None:
    """Reuse verified staging or replace only an interrupted, owned staging copy."""
    if target.is_symlink():
        raise RuntimeError('Recovery staging cannot be a symbolic link')
    if target.exists():
        if _tree_digest(target) == checksum:
            return
        shutil.rmtree(target)
    shutil.copytree(source, target)
    if _tree_digest(target) != checksum:
        raise RuntimeError('Staged recovery files failed verification')


def restore(repo: Path, *, confirmed: bool = False, launch: bool = True) -> None:
    """Restore a verified matched code/runtime/database point after explicit consent."""
    if not confirmed:
        raise PermissionError('Recovery requires explicit user confirmation')
    _assert_quiescent(repo)
    state = verify_recovery(repo)
    point = _point(repo, state)
    save_state(repo, state, 'restoring')
    try:
        failed = point / 'failed-state'
        failed.mkdir(exist_ok=True)
        database = database_path(repo)
        if not state.get('failed_database_copied'):
            for suffix in ('', '-wal', '-shm'):
                source = Path(str(database) + suffix)
                target = Path(str(failed / 'database.db') + suffix)
                if source.exists():
                    partial = target.with_name(target.name + '.partial')
                    shutil.copy2(source, partial)
                    os.replace(partial, target)
            save_state(repo, state, 'restoring', failed_database_copied=True)
        if not (failed / 'application.zip').exists():
            _run(repo, ['git', 'archive', '--format=zip',
                        f'--output={failed / "application.zip"}', 'HEAD'])
        if not (failed / 'tracked-changes.patch').exists():
            (failed / 'tracked-changes.patch').write_text(
                _run(repo, ['git', 'diff', '--binary', 'HEAD']), encoding='utf-8',
            )
        staged_runtime = repo / f'venv.restore-{state["attempt_id"]}'
        failed_runtime = repo / f'venv.failed-{state["attempt_id"]}'
        runtime_restored = (
            failed_runtime.exists() and (repo / 'venv').exists()
            and _tree_digest(repo / 'venv') == state['runtime_sha256']
        )
        if not runtime_restored:
            _stage_recovery_tree(point / 'runtime', staged_runtime, state['runtime_sha256'])
        staged_shell = repo / f'electron-dist.restore-{state["attempt_id"]}'
        failed_shell = repo / f'electron-dist.failed-{state["attempt_id"]}'
        shell_restored = (
            state.get('shell_existed') and failed_shell.exists()
            and (repo / 'electron-dist').exists()
            and _tree_digest(repo / 'electron-dist') == state['shell_sha256']
        )
        if state.get('shell_existed') and not shell_restored:
            _stage_recovery_tree(point / 'shell', staged_shell, state['shell_sha256'])
        staged_database = point / 'restore-database.db'
        database_restored = (
            state['database_existed'] and state.get('restore_database_sha256')
            and database.exists() and _digest(database) == state['restore_database_sha256']
            and not any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm'))
        )
        if state['database_existed'] and not database_restored and (
            not staged_database.exists()
            or _digest(staged_database) != state.get('restore_database_sha256')
        ):
            _snapshot_database(repo, point / 'database.db', staged_database)
            save_state(repo, state, 'restoring',
                       restore_database_sha256=_digest(staged_database))
        # The caller has stopped the entire stack and exited the desktop shell.
        # Retain the failed runtime/shell rather than deleting either one.
        _run(repo, ['git', 'reset', '--hard', state['previous_commit']])
        if not failed_runtime.exists():
            os.replace(repo / 'venv', failed_runtime)
        if (repo / 'venv').exists():
            if _tree_digest(repo / 'venv') != state['runtime_sha256']:
                raise RuntimeError('Runtime changed during recovery; preserved without overwriting')
            if staged_runtime.exists():
                shutil.rmtree(staged_runtime)
        else:
            os.replace(staged_runtime, repo / 'venv')
        if state.get('shell_existed'):
            if not failed_shell.exists() and (repo / 'electron-dist').exists():
                os.replace(repo / 'electron-dist', failed_shell)
            if (repo / 'electron-dist').exists():
                if _tree_digest(repo / 'electron-dist') != state['shell_sha256']:
                    raise RuntimeError('Desktop shell changed during recovery')
                if staged_shell.exists():
                    shutil.rmtree(staged_shell)
            else:
                os.replace(staged_shell, repo / 'electron-dist')
        if not database_restored:
            for suffix in ('', '-wal', '-shm'):
                existing = Path(str(database) + suffix)
                preserved = Path(str(failed / 'original-database.db') + suffix)
                if existing.exists() and not preserved.exists():
                    os.replace(existing, preserved)
        if state['database_existed']:
            if database.exists():
                if _digest(database) != state['restore_database_sha256']:
                    raise RuntimeError('Database changed during recovery; preserved without overwrite')
                staged_database.unlink(missing_ok=True)
            else:
                os.replace(staged_database, database)
        save_state(repo, state, 'restored', restored_at=utc_timestamp())
        if launch:
            launch_workspace(repo)
    except Exception as error:
        save_state(repo, state, 'failed', failed_phase='restoring',
                   error=diagnostic_error(error))
        raise


def launch_workspace(repo: Path) -> None:
    """Open the restored desktop or backend only after its durable recovery gate clears."""
    state = load_state(repo)
    if state and state.get('notification_pending'):
        raise RuntimeError('Dismiss the recovery notice before opening the restored workspace')
    shell = repo / 'electron-dist' / 'Sales Buddy.exe'
    if shell.exists():
        subprocess.Popen([str(shell)], cwd=repo)
    else:
        subprocess.Popen([str(_python(repo / 'venv')), '-m', 'app.supervisor'], cwd=repo)


def failure_report(repo: Path, state: dict) -> dict:
    """Build a shareable report from allowlisted metadata, never raw logs or data."""
    report = {'schema_version': 1, 'operation': 'update',
              'recovery': 'restored' if state.get('phase') == 'restored' else 'incomplete'}
    for field in ('previous_commit', 'target_commit'):
        value = state.get(field)
        if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40,64}', value):
            report[field] = value
    phases = PENDING_PHASES | {'ready', 'restored', 'shell', 'startup', 'preflight', 'activating'}
    sources = [
        ('upgrade', state.get('original_failure') or state),
        ('startup', (state.get('startup_failure') or {}) if 'startup_failure' in state
         else (read_json(startup_path(repo)) or {})),
    ]
    if state.get('phase') == 'failed' and state.get('original_failure'):
        sources.append(('recovery_failure', state))
    for name, source in sources:
        details = {'phase': source.get('failed_phase') or source.get('phase')}
        if details['phase'] not in phases:
            details['phase'] = 'unknown'
        error = source.get('error') or {}
        categories = {
            'operation_failed', 'dependency_missing', 'migration_foreign_keys',
            'database_locked', 'backup_failed', 'disk_space', 'permission', 'timeout',
            'rehearsal_failed',
        }
        types = {
            'Error', 'RuntimeError', 'ValueError', 'TimeoutError', 'PermissionError',
            'OSError', 'IntegrityError', 'OperationalError', 'ModuleNotFoundError',
            'CalledProcessError', 'StartupFailure', 'RehearsalError',
        }
        details['error'] = {
            'type': error.get('type') if error.get('type') in types else 'Error',
            'category': error.get('category') if error.get('category') in categories
            else 'operation_failed',
        }
        for field in ('exit_code', 'violation_count'):
            value = error.get(field)
            if type(value) is int and abs(value) <= 1000000000:
                details['error'][field] = value
        details['error']['parent_tables'] = sorted({
            table if isinstance(table, str) and table in SAFE_TABLES else 'other'
            for table in (error.get('parent_tables') or [])[:32]
        })
        code_files = {
            'upgrade.py', 'migrations.py', 'supervisor.py', '__init__.py', 'models.py',
            'db_paths.py', 'database_startup.py', 'upgrade_rehearsal.py', 'worker.py',
        }
        details['error']['frames'] = [
            {'file': frame['file'], 'line': frame['line']}
            for frame in (error.get('frames') or [])[-16:]
            if isinstance(frame, dict) and frame.get('file') in code_files
            and type(frame.get('line')) is int and 0 < frame['line'] <= 100000
        ]
        report[name] = details
    return report


def reporting_enabled() -> bool:
    """Respect the existing telemetry privacy preference, including local development."""
    return (
        os.environ.get('SALESBUDDY_TELEMETRY_OPT_OUT', '').lower() not in ('true', '1', 'yes')
        and os.environ.get('FLASK_ENV', '').lower() != 'development'
    )


def _report_directory(repo: Path) -> Path:
    """Keep pending diagnostics outside code/runtime/database restoration."""
    return database_path(repo).parent / 'diagnostic-outbox'


def queue_failure_report(repo: Path, state: dict) -> dict:
    """Freeze the first failure before restoration or any optional user interaction."""
    directory = _report_directory(repo)
    report_id = state['attempt_id']
    path = directory / f'{report_id}-report.json'
    existing = read_json(path)
    if existing:
        return existing
    item = {
        'schema_version': 1, 'kind': 'reports', 'report_id': report_id,
        'payload': {'schema_version': 1, 'report_id': report_id,
                    'report': failure_report(repo, state)},
        'created_at': utc_timestamp(), 'status': 'queued', 'attempts': 0,
    }
    atomic_json(path, item)
    save_state(repo, state, state['phase'], report_id=report_id, report_queued_at=utc_timestamp())
    return item


def queue_contact_request(repo: Path, state: dict) -> None:
    """Record only explicit consent and the original report ID, never cached user identity."""
    if not reporting_enabled():
        raise RuntimeError('Automatic reporting is disabled; contact requests cannot be sent')
    report_id = state['attempt_id']
    path = _report_directory(repo) / f'{report_id}-followup.json'
    if read_json(path):
        return
    atomic_json(path, {
        'schema_version': 1, 'kind': 'followups', 'report_id': report_id,
        'payload': {'schema_version': 1, 'report_id': report_id},
        'created_at': utc_timestamp(), 'status': 'queued', 'attempts': 0,
    })


def _report_token() -> str:
    """Use the existing CLI sign-in noninteractively, without importing the broken app."""
    executable = shutil.which('az')
    if not executable:
        raise RuntimeError('Azure CLI authentication is unavailable')
    result = subprocess.run(
        [executable, 'account', 'get-access-token', '--tenant', CORPORATE_TENANT,
         '--resource', 'https://management.azure.com', '--query', 'accessToken',
         '--output', 'tsv', '--only-show-errors'],
        capture_output=True, text=True, timeout=12, check=True,
        env={**os.environ, 'AZURE_CONFIG_DIR': os.environ.get('AZURE_CONFIG_DIR') or str(
            Path.home() / ('SalesBuddyDev' if os.environ.get('FLASK_ENV') == 'development'
                           else 'SalesBuddy') / '.azure'),
             'AZURE_CORE_COLLECT_TELEMETRY': 'no'},
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    token = result.stdout.strip()
    try:
        encoded = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    except (IndexError, ValueError, UnicodeError) as error:
        raise RuntimeError('Authentication did not return a usable token') from error
    if not isinstance(claims, dict) or claims.get('tid') != CORPORATE_TENANT:
        raise RuntimeError('A Microsoft corporate sign-in is required for diagnostic delivery')
    return token


def recovery_contact_email() -> str | None:
    """Resolve display-only contact from the same corporate claims APIM uses for follow-up."""
    try:
        encoded = _report_token().split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    except (OSError, ValueError, RuntimeError, IndexError, subprocess.SubprocessError) as error:
        logger.info('Microsoft email unavailable for recovery notice (%s)', type(error).__name__)
        return None
    for field in ('preferred_username', 'upn', 'email', 'unique_name'):
        value = claims.get(field)
        if isinstance(value, str) and len(value) <= 254 and re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value,
        ):
            return value
    logger.info('Corporate sign-in has no usable Microsoft email claim')
    return None


def send_pending_reports(repo: Path, *, limit: int = 4) -> None:
    """Deliver bounded queued events; keep failures durable without blocking workspace startup."""
    directory = _report_directory(repo)
    if not reporting_enabled():
        return
    if not directory.exists():
        return
    try:
        with upgrade_lock(directory, name='delivery.lock', timeout=0):
            _send_pending_reports(directory, limit)
    except UpgradeBusyError:
        logger.info('Diagnostic delivery is already running')


def _send_pending_reports(directory: Path, limit: int) -> None:
    """Retry immutable payloads independently, accepting only an explicit matching receipt."""
    token = None
    count = 0
    for path in sorted(directory.glob('*.json')):
        if count >= limit:
            break
        try:
            item = read_json(path)
        except (OSError, ValueError, RuntimeError):
            logger.exception('Saved diagnostic event could not be read; retained for investigation')
            continue
        if not item or item.get('status') in ('sent', 'rejected'):
            continue
        retry_at = item.get('retry_at')
        if isinstance(retry_at, (int, float)) and retry_at > time.time():
            continue
        count += 1
        try:
            if item.get('kind') not in ('reports', 'followups'):
                raise ValueError('Invalid diagnostic queue operation')
            report_id = item['report_id']
            if not re.fullmatch(r'[0-9a-f]{32}', report_id):
                raise ValueError('Invalid diagnostic report identity')
            payload = item['payload']
            if payload.get('report_id') != report_id:
                raise ValueError('Diagnostic queue identity mismatch')
            body = json.dumps(payload).encode('utf-8')
            if len(body) > 16384:
                raise ValueError('Diagnostic queue payload is too large')
            if token is None:
                token = _report_token()
            request = urllib.request.Request(
                f'{DIAGNOSTICS_URL}/{item["kind"]}', data=body, method='POST',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                receipt = json.loads(response.read(8192))
                if (
                    response.status != 202 or not isinstance(receipt, dict)
                    or receipt.get('report_id') != report_id
                    or receipt.get('status') != 'accepted'
                ):
                    raise ValueError('Diagnostic intake returned an invalid receipt')
            item.update(status='sent', sent_at=utc_timestamp(), retry_at=None, last_error=None)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            attempts = int(item.get('attempts', 0)) + 1
            rejected = isinstance(error, urllib.error.HTTPError) and error.code in (400, 409, 413, 422)
            delay = min(86400, REPORT_RETRY_SECONDS * 2 ** min(attempts - 1, 8))
            if isinstance(error, urllib.error.HTTPError) and error.headers:
                retry_after = error.headers.get('Retry-After', '')
                if re.fullmatch(r'[0-9]{1,6}', retry_after):
                    delay = max(delay, min(86400, int(retry_after)))
            item.update(
                status='rejected' if rejected else 'queued', attempts=attempts,
                retry_at=time.time() + delay,
                last_error=type(error).__name__,
            )
            logger.warning('Diagnostic %s delivery deferred (%s)', item.get('kind'), type(error).__name__)
        atomic_json(path, item)


def start_report_delivery(repo: Path, state: dict) -> None:
    """Launch the frozen sender before restoration, outside the Electron process lifetime."""
    if not reporting_enabled():
        return
    point = _point(repo, state)
    try:
        python = _python(point / 'runtime')
        runner = point / 'recover.py'
        argv = [str(python), str(runner), 'send-reports', '--repo', str(repo)]
        if os.name == 'nt':
            if any(re.search(r'["%!\r\n&|<>^]', str(value))
                   for value in (python, runner, repo, point)):
                raise RuntimeError('Diagnostic sender location cannot be launched safely')
            argv = ['cmd.exe', '/c', 'start', '', '/min', *argv]
        subprocess.Popen(
            argv,
            cwd=point, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, RuntimeError):
        logger.exception('Diagnostic sender could not start; the report remains queued')


def report_update_failure(repo: Path, state: dict) -> None:
    """Persist and attempt reporting immediately without making restoration depend on it."""
    try:
        queue_failure_report(repo, state)
    except (OSError, ValueError, RuntimeError):
        logger.exception('Automatic diagnostics could not be saved; recovery will still proceed')
        return
    start_report_delivery(repo, state)


def dismiss_recovery(repo: Path, *, contact: bool = False) -> None:
    """Release a successfully restored workspace without requiring export or delivery."""
    state = load_state(repo)
    if not state or state.get('phase') != 'restored' or not state.get('notification_pending'):
        raise RuntimeError('Recovery must finish before returning to the workspace')
    if contact:
        try:
            queue_contact_request(repo, state)
        except (OSError, ValueError, RuntimeError):
            logger.exception('The optional contact request could not be saved; workspace remains usable')
            contact = False
    save_state(repo, state, 'restored', notification_pending=False, acknowledged_at=utc_timestamp(),
               contact_requested=contact)
    start_report_delivery(repo, state)


def recovery_notice(repo: Path) -> bool:
    """Show an offline Windows gate that works even with a pre-safety restored shell."""
    if os.name != 'nt':
        raise RuntimeError('The offline recovery notice requires Windows')
    state = load_state(repo)
    if not state or not state.get('notification_pending'):
        raise RuntimeError('No failed-update recovery notice is pending')
    point = _point(repo, state)
    response = point / 'notice-response.json'
    response.unlink(missing_ok=True)
    report = json.dumps(failure_report(repo, state), indent=2)
    settings = point / 'notice-settings.json'
    try:
        delivery = read_json(_report_directory(repo) / f'{state["attempt_id"]}-report.json')
    except (OSError, ValueError, RuntimeError):
        logger.exception('Diagnostic delivery status is unavailable; restoration remains usable')
        delivery = None
    enabled = reporting_enabled()
    email = recovery_contact_email() if enabled and state.get('phase') == 'restored' else None
    atomic_json(settings, {
        'response': str(response), 'report': report,
        'restored': state.get('phase') == 'restored',
        'reporting_enabled': enabled,
        'contact_email': email,
        'report_sent': (delivery or {}).get('status') == 'sent',
        'report_available': delivery is not None,
        'report_rejected': (delivery or {}).get('status') == 'rejected',
        'report_id': state['attempt_id'],
    })
    values = base64.b64encode(str(settings).encode('utf-8')).decode('ascii')
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$settingsPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('VALUES'))
$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Sales Buddy - update failed'
$form.ClientSize = New-Object System.Drawing.Size(700, 450)
$form.AutoScaleMode = [Windows.Forms.AutoScaleMode]::Dpi
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.BackColor = [Drawing.Color]::FromArgb(32, 34, 37)
$form.ForeColor = [Drawing.Color]::FromArgb(229, 230, 232)
$form.Font = New-Object Drawing.Font('Segoe UI', 11)
$title = New-Object System.Windows.Forms.Label
$title.Text = 'Your previous version has been restored'
$title.Font = New-Object Drawing.Font('Segoe UI', 18)
$title.SetBounds(24, 22, 650, 54)
$description = New-Object System.Windows.Forms.Label
$description.SetBounds(24, 88, 650, 88)
if ($settings.restored) {
    $description.Text = 'The update could not start, so Sales Buddy restored your previous ' +
        'application and database. You can continue working. No report download or email is needed.'
} else {
    $title.Text = 'Automatic recovery needs attention'
    $description.Text = 'Automatic recovery could not finish. Do not continue using this ' +
        'installation. Your recovery snapshot and failed files have been retained. ' +
        'Contact the Sales Buddy maintainer for help.'
}
$delivery = New-Object System.Windows.Forms.Label
$delivery.SetBounds(24, 184, 650, 54)
if (-not $settings.report_available) {
    $delivery.Text = 'Automatic diagnostics could not be saved. An optional report copy is available below.'
} elseif (-not $settings.reporting_enabled) {
    $delivery.Text = 'Automatic reporting is disabled. Diagnostics are saved locally.'
} elseif ($settings.report_sent) {
    $delivery.Text = 'Sanitized diagnostics have been sent for review.'
} elseif ($settings.report_rejected) {
    $delivery.Text = 'Automatic reporting was not accepted. Diagnostics are saved locally; an optional copy is available below.'
} else {
    $delivery.Text = 'Sanitized diagnostics are saved and queued to send automatically when connected and signed in.'
}
$reference = New-Object System.Windows.Forms.Label
$reference.Text = 'Report ID: ' + $settings.report_id
$reference.SetBounds(24, 242, 650, 28)
$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save report (optional)'
$save.SetBounds(24, 384, 224, 40)
$save.FlatStyle = 'Flat'
$save.ForeColor = $form.ForeColor
$save.BackColor = [Drawing.Color]::FromArgb(45, 48, 53)
$feedback = New-Object System.Windows.Forms.Label
$feedback.SetBounds(24, 348, 650, 30)
$contact = New-Object System.Windows.Forms.CheckBox
$contact.Text = 'Contact me when a fix is available using my Microsoft email.'
$contact.SetBounds(24, 276, 650, 28)
$contact.ForeColor = $form.ForeColor
$contact.BackColor = $form.BackColor
$contact.Visible = [bool]$settings.restored -and [bool]$settings.reporting_enabled
$email = New-Object System.Windows.Forms.Label
$email.SetBounds(44, 308, 630, 32)
$email.AutoEllipsis = $true
$email.Visible = $contact.Visible
if ($settings.contact_email) {
    $email.Text = 'Microsoft email: ' + $settings.contact_email
} else {
    $email.Text = 'Microsoft email unavailable. A contact request will wait for sign-in.'
}
$emailTip = New-Object System.Windows.Forms.ToolTip
$emailTip.SetToolTip($email, $email.Text)
$continue = New-Object System.Windows.Forms.Button
$continue.Text = 'Continue working'
$continue.SetBounds(450, 384, 224, 40)
$continue.FlatStyle = 'Flat'
$continue.ForeColor = [Drawing.Color]::FromArgb(20, 30, 42)
$continue.BackColor = [Drawing.Color]::FromArgb(157, 195, 240)
$continue.Visible = [bool]$settings.restored
$form.AcceptButton = $continue
$save.Add_Click({
    $dialog = New-Object System.Windows.Forms.SaveFileDialog
    $dialog.Title = 'Save private diagnostic report'
    $dialog.Filter = 'JSON report (*.json)|*.json'
    $dialog.FileName = 'salesbuddy-update-failure.json'
    try {
        if ($dialog.ShowDialog($form) -ne 'OK') { return }
        [IO.File]::WriteAllText($dialog.FileName, $settings.report,
            (New-Object Text.UTF8Encoding($false)))
        $feedback.Text = 'An optional copy of the report was saved.'
    } catch {
        $feedback.Text = 'The report could not be saved. Choose another location and try again.'
    } finally { $dialog.Dispose() }
})
$continue.Add_Click({
    if (-not $settings.restored) { return }
    $form.Close()
})
$form.Add_FormClosing({
    if (-not $settings.restored) { return }
    @{ dismissed = $true; contact = [bool]$contact.Checked } |
        ConvertTo-Json | Set-Content -LiteralPath $settings.response -Encoding UTF8
})
$form.Controls.AddRange(@($title, $description, $delivery, $reference, $save, $feedback, $contact, $email, $continue))
$form.ShowDialog() | Out-Null
$emailTip.Dispose()
$form.Dispose()
""".replace('VALUES', values)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', encoded],
            check=True,
        )
    finally:
        settings.unlink(missing_ok=True)
    if not response.exists():
        return False
    answer = json.loads(response.read_text(encoding='utf-8-sig'))
    if answer.get('dismissed') is not True or type(answer.get('contact')) is not bool:
        raise RuntimeError('The recovery notice response is incomplete')
    with upgrade_lock(repo):
        dismiss_recovery(repo, contact=answer['contact'])
    response.unlink(missing_ok=True)
    return True


def recover_update(repo: Path) -> bool:
    """Automatically restore a failed update, then present its dismissible recovery notice."""
    with upgrade_lock(repo, name='recovery-notice.lock', timeout=0):
        return _recover_update(repo)


def _recover_update(repo: Path) -> bool:
    """Keep duplicate update launchers from restoring or acknowledging concurrently."""
    with upgrade_lock(repo):
        state = load_state(repo)
        if not state or not state.get('recovery_ready') or not state.get('changes_started'):
            raise RuntimeError('No changed upgrade has a verified recovery point')
        if state.get('phase') == 'ready':
            raise RuntimeError('An accepted update cannot be automatically restored')
        if not state.get('notification_pending') and state.get('phase') == 'restored':
            logger.info('The failed update has already been recovered and acknowledged')
            return True
        if not state.get('original_failure'):
            save_state(repo, state, state['phase'], notification_pending=True,
                       startup_failure=read_json(startup_path(repo)),
                       original_failure={
                           'phase': state['phase'], 'failed_phase': state.get('failed_phase'),
                           'error': state.get('error'),
                       })
        report_update_failure(repo, state)
        if state['phase'] != 'restored':
            try:
                restore(repo, confirmed=True, launch=False)
            except Exception as error:
                logger.exception('Automatic recovery failed; workspace remains blocked')
                latest = load_state(repo)
                if latest:
                    save_state(repo, latest, 'failed', failed_phase='restoring',
                               error=diagnostic_error(error))
    if not recovery_notice(repo):
        return False
    state = load_state(repo)
    if not state or state.get('phase') != 'restored' or state.get('notification_pending'):
        raise RuntimeError('The recovered workspace still requires dismissing the recovery notice')
    launch_workspace(repo)
    return True


def launch_recovery(repo: Path, parent_pid: int | None = None) -> None:
    """Hand recovery to the saved runtime, outside the installation being restored."""
    state = read_json(repo / 'data' / 'upgrade-state.json')
    if not state or not state.get('recovery_ready') or not state.get('changes_started'):
        raise RuntimeError('No changed upgrade has a verified recovery point')
    if state.get('phase') == 'ready':
        raise RuntimeError('An accepted update cannot be automatically restored')
    if (state.get('repo') != str(repo.resolve())
            or not isinstance(state.get('database'), str)
            or not Path(state['database']).is_absolute()
            or not re.fullmatch(r'[0-9a-f]{32}', state.get('attempt_id', ''))):
        raise RuntimeError('Recovery launch state belongs to another installation')
    point = Path(state['database']).parent / 'upgrade-recovery' / state['attempt_id']
    saved = read_json(point / 'manifest.json')
    if not saved or any(saved.get(key) != state.get(key) for key in (
        'repo', 'database', 'attempt_id', 'runner_sha256', 'backup_helper_sha256',
        'previous_commit',
    )):
        raise RuntimeError('Recovery launch state does not match its saved manifest')
    runner = point / 'recover.py'
    helper = point / 'db_paths.py'
    if (_digest(runner) != state.get('runner_sha256')
            or _digest(helper) != state.get('backup_helper_sha256')):
        raise RuntimeError('The frozen recovery entry points failed verification')
    python = _python(point / 'runtime')
    if not python.exists() or os.name != 'nt':
        raise RuntimeError('The saved Windows recovery runtime is unavailable')
    if any(re.search(r'["%!\r\n&|<>^]', str(value)) for value in (python, runner, repo, point)):
        raise RuntimeError('The recovery location cannot be launched safely')
    launcher = point / 'launch-recovery.cmd'
    launcher.write_text(
        '@echo off\nchcp 65001 >nul\n'
        f'"{python}" "{runner}" recover --repo "{repo}" '
        f'--parent-pid {parent_pid or os.getpid()}\n'
        'if %errorlevel% equ 2 exit /b 0\n'
        'if errorlevel 1 (\necho Recovery did not finish. Saved files have been retained.\npause\n)\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['cmd.exe', '/c', 'start', '', '/min', str(launcher)],
        cwd=point, check=True, timeout=10,
    )


def _wait_for_parent(pid: int) -> None:
    """Wait for the owning desktop executable to release its files before recovery."""
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle and ctypes.get_last_error() not in (0, 87):
            raise OSError(ctypes.get_last_error(), 'Could not verify desktop process exit')
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, 60000) != 0:
                    raise TimeoutError('Desktop app did not close before recovery')
            finally:
                kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        raise TimeoutError('Desktop app did not close before recovery')


def main() -> int:
    """Run the same safety gates for Electron, scripts, and offline recovery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=[
        'prepare', 'apply', 'accept', 'wait-ready', 'restore', 'verify', 'fail',
        'launch-recovery', 'recover', 'send-reports',
    ])
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--target', default='origin/main')
    parser.add_argument('--boot-id')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '5151')))
    parser.add_argument('--confirm', '--confirmed', dest='confirm', action='store_true')
    parser.add_argument('--wait-pid', '--parent-pid', dest='wait_pid', type=int)
    parser.add_argument('--shell-rebuild', action='store_true')
    parser.add_argument('--phase', choices=[
        'shell', 'startup', 'dependencies', 'code', 'rehearsing', 'migrating',
    ], default='startup')
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        if args.action == 'send-reports':
            send_pending_reports(repo)
            return 0
        if args.action in {'restore', 'recover'} and args.wait_pid:
            _wait_for_parent(args.wait_pid)
        if args.action == 'recover':
            try:
                return 0 if recover_update(repo) else 2
            except UpgradeBusyError as error:
                if error.lock_name != 'recovery-notice.lock':
                    raise
                logger.info('Recovery is already running; leaving its existing notice in control')
                return 0
        if args.action == 'launch-recovery':
            with upgrade_lock(repo):
                launch_recovery(repo, args.wait_pid)
            return 0
        if args.action == 'wait-ready':
            if not args.boot_id:
                raise ValueError('A startup identity is required')
            wait_ready(repo, args.boot_id, args.port)
            return 0
        with upgrade_lock(repo):
            if args.action == 'prepare':
                prepare(repo, args.target, shell_rebuild=args.shell_rebuild)
            elif args.action == 'apply':
                apply_update(repo)
            elif args.action == 'verify':
                verify_recovery(repo)
            elif args.action in ('accept', 'wait-ready'):
                if not args.boot_id:
                    raise ValueError('A startup identity is required')
                if args.action == 'accept':
                    accept(repo, args.boot_id, args.port)
                else:
                    wait_ready(repo, args.boot_id, args.port)
            elif args.action == 'fail':
                state = load_state(repo)
                if state is None:
                    raise RuntimeError('No pending upgrade can be marked failed')
                save_state(repo, state, 'failed', failed_phase=args.phase,
                           error={'type': 'Error', 'category': 'operation_failed', 'frames': []})
                report_update_failure(repo, state)
            else:
                restore(repo, confirmed=args.confirm)
        return 0
    except Exception as error:
        if args.action in {'restore', 'verify'}:
            report = {'schema_version': 1, 'operation': args.action,
                      'updated_at': utc_timestamp(), 'error': diagnostic_error(error)}
            try:
                atomic_json(repo / 'data' / 'recovery-error.json', report)
                state = load_state(repo)
                if state and (args.action != 'restore' or args.confirm):
                    save_state(repo, state, 'failed', failed_phase=(
                        'restoring' if args.action == 'restore' else 'verification'),
                               error=report['error'])
            except (OSError, ValueError, RuntimeError):
                logger.exception('Could not persist recovery-failure details')
        logger.exception('Upgrade operation failed; existing recovery artifacts are retained')
        return 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
