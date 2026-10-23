"""Regression tests for cross-process SQLite startup coordination."""

import multiprocessing
from contextlib import closing
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker

from app.migrations import _migrate_msx_task_hva_column
from app.services.database_startup import database_startup_lock


def test_shared_initializer_orders_schema_and_seeds_idempotently(monkeypatch):
    """Rehearsal and live startup share schema setup without shell side effects."""
    from flask import Flask
    from app.models import db, User, UserPreference
    from app.services.database_startup import initialize_database

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['TESTING'] = True
    db.init_app(app)
    calls = []
    monkeypatch.setattr('app.migrations.run_table_renames', lambda database: calls.append('rename'))
    monkeypatch.setattr('app.migrations.run_migrations', lambda database: calls.append('migrate'))
    monkeypatch.setattr(
        'app.services.dashboard_templates.initialize_dashboard_templates',
        lambda: calls.append('templates'),
    )
    monkeypatch.setattr(
        'app.services.migration_backups.prune_migration_backups',
        lambda engine: calls.append('cleanup'),
    )
    original_create_all = db.create_all

    def create_all() -> None:
        """Record ordering while creating a real isolated schema."""
        calls.append('create')
        original_create_all()

    def forbidden(*args, **kwargs) -> None:
        """Reject shell writes and background startup during rehearsal."""
        pytest.fail('Shared initializer must not start services or reconcile shell files')

    monkeypatch.setattr(db, 'create_all', create_all)
    monkeypatch.setattr('app.services.shell_prefs.reconcile_shell_prefs', forbidden)
    monkeypatch.setattr('app.start_background_services', forbidden)
    with app.app_context(), database_startup_lock(db.engine):
        initialize_database(db)
        initialize_database(db)
        assert calls == ['rename', 'create', 'migrate', 'templates', 'cleanup'] * 2
        assert db.session.get(User, 1).email == 'user@localhost'
        assert User.query.count() == 1
        assert UserPreference.query.count() == 1
        # Preferences must also be repaired if the canonical user already exists.
        UserPreference.query.delete()
        db.session.commit()
        initialize_database(db)
        assert UserPreference.query.count() == 1
        db.session.remove()
        db.engine.dispose()


@pytest.mark.parametrize('failure_stage', ['migrations', 'templates', 'seeds'])
def test_failed_initializer_never_prunes_migration_evidence(monkeypatch, failure_stage):
    """Cleanup runs only after every schema/template/canonical-record initialization step succeeds."""
    from flask import Flask
    from app.models import db
    from app.services.database_startup import initialize_database

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', TESTING=True)
    db.init_app(app)

    def fail(*args, **kwargs):
        """Stop one real initialization stage before evidence retention can execute."""
        raise RuntimeError('synthetic initialization failure')

    def forbidden(_engine):
        """Reject any cleanup before successful completion."""
        pytest.fail('Failed initialization must retain all migration evidence')

    monkeypatch.setattr('app.services.migration_backups.prune_migration_backups', forbidden)
    monkeypatch.setattr('app.migrations.run_table_renames', lambda database: None)
    monkeypatch.setattr('app.migrations.run_migrations', fail if failure_stage == 'migrations'
                        else lambda database: None)
    monkeypatch.setattr('app.services.dashboard_templates.initialize_dashboard_templates',
                        fail if failure_stage == 'templates' else lambda: None)
    with app.app_context():
        try:
            if failure_stage == 'seeds':
                monkeypatch.setattr(db.session, 'commit', fail)
            with pytest.raises(RuntimeError, match='synthetic initialization failure'):
                initialize_database(db)
        finally:
            db.session.remove()
            db.engine.dispose()


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
