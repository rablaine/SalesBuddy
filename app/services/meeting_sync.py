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
from datetime import datetime, date, timezone

from app.models import db, DailyMeetingCache

logger = logging.getLogger(__name__)

# Hour (local time) when the daily sync fires
SYNC_HOUR = 7

# Prevent concurrent syncs
_sync_lock = threading.Lock()


def sync_meetings_for_date(date_str: str) -> tuple[list, str | None]:
    """Fetch meetings + attendees from WorkIQ and update both caches.

    Single WorkIQ call (JSON shape per Phase 0 of
    PREFETCH_MEETINGS_BACKLOG.md) populates the PrefetchedMeeting tables
    AND the legacy DailyMeetingCache so the meeting picker keeps working
    without any additional WorkIQ traffic.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        Tuple of (meetings list in picker shape, error message or None).
    """
    from app.services.meeting_prefetch import prefetch_for_date_full

    logger.info("Syncing meetings for %s", date_str)
    _, picker_meetings, err = prefetch_for_date_full(date_str)
    if err:
        return [], err

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


def _run_sync(app) -> None:
    """Run the daily meeting sync with the sync lock held.

    Args:
        app: Flask app instance.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.debug("Meeting sync already running, skipping")
        return
    try:
        with app.app_context():
            today_str = date.today().strftime('%Y-%m-%d')
            # Single WorkIQ JSON pull populates both the legacy meeting
            # picker cache AND the PrefetchedMeeting attendee tables.
            sync_meetings_for_date(today_str)
    except Exception:
        logger.exception("Error in daily meeting sync")
    finally:
        _sync_lock.release()


def _run_prefetch_only(app) -> None:
    """Run only the attendee prefetch (skip the meeting-list cache write).

    Used at startup when the meeting list is already cached but the
    PrefetchedMeeting tables are empty -- e.g. user upgraded after the
    morning sync already ran today.
    """
    try:
        with app.app_context():
            from app.services.meeting_prefetch import prefetch_for_date
            today_str = date.today().strftime('%Y-%m-%d')
            prefetch_for_date(today_str)
    except Exception:
        logger.exception("Standalone meeting prefetch failed")


def start_meeting_sync_background(app) -> None:
    """Catch up on missed meeting sync at startup.

    If today's meetings haven't been fetched yet, fire a background sync.

    Args:
        app: Flask app instance.
    """
    with app.app_context():
        if not _should_sync_today():
            logger.debug("Today's meetings already cached, skipping startup sync")
            # Meeting list is cached but attendees may not be -- catch up the
            # prefetch independently so users who upgrade mid-day get the
            # benefit without waiting for tomorrow's 7 AM run.
            if _should_prefetch_today():
                logger.info("Today's attendee prefetch missing, kicking off")
                thread = threading.Thread(
                    target=_run_prefetch_only, args=(app,), daemon=True,
                )
                thread.start()
            return

    logger.info("Daily meeting cache missing for today, starting catchup sync")
    thread = threading.Thread(target=_run_sync, args=(app,), daemon=True)
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
                    with app.app_context():
                        if _should_sync_today():
                            logger.info("Daily scheduler triggering meeting sync")
                            _run_sync(app)
                    last_sync_date = today

                _time.sleep(300)
            except Exception:
                logger.exception("Error in daily meeting scheduler")
                _time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
