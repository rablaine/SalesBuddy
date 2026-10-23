"""Bounded migration snapshots using real isolated SQLite databases, including WAL data."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.services import migration_backups as backups


@pytest.fixture
def database(tmp_path):
    """Keep the synthetic WAL source open so committed pages need not reach the main file."""
    path = tmp_path / 'salesbuddy.db'
    engine = create_engine(f'sqlite:///{path}', poolclass=StaticPool)
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA journal_mode=WAL')
        raw.execute('PRAGMA wal_autocheckpoint=0')
        raw.executescript(
            "CREATE TABLE notes(id INTEGER PRIMARY KEY,content TEXT);"
            "INSERT INTO notes VALUES(1,'preserve original note');"
        )
    yield engine, path
    engine.dispose()


def snapshot(engine, path: Path, label: str, index: int) -> Path:
    """Create a genuine legacy-format backup, with deterministic names for ordering checks."""
    target = path.with_name(
        f'{path.name}.{label}-20261022T010000{index:06d}Z-{index:032x}.bak',
    )
    with engine.connect() as connection, closing(sqlite3.connect(target)) as saved:
        connection.connection.driver_connection.backup(saved)
        saved.execute('PRAGMA journal_mode=DELETE')
    return target


def test_repeated_backup_requests_reuse_the_same_verified_snapshot(database):
    """An unchanged database does not create another archive even over 121 attempts."""
    engine, path = database
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        first = backups.create_migration_backup(raw, 'owned-tasks')
        for _ in range(120):
            assert backups.create_migration_backup(raw, 'owned-tasks') == first
        assert not raw.in_transaction
    assert list(path.parent.glob('*.bak')) == [first]
    assert not list(path.parent.glob('*.bak-*'))


def test_legacy_wal_header_snapshot_can_be_reused_without_creating_sidecars(database):
    """Older online backups can retain a WAL header without having an external WAL file."""
    engine, path = database
    target = path.with_name(
        f'{path.name}.owned-tasks-20261022T010000000001Z-{"a" * 32}.bak',
    )
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        with closing(sqlite3.connect(target)) as saved:
            raw.backup(saved)
        for _ in range(4):
            assert backups.create_migration_backup(raw, 'owned-tasks') == target
    assert not list(path.parent.glob('*.bak-*'))


def test_committed_wal_changes_require_a_new_backup(database):
    """An unchanged main file cannot hide new committed note data in the WAL."""
    engine, path = database
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        first = backups.create_migration_backup(raw, 'owned-tasks')
        main_bytes = path.read_bytes()
        raw.execute("UPDATE notes SET content='new committed note' WHERE id=1")
        raw.commit()
        assert path.read_bytes() == main_bytes
        second = backups.create_migration_backup(raw, 'owned-tasks')
        assert second != first
        assert backups.create_migration_backup(raw, 'owned-tasks') == second
    with closing(sqlite3.connect(second)) as saved:
        assert saved.execute('SELECT content FROM notes').fetchall() == [('new committed note',)]
    with closing(sqlite3.connect(first)) as saved:
        assert saved.execute('SELECT content FROM notes').fetchall() == [('preserve original note',)]


@pytest.mark.parametrize('change', [
    'ALTER TABLE notes ADD COLUMN extra TEXT',
    'UPDATE notes SET id=2',
    'PRAGMA user_version=7',
    "CREATE TRIGGER keep_notes BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT,'keep'); END",
])
def test_schema_and_identity_changes_are_not_mistaken_for_an_identical_backup(database, change):
    """Reuse requires schema, record identities and database-version metadata to agree."""
    engine, _ = database
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        first = backups.create_migration_backup(raw, 'owned-tasks')
        raw.execute(change)
        raw.commit()
        assert backups.create_migration_backup(raw, 'owned-tasks') != first


def test_corrupt_backup_is_not_reused(database, caplog):
    """A filename or valid schema alone is not proof that a recovery snapshot is usable."""
    engine, path = database
    damaged = snapshot(engine, path, 'owned-tasks', 1)
    damaged.write_bytes(b'incomplete database backup')
    with engine.connect() as connection:
        created = backups.create_migration_backup(
            connection.connection.driver_connection, 'owned-tasks',
        )
    assert created != damaged
    assert 'Cannot reuse migration backup' in caplog.text
    with closing(sqlite3.connect(created)) as saved:
        assert saved.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def test_backup_verification_failure_aborts_and_removes_only_its_incomplete_file(
    database, monkeypatch,
):
    """Failed verification leaves the original schema and records usable, without junk archives."""
    engine, path = database

    def reject(_connection):
        """Simulate an integrity-check failure on the newly copied snapshot."""
        target = next(path.parent.glob('*.bak'))
        Path(str(target) + '-journal').write_bytes(b'incomplete backup journal')
        raise sqlite3.DatabaseError('synthetic backup verification failure')

    monkeypatch.setattr(backups, '_verify_integrity', reject)
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        with pytest.raises(sqlite3.DatabaseError, match='verification failure'):
            backups.create_migration_backup(raw, 'owned-tasks')
        assert not raw.in_transaction
        assert raw.execute('SELECT content FROM notes').fetchall() == [('preserve original note',)]
    assert list(path.parent.glob('*.bak')) == []
    assert list(path.parent.glob('*.bak-*')) == []


def test_backup_refuses_to_commit_an_existing_caller_transaction(database):
    """The backup helper must not accidentally commit a user's pending database writes."""
    engine, path = database
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute("UPDATE notes SET content='uncommitted edit'")
        with pytest.raises(RuntimeError, match='committed database connection'):
            backups.create_migration_backup(raw, 'owned-tasks')
        assert raw.in_transaction
        raw.rollback()
        assert raw.execute('SELECT content FROM notes').fetchone() == ('preserve original note',)
    assert list(path.parent.glob('*.bak')) == []


def test_memory_database_backup_and_retention_do_not_create_files(tmp_path, monkeypatch):
    """Existing in-memory tests need no on-disk migration snapshots or cleanup side effects."""
    monkeypatch.chdir(tmp_path)
    with closing(sqlite3.connect(':memory:')) as raw:
        raw.execute('CREATE TABLE example(id INTEGER PRIMARY KEY)')
        assert backups.create_migration_backup(raw, 'owned-tasks') is None
    engine = create_engine('sqlite:///:memory:')
    try:
        assert backups.prune_migration_backups(engine) == 0
    finally:
        engine.dispose()
    assert list(tmp_path.iterdir()) == []


def test_successful_startup_retention_reclaims_121_legacy_backups(database):
    """Retain the newest two per type and preserve all live data and unrelated backup families."""
    engine, path = database
    owned = [snapshot(engine, path, 'owned-tasks', index) for index in range(1, 122)]
    hva = [snapshot(engine, path, 'task-hva', index) for index in range(1, 5)]
    protected = [
        path.with_name(path.name + '.pre-repair.bak'),
        path.with_name(path.name + '.owned-tasks-not-a-generated-name.bak'),
        path.with_name(path.name + '.owned-tasks-20261322T010000000001Z-' + 'b' * 32 + '.bak'),
        path.with_name('fiscal-year.db.owned-tasks-20261022T010000000001Z-' + 'b' * 32 + '.bak'),
        path.with_name(path.name + '.owned-tasks-20261022T010000000001Z-' + 'c' * 32 + '.bak.partial'),
        path.parent / 'scheduled-backup.db',
        path.parent / 'upgrade-recovery' / 'old-attempt' / 'database.db',
    ]
    for other in protected:
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b'protected evidence')
    with engine.connect() as connection:
        before = connection.connection.driver_connection.execute('SELECT * FROM notes').fetchall()
    assert backups.prune_migration_backups(engine) == 121
    assert [item for item in owned if item.exists()] == owned[-2:]
    assert [item for item in hva if item.exists()] == hva[-2:]
    assert all(other.read_bytes() == b'protected evidence' for other in protected)
    with engine.connect() as connection:
        assert connection.connection.driver_connection.execute('SELECT * FROM notes').fetchall() == before
    assert backups.prune_migration_backups(engine) == 0


def test_retention_keeps_two_verified_copies_not_corrupt_newest_files(database, caplog):
    """Do not discard the last good evidence just because damaged files have newer names."""
    engine, path = database
    paths = [snapshot(engine, path, 'owned-tasks', index) for index in range(1, 5)]
    paths[-1].write_bytes(b'damaged')
    assert backups.prune_migration_backups(engine) == 2
    assert [item for item in paths if item.exists()] == paths[1:3]
    assert 'not verifiable' in caplog.text


def test_retention_preserves_everything_when_two_good_copies_are_unavailable(database, caplog):
    """A successful startup does not authorize deleting the last usable backup."""
    engine, path = database
    paths = [snapshot(engine, path, 'owned-tasks', index) for index in range(1, 4)]
    for item in paths[1:]:
        item.write_bytes(b'damaged')
    assert backups.prune_migration_backups(engine) == 0
    assert all(item.exists() for item in paths)
    assert 'fewer than two verified snapshots' in caplog.text


def test_retention_ignores_linked_and_active_sqlite_archives(database):
    """Never follow links or remove a snapshot that has SQLite journal/coordination sidecars."""
    engine, path = database
    paths = [snapshot(engine, path, 'owned-tasks', index) for index in range(1, 5)]
    linked = snapshot(engine, path, 'owned-tasks', 5)
    alias = path.parent / 'manual-evidence.db'
    alias.hardlink_to(linked)
    active = snapshot(engine, path, 'owned-tasks', 6)
    Path(str(active) + '-wal').write_bytes(b'active journal')
    assert backups.prune_migration_backups(engine) == 2
    assert all(item.exists() for item in paths[-2:])
    assert linked.exists() and alias.exists() and active.exists()
    assert Path(str(active) + '-wal').read_bytes() == b'active journal'


def test_retention_logs_delete_errors_without_breaking_successful_startup(database, monkeypatch, caplog):
    """Locked files remain for a later startup; cleanup is not another startup failure loop."""
    engine, path = database
    paths = [snapshot(engine, path, 'owned-tasks', index) for index in range(1, 5)]
    original_unlink = Path.unlink

    def unlink(candidate, *args, **kwargs):
        """Keep one legacy backup locked while allowing other safe deletions."""
        if candidate == paths[0]:
            raise PermissionError('synthetic locked archive')
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unlink)
    assert backups.prune_migration_backups(engine) == 1
    assert paths[0].exists()
    assert 'Could not remove old migration backup' in caplog.text
