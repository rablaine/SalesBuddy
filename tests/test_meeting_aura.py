"""Tests for the morning aura sync (today + next 7 weekdays).

Spec: docs/GHOST_AURA_SYNC_SPEC.md
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    Customer,
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
    """Patch query_workiq to return a meeting list and record dates called.

    When ``raw_meetings`` is None, returns a single placeholder meeting per
    call so aura tests exercise meeting storage rather than the valid
    no-meetings path.
    """
    called_with: list[str] = []

    def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
        # Date is embedded in the prompt; pull it out for assertions.
        import re
        m = re.search(r'(\d{4}-\d{2}-\d{2})', prompt or '')
        date_str = m.group(1) if m else '2026-01-01'
        if m:
            called_with.append(date_str)
        if raw_meetings is None:
            payload = json.dumps([{
                'subject': f'Stub meeting for {date_str}',
                'start_time': f'{date_str}T10:00:00-05:00',
                'end_time': f'{date_str}T11:00:00-05:00',
                'organizer_email': 'stub@microsoft.com',
                'is_recurring': False,
                'attendees': [{'name': 'Ext', 'email': 'ext@partner.com'}],
            }])
        else:
            payload = json.dumps(raw_meetings)
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
        # Seed at least one customer so ensure_meeting_aura's "no customers"
        # guard doesn't short-circuit the test. Tests covering that guard
        # explicitly delete customers themselves.
        if Customer.query.count() == 0:
            db.session.add(Customer(name='Aura Test Customer', tpid=999001))
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


@pytest.fixture(autouse=True)
def force_workiq_fallback(monkeypatch):
    """Keep historical fixture dates on the mocked WorkIQ code path."""
    def fail_outlook_fetch(target_date):
        raise RuntimeError(f'Outlook disabled for aura test: {target_date}')

    monkeypatch.setattr(
        'app.services.outlook_calendar.fetch_outlook_meetings_for_date',
        fail_outlook_fetch,
    )


# ---------------------------------------------------------------------------
# ensure_meeting_aura
# ---------------------------------------------------------------------------

def test_progress_retains_completed_dates_between_queries(monkeypatch, reset_sync_state):
    """Starting the next source query must preserve measured calendar progress."""
    from app.services.meeting_sync import _set_sync_state

    published = []
    monkeypatch.setattr(
        'app.services.sync_jobs.publish_progress', published.append,
    )
    dates = ['2026-04-20', '2026-04-21', '2026-04-22']
    _set_sync_state(running=True, aura_dates=dates, current_date=dates[0])
    _set_sync_state(current_date=dates[1])
    _set_sync_state(current_date=dates[2])

    assert [event['current'] for event in published] == [0, 1, 2]
    assert [event['total'] for event in published] == [3, 3, 3]
    assert [event['progress'] for event in published] == pytest.approx(
        [0, 100 / 3, 200 / 3],
    )
    assert published[-1]['message'] == 'Refreshing 2026-04-22 (3/3).'

class TestEnsureMeetingAura:
    def test_parallel_refresh_finishes_through_the_durable_worker(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        """Fetch threads must not detach the owning job or lose its persisted outcome."""
        from app.models import Job
        from app.services import job_queue, sync_jobs

        monkeypatch.setattr(
            'app.services.meeting_prefetch.fetch_meetings_for_date',
            lambda day_str: ([], None),
        )
        with app.app_context():
            job = sync_jobs.admit_sync(
                'meeting-aura', {'anchor': '2026-04-20', 'days_ahead': 4})
            assert job_queue.process_one('parallel-test-worker', app=app)
            db.session.refresh(job)
            assert job.status == Job.STATUS_DONE
            result = json.loads(job.result)
            assert result['success'] is True
            assert len(result['counts']) == 5
            assert result['errors'] == {}
            assert DailyMeetingCache.query.count() == 5
            assert sync_jobs.job_snapshot(job)['progress']['percent'] == 100

    def test_all_five_dates_fetch_together_before_serial_saves(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        """All five external calls overlap and no database save runs in their threads."""
        from app.services import meeting_sync

        barrier = threading.Barrier(5)
        lock = threading.Lock()
        fetched = set()
        saved = []
        published = []
        owner_thread = threading.get_ident()
        original_store = meeting_sync.sync_meetings_for_date

        def fetch(day_str):
            """Hold each source until all five dates have actually started."""
            assert threading.get_ident() != owner_thread
            barrier.wait(timeout=5)
            with lock:
                fetched.add(day_str)
            return [], None

        def store(day_str, prefetched_meetings=None):
            """Verify preparation finished before the first real cache transaction."""
            assert threading.get_ident() == owner_thread
            assert len(fetched) == 5
            saved.append(day_str)
            return original_store(day_str, prefetched_meetings=prefetched_meetings)

        monkeypatch.setattr('app.services.meeting_prefetch.fetch_meetings_for_date', fetch)
        monkeypatch.setattr(meeting_sync, 'sync_meetings_for_date', store)
        monkeypatch.setattr('app.services.sync_jobs.publish_progress', published.append)
        with app.app_context():
            counts, errors = meeting_sync.ensure_meeting_aura(
                start=date(2026, 4, 20), days_ahead=4)
            assert DailyMeetingCache.query.count() == 5

        assert errors == {}
        assert len(counts) == 5
        assert saved == sorted(fetched)
        assert [event['current'] for event in published] == (
            list(range(6)) + [value for n in range(5, 10) for value in (n, n + 1)])
        assert all(event['total'] == 10 for event in published)
        assert published[0]['message'] == 'Fetching 5 meeting dates in parallel.'
        assert published[5]['progress'] == 50
        assert published[-1]['message'] == 'Saved 5/5 meeting dates (0 failed).'
        assert published[-1]['date_states'] == dict.fromkeys(sorted(fetched), 'complete')
        assert all('complete' not in event.get('date_states', {}).values()
                   for event in published if event['phase'] == 'fetching')

    def test_out_of_order_fetch_does_not_finish_unsaved_dates(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        """A later calendar date finishing its fetch cannot mark earlier dates complete."""
        from app.services import meeting_sync

        release_first = threading.Event()
        published = []

        def fetch(day_str):
            """Finish Tuesday first, holding Monday until its progress is observed."""
            if day_str == '2026-04-20':
                assert release_first.wait(timeout=5)
            return [], None

        def progress(event):
            """Inspect the actual saved cache at each per-date completion transition."""
            published.append(event)
            if event['phase'] == 'fetching' and event['current'] == 1:
                assert event['current_date'] == '2026-04-21'
                assert event['date_states'] == {
                    '2026-04-20': 'fetching', '2026-04-21': 'queued'}
                assert DailyMeetingCache.query.count() == 0
                release_first.set()
            for day_str, state in event.get('date_states', {}).items():
                if state == 'complete':
                    assert DailyMeetingCache.query.filter_by(
                        meeting_date=date.fromisoformat(day_str)).one()

        monkeypatch.setattr('app.services.meeting_prefetch.fetch_meetings_for_date', fetch)
        monkeypatch.setattr('app.services.sync_jobs.publish_progress', progress)
        with app.app_context():
            counts, errors = meeting_sync.ensure_meeting_aura(
                start=date(2026, 4, 20), days_ahead=1)
        assert errors == {}
        assert counts == {'2026-04-20': 0, '2026-04-21': 0}
        assert published[-1]['date_states'] == {
            '2026-04-20': 'complete', '2026-04-21': 'complete'}

    def test_parallel_fetch_failure_preserves_cache_and_saves_other_dates(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        """A failed date keeps its prior cache while independent successful dates save."""
        from app.services.meeting_sync import ensure_meeting_aura

        failed_date = date(2026, 4, 21)

        def fetch(day_str):
            """Return one explicit source failure among otherwise empty valid calendars."""
            if day_str == failed_date.isoformat():
                return None, 'WorkIQ unavailable for this date'
            return [], None

        monkeypatch.setattr('app.services.meeting_prefetch.fetch_meetings_for_date', fetch)
        with app.app_context():
            db.session.add(DailyMeetingCache(
                meeting_date=failed_date, meetings_json='[{"title":"Keep this cache"}]'))
            db.session.commit()
            counts, errors = ensure_meeting_aura(start=date(2026, 4, 20), days_ahead=4)
            cached = DailyMeetingCache.query.filter_by(meeting_date=failed_date).one()
            assert cached.meetings_json == '[{"title":"Keep this cache"}]'
            assert DailyMeetingCache.query.count() == 5
        assert len(counts) == 5
        assert errors == {failed_date.isoformat(): 'WorkIQ unavailable for this date'}

    def test_skips_weekends(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        from app.services.meeting_sync import ensure_meeting_aura

        # Friday 2026-04-24 + 4 business days = Fri, Mon, Tue, Wed, Thu.
        # Saturday/Sunday in between are skipped.
        anchor = date(2026, 4, 24)
        called = _stub_workiq(monkeypatch)

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
            return json.dumps([{
                'subject': f'Stub for {d}',
                'start_time': f'{d}T10:00:00-05:00',
                'end_time': f'{d}T11:00:00-05:00',
                'organizer_email': 'stub@microsoft.com',
                'is_recurring': False,
                'attendees': [{'name': 'Ext', 'email': 'ext@partner.com'}],
            }])

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

        # Anchor to today (or the previous weekday) so the seeded ghost
        # falls inside the 5-business-day retention window enforced by
        # purge_expired(). Hard-coding a historical date here would cause
        # purge_expired to nuke the seeded row before ensure_meeting_aura
        # could exercise the upsert/preservation path.
        anchor = date.today()
        while anchor.weekday() >= 5:  # back up to Friday over the weekend
            anchor = anchor - timedelta(days=1)
        anchor_str = anchor.isoformat()
        start = datetime.combine(anchor, datetime.min.time()).replace(hour=10)
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
                expires_at=datetime.now(timezone.utc) + timedelta(days=2),
            ))
            db.session.add(PrefetchedMeeting(
                workiq_id=wid_n, subject=subject_n,
                start_time=start.replace(hour=11),
                end_time=start.replace(hour=11) + timedelta(minutes=30),
                meeting_date=anchor, organizer_email=organizer,
                is_recurring=False, customer_id=customer_id, note_id=note_id,
                dismissed=False,
                expires_at=datetime.now(timezone.utc) + timedelta(days=2),
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
            if d == anchor_str:
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
# Empty customer DB guard
# ---------------------------------------------------------------------------

class TestEnsureMeetingAuraNoCustomersGuard:
    def test_skips_aura_when_no_customers(
        self, app, clean_meeting_tables, reset_sync_state, monkeypatch,
    ):
        """Fresh install: aura must not run before MSX account import.

        Without customers, every ghost would land with customer_id=NULL
        because both _build_domain_map and _build_subject_matchers come up
        empty. Skipping prevents poisoning the cache with garbage that
        won't get re-resolved unless the user manually clicks Refresh.
        """
        from app.services.meeting_sync import ensure_meeting_aura
        with app.app_context():
            # clean_meeting_tables seeded a customer; remove it.
            Customer.query.delete()
            db.session.commit()
            assert Customer.query.count() == 0

            called = _stub_workiq(monkeypatch)
            counts, errors = ensure_meeting_aura(
                start=date(2026, 4, 24), days_ahead=2,
            )

            assert called == [], "WorkIQ must not be hit when no customers"
            assert counts == {}
            assert '_status' in errors
            assert 'no customers' in errors['_status'].lower()


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
        assert body['jobs'] == []

    def test_discovers_all_queued_manual_and_automatic_dates(
        self, app, client, clean_meeting_tables,
    ):
        """A fresh page discovers every pending date without starting another sync."""
        from app.models import Job
        from app.services.sync_jobs import admit_sync

        with app.app_context():
            first = admit_sync('meetings', {'date': '2026-04-20'})
            second = admit_sync('meetings', {'date': '2026-04-21'})
            aura = admit_sync('meeting-aura', {'anchor': '2026-04-24', 'days_ahead': 4})
            ids = [first.id, second.id, aura.id]
        response = client.get('/api/meetings/sync-status')
        assert response.status_code == 200
        jobs = response.json['jobs']
        assert [job['id'] for job in jobs] == ids
        assert jobs[0]['calendar_dates'] == ['2026-04-20']
        assert jobs[1]['calendar_dates'] == ['2026-04-21']
        assert jobs[2]['calendar_dates'] == [
            '2026-04-24', '2026-04-27', '2026-04-28', '2026-04-29', '2026-04-30']
        with app.app_context():
            assert Job.query.count() == 3

    def test_observed_job_remains_readable_when_newer_dates_have_been_admitted(self, app, client):
        """A completion cannot vanish between polls just because another date was started."""
        from app.models import Job
        from app.services.sync_jobs import admit_sync

        with app.app_context():
            first = admit_sync('meetings', {'date': '2026-04-20'})
            first_id = first.id
            first.status = Job.STATUS_DONE
            first.finished_at = datetime.now(timezone.utc)
            first.result = json.dumps({
                'date': '2026-04-20', 'synced_at': first.finished_at.isoformat(),
                'meetings': [], 'success': True})
            db.session.commit()
            second = admit_sync('meetings', {'date': '2026-04-21'})
            second_id = second.id
        jobs = client.get(f'/api/meetings/sync-status?job_id={first_id}').json['jobs']
        assert [job['id'] for job in jobs] == [first_id, second_id]
        assert jobs[0]['status'] == 'done'
        assert jobs[0]['result']['date'] == '2026-04-20'

    @pytest.mark.parametrize('identity', ['invalid', '0', '-1', '1.5'])
    def test_rejects_invalid_observed_identity(self, client, identity):
        """Malformed observation requests fail explicitly rather than dropping identities."""
        response = client.get('/api/meetings/sync-status?job_id=' + identity)
        assert response.status_code == 400
        assert 'positive integers' in response.json['error']
