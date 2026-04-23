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

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_meetings_for_date_caches_result(self, mock_query, app):
        """sync_meetings_for_date stores meetings in DailyMeetingCache."""
        mock_query.return_value = (
            '```json\n[{"subject": "Customer Sync", '
            '"start_time": "2026-04-21T14:00:00-05:00", '
            '"end_time": "2026-04-21T15:00:00-05:00", '
            '"organizer_email": "alice@contoso.com", '
            '"is_recurring": false, '
            '"attendees": [{"name": "Alice", "email": "alice@contoso.com"}]'
            '}]\n```'
        )

        with app.app_context():
            from app.services.meeting_sync import sync_meetings_for_date
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

            result, err = sync_meetings_for_date('2026-04-21')
            assert err is None
            assert len(result) == 1
            assert result[0]['title'] == 'Customer Sync'
            assert result[0]['start_time_display'] == '07:00 PM'  # UTC-normalized

            # Verify DB row
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date(2026, 4, 21)
            ).first()
            assert cache is not None
            assert len(cache.get_meetings()) == 1

            db.session.delete(cache)
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_meetings_updates_existing_cache(self, mock_query, app):
        """sync_meetings_for_date updates an existing cache row."""
        with app.app_context():
            from app.services.meeting_sync import sync_meetings_for_date
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

            # Seed existing cache
            cache = DailyMeetingCache(
                meeting_date=date(2026, 4, 22),
                meetings_json=json.dumps([{'title': 'Old'}]),
            )
            db.session.add(cache)
            db.session.commit()

            mock_query.return_value = (
                '```json\n[{"subject": "New Meeting", '
                '"start_time": "2026-04-22T10:00:00+00:00", '
                '"end_time": "2026-04-22T11:00:00+00:00", '
                '"organizer_email": "x@y.com", '
                '"is_recurring": false, "attendees": []}]\n```'
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
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
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

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_handles_workiq_error(self, mock_query, app):
        """sync_meetings_for_date returns error string on failure."""
        mock_query.side_effect = RuntimeError('WorkIQ timed out')

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

    @patch('app.services.workiq_service.query_workiq')
    def test_api_meetings_today_no_cache_does_live_fetch_and_caches(
        self, mock_query, client, app
    ):
        """GET /api/meetings for today with no cache fetches live and caches."""
        today_str = date.today().strftime('%Y-%m-%d')

        # Ensure no cache
        with app.app_context():
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

        mock_query.return_value = (
            '```json\n[{"subject": "Live Meeting", '
            '"start_time": "' + today_str + 'T15:00:00+00:00", '
            '"end_time": "' + today_str + 'T16:00:00+00:00", '
            '"organizer_email": "x@litware.com", '
            '"is_recurring": false, "attendees": []}]\n```'
        )

        resp = client.get(f'/api/meetings?date={today_str}')
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data['meetings']) == 1

        # Verify it was cached
        with app.app_context():
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date.today()
            ).first()
            assert cache is not None
            db.session.delete(cache)
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

    def test_api_meetings_missing_date_returns_400(self, client):
        """GET /api/meetings without date param returns 400."""
        resp = client.get('/api/meetings')
        assert resp.status_code == 400

    def test_api_meetings_invalid_date_returns_400(self, client):
        """GET /api/meetings with bad date format returns 400."""
        resp = client.get('/api/meetings?date=not-a-date')
        assert resp.status_code == 400

    @patch('app.services.workiq_service.query_workiq')
    def test_api_refresh_meetings(self, mock_query, client, app):
        """POST /api/meetings/refresh force-fetches and updates cache."""
        today_str = date.today().strftime('%Y-%m-%d')

        # Seed stale cache
        with app.app_context():
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            stale = DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json=json.dumps([{'title': 'Stale'}]),
            )
            db.session.add(stale)
            db.session.commit()

        mock_query.return_value = (
            '```json\n[{"subject": "Fresh Meeting", '
            '"start_time": "' + today_str + 'T16:00:00+00:00", '
            '"end_time": "' + today_str + 'T17:00:00+00:00", '
            '"organizer_email": "x@contoso.com", '
            '"is_recurring": false, "attendees": []}]\n```'
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
            from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
            cache = DailyMeetingCache.query.filter_by(
                meeting_date=date.today()
            ).first()
            assert cache is not None
            assert cache.get_meetings()[0]['title'] == 'Fresh Meeting'
            db.session.delete(cache)
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
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
        """start_meeting_sync_background does nothing if the full aura window is cached."""
        with app.app_context():
            from datetime import timedelta, datetime, timezone
            from app.services.meeting_sync import (
                start_meeting_sync_background,
                _aura_window_dates,
            )

            window = _aura_window_dates()
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
                # synced_at must be on/after the day itself to count as fresh.
                synced_at = datetime.combine(
                    d, datetime.min.time(), tzinfo=timezone.utc,
                ) + timedelta(hours=8)
                db.session.add(DailyMeetingCache(
                    meeting_date=d, meetings_json='[]', synced_at=synced_at,
                ))
            db.session.commit()

        start_meeting_sync_background(app)
        mock_run.assert_not_called()

        with app.app_context():
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            db.session.commit()

    @patch('app.services.meeting_sync._run_sync')
    def test_aura_needs_run_false_when_morning_sync_just_ran(
        self, mock_run, app
    ):
        """Regression: morning sync stamps every day in the window with the
        same synced_at (today 7am). A future-day cache must NOT be considered
        stale just because today 7am < tomorrow midnight. Otherwise every
        server restart after the morning sync re-triggers the full aura sync
        and runs the pre-sync ghost purge.
        """
        with app.app_context():
            from app.services.meeting_sync import (
                start_meeting_sync_background,
                _aura_window_dates,
                _aura_needs_run,
            )

            window = _aura_window_dates()
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            # Simulate a real morning sync: every day in the window gets
            # the SAME synced_at (today at 7am local).
            morning_sync_time = datetime.combine(
                date.today(), datetime.min.time()
            ).replace(hour=7)
            for d in window:
                db.session.add(DailyMeetingCache(
                    meeting_date=d,
                    meetings_json='[]',
                    synced_at=morning_sync_time,
                ))
            db.session.commit()

            # Direct check: the freshness rule should accept today's morning
            # sync as fresh for every day in the window.
            assert _aura_needs_run(app) is False

        # Startup catchup should also NOT trigger a re-sync.
        start_meeting_sync_background(app)
        mock_run.assert_not_called()

        with app.app_context():
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            db.session.commit()

    @patch('app.services.meeting_sync._run_sync')
    def test_aura_needs_run_true_when_cache_from_yesterday(
        self, mock_run, app
    ):
        """If the cache was synced yesterday (or earlier), it IS stale and
        a fresh sync should fire."""
        from datetime import timedelta as _td
        from unittest.mock import patch as _patch

        with app.app_context():
            from app.services.meeting_sync import (
                _aura_window_dates,
                _aura_needs_run,
            )

            window = _aura_window_dates()
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            # Stamp every day with yesterday-7am as synced_at.
            yesterday_sync = datetime.combine(
                date.today() - _td(days=1), datetime.min.time()
            ).replace(hour=7)
            for d in window:
                db.session.add(DailyMeetingCache(
                    meeting_date=d,
                    meetings_json='[]',
                    synced_at=yesterday_sync,
                ))
            db.session.commit()

            # Pin the boundary to "today 7am" so the test is not
            # time-of-day dependent (would fail if run before 7am local).
            today_boundary = datetime.combine(
                date.today(), datetime.min.time()
            ).replace(hour=7)
            with _patch(
                'app.services.meeting_sync._most_recent_sync_boundary',
                return_value=today_boundary,
            ):
                assert _aura_needs_run(app) is True

            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            db.session.commit()

    @patch('app.services.meeting_sync._run_sync')
    def test_aura_needs_run_true_when_prefetched_table_drifts_from_cache(
        self, mock_run, app
    ):
        """Self-heal: cache promises N meetings but prefetched_meetings has
        fewer rows for that date (interrupted sync, expired purge, etc).
        Restart should detect the drift and re-run the aura.

        Caught 2026-04-22: 27/28 had cached picker meetings but zero
        PrefetchedMeeting rows, so no ghosts appeared and restart didn't
        self-heal until this check was added.
        """
        from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee
        from datetime import timedelta as _td

        with app.app_context():
            from app.services.meeting_sync import (
                _aura_window_dates,
                _aura_needs_run,
                SYNC_HOUR,
            )

            window = _aura_window_dates()
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()

            # Fresh sync stamp on every cache row.
            fresh_sync = datetime.combine(
                date.today(), datetime.min.time()
            ).replace(hour=SYNC_HOUR)
            picker_payload = json.dumps([
                {'id': 'a', 'title': 'Meeting A'},
                {'id': 'b', 'title': 'Meeting B'},
            ])
            for d in window:
                db.session.add(DailyMeetingCache(
                    meeting_date=d,
                    meetings_json=picker_payload,
                    synced_at=fresh_sync,
                ))
            db.session.commit()

            # No PrefetchedMeeting rows at all -> drift -> needs run.
            assert _aura_needs_run(app) is True

            # Add matching rows for every day -> no drift -> no run needed.
            for d in window:
                for i, wid in enumerate(('a', 'b')):
                    db.session.add(PrefetchedMeeting(
                        workiq_id=f'{d.isoformat()}-{wid}',
                        subject=f'Meeting {wid.upper()}',
                        start_time=datetime.combine(
                            d, datetime.min.time()
                        ).replace(hour=10 + i),
                        meeting_date=d,
                        expires_at=datetime.combine(
                            d + _td(days=1), datetime.min.time()
                        ).replace(hour=23, minute=59),
                    ))
            db.session.commit()
            assert _aura_needs_run(app) is False

            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            PrefetchedMeetingAttendee.query.delete()
            PrefetchedMeeting.query.delete()
            db.session.commit()

    def test_most_recent_sync_boundary_before_sync_hour(self):
        """Before today's SYNC_HOUR, the boundary is yesterday at SYNC_HOUR
        so a sync that ran yesterday morning still counts as fresh."""
        from datetime import timedelta as _td
        from app.services.meeting_sync import (
            _most_recent_sync_boundary, SYNC_HOUR,
        )

        early_morning = datetime.combine(
            date.today(), datetime.min.time()
        ).replace(hour=SYNC_HOUR - 1)
        boundary = _most_recent_sync_boundary(now=early_morning)
        expected = datetime.combine(
            date.today() - _td(days=1), datetime.min.time()
        ).replace(hour=SYNC_HOUR)
        assert boundary == expected

    def test_most_recent_sync_boundary_after_sync_hour(self):
        """At/after today's SYNC_HOUR, the boundary is today at SYNC_HOUR."""
        from app.services.meeting_sync import (
            _most_recent_sync_boundary, SYNC_HOUR,
        )

        midday = datetime.combine(
            date.today(), datetime.min.time()
        ).replace(hour=SYNC_HOUR + 5)
        boundary = _most_recent_sync_boundary(now=midday)
        expected = datetime.combine(
            date.today(), datetime.min.time()
        ).replace(hour=SYNC_HOUR)
        assert boundary == expected

    @patch('app.services.meeting_sync._run_sync')
    def test_aura_needs_run_false_for_early_morning_restart(
        self, mock_run, app
    ):
        """Restart at 6am (before today's 7am sync) must NOT re-trigger:
        yesterday's 7am sync is still the current cycle."""
        from datetime import timedelta as _td
        from unittest.mock import patch as _patch

        with app.app_context():
            from app.services.meeting_sync import (
                _aura_window_dates,
                _aura_needs_run,
                SYNC_HOUR,
            )

            window = _aura_window_dates()
            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            # Yesterday's 7am sync stamped every day in the window.
            yesterday_sync = datetime.combine(
                date.today() - _td(days=1), datetime.min.time()
            ).replace(hour=SYNC_HOUR)
            for d in window:
                db.session.add(DailyMeetingCache(
                    meeting_date=d,
                    meetings_json='[]',
                    synced_at=yesterday_sync,
                ))
            db.session.commit()

            # Pretend "now" is 6am today (before the next scheduled sync) by
            # forcing the boundary helper to return yesterday's 7am.
            with _patch(
                'app.services.meeting_sync._most_recent_sync_boundary',
                return_value=yesterday_sync,
            ):
                assert _aura_needs_run(app) is False

            for d in window:
                DailyMeetingCache.query.filter_by(meeting_date=d).delete()
            db.session.commit()
