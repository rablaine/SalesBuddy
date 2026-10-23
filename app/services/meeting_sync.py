"""Daily meeting cache service.

Pre-fetches today's WorkIQ meetings at 7 AM local time and caches them
in the database. Note creation, attendee import, and Fill My Day all
read from the cache instead of making live WorkIQ calls for meeting lists.

Lifecycle:
- Daily at 7 AM local time, fetch today's meetings and store in DailyMeetingCache
- On startup, if today's meetings haven't been synced yet, do a catchup fetch
- User can manually refresh via the meeting selection modal to pick up new meetings
"""
import json
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone
from typing import Any

from app.models import db, DailyMeetingCache

logger = logging.getLogger(__name__)

# Hour (local time) when the daily sync fires
SYNC_HOUR = 7

# Default forward window for the morning aura, counted in BUSINESS DAYS
# beyond the start date. So today + 4 = 5 business days total.
DEFAULT_AURA_DAYS_AHEAD = 4
MEETING_FETCH_WORKERS = 5

# Prevent concurrent syncs. Held for the full duration of an aura run.
_sync_lock = threading.Lock()

# Lightweight observable state for the UI "sync in progress" modal. Read
# via get_sync_state_snapshot(); written only inside ensure_meeting_aura.
# A separate small lock keeps reads cheap and decoupled from _sync_lock.
_sync_state: dict[str, Any] = {
    'running': False,
    'current_date': None,
    'started_at': None,    # ISO datetime string in UTC
    'aura_dates': [],      # list of YYYY-MM-DD covered by the active run
}
_sync_state_lock = threading.Lock()


def get_sync_state_snapshot() -> dict[str, Any]:
    """Return a copy of the current sync state. Cheap, never blocks aura."""
    from app.services.sync_jobs import ACTIVE_STATES, job_snapshot, operation_runs

    job, _ = operation_runs('meeting-aura')
    if job:
        snapshot = job_snapshot(job)
        return {
            'running': job.status in ACTIVE_STATES, 'job_id': job.id,
            'started_at': snapshot['started_at'], 'current_date': snapshot['progress'].get(
                'current_date'), 'aura_dates': snapshot['progress'].get('aura_dates', []),
            'error': snapshot['error'], 'job': snapshot,
        }
    with _sync_state_lock:
        return dict(_sync_state)


def get_meeting_refresh_jobs(watched_ids: list[int]) -> list[dict[str, Any]]:
    """Discover active refreshes and retain observed jobs through their terminal result."""
    from app.models import Job
    from app.services.sync_jobs import ACTIVE_STATES, job_snapshot

    types = ('sync:meetings', 'sync:meeting-aura')
    latest = db.session.query(db.func.max(Job.id)).filter(
        Job.job_type.in_(types)).group_by(Job.job_type)
    jobs = Job.query.filter(
        Job.job_type.in_(types),
        db.or_(Job.status.in_(ACTIVE_STATES), Job.id.in_(watched_ids), Job.id.in_(latest)),
    ).order_by(Job.id).all()
    snapshots = []
    for job in jobs:
        snapshot = job_snapshot(job)
        options = snapshot['options']
        if snapshot['operation'] == 'meetings':
            dates = [options['date']]
        else:
            dates = [day.isoformat() for day in _aura_window_dates(
                start=date.fromisoformat(options['anchor']),
                days_ahead=options['days_ahead'])]
        snapshots.append({**snapshot, 'calendar_dates': dates})
    return snapshots


def _set_sync_state(progress: dict[str, Any] | None = None, **kwargs) -> None:
    from app.services.sync_jobs import publish_progress

    with _sync_state_lock:
        _sync_state.update(kwargs)
        state = dict(_sync_state)
    dates = state['aura_dates']
    current = state['current_date']
    completed = dates.index(current) if current in dates else 0
    publish_progress({
        'phase': 'calendar', 'current': completed, 'total': len(dates),
        'progress': 100 * completed / len(dates) if dates else None,
        'message': f'Refreshing {current} ({completed + 1}/{len(dates)}).'
        if current else 'Refreshing meeting calendar.',
        **state,
        **(progress or {}),
    })


def _clear_sync_state() -> None:
    with _sync_state_lock:
        _sync_state.update({
            'running': False,
            'current_date': None,
            'started_at': None,
            'aura_dates': [],
        })


def sync_meetings_for_date(
    date_str: str,
    prefetched_meetings: list[dict[str, Any]] | None = None,
) -> tuple[list, str | None]:
    """Fetch meetings + attendees from WorkIQ and update both caches.

    Single WorkIQ call (JSON shape per Phase 0 of
    PREFETCH_MEETINGS_BACKLOG.md) populates the PrefetchedMeeting tables
    AND the legacy DailyMeetingCache so the meeting picker keeps working
    without any additional WorkIQ traffic.

    Stale-ghost handling is post-sync, not pre-sync: we snapshot the
    workiq_ids already present for this date, run the WorkIQ call (which
    upserts what's still on the calendar), and only THEN drop the
    snapshot ids that WorkIQ no longer returned. If WorkIQ fails or
    returns nothing usable, the existing ghosts are preserved so the
    user doesn't get a wiped-out day from a transient WorkIQ hiccup.

    Dismissed ghosts and noted ghosts are always preserved.

    Args:
        date_str: Date in YYYY-MM-DD format.
        prefetched_meetings: Optional raw source payload already fetched by a
            parallel calendar-import worker. Database writes remain serialized.

    Returns:
        Tuple of (meetings list in picker shape, error message or None).
    """
    from app.services.meeting_prefetch import (
        prefetch_for_date_full,
        store_prefetched_meetings_for_date,
    )
    from app.models import PrefetchedMeeting, PrefetchedMeetingAttendee

    logger.info("Syncing meetings for %s", date_str)

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        # Malformed date: let prefetch_for_date_full surface the error.
        target_date = None

    # Snapshot purge candidates BEFORE the WorkIQ call so a failure
    # leaves the day intact.
    purge_candidate_ids: set[str] = set()
    if target_date is not None:
        purge_candidate_ids = {
            row.workiq_id for row in (
                PrefetchedMeeting.query
                .filter(PrefetchedMeeting.meeting_date == target_date)
                .filter(PrefetchedMeeting.note_id.is_(None))
                .filter(~PrefetchedMeeting.activity.has())
                .filter(PrefetchedMeeting.dismissed.is_(False))
                .all()
            )
        }

    if prefetched_meetings is None:
        _, picker_meetings, err = prefetch_for_date_full(date_str)
    else:
        _, picker_meetings, err = store_prefetched_meetings_for_date(
            date_str,
            prefetched_meetings,
        )
    if err:
        # WorkIQ call failed -- leave existing ghosts alone.
        return [], err

    # WorkIQ succeeded. Anything in the snapshot that wasn't re-upserted
    # by this run is stale (canceled / moved) and should go.
    if target_date is not None and purge_candidate_ids:
        returned_ids = {
            m.get('id') for m in picker_meetings if m.get('id')
        }
        stale_workiq_ids = purge_candidate_ids - returned_ids
        if stale_workiq_ids:
            stale_rows = (
                PrefetchedMeeting.query
                .filter(PrefetchedMeeting.workiq_id.in_(stale_workiq_ids))
                .filter(PrefetchedMeeting.note_id.is_(None))
                .filter(~PrefetchedMeeting.activity.has())
                .filter(PrefetchedMeeting.dismissed.is_(False))
                .all()
            )
            stale_ids = [row.id for row in stale_rows]
            if stale_ids:
                (PrefetchedMeetingAttendee.query
                    .filter(PrefetchedMeetingAttendee.meeting_id.in_(stale_ids))
                    .delete(synchronize_session=False))
                (PrefetchedMeeting.query
                    .filter(PrefetchedMeeting.id.in_(stale_ids))
                    .delete(synchronize_session=False))
                db.session.commit()
                logger.info(
                    "Post-sync purge: removed %d stale ghosts for %s",
                    len(stale_ids), date_str,
                )

    if target_date is None:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    payload = json.dumps(picker_meetings)
    cache = DailyMeetingCache.query.filter_by(meeting_date=target_date).first()
    if cache:
        cache.meetings_json = payload
        cache.synced_at = datetime.now(timezone.utc)
    else:
        cache = DailyMeetingCache(
            meeting_date=target_date,
            meetings_json=payload,
        )
        db.session.add(cache)
    db.session.commit()
    logger.info("Cached %d meetings for %s", len(picker_meetings), date_str)
    return picker_meetings, None


def _sync_aura_dates(aura_strs: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """Fetch five dates concurrently, then store each result on the owning worker thread."""
    from flask import current_app
    from app.services.meeting_prefetch import fetch_meetings_for_date

    app = current_app._get_current_object()
    payloads: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    total = len(aura_strs)
    date_states = dict.fromkeys(aura_strs, 'fetching')

    def fetch(day_str: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Isolate source context/session from the owning worker's write transaction."""
        with app.app_context():
            return fetch_meetings_for_date(day_str)

    def report(phase: str, current: int, day_str: str, message: str) -> None:
        """Measure completed fetch/save units without estimating external query time."""
        _set_sync_state(current_date=day_str, progress={
            'phase': phase, 'current': current, 'total': total * 2,
            'progress': 100 * current / (total * 2), 'message': message,
            'fetched_dates': len(payloads), 'failed_dates': len(errors),
            'total_dates': total,
            'date_states': dict(date_states), 'date_errors': dict(errors),
        })

    with ThreadPoolExecutor(max_workers=MEETING_FETCH_WORKERS) as executor:
        futures = {executor.submit(fetch, day_str): day_str for day_str in aura_strs}
        for completed, future in enumerate(as_completed(futures), 1):
            day_str = futures[future]
            try:
                meetings, error = future.result()
                if error or meetings is None:
                    raise RuntimeError(error or 'Meeting source returned no data.')
                payloads[day_str] = meetings
                date_states[day_str] = 'queued'
            except Exception as exc:
                logger.exception('Meeting fetch failed for %s', day_str)
                errors[day_str] = str(exc)
                counts[day_str] = 0
                date_states[day_str] = 'failed'
            report('fetching', completed, day_str,
                   f'Received {completed}/{total} date results ({len(errors)} failed).')

    for index, day_str in enumerate(aura_strs):
        if day_str in payloads:
            date_states[day_str] = 'saving'
            report('calendar', total + index, day_str,
                   f'Saving meeting date {day_str}.')
            try:
                meetings, error = sync_meetings_for_date(
                    day_str, prefetched_meetings=payloads[day_str])
                counts[day_str] = len(meetings)
                if error:
                    errors[day_str] = error
                date_states[day_str] = 'failed' if error else 'complete'
            except Exception as exc:
                logger.exception('Meeting save failed for %s', day_str)
                db.session.rollback()
                errors[day_str] = str(exc)
                counts[day_str] = 0
                date_states[day_str] = 'failed'
        saved = index + 1 - sum(day in errors for day in aura_strs[:index + 1])
        report('calendar', total + index + 1, day_str,
               f'Saved {saved}/{total} meeting dates ({len(errors)} failed).')
    return counts, errors


def ensure_meeting_aura(
    start: date | None = None,
    days_ahead: int = DEFAULT_AURA_DAYS_AHEAD,
) -> tuple[dict[str, int], dict[str, str]]:
    """Fetch today and the next weekdays in parallel, then save their caches serially.

    The full aura runs under ``_sync_lock``. If the lock is already held
    by another caller, this returns immediately without blocking; callers
    can poll :func:`get_sync_state_snapshot` to surface progress in the UI.

    Per-day failures are captured in the returned ``errors`` dict but do
    not abort the rest of the loop.

    Args:
        start: First date in the aura window. Defaults to ``date.today()``.
            If start itself falls on a weekend, the loop simply walks forward
            and picks the next business days.
        days_ahead: Number of additional **business days** to include after
            ``start``. Default is ``DEFAULT_AURA_DAYS_AHEAD`` (4), giving
            5 weekdays total.

    Returns:
        ``(counts, errors)`` keyed by ``YYYY-MM-DD``. If the lock was
        already held, ``errors`` will contain a single ``'_status'``
        marker and ``counts`` will be empty.
    """
    if start is None:
        start = date.today()

    # Gate on having a populated customer DB. If no customers exist (fresh
    # install where the user hasn't run the MSX account import yet), every
    # ghost would be stored with customer_id=NULL because _build_domain_map
    # and _build_subject_matchers both come up empty. Skipping here avoids
    # poisoning the cache with unmatched garbage. Caller (e.g. post-MSX-
    # import hook) is responsible for re-running once customers exist.
    from app.models import Customer
    if Customer.query.count() == 0:
        logger.info(
            "ensure_meeting_aura: no customers in DB, skipping aura "
            "(prevents unmatched ghosts)"
        )
        return {}, {'_status': 'no customers - run account import first'}

    if not _sync_lock.acquire(blocking=False):
        logger.info("ensure_meeting_aura: another sync is in flight, skipping")
        return {}, {'_status': 'already running'}

    # Purge ghosts that have aged out of the retention window. This is
    # the ONLY place purge_expired() runs -- per the ghost-aura design,
    # ad-hoc per-day refreshes must not delete other days' ghosts. The
    # morning scheduled sync and the startup catchup sync both flow
    # through this function, so doing it once here covers both.
    try:
        from app.services.meeting_prefetch import purge_expired
        purge_expired()
    except Exception:  # noqa: BLE001
        # Purging is housekeeping; never let it abort an aura run.
        logger.exception("ensure_meeting_aura: purge_expired failed")

    # Scrub blacklisted internal/segment meetings (e.g. "SME&C" kickoffs)
    # that may have been cached before the blacklist existed, and that a
    # customer name like "SME" would otherwise keep re-matching.
    try:
        from app.services.meeting_prefetch import purge_blacklisted
        purge_blacklisted()
    except Exception:  # noqa: BLE001
        logger.exception("ensure_meeting_aura: purge_blacklisted failed")

    weekdays = _aura_window_dates(start=start, days_ahead=days_ahead)
    aura_strs = [d.strftime('%Y-%m-%d') for d in weekdays]

    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    try:
        _set_sync_state(
            running=True,
            current_date=aura_strs[0] if aura_strs else None,
            started_at=datetime.now(timezone.utc).isoformat(),
            aura_dates=list(aura_strs),
            progress={'phase': 'fetching', 'current': 0, 'total': len(aura_strs) * 2,
                      'progress': 0,
                      'message': f'Fetching {len(aura_strs)} meeting dates in parallel.'},
        )
        logger.info(
            "ensure_meeting_aura: starting aura for %d weekday(s): %s",
            len(aura_strs), aura_strs,
        )

        counts, errors = _sync_aura_dates(aura_strs)

        logger.info(
            "ensure_meeting_aura complete: %d meetings across %d days, %d errors",
            sum(counts.values()), len(counts), len(errors),
        )
        return counts, errors
    finally:
        _clear_sync_state()
        _sync_lock.release()


def get_cached_meetings(date_str: str) -> tuple[list | None, datetime | None]:
    """Get cached meetings for a date, if available.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        Tuple of (meetings list or None if no cache, synced_at datetime or None).
    """
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None, None

    cache = DailyMeetingCache.query.filter_by(meeting_date=target_date).first()
    if not cache:
        return None, None

    return cache.get_meetings(), cache.synced_at


def _should_sync_today() -> bool:
    """Check if today's meetings have already been synced."""
    today = date.today()
    cache = DailyMeetingCache.query.filter_by(meeting_date=today).first()
    return cache is None


def _should_prefetch_today() -> bool:
    """Check if today's attendee prefetch has run."""
    from app.models import PrefetchedMeeting
    today = date.today()
    row = PrefetchedMeeting.query.filter_by(meeting_date=today).first()
    return row is None


def _has_meeting_customers() -> bool:
    """Defer automatic meeting work until accounts exist for matching."""
    from app.models import Customer

    return Customer.query.first() is not None


def _run_sync(app) -> None:
    """Run the morning aura sync (today + next 7 weekdays).

    The aura function manages its own lock + state; this wrapper only
    provides the Flask app context.
    """
    try:
        with app.app_context():
            if not _has_meeting_customers():
                logger.info("Automatic meeting sync deferred until accounts are imported")
                return
            from app.services.sync_jobs import admit_sync
            admit_sync('meeting-aura', initiator='scheduled')
    except Exception:
        logger.exception("Error in daily meeting sync")


def _run_prefetch_only(app) -> None:
    """Run only the attendee prefetch (skip the meeting-list cache write).

    Used at startup when the meeting list is already cached but the
    PrefetchedMeeting tables are empty -- e.g. user upgraded after the
    morning sync already ran today.
    """
    try:
        with app.app_context():
            if not _has_meeting_customers():
                logger.info("Automatic meeting prefetch deferred until accounts are imported")
                return
            from app.services.sync_jobs import admit_sync
            today_str = date.today().strftime('%Y-%m-%d')
            admit_sync('meetings', {'date': today_str}, initiator='scheduled')
    except Exception:
        logger.exception("Standalone meeting prefetch failed")


def _aura_window_dates(start: date | None = None,
                       days_ahead: int = DEFAULT_AURA_DAYS_AHEAD) -> list[date]:
    """Return the business days the aura would cover from ``start``.

    The window includes ``start`` itself (or, if ``start`` is a weekend,
    the next weekday) plus the next ``days_ahead`` business days. Total
    length is ``days_ahead + 1`` business days.
    """
    from datetime import timedelta
    if start is None:
        start = date.today()
    needed = days_ahead + 1
    out: list[date] = []
    cursor = start
    # Walk forward (potentially past weekends) until we have enough
    # business days. Cap at 14 calendar days as a paranoia bound.
    safety = 0
    while len(out) < needed and safety < 14:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor = cursor + timedelta(days=1)
        safety += 1
    return out


def _most_recent_sync_boundary(now: datetime | None = None) -> datetime:
    """Return the most recent SYNC_HOUR boundary in local naive time.

    If ``now`` is at or after today's SYNC_HOUR, the boundary is today
    at SYNC_HOUR. Otherwise it's yesterday at SYNC_HOUR. A cache is
    "fresh" iff its ``synced_at`` is at or after this boundary.
    """
    from datetime import timedelta
    if now is None:
        now = datetime.now()
    today_boundary = datetime.combine(
        now.date(), datetime.min.time()
    ).replace(hour=SYNC_HOUR)
    if now >= today_boundary:
        return today_boundary
    return today_boundary - timedelta(days=1)


def _aura_needs_run(app) -> bool:
    """True if any weekday in the aura window lacks a fresh cache.

    Two checks per day:
      1. The DailyMeetingCache row was synced at or after the most recent
         SYNC_HOUR boundary (see :func:`_most_recent_sync_boundary`). The
         morning sync stamps every day in the window with the same
         ``synced_at``, so a future-date cache is fresh as long as it was
         synced in the current cycle.
      2. The PrefetchedMeeting table has at least as many rows for that
         date as the cached picker list. The picker cache and the
         prefetched_meetings table can drift (interrupted sync, expired
         rows purged out from under us, etc.). When they disagree, the
         calendar shows fewer ghosts than the cache promises, so we
         re-run the aura to repopulate.

    Previous versions only checked freshness of the picker cache and
    missed the drift case entirely (caught 2026-04-22: 27/28 had cached
    picker meetings but zero PrefetchedMeeting rows, so no ghosts
    appeared and restart didn't self-heal).
    """
    from app.models import PrefetchedMeeting

    boundary = _most_recent_sync_boundary()
    with app.app_context():
        for d in _aura_window_dates():
            cache = DailyMeetingCache.query.filter_by(meeting_date=d).first()
            if cache is None or cache.synced_at is None:
                return True
            synced_at_local = cache.synced_at
            if synced_at_local.tzinfo is not None:
                synced_at_local = synced_at_local.astimezone().replace(tzinfo=None)
            if synced_at_local < boundary:
                return True

            # Drift check: cache promises N meetings, prefetched table
            # should have at least N rows for that date. We allow >=
            # because prefetched rows can survive across syncs (dismissed
            # / noted) while the cached picker list reflects only the
            # most recent WorkIQ snapshot.
            try:
                expected = len(cache.get_meetings() or [])
            except Exception:
                expected = 0
            if expected > 0:
                actual = (
                    PrefetchedMeeting.query
                    .filter_by(meeting_date=d)
                    .count()
                )
                if actual < expected:
                    logger.info(
                        "Aura drift: %s cache has %d meetings but "
                        "prefetched table has %d rows; re-syncing",
                        d, expected, actual,
                    )
                    return True
    return False


def start_meeting_sync_background(app) -> None:
    """Catch up on missed meeting sync at startup.

    If any weekday in the aura window (today + next 7 calendar days) is
    missing a fresh cache, fire a background aura sync. This covers both
    the "first run of the day" case and the "user just installed and has
    no future-day data yet" case.
    """
    with app.app_context():
        if not _has_meeting_customers():
            logger.info("Startup meeting sync deferred until accounts are imported")
            return
    if _aura_needs_run(app):
        logger.info("Aura window incomplete at startup, kicking off background sync")
        thread = threading.Thread(target=_run_sync, args=(app,), daemon=True)
        thread.start()
        return

    # Cache covers the full window. Still make sure today's attendee
    # prefetch has populated the PrefetchedMeeting tables -- handles the
    # rare case where the cache exists but the attendee tables don't.
    with app.app_context():
        if _should_prefetch_today():
            logger.info("Today's attendee prefetch missing, kicking off")
            thread = threading.Thread(
                target=_run_prefetch_only, args=(app,), daemon=True,
            )
            thread.start()


def start_daily_meeting_scheduler(app) -> None:
    """Start a background thread that fires the meeting sync at SYNC_HOUR daily.

    Runs in a loop, sleeping 5 minutes between checks. Safe to call once
    at app startup.

    Args:
        app: Flask app instance.
    """

    def _scheduler():
        logger.info(
            "Daily meeting scheduler started (sync hour: %d:00 local)", SYNC_HOUR
        )
        last_sync_date = None

        while True:
            try:
                now = datetime.now()
                today = now.date()

                if now.hour >= SYNC_HOUR and last_sync_date != today:
                    if _aura_needs_run(app):
                        logger.info("Daily scheduler triggering aura sync")
                        _run_sync(app)
                    last_sync_date = today

                _time.sleep(300)
            except Exception:
                logger.exception("Error in daily meeting scheduler")
                _time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
