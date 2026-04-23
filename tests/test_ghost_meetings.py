"""Tests for ghost meeting service + calendar/dismiss endpoints (Phase 3).

Covers:
- ``get_ghost_meetings_for_range`` filters: external-only, not dismissed,
  not promoted to a note, recurring-key dismissed list.
- ``dismiss_ghost`` marks single meetings + recurring series.
- The ``/api/notes/calendar`` API includes ghost meetings under ``ghosts``.
- The ``/api/meetings/ghosts/<id>/dismiss`` endpoint.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models import (
    Customer,
    DismissedRecurringMeeting,
    Note,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    db,
)
from app.services import ghost_meetings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_ghost_tables(app):
    with app.app_context():
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()
        yield
        PrefetchedMeetingAttendee.query.delete()
        PrefetchedMeeting.query.delete()
        DismissedRecurringMeeting.query.delete()
        db.session.commit()


def _seed_meeting(
    *,
    subject: str,
    meeting_date: date,
    start_time: datetime,
    customer_id: int = None,
    is_recurring: bool = False,
    recurring_key: str = None,
    dismissed: bool = False,
    note_id: int = None,
    workiq_id: str = None,
    external_emails: list = None,
    internal_emails: list = None,
) -> PrefetchedMeeting:
    """Insert a PrefetchedMeeting + attendees and return it."""
    m = PrefetchedMeeting(
        workiq_id=workiq_id or f'wid-{subject}-{start_time.isoformat()}',
        subject=subject,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        meeting_date=meeting_date,
        organizer_email='org@example.com',
        is_recurring=is_recurring,
        recurring_key=recurring_key,
        customer_id=customer_id,
        dismissed=dismissed,
        note_id=note_id,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db.session.add(m)
    db.session.flush()
    for email in (external_emails or []):
        db.session.add(PrefetchedMeetingAttendee(
            meeting_id=m.id,
            name=email.split('@')[0],
            email=email,
            domain=email.split('@')[1].lower(),
            is_external=True,
        ))
    for email in (internal_emails or []):
        db.session.add(PrefetchedMeetingAttendee(
            meeting_id=m.id,
            name=email.split('@')[0],
            email=email,
            domain=email.split('@')[1].lower(),
            is_external=False,
        ))
    db.session.commit()
    return m


# ---------------------------------------------------------------------------
# Service: get_ghost_meetings_for_range
# ---------------------------------------------------------------------------

class TestGetGhostMeetingsForRange:
    def test_returns_meeting_with_external_attendee(self, app, clean_ghost_tables):
        with app.app_context():
            d = date(2026, 4, 22)
            _seed_meeting(
                subject='Acme Sync',
                meeting_date=d,
                start_time=datetime(2026, 4, 22, 14, 0),
                external_emails=['x@acme.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(d, d)
            assert 22 in result
            assert len(result[22]) == 1
            assert result[22][0]['subject'] == 'Acme Sync'

    def test_excludes_pure_internal_meeting(self, app, clean_ghost_tables):
        with app.app_context():
            d = date(2026, 4, 22)
            _seed_meeting(
                subject='Team Standup',
                meeting_date=d,
                start_time=datetime(2026, 4, 22, 9, 0),
                internal_emails=['a@microsoft.com', 'b@microsoft.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(d, d)
            assert result == {}

    def test_includes_internal_only_meeting_when_customer_matched(
        self, app, clean_ghost_tables
    ):
        """Customer joined via Teams Room mailbox - no externals visible,
        but subject keyword match resolved a customer. Should still surface."""
        with app.app_context():
            c = Customer(name='Redsail', tpid=920050)
            db.session.add(c)
            db.session.commit()
            _seed_meeting(
                subject='Redsail Sync (Teams Room)',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                customer_id=c.id,
                internal_emails=['me@microsoft.com', 'room@microsoft.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(
                date(2026, 4, 22), date(2026, 4, 22)
            )
            assert 22 in result
            assert result[22][0]['customer_id'] == c.id
            db.session.delete(c)
            db.session.commit()

    def test_excludes_dismissed(self, app, clean_ghost_tables):
        with app.app_context():
            d = date(2026, 4, 22)
            _seed_meeting(
                subject='Hidden',
                meeting_date=d,
                start_time=datetime(2026, 4, 22, 10, 0),
                dismissed=True,
                external_emails=['x@acme.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(d, d)
            assert result == {}

    def test_excludes_promoted_to_note(self, app, clean_ghost_tables):
        with app.app_context():
            # Create a note to attach
            c = Customer(name='Acme', tpid=920001)
            db.session.add(c)
            db.session.commit()
            note = Note(customer_id=c.id, call_date=datetime(2026, 4, 22, 14, 0), content='x')
            db.session.add(note)
            db.session.commit()
            _seed_meeting(
                subject='Promoted Meeting',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                note_id=note.id,
                external_emails=['x@acme.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(
                date(2026, 4, 22), date(2026, 4, 22)
            )
            assert result == {}
            # Cleanup
            db.session.delete(note)
            db.session.delete(c)
            db.session.commit()

    def test_excludes_dismissed_recurring_series(self, app, clean_ghost_tables):
        with app.app_context():
            d = date(2026, 4, 22)
            _seed_meeting(
                subject='Weekly Acme',
                meeting_date=d,
                start_time=datetime(2026, 4, 22, 14, 0),
                is_recurring=True,
                recurring_key='abc123',
                external_emails=['x@acme.com'],
            )
            db.session.add(DismissedRecurringMeeting(recurring_key='abc123'))
            db.session.commit()
            result = ghost_meetings.get_ghost_meetings_for_range(d, d)
            assert result == {}

    def test_groups_by_day_of_month(self, app, clean_ghost_tables):
        with app.app_context():
            _seed_meeting(
                subject='Day 22',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 10, 0),
                external_emails=['x@acme.com'],
            )
            _seed_meeting(
                subject='Day 23',
                meeting_date=date(2026, 4, 23),
                start_time=datetime(2026, 4, 23, 10, 0),
                external_emails=['x@acme.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(
                date(2026, 4, 1), date(2026, 4, 30)
            )
            assert set(result.keys()) == {22, 23}

    def test_includes_customer_name_when_matched(self, app, clean_ghost_tables):
        with app.app_context():
            c = Customer(name='Redsail', tpid=920002)
            db.session.add(c)
            db.session.commit()
            _seed_meeting(
                subject='Redsail Call',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                customer_id=c.id,
                external_emails=['x@redsail.com'],
            )
            result = ghost_meetings.get_ghost_meetings_for_range(
                date(2026, 4, 22), date(2026, 4, 22)
            )
            entry = result[22][0]
            assert entry['customer_id'] == c.id
            assert entry['customer_name'] == 'Redsail'
            db.session.delete(c)
            db.session.commit()


# ---------------------------------------------------------------------------
# Service: dismiss_ghost
# ---------------------------------------------------------------------------

class TestDismissGhost:
    def test_marks_single_meeting_dismissed(self, app, clean_ghost_tables):
        with app.app_context():
            m = _seed_meeting(
                subject='One off',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 10, 0),
                external_emails=['x@acme.com'],
            )
            success, err = ghost_meetings.dismiss_ghost(m.id)
            assert success is True
            assert err is None
            db.session.refresh(m)
            assert m.dismissed is True
            # No recurring entry created for non-recurring
            assert DismissedRecurringMeeting.query.count() == 0

    def test_returns_error_for_unknown_id(self, app, clean_ghost_tables):
        with app.app_context():
            success, err = ghost_meetings.dismiss_ghost(999999)
            assert success is False
            assert err == 'Meeting not found'

    def test_recurring_dismissal_records_key_and_siblings(self, app, clean_ghost_tables):
        with app.app_context():
            m1 = _seed_meeting(
                subject='Weekly',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                is_recurring=True,
                recurring_key='wk-key',
                external_emails=['x@acme.com'],
                workiq_id='wid-1',
            )
            m2 = _seed_meeting(
                subject='Weekly',
                meeting_date=date(2026, 4, 29),
                start_time=datetime(2026, 4, 29, 14, 0),
                is_recurring=True,
                recurring_key='wk-key',
                external_emails=['x@acme.com'],
                workiq_id='wid-2',
            )
            success, err = ghost_meetings.dismiss_ghost(m1.id, dismiss_series=True)
            assert success is True

            # Both meetings dismissed
            db.session.refresh(m1)
            db.session.refresh(m2)
            assert m1.dismissed is True
            assert m2.dismissed is True

            # Series recorded for future prefetches
            assert DismissedRecurringMeeting.query.get('wk-key') is not None

    def test_single_dismissal_on_recurring_does_not_kill_series(self, app, clean_ghost_tables):
        """Default behavior: dismiss only this occurrence, leave siblings alone."""
        with app.app_context():
            m1 = _seed_meeting(
                subject='Weekly',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                is_recurring=True,
                recurring_key='wk-key2',
                external_emails=['x@acme.com'],
                workiq_id='wid-a',
            )
            m2 = _seed_meeting(
                subject='Weekly',
                meeting_date=date(2026, 4, 29),
                start_time=datetime(2026, 4, 29, 14, 0),
                is_recurring=True,
                recurring_key='wk-key2',
                external_emails=['x@acme.com'],
                workiq_id='wid-b',
            )
            success, _ = ghost_meetings.dismiss_ghost(m1.id)  # default: series=False
            assert success is True
            db.session.refresh(m1)
            db.session.refresh(m2)
            assert m1.dismissed is True
            assert m2.dismissed is False  # sibling untouched
            assert DismissedRecurringMeeting.query.get('wk-key2') is None


# ---------------------------------------------------------------------------
# API: /api/notes/calendar (ghosts under .ghosts key)
# ---------------------------------------------------------------------------

class TestCalendarApiGhosts:
    def test_calendar_payload_includes_ghosts(self, app, client, clean_ghost_tables):
        with app.app_context():
            _seed_meeting(
                subject='Acme Call',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                external_emails=['x@acme.com'],
            )
        resp = client.get('/api/notes/calendar?year=2026&month=4')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'ghosts' in data
        assert '22' in data['ghosts'] or 22 in data['ghosts']
        # JSON keys come back as strings; jsonify stringifies dict int keys.
        ghosts_22 = data['ghosts'].get('22') or data['ghosts'].get(22)
        assert ghosts_22 is not None
        assert ghosts_22[0]['subject'] == 'Acme Call'


# ---------------------------------------------------------------------------
# API: /api/meetings/ghosts/<id>/dismiss
# ---------------------------------------------------------------------------

class TestDismissGhostApi:
    def test_dismiss_endpoint_marks_dismissed(self, app, client, clean_ghost_tables):
        with app.app_context():
            m = _seed_meeting(
                subject='Acme',
                meeting_date=date(2026, 4, 22),
                start_time=datetime(2026, 4, 22, 14, 0),
                external_emails=['x@acme.com'],
            )
            mid = m.id
        resp = client.post(f'/api/meetings/ghosts/{mid}/dismiss')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        with app.app_context():
            m = db.session.get(PrefetchedMeeting, mid)
            assert m.dismissed is True

    def test_dismiss_endpoint_404_for_unknown(self, app, client, clean_ghost_tables):
        resp = client.post('/api/meetings/ghosts/999999/dismiss')
        assert resp.status_code == 404
        assert resp.get_json()['success'] is False
