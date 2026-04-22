"""Meeting prefetch service.

Phase 1 of ``docs/PREFETCH_MEETINGS_BACKLOG.md``.

Pulls today's meetings *with attendees* from WorkIQ as JSON each morning,
stores them in ``PrefetchedMeeting`` / ``PrefetchedMeetingAttendee`` rows,
and resolves each meeting to a known customer via domain matching. The
note-form attendee scrape can then read attendees from the cache instead
of firing a live WorkIQ call mid-call.

Per Phase 0 findings:
- WorkIQ does NOT expose Graph eventIds, so ``workiq_id`` is a synthetic
  hash of (subject, start_time, organizer_email).
- WorkIQ does NOT expose per-attendee response_status; the column is kept
  but always null for now.
- WorkIQ does NOT expose the recurrence rule, only an ``is_recurring``
  boolean. ``recurring_key`` is hashed from subject+organizer for
  per-series dismissal in Phase 3.
- A 7-day JSON pull silently truncates; this service only handles a single
  day at a time. Week-ahead chunking is Phase 4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, date, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import joinedload

from app.models import (
    db,
    Customer,
    CustomerContact,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    utc_now,
)

logger = logging.getLogger(__name__)

INTERNAL_DOMAIN = 'microsoft.com'

# Match a JSON array spanning the whole match. Non-greedy so we stop at the
# first balanced ``]``. WorkIQ wraps the array with prose preamble + tail
# offer-suggestions, so we can't just ``json.loads(response)``.
_JSON_ARRAY_RE = re.compile(r'\[\s*\{[\s\S]*\}\s*\]')

# Match the legacy X.500 / Exchange DN format WorkIQ sometimes returns for
# self-organized events instead of a clean SMTP address. We treat these as
# "unknown organizer" rather than trying to parse the CN out.
_X500_RE = re.compile(r'^/O=', re.IGNORECASE)


# ---------------------------------------------------------------------------
# WorkIQ JSON pull
# ---------------------------------------------------------------------------

def _build_prompt(date_str: str) -> str:
    return (
        f"List every meeting on my calendar for {date_str}. For each "
        f"meeting return a JSON object inside a ```json code block with "
        f"these fields: subject, start_time (ISO 8601 with timezone), "
        f"end_time, organizer_email, is_recurring, and attendees (an array "
        f"of objects with name and email). Wrap the whole list in a single "
        f"JSON array. Do not omit any meeting, even internal ones."
    )


def _extract_json_array(response: str) -> List[Dict[str, Any]]:
    """Pull the JSON meeting array out of a WorkIQ prose response.

    Returns an empty list if no parseable array is found.
    """
    if not response:
        return []
    match = _JSON_ARRAY_RE.search(response)
    if not match:
        logger.warning("Prefetch: no JSON array found in WorkIQ response")
        return []
    raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Prefetch: JSON parse failed: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("Prefetch: parsed JSON is not a list")
        return []
    return data


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into a naive UTC datetime.

    Returns ``None`` if the input is missing or unparseable. We normalize to
    naive UTC because SQLite stores datetimes as text strings without
    timezone info (per repo datetime conventions).
    """
    if not value or not isinstance(value, str):
        return None
    try:
        # ``fromisoformat`` handles offsets like ``-05:00`` natively in 3.11+.
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _normalize_organizer(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if _X500_RE.match(value):
        return None
    return value.lower()


def _synthetic_id(subject: str, start_time: Optional[datetime],
                  organizer: Optional[str]) -> str:
    """Stable hash key for upserts. WorkIQ doesn't expose Graph eventId."""
    parts = [
        (subject or '').strip().lower(),
        start_time.isoformat() if start_time else '',
        organizer or '',
    ]
    raw = '|'.join(parts).encode('utf-8')
    return hashlib.sha1(raw).hexdigest()


def _recurring_key(subject: str, organizer: Optional[str]) -> str:
    raw = f"{(subject or '').strip().lower()}|{organizer or ''}".encode('utf-8')
    return hashlib.sha1(raw).hexdigest()


# ---------------------------------------------------------------------------
# Customer matching
# ---------------------------------------------------------------------------

def _customer_domain(customer: Customer) -> Optional[str]:
    """Extract a clean domain from a customer's website."""
    if not customer.website:
        return None
    d = customer.website.lower().strip()
    d = d.replace('https://', '').replace('http://', '')
    d = d.replace('www.', '').split('/', 1)[0]
    return d or None


def _build_domain_map() -> Dict[str, Tuple[int, str]]:
    """Build ``{domain: (customer_id, matched_via)}`` for fast lookup.

    Iterates customers in updated_at DESC so the most-recently-touched
    customer wins on collisions (proxy for "active").
    """
    domain_map: Dict[str, Tuple[int, str]] = {}

    customers = (
        Customer.query
        .options(joinedload(Customer.contacts))
        .order_by(Customer.created_at.desc().nullslast())
        .all()
    )

    # Pass 1: website match (preferred, more authoritative).
    for c in customers:
        d = _customer_domain(c)
        if d and d not in domain_map:
            domain_map[d] = (c.id, 'website')

    # Pass 2: contact-email domains. Only fill gaps; don't overwrite website.
    for c in customers:
        for contact in c.contacts:
            if not contact.email or '@' not in contact.email:
                continue
            domain = contact.email.split('@', 1)[1].lower().strip()
            if domain and domain != INTERNAL_DOMAIN and domain not in domain_map:
                domain_map[domain] = (c.id, 'contact_email')

    return domain_map


def _resolve_customer(
    attendees: List[Dict[str, Any]],
    domain_map: Dict[str, Tuple[int, str]],
) -> Tuple[Optional[int], Optional[str]]:
    """Pick the first external attendee domain that matches a known customer.

    Returns ``(customer_id, matched_via)`` or ``(None, None)``.
    """
    seen: List[str] = []
    for att in attendees:
        email = (att.get('email') or '').strip().lower()
        if not email or '@' not in email:
            continue
        domain = email.split('@', 1)[1]
        if domain == INTERNAL_DOMAIN or domain in seen:
            continue
        seen.append(domain)
        if domain in domain_map:
            customer_id, matched_via = domain_map[domain]
            return customer_id, matched_via
    return None, None


# ---------------------------------------------------------------------------
# Upsert + purge
# ---------------------------------------------------------------------------

def _expires_at_for(meeting_date: date) -> datetime:
    """Meetings live until end-of-day local on the day after they occur."""
    return datetime.combine(
        meeting_date + timedelta(days=1),
        time(23, 59, 59),
    )


def _upsert_meeting(
    raw: Dict[str, Any],
    target_date: date,
    domain_map: Dict[str, Tuple[int, str]],
) -> Optional[PrefetchedMeeting]:
    """Create or update a PrefetchedMeeting from one raw WorkIQ JSON item."""
    subject = (raw.get('subject') or '').strip()
    start_time = _parse_dt(raw.get('start_time'))
    end_time = _parse_dt(raw.get('end_time'))
    organizer = _normalize_organizer(raw.get('organizer_email'))
    is_recurring = bool(raw.get('is_recurring'))

    if not subject or not start_time:
        logger.debug("Prefetch: skipping meeting with no subject or start_time")
        return None

    workiq_id = _synthetic_id(subject, start_time, organizer)
    raw_attendees = raw.get('attendees') or []
    if not isinstance(raw_attendees, list):
        raw_attendees = []

    customer_id, matched_via = _resolve_customer(raw_attendees, domain_map)

    existing = PrefetchedMeeting.query.filter_by(workiq_id=workiq_id).first()
    if existing:
        existing.subject = subject
        existing.start_time = start_time
        existing.end_time = end_time
        existing.meeting_date = target_date
        existing.organizer_email = organizer
        existing.is_recurring = is_recurring
        existing.recurring_key = _recurring_key(subject, organizer) if is_recurring else None
        existing.fetched_at = utc_now()
        existing.expires_at = _expires_at_for(target_date)
        # Preserve dismissed + note_id; only refresh customer match if it
        # was previously unmatched OR if the new resolution found one.
        if customer_id is not None:
            existing.customer_id = customer_id
            existing.matched_via = matched_via
        # Replace attendees wholesale (cascade=delete-orphan handles it).
        existing.attendees = []
        db.session.flush()
        meeting = existing
    else:
        meeting = PrefetchedMeeting(
            workiq_id=workiq_id,
            subject=subject,
            start_time=start_time,
            end_time=end_time,
            meeting_date=target_date,
            organizer_email=organizer,
            is_recurring=is_recurring,
            recurring_key=_recurring_key(subject, organizer) if is_recurring else None,
            customer_id=customer_id,
            matched_via=matched_via,
            expires_at=_expires_at_for(target_date),
        )
        db.session.add(meeting)
        db.session.flush()

    seen_emails: set = set()
    for att in raw_attendees:
        if not isinstance(att, dict):
            continue
        name = (att.get('name') or '').strip() or None
        email = (att.get('email') or '').strip().lower() or None
        if email and email in seen_emails:
            continue
        if email:
            seen_emails.add(email)
        domain = email.split('@', 1)[1] if email and '@' in email else None
        is_external = bool(domain and domain != INTERNAL_DOMAIN)
        db.session.add(PrefetchedMeetingAttendee(
            meeting=meeting,
            name=name,
            email=email,
            domain=domain,
            is_external=is_external,
            response_status=None,
        ))

    return meeting


def purge_expired() -> int:
    """Delete meetings past ``expires_at``. Returns rows deleted."""
    now = datetime.now()
    expired = PrefetchedMeeting.query.filter(
        PrefetchedMeeting.expires_at < now
    ).all()
    count = len(expired)
    for m in expired:
        db.session.delete(m)
    if count:
        db.session.commit()
        logger.info("Prefetch: purged %d expired meetings", count)
    return count


def prefetch_for_date(date_str: str) -> Tuple[int, Optional[str]]:
    """Pull WorkIQ meetings + attendees for a date and upsert them.

    Args:
        date_str: ``YYYY-MM-DD``.

    Returns:
        ``(meetings_stored, error_or_none)``.
    """
    from app.services.workiq_service import query_workiq

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError as exc:
        return 0, f"Bad date: {exc}"

    purge_expired()

    prompt = _build_prompt(date_str)
    logger.info("Prefetch: querying WorkIQ for %s", date_str)
    try:
        # Today JSON observed at 131s in Phase 0 probe; give 240s headroom.
        response = query_workiq(prompt, timeout=240, operation='meeting_list')
    except Exception as exc:  # noqa: BLE001
        logger.error("Prefetch: WorkIQ call failed for %s: %s", date_str, exc)
        return 0, str(exc)

    raw_meetings = _extract_json_array(response)
    if not raw_meetings:
        logger.warning("Prefetch: no meetings parsed for %s", date_str)
        return 0, None

    domain_map = _build_domain_map()
    stored = 0
    for raw in raw_meetings:
        if not isinstance(raw, dict):
            continue
        meeting = _upsert_meeting(raw, target_date, domain_map)
        if meeting is not None:
            stored += 1

    db.session.commit()
    logger.info("Prefetch: stored %d meetings for %s", stored, date_str)
    return stored, None


# ---------------------------------------------------------------------------
# Lookup helpers (used by the note-form attendee scrape path)
# ---------------------------------------------------------------------------

def _normalize_subject(subject: str) -> str:
    """Lowercase + collapse whitespace for fuzzy subject comparison."""
    return re.sub(r'\s+', ' ', (subject or '').strip().lower())


def find_prefetched_meeting(
    meeting_title: str,
    meeting_date: str,
) -> Optional[PrefetchedMeeting]:
    """Look up a cached meeting by approximate title + date.

    Strategy: exact (case-insensitive, whitespace-collapsed) match first;
    fall back to ``startswith`` on either side. Returns ``None`` if no
    confident match.
    """
    try:
        target_date = datetime.strptime(meeting_date, '%Y-%m-%d').date()
    except ValueError:
        return None

    candidates = (
        PrefetchedMeeting.query
        .options(joinedload(PrefetchedMeeting.attendees))
        .filter_by(meeting_date=target_date)
        .all()
    )
    if not candidates:
        return None

    needle = _normalize_subject(meeting_title)
    if not needle:
        return None

    # Exact match.
    for m in candidates:
        if _normalize_subject(m.subject) == needle:
            return m

    # Prefix match either direction.
    for m in candidates:
        cand = _normalize_subject(m.subject)
        if cand.startswith(needle) or needle.startswith(cand):
            return m

    return None


def get_cached_attendees(
    meeting_title: str,
    meeting_date: str,
) -> Optional[List[Dict[str, Any]]]:
    """Return cached attendees in the same shape WorkIQ ``ask`` produces.

    Each dict has ``name``, ``email``, and ``title`` (always ``None`` from
    the cache because the morning JSON pull doesn't include transcript-
    derived titles). Returns ``None`` if no cached meeting matched, so the
    caller knows to fall back to the live WorkIQ scrape.
    """
    meeting = find_prefetched_meeting(meeting_title, meeting_date)
    if meeting is None:
        return None
    return [
        {'name': a.name, 'email': a.email, 'title': None}
        for a in meeting.attendees
    ]
