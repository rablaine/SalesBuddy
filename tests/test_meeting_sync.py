"""Tests for daily meeting cache feature (Issue #120).

Tests the DailyMeetingCache model, meeting_sync service,
and the /api/meetings and /api/meetings/refresh endpoints.
"""
import json
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from app.models import db, DailyMeetingCache


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestDailyMeetingCacheModel:
    """Tests for the DailyMeetingCache model."""

    def test_create_cache_entry(self, app):
        """Cache row stores meeting_date, meetings_json, synced_at."""
        with app.app_context():
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 17),
                meetings_json=json.dumps([{'title': 'Standup', 'start_time': None}]),
            )
            db.session.add(cache)
            db.session.commit()

            loaded = DailyMeetingCache.query.filter_by(
                meeting_date=date(2026, 4, 17)
            ).first()
            assert loaded is not None
            assert loaded.synced_at is not None

            # Cleanup
            db.session.delete(loaded)
            db.session.commit()

    def test_get_meetings_deserializes(self, app):
        """get_meetings() returns parsed list from meetings_json."""
        with app.app_context():
            meetings = [
                {'title': 'Call A', 'start_time': '2026-04-17T09:00:00'},
                {'title': 'Call B', 'start_time': '2026-04-17T10:00:00'},
            ]
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 18),
                meetings_json=json.dumps(meetings),
            )
            db.session.add(cache)
            db.session.commit()

            result = cache.get_meetings()
            assert len(result) == 2
            assert result[0]['title'] == 'Call A'

            db.session.delete(cache)
            db.session.commit()

    def test_get_meetings_handles_bad_json(self, app):
        """get_meetings() returns empty list on invalid JSON."""
        with app.app_context():
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 19),
                meetings_json='not-json',
            )
            db.session.add(cache)
            db.session.commit()

            assert cache.get_meetings() == []

            db.session.delete(cache)
            db.session.commit()

    def test_unique_constraint_on_date(self, app):
        """Only one cache row per date."""
        with app.app_context():
            c1 = DailyMeetingCache(
                meeting_date=date(2026, 4, 20),
                meetings_json='[]',
            )
            db.session.add(c1)
            db.session.commit()

            c2 = DailyMeetingCache(
                meeting_date=date(2026, 4, 20),
                meetings_json='[]',
            )
            db.session.add(c2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

            db.session.delete(c1)
            db.session.commit()


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------

class TestMeetingSyncService:
    """Tests for app.services.meeting_sync functions."""

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_sync_meetings_for_date_caches_result(self, mock_fetch, app):
        """sync_meetings_for_date stores meetings in DailyMeetingCache."""
        mock_fetch.return_value = (
            [
                {
                    'id': '1',
                    'title': 'Customer Sync',
                    'start_time': datetime(2026, 4, 21, 14, 0),
                    'customer': 'Contoso',
                    'attendees': ['Alice'],
                },
            ],
            'raw workiq response',
        )

        with app.app_context():
            from app.services.meeting_sync import sync_meetings_for_date

            result, err = sync_meetings_for_date('2026-04-21')
            assert err is None
            assert len(result) == 1
            assert result[0]['title'] == 'Customer Sync'
            assert result[0]['start_time_display'] == '02:00 PM'

            # Verify DB row
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date(2026, 4, 21)
            ).first()
            assert cache is not None
            assert cache.raw_response == 'raw workiq response'
            assert len(cache.get_meetings()) == 1

            db.session.delete(cache)
            db.session.commit()

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_sync_meetings_updates_existing_cache(self, mock_fetch, app):
        """sync_meetings_for_date updates an existing cache row."""
        with app.app_context():
            from app.services.meeting_sync import sync_meetings_for_date

            # Seed existing cache
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 22),
                meetings_json=json.dumps([{'title': 'Old'}]),
            )
            db.session.add(cache)
            db.session.commit()

            mock_fetch.return_value = (
                [
                    {
                        'id': '2',
                        'title': 'New Meeting',
                        'start_time': datetime(2026, 4, 22, 10, 0),
                        'customer': '',
                        'attendees': [],
                    },
                ],
                'new raw',
            )

            result, err = sync_meetings_for_date('2026-04-22')
            assert err is None
            assert result[0]['title'] == 'New Meeting'

            # Only one row for that date
            rows = DailyMeetingCache.query.filter_by(
                meeting_date=date(2026, 4, 22)
            ).all()
            assert len(rows) == 1
            assert rows[0].get_meetings()[0]['title'] == 'New Meeting'

            db.session.delete(rows[0])
            db.session.commit()

    def test_get_cached_meetings_returns_none_when_missing(self, app):
        """get_cached_meetings returns (None, None) for uncached dates."""
        with app.app_context():
            from app.services.meeting_sync import get_cached_meetings

            result, synced_at = get_cached_meetings('2026-01-01')
            assert result is None
            assert synced_at is None

    def test_get_cached_meetings_returns_data(self, app):
        """get_cached_meetings returns cached meetings when present."""
        with app.app_context():
            from app.services.meeting_sync import get_cached_meetings

            meetings = [{'title': 'Cached Call'}]
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 23),
                meetings_json=json.dumps(meetings),
            )
            db.session.add(cache)
            db.session.commit()

            result, synced_at = get_cached_meetings('2026-04-23')
            assert result is not None
            assert len(result) == 1
            assert result[0]['title'] == 'Cached Call'
            assert synced_at is not None

            db.session.delete(cache)
            db.session.commit()

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_sync_handles_workiq_error(self, mock_fetch, app):
        """sync_meetings_for_date returns error string on failure."""
        mock_fetch.side_effect = RuntimeError('WorkIQ timed out')

        with app.app_context():
            from app.services.meeting_sync import sync_meetings_for_date

            result, err = sync_meetings_for_date('2026-04-24')
            assert result == []
            assert 'timed out' in err


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestMeetingsAPI:
    """Tests for /api/meetings and /api/meetings/refresh endpoints."""

    def test_api_meetings_returns_from_cache_for_today(self, client, app):
        """GET /api/meetings for today's date returns from daily cache."""
        today_str = date.today().strftime('%Y-%m-%d')
        with app.app_context():
            # Seed cache for today
            meetings = [
                {
                    'id': '1',
                    'title': 'Morning Standup',
                    'start_time': None,
                    'start_time_display': '09:00 AM',
                    'customer': '',
                    'attendees': [],
                },
            ]
            cache = DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json=json.dumps(meetings),
            )
            db.session.add(cache)
            db.session.commit()

        resp = client.get(f'/api/meetings?date={today_str}')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['from_cache'] is True
        assert len(data['meetings']) == 1
        assert data['meetings'][0]['title'] == 'Morning Standup'
        assert data['synced_at'] is not None

        # Cleanup
        with app.app_context():
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            db.session.commit()

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_api_meetings_live_fetch_for_non_today(self, mock_fetch, client):
        """GET /api/meetings for a past date does a live WorkIQ fetch."""
        mock_fetch.return_value = (
            [
                {
                    'id': 'x',
                    'title': 'Old Meeting',
                    'start_time': datetime(2026, 3, 15, 11, 0),
                    'customer': 'Fabrikam',
                    'attendees': [],
                    'start_time_str': '11:00 AM',
                },
            ],
            'raw response',
        )

        resp = client.get('/api/meetings?date=2026-03-15')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['from_cache'] is False
        assert len(data['meetings']) == 1
        assert data['meetings'][0]['title'] == 'Old Meeting'
        mock_fetch.assert_called_once_with('2026-03-15')

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_api_meetings_today_no_cache_does_live_fetch_and_caches(
        self, mock_fetch, client, app
    ):
        """GET /api/meetings for today with no cache fetches live and caches."""
        today_str = date.today().strftime('%Y-%m-%d')

        # Ensure no cache
        with app.app_context():
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            db.session.commit()

        mock_fetch.return_value = (
            [
                {
                    'id': 'live1',
                    'title': 'Live Meeting',
                    'start_time': datetime(2026, 4, 17, 15, 0),
                    'customer': 'Litware',
                    'attendees': [],
                },
            ],
            'raw live',
        )

        resp = client.get(f'/api/meetings?date={today_str}')
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data['meetings']) == 1

        # Verify it was cached
        with app.app_context():
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date.today()
            ).first()
            assert cache is not None
            db.session.delete(cache)
            db.session.commit()

    def test_api_meetings_missing_date_returns_400(self, client):
        """GET /api/meetings without date param returns 400."""
        resp = client.get('/api/meetings')
        assert resp.status_code == 400

    def test_api_meetings_invalid_date_returns_400(self, client):
        """GET /api/meetings with bad date format returns 400."""
        resp = client.get('/api/meetings?date=not-a-date')
        assert resp.status_code == 400

    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_api_refresh_meetings(self, mock_fetch, client, app):
        """POST /api/meetings/refresh force-fetches and updates cache."""
        today_str = date.today().strftime('%Y-%m-%d')

        # Seed stale cache
        with app.app_context():
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            stale = DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json=json.dumps([{'title': 'Stale'}]),
            )
            db.session.add(stale)
            db.session.commit()

        mock_fetch.return_value = (
            [
                {
                    'id': 'fresh',
                    'title': 'Fresh Meeting',
                    'start_time': datetime(2026, 4, 17, 16, 0),
                    'customer': 'Contoso',
                    'attendees': [],
                },
            ],
            'refreshed raw',
        )

        resp = client.post(
            '/api/meetings/refresh',
            json={'date': today_str},
            content_type='application/json',
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data['meetings']) == 1
        assert data['meetings'][0]['title'] == 'Fresh Meeting'

        # Verify cache updated
        with app.app_context():
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date.today()
            ).first()
            assert cache is not None
            assert cache.get_meetings()[0]['title'] == 'Fresh Meeting'
            db.session.delete(cache)
            db.session.commit()

    def test_api_refresh_missing_date_returns_400(self, client):
        """POST /api/meetings/refresh without date returns 400."""
        resp = client.post(
            '/api/meetings/refresh',
            json={},
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_api_meetings_fuzzy_match_from_cache(self, client, app):
        """GET /api/meetings with customer_name does fuzzy match on cached data."""
        today_str = date.today().strftime('%Y-%m-%d')
        with app.app_context():
            meetings = [
                {
                    'id': '1',
                    'title': 'Contoso Quarterly Review',
                    'start_time': None,
                    'start_time_display': '',
                    'customer': 'Contoso',
                    'attendees': [],
                },
                {
                    'id': '2',
                    'title': 'Internal Standup',
                    'start_time': None,
                    'start_time_display': '',
                    'customer': '',
                    'attendees': [],
                },
            ]
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            cache = DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json=json.dumps(meetings),
            )
            db.session.add(cache)
            db.session.commit()

        resp = client.get(
            f'/api/meetings?date={today_str}&customer_name=Contoso'
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['auto_selected_index'] == 0
        assert 'Contoso' in data['auto_selected_reason']

        with app.app_context():
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            db.session.commit()


# ---------------------------------------------------------------------------
# Scheduler / startup tests
# ---------------------------------------------------------------------------

class TestMeetingSyncScheduler:
    """Tests for startup catchup and daily scheduler logic."""

    def test_should_sync_today_returns_true_when_no_cache(self, app):
        """_should_sync_today returns True when no cache for today."""
        with app.app_context():
            from app.services.meeting_sync import _should_sync_today

            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            db.session.commit()

            assert _should_sync_today() is True

    def test_should_sync_today_returns_false_when_cached(self, app):
        """_should_sync_today returns False when today is already cached."""
        with app.app_context():
            from app.services.meeting_sync import _should_sync_today

            cache = DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json='[]',
            )
            db.session.add(cache)
            db.session.commit()

            assert _should_sync_today() is False

            db.session.delete(cache)
            db.session.commit()

    @patch('app.services.meeting_sync._run_sync')
    def test_start_meeting_sync_background_skips_if_cached(
        self, mock_run, app
    ):
        """start_meeting_sync_background does nothing if today is cached."""
        with app.app_context():
            from app.services.meeting_sync import start_meeting_sync_background

            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            cache = DailyMeetingCache(
                meeting_date=date.today(), meetings_json='[]'
            )
            db.session.add(cache)
            db.session.commit()

        start_meeting_sync_background(app)
        mock_run.assert_not_called()

        with app.app_context():
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            db.session.commit()
