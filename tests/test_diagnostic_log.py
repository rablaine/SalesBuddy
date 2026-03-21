"""Tests for the diagnostic logging service."""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from app.services.diagnostic_log import (
    diag_log, prune_old_entries, get_log_stats, get_log_path,
    set_correlation_id, get_correlation_id,
    LOG_FILE, RETENTION_HOURS,
)


@pytest.fixture(autouse=True)
def _isolate_log_file(tmp_path, monkeypatch):
    """Redirect diagnostic log to a temp directory for each test."""
    log_file = str(tmp_path / 'diagnostic.jsonl')
    monkeypatch.setattr('app.services.diagnostic_log.LOG_FILE', log_file)
    monkeypatch.setattr('app.services.diagnostic_log.LOG_DIR', str(tmp_path))
    yield log_file


class TestDiagLog:
    """Tests for the core diag_log function."""

    def test_creates_log_entry(self, _isolate_log_file):
        """diag_log writes a JSON line to the file."""
        diag_log('test_cat', key='value', number=42)

        with open(_isolate_log_file, 'r') as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry['cat'] == 'test_cat'
        assert entry['key'] == 'value'
        assert entry['number'] == 42
        assert 'ts' in entry
        assert 'cid' in entry

    def test_multiple_entries(self, _isolate_log_file):
        """Multiple calls append multiple lines."""
        diag_log('a')
        diag_log('b')
        diag_log('c')

        with open(_isolate_log_file, 'r') as f:
            lines = [l.strip() for l in f if l.strip()]
        assert len(lines) == 3

    def test_truncates_large_values(self, _isolate_log_file):
        """Strings longer than MAX_PAYLOAD_CHARS are truncated."""
        big = 'x' * 10000
        diag_log('big', payload=big)

        with open(_isolate_log_file, 'r') as f:
            entry = json.loads(f.readline())
        assert len(entry['payload']) < 6000
        assert entry['payload'].endswith('...[truncated]')

    def test_never_raises(self, _isolate_log_file, monkeypatch):
        """diag_log should never raise, even on write failure."""
        monkeypatch.setattr(
            'app.services.diagnostic_log.LOG_FILE', '/nonexistent/path/log.jsonl'
        )
        diag_log('should_not_crash', foo='bar')  # Should not raise


class TestCorrelationId:
    """Tests for request correlation ID management."""

    def test_set_and_get(self):
        """set_correlation_id stores and get retrieves it."""
        cid = set_correlation_id('test-123')
        assert cid == 'test-123'
        assert get_correlation_id() == 'test-123'

    def test_auto_generate(self):
        """set_correlation_id generates an ID when none provided."""
        cid = set_correlation_id()
        assert len(cid) == 12
        assert get_correlation_id() == cid

    def test_default_when_unset(self):
        """get_correlation_id returns 'none' when never set (new thread)."""
        import threading
        result = [None]

        def worker():
            result[0] = get_correlation_id()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert result[0] == 'none'


class TestPruneOldEntries:
    """Tests for log retention pruning."""

    def test_prunes_old_entries(self, _isolate_log_file):
        """Entries older than RETENTION_HOURS are removed."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        with open(_isolate_log_file, 'w') as f:
            f.write(json.dumps({'ts': old_ts, 'cat': 'old'}) + '\n')
            f.write(json.dumps({'ts': new_ts, 'cat': 'new'}) + '\n')

        removed = prune_old_entries()
        assert removed == 1

        with open(_isolate_log_file, 'r') as f:
            lines = [l.strip() for l in f if l.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])['cat'] == 'new'

    def test_keeps_recent_entries(self, _isolate_log_file):
        """Recent entries within the window are kept."""
        recent = datetime.now(timezone.utc).isoformat()

        with open(_isolate_log_file, 'w') as f:
            f.write(json.dumps({'ts': recent, 'cat': 'keep'}) + '\n')

        removed = prune_old_entries()
        assert removed == 0

    def test_no_file(self, _isolate_log_file):
        """Returns 0 when log file doesn't exist."""
        removed = prune_old_entries()
        assert removed == 0


class TestGetLogStats:
    """Tests for log statistics."""

    def test_no_file(self, _isolate_log_file):
        """Stats report file doesn't exist."""
        stats = get_log_stats()
        assert stats['exists'] is False
        assert stats['entry_count'] == 0

    def test_with_entries(self, _isolate_log_file):
        """Stats report correct count and size."""
        diag_log('a', x=1)
        diag_log('b', x=2)

        stats = get_log_stats()
        assert stats['exists'] is True
        assert stats['entry_count'] == 2
        assert stats['size_bytes'] > 0
        assert stats['oldest'] is not None
        assert stats['newest'] is not None


class TestGetLogPath:
    """Tests for log path helper."""

    def test_returns_none_when_missing(self, _isolate_log_file):
        """Returns None when log file doesn't exist."""
        assert get_log_path() is None

    def test_returns_path_when_exists(self, _isolate_log_file):
        """Returns the path when log file exists."""
        diag_log('create_it')
        path = get_log_path()
        assert path is not None
        assert os.path.exists(path)


class TestAdminEndpoints:
    """Tests for the admin diagnostic log API endpoints."""

    def test_stats_endpoint(self, app, client):
        """GET /api/admin/diagnostic-log/stats returns stats."""
        resp = client.get('/api/admin/diagnostic-log/stats')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'exists' in data

    def test_download_no_file(self, app, client):
        """Download returns 404 when no log file exists."""
        resp = client.get('/api/admin/diagnostic-log/download')
        assert resp.status_code == 404

    def test_download_with_file(self, app, client, _isolate_log_file):
        """Download returns a zip when log file exists."""
        diag_log('test_entry', msg='hello')

        resp = client.get('/api/admin/diagnostic-log/download')
        assert resp.status_code == 200
        assert resp.content_type == 'application/zip'
        assert b'PK' in resp.data[:4]  # ZIP magic bytes

    def test_clear_endpoint(self, app, client, _isolate_log_file):
        """POST /api/admin/diagnostic-log/clear deletes the log."""
        diag_log('to_be_cleared')
        assert get_log_path() is not None

        resp = client.post('/api/admin/diagnostic-log/clear')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert get_log_path() is None


class TestFlaskIntegration:
    """Tests for the Flask before/after request hooks."""

    def test_correlation_id_set_on_request(self, app, client):
        """Each request gets a correlation ID."""
        diag_log('before_request_test')
        # Just verify the app doesn't crash with the hooks
        resp = client.get('/')
        assert resp.status_code == 200
