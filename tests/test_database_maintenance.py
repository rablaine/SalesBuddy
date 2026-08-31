"""Tests for manual SQLite database maintenance."""

import sqlite3
from unittest.mock import patch

from app.services.database_maintenance import (
    VacuumInProgressError,
    database_free_space,
    should_vacuum_database,
    vacuum_database,
)


def test_vacuum_database_reclaims_deleted_pages(tmp_path):
    """Vacuum compacts deleted pages without changing surviving data."""
    db_path = tmp_path / 'vacuum-test.db'
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, payload BLOB)')
        connection.executemany(
            'INSERT INTO records (payload) VALUES (?)',
            [(bytes([index % 251]) * 8192,) for index in range(600)],
        )
        connection.commit()
        connection.execute('DELETE FROM records WHERE id <= 500')
        connection.commit()
    finally:
        connection.close()

    result = vacuum_database(db_path)

    connection = sqlite3.connect(str(db_path))
    try:
        remaining = connection.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        connection.close()

    assert remaining == 100
    assert integrity == 'ok'
    assert result['free_pages_before'] > 0
    assert result['reclaimed_bytes'] > 0
    assert result['after_bytes'] < result['before_bytes']


def test_database_free_space_controls_conditional_compaction(tmp_path):
    """Free-page thresholds avoid unnecessary full database rewrites."""
    db_path = tmp_path / 'threshold-test.db'
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute('CREATE TABLE records (payload BLOB)')
        connection.executemany(
            'INSERT INTO records VALUES (?)',
            [(b'x' * 8192,) for _ in range(100)],
        )
        connection.commit()
        connection.execute('DELETE FROM records')
        connection.commit()
    finally:
        connection.close()

    metrics = database_free_space(db_path)

    assert metrics['reclaimable_bytes'] > 0
    assert should_vacuum_database(metrics) is False
    assert should_vacuum_database(
        metrics,
        min_reclaim_bytes=1,
        min_free_percent=1,
    ) is True


def test_admin_vacuum_endpoint_returns_compaction_metrics(client):
    """Admin endpoint releases pooled connections and returns service metrics."""
    metrics = {
        'before_bytes': 10_000,
        'after_bytes': 4_000,
        'reclaimed_bytes': 6_000,
        'free_pages_before': 12,
        'free_percent_before': 60.0,
        'page_size': 4096,
    }
    with patch(
        'app.services.database_maintenance.vacuum_database',
        return_value=metrics,
    ) as vacuum:
        response = client.post('/api/admin/vacuum-database')

    assert response.status_code == 200
    assert response.get_json() == {'success': True, **metrics}
    vacuum.assert_called_once()


def test_admin_panel_has_database_compaction_control(client):
    """Danger Zone exposes manual compaction with status feedback."""
    response = client.get('/admin')

    assert response.status_code == 200
    assert b'id="vacuumDatabaseBtn"' in response.data
    assert b'/api/admin/vacuum-database' in response.data
    assert b'id="vacuumDatabaseResult"' in response.data
    assert b'compacting database...' in response.data
    assert b'Database compaction was not needed' in response.data


def test_admin_vacuum_endpoint_rejects_duplicate_run(client):
    """A second compaction request receives a conflict response."""
    with patch(
        'app.services.database_maintenance.vacuum_database',
        side_effect=VacuumInProgressError('Database compaction is already running'),
    ):
        response = client.post('/api/admin/vacuum-database')

    assert response.status_code == 409
    assert response.get_json() == {
        'success': False,
        'error': 'Database compaction is already running',
    }