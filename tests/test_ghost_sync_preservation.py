"""Tests for the morning-sync ghost preservation rules.

Spec: docs/GHOST_SYNC_PRESERVATION_SPEC.md

Covers:
- Pre-sync purge wipes un-noted, undismissed ghosts.
- Pre-sync purge preserves dismissed ghosts and noted ghosts.
- Re-sync of the same meeting preserves the ``dismissed`` flag and ``note_id``
  via the existing upsert path.
- Note creation with ``from_meeting`` back-links the PrefetchedMeeting row.
- Bad ``from_meeting`` value never breaks note creation.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.models import (
    Customer,
    Note,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_ghost(
    *,
    subject: str = 'Test Meeting',
    meeting_date: date,
    start_time: datetime,
    workiq_id: str = 'wid-test-1',
    customer_id: int | None = None,
    dismissed: bool = False,
    note_id: int | None = None,
    organizer: str = 'org@example.com',
) -> PrefetchedMeeting:
    m = PrefetchedMeeting(
        workiq_id=workiq_id,
        subject=subject,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        meeting_date=meeting_date,
        organizer_email=organizer,
        is_recurring=False,
        recurring_key=None,
        customer_id=customer_id,
        dismissed=dismissed,
        note_id=note_id,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db.session.add(m)
    db.session.flush()
    db.session.add(PrefetchedMeetingAttendee(
        meeting_id=m.id,
        name='External User',
        email='ext@partner.com',
        domain='partner.com',
        is_external=True,
    ))
    db.session.commit()
    return m


def _stub_workiq(monkeypatch, raw_meetings: list[dict]) -> None:
    """Patch ``query_workiq`` to return a JSON-shaped string."""
    payload = json.dumps(raw_meetings)

    def fake_query(prompt, timeout=None, operation=None):  # noqa: ARG001
        return payload

    monkeypatch.setattr(
        'app.services.workiq_service.query_workiq', fake_query,
    )
    # The prefetch module imports lazily inside the function, so also patch
    # it where it's actually looked up if a direct module import has happened.
    monkeypatch.setattr(
        'app.services.meeting_prefetch.query_workiq', fake_query,
        raising=False,
    )


@pytest.fixture
def clean_meeting_tables(app):
    with app.app_context():
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        from app.models import DailyMeetingCache, DismissedRecurringMeeting
        DailyMeetingCache.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()
        yield
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        DailyMeetingCache.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()


# ---------------------------------------------------------------------------
# Pre-sync purge
# ---------------------------------------------------------------------------

class TestPreSyncPurge:
    def test_purges_undismissed_un_noted_ghosts(
        self, app, clean_meeting_tables, monkeypatch,
    ):
        from app.services.meeting_sync import sync_meetings_for_date
        with app.app_context():
            today = date.today()
            _seed_ghost(
                meeting_date=today,
                start_time=datetime.combine(today, datetime.min.time()).replace(hour=10),
                workiq_id='will-be-purged',
            )
            _stub_workiq(monkeypatch, [])

            sync_meetings_for_date(today.strftime('%Y-%m-%d'))

            remaining = PrefetchedMeeting.query.filter_by(
                meeting_date=today,
            ).all()
            assert len(remaining) == 0

    def test_preserves_dismissed_ghost(
        self, app, clean_meeting_tables, monkeypatch,
    ):
        from app.services.meeting_sync import sync_meetings_for_date
        with app.app_context():
            today = date.today()
            _seed_ghost(
                meeting_date=today,
                start_time=datetime.combine(today, datetime.min.time()).replace(hour=10),
                workiq_id='dismissed-survives',
                dismissed=True,
            )
            _stub_workiq(monkeypatch, [])

            sync_meetings_for_date(today.strftime('%Y-%m-%d'))

            row = PrefetchedMeeting.query.filter_by(
                workiq_id='dismissed-survives',
            ).first()
            assert row is not None
            assert row.dismissed is True

    def test_preserves_noted_ghost(
        self, app, clean_meeting_tables, monkeypatch,
    ):
        from app.services.meeting_sync import sync_meetings_for_date
        with app.app_context():
            today = date.today()
            customer = Customer(name='Acme', tpid=910101)
            db.session.add(customer)
            db.session.flush()
            note = Note(
                customer_id=customer.id,
                call_date=datetime.combine(today, datetime.min.time()),
                content='x',
            )
            db.session.add(note)
            db.session.flush()
            _seed_ghost(
                meeting_date=today,
                start_time=datetime.combine(today, datetime.min.time()).replace(hour=11),
                workiq_id='noted-survives',
                note_id=note.id,
                customer_id=customer.id,
            )
            _stub_workiq(monkeypatch, [])

            sync_meetings_for_date(today.strftime('%Y-%m-%d'))

            row = PrefetchedMeeting.query.filter_by(
                workiq_id='noted-survives',
            ).first()
            assert row is not None
            assert row.note_id == note.id


# ---------------------------------------------------------------------------
# Re-sync preserves dismissed/note_id via upsert
# ---------------------------------------------------------------------------

class TestResyncPreservesFlags:
    def _meeting_payload(self, subject, start_dt, organizer):
        return {
            'subject': subject,
            'start_time': start_dt.isoformat(),
            'end_time': (start_dt + timedelta(minutes=30)).isoformat(),
            'organizer': {'name': 'Org', 'email': organizer},
            'is_recurring': False,
            'attendees': [
                {'name': 'External', 'email': 'ext@partner.com'},
            ],
        }

    def test_preserves_dismissed_when_meeting_returns(
        self, app, clean_meeting_tables, monkeypatch,
    ):
        from app.services.meeting_sync import sync_meetings_for_date
        from app.services.meeting_prefetch import _synthetic_id

        with app.app_context():
            today = date.today()
            start = datetime.combine(today, datetime.min.time()).replace(hour=14)
            organizer = 'returner@example.com'
            subject = 'Recurring Standup'
            wid = _synthetic_id(subject, start, organizer)

            _seed_ghost(
                meeting_date=today,
                start_time=start,
                workiq_id=wid,
                dismissed=True,
                organizer=organizer,
                subject=subject,
            )
            _stub_workiq(monkeypatch, [
                self._meeting_payload(subject, start, organizer),
            ])

            sync_meetings_for_date(today.strftime('%Y-%m-%d'))

            row = PrefetchedMeeting.query.filter_by(workiq_id=wid).first()
            assert row is not None, "meeting should still exist after re-sync"
            assert row.dismissed is True, "dismissed flag should persist"

    def test_preserves_note_id_when_meeting_returns(
        self, app, clean_meeting_tables, monkeypatch,
    ):
        from app.services.meeting_sync import sync_meetings_for_date
        from app.services.meeting_prefetch import _synthetic_id

        with app.app_context():
            today = date.today()
            start = datetime.combine(today, datetime.min.time()).replace(hour=15)
            organizer = 'returner2@example.com'
            subject = 'Customer Sync'
            wid = _synthetic_id(subject, start, organizer)

            customer = Customer(name='Beta Co', tpid=910202)
            db.session.add(customer)
            db.session.flush()
            note = Note(
                customer_id=customer.id,
                call_date=datetime.combine(today, datetime.min.time()),
                content='x',
            )
            db.session.add(note)
            db.session.flush()
            _seed_ghost(
                meeting_date=today,
                start_time=start,
                workiq_id=wid,
                note_id=note.id,
                customer_id=customer.id,
                organizer=organizer,
                subject=subject,
            )
            _stub_workiq(monkeypatch, [
                self._meeting_payload(subject, start, organizer),
            ])

            sync_meetings_for_date(today.strftime('%Y-%m-%d'))

            row = PrefetchedMeeting.query.filter_by(workiq_id=wid).first()
            assert row is not None
            assert row.note_id == note.id


# ---------------------------------------------------------------------------
# Note creation back-links via from_meeting
# ---------------------------------------------------------------------------

class TestNoteCreationBackLink:
    def test_from_meeting_links_ghost(
        self, app, client, clean_meeting_tables,
    ):
        with app.app_context():
            today = date.today()
            customer = Customer(name='Gamma', tpid=910303)
            db.session.add(customer)
            db.session.flush()
            ghost = _seed_ghost(
                meeting_date=today,
                start_time=datetime.combine(today, datetime.min.time()).replace(hour=9),
                workiq_id='backlink-target',
                customer_id=customer.id,
            )
            ghost_id = ghost.id
            customer_id = customer.id

        resp = client.post('/note/new', data={
            'customer_id': str(customer_id),
            'call_date': today.strftime('%Y-%m-%d'),
            'call_time': '09:00',
            'content': 'Notes from the call.',
            'from_meeting': str(ghost_id),
        }, follow_redirects=False)
        assert resp.status_code in (302, 303), resp.data

        with app.app_context():
            row = db.session.get(PrefetchedMeeting, ghost_id)
            assert row.note_id is not None
            assert row.note_id > 0

    def test_invalid_from_meeting_does_not_break(
        self, app, client, clean_meeting_tables,
    ):
        with app.app_context():
            today = date.today()
            customer = Customer(name='Delta', tpid=910404)
            db.session.add(customer)
            db.session.flush()
            customer_id = customer.id

        resp = client.post('/note/new', data={
            'customer_id': str(customer_id),
            'call_date': today.strftime('%Y-%m-%d'),
            'call_time': '09:00',
            'content': 'Notes ok.',
            'from_meeting': '99999999',
        }, follow_redirects=False)
        assert resp.status_code in (302, 303)

        with app.app_context():
            n = Note.query.filter_by(content='Notes ok.').first()
            assert n is not None
