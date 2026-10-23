"""Regression coverage for legacy and partially migrated task databases."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.migrations import _migrate_msx_task_hva_column, _migrate_owned_task_inventory


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


def seed_tasks(database, classification_sql: str, milestone_nullable: bool = False) -> None:
    """Seed stable task IDs, inbound links, indexes, and a trigger."""
    with database.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.executescript(f"""
            CREATE TABLE msx_tasks (
                id INTEGER PRIMARY KEY,
                msx_task_id TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL,
                milestone_id INTEGER {'NULL' if milestone_nullable else 'NOT NULL'}
                    REFERENCES milestones(id),
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


@pytest.mark.parametrize("nullable", [False, True])
@pytest.mark.parametrize("missing_parent", [False, True])
@pytest.mark.parametrize("legacy_milestone", [False, True])
@pytest.mark.parametrize("note_parent", ["call_logs_backup", "_old_call_logs"])
def test_owned_inventory_repairs_legacy_parent_names_only_when_ids_are_valid(
    migration_db, nullable, missing_parent, legacy_milestone, note_parent,
):
    """Old rename damage is corrected without losing IDs or hiding genuinely orphaned links."""
    seed_tasks(migration_db, 'is_hva BOOLEAN NOT NULL DEFAULT 0', nullable)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        if legacy_milestone:
            raw.executescript("""
                ALTER TABLE milestones RENAME TO _old_milestones;
                CREATE TABLE milestones (id INTEGER PRIMARY KEY);
                INSERT INTO milestones VALUES (3);
                DROP TABLE _old_milestones;
            """)
        raw.executescript(f"""
            CREATE TABLE call_logs (id INTEGER PRIMARY KEY);
            ALTER TABLE msx_tasks ADD COLUMN note_id INTEGER REFERENCES call_logs(id);
            ALTER TABLE call_logs RENAME TO "{note_parent}";
            DROP TABLE "{note_parent}";
            CREATE TABLE notes (id INTEGER PRIMARY KEY);
            INSERT INTO notes VALUES (31);
        """)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO msx_tasks "
                    "(id, msx_task_id, subject, milestone_id, is_hva, note_id) "
                    "VALUES (7, 'legacy', 'Preserve links', 3, 1, 31)")
        raw.execute("INSERT INTO task_links VALUES (9, 7)")
        if missing_parent:
            raw.execute("DELETE FROM notes WHERE id=31")
        raw.commit()
        raw.execute("PRAGMA foreign_keys=ON")
        assert len(raw.execute("PRAGMA foreign_key_check(msx_tasks)").fetchall()) == (
            2 if legacy_milestone else 1
        )
    if missing_parent:
        with pytest.raises(RuntimeError, match="failed; rolled back"):
            _migrate_owned_task_inventory(migration_db)
    else:
        _migrate_owned_task_inventory(migration_db)
        _migrate_owned_task_inventory(migration_db)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("SELECT id, milestone_id, note_id FROM msx_tasks").fetchall() == [
            (7, 3, 31),
        ]
        assert raw.execute("SELECT task_id FROM task_links").fetchall() == [(7,)]
        targets = {fk[3]: fk[2] for fk in raw.execute("PRAGMA foreign_key_list(msx_tasks)")}
        assert targets["note_id"] == (note_parent if missing_parent else "notes")
        assert targets["milestone_id"] == (
            "_old_milestones" if missing_parent and legacy_milestone else "milestones"
        )
        if not missing_parent:
            assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(list(migration_db.directory.glob("*.owned-tasks-*.bak"))) == 1
    backup = next(migration_db.directory.glob("*.owned-tasks-*.bak"))
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert saved.execute("SELECT id, milestone_id, note_id FROM msx_tasks").fetchall() == [
            (7, 3, 31),
        ]
        assert saved.execute("SELECT task_id FROM task_links").fetchall() == [(7,)]


def test_owned_inventory_migration_backs_up_and_preserves_all_task_links_and_schema(migration_db):
    """The approved nullable-link rebuild preserves rows, inbound FKs, triggers and indexes."""
    seed_tasks(migration_db, 'is_hva BOOLEAN NOT NULL DEFAULT 0')
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute("INSERT INTO msx_tasks VALUES (7, 'linked', 'Demo', 3, 1, 'custom')")
        raw.execute("INSERT INTO task_links VALUES (9, 7)")
        raw.commit()
    _migrate_owned_task_inventory(migration_db)
    _migrate_owned_task_inventory(migration_db)
    columns = {column["name"]: column for column in inspect(migration_db.engine).get_columns(
        "msx_tasks",
    )}
    assert columns["milestone_id"]["nullable"] is True
    assert "msx_regarding_name" in columns
    assert len(list(migration_db.directory.glob("*.owned-tasks-*.bak"))) == 1
    backup = next(migration_db.directory.glob("*.owned-tasks-*.bak"))
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert saved.execute("SELECT id, milestone_id FROM msx_tasks").fetchall() == [(7, 3)]
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        assert raw.execute("SELECT task_id FROM task_links").fetchall() == [(7,)]
        assert raw.execute("SELECT extra_value, is_current_owner FROM msx_tasks").fetchone() == (
            "custom", 1,
        )
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_task_subject'",
        ).fetchone()
        raw.execute("INSERT INTO msx_tasks (id, msx_task_id, subject, is_hva) "
                    "VALUES (8, 'outside', 'Outside SB', 1)")
        raw.commit()
        with pytest.raises(sqlite3.IntegrityError, match="task is linked"):
            raw.execute("DELETE FROM msx_tasks WHERE id=7")
        raw.rollback()


def test_owned_inventory_rebuild_failure_rolls_back_and_restores_foreign_keys(migration_db):
    """A failed replacement leaves the original task table and a usable backup."""
    seed_tasks(migration_db, 'is_hva BOOLEAN NOT NULL DEFAULT 0')
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute("INSERT INTO msx_tasks VALUES (7, 'linked', 'Demo', 3, 1, 'custom')")
        raw.commit()
        raw.set_authorizer(
            lambda action, name, *args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DROP_TABLE and name == "msx_tasks" else sqlite3.SQLITE_OK,
        )
        try:
            with pytest.raises(RuntimeError, match="failed; rolled back"):
                _migrate_owned_task_inventory(migration_db)
        finally:
            raw.set_authorizer(None)
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("SELECT id, subject FROM msx_tasks").fetchall() == [(7, "Demo")]
    assert len(list(migration_db.directory.glob("*.owned-tasks-*.bak"))) == 1


def test_unchanged_owned_inventory_failure_reuses_verified_backup(migration_db):
    """Repeated failed startups must not write another full database snapshot each time."""
    seed_tasks(migration_db, 'is_hva BOOLEAN NOT NULL DEFAULT 0')
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute("INSERT INTO msx_tasks VALUES (7, 'linked', 'Demo', 3, 1, 'custom')")
        raw.commit()
        raw.set_authorizer(
            lambda action, name, *args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DROP_TABLE and name == "msx_tasks" else sqlite3.SQLITE_OK,
        )
        try:
            for _ in range(121):
                with pytest.raises(RuntimeError, match="failed; rolled back"):
                    _migrate_owned_task_inventory(migration_db)
        finally:
            raw.set_authorizer(None)
    backups = list(migration_db.directory.glob("*.owned-tasks-*.bak"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as saved:
        assert saved.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert saved.execute('SELECT subject FROM msx_tasks').fetchall() == [('Demo',)]


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
    def fail_backup(*args, **kwargs):
        """Reject the shared verified-backup step before classification DDL can execute."""
        raise RuntimeError('Migration backup failed verification')

    monkeypatch.setattr('app.services.migration_backups.create_migration_backup', fail_backup)
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
