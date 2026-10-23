"""Verified, reusable SQLite migration backups with successful-startup retention."""

import hashlib
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
MIGRATION_BACKUP_KEEP = 2
MIGRATION_BACKUP_LABELS = frozenset({
    'initiative-projects', 'owned-tasks', 'nullable-column', 'note-datetime',
    'task-note-nullable', 'customer-tpid-type', 'customer-tpid', 'single-user',
    'task-hva', 'revenue-parents', 'estimated-acr',
})


def _backup_candidates(database: Path) -> dict[str, list[Path]]:
    """Find only this database's recognized migration snapshots, never recovery directories."""
    labels = '|'.join(re.escape(label) for label in sorted(MIGRATION_BACKUP_LABELS))
    pattern = re.compile(
        rf'{re.escape(database.name)}\.({labels})-(\d{{8}}T\d{{12}}Z)-[0-9a-f]{{32}}\.bak',
    )
    groups: dict[str, list[Path]] = {}
    for path in database.parent.iterdir():
        match = pattern.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_nlink != 1 or any(
            Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')
        ):
            continue
        try:
            datetime.strptime(match[2], '%Y%m%dT%H%M%S%fZ')
        except ValueError:
            continue
        groups.setdefault(match[1], []).append(path)
    for paths in groups.values():
        paths.sort(key=lambda path: path.name, reverse=True)
    return groups


def _database_digest(connection: sqlite3.Connection) -> str:
    """Hash schema, values and accessible row identities within the caller's read snapshot."""
    digest = hashlib.sha256()
    schema = connection.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name',
    ).fetchall()
    digest.update(repr(schema).encode('utf-8'))
    for pragma in ('user_version', 'application_id', 'encoding'):
        digest.update(repr(connection.execute(f'PRAGMA {pragma}').fetchall()).encode('utf-8'))
    without_rowid = {
        row[1] for row in connection.execute('PRAGMA main.table_list') if row[4]
    }
    for kind, name, _, _ in schema:
        if kind != 'table':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        columns = {
            row[1].lower() for row in connection.execute(f'PRAGMA table_xinfo({quoted})')
        }
        rowid = next(
            (alias for alias in ('_rowid_', 'rowid', 'oid') if alias not in columns), None,
        )
        selection = f'{rowid},*' if rowid and name not in without_rowid else '*'
        digest.update(repr(name).encode('utf-8'))
        for row in connection.execute(f'SELECT {selection} FROM {quoted}'):
            digest.update(repr(row).encode('utf-8'))
            digest.update(b'\n')
    return digest.hexdigest()


def _verify_integrity(connection: sqlite3.Connection) -> None:
    """Reject incomplete or corrupt snapshots rather than treating them as recovery evidence."""
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise sqlite3.DatabaseError('Migration backup failed integrity verification')


def _readonly(database: Path, *, snapshot: bool = False) -> sqlite3.Connection:
    """Read a live database normally, or a self-contained snapshot without creating sidecars."""
    options = '?mode=ro&immutable=1' if snapshot else '?mode=ro'
    return sqlite3.connect(database.resolve().as_uri() + options, uri=True)


def create_migration_backup(connection: sqlite3.Connection, label: str) -> Path | None:
    """Reuse an identical verified snapshot, or create one including committed WAL contents.

    Callers must hold the shared startup lock and have no active transaction.
    A read transaction keeps fingerprinting and online backup on the same snapshot.
    """
    if label not in MIGRATION_BACKUP_LABELS:
        raise ValueError(f'Unregistered migration backup label: {label}')
    if connection.in_transaction:
        raise RuntimeError('Migration backup requires a committed database connection')
    filename = next(
        row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main'
    )
    if not filename:
        with closing(sqlite3.connect(':memory:')) as saved:
            connection.backup(saved)
            _verify_integrity(saved)
        return None

    database = Path(filename)
    connection.execute('BEGIN')
    created: Path | None = None
    try:
        expected = _database_digest(connection)
        candidates = _backup_candidates(database).get(label, [])
        for candidate in candidates[:MIGRATION_BACKUP_KEEP]:
            try:
                with closing(_readonly(candidate, snapshot=True)) as saved:
                    saved.execute('BEGIN')
                    _verify_integrity(saved)
                    if _database_digest(saved) == expected:
                        logger.info('Reusing verified %s migration backup: %s', label, candidate)
                        return candidate
            except (OSError, sqlite3.DatabaseError):
                logger.warning('Cannot reuse migration backup: %s', candidate, exc_info=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target = database.with_name(f'{database.name}.{label}-{stamp}-{uuid4().hex}.bak')
        target.touch(exist_ok=False)
        created = target
        with closing(sqlite3.connect(str(target))) as saved:
            connection.backup(saved)
            saved.execute('PRAGMA journal_mode=DELETE')
            _verify_integrity(saved)
            if _database_digest(saved) != expected:
                raise RuntimeError('Migration backup contents do not match the source snapshot')
        logger.info('Created verified %s migration backup: %s', label, target)
        return target
    except Exception:
        if created is not None:
            for path in (
                created, Path(str(created) + '-journal'),
                Path(str(created) + '-wal'), Path(str(created) + '-shm'),
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.exception('Could not remove incomplete migration backup: %s', path)
        raise
    finally:
        connection.rollback()


def prune_migration_backups(engine: Engine) -> int:
    """After successful initialization, keep two verified snapshots per migration type.

    Only recognized loose migration backups beside this exact database are eligible.
    Cleanup errors are logged without turning a successful startup into a failure.
    """
    filename = engine.url.database
    if engine.dialect.name != 'sqlite' or not filename or filename == ':memory:':
        return 0
    removed = 0
    reclaimed = 0
    try:
        database = Path(filename)
        groups = _backup_candidates(database)
        if not any(len(paths) > MIGRATION_BACKUP_KEEP for paths in groups.values()):
            return 0
        with closing(_readonly(database)) as current:
            _verify_integrity(current)
        for label, paths in groups.items():
            if len(paths) <= MIGRATION_BACKUP_KEEP:
                continue
            keep: set[Path] = set()
            for path in paths:
                try:
                    with closing(_readonly(path, snapshot=True)) as saved:
                        _verify_integrity(saved)
                    keep.add(path)
                except (OSError, sqlite3.DatabaseError):
                    logger.warning('Migration backup is not verifiable: %s', path, exc_info=True)
                if len(keep) == MIGRATION_BACKUP_KEEP:
                    break
            if len(keep) < MIGRATION_BACKUP_KEEP:
                logger.error('Keeping all %s backups: fewer than two verified snapshots', label)
                continue
            for path in paths:
                if path in keep:
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    reclaimed += size
                except OSError:
                    logger.exception('Could not remove old migration backup: %s', path)
        if removed:
            logger.info('Removed %d migration backups, reclaiming %d bytes', removed, reclaimed)
    except (OSError, sqlite3.DatabaseError):
        logger.exception('Migration backup retention could not finish; backups were retained')
    return removed
