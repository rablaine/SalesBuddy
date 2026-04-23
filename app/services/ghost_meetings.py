"""Ghost meeting query helpers for the home-page Activities calendar.

Phase 3 of ``docs/PREFETCH_MEETINGS_BACKLOG.md``. Reads ``PrefetchedMeeting``
rows and shapes them for the calendar UI:

- only meetings with at least one external attendee (skip pure-internal
  team standups, all-hands, etc.)
- exclude already-dismissed meetings
- exclude meetings already promoted to a real ``Note`` (``note_id`` set)
- exclude meetings whose ``recurring_key`` was previously dismissed via
  ``DismissedRecurringMeeting``

The calendar JS calls a single endpoint per month and renders ghosts
alongside the real notes already displayed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import joinedload

from app.models import (
    db,
    DismissedRecurringMeeting,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
)

logger = logging.getLogger(__name__)


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Render a naive UTC datetime as an ISO 8601 string with explicit Z.

    Per repo datetime conventions, server-stored datetimes are naive UTC.
    The calendar JS does the UTC→local conversion at render time, so we
    must mark the timestamp as UTC explicitly (``Z``) rather than letting
    the client guess at local time.
    """
    if dt is None:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _meeting_to_dict(
    meeting: PrefetchedMeeting,
    external_attendees: List[PrefetchedMeetingAttendee],
) -> Dict[str, Any]:
    """Shape a meeting + its external attendees for the calendar payload."""
    return {
        'id': meeting.id,
        'subject': meeting.subject,
        'start_time_utc': _iso_utc(meeting.start_time),
        'meeting_date': meeting.meeting_date.isoformat() if meeting.meeting_date else None,
        'customer_id': meeting.customer_id,
        'customer_name': meeting.customer.get_display_name() if meeting.customer else None,
        'is_recurring': meeting.is_recurring,
        'recurring_key': meeting.recurring_key,
        'external_attendees': [
            {
                'name': a.name,
                'email': a.email,
                'domain': a.domain,
            }
            for a in external_attendees
        ],
        'attendee_count': len(external_attendees),
    }


def get_ghost_meetings_for_range(
    start: date,
    end: date,
) -> Dict[int, List[Dict[str, Any]]]:
    """Return ghost meetings grouped by day-of-month for a date range.

    Args:
        start: inclusive lower bound on ``meeting_date``
        end: inclusive upper bound on ``meeting_date``

    Returns:
        Dict mapping day-of-month (int) -> list of meeting dicts. Only days
        with at least one ghost are present. Meetings within the same day
        are ordered by ``start_time``.
    """
    # Pull dismissed recurring keys once; tiny table, almost always small.
    dismissed_keys = {
        row.recurring_key
        for row in DismissedRecurringMeeting.query.all()
    }

    meetings = (
        PrefetchedMeeting.query
        .options(
            joinedload(PrefetchedMeeting.attendees),
            joinedload(PrefetchedMeeting.customer),
        )
        .filter(PrefetchedMeeting.meeting_date >= start)
        .filter(PrefetchedMeeting.meeting_date <= end)
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .filter(PrefetchedMeeting.note_id.is_(None))
        .order_by(PrefetchedMeeting.start_time)
        .all()
    )

    days: Dict[int, List[Dict[str, Any]]] = {}
    for m in meetings:
        if m.recurring_key and m.recurring_key in dismissed_keys:
            continue
        external = [a for a in m.attendees if a.is_external]
        # Surface the meeting if EITHER (a) we matched a customer (by domain
        # or by subject keyword), OR (b) at least one external attendee
        # showed up. This catches the common case where the customer joined
        # via a Teams Room mailbox or a distribution list (no externals
        # visible) but the subject contains a customer nickname.
        if not external and m.customer_id is None:
            continue
        day = m.meeting_date.day
        days.setdefault(day, []).append(_meeting_to_dict(m, external))
    return days


def dismiss_ghost(
    meeting_id: int,
    dismiss_series: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Mark a ghost meeting as dismissed.

    Args:
        meeting_id: PrefetchedMeeting id to dismiss.
        dismiss_series: If True AND the meeting is recurring, also store
            the ``recurring_key`` in ``DismissedRecurringMeeting`` and
            dismiss every sibling row in the cache. If False (default),
            only this single occurrence is dismissed.

    Returns ``(success, error_message)``.
    """
    meeting = PrefetchedMeeting.query.get(meeting_id)
    if meeting is None:
        return False, 'Meeting not found'

    meeting.dismissed = True

    if dismiss_series and meeting.is_recurring and meeting.recurring_key:
        existing = DismissedRecurringMeeting.query.get(meeting.recurring_key)
        if existing is None:
            db.session.add(DismissedRecurringMeeting(
                recurring_key=meeting.recurring_key,
            ))
        # Also mark sibling rows (other days of same series already in cache)
        # as dismissed so the calendar reflects it without waiting for the
        # next prefetch cycle.
        siblings = (
            PrefetchedMeeting.query
            .filter(PrefetchedMeeting.recurring_key == meeting.recurring_key)
            .filter(PrefetchedMeeting.id != meeting.id)
            .all()
        )
        for s in siblings:
            s.dismissed = True

    db.session.commit()
    return True, None
