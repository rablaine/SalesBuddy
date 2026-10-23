"""Exercise historical task schemas through the real offline startup pipeline."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine

from app.models import Customer, Milestone, Note, db
from app import upgrade_rehearsal


CORE_IDS = {
    'customers': [13],
    'notes': [31],
    'milestones': [47],
    'msx_tasks': [71, 84],
    'engagements': [],
}
OWNER_COLUMNS = (
    'is_current_owner', 'msx_regarding_id', 'msx_regarding_type', 'msx_regarding_name',
)
LATER_COLUMNS = ('meeting_id', 'statecode', 'statuscode', 'actual_end', 'msx_created_on')


def _seed_history(
    database: Path,
    *,
    classification: str = 'is_hva',
    note_parent: str = 'notes',
    milestone_nullable: bool = True,
    owner_columns: bool = False,
    canonical: bool = False,
    note_nullable: bool = True,
    milestone_parent: str = 'milestones',
    dangling_note: bool = False,
) -> None:
    """Create synthetic model-valid parents, then install an actual historical task DDL."""
    engine = create_engine(f'sqlite:///{database}')
    timestamp = datetime(2025, 4, 9, 15, 30, tzinfo=timezone.utc)
    try:
        db.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(Customer.__table__.insert().values(
                id=13, name='Synthetic Upgrade Account', tpid=900000001,
                created_at=timestamp,
            ))
            connection.execute(Note.__table__.insert().values(
                id=31, customer_id=13, content='<p>Synthetic regression note only.</p>',
                call_date=datetime(2025, 4, 9, 10, 30),
                created_at=timestamp, updated_at=timestamp,
            ))
            connection.execute(Milestone.__table__.insert().values(
                id=47, customer_id=13, msx_milestone_id='synthetic-milestone',
                url='https://example.invalid/milestone', title='Synthetic milestone',
                created_at=timestamp, updated_at=timestamp,
            ))
    finally:
        engine.dispose()

    with closing(sqlite3.connect(database)) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='msx_tasks'",
        ).fetchone()[0]
        if not canonical:
            removed = LATER_COLUMNS + (() if owner_columns else OWNER_COLUMNS)
            for column in removed:
                schema, count = re.subn(
                    rf'^\s*"?{column}"?\s+[^\n]*\n', '', schema, flags=re.MULTILINE,
                )
                assert count == 1, f'Fixture could not remove task column {column}'
            schema = re.sub(
                r'^\s*(?:UNIQUE \(meeting_id\)|FOREIGN KEY\(meeting_id\)[^\n]*)[^\n]*\n',
                '', schema, flags=re.MULTILINE,
            )
            if classification == 'is_hok':
                schema = schema.replace('is_hva', 'is_hok')
            elif classification == 'both':
                schema, count = re.subn(
                    r'(\bis_hva BOOLEAN NOT NULL,)',
                    r'is_hok BOOLEAN NOT NULL,\n\t\1', schema,
                )
                assert count == 1, 'Fixture could not add partial classification column'
            else:
                assert classification == 'is_hva'
            if not milestone_nullable:
                schema, count = re.subn(
                    r'(\bmilestone_id INTEGER)(?! NOT NULL)', r'\1 NOT NULL', schema,
                )
                assert count == 1, 'Fixture could not restore required milestone'
            if not note_nullable:
                schema, count = re.subn(
                    r'(\bnote_id INTEGER)(?! NOT NULL)', r'\1 NOT NULL', schema,
                )
                assert count == 1, 'Fixture could not restore required note'
            schema = schema.replace('REFERENCES notes ', f'REFERENCES {note_parent} ')
            schema = schema.replace(
                'REFERENCES milestones ', f'REFERENCES {milestone_parent} ',
            )
            # Recreate only an empty, synthetic table. Never mutate sqlite_master directly.
            connection.execute('DROP TABLE msx_tasks')
            connection.execute(schema)

        columns = {row[1] for row in connection.execute('PRAGMA table_info(msx_tasks)')}
        for task_id, category, flag in ((71, 861980002, 1), (84, 0, 0)):
            values = {
                'id': task_id, 'msx_task_id': f'synthetic-task-{task_id}',
                'msx_task_url': f'https://example.invalid/task/{task_id}',
                'subject': f'Synthetic historical task {task_id}',
                'description': 'Preserve synthetic task details',
                'task_category': category, 'task_category_name': 'Synthetic category',
                'duration_minutes': 60, 'note_id': 99991 if dangling_note else 31,
                'milestone_id': None if milestone_nullable and task_id == 84 else 47,
                'created_at': '2025-04-09 15:30:00',
            }
            if 'is_hok' in columns:
                values['is_hok'] = 1 - flag if classification == 'both' else flag
            if 'is_hva' in columns:
                values['is_hva'] = flag
            if 'is_current_owner' in columns:
                values.update(
                    is_current_owner=0 if task_id == 71 else 1,
                    msx_regarding_id='synthetic-regarding',
                    msx_regarding_type='msp_milestone',
                    msx_regarding_name='Synthetic regarding',
                )
            names = ', '.join(f'"{column}"' for column in values)
            placeholders = ', '.join('?' for _ in values)
            connection.execute(
                f'INSERT INTO msx_tasks ({names}) VALUES ({placeholders})',
                tuple(values.values()),
            )
        connection.executescript("""
            CREATE TABLE upgrade_history_task_links (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES msx_tasks(id)
            );
            INSERT INTO upgrade_history_task_links VALUES (101, 71), (102, 84);
            CREATE INDEX ix_upgrade_history_task_subject ON msx_tasks(subject);
        """)
        connection.commit()


def _core_rows(database: Path) -> dict:
    """Capture synthetic core data and inbound links without invoking the application."""
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            for table in (*CORE_IDS, 'upgrade_history_task_links')
        }


def _assert_upgraded(database: Path, *, milestone_nullable: bool, owners_retained: bool) -> None:
    """Assert identities, content, relationships and new nullable-link semantics."""
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        for table, expected in CORE_IDS.items():
            assert [
                row[0] for row in connection.execute(f'SELECT id FROM "{table}" ORDER BY id')
            ] == expected
        columns = {
            row[1]: row for row in connection.execute('PRAGMA table_info(msx_tasks)')
        }
        assert 'is_hok' not in columns
        assert 'is_hva' in columns
        assert columns['milestone_id'][3] == 0
        assert columns['note_id'][3] == 0
        assert set(OWNER_COLUMNS).issubset(columns)
        targets = {
            row[3]: row[2] for row in connection.execute('PRAGMA foreign_key_list(msx_tasks)')
        }
        assert targets['note_id'] == 'notes'
        assert targets['milestone_id'] == 'milestones'
        assert connection.execute(
            'SELECT id, note_id, milestone_id, is_hva FROM msx_tasks ORDER BY id',
        ).fetchall() == [(71, 31, 47, 1), (84, 31, None if milestone_nullable else 47, 0)]
        assert connection.execute(
            'SELECT id, task_id FROM upgrade_history_task_links ORDER BY id',
        ).fetchall() == [(101, 71), (102, 84)]
        assert connection.execute(
            'SELECT customer_id, content FROM notes WHERE id=31',
        ).fetchone() == (13, '<p>Synthetic regression note only.</p>')
        assert connection.execute(
            'SELECT customer_id, title FROM milestones WHERE id=47',
        ).fetchone() == (13, 'Synthetic milestone')
        assert connection.execute(
            'SELECT subject, description, duration_minutes FROM msx_tasks WHERE id=71',
        ).fetchone() == (
            'Synthetic historical task 71', 'Preserve synthetic task details', 60,
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='ix_upgrade_history_task_subject'",
        ).fetchone() == ('ix_upgrade_history_task_subject',)
        if owners_retained:
            assert connection.execute(
                'SELECT is_current_owner, msx_regarding_id, msx_regarding_type, '
                'msx_regarding_name FROM msx_tasks WHERE id=71',
            ).fetchone() == (
                0, 'synthetic-regarding', 'msp_milestone', 'Synthetic regarding',
            )
        else:
            assert connection.execute(
                'SELECT is_current_owner FROM msx_tasks ORDER BY id',
            ).fetchall() == [(1,), (1,)]


@pytest.mark.parametrize('classification', ['is_hok', 'both', 'is_hva'])
@pytest.mark.parametrize('note_parent', ['notes', 'call_logs_backup', '_old_call_logs'])
@pytest.mark.parametrize('milestone_nullable', [False, True], ids=['required', 'nullable'])
def test_full_pipeline_upgrades_historical_classification_and_note_fks(
    tmp_path: Path, classification: str, note_parent: str, milestone_nullable: bool,
) -> None:
    """Classification and FK repairs must compose under complete startup ordering."""
    database = tmp_path / 'historical.db'
    _seed_history(
        database, classification=classification, note_parent=note_parent,
        milestone_nullable=milestone_nullable,
    )
    result = upgrade_rehearsal.initialize_offline(database)
    assert result['success'] is True
    assert result['repeated'] is True
    assert result['core_counts'] == {table: len(ids) for table, ids in CORE_IDS.items()}
    assert result['preexisting_fk_violations'] == (0 if note_parent == 'notes' else 2)
    assert result['remaining_fk_violations'] == 0
    _assert_upgraded(database, milestone_nullable=milestone_nullable, owners_retained=False)


@pytest.mark.parametrize('milestone_nullable', [False, True], ids=['required', 'nullable'])
def test_full_pipeline_resumes_interrupted_owned_task_update(
    tmp_path: Path, milestone_nullable: bool,
) -> None:
    """Existing owner metadata must survive the deferred constraint and FK repair."""
    database = tmp_path / 'interrupted.db'
    _seed_history(
        database, classification='both', note_parent='_old_call_logs',
        milestone_parent='_old_milestones', milestone_nullable=milestone_nullable,
        owner_columns=True,
    )
    result = upgrade_rehearsal.initialize_offline(database)
    assert result['success'] is True
    assert result['repeated'] is True
    assert result['preexisting_fk_violations'] == (3 if milestone_nullable else 4)
    assert result['remaining_fk_violations'] == 0
    _assert_upgraded(database, milestone_nullable=milestone_nullable, owners_retained=True)


@pytest.mark.parametrize(
    ('note_parent', 'milestone_nullable', 'owner_columns'),
    [
        pytest.param('notes', False, False, id='original-required-links'),
        pytest.param('notes', True, True, id='interrupted-owner-update'),
        pytest.param('call_logs_backup', False, True, id='legacy-backup-parent'),
        pytest.param('_old_call_logs', True, False, id='legacy-old-parent'),
    ],
)
def test_full_pipeline_handles_original_required_note_schema(
    tmp_path: Path, note_parent: str, milestone_nullable: bool, owner_columns: bool,
) -> None:
    """Note relaxation preserves inbound links, unknown columns, triggers and backups."""
    database = tmp_path / 'required-note.db'
    _seed_history(
        database, classification='is_hok', note_parent=note_parent,
        milestone_nullable=milestone_nullable, owner_columns=owner_columns,
        note_nullable=False,
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("""
            ALTER TABLE msx_tasks ADD COLUMN historical_detail TEXT DEFAULT 'keep detail';
            UPDATE msx_tasks SET historical_detail='task-specific detail' WHERE id=71;
            CREATE TRIGGER upgrade_history_description_guard
            BEFORE UPDATE OF description ON msx_tasks
            WHEN NEW.description='blocked synthetic edit'
            BEGIN SELECT RAISE(ABORT, 'synthetic description guard'); END;
        """)
        original_trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='upgrade_history_description_guard'",
        ).fetchone()[0]
    result = upgrade_rehearsal.initialize_offline(database)
    assert result['success'] is True
    assert result['repeated'] is True
    assert result['remaining_fk_violations'] == 0
    _assert_upgraded(
        database, milestone_nullable=milestone_nullable, owners_retained=owner_columns,
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            'SELECT historical_detail FROM msx_tasks ORDER BY id',
        ).fetchall() == [('task-specific detail',), ('keep detail',)]
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='upgrade_history_description_guard'",
        ).fetchone() == (original_trigger,)
        assert connection.execute(
            'PRAGMA foreign_key_list(upgrade_history_task_links)',
        ).fetchone()[2] == 'msx_tasks'
        with pytest.raises(sqlite3.IntegrityError, match='synthetic description guard'):
            connection.execute(
                "UPDATE msx_tasks SET description='blocked synthetic edit' WHERE id=71",
            )
        connection.rollback()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('msx_tasks_old', '_msx_tasks_note_new', '_msx_tasks_owned_new')",
        ).fetchall() == []
    backups = list(tmp_path.glob('required-note.db.task-note-nullable-*.bak'))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        note_column = next(
            row for row in connection.execute('PRAGMA table_info(msx_tasks)')
            if row[1] == 'note_id'
        )
        assert note_column[3] == 1
        assert connection.execute(
            'SELECT id, historical_detail FROM msx_tasks ORDER BY id',
        ).fetchall() == [(71, 'task-specific detail'), (84, 'keep detail')]
        assert connection.execute(
            'SELECT id, task_id FROM upgrade_history_task_links ORDER BY id',
        ).fetchall() == [(101, 71), (102, 84)]


def test_full_pipeline_preserves_current_schema_and_repeats_without_changes(tmp_path: Path) -> None:
    """Current installations preserve all core rows, not just their row counts."""
    database = tmp_path / 'current.db'
    _seed_history(database, canonical=True)
    original = _core_rows(database)
    first = upgrade_rehearsal.initialize_offline(database)
    second = upgrade_rehearsal.initialize_offline(database)
    assert first == second
    assert first['success'] is True
    assert first['repeated'] is True
    assert first['preexisting_fk_violations'] == first['remaining_fk_violations'] == 0
    assert _core_rows(database) == original
    _assert_upgraded(database, milestone_nullable=True, owners_retained=True)


@pytest.mark.parametrize('existing_file', [False, True], ids=['missing', 'empty'])
def test_full_pipeline_initializes_empty_database(tmp_path: Path, existing_file: bool) -> None:
    """A first installation uses the same complete, repeatable initialization."""
    database = tmp_path / 'empty.db'
    if existing_file:
        database.touch()
    result = upgrade_rehearsal.initialize_offline(database)
    assert result == {
        'success': True, 'repeated': True,
        'core_counts': {table: 0 for table in CORE_IDS},
        'preexisting_fk_violations': 0, 'remaining_fk_violations': 0,
    }
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('SELECT id FROM users WHERE id=1').fetchone() == (1,)
        assert connection.execute('SELECT COUNT(*) FROM user_preferences').fetchone()[0] == 1


@pytest.mark.parametrize(
    ('note_parent', 'canonical'),
    [
        pytest.param('notes', False, id='required-milestone'),
        pytest.param('call_logs_backup', False, id='backup-note-parent'),
        pytest.param('_old_call_logs', False, id='old-note-parent'),
        pytest.param('notes', True, id='current-no-rebuild'),
    ],
)
def test_rehearsal_rejects_real_dangling_notes_without_touching_original(
    tmp_path: Path, note_parent: str, canonical: bool,
) -> None:
    """A failed disposable rehearsal cannot damage the source database or hide bad IDs."""
    source = tmp_path / 'original.db'
    candidate = tmp_path / 'rehearsal-copy.db'
    _seed_history(
        source, note_parent=note_parent, milestone_nullable=canonical,
        dangling_note=True, canonical=canonical,
    )
    original_bytes = source.read_bytes()
    original_rows = _core_rows(source)
    shutil.copy2(source, candidate)
    with pytest.raises(RuntimeError) as failure:
        upgrade_rehearsal.initialize_offline(candidate)
    error: BaseException | None = failure.value
    messages = []
    while error is not None:
        messages.append(str(error).lower())
        error = error.__cause__ or error.__context__
    assert any('foreign key' in message for message in messages)
    assert source.read_bytes() == original_bytes
    assert _core_rows(source) == original_rows
    assert not list(tmp_path.glob('original.db.*'))
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute('SELECT note_id FROM msx_tasks ORDER BY id').fetchall() == [
            (99991,), (99991,),
        ]
        assert len(connection.execute('PRAGMA foreign_key_check').fetchall()) == 2


@pytest.mark.parametrize('live', [False, True], ids=['rehearsal', 'live'])
def test_offline_cli_writes_result_json_without_starting_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool,
) -> None:
    """Exercise the real CLI parser and JSON writer without running a child command."""
    database = tmp_path / 'cli.db'
    output = tmp_path / 'result.json'
    expected = {
        'success': True, 'repeated': not live,
        'core_counts': {table: 0 for table in CORE_IDS},
        'preexisting_fk_violations': 0, 'remaining_fk_violations': 0,
    }
    calls = []

    def initialize(candidate: Path, *, repeat: bool = True) -> dict:
        """Record only the CLI's offline entrypoint arguments."""
        calls.append((candidate, repeat))
        return expected

    monkeypatch.setattr(upgrade_rehearsal, 'initialize_offline', initialize)
    monkeypatch.setattr(sys, 'argv', [
        'upgrade_rehearsal', '--database', str(database), '--result', str(output),
        *(['--live'] if live else []),
    ])
    assert upgrade_rehearsal.main() == 0
    assert calls == [(database.resolve(), not live)]
    assert json.loads(output.read_text(encoding='utf-8')) == expected


def test_offline_cli_sanitizes_chained_failure_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure output includes useful code locations but no row data, SQL or private paths."""
    output = tmp_path / 'failed.json'
    private_value = 'SYNTHETIC_PRIVATE_CUSTOMER_99991'
    private_path = str(tmp_path / 'private-source.db')

    def fail_initialize(candidate: Path, *, repeat: bool = True) -> dict:
        """Simulate a migration wrapper retaining its original SQLite exception."""
        try:
            raise sqlite3.IntegrityError(
                f'FOREIGN KEY constraint failed: {private_value} SELECT * FROM notes',
            )
        except sqlite3.IntegrityError as error:
            raise RuntimeError(f'Rehearsal failed; backup: {private_path}') from error

    monkeypatch.setattr(upgrade_rehearsal, 'initialize_offline', fail_initialize)
    monkeypatch.setattr(sys, 'argv', [
        'upgrade_rehearsal', '--database', str(tmp_path / 'unused.db'),
        '--result', str(output),
    ])
    with pytest.raises(RuntimeError, match='Rehearsal failed'):
        upgrade_rehearsal.main()
    serialized = output.read_text(encoding='utf-8')
    result = json.loads(serialized)
    assert result['success'] is False
    diagnostic = result['error']
    assert diagnostic['type'] == 'RuntimeError'
    assert diagnostic['category'] == 'migration_foreign_keys'
    assert diagnostic['frames']
    assert private_value not in serialized
    assert private_path not in serialized
    assert str(tmp_path) not in serialized
    assert 'SELECT * FROM notes' not in serialized
    for frame in diagnostic['frames']:
        assert set(frame) == {'file', 'function', 'line'}
        assert '/' not in frame['file']
        assert '\\' not in frame['file']
