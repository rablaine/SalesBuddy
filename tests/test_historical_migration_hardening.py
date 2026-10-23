"""Offline historical migration regressions using synthetic SQLite databases only."""
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import migrations


@pytest.fixture(params=[False, True], ids=['fk-off', 'fk-on'])
def history_db(tmp_path, request):
    """Use a file-backed database with each historical FK enforcement setting."""
    engine = create_engine(f'sqlite:///{tmp_path / "history.db"}', poolclass=StaticPool)
    session = scoped_session(sessionmaker(bind=engine))
    with engine.connect() as connection:
        connection.connection.driver_connection.execute(
            f'PRAGMA foreign_keys={int(request.param)}',
        )
    yield SimpleNamespace(engine=engine, session=session, directory=tmp_path,
                          foreign_keys=int(request.param))
    session.remove()
    engine.dispose()


def seed(database, sql: str) -> None:
    """Install a synthetic historical schema without running the application."""
    with database.engine.connect() as connection:
        connection.connection.driver_connection.executescript(sql)


def assert_integrity(database) -> None:
    """Check actual integrity and preservation of the caller's FK mode."""
    with database.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []
        assert raw.execute('PRAGMA foreign_keys').fetchone() == (database.foreign_keys,)


def test_nullable_action_items_keep_canonical_name_and_entire_schema(history_db):
    """The old path stranded rows in action_items_new and silently lost indexes/triggers."""
    seed(history_db, """
        CREATE TABLE engagements (id INTEGER PRIMARY KEY);
        INSERT INTO engagements VALUES (3);
        CREATE TABLE action_items (
            id INTEGER PRIMARY KEY, engagement_id INTEGER NOT NULL REFERENCES engagements(id),
            detail TEXT NOT NULL DEFAULT 'keep, this', custom_value INTEGER CHECK(custom_value > 0)
        );
        INSERT INTO action_items VALUES (7, 3, 'preserve action', 9);
        CREATE TABLE action_links (item_id INTEGER REFERENCES action_items(id));
        INSERT INTO action_links VALUES (7);
        CREATE INDEX ix_custom_action ON action_items(detail);
        CREATE TRIGGER action_guard BEFORE DELETE ON action_items
        BEGIN SELECT RAISE(ABORT, 'keep action'); END;
    """)
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM action_items').fetchall() == [
            (7, 3, 'preserve action', 9),
        ]
        assert next(column for column in inspect(history_db.engine).get_columns(
            'action_items',
        ) if column['name'] == 'engagement_id')['nullable']
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('ix_custom_action','action_guard')",
        ).fetchone() == (2,)
        assert raw.execute('PRAGMA foreign_key_list(action_links)').fetchone()[2] == 'action_items'
        assert not raw.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('action_items_new','action_items_old')",
        ).fetchall()
    assert_integrity(history_db)
    assert len(list(history_db.directory.glob('*.bak'))) == 1


def test_nullable_notes_keep_extra_columns_and_inbound_links(history_db):
    """Making customer optional must not discard newer fields on an older constraint shape."""
    seed(history_db, """
        CREATE TABLE customers (id INTEGER PRIMARY KEY);
        INSERT INTO customers VALUES (13);
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            call_date DATETIME NOT NULL, content TEXT NOT NULL, created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL, extra_context TEXT NOT NULL DEFAULT 'keep',
            UNIQUE(content, extra_context)
        );
        INSERT INTO notes VALUES (31,13,'2026-10-22 09:30:00','keep note','2026-10-22',
                                  '2026-10-22','extra context');
        CREATE TABLE note_links (note_id INTEGER REFERENCES notes(id));
        INSERT INTO note_links VALUES (31);
        CREATE INDEX ix_extra_note_context ON notes(extra_context);
    """)
    migrations._migrate_notes_nullable_customer(history_db, inspect(history_db.engine))
    migrations._migrate_notes_nullable_customer(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT extra_context FROM notes').fetchone() == ('extra context',)
        assert raw.execute('PRAGMA foreign_key_list(note_links)').fetchone()[2] == 'notes'
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='ix_extra_note_context'",
        ).fetchone() == (1,)
    assert_integrity(history_db)


@pytest.mark.parametrize('partial', [False, True])
def test_note_date_conversion_preserves_children_and_existing_times(history_db, partial):
    """Convert only the column/value intended, preserving other fields and partial helper data."""
    seed(history_db, """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, call_date DATE NOT NULL,
            content TEXT NOT NULL, extra_context TEXT NOT NULL DEFAULT 'keep'
        );
        INSERT INTO notes VALUES (1,'2026-10-22','date only','context');
        INSERT INTO notes VALUES (2,'2026-10-22 10:30:15','existing time','context');
        INSERT INTO notes VALUES (3,'2026-10-22T11:45:20','iso time','context');
        CREATE TABLE note_links (note_id INTEGER REFERENCES notes(id));
        INSERT INTO note_links VALUES (1), (2), (3);
        CREATE INDEX ix_history_note_content ON notes(content);
    """)
    if partial:
        seed(history_db, "ALTER TABLE notes ADD COLUMN call_datetime DATETIME; "
                         "UPDATE notes SET call_datetime='2026-10-22 12:00:00' WHERE id=1;")
    migrations._migrate_call_date_to_datetime(history_db, inspect(history_db.engine))
    migrations._migrate_call_date_to_datetime(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT id,call_date FROM notes ORDER BY id').fetchall() == [
            (1, '2026-10-22 00:00:00'), (2, '2026-10-22 10:30:15'),
            (3, '2026-10-22 11:45:20'),
        ]
        assert raw.execute('PRAGMA foreign_key_list(note_links)').fetchone()[2] == 'notes'
        assert 'DATETIME' == str(next(c for c in inspect(history_db.engine).get_columns(
            'notes',
        ) if c['name'] == 'call_date')['type'])
        assert raw.execute('SELECT extra_context FROM notes').fetchall() == [('context',)] * 3
        if partial:
            assert raw.execute('SELECT call_datetime FROM notes WHERE id=1').fetchone() == (
                '2026-10-22 12:00:00',
            )
    assert_integrity(history_db)


def test_pre_msx_milestones_preserve_ids_and_note_associations(history_db):
    """The original migration explicitly deleted associations and milestone records."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        INSERT INTO customers VALUES (13);
        CREATE TABLE notes(id INTEGER PRIMARY KEY);
        INSERT INTO notes VALUES (31);
        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL, customer_id INTEGER REFERENCES customers(id),
            title TEXT, custom_payload TEXT
        );
        INSERT INTO milestones VALUES (47,'https://example.invalid/old',13,'Keep','custom');
        CREATE TABLE notes_milestones (
            note_id INTEGER REFERENCES notes(id), milestone_id INTEGER REFERENCES milestones(id),
            PRIMARY KEY(note_id,milestone_id)
        );
        INSERT INTO notes_milestones VALUES (31,47);
    """)
    migrations._migrate_milestones_for_msx(history_db, inspect(history_db.engine))
    migrations._migrate_milestones_for_msx(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT id,title,custom_payload FROM milestones').fetchall() == [
            (47, 'Keep', 'custom'),
        ]
        assert raw.execute('SELECT * FROM notes_milestones').fetchall() == [(31,47)]
        assert raw.execute(
            'SELECT msx_milestone_id,milestone_number FROM milestones',
        ).fetchone() == (None,None)
    assert_integrity(history_db)


def test_user_column_removal_does_not_redirect_children_or_drop_extra_fields(history_db):
    """The March rename-original rebuild generated the dangling _old_* parent names."""
    seed(history_db, """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        INSERT INTO users VALUES (1);
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, user_id INTEGER REFERENCES users(id),
            extra_context TEXT NOT NULL DEFAULT 'keep', UNIQUE(name,extra_context)
        );
        INSERT INTO customers VALUES(13,'Keep',1,'historical extra field');
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
            user_id INTEGER REFERENCES users(id), extra_context TEXT NOT NULL
        );
        INSERT INTO notes VALUES(31,13,1,'note extra');
        CREATE TABLE history_links(note_id INTEGER REFERENCES notes(id));
        INSERT INTO history_links VALUES(31);
        CREATE INDEX ix_history_customer_name ON customers(name);
    """)
    migrations._drop_user_id_columns(history_db, inspect(history_db.engine))
    migrations._drop_user_id_columns(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        for table in ('customers','notes'):
            assert 'user_id' not in {c['name'] for c in inspect(history_db.engine).get_columns(table)}
        assert raw.execute('SELECT * FROM customers').fetchall() == [
            (13,'Keep','historical extra field'),
        ]
        assert raw.execute('SELECT * FROM notes').fetchall() == [(31,13,'note extra')]
        assert raw.execute('PRAGMA foreign_key_list(history_links)').fetchone()[2] == 'notes'
        assert raw.execute('PRAGMA foreign_key_list(notes)').fetchone()[2] == 'customers'
    assert_integrity(history_db)


def test_customer_unique_migration_leaves_multiple_unknown_tpids_alone(history_db):
    """NULL TPIDs may repeat; the old GROUP BY NULL branch indexed an empty result."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid BIGINT);
        INSERT INTO customers VALUES(1,NULL),(2,NULL);
    """)
    migrations._migrate_customer_tpid_unique(history_db, inspect(history_db.engine))
    migrations._migrate_customer_tpid_unique(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM customers ORDER BY id')).fetchall() == [
            (1,None),(2,None),
        ]


def test_customer_dedup_keeps_every_actual_foreign_key_link(history_db):
    """Dedup cannot use obsolete table names or forget newer customer-linked entities."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid BIGINT,name TEXT);
        INSERT INTO customers VALUES(1,123,'Keep'),(2,123,'Duplicate');
        CREATE TABLE notes(id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(id));
        INSERT INTO notes VALUES(31,1),(32,1),(33,2);
        CREATE TABLE revenue_analyses(id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id), review TEXT);
        INSERT INTO revenue_analyses VALUES(4,2,'Preserve');
        CREATE TABLE newer_customer_data(id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id), payload TEXT);
        INSERT INTO newer_customer_data VALUES(5,2,'Preserve newer relationship');
    """)
    migrations._migrate_customer_tpid_unique(history_db, inspect(history_db.engine))
    migrations._migrate_customer_tpid_unique(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM customers').fetchall() == [(1,123,'Keep')]
        assert raw.execute('SELECT customer_id FROM notes').fetchall() == [(1,),(1,),(1,)]
        assert raw.execute('SELECT customer_id,review FROM revenue_analyses').fetchone() == (
            1,'Preserve',
        )
        assert raw.execute('SELECT customer_id,payload FROM newer_customer_data').fetchone() == (
            1,'Preserve newer relationship',
        )
    assert_integrity(history_db)


def test_rebuild_preserves_views_external_triggers_and_autoincrement_highwater(history_db):
    """Renaming a replacement must not rewrite views or reuse deleted high IDs."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY);
        INSERT INTO engagements VALUES(3);
        CREATE TABLE action_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id),
            detail TEXT DEFAULT 'kept'
        );
        INSERT INTO action_items(id,engagement_id) VALUES(7,3),(999,3);
        DELETE FROM action_items WHERE id=999;
        CREATE VIEW saved_actions AS SELECT id,detail FROM action_items;
        CREATE TABLE event_source(id INTEGER PRIMARY KEY);
        CREATE TRIGGER external_action_writer AFTER INSERT ON event_source
        BEGIN INSERT INTO action_items(engagement_id,detail) VALUES(3,'external trigger'); END;
    """)
    with history_db.engine.connect() as connection:
        connection.connection.driver_connection.execute('PRAGMA legacy_alter_table=OFF')
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM saved_actions').fetchall() == [(7,'kept')]
        assert raw.execute('PRAGMA legacy_alter_table').fetchone() == (0,)
        raw.execute('INSERT INTO event_source VALUES(1)')
        assert raw.execute(
            "SELECT id FROM action_items WHERE detail='external trigger'",
        ).fetchone() == (1000,)
        raw.commit()
    assert_integrity(history_db)


def test_user_column_removal_retains_quoted_defaults_and_composite_keys(history_db):
    """Parse real DDL, not a list of current model columns, including WITHOUT ROWID."""
    seed(history_db, """
        CREATE TABLE customers(
            id INTEGER PRIMARY KEY, name TEXT DEFAULT 'last, first',
            "user_id" INTEGER, extra TEXT CHECK(extra IN ('a,b','c'))
        ) WITHOUT ROWID;
        INSERT INTO customers VALUES(13,'Last, First',1,'a,b');
        CREATE INDEX ix_old_owner ON customers(user_id);
        CREATE INDEX ix_unrelated_name ON customers(name);
    """)
    migrations._drop_user_id_columns(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM customers').fetchall() == [(13,'Last, First','a,b')]
        schema = raw.execute("SELECT sql FROM sqlite_master WHERE name='customers'").fetchone()[0]
        assert 'WITHOUT ROWID' in schema
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL",
        ).fetchall() == [('ix_unrelated_name',)]
    assert_integrity(history_db)


def test_rebuild_failure_rolls_back_and_retry_preserves_original(history_db, monkeypatch):
    """A failure after copying must not leave a renamed parent or a success-shaped result."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY);
        INSERT INTO engagements VALUES(3);
        CREATE TABLE action_items(id INTEGER PRIMARY KEY,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id), detail TEXT);
        INSERT INTO action_items VALUES(7,3,'original');
    """)
    original = migrations._rebuild_sqlite_table

    def break_schema(database, table, sql, replacement, label, **kwargs):
        """Inject a real constraint failure during copying, not a mocked migration outcome."""
        sql = sql.replace('detail TEXT', "detail TEXT CHECK(detail='impossible')")
        return original(database, table, sql, replacement, label, **kwargs)

    monkeypatch.setattr(migrations, '_rebuild_sqlite_table', break_schema)
    with pytest.raises(RuntimeError, match='migration failed; rolled back'):
        migrations._make_column_nullable(
            history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
        )
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM action_items').fetchall() == [(7,3,'original')]
        assert raw.execute('PRAGMA table_info(action_items)').fetchall()[1][3] == 1
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_action_items_engagement_id_nullable'",
        ).fetchone() == (0,)
    assert_integrity(history_db)
    monkeypatch.setattr(migrations, '_rebuild_sqlite_table', original)
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    assert_integrity(history_db)
    assert len(list(history_db.directory.glob('*.bak'))) == 1


def test_user_column_dependent_trigger_aborts_without_silently_breaking_it(history_db):
    """SQLite accepts trigger bodies with missing NEW fields, so detect that before removal."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,user_id INTEGER,name TEXT);
        INSERT INTO customers VALUES(13,1,'keep');
        CREATE TRIGGER legacy_owner_guard BEFORE UPDATE ON customers
        WHEN NEW.user_id IS NULL BEGIN SELECT RAISE(ABORT,'requires owner'); END;
    """)
    with pytest.raises(RuntimeError, match='migration failed; rolled back'):
        migrations._drop_user_id_columns(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM customers').fetchall() == [(13,1,'keep')]
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='legacy_owner_guard'",
        ).fetchone() == (1,)
    assert_integrity(history_db)


def test_unparseable_note_date_fails_without_mutating_any_original_value(history_db):
    """Do not replace malformed dates with fabricated January timestamps."""
    seed(history_db, """
        CREATE TABLE notes(id INTEGER PRIMARY KEY,call_date DATE NOT NULL,content TEXT);
        INSERT INTO notes VALUES(1,'not a date','keep');
    """)
    with pytest.raises(RuntimeError, match='Unrecognized historical call_date'):
        migrations._migrate_call_date_to_datetime(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM notes')).fetchall() == [
            (1,'not a date','keep'),
        ]
    assert_integrity(history_db)


def test_customer_dedup_constraint_conflict_is_atomic(history_db):
    """Conflicting association metadata must not be discarded merely to enforce uniqueness."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid BIGINT,name TEXT);
        INSERT INTO customers VALUES(1,123,'Keep'),(2,123,'Duplicate');
        CREATE TABLE memberships(
            customer_id INTEGER REFERENCES customers(id), category TEXT, detail TEXT,
            PRIMARY KEY(customer_id,category)
        );
        INSERT INTO memberships VALUES(1,'same','first metadata'),(2,'same','different metadata');
    """)
    with pytest.raises(RuntimeError, match='deduplication failed; rolled back'):
        migrations._migrate_customer_tpid_unique(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT COUNT(*) FROM customers').fetchone() == (2,)
        assert raw.execute('SELECT * FROM memberships ORDER BY customer_id').fetchall() == [
            (1,'same','first metadata'),(2,'same','different metadata'),
        ]
    assert_integrity(history_db)


def test_rebuild_preserves_unchanged_preexisting_noncore_audit_orphans(history_db):
    """Do not turn an unrelated old audit link into a new upgrade blocker during a rebuild."""
    seed(history_db, """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        INSERT INTO customers VALUES(13);
        CREATE TABLE notes(id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id), content TEXT);
        INSERT INTO notes VALUES(31,13,'keep');
        CREATE TABLE audit_history(id INTEGER PRIMARY KEY,note_id INTEGER REFERENCES notes(id));
        INSERT INTO audit_history VALUES(1,31),(2,999);
    """)
    with history_db.engine.connect() as connection:
        connection.connection.driver_connection.execute(
            f'PRAGMA foreign_keys={history_db.foreign_keys}',
        )
    migrations._migrate_notes_nullable_customer(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT * FROM audit_history ORDER BY id').fetchall() == [(1,31),(2,999)]
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == [
            ('audit_history',2,'notes',0),
        ]


def test_nullable_constraint_parser_does_not_edit_literal_or_nested_check(history_db):
    """SQL-looking text in defaults and CHECK expressions is not the nullable constraint."""
    seed(history_db, """
        CREATE TABLE action_items(
            id INTEGER PRIMARY KEY,
            engagement_id INTEGER DEFAULT 'NOT NULL' CHECK(engagement_id IS NOT NULL) NOT NULL
        );
        INSERT INTO action_items VALUES(7,3);
    """)
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        schema = raw.execute("SELECT sql FROM sqlite_master WHERE name='action_items'").fetchone()[0]
        assert "DEFAULT 'NOT NULL'" in schema
        assert 'CHECK(engagement_id IS NOT NULL)' in schema
        assert raw.execute('PRAGMA table_info(action_items)').fetchall()[1][3] == 0
    assert_integrity(history_db)


def test_add_column_rechecks_stale_inspector_and_never_swallows_database_errors(
    history_db, monkeypatch,
):
    """A reused inspector must see successful DDL and must not hide a failed schema query."""
    seed(history_db, 'CREATE TABLE historical(id INTEGER PRIMARY KEY);')
    inspector = inspect(history_db.engine)
    migrations._add_column_if_not_exists(history_db, inspector, 'historical', 'added', 'TEXT')
    migrations._add_column_if_not_exists(history_db, inspector, 'historical', 'added', 'TEXT')
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT added FROM historical')).fetchall() == []

    def fail_columns(_table):
        """A schema read error must stop migration, not pretend the table is absent."""
        raise RuntimeError('synthetic schema failure')

    monkeypatch.setattr(inspector, 'get_columns', fail_columns)
    with pytest.raises(RuntimeError, match='synthetic schema failure'):
        migrations._add_column_if_not_exists(history_db, inspector, 'historical', 'other', 'TEXT')


@pytest.mark.parametrize('partial', ['none','renamed','added','partly-filled'])
def test_historical_acr_conversion_preserves_links_schema_and_partial_evidence(history_db, partial):
    """Currency conversion must not drop raw text after a caught parse error or interrupted DDL."""
    seed(history_db, """
        CREATE TABLE engagements(
            id INTEGER PRIMARY KEY, estimated_acr VARCHAR(50), extra_context TEXT NOT NULL
        );
        INSERT INTO engagements VALUES(1,'$500/mo','keep one'),(2,'$50k/month','keep two'),
                                      (3,'1,234.99','keep three'),(4,NULL,'keep null');
        CREATE TABLE engagement_history(id INTEGER PRIMARY KEY,
            engagement_id INTEGER REFERENCES engagements(id));
        INSERT INTO engagement_history VALUES(9,1);
        CREATE INDEX ix_history_acr ON engagements(estimated_acr);
        CREATE VIEW historic_acr_view AS SELECT id,extra_context FROM engagements;
    """)
    if partial != 'none':
        seed(history_db, 'ALTER TABLE engagements RENAME COLUMN estimated_acr TO _estimated_acr_old;')
        if partial != 'renamed':
            seed(history_db, 'ALTER TABLE engagements ADD COLUMN estimated_acr INTEGER;')
        if partial == 'partly-filled':
            seed(history_db, 'UPDATE engagements SET estimated_acr=500 WHERE id=1;')
    migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT id,estimated_acr FROM engagements ORDER BY id').fetchall() == [
            (1,500),(2,50000),(3,1234),(4,None),
        ]
        assert raw.execute('SELECT * FROM engagement_history').fetchall() == [(9,1)]
        assert raw.execute('SELECT extra_context FROM historic_acr_view ORDER BY id').fetchall() == [
            ('keep one',),('keep two',),('keep three',),('keep null',),
        ]
        if partial != 'none':
            assert raw.execute(
                'SELECT _estimated_acr_original FROM engagements ORDER BY id',
            ).fetchall() == [('$500/mo',),('$50k/month',),('1,234.99',),(None,)]
    assert_integrity(history_db)
    assert len(list(history_db.directory.glob('*.bak'))) == 1


@pytest.mark.parametrize('amount', ['not numeric', 'Infinity', '9999999999999999999999', ''])
def test_unrecognized_acr_is_not_silently_replaced_with_null(history_db, amount):
    """The previous broad catches logged warnings and then deleted the only original value."""
    seed(history_db, 'CREATE TABLE engagements(id INTEGER PRIMARY KEY, estimated_acr TEXT);')
    with history_db.engine.begin() as connection:
        connection.execute(text('INSERT INTO engagements VALUES(1,:amount)'), {'amount': amount})
    with pytest.raises(RuntimeError, match='historical|Historical'):
        migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM engagements')).fetchall() == [(1,amount)]


@pytest.mark.parametrize('amount,expected', [
    ('1.999999999999999999999999999999', 1),
    ('-1.999999999999999999999999999999', -1),
    ('$0.001999999999999999999999999999999k/month', 1),
])
def test_historical_acr_truncates_without_decimal_context_rounding(amount, expected):
    """High-precision source text must not round upward before conversion to whole dollars."""
    assert migrations._parse_historical_acr(amount) == expected


def test_conflicting_partial_acr_is_not_overwritten(history_db):
    """Two disagreeing values are evidence requiring review, not permission to pick one."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY,
            estimated_acr INTEGER, _estimated_acr_old TEXT);
        INSERT INTO engagements VALUES(1,999,'$500');
    """)
    with pytest.raises(RuntimeError, match='Conflicting interrupted'):
        migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM engagements')).fetchall() == [(1,999,'$500')]


@pytest.mark.parametrize('starting_amount', [None, 500])
def test_recovered_acr_allows_later_user_edits_without_replaying_original(
    history_db, starting_amount,
):
    """Preserved conversion evidence is not authoritative after a successful recovery."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY,
            estimated_acr INTEGER, _estimated_acr_old TEXT);
    """)
    with history_db.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO engagements VALUES(1,:amount,'$500')"),
            {'amount': starting_amount},
        )
    migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    seed(history_db, 'UPDATE engagements SET estimated_acr=750;')
    migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT estimated_acr FROM engagements')).scalar_one() == 750


def test_conflicting_acr_archive_is_preserved_for_review(history_db):
    """A previously archived value must never be overwritten by another temporary source."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY, estimated_acr INTEGER,
            _estimated_acr_old TEXT, _estimated_acr_original TEXT);
        INSERT INTO engagements VALUES(1,500,'$500','different historical evidence');
    """)
    with pytest.raises(RuntimeError, match='Conflicting estimated ACR recovery archives'):
        migrations._migrate_estimated_acr_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM engagements')).fetchall() == [
            (1,500,'$500','different historical evidence'),
        ]


@pytest.mark.parametrize('value', ['garbage','123.5','1e3','99999999999999999999999999'])
def test_invalid_tpid_does_not_become_zero_or_truncated_integer(history_db, value):
    """SQLite CAST accepts bad input as zero, which could later merge unrelated customers."""
    seed(history_db, 'CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid BLOB);')
    with history_db.engine.begin() as connection:
        connection.execute(text('INSERT INTO customers VALUES(1,:value)'), {'value': value})
    with pytest.raises(RuntimeError, match='TPID'):
        migrations._migrate_customer_tpid_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT tpid FROM customers')).scalar_one() == value


def test_valid_tpid_conversion_is_repeatable(history_db):
    """Integral legacy values convert without changing unknown TPIDs or customer identities."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid BLOB);
        INSERT INTO customers VALUES(1,'00123'),(2,456.0),(3,NULL);
    """)
    migrations._migrate_customer_tpid_to_int(history_db, inspect(history_db.engine))
    migrations._migrate_customer_tpid_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(text('SELECT * FROM customers ORDER BY id')).fetchall() == [
            (1,123),(2,456),(3,None),
        ]


def test_legacy_alignment_selections_are_not_deleted(history_db):
    """Old seller-scoped alignment intent must survive conversion to territory-only inserts."""
    seed(history_db, """
        CREATE TABLE user_preferences(id INTEGER PRIMARY KEY);
        CREATE TABLE alignment_selections(
            id INTEGER PRIMARY KEY, fy_label TEXT NOT NULL, msx_territory_id TEXT NOT NULL,
            territory_name TEXT NOT NULL, seller_msx_user_id TEXT NOT NULL,
            seller_name TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(fy_label,msx_territory_id,seller_msx_user_id)
        );
        INSERT INTO alignment_selections(
            id,fy_label,msx_territory_id,territory_name,seller_msx_user_id,seller_name
        ) VALUES(7,'FY27','territory-a','Keep territory','seller-a','Keep seller metadata');
    """)
    migrations._migrate_alignment_tables(history_db, inspect(history_db.engine))
    migrations._migrate_alignment_tables(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute(
            'SELECT id,territory_name,seller_name FROM alignment_selections',
        ).fetchall() == [(7,'Keep territory','Keep seller metadata')]
        raw.execute(
            'INSERT INTO alignment_selections(id,fy_label,msx_territory_id,territory_name) '
            "VALUES(8,'FY27','territory-b','New territory-only selection')",
        )
        raw.commit()
    assert_integrity(history_db)


def test_tpid_text_affinity_converts_storage_type_and_retains_links(history_db):
    """Updating a VARCHAR column alone cannot persist integers because of SQLite affinity."""
    seed(history_db, """
        CREATE TABLE customers(id INTEGER PRIMARY KEY,tpid VARCHAR(50),extra TEXT);
        INSERT INTO customers VALUES(13,'00123','keep'),(14,NULL,'unknown');
        CREATE TABLE notes(id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(id));
        INSERT INTO notes VALUES(31,13);
        CREATE INDEX ix_customer_extra ON customers(extra);
    """)
    for _ in range(2):
        migrations._migrate_customer_tpid_to_int(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute('SELECT id,tpid,typeof(tpid),extra FROM customers').fetchall() == [
            (13,123,'integer','keep'),(14,None,'null','unknown'),
        ]
        assert raw.execute('SELECT * FROM notes').fetchall() == [(31,13)]
        assert raw.execute("SELECT name FROM sqlite_master WHERE name='ix_customer_extra'").fetchone()
    assert_integrity(history_db)


def test_duplicate_legacy_alignment_is_blocked_without_deleting_user_intent(history_db):
    """Conflicting seller-scoped rows are not automatically collapsed or reset to empty."""
    seed(history_db, """
        CREATE TABLE user_preferences(id INTEGER PRIMARY KEY);
        CREATE TABLE alignment_selections(
            id INTEGER PRIMARY KEY, fy_label TEXT, msx_territory_id TEXT,
            seller_msx_user_id TEXT NOT NULL, seller_name TEXT NOT NULL, active BOOLEAN
        );
        INSERT INTO alignment_selections VALUES(1,'FY27','same','one','First',1),
                                               (2,'FY27','same','two','Second',0);
    """)
    with pytest.raises(RuntimeError, match='Conflicting historical territory selections'):
        migrations._migrate_alignment_tables(history_db, inspect(history_db.engine))
    with history_db.engine.connect() as connection:
        assert connection.execute(
            text('SELECT id,seller_name,active FROM alignment_selections ORDER BY id'),
        ).fetchall() == [(1,'First',1),(2,'Second',0)]


def test_process_death_after_original_drop_rolls_back_whole_rebuild(history_db):
    """Exercise SQLite crash recovery after DROP, not just Python exception rollback."""
    seed(history_db, """
        CREATE TABLE engagements(id INTEGER PRIMARY KEY);
        INSERT INTO engagements VALUES(3);
        CREATE TABLE action_items(id INTEGER PRIMARY KEY,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id), detail TEXT);
        INSERT INTO action_items VALUES(7,3,'survive process death');
        CREATE INDEX ix_crash_detail ON action_items(detail);
    """)
    code = """
import os
import sys
from types import SimpleNamespace
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from app.migrations import _make_column_nullable
engine = create_engine('sqlite:///' + sys.argv[1], poolclass=StaticPool)
session = scoped_session(sessionmaker(bind=engine))
with engine.connect() as connection:
    raw = connection.connection.driver_connection
    def crash(statement):
        if statement.startswith('ALTER TABLE \"_action_items_engagement_id_nullable\"'):
            os._exit(73)
    raw.set_trace_callback(crash)
_make_column_nullable(SimpleNamespace(engine=engine,session=session),
                      inspect(engine), 'action_items', 'engagement_id')
raise RuntimeError('The intended crash point was not reached')
"""
    path = history_db.directory / 'history.db'
    result = subprocess.run(
        [sys.executable,'-c',code,str(path)], cwd=Path(__file__).parents[1],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute('SELECT * FROM action_items').fetchall() == [
            (7,3,'survive process death'),
        ]
        assert raw.execute('PRAGMA table_info(action_items)').fetchall()[1][3] == 1
        assert raw.execute("SELECT name FROM sqlite_master WHERE name='ix_crash_detail'").fetchone()
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE name='_action_items_engagement_id_nullable'",
        ).fetchone() is None
        assert raw.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    migrations._make_column_nullable(
        history_db, inspect(history_db.engine), 'action_items', 'engagement_id',
    )
    assert_integrity(history_db)


def replace_fixture_table(raw, table: str, schema: str) -> None:
    """Install synthetic historical DDL while keeping fixture rows and unrelated schema."""
    objects = raw.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
        "AND sql IS NOT NULL", (table,),
    ).fetchall()
    schema, changed = re.subn(
        rf'CREATE TABLE "?{table}"?', 'CREATE TABLE fixture_new', schema, count=1,
    )
    assert changed == 1
    before = {row[1] for row in raw.execute(f'PRAGMA table_info("{table}")')}
    raw.execute(schema)
    after = {row[1] for row in raw.execute('PRAGMA table_info(fixture_new)')}
    names = ','.join(f'"{column}"' for column in sorted(before & after))
    raw.execute(f'INSERT INTO fixture_new ({names}) SELECT {names} FROM "{table}"')
    raw.execute(f'DROP TABLE "{table}"')
    raw.execute(f'ALTER TABLE fixture_new RENAME TO "{table}"')
    for (sql,) in objects:
        raw.execute(sql)


def seed_full_history(path: Path, scenario: str) -> None:
    """Create real model-valid records and project them onto an earlier constraint/type shape."""
    from app.models import ActionItem, Customer, Engagement, Milestone, Note, User, db

    engine = create_engine(f'sqlite:///{path}')
    try:
        db.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(User.__table__.insert().values(
                id=1, email='synthetic@example.invalid', name='Synthetic', is_admin=True,
            ))
            connection.execute(Customer.__table__.insert().values(
                id=13, name='Synthetic offline migration account', tpid=-999,
            ))
            connection.execute(Note.__table__.insert().values(
                id=31, customer_id=13, content='Keep the synthetic note',
                call_date=datetime(2026,10,22,10,30),
            ))
            connection.execute(Milestone.__table__.insert().values(
                id=47, customer_id=13, url='https://example.invalid/fixture', title='Keep milestone',
            ))
            connection.execute(Engagement.__table__.insert().values(
                id=61, customer_id=13, title='Keep engagement',
            ))
            connection.execute(ActionItem.__table__.insert().values(
                id=71, engagement_id=61, note_id=31, title='Keep action',
            ))
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute('INSERT INTO notes_milestones VALUES(31,47)')
        raw.execute('INSERT INTO notes_engagements VALUES(31,61)')
        raw.execute('INSERT INTO engagements_milestones VALUES(61,47)')
        for table in ('customers','notes','milestones','action_items'):
            raw.execute(f'ALTER TABLE "{table}" ADD COLUMN custom_history TEXT')
            raw.execute(f"UPDATE \"{table}\" SET custom_history='preserve extra, field'")
        if scenario in ('date','partial-date','combined'):
            schema = raw.execute("SELECT sql FROM sqlite_master WHERE name='notes'").fetchone()[0]
            schema = schema.replace('call_date DATETIME', 'call_date DATE')
            replace_fixture_table(raw, 'notes', schema)
            if scenario == 'partial-date':
                raw.execute('ALTER TABLE notes ADD COLUMN call_datetime DATETIME')
                raw.execute("UPDATE notes SET call_datetime='2026-10-22 12:45:00'")
        if scenario in ('notes-nullable','combined'):
            schema = raw.execute("SELECT sql FROM sqlite_master WHERE name='notes'").fetchone()[0]
            schema = schema.replace('customer_id INTEGER,', 'customer_id INTEGER NOT NULL,')
            assert 'customer_id INTEGER NOT NULL' in schema
            replace_fixture_table(raw, 'notes', schema)
        if scenario in ('ownership','combined'):
            for table in ('customers','notes','milestones'):
                raw.execute(f'ALTER TABLE "{table}" ADD COLUMN user_id INTEGER REFERENCES users(id)')
                raw.execute(f'UPDATE "{table}" SET user_id=1')
        if scenario in ('milestone','combined'):
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='milestones'",
            ).fetchone()[0]
            schema, count = re.subn(r'^\s*msx_milestone_id [^\n]+\n', '', schema,
                                    flags=re.MULTILINE)
            assert count == 1
            schema, count = re.subn(r'^\s*UNIQUE \(msx_milestone_id\),?\s*\n?', '',
                                    schema, flags=re.MULTILINE)
            assert count == 1
            # Removing a final table constraint may leave the preceding comma.
            schema = re.sub(r',\s*\)', '\n)', schema)
            replace_fixture_table(raw, 'milestones', schema)
        if scenario in ('action','combined'):
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='action_items'",
            ).fetchone()[0]
            schema = schema.replace('engagement_id INTEGER,', 'engagement_id INTEGER NOT NULL,')
            assert 'engagement_id INTEGER NOT NULL' in schema
            replace_fixture_table(raw, 'action_items', schema)
        if scenario in ('acr','partial-acr','combined'):
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='engagements'",
            ).fetchone()[0]
            schema, count = re.subn(
                r'(^\s*estimated_acr\s+)INTEGER\b', r'\1VARCHAR(50)',
                schema, count=1, flags=re.MULTILINE,
            )
            assert count == 1
            replace_fixture_table(raw, 'engagements', schema)
            raw.execute("UPDATE engagements SET estimated_acr='$50k/month'")
            if scenario == 'partial-acr':
                raw.execute(
                    'ALTER TABLE engagements RENAME COLUMN estimated_acr TO _estimated_acr_old',
                )
                raw.execute('ALTER TABLE engagements ADD COLUMN estimated_acr INTEGER')
        if scenario in ('tpid','combined'):
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='customers'",
            ).fetchone()[0]
            schema, count = re.subn(r'(\btpid\s+)BIGINT\b', r'\1VARCHAR(50)', schema)
            assert count == 1
            replace_fixture_table(raw, 'customers', schema)
        if scenario == 'alignment':
            raw.execute('ALTER TABLE alignment_selections ADD COLUMN seller_msx_user_id '
                        "TEXT NOT NULL DEFAULT 'historical-seller'")
            raw.execute('ALTER TABLE alignment_selections ADD COLUMN seller_name '
                        "TEXT NOT NULL DEFAULT 'Keep seller metadata'")
            raw.execute(
                'INSERT INTO alignment_selections(id,fy_label,msx_territory_id,territory_name,'
                'active,created_at,updated_at) '
                "VALUES(83,'FY27','synthetic-territory','Keep territory',1,"
                "'2026-07-20 10:00:00','2026-07-20 10:00:00')",
            )
        if scenario == 'call-logs':
            raw.execute('ALTER TABLE notes RENAME TO call_logs')
        raw.execute('CREATE VIEW history_notes AS SELECT id,content FROM '
                    + ('call_logs' if scenario == 'call-logs' else 'notes'))


@pytest.mark.parametrize('scenario', [
    'date','partial-date','notes-nullable','ownership','milestone','action','combined','call-logs',
    'acr','partial-acr','tpid','alignment',
])
def test_full_initializer_historical_schema_matrix(tmp_path, scenario):
    """Run the exact real startup twice, not just isolated migration helpers."""
    from app.upgrade_rehearsal import initialize_offline

    path = tmp_path / 'history.db'
    seed_full_history(path, scenario)
    with closing(sqlite3.connect(path)) as raw:
        before = {
            table: raw.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in ('notes_milestones','notes_engagements','engagements_milestones')
        }
    result = initialize_offline(path)
    assert result['success'] and result['repeated']
    assert result['remaining_fk_violations'] == 0
    assert result['core_counts'] == {
        'customers':1,'notes':1,'milestones':1,'msx_tasks':0,'engagements':1,
    }
    assert initialize_offline(path)['success']
    with closing(sqlite3.connect(path)) as raw:
        for table, expected in before.items():
            assert raw.execute(f'SELECT * FROM "{table}"').fetchall() == expected
        for table in ('customers','notes','milestones','action_items'):
            assert raw.execute(f'SELECT custom_history FROM "{table}"').fetchone() == (
                'preserve extra, field',
            )
        assert raw.execute('SELECT id,content FROM notes').fetchall() == [
            (31,'Keep the synthetic note'),
        ]
        assert raw.execute('SELECT call_date FROM notes').fetchone() == (
            '2026-10-22 10:30:00.000000',
        )
        assert raw.execute('SELECT id,title FROM action_items').fetchall() == [(71,'Keep action')]
        if scenario in ('acr','partial-acr','combined'):
            assert raw.execute('SELECT estimated_acr FROM engagements').fetchone() == (50000,)
        if scenario in ('tpid','combined'):
            assert raw.execute('SELECT tpid,typeof(tpid) FROM customers').fetchone() == (
                -999,'integer',
            )
        if scenario == 'alignment':
            assert raw.execute(
                'SELECT id,territory_name,seller_name FROM alignment_selections',
            ).fetchall() == [(83,'Keep territory','Keep seller metadata')]
        assert raw.execute('SELECT * FROM history_notes').fetchall() == [
            (31,'Keep the synthetic note'),
        ]
        assert raw.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []
    if scenario == 'partial-acr':
        with closing(sqlite3.connect(path)) as raw, raw:
            raw.execute('UPDATE engagements SET estimated_acr=75000')
        assert initialize_offline(path)['success']
        with closing(sqlite3.connect(path)) as raw:
            assert raw.execute('SELECT estimated_acr FROM engagements').fetchone() == (75000,)


@pytest.mark.parametrize('scenario', ['bad-date', 'bad-acr', 'duplicate-customers'])
def test_real_rehearsal_blocks_ambiguous_history_without_touching_source(tmp_path, scenario):
    """The actual updater rejects unsafe history using its private copy and sanitized result."""
    from scripts.upgrade import RehearsalError, rehearse_database

    source = tmp_path / 'original.db'
    seed_full_history(source, {'bad-date': 'date', 'bad-acr': 'acr'}.get(scenario, 'ownership'))
    with closing(sqlite3.connect(source)) as raw, raw:
        if scenario == 'bad-date':
            raw.execute("UPDATE notes SET call_date='not a date'")
        elif scenario == 'bad-acr':
            raw.execute("UPDATE engagements SET estimated_acr='unknown amount'")
        else:
            schema = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name='customers'",
            ).fetchone()[0]
            schema = schema.replace('UNIQUE (tpid)', 'CHECK (tpid IS NULL OR tpid <> 0)')
            replace_fixture_table(raw, 'customers', schema)
            for index in raw.execute('PRAGMA index_list(customers)').fetchall():
                columns = raw.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
                if index[2] and [column[2] for column in columns] == ['tpid']:
                    raw.execute(f'DROP INDEX "{index[1]}"')
            columns = [row[1] for row in raw.execute('PRAGMA table_info(customers)')]
            values = ','.join('14' if column == 'id' else f'"{column}"' for column in columns)
            raw.execute(f'INSERT INTO customers SELECT {values} FROM customers WHERE id=13')
    original = source.read_bytes()
    trial = tmp_path / 'rehearsal'
    with pytest.raises(RehearsalError):
        rehearse_database(Path(__file__).parents[1], source, trial)
    assert source.read_bytes() == original
    assert not list(tmp_path.glob('original.db.*'))
    result = json.loads((trial / 'rehearsal-result.json').read_text())
    assert result['success'] is False and result['error']
    with closing(sqlite3.connect(source)) as raw:
        assert raw.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert raw.execute('SELECT id FROM notes').fetchall() == [(31,)]
        if scenario == 'duplicate-customers':
            assert raw.execute('SELECT id FROM customers ORDER BY id').fetchall() == [(13,),(14,)]


def test_real_initializer_prunes_legacy_backup_pile_after_preserving_records(tmp_path):
    """A real successful startup reclaims existing backup storms, not only newly created files."""
    from app.upgrade_rehearsal import initialize_offline

    path = tmp_path / 'history.db'
    seed_full_history(path, 'ownership')
    snapshots = []
    with closing(sqlite3.connect(path)) as source:
        for index in range(121):
            target = path.with_name(
                f'{path.name}.owned-tasks-20261022T010000{index:06d}Z-{index:032x}.bak',
            )
            with closing(sqlite3.connect(target)) as saved:
                source.backup(saved)
            snapshots.append(target)
    result = initialize_offline(path)
    assert result['success'] and result['repeated']
    assert [snapshot for snapshot in snapshots if snapshot.exists()] == snapshots[-2:]
    assert len(list(tmp_path.glob('*.single-user-*.bak'))) == 2
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT id,content,customer_id FROM notes').fetchall() == [
            (31, 'Keep the synthetic note', 13),
        ]
        assert connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
