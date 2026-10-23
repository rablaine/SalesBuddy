"""Reproduce deployed legacy revenue FK damage without opening a production database."""
import sqlite3
from contextlib import closing
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.migrations import _cleanup_total_revenue_rows, _migrate_legacy_revenue_parents


@pytest.fixture
def revenue_db(tmp_path):
    """Create real SQLite legacy tables, child links, indexes and triggers with stable IDs."""
    path = tmp_path / 'legacy-revenue.db'
    engine = create_engine(f'sqlite:///{path}', poolclass=StaticPool)
    session = scoped_session(sessionmaker(bind=engine))
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE customers (id INTEGER PRIMARY KEY);
            CREATE TABLE notes (id INTEGER PRIMARY KEY);
            CREATE TABLE revenue_imports (id INTEGER PRIMARY KEY);
            INSERT INTO customers VALUES (31);
            INSERT INTO notes VALUES (41);
            INSERT INTO revenue_imports VALUES (51);
            CREATE TABLE customer_revenue_data (
                id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_id INTEGER REFERENCES "_old_customers"(id),
                last_import_id INTEGER NOT NULL REFERENCES "_old_revenue_imports"(id),
                revenue REAL NOT NULL CHECK (revenue >= 0),
                UNIQUE(customer_name, last_import_id)
            );
            CREATE TABLE product_revenue_data (
                id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_id INTEGER REFERENCES "_old_customers"(id),
                last_import_id INTEGER NOT NULL REFERENCES "_old_revenue_imports"(id),
                revenue REAL NOT NULL
            );
            CREATE TABLE revenue_analyses (
                id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_id INTEGER REFERENCES "_old_customers"(id),
                review_notes TEXT
            );
            CREATE TABLE revenue_engagements (
                id INTEGER PRIMARY KEY,
                analysis_id INTEGER NOT NULL REFERENCES revenue_analyses(id),
                note_id INTEGER REFERENCES "call_logs_backup"(id),
                status TEXT NOT NULL
            );
            CREATE TABLE revenue_review_notes (
                id INTEGER PRIMARY KEY,
                analysis_id INTEGER NOT NULL REFERENCES revenue_analyses(id),
                review_notes TEXT NOT NULL
            );
            CREATE INDEX ix_legacy_customer_revenue ON customer_revenue_data(customer_id);
            CREATE TRIGGER keep_revenue_positive BEFORE UPDATE ON customer_revenue_data
            WHEN NEW.revenue < 0 BEGIN SELECT RAISE(ABORT, 'negative revenue'); END;
            INSERT INTO customer_revenue_data VALUES (1, 'Keep', 31, 51, 123.45);
            INSERT INTO product_revenue_data VALUES (2, 'Keep', 31, 51, 67.89);
            INSERT INTO revenue_analyses VALUES (3, 'Keep', 31, 'Preserve this review');
            INSERT INTO revenue_engagements VALUES (4, 3, 41, 'completed');
            INSERT INTO revenue_review_notes VALUES (5, 3, 'Preserve review history');
            PRAGMA foreign_keys=ON;
        """)
    yield SimpleNamespace(engine=engine, session=session, path=path)
    session.remove()
    engine.dispose()


def rows(raw) -> dict:
    """Capture every fixture value, including inbound child records."""
    return {table: raw.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            for table in ('customer_revenue_data', 'product_revenue_data', 'revenue_analyses',
                          'revenue_engagements', 'revenue_review_notes')}


def test_real_cleanup_failure_repaired_before_write_with_all_records_preserved(revenue_db):
    """The production error occurs even with zero Total rows; all repaired SQL must compile."""
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        before = rows(raw)
        for table in ('customer_revenue_data', 'product_revenue_data', 'revenue_analyses'):
            with pytest.raises(sqlite3.OperationalError, match='no such table: main._old_'):
                raw.execute(f"EXPLAIN DELETE FROM {table} WHERE LOWER(customer_name)='total'")
    _migrate_legacy_revenue_parents(revenue_db)
    _cleanup_total_revenue_rows(revenue_db, list(before))
    _migrate_legacy_revenue_parents(revenue_db)
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert rows(raw) == before
        assert raw.execute('PRAGMA foreign_keys').fetchone() == (1,)
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []
        assert raw.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
            "('ix_legacy_customer_revenue', 'keep_revenue_positive')",
        ).fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError, match='negative revenue'):
            raw.execute('UPDATE customer_revenue_data SET revenue=-1')
        raw.rollback()
        for table in before:
            assert all(not fk[2].startswith('_old_') and fk[2] != 'call_logs_backup'
                       for fk in raw.execute(f'PRAGMA foreign_key_list({table})'))
    backups = list(revenue_db.path.parent.glob('*.revenue-parents-*.bak'))
    assert len(backups) == 4
    with closing(sqlite3.connect(backups[0])) as saved:
        assert saved.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert rows(saved) == before


@pytest.mark.parametrize('column', ['customer_id', 'last_import_id'])
def test_true_orphans_abort_and_preserve_legacy_table(revenue_db, column):
    """Never delete records, null links or fabricate a canonical parent to make startup pass."""
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute('PRAGMA foreign_keys=OFF')
        raw.execute(f'UPDATE customer_revenue_data SET {column}=999')
        raw.commit()
        raw.execute('PRAGMA foreign_keys=ON')
        schema = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='customer_revenue_data'",
        ).fetchone()
        before = raw.execute('SELECT * FROM customer_revenue_data').fetchall()
    with pytest.raises(RuntimeError, match='revenue-parents migration failed; rolled back'):
        _migrate_legacy_revenue_parents(revenue_db)
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='customer_revenue_data'",
        ).fetchone() == schema
        assert raw.execute('SELECT * FROM customer_revenue_data').fetchall() == before
        assert raw.execute('PRAGMA foreign_keys').fetchone() == (1,)


def test_existing_legacy_parent_is_not_redirected(revenue_db):
    """Only dangling parent names are eligible, not real tables with potentially different IDs."""
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.executescript("""
            CREATE TABLE _old_customers (id INTEGER PRIMARY KEY);
            INSERT INTO _old_customers VALUES (31);
        """)
    _migrate_legacy_revenue_parents(revenue_db)
    with revenue_db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        parents = {fk[3]: fk[2] for fk in raw.execute(
            'PRAGMA foreign_key_list(customer_revenue_data)',
        )}
        assert parents == {'customer_id': '_old_customers', 'last_import_id': 'revenue_imports'}
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []


def test_healthy_revenue_database_is_noop(revenue_db):
    """Once fixed, neither startup nor repeated rehearsal should create more backups."""
    _migrate_legacy_revenue_parents(revenue_db)
    before = set(revenue_db.path.parent.glob('*.bak'))
    _migrate_legacy_revenue_parents(revenue_db)
    assert set(revenue_db.path.parent.glob('*.bak')) == before


def test_real_startup_pipeline_repairs_legacy_revenue_before_cleanup(tmp_path, monkeypatch):
    """The formerly failing initializer must succeed twice with actual models and review history."""
    from flask import Flask
    from sqlalchemy.exc import OperationalError
    from app import migrations
    from app.models import (
        Customer, CustomerRevenueData, Note, ProductRevenueData, RevenueAnalysis,
        RevenueImport, RevenueReviewNote, db,
    )
    from app.upgrade_rehearsal import initialize_offline

    path = tmp_path / 'startup.db'
    application = Flask(__name__)
    application.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI=f'sqlite:///{path}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    with application.app_context():
        db.create_all()
        customer = Customer(name='Isolated migration fixture', tpid=-999)
        imported = RevenueImport(filename='fixture', record_count=2)
        db.session.add_all([customer, imported])
        db.session.flush()
        note = Note(customer_id=customer.id, content='Preserve this note',
                    call_date=datetime(2026, 10, 22, 9, 0))
        analysis = RevenueAnalysis(
            customer_name=customer.name, customer_id=customer.id, bucket='Analytics',
            months_analyzed=3, avg_revenue=123.45, latest_revenue=123.45, category='STABLE',
            recommended_action='MONITOR', confidence='HIGH', priority_score=10,
        )
        db.session.add_all([
            note, analysis,
            CustomerRevenueData(
                customer_name=customer.name, customer_id=customer.id, bucket='Analytics',
                fiscal_month='FY27-Oct', month_date=date(2026, 10, 1), revenue=123.45,
                last_import_id=imported.id,
            ),
            ProductRevenueData(
                customer_name=customer.name, customer_id=customer.id, bucket='Analytics',
                product='Fixture', fiscal_month='FY27-Oct', month_date=date(2026, 10, 1),
                revenue=67.89, last_import_id=imported.id,
            ),
        ])
        db.session.flush()
        db.session.add(RevenueReviewNote(
            analysis_id=analysis.id, review_status='reviewed', review_notes='Keep review history',
        ))
        db.session.commit()
        note_id, analysis_id = note.id, analysis.id
        db.session.remove()
        db.engine.dispose()
    with closing(sqlite3.connect(path)) as raw, raw:
        for table in ('customer_revenue_data', 'product_revenue_data', 'revenue_analyses'):
            schema = raw.execute(
                'SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table),
            ).fetchone()[0]
            objects = raw.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL", (table,),
            ).fetchall()
            damaged = schema.replace(f'CREATE TABLE {table}', 'CREATE TABLE fixture_new', 1)
            damaged = damaged.replace('REFERENCES customers (id)',
                                      'REFERENCES "_old_customers" (id)')
            damaged = damaged.replace('REFERENCES revenue_imports (id)',
                                      'REFERENCES "_old_revenue_imports" (id)')
            assert damaged != schema
            raw.execute(damaged)
            raw.execute(f'INSERT INTO fixture_new SELECT * FROM {table}')
            raw.execute(f'DROP TABLE {table}')
            raw.execute(f'ALTER TABLE fixture_new RENAME TO {table}')
            for (sql,) in objects:
                raw.execute(sql)
        raw.execute(
            'CREATE TABLE revenue_engagements (id INTEGER PRIMARY KEY, '
            'analysis_id INTEGER REFERENCES revenue_analyses(id), '
            'note_id INTEGER REFERENCES call_logs_backup(id))',
        )
        raw.execute('INSERT INTO revenue_engagements VALUES (1, ?, ?)', (analysis_id, note_id))
        raw.executescript("""
            CREATE TABLE unrelated_legacy_links (note_id INTEGER REFERENCES notes(id));
            INSERT INTO unrelated_legacy_links VALUES (999);
        """)
        before = rows(raw)
        core_before = {
            table: raw.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            for table in ('customers', 'notes', 'revenue_imports')
        }
    repair = migrations._migrate_legacy_revenue_parents
    monkeypatch.setattr(migrations, '_migrate_legacy_revenue_parents', lambda database: None)
    with pytest.raises(OperationalError, match='no such table: main._old_revenue_imports'):
        initialize_offline(path)
    monkeypatch.setattr(migrations, '_migrate_legacy_revenue_parents', repair)
    result = initialize_offline(path)
    assert result['success'] and result['repeated']
    assert result['remaining_fk_violations'] == 1
    assert initialize_offline(path)['success']
    with closing(sqlite3.connect(path)) as raw:
        assert rows(raw) == before
        for table, expected in core_before.items():
            assert raw.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() == expected
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == [
            ('unrelated_legacy_links', 1, 'notes', 0),
        ]


@pytest.fixture
def current_startup_db(tmp_path):
    """Create one valid current note and customer without running services or using live data."""
    from flask import Flask
    from app.models import Customer, Note, db

    path = tmp_path / 'current.db'
    application = Flask(__name__)
    application.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI=f'sqlite:///{path}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    with application.app_context():
        db.create_all()
        customer = Customer(name='Isolated guard fixture', tpid=-998)
        db.session.add(customer)
        db.session.flush()
        db.session.add(Note(customer_id=customer.id, content='Keep this note',
                            call_date=datetime(2026, 10, 22, 9, 0)))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()
    return path


def test_rehearsal_still_rejects_preexisting_core_row_orphans(current_startup_db):
    """Allowing historical audit/join orphans must not excuse a broken live note/customer link."""
    from app.upgrade_rehearsal import initialize_offline

    with closing(sqlite3.connect(current_startup_db)) as connection, connection:
        connection.execute('UPDATE notes SET customer_id=999')
    with pytest.raises(RuntimeError, match='Upgrade rehearsal foreign key violations'):
        initialize_offline(current_startup_db)


def test_rehearsal_still_rejects_new_noncore_orphans(current_startup_db, monkeypatch):
    """A migration cannot introduce the same non-core orphan that a historical DB may retain."""
    from app import upgrade_rehearsal

    original = upgrade_rehearsal.initialize_database

    def introduce_orphan(database) -> None:
        """Model an actual migration introducing an invalid audit link with FK checks restored."""
        original(database)
        raw_connection = database.engine.raw_connection()
        try:
            raw = raw_connection.driver_connection
            raw.execute('PRAGMA foreign_keys=OFF')
            raw.execute('CREATE TABLE broken_new_audit (note_id INTEGER REFERENCES notes(id))')
            raw.execute('INSERT INTO broken_new_audit VALUES (999)')
            raw.commit()
            raw.execute('PRAGMA foreign_keys=ON')
        finally:
            raw_connection.close()

    monkeypatch.setattr(upgrade_rehearsal, 'initialize_database', introduce_orphan)
    with pytest.raises(RuntimeError, match='Upgrade rehearsal foreign key violations'):
        upgrade_rehearsal.initialize_offline(current_startup_db)
