"""Isolated regression coverage for the initiative project SQLite rebuild."""
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.migrations import _migrate_initiative_project_links


LEGACY_SCHEMA = """
CREATE TABLE manager_initiative_items (
    id INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES manager_initiative_sections(id),
    item_type VARCHAR(20) NOT NULL,
    milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    engagement_id INTEGER REFERENCES engagements(id) ON DELETE SET NULL,
    title_snapshot VARCHAR(500) NOT NULL,
    customer_snapshot VARCHAR(300) NOT NULL,
    talking_points TEXT NOT NULL,
    points_created_at DATETIME,
    created_at DATETIME NOT NULL,
    extra_snapshot TEXT DEFAULT 'keep, (this)',
    UNIQUE(section_id, milestone_id),
    UNIQUE(section_id, engagement_id),
    CHECK(item_type IN ('milestone', 'engagement')),
    CHECK((item_type = 'milestone' AND engagement_id IS NULL)
          OR (item_type = 'engagement' AND milestone_id IS NULL))
);
CREATE INDEX ix_manager_initiative_items_section_id ON manager_initiative_items(section_id);
CREATE UNIQUE INDEX ix_initiative_snapshot ON manager_initiative_items(title_snapshot)
    WHERE title_snapshot != '';
CREATE TABLE manager_discussed_points (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES manager_initiative_items(id),
    text TEXT NOT NULL,
    created_at DATETIME,
    discussed_at DATETIME NOT NULL
);
CREATE TRIGGER initiative_history_guard BEFORE DELETE ON manager_initiative_items
WHEN EXISTS (SELECT 1 FROM manager_discussed_points WHERE item_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'discussion history exists'); END;
INSERT INTO manager_initiative_items
    (id, section_id, item_type, milestone_id, engagement_id, title_snapshot,
     customer_snapshot, talking_points, points_created_at, created_at)
VALUES
    (41, 1, 'milestone', 1, NULL, 'Milestone snapshot', 'Customer',
     'Immutable points', '2026-09-01 12:00:00', '2026-08-01 12:00:00'),
    (57, 1, 'engagement', NULL, 1, 'Engagement snapshot', 'Customer',
     'Current points', NULL, '2026-08-02 12:00:00'),
    (63, 1, 'milestone', NULL, NULL, 'Deleted source snapshot', 'Customer',
     '', NULL, '2026-08-03 12:00:00');
INSERT INTO manager_discussed_points VALUES
    (91, 41, 'Historical block', '2026-09-01 12:00:00', '2026-09-03 12:00:00');
"""


@pytest.fixture
def migration_db():
    """Provide only an isolated disk database under the repository, never live data."""
    directory = Path(__file__).parent / f'.initiative-migration-{uuid4().hex}'
    directory.mkdir()
    engine = create_engine(
        f'sqlite:///{(directory / "test.db").as_posix()}', poolclass=StaticPool,
    )
    session = scoped_session(sessionmaker(bind=engine))
    database = SimpleNamespace(engine=engine, session=session, directory=directory)
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA journal_mode=WAL')
        raw.execute('PRAGMA wal_autocheckpoint=0')
        raw.execute('PRAGMA foreign_keys=ON')
        for table in ('manager_initiative_sections', 'milestones', 'engagements', 'projects'):
            raw.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            raw.execute(f'INSERT INTO {table} VALUES (1)')
        raw.commit()
    yield database
    session.remove()
    engine.dispose()
    shutil.rmtree(directory)


def _seed_legacy(database) -> None:
    """Seed deployed DDL with stable IDs, orphan snapshots, indexes, and history."""
    with database.engine.connect() as connection:
        connection.connection.driver_connection.executescript(LEGACY_SCHEMA)


def _rows(database, table: str) -> list:
    """Read persisted values in stable ID order."""
    with database.engine.connect() as connection:
        return connection.execute(text(f'SELECT * FROM {table} ORDER BY id')).fetchall()


def test_legacy_upgrade_preserves_rows_history_schema_and_wal_backup(migration_db):
    """Rebuild without losing IDs, snapshots, points, references, or custom schema."""
    _seed_legacy(migration_db)
    before = _rows(migration_db, 'manager_initiative_items')
    history = _rows(migration_db, 'manager_discussed_points')
    _migrate_initiative_project_links(migration_db)

    with migration_db.engine.connect() as connection:
        copied = connection.execute(text(
            'SELECT id, section_id, item_type, milestone_id, engagement_id, title_snapshot, '
            'customer_snapshot, talking_points, points_created_at, created_at, extra_snapshot '
            'FROM manager_initiative_items ORDER BY id'
        )).fetchall()
        assert copied == before
        assert connection.execute(text('PRAGMA foreign_keys')).scalar() == 1
        assert connection.execute(text('PRAGMA foreign_key_check')).fetchall() == []
        assert connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )).fetchall() == [('initiative_history_guard',)]
    assert _rows(migration_db, 'manager_discussed_points') == history
    inspector = inspect(migration_db.engine)
    assert {index['name'] for index in inspector.get_indexes('manager_initiative_items')} == {
        'ix_manager_initiative_items_section_id', 'ix_initiative_snapshot',
    }
    assert {tuple(unique['column_names']) for unique in
            inspector.get_unique_constraints('manager_initiative_items')} == {
        ('section_id', 'milestone_id'), ('section_id', 'engagement_id'),
        ('section_id', 'project_id'),
    }
    assert inspector.get_foreign_keys('manager_discussed_points')[0]['referred_table'] == (
        'manager_initiative_items'
    )
    backups = list(migration_db.directory.glob('*.bak'))
    assert len(backups) == 1
    with closing(sqlite3.connect(str(backups[0]))) as backup:
        assert backup.execute('SELECT * FROM manager_initiative_items ORDER BY id').fetchall() == [
            tuple(row) for row in before
        ]
        assert backup.execute('SELECT * FROM manager_discussed_points').fetchall() == [
            tuple(row) for row in history
        ]
        assert 'project_id' not in {
            row[1] for row in backup.execute('PRAGMA table_info(manager_initiative_items)')
        }


def test_second_run_is_noop(migration_db):
    """Repeat execution changes neither schema nor data and creates no extra backup."""
    _seed_legacy(migration_db)
    _migrate_initiative_project_links(migration_db)
    before = _rows(migration_db, 'manager_initiative_items')
    with migration_db.engine.connect() as connection:
        schema = connection.execute(text('SELECT * FROM sqlite_master ORDER BY name')).fetchall()
    _migrate_initiative_project_links(migration_db)
    assert _rows(migration_db, 'manager_initiative_items') == before
    assert len(list(migration_db.directory.glob('*.bak'))) == 1
    with migration_db.engine.connect() as connection:
        after_schema = connection.execute(
            text('SELECT * FROM sqlite_master ORDER BY name')
        ).fetchall()
        assert after_schema == schema


@pytest.mark.parametrize('force_rebuild', [False, True])
def test_project_links_and_discussions_survive_migration_reruns(
    migration_db, monkeypatch, force_rebuild,
):
    """Project values survive both no-op reruns and the existing-column rebuild path."""
    import app.migrations as migrations

    _seed_legacy(migration_db)
    _migrate_initiative_project_links(migration_db)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('INSERT INTO projects VALUES (2)')
        raw.execute('INSERT INTO manager_initiative_sections VALUES (3)')
        for item_id, project_id in ((101, 1), (102, 2)):
            raw.execute(
                'INSERT INTO manager_initiative_items '
                '(id, section_id, item_type, project_id, title_snapshot, customer_snapshot, '
                'talking_points, points_created_at, created_at) '
                "VALUES (?, 3, 'project', ?, ?, '', ?, ?, ?)",
                (item_id, project_id, f'Project {project_id} snapshot', 'Current project points',
                 '2026-09-30 20:45:00', '2026-09-30 20:44:00'),
            )
            raw.execute(
                'INSERT INTO manager_discussed_points VALUES (?, ?, ?, ?, ?)',
                (item_id, item_id, 'Immutable project discussion',
                 '2026-09-30 20:45:00', '2026-09-30 20:46:00'),
            )
        # Source deletion clears the FK only; the project snapshot and history remain.
        raw.execute('DELETE FROM projects WHERE id=2')
        raw.commit()
    items_before = _rows(migration_db, 'manager_initiative_items')
    history_before = _rows(migration_db, 'manager_discussed_points')
    if force_rebuild:
        original_inspector = inspect(migration_db.engine)

        def require_rebuild(engine):
            """Force the rebuild branch while preserving actual schema introspection."""
            return SimpleNamespace(
                get_table_names=original_inspector.get_table_names,
                get_columns=original_inspector.get_columns,
                get_check_constraints=lambda table: [],
                get_foreign_keys=original_inspector.get_foreign_keys,
                get_unique_constraints=original_inspector.get_unique_constraints,
                get_indexes=original_inspector.get_indexes,
            )

        monkeypatch.setattr(migrations, 'inspect', require_rebuild)
    for _ in range(3):
        _migrate_initiative_project_links(migration_db)
        assert _rows(migration_db, 'manager_initiative_items') == items_before
        assert _rows(migration_db, 'manager_discussed_points') == history_before
    assert len(list(migration_db.directory.glob('*.bak'))) == (4 if force_rebuild else 1)


def test_full_startup_migrations_preserve_existing_project_links_and_discussions():
    """Run the actual create-all/migration startup sequence on isolated model metadata."""
    from flask import Flask
    from app.migrations import run_migrations, run_table_renames
    from app.models import (
        ManagerDiscussedPoint, ManagerInitiativeItem, ManagerInitiativeSection, Project, db,
    )

    flask_app = Flask(__name__)
    flask_app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(flask_app)
    with flask_app.app_context():
        try:
            db.create_all()
            section = ManagerInitiativeSection(name='Startup persistence regression')
            projects = [Project(title=f'Project regression {index}') for index in (1, 2)]
            db.session.add_all([section, *projects])
            db.session.flush()
            for project in projects:
                item = ManagerInitiativeItem(
                    section=section, item_type='project', project=project,
                    title_snapshot=project.title, customer_snapshot='',
                    talking_points='Persisted project talking points',
                )
                db.session.add(item)
                db.session.flush()
                db.session.add(ManagerDiscussedPoint(
                    item=item, text='Immutable project discussion', created_at=item.created_at,
                ))
            db.session.commit()
            items_before = _rows(db, 'manager_initiative_items')
            history_before = _rows(db, 'manager_discussed_points')
            sections_before = _rows(db, 'manager_initiative_sections')
            projects_before = _rows(db, 'projects')
            for _ in range(2):
                db.session.remove()
                run_table_renames(db)
                db.create_all()
                run_migrations(db)
                assert _rows(db, 'manager_initiative_items') == items_before
                assert _rows(db, 'manager_discussed_points') == history_before
                assert _rows(db, 'manager_initiative_sections') == sections_before
                assert _rows(db, 'projects') == projects_before
        finally:
            db.session.remove()
            db.engine.dispose()


def test_fresh_model_schema_is_noop(migration_db):
    """The parent's model-created project schema requires no backup or rebuild."""
    from app.models import ManagerInitiativeItem

    ManagerInitiativeItem.__table__.create(migration_db.engine)
    with migration_db.engine.connect() as connection:
        schema = connection.execute(text('SELECT * FROM sqlite_master ORDER BY name')).fetchall()
    _migrate_initiative_project_links(migration_db)
    assert not list(migration_db.directory.glob('*.bak'))
    with migration_db.engine.connect() as connection:
        after_schema = connection.execute(
            text('SELECT * FROM sqlite_master ORDER BY name')
        ).fetchall()
        assert after_schema == schema


def test_project_constraints_and_source_deletion(migration_db):
    """Enforce exclusive types and uniqueness while keeping source-deleted snapshots."""
    _seed_legacy(migration_db)
    _migrate_initiative_project_links(migration_db)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        insert = (
            'INSERT INTO manager_initiative_items '
            '(section_id, item_type, milestone_id, engagement_id, project_id, title_snapshot, '
            'customer_snapshot, talking_points, created_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)'
        )
        raw.execute(insert, ('project', None, None, 1, 'Project', '', '', '2026-09-30'))
        raw.commit()
        invalid = [
            ('project', None, None, 1),  # Duplicate project within section.
            ('unknown', None, None, None),
            ('project', 1, None, None),
            ('project', None, 1, None),
            ('milestone', None, None, 1),
            ('engagement', None, None, 1),
            ('milestone', 1, None, None),  # Existing milestone uniqueness.
            ('engagement', None, 1, None),  # Existing engagement uniqueness.
            ('project', None, None, 999),  # Project foreign key.
        ]
        for index, values in enumerate(invalid):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(insert, (*values, f'Invalid {index}', '', '', '2026-09-30'))
            raw.rollback()
        for table in ('projects', 'milestones', 'engagements'):
            raw.execute(f'DELETE FROM {table} WHERE id=1')
        raw.commit()
        assert raw.execute(
            'SELECT milestone_id, engagement_id, project_id FROM manager_initiative_items'
        ).fetchall() == [(None, None, None)] * 4
        assert raw.execute('SELECT item_id, text FROM manager_discussed_points').fetchall() == [
            (41, 'Historical block'),
        ]
        with pytest.raises(sqlite3.IntegrityError, match='discussion history'):
            raw.execute('DELETE FROM manager_initiative_items WHERE id=41')
        raw.rollback()


@pytest.mark.parametrize('foreign_keys', [0, 1])
def test_failure_after_drop_rolls_back_and_restores_fk_state(migration_db, foreign_keys):
    """A failed rename restores the dropped original and immutable history atomically."""
    _seed_legacy(migration_db)
    before = _rows(migration_db, 'manager_initiative_items')
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute(f'PRAGMA foreign_keys={foreign_keys}')
        raw.set_authorizer(
            lambda action, *args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK
        )
        try:
            with pytest.raises(RuntimeError, match='failed; rolled back'):
                _migrate_initiative_project_links(migration_db)
        finally:
            raw.set_authorizer(None)
        assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == foreign_keys
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE name='_initiative_items_project_new'"
        ).fetchall() == []
        assert raw.execute('SELECT item_id FROM manager_discussed_points').fetchall() == [(41,)]
    assert _rows(migration_db, 'manager_initiative_items') == before
    assert len(list(migration_db.directory.glob('*.bak'))) == 1


def test_backup_failure_never_changes_schema(migration_db, monkeypatch):
    """Backup errors are explicit and prevent any destructive schema operation."""
    _seed_legacy(migration_db)
    before = _rows(migration_db, 'manager_initiative_items')

    def fail_backup(*args, **kwargs):
        """Simulate an inaccessible backup destination."""
        raise OSError('backup unavailable')

    monkeypatch.setattr(sqlite3, 'connect', fail_backup)
    with pytest.raises(RuntimeError, match='failed; rolled back') as failure:
        _migrate_initiative_project_links(migration_db)
    assert isinstance(failure.value.__cause__, OSError)
    assert _rows(migration_db, 'manager_initiative_items') == before
    assert 'project_id' not in {
        column['name'] for column in inspect(migration_db.engine).get_columns(
            'manager_initiative_items'
        )
    }


def test_foreign_key_violation_is_explicit_and_rolls_back(migration_db):
    """Invalid initiative references prevent migration rather than being silently lost."""
    _seed_legacy(migration_db)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA foreign_keys=OFF')
        raw.execute('UPDATE manager_discussed_points SET item_id=999 WHERE id=91')
        raw.commit()
        raw.execute('PRAGMA foreign_keys=ON')
    before = _rows(migration_db, 'manager_initiative_items')
    with pytest.raises(RuntimeError, match='failed; rolled back') as failure:
        _migrate_initiative_project_links(migration_db)
    assert 'foreign key violations' in str(failure.value.__cause__)
    assert _rows(migration_db, 'manager_initiative_items') == before
    assert _rows(migration_db, 'manager_discussed_points')[0].item_id == 999
    with migration_db.engine.connect() as connection:
        assert connection.execute(text('PRAGMA foreign_keys')).scalar() == 1


def test_unrelated_legacy_foreign_key_violations_do_not_block_upgrade(migration_db):
    """Old unrelated FK damage must not prevent initiative startup schema upgrades."""
    _seed_legacy(migration_db)
    with migration_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA foreign_keys=OFF')
        raw.execute(
            'CREATE TABLE unrelated_legacy_data '
            '(id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES removed_legacy_table(id))'
        )
        raw.execute('INSERT INTO unrelated_legacy_data VALUES (1, 999)')
        raw.commit()
        raw.execute('PRAGMA foreign_keys=ON')
    _migrate_initiative_project_links(migration_db)
    _migrate_initiative_project_links(migration_db)
    assert 'project_id' in {
        column['name'] for column in inspect(migration_db.engine).get_columns(
            'manager_initiative_items'
        )
    }
    assert _rows(migration_db, 'unrelated_legacy_data') == [(1, 999)]
    assert len(list(migration_db.directory.glob('*.bak'))) == 1


def test_missing_table_is_noop(migration_db):
    """Startup before table creation does not manufacture a schema or backup."""
    _migrate_initiative_project_links(migration_db)
    assert 'manager_initiative_items' not in inspect(migration_db.engine).get_table_names()
    assert not list(migration_db.directory.glob('*.bak'))


def test_in_memory_database_backup_and_upgrade():
    """Memory-only isolated databases are backed up without filesystem artifacts."""
    engine = create_engine('sqlite://', poolclass=StaticPool)
    session = scoped_session(sessionmaker(bind=engine))
    database = SimpleNamespace(engine=engine, session=session)
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            raw.execute('PRAGMA foreign_keys=ON')
            for table in ('manager_initiative_sections', 'milestones', 'engagements', 'projects'):
                raw.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                raw.execute(f'INSERT INTO {table} VALUES (1)')
            raw.commit()
        _seed_legacy(database)
        _migrate_initiative_project_links(database)
        _migrate_initiative_project_links(database)
        assert len(_rows(database, 'manager_initiative_items')) == 3
        assert _rows(database, 'manager_discussed_points')[0].item_id == 41
    finally:
        session.remove()
        engine.dispose()
