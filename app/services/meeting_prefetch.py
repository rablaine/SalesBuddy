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
MAX_MEETING_ATTENDEES = 15


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


# Meeting subjects matching any of these patterns are internal / segment
# noise that must never be synced as customer meetings, even when their
# title happens to contain a customer's name as a substring. The canonical
# case is Microsoft's "SME&C" (Small, Medium & Corporate) segment, which
# floods calendars with kickoffs and collides with a customer literally
# named "SME". Blacklisted meetings are dropped wholesale: no ghost, no
# picker entry, no customer resolution.
_SUBJECT_BLACKLIST = [
    re.compile(r'^\s*Canceled\s*:', re.IGNORECASE),
    re.compile(r'\b(?:OOO|OOF)\b', re.IGNORECASE),
    re.compile(r'\bOut\s+of\s+Office\b', re.IGNORECASE),
    re.compile(r'\bFY27\s+CAIP\b', re.IGNORECASE),
    re.compile(r'\bFY27\s+Partner\b', re.IGNORECASE),
    re.compile(r'\bMCAPS\b', re.IGNORECASE),
    re.compile(r'\bSME\s*&\s*C\b', re.IGNORECASE),
    re.compile(r'\bOffice\s+Hours\b', re.IGNORECASE),
]


def _is_blacklisted_subject(subject: Optional[str]) -> bool:
    """True if a meeting subject matches the hard sync blacklist.

    See :data:`_SUBJECT_BLACKLIST`. Blacklisted meetings are skipped during
    upsert and scrubbed from both caches by :func:`purge_blacklisted`.
    """
    if not subject:
        return False
    return any(pat.search(subject) for pat in _SUBJECT_BLACKLIST)


# ---------------------------------------------------------------------------
# WorkIQ JSON pull
# ---------------------------------------------------------------------------

def _build_prompt(date_str: str) -> str:
    return (
        f"List every meeting on my calendar for {date_str}. Return one JSON "
        f"array inside a single ```json code block. Each meeting object must "
        f"contain: subject, "
        f"start_time (ISO 8601 with timezone), end_time, organizer_email, "
        f"is_recurring, and attendees. Return no more than "
        f"{MAX_MEETING_ATTENDEES} attendees per meeting. For meetings over "
        f"{MAX_MEETING_ATTENDEES} attendees, choose external attendees first, "
        f"then the organizer, then other attendees. The attendees array must "
        f"include the organizer when the organizer is an attendee. Every "
        f"attendee object must contain name and a non-null email address; do "
        f"not return attendee entries without an email address. Include "
        f"meetings with no qualifying attendees using an empty array. Do not "
        f"omit any meeting. Do not include prose outside the code block."
    )


def _extract_json_array(response: str) -> List[Dict[str, Any]]:
    """Pull the JSON meeting array out of a WorkIQ prose response.

    Raises ``ValueError`` if no parseable array is found OR the JSON is
    malformed. This is intentional: callers must distinguish "WorkIQ
    failed / returned garbage" (preserve existing cached meetings, surface
    error to UI) from "WorkIQ returned a valid but empty list" (wipe
    cache). An empty list isn't a realistic WorkIQ response - it returns
    prose like "No meetings found" - so any non-parseable response is
    treated as a sync failure rather than silently wiping the day.
    """
    if not response:
        raise ValueError("empty WorkIQ response")
    from app.services.workiq_service import repair_json_control_chars
    match = _JSON_ARRAY_RE.search(response)
    if not match:
        raise ValueError("no JSON array found in WorkIQ response")
    # WorkIQ sometimes emits raw newline bytes inside JSON string values
    # (e.g. an attendee name split across lines). Escape them so json.loads
    # doesn't fail with "Invalid control character".
    raw = repair_json_control_chars(match.group(0))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse failed: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("parsed JSON is not a list")
    aliases = {
        'starttime': 'start_time',
        'endtime': 'end_time',
        'organizeremail': 'organizer_email',
        'isrecurring': 'is_recurring',
        'externalcompany': 'external_company',
    }
    for item in data:
        if not isinstance(item, dict):
            continue
        for key in list(item):
            canonical_key = aliases.get(key.replace('_', '').lower())
            if canonical_key and canonical_key not in item:
                item[canonical_key] = item[key]
    return data


def _known_internal_name(email: str) -> Optional[str]:
    """Resolve a Microsoft organizer email through known internal records."""
    if '@' not in email:
        return None
    alias, domain = email.lower().split('@', 1)
    if not _is_internal_domain(domain):
        return None

    from app.models import InternalContact, Seller, SolutionEngineer

    for model in (Seller, SolutionEngineer, InternalContact):
        record = model.query.filter(db.func.lower(model.alias) == alias).first()
        if record is not None:
            return record.name
    return None


def _include_missing_organizer(
    attendees: List[Dict[str, Any]],
    organizer_email: Optional[str],
) -> List[Dict[str, Any]]:
    """Include a missing organizer while preserving the attendee limit."""
    if not organizer_email:
        return attendees[:MAX_MEETING_ATTENDEES]

    normalized_email = organizer_email.strip().lower()
    attendee_emails = {
        (attendee.get('email') or '').strip().lower()
        for attendee in attendees
        if isinstance(attendee, dict)
    }
    if normalized_email in attendee_emails:
        return attendees[:MAX_MEETING_ATTENDEES]

    organizer = {
        'name': _known_internal_name(normalized_email) or organizer_email,
        'email': normalized_email,
    }
    return attendees[:MAX_MEETING_ATTENDEES - 1] + [organizer]


def _legacy_meetings_to_raw(
    meetings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert proven markdown-parser output into prefetch meeting shape."""
    converted = []
    for meeting in meetings:
        start_time = meeting.get('start_time')
        if not start_time or not meeting.get('title'):
            continue
        local_start = start_time.astimezone()
        local_end = local_start + timedelta(minutes=30)
        converted.append({
            'subject': meeting['title'],
            'start_time': local_start.isoformat(),
            'end_time': local_end.isoformat(),
            'organizer_email': None,
            'is_recurring': False,
            'attendees': [],
            'external_company': meeting.get('customer') or '',
        })
    return converted


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
        # WorkIQ sometimes omits the requested timezone offset and returns
        # the user's local wall-clock time. Treating that value as UTC shifts
        # Eastern meetings four or five hours earlier in the calendar.
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_to_naive_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive-UTC datetime to naive local time.

    PrefetchedMeeting.start_time is stored as naive UTC (per repo datetime
    conventions). The meeting picker UI and the note ``call_date`` field
    both expect naive local time (the WorkIQ markdown path historically
    produced local times). Convert at the serialization boundary so the JS
    ``new Date(iso_string)`` parses to the correct wall-clock time.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # Defensive: if somehow tz-aware, normalize to UTC first.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


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
    'corporate', 'enterprise', 'commercial', 'industrial', 'financial',
    # Consumer-brand first tokens that show up in generic meeting titles.
    'apple', 'delta', 'uber', 'oracle', 'amazon', 'google', 'meta',
    'microsoft', 'msft', 'azure', 'aws', 'gcp',
    # Common English nouns/verbs that show up in non-customer meeting
    # titles (e.g. "Start of day calendar block" matches "BLOCK
    # COMMUNICATIONS"). Conservative list: only words that commonly
    # appear in generic business meeting titles.
    'block', 'call', 'sync', 'review', 'update', 'status', 'check',
    'meeting', 'standup', 'stand', 'daily', 'weekly', 'monthly',
    'team', 'group', 'office', 'hours', 'hour', 'time', 'today',
    'tomorrow', 'next', 'last', 'final', 'initial', 'kickoff', 'kick',
    'close', 'open', 'test', 'demo', 'training', 'plan',
    'planning', 'project', 'program', 'portfolio', 'product',
    'platform', 'service', 'process', 'strategy', 'vision', 'mission',
    'start', 'stop', 'pause', 'break', 'lunch', 'focus', 'deep',
    'quick', 'brief', 'short', 'long', 'note', 'notes', 'action',
    'items', 'item', 'task', 'tasks', 'work', 'works', 'working',
    'client', 'customer', 'partner', 'vendor', 'supplier', 'account',
    'accounts', 'sales', 'marketing', 'finance', 'legal', 'hr',
    'engineering', 'ops', 'operations', 'design', 'research',
    'calendar', 'event', 'room', 'space', 'board', 'table', 'desk',
    'side', 'front', 'back', 'top', 'bottom', 'main', 'sub',
    # Generic business/real-estate nouns that appear in company names but
    # collide with everyday meeting titles (e.g. "Corporate Office
    # Properties" vs "FY27 Role Breakout | Corporate DSE").
    'properties', 'property', 'offices', 'holdings',
    'ventures', 'capital', 'partners', 'associates', 'industries',
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
        if low in _GENERIC_FIRST_TOKENS:
            # Common English word that collides with generic meeting
            # titles (e.g. "block" matching "Start of day calendar block").
            return None
        if tok.isupper() and len(tok) >= 3:
            return tok
        if len(tok) >= 4:
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

# Ghost-aura retention: a prefetched meeting stays in the cache for this many
# business days AFTER the meeting date so the home-page calendar shows a
# trailing aura behind today (matching the forward aura built by
# meeting_sync.ensure_meeting_aura). 5 business days = roughly a working week.
GHOST_RETENTION_BUSINESS_DAYS = 5


def _add_business_days(start: date, n: int) -> date:
    """Return the date that is ``n`` business days after ``start`` (Mon-Fri).

    Weekends do not count. ``n=0`` returns ``start`` unchanged. If ``start``
    is itself a weekend day, it is treated as the prior Friday for counting
    purposes (we still consume ``n`` weekdays from there).
    """
    d = start
    remaining = n
    while remaining > 0:
        d = d + timedelta(days=1)
        if d.weekday() < 5:  # Mon=0 .. Fri=4
            remaining -= 1
    return d


def _expires_at_for(meeting_date: date) -> datetime:
    """When a prefetched meeting row should be purged.

    Meetings live until end-of-day local on the date that is
    ``GHOST_RETENTION_BUSINESS_DAYS`` business days after ``meeting_date``.
    This matches the "ghost aura" design: a configurable number of business
    days of meetings remain visible behind today on the home calendar so
    sellers can backfill notes for recent calls.
    """
    expiry_date = _add_business_days(meeting_date, GHOST_RETENTION_BUSINESS_DAYS)
    return datetime.combine(expiry_date, time(23, 59, 59))


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
    recurring_key = _recurring_key(subject, organizer) if is_recurring else None

    if not subject or not start_time:
        logger.debug("Prefetch: skipping meeting with no subject or start_time")
        return None

    if _is_blacklisted_subject(subject):
        logger.debug("Prefetch: skipping blacklisted subject %r", subject)
        return None

    workiq_id = _synthetic_id(subject, start_time, organizer)
    raw_attendees = raw.get('attendees') or []
    if not isinstance(raw_attendees, list):
        raw_attendees = []
    raw_attendees = _include_missing_organizer(raw_attendees, organizer)

    matching_subject = ' '.join(filter(None, [
        subject,
        (raw.get('external_company') or '').strip(),
    ]))
    customer_id, matched_via = _resolve_customer(
        raw_attendees, domain_map,
        subject=matching_subject, subject_matchers=subject_matchers,
    )

    existing = PrefetchedMeeting.query.filter_by(workiq_id=workiq_id).first()
    if existing:
        existing.subject = subject
        existing.start_time = start_time
        existing.end_time = end_time
        existing.meeting_date = target_date
        existing.organizer_email = organizer
        existing.is_recurring = is_recurring
        existing.recurring_key = recurring_key
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
            recurring_key=recurring_key,
            customer_id=customer_id,
            matched_via=matched_via,
            expires_at=_expires_at_for(target_date),
        )
        db.session.add(meeting)
        db.session.flush()

    if recurring_key:
        from app.models import DismissedRecurringMeeting
        if db.session.get(DismissedRecurringMeeting, recurring_key) is not None:
            meeting.dismissed = True

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
    """Delete meetings older than the current Microsoft fiscal year.

    Activity coverage needs a durable ledger for the full fiscal year. The
    home-page ghost query still applies its short display window, so retaining
    these rows does not add stale meetings to that calendar.

    Returns the number of rows deleted.
    """
    today = date.today()
    fiscal_start_year = today.year if today.month >= 7 else today.year - 1
    cutoff_date = date(fiscal_start_year, 7, 1)

    expired = PrefetchedMeeting.query.filter(
        PrefetchedMeeting.meeting_date < cutoff_date
    ).all()
    count = len(expired)
    for m in expired:
        db.session.delete(m)
    if count:
        db.session.commit()
        logger.info(
            "Prefetch: purged %d meetings before fiscal year start %s",
            count, cutoff_date.isoformat(),
        )
    return count


def purge_blacklisted() -> int:
    """Remove already-cached meetings whose subject is now blacklisted.

    Covers rows stored before a blacklist pattern existed. Deletes matching
    ``PrefetchedMeeting`` ghosts (attendees cascade) and strips matching
    entries out of every ``DailyMeetingCache`` JSON blob so neither the
    home-calendar ghost dots nor the meeting picker keep showing them.

    Returns the number of ghost rows deleted.
    """
    from app.models import DailyMeetingCache

    ghosts = PrefetchedMeeting.query.all()
    removed = [m for m in ghosts if _is_blacklisted_subject(m.subject)]
    for m in removed:
        db.session.delete(m)

    caches_changed = 0
    for cache in DailyMeetingCache.query.all():
        try:
            meetings = json.loads(cache.meetings_json) if cache.meetings_json else []
        except (ValueError, TypeError):
            continue
        if not isinstance(meetings, list):
            continue
        kept = [
            mt for mt in meetings
            if not (isinstance(mt, dict) and _is_blacklisted_subject(mt.get('title')))
        ]
        if len(kept) != len(meetings):
            cache.meetings_json = json.dumps(kept)
            caches_changed += 1

    if removed or caches_changed:
        db.session.commit()
        logger.info(
            "Prefetch: purged %d blacklisted ghosts and scrubbed %d cache day(s)",
            len(removed), caches_changed,
        )
    return len(removed)


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
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError as exc:
        return 0, [], f"Bad date: {exc}"

    # NOTE: purge_expired() is intentionally NOT called here. Per the
    # ghost-aura design it should only run during the scheduled morning
    # aura and the startup catchup aura -- both of which go through
    # ensure_meeting_aura(). Manual per-day refreshes (the calendar
    # day-number click, ad-hoc back-fills, etc.) must NOT delete other
    # days' ghosts as a side effect.

    raw_meetings: Optional[List[Dict[str, Any]]] = None
    if target_date < date.today():
        try:
            from app.services.outlook_calendar import fetch_outlook_meetings_for_date
            raw_meetings = fetch_outlook_meetings_for_date(target_date)
            logger.info(
                'Prefetch: read %d historical meetings from Outlook for %s',
                len(raw_meetings), date_str,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                'Prefetch: Outlook historical fetch failed for %s; using WorkIQ',
                date_str,
                exc_info=True,
            )

    if raw_meetings is None:
        raw_meetings, error = fetch_workiq_meetings_for_date(date_str)
        if error:
            return 0, [], error

    return store_prefetched_meetings_for_date(date_str, raw_meetings)


def fetch_workiq_meetings_for_date(
    date_str: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Fetch and parse one date from WorkIQ without touching the database."""
    from app.services.workiq_service import query_workiq

    prompt = _build_prompt(date_str)
    # WorkIQ sometimes ignores the JSON contract and returns its legacy
    # markdown table. Parse that response in place, then make one explicit
    # markdown-table request if needed. Repeating the same JSON prompt three
    # times proved both slow and unreliable for historical dates.
    last_parse_error: Optional[str] = None
    raw_meetings: Optional[List[Dict[str, Any]]] = None
    logger.info("Prefetch: querying WorkIQ for %s", date_str)
    try:
        # Today JSON observed at 131s in Phase 0 probe; give 240s headroom.
        response = query_workiq(prompt, timeout=240, operation='meeting_list')
    except Exception as exc:  # noqa: BLE001
        logger.error("Prefetch: WorkIQ call failed for %s: %s", date_str, exc)
        return None, str(exc)

    try:
        raw_meetings = _extract_json_array(response)
    except ValueError as exc:
        last_parse_error = str(exc)
        logger.warning(
            'Prefetch: JSON parse failed for %s; retrying once: %s',
            date_str, exc,
        )
        try:
            retry_response = query_workiq(
                prompt,
                timeout=240,
                operation='meeting_list',
            )
            raw_meetings = _extract_json_array(retry_response)
        except Exception as retry_exc:  # noqa: BLE001
            last_parse_error = str(retry_exc)

    if raw_meetings is None:
        from app.services.workiq_service import (
            _parse_meetings_response,
            get_meetings_for_date,
        )
        raw_meetings = _legacy_meetings_to_raw(
            _parse_meetings_response(response, date_str),
        ) or None
        if raw_meetings is None:
            logger.warning(
                "Prefetch: JSON parsing failed for %s; trying markdown fallback: %s",
                date_str, last_parse_error,
            )
            legacy_meetings, _ = get_meetings_for_date(date_str)
            raw_meetings = _legacy_meetings_to_raw(legacy_meetings) or None
            if raw_meetings is None:
                last_parse_error = (
                    f'{last_parse_error}; markdown fallback returned no meetings'
                )

    if raw_meetings is None:
        # Both formats returned unusable data. Preserve existing rows
        # rather than wiping them; UI surfaces the stale-sync indicator.
        try:
            from app.services.telemetry_shipper import queue_workiq_call
            queue_workiq_call('meeting_list', 'parse_failed',
                              failure_type='parse_meeting_list_json')
        except Exception:
            pass
        logger.error(
            "Prefetch: JSON and markdown parsing failed for %s: %s",
            date_str, last_parse_error,
        )
        return None, f"JSON and markdown parsing failed: {last_parse_error}"

    return raw_meetings, None


def store_prefetched_meetings_for_date(
    date_str: str,
    raw_meetings: List[Dict[str, Any]],
) -> Tuple[int, List[Dict[str, Any]], Optional[str]]:
    """Upsert already-fetched meetings for one date into local caches."""
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError as exc:
        return 0, [], f"Bad date: {exc}"

    if not raw_meetings:
        logger.warning("Prefetch: empty meeting list for %s", date_str)
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

    # Re-resolve any still-unmatched ghosts for this date. Catches the
    # case where the customer DB grew between yesterday's aura and today's
    # (e.g. user ran the MSX account import after the morning sync). Only
    # touches rows where customer_id is already NULL so we never override
    # a prior match.
    rematched = _rematch_unmatched_for_date(
        target_date, domain_map, subject_matchers,
    )
    if rematched:
        logger.info(
            "Prefetch: rematched %d previously-unmatched ghosts for %s",
            rematched, date_str,
        )

    db.session.commit()
    picker_list = [_to_picker_dict(m) for m in stored_meetings]
    logger.info("Prefetch: stored %d meetings for %s",
                len(stored_meetings), date_str)
    return len(stored_meetings), picker_list, None


def _rematch_unmatched_for_date(
    target_date: date,
    domain_map: Dict[str, Tuple[int, str]],
    subject_matchers: List[Tuple[re.Pattern, int, str]],
) -> int:
    """Try to map previously-unmatched ghosts using a fresh domain map.

    Iterates ``PrefetchedMeeting`` rows for ``target_date`` that have
    ``customer_id IS NULL`` and runs ``_resolve_customer`` against their
    cached attendees + subject. Updates rows in place. Returns the count
    of rows that gained a customer match.
    """
    unmatched = (
        PrefetchedMeeting.query
        .options(joinedload(PrefetchedMeeting.attendees))
        .filter_by(meeting_date=target_date, customer_id=None)
        .all()
    )
    if not unmatched:
        return 0

    fixed = 0
    for ghost in unmatched:
        attendee_dicts = [
            {'email': a.email or '', 'name': a.name or ''}
            for a in ghost.attendees
        ]
        cid, matched_via = _resolve_customer(
            attendee_dicts, domain_map,
            subject=ghost.subject, subject_matchers=subject_matchers,
        )
        if cid is not None:
            ghost.customer_id = cid
            ghost.matched_via = matched_via
            fixed += 1
    return fixed


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

    # PrefetchedMeeting.start_time is naive UTC; convert to naive local
    # so the picker JS (which does `new Date(iso)`) and call_date (which
    # is stored as naive local per repo conventions) get the right wall
    # clock time. See _utc_to_naive_local for rationale.
    start_local = _utc_to_naive_local(meeting.start_time)
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
