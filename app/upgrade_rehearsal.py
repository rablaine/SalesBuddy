"""Execute real schema initialization offline, without launching services or schedulers."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Flask
from sqlalchemy import event

from app.models import db
from app.services.database_startup import database_startup_lock, initialize_database
from scripts.upgrade import CORE_TABLES, _schema_fingerprint, atomic_json, diagnostic_error


def _inventory(database: Path) -> tuple[dict, set]:
    """Capture core identities and existing FK violations in memory, never in reports."""
    if not database.exists():
        return {}, set()
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        inventory = {}
        for canonical in CORE_TABLES:
            table = 'call_logs' if canonical == 'notes' and 'notes' not in tables else canonical
            if table in tables:
                inventory[canonical] = {
                    row[0] for row in connection.execute(f'SELECT id FROM "{table}"')
                }
        violations = set(connection.execute('PRAGMA foreign_key_check').fetchall())
    return inventory, violations


def _core_rows(database: Path) -> dict:
    """Keep core values in memory to detect non-idempotent second-pass migrations."""
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        return {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            for table in CORE_TABLES if table in tables
        }


def _valid_links(database: Path, identities: dict) -> dict:
    """Record existing valid core relationships without retaining their contents on disk."""
    if not database.exists():
        return {}
    links = {}
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        for canonical in CORE_TABLES:
            table = 'call_logs' if canonical == 'notes' and 'notes' not in tables else canonical
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for column, parent in (
                ('customer_id', 'customers'), ('note_id', 'notes'),
                ('milestone_id', 'milestones'), ('engagement_id', 'engagements'),
            ):
                if column in columns:
                    for identity, value in connection.execute(
                        f'SELECT id, "{column}" FROM "{table}"',
                    ):
                        if value is not None and value in identities.get(parent, set()):
                            links[canonical, identity, column] = value
    return links


def initialize_offline(database: Path, *, repeat: bool = True) -> dict:
    """Rehearse the exact startup schema pipeline and assert identity/FK preservation."""
    before, existing_violations = _inventory(database)
    before_links = _valid_links(database, before)
    application = Flask(__name__)
    application.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI=f'sqlite:///{database}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    with application.app_context():
        engine = db.engine

        @event.listens_for(engine, 'connect')
        def enable_foreign_keys(connection, _record) -> None:
            """Exercise migrations with actual foreign-key enforcement enabled."""
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=10000')

        try:
            with database_startup_lock(engine):
                initialize_database(db)
                after, violations = _inventory(database)
                for table, ids in before.items():
                    if not ids.issubset(after.get(table, set())):
                        raise RuntimeError(f'Upgrade rehearsal lost core identities in {table}')
                after_links = _valid_links(database, after)
                if any(after_links.get(key) != value for key, value in before_links.items()):
                    raise RuntimeError('Upgrade rehearsal changed an existing core relationship')
                invalid = {
                    row for row in violations
                    if row not in existing_violations or row[0] in CORE_TABLES
                    or row[2] in CORE_TABLES
                }
                if invalid:
                    raise RuntimeError(f'Upgrade rehearsal foreign key violations: {sorted(invalid)}')
                if repeat:
                    contents = _core_rows(database)
                    schema = _schema_fingerprint(database)
                    initialize_database(db)
                    repeated, repeated_violations = _inventory(database)
                    if (repeated != after or repeated_violations != violations
                            or _core_rows(database) != contents
                            or _schema_fingerprint(database) != schema):
                        raise RuntimeError('Upgrade rehearsal is not repeatable')
                return {'success': True, 'repeated': repeat, 'core_counts': {
                    table: len(ids) for table, ids in after.items()
                }, 'preexisting_fk_violations': len(existing_violations),
                    'remaining_fk_violations': len(violations)}
        finally:
            db.session.remove()
            engine.dispose()


def main() -> int:
    """Write a sanitized result even when a migration raises before Flask can serve."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    try:
        result = initialize_offline(args.database.resolve(), repeat=not args.live)
    except Exception as error:
        atomic_json(args.result, {'success': False, 'error': diagnostic_error(error)})
        raise
    atomic_json(args.result, result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
