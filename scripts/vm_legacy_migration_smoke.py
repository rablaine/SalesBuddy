"""Reproduce the historical task foreign-key failure only in an existing disposable VM kit."""
from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
import traceback
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from scripts import vm_upgrade_smoke as smoke
else:
    import vm_upgrade_smoke as smoke

MARKER = 'vm-legacy-migration-test.json'
FIX_LINE = "        ('note_id', '_old_call_logs'): 'notes',\n"


def pre_fix_source(source: str) -> str:
    """Recreate the historical omission, without injecting an exception or replacing migrations."""
    try:
        functions = [
            node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == '_migrate_owned_task_inventory'
        ]
    except SyntaxError as error:
        raise RuntimeError('The expected legacy-note repair is missing or ambiguous') from error
    if len(functions) != 1:
        raise RuntimeError('The expected legacy-note repair is missing or ambiguous')
    function = functions[0]
    lines = source.splitlines(keepends=True)
    start, end = function.lineno - 1, function.end_lineno
    original = ''.join(lines[start:end])
    if original.count(FIX_LINE) != 1:
        raise RuntimeError('The expected legacy-note repair is missing or ambiguous')
    return ''.join(lines[:start]) + original.replace(FIX_LINE, '', 1) + ''.join(lines[end:])


def legacy_task_sql(source: str) -> str:
    """Retain the actual task table definition while restoring its two legacy constraints."""
    replacements = (
        (r'(?i)(CREATE TABLE\s+)(?:"msx_tasks"|msx_tasks)',
         r'\1"_vm_legacy_tasks"'),
        (r'(?i)((?:"milestone_id"|milestone_id)\s+INTEGER)(?!\s+NOT\s+NULL)',
         r'\1 NOT NULL'),
        (r'(?i)(REFERENCES\s+)(?:"notes"|notes)(?=\s*\()', r'\1"_old_call_logs"'),
    )
    for pattern, replacement in replacements:
        source, count = re.subn(pattern, replacement, source)
        if count != 1:
            raise RuntimeError('The VM task schema differs from the expected current schema')
    return source


def seed_legacy(database: Path, fixture: dict, backup: Path) -> dict:
    """Back up and atomically reproduce valid task IDs with a dangling legacy parent name."""
    from app.db_paths import backup_database

    smoke.verify_records(fixture)
    if backup.exists():
        raise RuntimeError('The pre-fixture backup already exists; it will not be overwritten')
    if not backup_database(backup, src=database):
        raise RuntimeError('The verified pre-fixture backup failed; no schema was changed')
    with closing(sqlite3.connect(database, timeout=30)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('BEGIN IMMEDIATE')
        expected = {'customers': 1, 'notes': 2, 'milestones': 0, 'msx_tasks': 0}
        for table, count in expected.items():
            if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != count:
                raise RuntimeError('Only the original synthetic VM records may be present')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('The VM already has foreign-key errors; no fixture was installed')
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            quoted = table.replace('"', '""')
            if any(row[2] == 'msx_tasks' for row in connection.execute(
                f'PRAGMA foreign_key_list("{quoted}")',
            )):
                raise RuntimeError('Unexpected inbound task references; no schema was changed')
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('_old_call_logs', '_vm_legacy_tasks')",
        ).fetchone():
            raise RuntimeError('A legacy fixture already exists; refusing to overwrite it')
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='msx_tasks'",
        ).fetchone()[0]
        rewritten = legacy_task_sql(schema)
        objects = connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='msx_tasks' "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        milestone = connection.execute(
            'INSERT INTO milestones '
            '(msx_milestone_id, url, title, customer_id, created_at, updated_at, on_my_team) '
            'VALUES (?, ?, ?, ?, ?, ?, 0)',
            (uuid.uuid4().hex, 'https://example.invalid/vm-legacy-migration',
             'VM TEST ONLY - legacy migration', fixture['customer_id'], now, now),
        ).lastrowid
        task = connection.execute(
            'INSERT INTO msx_tasks (msx_task_id, subject, task_category, duration_minutes, '
            'is_hva, is_current_owner, note_id, milestone_id, created_at) '
            'VALUES (?, ?, 0, 60, 0, 1, ?, ?, ?)',
            (uuid.uuid4().hex, 'VM TEST ONLY - preserve legacy task links',
             fixture['note_ids'][0], milestone, now),
        ).lastrowid
        connection.execute(rewritten)
        connection.execute('INSERT INTO "_vm_legacy_tasks" SELECT * FROM msx_tasks')
        connection.execute('DROP TABLE msx_tasks')
        connection.execute('ALTER TABLE "_vm_legacy_tasks" RENAME TO msx_tasks')
        for (sql,) in objects:
            connection.execute(sql)
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        if len(violations) != 1 or violations[0][:3] != ('msx_tasks', task, '_old_call_logs'):
            raise RuntimeError('The fixture did not produce the exact legacy foreign-key damage')
        return {'task_id': task, 'milestone_id': milestone, 'note_id': fixture['note_ids'][0]}


def healthy_boot(repo: Path, revision: str) -> str:
    """Require the real desktop's current web and worker acceptance, not just an open port."""
    from scripts.upgrade import read_json, readiness_matches, startup_path

    from dotenv import dotenv_values

    port = int(dotenv_values(repo / '.env').get('PORT') or 5151)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=10) as response:
        health = json.load(response)
    status = read_json(startup_path(repo))
    boot = health.get('boot_id')
    if not boot or not readiness_matches(status, health, boot, revision):
        raise RuntimeError('The VM workspace and worker have not both reached current readiness')
    return boot


def validate(repo: Path, kit: Path) -> dict:
    """Fence this add-on to the exact original kit, local remote, database and helper payloads."""
    from scripts.upgrade import database_path

    smoke.validate_install(repo)
    state = smoke.session(repo, kit)
    manifest = json.loads((kit / 'legacy-migration-addon.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('kit_id') != state['kit_id']:
        raise RuntimeError('This legacy test belongs to a different VM kit')
    for name in ('vm_legacy_migration_smoke.py', 'vm_upgrade_smoke.py'):
        if smoke.digest(kit / name) != manifest['files'].get(name):
            raise RuntimeError('Legacy test helper verification failed')
    remote = Path(state['remote'])
    work = Path(state['work']).resolve()
    if (remote.resolve() != work / 'updates.git' or not remote.is_dir()
            or work.parent != kit.resolve()
            or Path(state['database']).resolve() != database_path(repo)):
        raise RuntimeError('The original local VM test paths no longer match')
    return state


def start(repo: Path, kit: Path, fixture: dict) -> None:
    """Stage a local historical candidate, seed the backed-up VM and request its real update."""
    from scripts.upgrade import load_state

    marker = repo / 'data' / MARKER
    if marker.exists():
        raise RuntimeError('This legacy test already started. Use step 7, not step 6 again.')
    smoke.check(repo, fixture, good=True)
    if smoke.run(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Tracked installation files changed; nothing was overwritten')
    request = repo / 'data' / 'electron-update.request'
    if request.exists():
        raise RuntimeError('Another update request is already pending')
    original = load_state(repo)
    boot = healthy_boot(repo, fixture['good'])
    work = Path(fixture['work']) / ('legacy-migration-' + uuid.uuid4().hex[:8])
    work.mkdir()
    checkout = work / 'candidate'
    smoke.run(repo, 'worktree', 'add', '-b', f'feature/vm-{work.name}',
              str(checkout), fixture['good'])
    try:
        migration = checkout / 'app' / 'migrations.py'
        migration.write_text(pre_fix_source(migration.read_text(encoding='utf-8')),
                             encoding='utf-8')
        target = smoke.commit_files(
            checkout, ['app/migrations.py'], 'test: VM-only historical legacy migration failure',
            work / 'paths.txt',
        )
    finally:
        smoke.run(repo, 'worktree', 'remove', str(checkout))
    remote = Path(fixture['remote'])
    old_remote = smoke.run(repo, '--git-dir', str(remote), 'rev-parse', 'refs/heads/main')
    if old_remote != fixture['good']:
        raise RuntimeError('The local test remote is not on the completed good update')
    smoke.run(repo, '--git-dir', str(remote), 'fetch', str(repo), target)
    backup = work / 'before-legacy-fixture.db'
    state = {
        'schema_version': 1, 'kit_id': fixture['kit_id'], 'repo': str(repo),
        'baseline': fixture['good'], 'target': target, 'previous_attempt': original['attempt_id'],
        'previous_boot': boot, 'shell_sha256': smoke.digest(
            repo / 'electron-dist' / 'resources' / 'app.asar'),
        'backup': str(backup), 'stage': 'preparing',
    }
    smoke.write_json(marker, state)
    state.update(seed_legacy(Path(fixture['database']), fixture, backup))
    state['stage'] = 'seeded'
    smoke.write_json(marker, state)
    smoke.run(repo, '--git-dir', str(remote), 'update-ref', 'refs/heads/main',
              target, old_remote)
    state['stage'] = 'requested'
    smoke.write_json(marker, state)
    request.write_text('update\n', encoding='utf-8')
    print('Legacy task fixture installed, with a verified database backup.')
    print('Real desktop update requested. Expect automatic restoration, not a startup loop.')
    print('After Continue working and the workspace opens, run 7-CHECK-LEGACY-ROLLBACK.cmd.')


def verify_task(database: Path, state: dict, *, legacy: bool) -> None:
    """Prove task and canonical relationships survive, with exactly the expected FK shape."""
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        row = connection.execute(
            'SELECT note_id, milestone_id FROM msx_tasks WHERE id=?', (state['task_id'],),
        ).fetchone()
        if row != (state['note_id'], state['milestone_id']):
            raise RuntimeError('The legacy task or its note/milestone links were lost')
        parents = {row[3]: row[2] for row in connection.execute('PRAGMA foreign_key_list(msx_tasks)')}
        nullable = next(row[3] == 0 for row in connection.execute(
            'PRAGMA table_info(msx_tasks)',
        ) if row[1] == 'milestone_id')
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        expected_parent = '_old_call_logs' if legacy else 'notes'
        if parents.get('note_id') != expected_parent or nullable == legacy:
            raise RuntimeError('The task schema does not match the expected legacy/repair state')
        if legacy:
            if len(violations) != 1 or violations[0][:3] != (
                'msx_tasks', state['task_id'], '_old_call_logs',
            ):
                raise RuntimeError('The saved snapshot does not contain the original legacy fault')
        elif violations:
            raise RuntimeError('The restored database still has foreign-key errors')


def check(repo: Path, fixture: dict, *, restart: bool = False) -> None:
    """Match fresh migration-failure evidence, restored records and actual accepted boot identity."""
    from scripts.upgrade import load_state, read_json

    marker = repo / 'data' / MARKER
    state = read_json(marker)
    if (not state or state.get('schema_version') != 1
            or state.get('repo') != str(repo) or state.get('kit_id') != fixture['kit_id']
            or state.get('baseline') != fixture['good']
            or state.get('stage') not in {'requested', 'checked'}):
        raise RuntimeError('The legacy test has not finished requesting its update')
    upgrade = load_state(repo)
    if (not upgrade or upgrade['attempt_id'] == state['previous_attempt']
            or upgrade.get('target_commit') != state['target']
            or upgrade.get('previous_commit') != state['baseline']
            or upgrade.get('phase') != 'restored' or upgrade.get('notification_pending')
            or not upgrade.get('acknowledged_at') or not upgrade.get('report_queued_at')):
        raise RuntimeError('Finish this legacy test recovery and Continue working before checking')
    failure = upgrade.get('original_failure') or {}
    if (failure.get('failed_phase') != 'rehearsing'
            or failure.get('error', {}).get('category') != 'migration_foreign_keys'
            or failure.get('error', {}).get('parent_tables') != ['_old_call_logs']):
        raise RuntimeError('The actual historical migration failure was not reached')
    queued = datetime.fromisoformat(upgrade['report_queued_at'])
    dismissed = datetime.fromisoformat(upgrade['acknowledged_at'])
    report_path = Path(fixture['database']).parent / 'diagnostic-outbox' / (
        upgrade['attempt_id'] + '-report.json'
    )
    report = read_json(report_path) or {}
    reported_failure = report.get('payload', {}).get('report', {}).get('upgrade', {})
    if (queued.tzinfo is None or dismissed.tzinfo is None or queued > dismissed
            or report.get('report_id') != upgrade['attempt_id']
            or reported_failure.get('phase') != 'rehearsing'
            or reported_failure.get('error', {}).get('parent_tables') != ['_old_call_logs']):
        raise RuntimeError('The original migration failure was not durably queued before dismissal')
    point = Path(fixture['database']).parent / 'upgrade-recovery' / upgrade['attempt_id']
    trial = read_json(point / 'trial' / 'rehearsal-result.json') or {}
    if (trial.get('success') is not False
            or trial.get('error', {}).get('category') != 'migration_foreign_keys'
            or trial.get('error', {}).get('parent_tables') != ['_old_call_logs']
            or not any(frame.get('function') == '_rebuild_sqlite_table'
                       for frame in trial.get('error', {}).get('frames', []))
            or (point / 'live-result.json').exists()):
        raise RuntimeError('Missing genuine SQLite rehearsal failure or live migration was reached')
    if (smoke.run(repo, 'rev-parse', 'HEAD') != state['baseline']
            or smoke.digest(repo / 'electron-dist' / 'resources' / 'app.asar')
            != state['shell_sha256']):
        raise RuntimeError('The known-good application and desktop were not restored exactly')
    smoke.verify_records(fixture)
    verify_task(point / 'database.db', state, legacy=True)
    verify_task(point / 'failed-state' / 'database.db', state, legacy=True)
    smoke.verify_records({**fixture, 'database': str(point / 'database.db')})
    smoke.verify_records({**fixture, 'database': str(point / 'failed-state' / 'database.db')})
    verify_task(Path(fixture['database']), state, legacy=False)
    boot = healthy_boot(repo, state['baseline'])
    if boot == state['previous_boot']:
        raise RuntimeError('The checker is seeing the original pre-update boot')
    if restart:
        if state.get('restart_boot') == boot or not state.get('restart_boot'):
            raise RuntimeError('A new desktop/worker boot has not completed after restart')
        if upgrade['attempt_id'] != state.get('checked_attempt'):
            raise RuntimeError('Restart unexpectedly created a new recovery attempt')
        print('PASS: a second clean startup completed without a migration/recovery loop.')
    else:
        state.update(stage='checked', checked_attempt=upgrade['attempt_id'], restart_boot=boot)
        smoke.write_json(marker, state)
    print('PASS: real legacy migration failed on rehearsal, before live migration or candidate startup.')
    print('PASS: previous version restored; both notes and legacy task links survive; all FKs are valid.')
    print('PASS: diagnostics were queued before dismissal; current web and worker are healthy.')


def main() -> int:
    """Dispatch explicitly confirmed disposable-VM actions, surfacing every error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'check', 'check-restart'))
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--confirm-vm', action='store_true', required=True)
    args = parser.parse_args()
    repo, kit = args.repo.resolve(), args.kit.resolve()
    sys.path.insert(0, str(repo))
    fixture = validate(repo, kit)
    if args.action == 'start':
        start(repo, kit, fixture)
    else:
        check(repo, fixture, restart=args.action == 'check-restart')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'LEGACY VM TEST STOPPED: {error}', file=sys.stderr, flush=True)
        traceback.print_exc()
        raise SystemExit(1)
