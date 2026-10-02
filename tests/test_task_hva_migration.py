"""Regression coverage for legacy and partially migrated task databases."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.migrations import _migrate_msx_task_hva_column


@pytest.fixture
def migration_db(tmp_path):
    """Provide a WAL-enabled isolated database with task links and custom schema."""
    engine = create_engine(f'sqlite:///{tmp_path / "tasks.db"}', poolclass=StaticPool)
    session = scoped_session(sessionmaker(bind=engine))
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA journal_mode=WAL')
        raw.execute('PRAGMA wal_autocheckpoint=0')
        raw.execute('PRAGMA foreign_keys=ON')
        raw.executescript("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY);
            CREATE TABLE milestones (id INTEGER PRIMARY KEY);
            INSERT INTO milestones VALUES (3);
        """)
    yield SimpleNamespace(engine=engine, session=session, directory=tmp_path)
    session.remove()
    engine.dispose()


def seed_tasks(database, classification_sql: str) -> None:
    """Seed stable task IDs, inbound links, indexes, and a trigger."""
    with database.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.executescript(f"""
            CREATE TABLE msx_tasks (
                id INTEGER PRIMARY KEY,
                msx_task_id TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL,
                milestone_id INTEGER NOT NULL REFERENCES milestones(id),
                {classification_sql},
                extra_value TEXT DEFAULT 'keep me'
            );
            CREATE INDEX ix_task_subject ON msx_tasks(subject);
            CREATE TABLE task_links (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES msx_tasks(id)
            );
            CREATE TRIGGER task_link_guard BEFORE DELETE ON msx_tasks
            WHEN EXISTS (SELECT 1 FROM task_links WHERE task_id = OLD.id)
            BEGIN SELECT RAISE(ABORT, 'task is linked'); END;
        """)


@pytest.mark.parametrize('partial', [False, True])
def test_upgrade_preserves_values_links_schema_backup_and_new_inserts(migration_db, partial):
    """Both deployed schema shapes retain rows and support HVA-only inserts."""
    classification = 'is_hok BOOLEAN NOT NULL'
    if partial:
        classification += ', is_hva BOOLEAN NOT NULL DEFAULT 0'
    seed_tasks(migration_db, classification)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        if partial:
            raw.execute(
                "INSERT INTO msx_tasks VALUES (7, 'old-task', 'Demo', 3, 0, 1, 'custom')"
            )
        else:
            raw.execute(
                "INSERT INTO msx_tasks VALUES (7, 'old-task', 'Demo', 3, 1, 'custom')"
            )
        raw.execute("INSERT INTO task_links VALUES (9, 7)")
        raw.commit()
        before_schema = raw.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall()
        before_rows = raw.execute('SELECT * FROM msx_tasks').fetchall()

    inspector = inspect(migration_db.engine)
    _migrate_msx_task_hva_column(migration_db, inspector)
    assert 'is_hok' not in {c['name'] for c in inspector.get_columns('msx_tasks')}
    assert 'is_hva' in {c['name'] for c in inspector.get_columns('msx_tasks')}
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute(
            'SELECT id, msx_task_id, subject, milestone_id, is_hva, extra_value FROM msx_tasks'
        ).fetchall() == [(7, 'old-task', 'Demo', 3, 1, 'custom')]
        assert raw.execute('SELECT * FROM task_links').fetchall() == [(9, 7)]
        assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall() == [('task_link_guard',)]
        raw.execute(
            "INSERT INTO msx_tasks (msx_task_id, subject, milestone_id, is_hva) "
            "VALUES ('new-task', 'Assessment', 3, 1)"
        )
        raw.commit()
    assert {i['name'] for i in inspector.get_indexes('msx_tasks')} == {'ix_task_subject'}
    assert inspector.get_foreign_keys('task_links')[0]['referred_table'] == 'msx_tasks'
    assert inspector.get_unique_constraints('msx_tasks')[0]['column_names'] == ['msx_task_id']

    backups = list(migration_db.directory.glob('*.bak'))
    assert len(backups) == 1
    with closing(sqlite3.connect(str(backups[0]))) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT * FROM msx_tasks').fetchall() == before_rows
        assert backup.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall() == before_schema
        assert backup.execute('SELECT * FROM task_links').fetchall() == [(9, 7)]

    with migration_db.engine.connect() as connection:
        schema = connection.execute(text('SELECT * FROM sqlite_master ORDER BY name')).fetchall()
        rows = connection.execute(text('SELECT * FROM msx_tasks ORDER BY id')).fetchall()
    _migrate_msx_task_hva_column(migration_db, inspector)
    assert len(list(migration_db.directory.glob('*.bak'))) == 1
    with migration_db.engine.connect() as connection:
        assert connection.execute(
            text('SELECT * FROM sqlite_master ORDER BY name')
        ).fetchall() == schema
        assert connection.execute(text('SELECT * FROM msx_tasks ORDER BY id')).fetchall() == rows


def test_backup_failure_prevents_schema_changes(migration_db, monkeypatch):
    """Never change the schema if the required backup fails."""
    seed_tasks(migration_db, 'is_hok BOOLEAN NOT NULL, is_hva BOOLEAN NOT NULL DEFAULT 0')
    monkeypatch.setattr('app.db_paths.backup_database', lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match='rolled back') as error:
        _migrate_msx_task_hva_column(migration_db, inspect(migration_db.engine))
    assert 'backup failed verification' in str(error.value.__cause__)
    assert 'is_hok' in {
        c['name'] for c in inspect(migration_db.engine).get_columns('msx_tasks')
    }


def test_dependent_legacy_index_aborts_safely(migration_db):
    """Unexpected schema dependencies abort with a backup instead of discarding them."""
    seed_tasks(migration_db, 'is_hok BOOLEAN NOT NULL, is_hva BOOLEAN NOT NULL DEFAULT 0')
    with migration_db.engine.connect() as connection:
        connection.execute(text('CREATE INDEX ix_custom_legacy ON msx_tasks(is_hok)'))
        connection.commit()
    with pytest.raises(RuntimeError, match='rolled back'):
        _migrate_msx_task_hva_column(migration_db, inspect(migration_db.engine))
    assert 'is_hok' in {
        c['name'] for c in inspect(migration_db.engine).get_columns('msx_tasks')
    }
    assert len(list(migration_db.directory.glob('*.bak'))) == 1
    with migration_db.engine.connect() as connection:
        assert connection.execute(text('PRAGMA integrity_check')).scalar() == 'ok'


@pytest.mark.parametrize('classification', ['is_hva BOOLEAN NOT NULL DEFAULT 0', 'other BOOLEAN'])
def test_current_and_missing_classification_schemas(migration_db, classification):
    """Fresh schemas are a no-op and missing classification receives the current field."""
    seed_tasks(migration_db, classification)
    _migrate_msx_task_hva_column(migration_db, inspect(migration_db.engine))
    assert 'is_hva' in {
        c['name'] for c in inspect(migration_db.engine).get_columns('msx_tasks')
    }
    assert list(migration_db.directory.glob('*.bak')) == []


def test_in_memory_upgrade():
    """In-memory databases use a verified in-memory backup."""
    engine = create_engine('sqlite://', poolclass=StaticPool)
    session = scoped_session(sessionmaker(bind=engine))
    database = SimpleNamespace(engine=engine, session=session)
    try:
        with engine.connect() as connection:
            connection.execute(text('CREATE TABLE msx_tasks (id INTEGER, is_hok BOOLEAN NOT NULL)'))
            connection.execute(text('INSERT INTO msx_tasks VALUES (1, 1)'))
            connection.commit()
        _migrate_msx_task_hva_column(database, inspect(engine))
        with engine.connect() as connection:
            assert connection.execute(text('SELECT id, is_hva FROM msx_tasks')).fetchall() == [(1, 1)]
    finally:
        session.remove()
        engine.dispose()


def test_concurrent_startup_rechecks_schema_under_lock(migration_db, monkeypatch):
    """Concurrent web/worker migrations both succeed without acting on stale columns."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app import db_paths

    seed_tasks(migration_db, 'is_hok BOOLEAN NOT NULL, is_hva BOOLEAN NOT NULL DEFAULT 0')
    backup_database = db_paths.backup_database
    barrier = Barrier(2)

    def synchronized_backup(*args, **kwargs):
        """Make both processes finish their backups before either changes the schema."""
        result = backup_database(*args, **kwargs)
        barrier.wait(timeout=10)
        return result

    def migrate():
        """Run one independent startup connection against the shared isolated file."""
        engine = create_engine(migration_db.engine.url)
        session = scoped_session(sessionmaker(bind=engine))
        try:
            database = SimpleNamespace(engine=engine, session=session)
            _migrate_msx_task_hva_column(database, inspect(engine))
        finally:
            session.remove()
            engine.dispose()

    monkeypatch.setattr(db_paths, 'backup_database', synchronized_backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    assert 'is_hok' not in {
        c['name'] for c in inspect(migration_db.engine).get_columns('msx_tasks')
    }
    assert len(list(migration_db.directory.glob('*.bak'))) == 2
