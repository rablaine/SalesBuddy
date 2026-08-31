"""Manual SQLite database maintenance operations."""

import sqlite3
import threading
from pathlib import Path


_VACUUM_LOCK = threading.Lock()
DEFAULT_MIN_RECLAIM_BYTES = 50 * 1024 * 1024
DEFAULT_MIN_FREE_PERCENT = 10.0


class VacuumInProgressError(RuntimeError):
    """Raised when another database compaction is already running."""


def _database_disk_bytes(db_path: Path) -> int:
    """Return bytes used by the database and its WAL sidecars."""
    return sum(
        path.stat().st_size
        for path in (
            db_path,
            db_path.with_name(db_path.name + '-wal'),
            db_path.with_name(db_path.name + '-shm'),
        )
        if path.exists()
    )


def database_free_space(db_path: Path, timeout_seconds: int = 10) -> dict:
    """Return reclaimable SQLite free-page metrics without changing the database."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f'Database not found at {db_path}')

    connection = sqlite3.connect(str(db_path), timeout=timeout_seconds)
    try:
        connection.execute(f'PRAGMA busy_timeout={timeout_seconds * 1000}')
        page_size = int(connection.execute('PRAGMA page_size').fetchone()[0])
        page_count = int(connection.execute('PRAGMA page_count').fetchone()[0])
        free_pages = int(connection.execute('PRAGMA freelist_count').fetchone()[0])
    finally:
        connection.close()

    return {
        'page_size': page_size,
        'page_count': page_count,
        'free_pages': free_pages,
        'reclaimable_bytes': free_pages * page_size,
        'free_percent': round(
            (free_pages / page_count * 100) if page_count else 0,
            1,
        ),
    }


def should_vacuum_database(
    metrics: dict,
    min_reclaim_bytes: int = DEFAULT_MIN_RECLAIM_BYTES,
    min_free_percent: float = DEFAULT_MIN_FREE_PERCENT,
) -> bool:
    """Return whether free pages justify rewriting the database."""
    return (
        metrics['reclaimable_bytes'] >= min_reclaim_bytes
        and metrics['free_percent'] >= min_free_percent
    )


def vacuum_database(db_path: Path, timeout_seconds: int = 60) -> dict:
    """Compact a SQLite database and return before-and-after metrics."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f'Database not found at {db_path}')
    if not _VACUUM_LOCK.acquire(blocking=False):
        raise VacuumInProgressError('Database compaction is already running')

    try:
        before_bytes = _database_disk_bytes(db_path)
        free_space = database_free_space(db_path, timeout_seconds)
        connection = sqlite3.connect(str(db_path), timeout=timeout_seconds)
        try:
            connection.execute(f'PRAGMA busy_timeout={timeout_seconds * 1000}')
            connection.execute('VACUUM')
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError(f'Database integrity check failed: {integrity}')
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            connection.close()

        after_bytes = _database_disk_bytes(db_path)
        return {
            'before_bytes': before_bytes,
            'after_bytes': after_bytes,
            'reclaimed_bytes': max(0, before_bytes - after_bytes),
            'free_pages_before': free_space['free_pages'],
            'free_percent_before': free_space['free_percent'],
            'page_size': free_space['page_size'],
        }
    finally:
        _VACUUM_LOCK.release()