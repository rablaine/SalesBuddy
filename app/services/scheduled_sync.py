"""
Scheduled milestone sync for Sales Buddy.

Runs milestone import on Mon/Wed/Fri using a background daemon thread.
The sync time is randomly assigned per-user (stored in UserPreference) in
5-minute slots between 9:30 AM and 4:30 PM to stagger MSX load across
Sales Buddy instances. If missed (server was off), catches up on startup.

No environment variables needed - everything is stored in the database.
"""                                                                          
import random
import time
import threading
import logging
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()

# Random sync time: 5-minute slots between 9:30 AM and 4:30 PM (84 slots)
# e.g. slot 0 = 9:30, slot 1 = 9:35, ..., slot 83 = 16:25
SYNC_START_HOUR = 9
SYNC_START_MINUTE = 30
SYNC_SLOT_COUNT = 84  # (4:30 PM - 9:30 AM) / 5 minutes

# Days the sync runs: Monday=0, Wednesday=2, Friday=4
SYNC_DAYS = {0, 2, 4}


def _ensure_sync_time(pref):
    """Assign a random sync time if not yet set. Returns (hour, minute).

    Picks a random 5-minute slot between 9:30 AM and 4:30 PM so that
    many Sales Buddy instances don't all hit MSX at the same time.
    """
    if pref.milestone_sync_hour is None or pref.milestone_sync_minute is None:
        slot = random.randint(0, SYNC_SLOT_COUNT - 1)
        total_minutes = (SYNC_START_HOUR * 60 + SYNC_START_MINUTE) + slot * 5
        pref.milestone_sync_hour = total_minutes // 60
        pref.milestone_sync_minute = total_minutes % 60
        from app import db
        db.session.commit()
        logger.info(
            "Assigned milestone sync time: %02d:%02d",
            pref.milestone_sync_hour, pref.milestone_sync_minute
        )
    return pref.milestone_sync_hour, pref.milestone_sync_minute


def _is_sync_day() -> bool:
    """Return True if today is Mon, Wed, or Fri."""
    return datetime.now().weekday() in SYNC_DAYS


def _last_sync_day() -> date:
    """Return the most recent Mon/Wed/Fri that is <= today."""
    today = datetime.now().date()
    for days_back in range(7):
        candidate = today - timedelta(days=days_back)
        if candidate.weekday() in SYNC_DAYS:
            return candidate
    return today  # fallback, shouldn't happen with MWF


def _missed_sync(pref) -> bool:
    """Check if the most recent scheduled sync (any MWF) was missed.

    Used for startup catchup. Looks backward to the last MWF sync day,
    so if the app starts on Tuesday it can still catch up Monday's missed sync.

    Args:
        pref: UserPreference instance (must have sync_hour/minute set).
    """
    last_day = _last_sync_day()
    hour, minute = pref.milestone_sync_hour, pref.milestone_sync_minute
    target = datetime(last_day.year, last_day.month, last_day.day,
                      hour, minute, 0)

    now = datetime.now()
    if now < target:
        return False

    if not pref.last_milestone_sync:
        return True

    last_sync = pref.last_milestone_sync
    if last_sync.tzinfo:
        last_sync_local = last_sync.astimezone().replace(tzinfo=None)
    else:
        last_sync_local = last_sync.replace(
            tzinfo=timezone.utc
        ).astimezone().replace(tzinfo=None)

    return last_sync_local < target


def _should_sync(pref) -> bool:
    """Check if a milestone sync is needed now.

    Returns True if:
    - Today is a sync day (Mon/Wed/Fri), AND
    - We're past today's scheduled time, AND
    - Either never synced or last sync was before today's scheduled time.

    Args:
        pref: UserPreference instance (must have sync_hour/minute set).
    """                                                                        
    if not _is_sync_day():
        return False
    hour, minute = pref.milestone_sync_hour, pref.milestone_sync_minute
    now = datetime.now()
    today_sync_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Haven't reached today's sync time yet
    if now < today_sync_time:
        return False

    if not pref.last_milestone_sync:
        return True

    # last_milestone_sync is stored as UTC - convert to local for comparison
    last_sync = pref.last_milestone_sync
    if last_sync.tzinfo:
        last_sync_local = last_sync.astimezone().replace(tzinfo=None)
    else:
        last_sync_local = last_sync.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

    return last_sync_local < today_sync_time


def _run_sync(app):
    """Execute the milestone sync within app context. Updates last_milestone_sync."""
    if not _sync_lock.acquire(blocking=False):
        logger.debug("Milestone sync already in progress, skipping")
        return
    try:
        with app.app_context():
            from app import db
            from app.models import UserPreference
            from app.services.milestone_sync import sync_all_customer_milestones

            result = sync_all_customer_milestones()
            if result.get('success'):
                logger.info(
                    "Milestone sync complete: %d customers, %d new, %d updated",
                    result.get('customers_synced', 0),
                    result.get('milestones_created', 0),
                    result.get('milestones_updated', 0),
                )
            else:
                logger.error("Milestone sync failed: %s", result.get('error', 'Unknown'))

            # Update last sync time regardless of success (avoid retry storms)
            pref = UserPreference.query.first()
            if pref:
                pref.last_milestone_sync = datetime.now(timezone.utc)
                db.session.commit()

            # Run marketing insights sync after milestone sync completes
            _run_marketing_sync(app)

            # Check if a U2C snapshot is due (5th of FQ start month)
            _check_u2c_snapshot()
    except Exception:
        logger.exception("Error during milestone sync")
    finally:
        _sync_lock.release()


def _run_marketing_sync(app):
    """Run marketing insights sync in the current thread (already in app context)."""
    try:
        from app.services.marketing_sync import sync_marketing_stream
        logger.info("Starting marketing insights sync (post-milestone)")
        for _ in sync_marketing_stream():
            pass  # Consume generator to completion
        logger.info("Marketing insights sync complete")
    except Exception:
        logger.exception("Error during marketing insights sync")


def _check_u2c_snapshot():
    """Create a U2C snapshot if today is the 5th of a fiscal quarter start month."""
    try:
        from app.services.u2c_snapshot import is_snapshot_due, create_snapshot
        if is_snapshot_due():
            logger.info("U2C snapshot due - creating automatically")
            result = create_snapshot()
            if result.get('success'):
                logger.info(
                    "U2C snapshot created: %s, %d milestones, $%.2f ACR",
                    result['fiscal_quarter'],
                    result['total_items'],
                    result['total_monthly_acr'],
                )
            else:
                logger.warning("U2C snapshot skipped: %s", result.get('error'))
    except Exception:
        logger.exception("Error checking U2C snapshot")


def start_milestone_sync_background(app):
    """Catch up on missed sync at startup. Fires once if sync is overdue.

    Args:
        app: Flask application instance.
    """
    with app.app_context():
        from app.models import UserPreference, SyncStatus
        pref = UserPreference.query.first()
        if not pref:
            return
        if not SyncStatus.is_complete('accounts'):
            logger.debug("Skipping milestone sync - first account sync not yet completed")
            return
        if not pref.milestone_auto_sync:
            logger.debug("Milestone auto-sync disabled in settings")
            return
        _ensure_sync_time(pref)
        if not _missed_sync(pref):
            logger.debug("Milestone sync not needed at startup")
            return

    logger.info("Milestone sync overdue, starting catchup")
    thread = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    thread.start()


def start_daily_milestone_scheduler(app):
    """Start a daemon thread that fires milestone sync at the stored time daily.

    Args:
        app: Flask application instance.
    """

    def _scheduler():
        logger.info("Milestone daily scheduler started")
        last_sync_date = None

        while True:
            try:
                should_run = False
                with app.app_context():
                    from app.models import UserPreference, SyncStatus
                    pref = UserPreference.query.first()
                    if not pref:
                        time.sleep(300)
                        continue

                    if not SyncStatus.is_complete('accounts'):
                        time.sleep(300)
                        continue

                    _ensure_sync_time(pref)

                    if not pref.milestone_auto_sync:
                        last_sync_date = datetime.now().date()
                        time.sleep(300)
                        continue

                    today = datetime.now().date()
                    if last_sync_date != today and _should_sync(pref):
                        should_run = True
                        last_sync_date = today

                if should_run:
                    logger.info("Daily scheduler triggering milestone sync")
                    _run_sync(app)

                time.sleep(300)
            except Exception:
                logger.exception("Error in milestone daily scheduler")
                time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
