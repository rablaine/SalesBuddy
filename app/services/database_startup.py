"""Serialize SQLite schema initialization across web and worker processes."""

import errno
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import Engine
from flask_sqlalchemy import SQLAlchemy

logger = logging.getLogger(__name__)
STARTUP_LOCK_TIMEOUT_SECONDS = 300


def initialize_database(db: SQLAlchemy) -> None:
    """Initialize schema and canonical records inside the caller's startup lock.

    The caller owns the Flask application context and startup lock. This helper
    deliberately does not reconcile shell files or start background services so
    the upgrade rehearsal can safely use it against an isolated database.
    """
    from app.migrations import run_migrations, run_table_renames
    from app.models import User, UserPreference
    from app.services.dashboard_templates import initialize_dashboard_templates

    run_table_renames(db)
    db.create_all()
    run_migrations(db)
    initialize_dashboard_templates()
    if db.session.get(User, 1) is None:
        db.session.add(User(
            id=1, email='user@localhost', name='Local User', is_admin=True,
        ))
        db.session.commit()
    if UserPreference.query.first() is None:
        db.session.add(UserPreference())
        db.session.commit()


@contextmanager
def database_startup_lock(
    engine: Engine, timeout_seconds: float = STARTUP_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold an OS-managed schema lock, including backups, until startup finishes.

    Lock the sidecar rather than SQLite itself so online backups can use their
    own read connection. The OS releases the lock if a process crashes.
    """
    database = engine.url.database
    if engine.dialect.name != 'sqlite' or not database or database == ':memory:':
        yield
        return

    lock_path = Path(database).resolve().with_suffix(Path(database).suffix + '.startup.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        waiting_logged = False
        try:
            while not acquired:
                handle.seek(0)
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not waiting_logged:
                        logger.info('Waiting for database startup lock: %s', lock_path)
                        waiting_logged = True
                    if time.monotonic() >= deadline:
                        logger.error('Database startup lock timed out: %s', lock_path)
                        raise RuntimeError(
                            f'Database initialization is still running in another process: '
                            f'{lock_path}'
                        ) from error
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
