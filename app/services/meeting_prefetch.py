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


def _is_internal_domain(domain: Optional[str]) -> bool:
    """True if a domain belongs to Microsoft.

    Catches both ``microsoft.com`` and any subdomain like
    ``service.microsoft.com`` (FastTrack/CAT shared mailboxes,
    Garage rooms) or ``expansion.microsoft.com``. These are all
    internal resource accounts and should not be treated as external
    customer/partner attendees.
    """
    if not domain:
        return False
    d = domain.lower().strip()
    return d == INTERNAL_DOMAIN or d.endswith('.' + INTERNAL_DOMAIN)

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


# Words that are too generic to match a customer on. If a customer name OR
# nickname is *only* these words after tokenization, we skip subject matching
# for it (domain matching still works). Lowercase comparison.
_SUBJECT_STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'of', 'for', 'inc', 'llc', 'ltd', 'corp',
    'corporation', 'company', 'co', 'group', 'holdings', 'systems',
    'solutions', 'technologies', 'tech', 'services', 'data', 'global',
    'international', 'us', 'usa', 'na', 'north', 'america',
})

# Tokens that are real English adjectives, common brand words, or short
# acronyms that would collide with non-customer meeting titles if used as
# a standalone first-token matcher. A customer whose first distinctive
# token is in this set falls back to domain-only / full-name matching.
# "American Express" won't match a meeting titled "American manufacturing
# playbook"; "Apple Inc" won't match "Apple vs Microsoft privacy talk".
_GENERIC_FIRST_TOKENS = frozenset({
    # Common English adjectives that also appear in company names.
    'american', 'national', 'general', 'united', 'global', 'pacific',
    'atlantic', 'premier', 'advanced', 'digital', 'modern', 'central',
    'southern', 'northern', 'eastern', 'western', 'first', 'prime',
    'core', 'alpha', 'beta', 'home', 'federal', 'state', 'city',
    'metro', 'regional', 'local', 'new', 'old', 'big', 'best',
    'true', 'pure', 'smart', 'simple', 'open', 'free', 'direct',
    # Consumer-brand first tokens that show up in generic meeting titles.
    'apple', 'delta', 'uber', 'oracle', 'amazon', 'google', 'meta',
    'microsoft', 'msft', 'azure', 'aws', 'gcp',
})

# Tokenize on any non-word run (so "QS/1", "AT&T", "Coca-Cola" all split
# cleanly). We keep digits because some customer names are numeric ("3M").
_TOKEN_RE = re.compile(r'\w+', re.UNICODE)


def _phrase_is_distinctive(phrase: str) -> bool:
    """True if a phrase has at least one non-stopword, non-trivial token.

    Used to gate which customer name / nickname strings are safe to match
    against meeting subjects. Prevents "Data" alone from matching every
    meeting with the word "data" in the title.
    """
    tokens = [t.lower() for t in _TOKEN_RE.findall(phrase)]
    meaningful = [t for t in tokens if len(t) >= 2 and t not in _SUBJECT_STOPWORDS]
    return bool(meaningful)


def _first_distinctive_token(phrase: str) -> Optional[str]:
    """Return the first token that's safe to use as a standalone matcher.

    Skips leading stopwords ("The", "A", corporate suffixes) and returns
    the original-case token if it passes the standalone-safety rules:

      * all-uppercase AND >= 3 chars (e.g. "AWP", "IBM", "BMW"), OR
      * mixed/lowercase AND >= 4 chars AND not in _GENERIC_FIRST_TOKENS
        (e.g. "Raptor", "Tesla", but NOT "American", "Delta", "Apple").

    Returns ``None`` if no token qualifies; the caller should skip
    registering a first-token matcher for that phrase.
    """
    # Preserve original case so we can check all-uppercase-ness below,
    # but still split on non-word runs consistently.
    raw_tokens = _TOKEN_RE.findall(phrase)
    for tok in raw_tokens:
        low = tok.lower()
        if len(low) < 2 or low in _SUBJECT_STOPWORDS:
            continue
        # First non-stopword token. Decide if it's safe to use standalone.
        if tok.isupper() and len(tok) >= 3:
            return tok
        if len(tok) >= 4 and low not in _GENERIC_FIRST_TOKENS:
            return tok
        # First distinctive token exists but isn't safe; don't fall
        # through to later tokens (that would drift too far from the
        # customer's actual name).
        return None
    return None


def _build_subject_matchers() -> List[Tuple[re.Pattern, int, str]]:
    """Build ``(compiled_regex, customer_id, matched_via)`` list.

    Two tiers, returned as a single list (caller picks the longest match,
    so the full-name tier wins whenever it can):

      1. Full nickname / legal name word-bounded regex. Most specific.
      2. First-distinctive-token fallback (see :func:`_first_distinctive_token`
         and :func:`_build_first_token_matchers`) so titles like
         "AWP & MSFT sync" resolve to "AWP Inc" without needing the
         legal suffix. Ambiguous tokens (claimed by >1 customer) are
         dropped.
    """
    matchers: List[Tuple[re.Pattern, int, str]] = []
    customers = (
        Customer.query
        .order_by(Customer.created_at.desc().nullslast())
        .all()
    )
    seen_phrases: set = set()
    for c in customers:
        for phrase, label in ((c.nickname, 'subject_nickname'),
                              (c.name, 'subject_name')):
            if not phrase:
                continue
            phrase = phrase.strip()
            if not phrase or not _phrase_is_distinctive(phrase):
                continue
            key = (phrase.lower(), c.id)
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            # Word-bounded, case-insensitive. Escape to handle "QS/1", "AT&T".
            try:
                pattern = re.compile(
                    r'(?<!\w)' + re.escape(phrase) + r'(?!\w)',
                    re.IGNORECASE,
                )
            except re.error:
                continue
            matchers.append((pattern, c.id, label))

    matchers.extend(_build_first_token_matchers())
    return matchers


def _build_first_token_matchers() -> List[Tuple[re.Pattern, int, str]]:
    """Build fallback ``(regex, customer_id, matched_via)`` list keyed on
    each customer's first distinctive token.

    Used as a lower-priority tier after full-name / nickname matching so
    meetings like "AWP & MSFT sync" or "Raptor/MSFT review" resolve to
    "AWP Inc" and "Raptor Technologies, LLC" respectively, without
    requiring the full legal suffix in the title.

    A token registered by two or more customers is ambiguous -- drop it
    entirely so we don't silently pick the wrong one.
    """
    # token.lower() -> list of (customer_id, original_token, source_phrase_label)
    token_owners: Dict[str, List[Tuple[int, str, str]]] = {}
    customers = (
        Customer.query
        .order_by(Customer.created_at.desc().nullslast())
        .all()
    )
    for c in customers:
        for phrase, label in ((c.nickname, 'subject_first_token_nickname'),
                              (c.name, 'subject_first_token_name')):
            if not phrase:
                continue
            tok = _first_distinctive_token(phrase)
            if not tok:
                continue
            token_owners.setdefault(tok.lower(), []).append((c.id, tok, label))

    matchers: List[Tuple[re.Pattern, int, str]] = []
    for low, owners in token_owners.items():
        # Ambiguous: same token claimed by multiple customers. Skip.
        unique_cids = {cid for cid, _, _ in owners}
        if len(unique_cids) > 1:
            continue
        cid, tok, label = owners[0]
        try:
            pattern = re.compile(
                r'(?<!\w)' + re.escape(tok) + r'(?!\w)',
                re.IGNORECASE,
            )
        except re.error:
            continue
        matchers.append((pattern, cid, label))
    return matchers


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
            if domain and not _is_internal_domain(domain) and domain not in domain_map:
                domain_map[domain] = (c.id, 'contact_email')

    return domain_map


def _resolve_customer(
    attendees: List[Dict[str, Any]],
    domain_map: Dict[str, Tuple[int, str]],
    subject: str = '',
    subject_matchers: Optional[List[Tuple[re.Pattern, int, str]]] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Resolve a meeting to a known customer.

    Order of precedence:
    1. External attendee domain matches a customer website / contact email.
    2. Subject contains a customer's nickname or full name (word-bounded).
       Longest matching phrase wins, so "Redsail" beats "Acme" if a meeting
       title is "Redsail Acme cross-sell".

    Returns ``(customer_id, matched_via)`` or ``(None, None)``.
    """
    seen: List[str] = []
    for att in attendees:
        email = (att.get('email') or '').strip().lower()
        if not email or '@' not in email:
            continue
        domain = email.split('@', 1)[1]
        if _is_internal_domain(domain) or domain in seen:
            continue
        seen.append(domain)
        if domain in domain_map:
            customer_id, matched_via = domain_map[domain]
            return customer_id, matched_via

    # Subject-based fallback. Picks the longest matched phrase for specificity.
    if subject and subject_matchers:
        best_len = 0
        best: Optional[Tuple[int, str]] = None
        for pattern, cid, label in subject_matchers:
            m = pattern.search(subject)
            if m and (m.end() - m.start()) > best_len:
                best_len = m.end() - m.start()
                best = (cid, label)
        if best is not None:
            return best

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
    subject_matchers: Optional[List[Tuple[re.Pattern, int, str]]] = None,
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

    customer_id, matched_via = _resolve_customer(
        raw_attendees, domain_map,
        subject=subject, subject_matchers=subject_matchers,
    )

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
        is_external = bool(domain and not _is_internal_domain(domain))
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
    stored, _, err = prefetch_for_date_full(date_str)
    return stored, err


def prefetch_for_date_full(
    date_str: str,
) -> Tuple[int, List[Dict[str, Any]], Optional[str]]:
    """Same as ``prefetch_for_date`` but also returns the picker-shaped list.

    The picker list is what the meeting-selection modal expects; returning
    it here lets the legacy DailyMeetingCache be populated from the SAME
    WorkIQ call instead of making a second one.
    """
    from app.services.workiq_service import query_workiq

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError as exc:
        return 0, [], f"Bad date: {exc}"

    purge_expired()

    prompt = _build_prompt(date_str)
    logger.info("Prefetch: querying WorkIQ for %s", date_str)
    try:
        # Today JSON observed at 131s in Phase 0 probe; give 240s headroom.
        response = query_workiq(prompt, timeout=240, operation='meeting_list')
    except Exception as exc:  # noqa: BLE001
        logger.error("Prefetch: WorkIQ call failed for %s: %s", date_str, exc)
        return 0, [], str(exc)

    raw_meetings = _extract_json_array(response)
    if not raw_meetings:
        logger.warning("Prefetch: no meetings parsed for %s", date_str)
        return 0, [], None

    domain_map = _build_domain_map()
    subject_matchers = _build_subject_matchers()
    stored_meetings: List[PrefetchedMeeting] = []
    for raw in raw_meetings:
        if not isinstance(raw, dict):
            continue
        meeting = _upsert_meeting(raw, target_date, domain_map, subject_matchers)
        if meeting is not None:
            stored_meetings.append(meeting)

    db.session.commit()
    picker_list = [_to_picker_dict(m) for m in stored_meetings]
    logger.info("Prefetch: stored %d meetings for %s",
                len(stored_meetings), date_str)
    return len(stored_meetings), picker_list, None


def _to_picker_dict(meeting: PrefetchedMeeting) -> Dict[str, Any]:
    """Convert a PrefetchedMeeting into the meeting-picker payload shape.

    Mirrors what the legacy markdown-table sync produced so the
    DailyMeetingCache and the /api/meetings response stay backwards
    compatible. ``customer`` is the matched customer name when known,
    otherwise the first external attendee's domain (best-effort hint for
    the fuzzy customer-name matcher).
    """
    if meeting.customer is not None:
        customer_display = meeting.customer.name
    else:
        customer_display = ''
        for att in meeting.attendees:
            if att.is_external and att.domain:
                customer_display = att.domain
                break

    start_local = meeting.start_time
    return {
        'id': meeting.workiq_id,
        'title': meeting.subject,
        'start_time': start_local.isoformat() if start_local else None,
        'start_time_display': (
            start_local.strftime('%I:%M %p') if start_local else ''
        ),
        'customer': customer_display,
        'attendees': [
            {'name': a.name, 'email': a.email}
            for a in meeting.attendees
        ],
    }


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
