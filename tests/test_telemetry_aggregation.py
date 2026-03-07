"""
Tests for telemetry aggregation and central shipping services.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models import db, UsageEvent, DailyFeatureStats


# =============================================================================
# Helper to seed raw UsageEvent rows
# =============================================================================

def _seed_events(app, events: list[dict]) -> None:
    """Insert raw UsageEvent rows for testing."""
    with app.app_context():
        for e in events:
            db.session.add(UsageEvent(**e))
        db.session.commit()


def _yesterday() -> datetime:
    """Return midnight-UTC of yesterday."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1)


def _days_ago(n: int) -> datetime:
    """Return midnight-UTC of *n* days ago."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=n)


# =============================================================================
# DailyFeatureStats model tests
# =============================================================================

class TestDailyFeatureStatsModel:
    """Tests for the DailyFeatureStats ORM model."""

    def test_create_daily_feature_stats(self, app):
        """Should be able to create and query a stats row."""
        with app.app_context():
            stat = DailyFeatureStats(
                date=_yesterday().date(),
                category='Call Logs',
                endpoint='/call-logs',
                method='GET',
                is_api=False,
                event_count=42,
                error_count=1,
                avg_response_ms=55.3,
                unique_referrers=3,
            )
            db.session.add(stat)
            db.session.commit()

            fetched = DailyFeatureStats.query.filter_by(category='Call Logs').first()
            assert fetched is not None
            assert fetched.event_count == 42
            assert fetched.error_count == 1
            assert fetched.avg_response_ms == 55.3
            assert fetched.unique_referrers == 3
            assert fetched.method == 'GET'
            assert fetched.is_api is False

            # Cleanup
            db.session.delete(fetched)
            db.session.commit()

    def test_unique_constraint(self, app):
        """Should enforce unique constraint on (date, category, endpoint, method)."""
        with app.app_context():
            d = _yesterday().date()
            stat1 = DailyFeatureStats(
                date=d, category='Admin', endpoint='/admin',
                method='GET', event_count=5,
            )
            stat2 = DailyFeatureStats(
                date=d, category='Admin', endpoint='/admin',
                method='GET', event_count=10,
            )
            db.session.add(stat1)
            db.session.commit()

            db.session.add(stat2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

            # Cleanup
            DailyFeatureStats.query.filter_by(date=d, category='Admin').delete()
            db.session.commit()


# =============================================================================
# Aggregation service tests
# =============================================================================

class TestAggregateDaily:
    """Tests for aggregate_daily_stats()."""

    def _cleanup(self, app):
        with app.app_context():
            DailyFeatureStats.query.delete()
            UsageEvent.query.delete()
            db.session.commit()

    def test_aggregate_yesterday_events(self, app):
        """Should roll up yesterday's raw events into DailyFeatureStats."""
        self._cleanup(app)
        ts = _yesterday() + timedelta(hours=10)
        _seed_events(app, [
            {'endpoint': '/call-logs', 'method': 'GET', 'category': 'Call Logs',
             'status_code': 200, 'response_time_ms': 50.0, 'timestamp': ts,
             'is_api': False, 'blueprint': 'call_logs'},
            {'endpoint': '/call-logs', 'method': 'GET', 'category': 'Call Logs',
             'status_code': 200, 'response_time_ms': 70.0, 'timestamp': ts + timedelta(hours=1),
             'is_api': False, 'blueprint': 'call_logs'},
            {'endpoint': '/call-logs', 'method': 'GET', 'category': 'Call Logs',
             'status_code': 500, 'response_time_ms': 200.0, 'timestamp': ts + timedelta(hours=2),
             'is_api': False, 'blueprint': 'call_logs'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import aggregate_daily_stats
            result = aggregate_daily_stats(days_back=1)
            assert result['days_processed'] == 1
            assert result['rows_upserted'] >= 1

            stat = DailyFeatureStats.query.filter_by(
                category='Call Logs', endpoint='/call-logs',
            ).first()
            assert stat is not None
            assert stat.event_count == 3
            assert stat.error_count == 1  # one 500
            assert stat.avg_response_ms is not None

        self._cleanup(app)

    def test_aggregate_skips_today(self, app):
        """Should never aggregate today's events (day not over yet)."""
        self._cleanup(app)
        now = datetime.now(timezone.utc)
        _seed_events(app, [
            {'endpoint': '/admin', 'method': 'GET', 'category': 'Admin',
             'status_code': 200, 'response_time_ms': 10.0, 'timestamp': now,
             'is_api': False, 'blueprint': 'admin'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import aggregate_daily_stats
            result = aggregate_daily_stats(days_back=1)
            # today shouldn't be aggregated
            stat = DailyFeatureStats.query.filter_by(category='Admin').first()
            assert stat is None

        self._cleanup(app)

    def test_idempotent_aggregation(self, app):
        """Running aggregation twice should not create duplicate rows."""
        self._cleanup(app)
        ts = _yesterday() + timedelta(hours=5)
        _seed_events(app, [
            {'endpoint': '/topics', 'method': 'GET', 'category': 'Topics',
             'status_code': 200, 'response_time_ms': 30.0, 'timestamp': ts,
             'is_api': False, 'blueprint': 'topics'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import aggregate_daily_stats
            aggregate_daily_stats(days_back=1)
            result = aggregate_daily_stats(days_back=1)

            count = DailyFeatureStats.query.filter_by(
                category='Topics', endpoint='/topics',
            ).count()
            assert count == 1

        self._cleanup(app)

    def test_prune_old_raw_events(self, app):
        """Should optionally prune raw events older than retention period."""
        self._cleanup(app)
        old_ts = _days_ago(100) + timedelta(hours=3)
        recent_ts = _yesterday() + timedelta(hours=3)
        _seed_events(app, [
            {'endpoint': '/old', 'method': 'GET', 'category': 'Admin',
             'status_code': 200, 'response_time_ms': 10.0, 'timestamp': old_ts,
             'is_api': False, 'blueprint': 'admin'},
            {'endpoint': '/recent', 'method': 'GET', 'category': 'Admin',
             'status_code': 200, 'response_time_ms': 10.0, 'timestamp': recent_ts,
             'is_api': False, 'blueprint': 'admin'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import aggregate_daily_stats
            result = aggregate_daily_stats(
                days_back=101, prune_raw=True, raw_retention_days=90,
            )
            assert result.get('raw_events_pruned', 0) >= 1

            # Old event should be gone, recent should remain
            remaining = UsageEvent.query.all()
            endpoints = [e.endpoint for e in remaining]
            assert '/old' not in endpoints
            assert '/recent' in endpoints

        self._cleanup(app)

    def test_multiple_days(self, app):
        """Should aggregate events from multiple past days."""
        self._cleanup(app)
        _seed_events(app, [
            {'endpoint': '/sellers', 'method': 'GET', 'category': 'Sellers',
             'status_code': 200, 'response_time_ms': 20.0,
             'timestamp': _days_ago(2) + timedelta(hours=5),
             'is_api': False, 'blueprint': 'sellers'},
            {'endpoint': '/sellers', 'method': 'GET', 'category': 'Sellers',
             'status_code': 200, 'response_time_ms': 25.0,
             'timestamp': _days_ago(1) + timedelta(hours=5),
             'is_api': False, 'blueprint': 'sellers'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import aggregate_daily_stats
            result = aggregate_daily_stats(days_back=3)
            assert result['days_processed'] >= 2

        self._cleanup(app)


# =============================================================================
# Feature health report tests
# =============================================================================

class TestFeatureHealth:
    """Tests for get_feature_health()."""

    def _cleanup(self, app):
        with app.app_context():
            DailyFeatureStats.query.delete()
            UsageEvent.query.delete()
            db.session.commit()

    def test_empty_report(self, app):
        """Should return empty ranking when no data exists."""
        self._cleanup(app)
        with app.app_context():
            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            assert report['feature_ranking'] == []
            assert 'dead_features' in report
            assert 'period' in report
            assert report['period']['days'] == 30

        self._cleanup(app)

    def test_ranking_with_aggregated_data(self, app):
        """Should rank features by total events."""
        self._cleanup(app)
        d = _yesterday().date()
        with app.app_context():
            db.session.add(DailyFeatureStats(
                date=d, category='Call Logs', endpoint='/call-logs',
                method='GET', event_count=100, error_count=2,
                avg_response_ms=50.0, is_api=False,
            ))
            db.session.add(DailyFeatureStats(
                date=d, category='Admin', endpoint='/admin',
                method='GET', event_count=20, error_count=0,
                avg_response_ms=30.0, is_api=False,
            ))
            db.session.commit()

            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            ranking = report['feature_ranking']
            assert len(ranking) >= 2
            # Call Logs should be first (more events)
            assert ranking[0]['category'] == 'Call Logs'
            assert ranking[0]['events'] == 100
            assert ranking[1]['category'] == 'Admin'

        self._cleanup(app)

    def test_dead_features_detection(self, app):
        """Should identify known categories with zero usage as dead."""
        self._cleanup(app)
        with app.app_context():
            # Only one category has data
            d = _yesterday().date()
            db.session.add(DailyFeatureStats(
                date=d, category='Call Logs', endpoint='/call-logs',
                method='GET', event_count=10, is_api=False,
            ))
            db.session.commit()

            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            # All known categories except Call Logs should be "dead"
            assert 'Admin' in report['dead_features']
            assert 'Call Logs' not in report['dead_features']

        self._cleanup(app)

    def test_today_live_data_included(self, app):
        """Should include today's raw events in the ranking."""
        self._cleanup(app)
        now = datetime.now(timezone.utc)
        _seed_events(app, [
            {'endpoint': '/revenue', 'method': 'GET', 'category': 'Revenue',
             'status_code': 200, 'response_time_ms': 15.0, 'timestamp': now,
             'is_api': False, 'blueprint': 'revenue'},
        ])

        with app.app_context():
            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            cats = [r['category'] for r in report['feature_ranking']]
            assert 'Revenue' in cats

        self._cleanup(app)

    def test_share_percentage(self, app):
        """Feature ranking should include share_pct that sums to ~100."""
        self._cleanup(app)
        d = _yesterday().date()
        with app.app_context():
            db.session.add(DailyFeatureStats(
                date=d, category='A', endpoint='/a', method='GET',
                event_count=50, is_api=False,
            ))
            db.session.add(DailyFeatureStats(
                date=d, category='B', endpoint='/b', method='GET',
                event_count=50, is_api=False,
            ))
            db.session.commit()

            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            total_share = sum(r['share_pct'] for r in report['feature_ranking'])
            assert 99.0 <= total_share <= 101.0

        self._cleanup(app)

    def test_trend_analysis(self, app):
        """Should compute trend direction for features across the period."""
        self._cleanup(app)
        with app.app_context():
            # First half: 10 events, second half: 50 events -> "up"
            first_half_date = _days_ago(25).date()
            second_half_date = _days_ago(5).date()
            db.session.add(DailyFeatureStats(
                date=first_half_date, category='AI', endpoint='/ai',
                method='GET', event_count=10, is_api=False,
            ))
            db.session.add(DailyFeatureStats(
                date=second_half_date, category='AI', endpoint='/ai',
                method='GET', event_count=50, is_api=False,
            ))
            db.session.commit()

            from app.services.telemetry_aggregation import get_feature_health
            report = get_feature_health(days=30)
            ai_trend = next(
                (t for t in report['trends'] if t['category'] == 'AI'), None,
            )
            assert ai_trend is not None
            assert ai_trend['direction'] == 'up'

        self._cleanup(app)


# =============================================================================
# Telemetry shipper unit tests
# =============================================================================

class TestInstanceId:
    """Tests for instance ID generation and persistence."""

    def test_get_instance_id_creates_file(self, app):
        """Should create ID file if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                'app.services.telemetry_shipper._get_data_dir',
                return_value=Path(tmpdir),
            ):
                from app.services.telemetry_shipper import get_instance_id
                instance_id = get_instance_id()
                assert len(instance_id) == 36  # UUID format
                # File should have been created
                id_file = Path(tmpdir) / '.notehelper_instance_id'
                assert id_file.exists()
                assert id_file.read_text().strip() == instance_id

    def test_get_instance_id_stable(self, app):
        """Should return the same ID on subsequent calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                'app.services.telemetry_shipper._get_data_dir',
                return_value=Path(tmpdir),
            ):
                from app.services.telemetry_shipper import get_instance_id
                id1 = get_instance_id()
                id2 = get_instance_id()
                assert id1 == id2


class TestTelemetryOptOut:
    """Tests for opt-out logic."""

    def test_enabled_by_default(self):
        """Telemetry should be enabled when env var is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NOTEHELPER_TELEMETRY_OPT_OUT', None)
            from app.services.telemetry_shipper import is_telemetry_enabled
            assert is_telemetry_enabled() is True

    def test_opt_out_true(self):
        """Should be disabled when NOTEHELPER_TELEMETRY_OPT_OUT=true."""
        with patch.dict(os.environ, {'NOTEHELPER_TELEMETRY_OPT_OUT': 'true'}):
            from app.services.telemetry_shipper import is_telemetry_enabled
            assert is_telemetry_enabled() is False

    def test_opt_out_yes(self):
        """Should be disabled when NOTEHELPER_TELEMETRY_OPT_OUT=yes."""
        with patch.dict(os.environ, {'NOTEHELPER_TELEMETRY_OPT_OUT': 'yes'}):
            from app.services.telemetry_shipper import is_telemetry_enabled
            assert is_telemetry_enabled() is False

    def test_opt_out_one(self):
        """Should be disabled when NOTEHELPER_TELEMETRY_OPT_OUT=1."""
        with patch.dict(os.environ, {'NOTEHELPER_TELEMETRY_OPT_OUT': '1'}):
            from app.services.telemetry_shipper import is_telemetry_enabled
            assert is_telemetry_enabled() is False

    def test_opt_out_false(self):
        """Should remain enabled when env var is set to 'false'."""
        with patch.dict(os.environ, {'NOTEHELPER_TELEMETRY_OPT_OUT': 'false'}):
            from app.services.telemetry_shipper import is_telemetry_enabled
            assert is_telemetry_enabled() is True


class TestBuildCustomEvent:
    """Tests for App Insights envelope builder."""

    def test_envelope_structure(self):
        """Should produce a valid App Insights event envelope."""
        from app.services.telemetry_shipper import _build_custom_event

        env = _build_custom_event(
            name='TestEvent',
            properties={'key': 'value'},
            measurements={'count': 42.0},
        )
        assert env['name'] == 'Microsoft.ApplicationInsights.Event'
        assert 'iKey' in env
        assert env['data']['baseType'] == 'EventData'
        assert env['data']['baseData']['name'] == 'TestEvent'
        assert env['data']['baseData']['properties'] == {'key': 'value'}
        assert env['data']['baseData']['measurements'] == {'count': 42.0}
        assert env['data']['baseData']['ver'] == 2

    def test_envelope_has_timestamp(self):
        """Should include an ISO timestamp."""
        from app.services.telemetry_shipper import _build_custom_event

        env = _build_custom_event('X', {}, {})
        assert 'time' in env
        # Should be parseable
        datetime.fromisoformat(env['time'])


class TestQueueEvent:
    """Tests for queue_event() buffer mechanics."""

    def _reset_buffer(self):
        """Clear the in-memory buffer and stats between tests."""
        import app.services.telemetry_shipper as ts
        with ts._buffer_lock:
            ts._buffer.clear()
        with ts._stats_lock:
            ts._stats['events_queued'] = 0
            ts._stats['events_flushed'] = 0
            ts._stats['flush_count'] = 0
            ts._stats['flush_errors'] = 0
            ts._stats['last_flush_time'] = None
            ts._stats['last_flush_events'] = 0
            ts._stats['last_error'] = None

    def test_queue_event_adds_to_buffer(self):
        """Should add an envelope to the in-memory buffer."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event

        queue_event(
            category='Call Logs', method='GET',
            status_code=200, response_time_ms=50.0, is_api=False,
        )
        with ts._buffer_lock:
            assert len(ts._buffer) == 1
            env = ts._buffer[0]
            assert env['data']['baseData']['name'] == 'NoteHelper.FeatureUsage'
            assert env['data']['baseData']['properties']['category'] == 'Call Logs'
            assert env['data']['baseData']['measurements']['status_code'] == 200.0
        self._reset_buffer()

    def test_queue_event_increments_stats(self):
        """Should increment events_queued counter."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event

        queue_event(
            category='Admin', method='POST',
            status_code=200, response_time_ms=10.0, is_api=False,
        )
        queue_event(
            category='AI', method='GET',
            status_code=200, response_time_ms=5.0, is_api=True,
        )
        with ts._stats_lock:
            assert ts._stats['events_queued'] == 2
        self._reset_buffer()

    def test_queue_event_skipped_when_opted_out(self):
        """Should not buffer events when telemetry is disabled."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event

        with patch('app.services.telemetry_shipper.is_telemetry_enabled', return_value=False):
            queue_event(
                category='Call Logs', method='GET',
                status_code=200, response_time_ms=10.0, is_api=False,
            )
        with ts._buffer_lock:
            assert len(ts._buffer) == 0
        self._reset_buffer()

    def test_queue_event_records_error_flag(self):
        """Should set is_error measurement to 1.0 for 4xx/5xx codes."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event

        queue_event(
            category='Admin', method='GET',
            status_code=500, response_time_ms=100.0, is_api=False,
        )
        with ts._buffer_lock:
            env = ts._buffer[0]
            assert env['data']['baseData']['measurements']['is_error'] == 1.0
        self._reset_buffer()


class TestFlushBuffer:
    """Tests for flush_buffer() with mocked HTTP."""

    def _reset_buffer(self):
        import app.services.telemetry_shipper as ts
        with ts._buffer_lock:
            ts._buffer.clear()
        with ts._stats_lock:
            ts._stats['events_queued'] = 0
            ts._stats['events_flushed'] = 0
            ts._stats['flush_count'] = 0
            ts._stats['flush_errors'] = 0
            ts._stats['last_flush_time'] = None
            ts._stats['last_flush_events'] = 0
            ts._stats['last_error'] = None

    def test_flush_sends_to_app_insights(self):
        """Should POST buffered events to the App Insights Track API."""
        self._reset_buffer()
        from app.services.telemetry_shipper import queue_event, flush_buffer

        queue_event(
            category='Call Logs', method='GET',
            status_code=200, response_time_ms=50.0, is_api=False,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch('app.services.telemetry_shipper.http_requests.post', return_value=mock_resp) as mock_post:
            result = flush_buffer()
            assert result['flushed'] is True
            assert result['events_sent'] == 1

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert '/v2/track' in call_args[0][0]
            assert call_args[1]['headers']['Content-Type'] == 'application/x-json-stream'
        self._reset_buffer()

    def test_flush_empty_buffer(self):
        """Should return 'buffer empty' when there's nothing to flush."""
        self._reset_buffer()
        from app.services.telemetry_shipper import flush_buffer
        result = flush_buffer()
        assert result['flushed'] is False
        assert result['events_sent'] == 0
        self._reset_buffer()

    def test_flush_clears_buffer(self):
        """Should clear the buffer after a successful flush."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event, flush_buffer

        queue_event(
            category='Admin', method='GET',
            status_code=200, response_time_ms=10.0, is_api=False,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch('app.services.telemetry_shipper.http_requests.post', return_value=mock_resp):
            flush_buffer()

        with ts._buffer_lock:
            assert len(ts._buffer) == 0
        self._reset_buffer()

    def test_flush_restores_buffer_on_error(self):
        """Should put events back in the buffer if HTTP fails."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event, flush_buffer

        queue_event(
            category='Topics', method='GET',
            status_code=200, response_time_ms=10.0, is_api=False,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception('Server Error')

        with patch('app.services.telemetry_shipper.http_requests.post', return_value=mock_resp):
            result = flush_buffer()
            assert result['flushed'] is False
            assert 'error' in result

        # Events should be restored
        with ts._buffer_lock:
            assert len(ts._buffer) == 1
        self._reset_buffer()

    def test_flush_updates_stats(self):
        """Should update flush stats counters after successful flush."""
        self._reset_buffer()
        import app.services.telemetry_shipper as ts
        from app.services.telemetry_shipper import queue_event, flush_buffer

        queue_event(
            category='Revenue', method='GET',
            status_code=200, response_time_ms=20.0, is_api=False,
        )
        queue_event(
            category='Sellers', method='GET',
            status_code=200, response_time_ms=30.0, is_api=False,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch('app.services.telemetry_shipper.http_requests.post', return_value=mock_resp):
            flush_buffer()

        with ts._stats_lock:
            assert ts._stats['events_flushed'] == 2
            assert ts._stats['flush_count'] == 1
            assert ts._stats['last_flush_events'] == 2
            assert ts._stats['last_flush_time'] is not None
        self._reset_buffer()

    def test_payload_is_newline_delimited_json(self):
        """Should send newline-delimited JSON (not a JSON array)."""
        self._reset_buffer()
        from app.services.telemetry_shipper import queue_event, flush_buffer

        queue_event(
            category='Sellers', method='GET',
            status_code=200, response_time_ms=20.0, is_api=False,
        )
        queue_event(
            category='Topics', method='POST',
            status_code=201, response_time_ms=30.0, is_api=False,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch('app.services.telemetry_shipper.http_requests.post', return_value=mock_resp) as mock_post:
            flush_buffer()

            payload = mock_post.call_args[1]['data']
            lines = payload.strip().split('\n')
            assert len(lines) == 2
            for line in lines:
                parsed = json.loads(line)
                assert parsed['name'] == 'Microsoft.ApplicationInsights.Event'
        self._reset_buffer()


class TestGetFlushStats:
    """Tests for the get_flush_stats() function."""

    def test_returns_expected_keys(self):
        """Should return dict with all expected stat keys."""
        from app.services.telemetry_shipper import get_flush_stats
        stats = get_flush_stats()
        assert 'enabled' in stats
        assert 'instance_id' in stats
        assert 'buffer_size' in stats
        assert 'events_queued' in stats
        assert 'events_flushed' in stats
        assert 'flush_count' in stats


# =============================================================================
# Admin API endpoint tests
# =============================================================================

class TestFeatureHealthAPI:
    """Tests for the feature health admin API."""

    def test_feature_health_endpoint(self, client, app):
        """GET /api/admin/telemetry/feature-health should return report."""
        resp = client.get('/api/admin/telemetry/feature-health?days=7')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'feature_ranking' in data
        assert 'dead_features' in data
        assert 'period' in data

    def test_feature_health_default_days(self, client, app):
        """Should default to 30 days when no param given."""
        resp = client.get('/api/admin/telemetry/feature-health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['period']['days'] == 30


class TestAggregateAPI:
    """Tests for the manual aggregation API endpoint."""

    def test_aggregate_endpoint(self, client, app):
        """POST /api/admin/telemetry/aggregate should return success."""
        resp = client.post(
            '/api/admin/telemetry/aggregate',
            data=json.dumps({'days_back': 1}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'days_processed' in data


class TestFlushAPI:
    """Tests for the manual flush admin API endpoint."""

    def test_flush_endpoint(self, client, app):
        """POST /api/admin/telemetry/flush should return success."""
        with patch(
            'app.services.telemetry_shipper.flush_buffer',
            return_value={'flushed': False, 'reason': 'buffer empty', 'events_sent': 0},
        ):
            resp = client.post('/api/admin/telemetry/flush')
            assert resp.status_code == 200

    def test_flush_endpoint_when_disabled(self, client, app):
        """Should indicate disabled when telemetry is opted out."""
        with patch('app.services.telemetry_shipper.is_telemetry_enabled', return_value=False):
            resp = client.post('/api/admin/telemetry/flush')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is False


class TestShippingStatusAPI:
    """Tests for the shipping status admin API endpoint."""

    def test_shipping_status_endpoint(self, client, app):
        """GET /api/admin/telemetry/shipping-status should return stats."""
        resp = client.get('/api/admin/telemetry/shipping-status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'enabled' in data
        assert 'instance_id' in data
        assert 'buffer_size' in data
        assert 'events_queued' in data

    def test_shipping_status_shows_instance_id(self, client, app):
        """Should include a UUID-format instance ID."""
        resp = client.get('/api/admin/telemetry/shipping-status')
        data = resp.get_json()
        instance_id = data['instance_id']
        assert len(instance_id) == 36  # UUID format: 8-4-4-4-12
