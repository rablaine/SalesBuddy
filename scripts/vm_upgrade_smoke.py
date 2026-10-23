"""Operate a disposable Windows VM smoke test, never a developer or real-user install."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

NOTE_TEXT = (
    '<p>VM TEST ONLY: this note must survive the failed update.</p>',
    '<p>VM TEST ONLY: this second note must survive the successful update.</p>',
)
CUSTOMER_TPID = -901001
MARKER = 'vm-upgrade-test.json'
VERSION_FILE = 'VM_UPGRADE_TEST_VERSION.txt'
AUTHOR = ['-c', 'user.name=Sales Buddy VM Test', '-c', 'user.email=vm-test@example.invalid']
TRAILER = 'Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>'

BROKEN_SUPERVISOR = '''"""Intentional disposable-VM startup failure. NEVER publish this version."""
import os
import sqlite3
from pathlib import Path
from scripts.upgrade import (
    atomic_json, database_path, diagnostic_error, load_state, save_state, startup_path,
)

repo = Path(__file__).resolve().parents[1]
if not (repo / 'data' / 'vm-upgrade-test.json').is_file():
    raise RuntimeError('VM test marker missing; refusing the test database mutation')
try:
    with sqlite3.connect(database_path(repo)) as connection:
        connection.execute('CREATE TABLE vm_update_canary (id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO vm_update_canary VALUES (1)')
    session = __import__('json').loads(
        (repo / 'data' / 'vm-upgrade-test.json').read_text(encoding='utf-8'))
    state = load_state(repo)
    with (repo / 'electron-dist' / 'resources' / 'app.asar').open('rb') as handle:
        shell_hash = __import__('hashlib').file_digest(handle, 'sha256').hexdigest()
    atomic_json(Path(session['work']) / 'fault-reached.json', {
        'attempt_id': state['attempt_id'], 'target_commit': state['target_commit'],
        'shell_sha256': shell_hash, 'canary_committed': True,
    })
    raise RuntimeError('Intentional VM-only startup failure after database mutation')
except Exception as error:
    state = load_state(repo)
    save_state(repo, state, 'failed', failed_phase='startup', error=diagnostic_error(error))
    atomic_json(startup_path(repo), {
        'schema_version': 1, 'state': 'failed', 'phase': 'failed',
        'boot_id': os.environ.get('SALESBUDDY_BOOT_ID'),
        'error': diagnostic_error(error), 'children': [],
    })
    raise
'''


def run(repo: Path, *args: str) -> str:
    """Run one checked local Git command without printing credentials or command output."""
    return subprocess.run(
        ['git', *args], cwd=repo, check=True, capture_output=True, text=True, timeout=120,
    ).stdout.strip()


def digest(path: Path) -> str:
    """Hash a payload without reading a desktop distribution into memory."""
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def write_json(path: Path, value: dict) -> None:
    """Persist VM-only control state with the same atomic writer as the product."""
    from scripts.upgrade import atomic_json
    atomic_json(path, value)


def validate_install(repo: Path) -> None:
    """Limit all operations to the signed-in VM user's standard installed application."""
    local = os.environ.get('LOCALAPPDATA')
    if not local or repo.resolve() != (Path(local) / 'SalesBuddy').resolve():
        raise RuntimeError('This kit only operates on %LOCALAPPDATA%\\SalesBuddy inside the VM')
    if not (repo / '.git').exists() or not (repo / 'venv' / 'Scripts' / 'python.exe').is_file():
        raise RuntimeError('Install the normal Sales Buddy release in the VM first')
    if Path(run(repo, 'rev-parse', '--show-toplevel')).resolve() != repo.resolve():
        raise RuntimeError('The installation is not its own Git working tree')


def validate_payload(kit: Path) -> dict:
    """Verify every bundled payload before stopping or changing the VM installation."""
    manifest = json.loads((kit / 'kit-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1:
        raise RuntimeError('Unsupported VM kit manifest')
    for relative, expected in manifest['files'].items():
        path = (kit / relative).resolve()
        if not path.is_relative_to(kit.resolve()) or digest(path) != expected:
            raise RuntimeError(f'VM kit payload verification failed: {relative}')
    return manifest


def extract_source(source: Path, repo: Path) -> list[str]:
    """Extract only installation-relative source files, excluding secrets and Git metadata."""
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        for name in names:
            path = Path(name)
            if (not name or path.is_absolute() or '..' in path.parts
                    or '.git' in path.parts or path.name == '.env'
                    or not (repo / path).resolve().is_relative_to(repo.resolve())):
                raise RuntimeError('Unsafe source archive member')
        archive.extractall(repo)
    return names


def seed_database(repo: Path, backup: Path) -> dict:
    """Initialize offline fixture records without starting the app or authenticating."""
    from dotenv import load_dotenv
    from flask import Flask
    from sqlalchemy import select
    from app.db_paths import backup_database, resolve_db_path
    from app.models import Customer, Note, UserPreference, db
    from app.services.database_startup import database_startup_lock, initialize_database

    load_dotenv(repo / '.env')
    database = resolve_db_path().resolve()
    if database.exists() and not backup_database(backup, src=database):
        raise RuntimeError('The initial VM database backup failed')
    application = Flask(__name__)
    application.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI=f'sqlite:///{database}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    with application.app_context():
        try:
            with database_startup_lock(db.engine):
                initialize_database(db)
                if db.session.execute(select(Customer.id).limit(1)).first() or (
                    db.session.execute(select(Note.id).limit(1)).first()
                ):
                    raise RuntimeError('This kit requires a fresh VM with no customers or notes')
                pref = UserPreference.query.one()
                pref.first_run_modal_dismissed = True
                pref.guided_tour_completed = True
                pref.dark_mode = True
                pref.user_role = 'se'
                pref.copilot_actions_enabled = False
                pref.milestone_auto_sync = False
                pref.msx_auto_writeback = False
                customer = Customer(name='VM TEST ONLY - Upgrade Safety', tpid=CUSTOMER_TPID)
                db.session.add(customer)
                db.session.flush()
                notes = [Note(customer_id=customer.id, content=text,
                              call_date=datetime(2026, 10, 22, 10, 30))
                         for text in NOTE_TEXT]
                db.session.add_all(notes)
                db.session.commit()
                return {'database': str(database), 'customer_id': customer.id,
                        'note_ids': [note.id for note in notes]}
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()
            db.engine.dispose()


def require_fresh_database(repo: Path) -> None:
    """Reject non-fresh installations before changing source, branches or user records."""
    from dotenv import load_dotenv

    load_dotenv(repo / '.env')
    spec = importlib.util.spec_from_file_location('vm_initial_db_paths', repo / 'app' / 'db_paths.py')
    if not spec or not spec.loader:
        raise RuntimeError('The installed database resolver cannot be loaded')
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    database = resolver.resolve_db_path().resolve()
    if not database.is_file():
        raise RuntimeError('Open the freshly installed app once before running this kit')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('customers', 'notes', 'call_logs', 'engagements', 'milestones'):
            if table in tables and connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise RuntimeError('This kit requires a fresh VM with no customer work or notes')


def commit_files(repo: Path, paths: list[str], title: str, path_list: Path) -> str:
    """Commit only explicit fixture source paths, never an environment file or user data."""
    path_list.write_bytes(b'\0'.join(name.encode('utf-8') for name in paths) + b'\0')
    run(repo, 'add', f'--pathspec-from-file={path_list}', '--pathspec-file-nul')
    run(repo, *AUTHOR, 'commit', '-m', title, '-m', TRAILER)
    return run(repo, 'rev-parse', 'HEAD')


def create_target(repo: Path, work: Path, baseline: str, *, broken: bool) -> str:
    """Create a local-only update revision, without modifying the installed baseline."""
    label = 'broken' if broken else 'good'
    checkout = work / label
    run(repo, 'worktree', 'add', '-b', f'feature/vm-{label}-{work.name}', str(checkout), baseline)
    try:
        (checkout / VERSION_FILE).write_text(label + '\n', encoding='utf-8')
        status = checkout / 'electron' / 'status.html'
        with status.open('a', encoding='utf-8') as handle:
            handle.write(f'\n<!-- VM smoke-test desktop: {label} -->\n')
        paths = [VERSION_FILE, 'electron/status.html']
        if broken:
            (checkout / 'app' / 'supervisor.py').write_text(BROKEN_SUPERVISOR, encoding='utf-8')
            paths.append('app/supervisor.py')
        return commit_files(checkout, paths, f'test: VM-only {label} update',
                            work / f'{label}-paths.txt')
    finally:
        run(repo, 'worktree', 'remove', str(checkout))


def setup(repo: Path, kit: Path) -> None:
    """Bootstrap the already-installed disposable VM into a clean safety baseline."""
    if (repo / 'data' / MARKER).exists():
        raise RuntimeError('This VM already has a smoke-test session; use the test/check buttons')
    if run(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Tracked install files have local changes; nothing was overwritten')
    require_fresh_database(repo)
    work = kit / f'vm-session-{uuid.uuid4().hex[:8]}'
    work.mkdir()
    original = {'commit': run(repo, 'rev-parse', 'HEAD'),
                'origin': run(repo, 'remote', 'get-url', 'origin')}
    if (repo / '.env').exists():
        shutil.copy2(repo / '.env', work / 'initial.env')
    (work / 'original-install.json').write_text(json.dumps(original, indent=2), encoding='utf-8')
    run(repo, 'switch', '-c', f'feature/vm-upgrade-safety-{work.name}')
    paths = extract_source(kit / 'payload' / 'source.zip', repo)
    (repo / VERSION_FILE).write_text('baseline\n', encoding='utf-8')
    baseline = commit_files(repo, [*paths, VERSION_FILE], 'test: VM upgrade safety baseline',
                            work / 'baseline-paths.txt')
    fixture = seed_database(repo, work / 'initial-database.db')
    broken = create_target(repo, work, baseline, broken=True)
    good = create_target(repo, work, baseline, broken=False)
    remote = work / 'updates.git'
    run(repo, 'clone', '--bare', str(repo), str(remote))
    run(repo, '--git-dir', str(remote), 'update-ref', 'refs/heads/main', baseline)
    run(repo, '--git-dir', str(remote), 'symbolic-ref', 'HEAD', 'refs/heads/main')
    run(repo, 'remote', 'set-url', 'origin', str(remote))
    run(repo, 'fetch', 'origin', 'main')
    run(repo, 'branch', '--set-upstream-to=origin/main')
    write_json(repo / 'data' / MARKER, {
        'schema_version': 1, 'kit_id': json.loads(
            (kit / 'kit-manifest.json').read_text(encoding='utf-8'))['kit_id'],
        'repo': str(repo), 'baseline': baseline, 'broken': broken, 'good': good,
        'remote': str(remote), 'work': str(work), **fixture,
        'baseline_shell_sha256': digest(kit / 'payload' / 'shell' / 'resources' / 'app.asar'),
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    print('Offline test customer and two notes created. Corporate sign-in is not needed.')
    print('The VM origin now points to a LOCAL test repository. Nothing will be pushed or published.')


def session(repo: Path, kit: Path) -> dict:
    """Require the exact installed session that this test kit initialized."""
    state = json.loads((repo / 'data' / MARKER).read_text(encoding='utf-8'))
    kit_id = json.loads((kit / 'kit-manifest.json').read_text(encoding='utf-8'))['kit_id']
    if state.get('repo') != str(repo) or state.get('kit_id') != kit_id:
        raise RuntimeError('The VM test session belongs to another kit or installation')
    if Path(run(repo, 'remote', 'get-url', 'origin')).resolve() != Path(state['remote']).resolve():
        raise RuntimeError('The test origin changed; refusing to fetch or request an update')
    return state


def verify_records(state: dict) -> None:
    """Check exact synthetic record contents and absence of the failed-version canary."""
    database = Path(state['database'])
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Database integrity check failed')
        for identity, expected in zip(state['note_ids'], NOTE_TEXT, strict=True):
            row = connection.execute('SELECT content, customer_id FROM notes WHERE id=?',
                                     (identity,)).fetchone()
            if row != (expected, state['customer_id']):
                raise RuntimeError('A test note or its customer link did not survive')
        if connection.execute(
            "SELECT name FROM sqlite_master WHERE name='vm_update_canary'",
        ).fetchone():
            raise RuntimeError('The failed-version database change was not rolled back')


def check(repo: Path, state: dict, *, good: bool) -> None:
    """Verify durable outcome, exact installed revision, receipt, and actual test records."""
    from scripts.upgrade import load_state

    upgrade = load_state(repo)
    if not upgrade:
        raise RuntimeError('No durable update result is available yet')
    expected = state['good'] if good else state['baseline']
    if run(repo, 'rev-parse', 'HEAD') != expected:
        raise RuntimeError('The installed revision does not match the expected test result')
    if upgrade['phase'] != ('ready' if good else 'restored') or (
        upgrade.get('notification_pending')
    ):
        raise RuntimeError('Finish recovery and dismiss its notice before checking')
    if not good and (not upgrade.get('report_queued_at') or not upgrade.get('acknowledged_at')):
        raise RuntimeError('Recovery has no durable diagnostic queue or dismissal receipt')
    if not upgrade.get('needs_shell_rebuild') or not upgrade.get('shell_existed'):
        raise RuntimeError('This attempt did not exercise desktop replacement')
    shell_hash = digest(repo / 'electron-dist' / 'resources' / 'app.asar')
    if good:
        if upgrade.get('target_commit') != state['good'] or shell_hash == (
            state['baseline_shell_sha256']
        ):
            raise RuntimeError('The successful update did not replace the desktop payload')
    else:
        proof = Path(state['work']) / 'fault-reached.json'
        if not proof.is_file():
            failure = upgrade.get('original_failure') or upgrade
            phase = failure.get('failed_phase') or failure.get('phase') or 'unknown'
            raise RuntimeError(
                f'The intended startup fault was not reached; recovery began during {phase}. '
                f'Retain diagnostic report {upgrade["attempt_id"]}. Do not run the good update yet.'
            )
        reached = json.loads(proof.read_text(encoding='utf-8'))
        if (reached.get('attempt_id') != upgrade['attempt_id']
                or reached.get('target_commit') != state['broken']
                or reached.get('canary_committed') is not True
                or reached.get('shell_sha256') == state['baseline_shell_sha256']
                or (upgrade.get('original_failure') or {}).get('failed_phase') != 'startup'):
            raise RuntimeError('The intended post-rebuild, post-database-change fault was not reached')
        if shell_hash != state['baseline_shell_sha256']:
            raise RuntimeError('The previous desktop payload was not restored exactly')
    verify_records(state)
    print('PASS: expected version, database integrity, both notes and customer links are intact.')
    if not good:
        print('PASS: failed-version database mutation is gone; diagnostics were queued before dismissal.')


def request_update(repo: Path, state: dict, *, good: bool) -> None:
    """Exercise the real Electron watcher, including shell rebuild/staging, using a local origin."""
    if run(repo, 'rev-parse', 'HEAD') != state['baseline']:
        raise RuntimeError('Start this test from the safety baseline')
    if run(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('The test installation has tracked local changes')
    if good:
        check(repo, state, good=False)
    else:
        verify_records(state)
    from dotenv import dotenv_values
    values = dotenv_values(repo / '.env')
    port = int(values.get('PORT') or 5151)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=10) as response:
        health = json.load(response)
    if (health.get('status') != 'healthy' or health.get('worker_ready') is not True
            or health.get('commit_full') != state['baseline']):
        raise RuntimeError('Open the packaged desktop baseline and wait for full readiness first')
    target = state['good'] if good else state['broken']
    run(repo, '--git-dir', state['remote'], 'update-ref', 'refs/heads/main', target)
    # This is the same request used by Admin > Update, including its shell-rebuild flag.
    (repo / 'data' / 'electron-update.request').write_text('rebuild', encoding='utf-8')
    print('Update requested through the actual desktop shell. Keep Sales Buddy open.')
    print('This rebuild uses public npm/Electron downloads, but no corporate authentication.')
    if not good:
        print('EXPECTED: startup fails, diagnostics are queued, then the saved version is restored.')
        print('Diagnostics are submitted automatically, or queued if this VM is not signed in.')
        print('Continue must work without downloading a report. Contact requests are optional.')


def main() -> int:
    """Run only when the PowerShell VM guard has obtained disposable-VM confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'fail', 'check-rollback', 'good', 'check-good'])
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--confirm-vm', action='store_true')
    args = parser.parse_args()
    if not args.confirm_vm:
        parser.error('Use the numbered VM-only launchers, not this script directly')
    repo, kit = args.repo.resolve(), args.kit.resolve()
    validate_install(repo)
    validate_payload(kit)
    sys.path.insert(0, str(repo))
    if args.action == 'setup':
        setup(repo, kit)
    else:
        state = session(repo, kit)
        if args.action.startswith('check-'):
            check(repo, state, good=args.action == 'check-good')
        else:
            request_update(repo, state, good=args.action == 'good')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'VM TEST STOPPED: {error}', file=sys.stderr)
        raise
