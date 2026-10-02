"""Regression tests for cross-process SQLite startup coordination."""

import multiprocessing
from contextlib import closing
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker

from app.migrations import _migrate_msx_task_hva_column
from app.services.database_startup import database_startup_lock


def _upgrade_in_process(url, acquired, release) -> None:
    """Run a real upgrade in a separate process, pausing while holding the lock."""
    engine = create_engine(url, connect_args={'timeout': 0.001})
    session = scoped_session(sessionmaker(bind=engine))
    try:
        with database_startup_lock(engine):
            acquired.set()
            if not release.wait(15):
                raise RuntimeError('Test did not release the startup lock')
            database = SimpleNamespace(engine=engine, session=session)
            _migrate_msx_task_hva_column(database, inspect(engine))
    finally:
        session.remove()
        engine.dispose()


def test_processes_serialize_upgrade_and_backup(tmp_path, caplog):
    """A second startup waits outside SQLite and does not repeat the backup."""
    import sqlite3

    db_path = tmp_path / 'tasks.db'
    url = f'sqlite:///{db_path}'
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(
            'CREATE TABLE customers (id INTEGER PRIMARY KEY);'
            'CREATE TABLE msx_tasks (id INTEGER PRIMARY KEY, is_hok BOOLEAN NOT NULL, '
            'is_hva BOOLEAN NOT NULL DEFAULT 0);'
            'INSERT INTO msx_tasks VALUES (1, 0, 1);'
        )
    context = multiprocessing.get_context('spawn')
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_upgrade_in_process, args=(url, acquired, release))
    process.start()
    engine = create_engine(url, connect_args={'timeout': 0.001})
    session = scoped_session(sessionmaker(bind=engine))
    try:
        assert acquired.wait(15)
        with caplog.at_level('INFO'):
            with pytest.raises(RuntimeError, match='another process'):
                with database_startup_lock(engine, timeout_seconds=0.15):
                    pytest.fail('Another process still holds the schema lock')
        assert 'Waiting for database startup lock' in caplog.text
        assert 'Database startup lock timed out' in caplog.text
        release.set()
        with database_startup_lock(engine, timeout_seconds=15):
            database = SimpleNamespace(engine=engine, session=session)
            _migrate_msx_task_hva_column(database, inspect(engine))
        process.join(15)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert len(list(tmp_path.glob('*.bak'))) == 1
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute('SELECT id, is_hva FROM msx_tasks').fetchall() == [(1, 1)]
            assert 'is_hok' not in {
                row[1] for row in connection.execute('PRAGMA table_info(msx_tasks)')
            }
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        release.set()
        process.join(15)
        session.remove()
        engine.dispose()


def test_lock_released_after_startup_error(tmp_path):
    """A failed startup releases its OS lock without deleting the shared sidecar."""
    engine = create_engine(f'sqlite:///{tmp_path / "tasks.db"}')
    try:
        with pytest.raises(ValueError, match='startup failed'):
            with database_startup_lock(engine):
                raise ValueError('startup failed')
        with database_startup_lock(engine, timeout_seconds=0):
            assert (tmp_path / 'tasks.db.startup.lock').exists()
    finally:
        engine.dispose()


def test_memory_database_does_not_create_sidecar(tmp_path, monkeypatch):
    """Isolated in-memory test databases do not need interprocess coordination."""
    monkeypatch.chdir(tmp_path)
    engine = create_engine('sqlite:///:memory:')
    try:
        with database_startup_lock(engine):
            assert list(tmp_path.iterdir()) == []
    finally:
        engine.dispose()
