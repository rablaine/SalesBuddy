"""Tests for the morning aura sync (today + next 7 weekdays).

Spec: docs/GHOST_AURA_SYNC_SPEC.md
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    DailyMeetingCache,
    DismissedRecurringMeeting,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_workiq(monkeypatch, raw_meetings: list[dict] | None = None) -> list[str]:
    """Patch query_workiq to return an empty meeting list and record dates called."""
    payload = json.dumps(raw_meetings or [])
    called_with: list[str] = []

    def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
        # Date is embedded in the prompt; pull it out for assertions.
        import re
        m = re.search(r'(\d{4}-\d{2}-\d{2})', prompt or '')
        if m:
            called_with.append(m.group(1))
        return payload

    monkeypatch.setattr(
        'app.services.workiq_service.query_workiq', fake_query, raising=False,
    )
    monkeypatch.setattr(
        'app.services.meeting_prefetch.query_workiq', fake_query, raising=False,
    )
    return called_with


@pytest.fixture
def clean_meeting_tables(app):
    with app.app_context():
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        DailyMeetingCache.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()
        yield
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        DailyMeetingCache.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()


@pytest.fixture
def reset_sync_state():
    from app.services import meeting_sync
    # Make sure no prior test left the lock or state in a weird shape.
    if meeting_sync._sync_lock.locked():
        try:
            meeting_sync._sync_lock.release()
        except RuntimeError:
            pass
    meeting_sync._clear_sync_state()
    yield
    if meeting_sync._sync_lock.locked():
        try:
            meeting_sync._sync_lock.release()
        except RuntimeError:
            pass
    meeting_sync._clear_sync_state()


# ---------------------------------------------------------------------------
# ensure_meeting_aura
# ---------------------------------------------------------------------------

class TestEnsureMeetingAura:
    def test_skips_weekends(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        from app.services.meeting_sync import ensure_meeting_aura

        # Friday 2026-04-24 + 4 business days = Fri, Mon, Tue, Wed, Thu.
        # Saturday/Sunday in between are skipped.
        anchor = date(2026, 4, 24)
        called = _stub_workiq(monkeypatch, [])

        with app.app_context():
            counts, errors = ensure_meeting_aura(start=anchor, days_ahead=4)

        assert sorted(called) == [
            '2026-04-24', '2026-04-27', '2026-04-28',
            '2026-04-29', '2026-04-30',
        ]
        assert sorted(counts.keys()) == [
            '2026-04-24', '2026-04-27', '2026-04-28',
            '2026-04-29', '2026-04-30',
        ]
        assert errors == {}

    def test_one_day_failure_does_not_abort_rest(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        from app.services.meeting_sync import ensure_meeting_aura

        anchor = date(2026, 4, 20)  # Mon → Mon..Wed (days_ahead=2)
        # Make day 2 raise.
        boom_date = '2026-04-21'

        def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
            import re
            m = re.search(r'(\d{4}-\d{2}-\d{2})', prompt or '')
            d = m.group(1) if m else None
            if d == boom_date:
                raise RuntimeError("simulated workiq failure")
            return json.dumps([])

        monkeypatch.setattr(
            'app.services.workiq_service.query_workiq', fake_query, raising=False,
        )
        monkeypatch.setattr(
            'app.services.meeting_prefetch.query_workiq', fake_query, raising=False,
        )

        with app.app_context():
            counts, errors = ensure_meeting_aura(start=anchor, days_ahead=2)

        # All three weekdays appear in counts.
        assert set(counts.keys()) == {'2026-04-20', '2026-04-21', '2026-04-22'}
        # Only the boom day errored.
        assert boom_date in errors
        assert '2026-04-20' not in errors
        assert '2026-04-22' not in errors

    def test_re_runs_populated_days_preserving_dismissed_and_noted(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        from app.services.meeting_sync import ensure_meeting_aura
        from app.services.meeting_prefetch import _synthetic_id
        from app.models import Customer, Note

        anchor = date(2026, 4, 20)  # Monday
        start = datetime(2026, 4, 20, 10, 0)
        organizer = 'rerun@example.com'
        subject_d = 'Recurring Standup'
        subject_n = 'Customer Sync'
        wid_d = _synthetic_id(subject_d, start, organizer)
        wid_n = _synthetic_id(
            subject_n, start.replace(hour=11), organizer,
        )

        with app.app_context():
            customer = Customer(name='Acme Aura', tpid=940101)
            db.session.add(customer)
            db.session.flush()
            note = Note(
                customer_id=customer.id,
                call_date=start.replace(hour=11),
                content='already noted',
            )
            db.session.add(note)
            db.session.flush()
            note_id = note.id
            customer_id = customer.id

            # Pre-seed: dismissed + noted ghost on Monday.
            db.session.add(PrefetchedMeeting(
                workiq_id=wid_d, subject=subject_d, start_time=start,
                end_time=start + timedelta(minutes=30),
                meeting_date=anchor, organizer_email=organizer,
                is_recurring=False, customer_id=None, dismissed=True,
                expires_at=datetime.utcnow() + timedelta(days=2),
            ))
            db.session.add(PrefetchedMeeting(
                workiq_id=wid_n, subject=subject_n,
                start_time=start.replace(hour=11),
                end_time=start.replace(hour=11) + timedelta(minutes=30),
                meeting_date=anchor, organizer_email=organizer,
                is_recurring=False, customer_id=customer_id, note_id=note_id,
                dismissed=False,
                expires_at=datetime.utcnow() + timedelta(days=2),
            ))
            db.session.commit()
            seeded_ids = {
                wid_d: PrefetchedMeeting.query.filter_by(workiq_id=wid_d).first().id,
                wid_n: PrefetchedMeeting.query.filter_by(workiq_id=wid_n).first().id,
            }

        # Stub WorkIQ to return ONLY the dismissed meeting on Monday
        # (the noted meeting won't be returned, simulating Outlook drift).
        monday_payload = [{
            'subject': subject_d,
            'start_time': start.isoformat(),
            'end_time': (start + timedelta(minutes=30)).isoformat(),
            'organizer': {'name': 'Org', 'email': organizer},
            'is_recurring': False,
            'attendees': [{'name': 'External', 'email': 'ext@p.com'}],
        }]

        def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
            import re
            m = re.search(r'(\d{4}-\d{2}-\d{2})', prompt or '')
            d = m.group(1) if m else None
            if d == '2026-04-20':
                return json.dumps(monday_payload)
            return json.dumps([])

        monkeypatch.setattr(
            'app.services.workiq_service.query_workiq', fake_query, raising=False,
        )
        monkeypatch.setattr(
            'app.services.meeting_prefetch.query_workiq', fake_query, raising=False,
        )

        with app.app_context():
            ensure_meeting_aura(start=anchor, days_ahead=0)

            # Dismissed row preserved with same id (upsert in place).
            dismissed_row = PrefetchedMeeting.query.filter_by(workiq_id=wid_d).first()
            assert dismissed_row is not None
            assert dismissed_row.dismissed is True
            assert dismissed_row.id == seeded_ids[wid_d]

            # Noted row preserved even though Outlook didn't return it -
            # the pre-sync purge skips note_id IS NOT NULL rows.
            noted_row = PrefetchedMeeting.query.filter_by(workiq_id=wid_n).first()
            assert noted_row is not None
            assert noted_row.note_id == note_id
            assert noted_row.id == seeded_ids[wid_n]

    def test_lock_already_held_returns_status_marker(
        self, app, clean_meeting_tables, reset_sync_state,
    ):
        from app.services.meeting_sync import ensure_meeting_aura, _sync_lock

        with app.app_context():
            assert _sync_lock.acquire(blocking=False) is True
            try:
                counts, errors = ensure_meeting_aura(
                    start=date(2026, 4, 20), days_ahead=0,
                )
            finally:
                _sync_lock.release()

        assert counts == {}
        assert errors == {'_status': 'already running'}

    def test_state_snapshot_reflects_running(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        from app.services.meeting_sync import (
            ensure_meeting_aura,
            get_sync_state_snapshot,
        )

        gate = threading.Event()
        observed: dict = {}

        def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
            # Snapshot state from inside the sync.
            observed['snapshot'] = get_sync_state_snapshot()
            gate.set()
            return json.dumps([])

        monkeypatch.setattr(
            'app.services.workiq_service.query_workiq', fake_query, raising=False,
        )
        monkeypatch.setattr(
            'app.services.meeting_prefetch.query_workiq', fake_query, raising=False,
        )

        with app.app_context():
            ensure_meeting_aura(start=date(2026, 4, 20), days_ahead=0)

        assert gate.is_set()
        snap = observed['snapshot']
        assert snap['running'] is True
        assert snap['current_date'] == '2026-04-20'
        assert snap['started_at'] is not None
        assert snap['aura_dates'] == ['2026-04-20']

        # After return, state is cleared.
        cleared = get_sync_state_snapshot()
        assert cleared['running'] is False
        assert cleared['current_date'] is None


# ---------------------------------------------------------------------------
# /api/meetings/today-status
# ---------------------------------------------------------------------------

class TestTodayStatusEndpoint:
    def test_synced_true_when_cache_is_today(
        self, app, client, clean_meeting_tables,
    ):
        with app.app_context():
            db.session.add(DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json='[]',
                synced_at=datetime.now(timezone.utc),
            ))
            db.session.commit()

        resp = client.get('/api/meetings/today-status')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['synced'] is True
        assert body['synced_at'] is not None
        assert body['today'] == date.today().strftime('%Y-%m-%d')

    def test_synced_false_when_no_cache(self, app, client, clean_meeting_tables):
        resp = client.get('/api/meetings/today-status')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['synced'] is False
        assert body['synced_at'] is None

    def test_synced_false_when_cache_is_stale(
        self, app, client, clean_meeting_tables,
    ):
        with app.app_context():
            yesterday_utc = datetime.now(timezone.utc) - timedelta(days=2)
            db.session.add(DailyMeetingCache(
                meeting_date=date.today(),
                meetings_json='[]',
                synced_at=yesterday_utc,
            ))
            db.session.commit()

        resp = client.get('/api/meetings/today-status')
        body = resp.get_json()
        assert body['synced'] is False
        assert body['synced_at'] is not None  # we still report when it last synced


# ---------------------------------------------------------------------------
# /api/meetings/sync-status
# ---------------------------------------------------------------------------

class TestSyncStatusEndpoint:
    def test_returns_idle_state_when_nothing_running(
        self, app, client, reset_sync_state,
    ):
        resp = client.get('/api/meetings/sync-status')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['running'] is False
        assert body['current_date'] is None
        assert body['aura_dates'] == []
