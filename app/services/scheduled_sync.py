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
import errno
import os
from contextlib import contextmanager
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

            # Refresh revenue from MSXI. Chained here rather than given its own
            # timer, but rate-limited to weekly - ACR only moves monthly and a
            # full pull is far heavier than a milestone sync.
            _run_revenue_sync()

            # Refresh the official U2C baseline. Runs after the milestone sync
            # so freshly synced milestones can be matched to MSXi rows.
            _run_u2c_import()
    except Exception:
        logger.exception("Error during milestone sync")
    finally:
        _sync_lock.release()


# Revenue is refreshed at most this often, regardless of how often the
# milestone sync it rides along with runs.
REVENUE_SYNC_INTERVAL_DAYS = 7


def _revenue_sync_due() -> bool:
    """True when revenue has never synced or the last success is a week old.

    Keyed off SyncStatus rather than a fixed weekday so a machine that was off
    on the chosen day still catches up on its next run.
    """
    from app.models import SyncStatus

    status = SyncStatus.get_status('revenue_sync')
    completed = status.get('completed_at')
    if not completed:
        return True
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - completed).total_seconds() / 86400
    if age_days < REVENUE_SYNC_INTERVAL_DAYS:
        logger.debug("Revenue sync last ran %.1f days ago, skipping", age_days)
        return False
    return True


def _run_revenue_sync():
    """Pull revenue from MSXI in the current thread (already in app context)."""
    if not _revenue_sync_due():
        return
    try:
        from app.services.revenue_sync import sync_revenue
        logger.info("Starting revenue sync (weekly)")
        result = sync_revenue()
        if result.get('success'):
            logger.info(
                "Revenue sync complete: %d bucket rows, %d product rows, %d customers with data",
                result.get('bucket_rows', 0), result.get('product_rows', 0),
                result.get('customers_with_data', 0),
            )
        else:
            logger.error("Revenue sync failed: %s", result.get('error', 'Unknown'))
    except Exception:
        logger.exception("Error during revenue sync")


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


# The official U2C baseline is refreshed at most this often. MSXi publishes
# weekly, so daily is comfortably ahead of the data without being wasteful.
U2C_IMPORT_INTERVAL_HOURS = 20
# A failed attempt retries much sooner - the usual cause is the machine being
# off VPN or not yet signed in at boot, which fixes itself within the hour.
U2C_RETRY_INTERVAL_HOURS = 1
U2C_HEARTBEAT_INTERVAL_SECONDS = 30


@contextmanager
def _u2c_process_lock():
    """Acquire the cross-process lock guarding U2C snapshot writes."""
    from app.db_paths import resolve_data_dir

    lock_path = resolve_data_dir() / '.u2c-import.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+b')
    acquired = False
    try:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _u2c_import_due() -> bool:
    """True when the official U2C baseline is due for a refresh.

    Keyed off SyncStatus rather than a calendar so a machine that was off still
    catches up on its next run, exactly like ``_revenue_sync_due``.
    """
    from app.models import SyncStatus

    status = SyncStatus.get_status('u2c_import')
    state = status.get('state')
    if state == 'in_progress':
        return False
    completed = status.get('completed_at')
    if not completed:
        return True
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - completed).total_seconds() / 3600
    threshold = (U2C_RETRY_INTERVAL_HOURS if state == 'failed'
                 else U2C_IMPORT_INTERVAL_HOURS)
    if age_hours < threshold:
        logger.debug("U2C import last ran %.1fh ago (threshold %dh), skipping",
                     age_hours, threshold)
        return False
    return True


def run_u2c_import(force: bool = False) -> dict | None:
    """Run one U2C refresh while holding the cross-process write lock."""
    with _u2c_process_lock() as acquired:
        if not acquired:
            return {
                'success': False,
                'outcome': 'in_progress',
                'error': 'A U2C refresh is already in progress.',
            }
        return _run_u2c_import_locked(force)


def _run_u2c_import_locked(force: bool = False) -> dict | None:
    """Refresh the official U2C baseline and record its SyncStatus.

    Always re-matches the current snapshot against local milestones first -
    that's a free local pass, and it matters most right after a milestone sync.
    The MSXi refresh itself only runs when due unless ``force`` is true.
    """
    import json

    from app.models import SyncStatus
    from app.services.u2c_snapshot import (
        OUTCOME_BROKEN, refresh_official_snapshot, rematch_current_snapshot,
    )

    try:
        rematch_current_snapshot()
    except Exception:
        logger.exception("Error re-matching U2C snapshot items")

    if not force and not _u2c_import_due():
        return None

    if not SyncStatus.try_mark_started('u2c_import'):
        return {
            'success': False,
            'outcome': 'in_progress',
            'error': 'A U2C refresh is already in progress.',
        }

    from flask import current_app

    heartbeat_stop = threading.Event()
    flask_app = current_app._get_current_object()

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(U2C_HEARTBEAT_INTERVAL_SECONDS):
            try:
                with flask_app.app_context():
                    SyncStatus.update_heartbeat('u2c_import')
            except Exception:
                logger.exception("Error updating U2C import heartbeat")

    heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        result = refresh_official_snapshot()
    except Exception as exc:
        logger.exception("Error refreshing the official U2C baseline")
        result = {
            'success': False,
            'outcome': OUTCOME_BROKEN,
            'error': str(exc),
        }
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join()

    outcome = result.get('outcome')
    if result.get('success'):
        logger.info("U2C refresh (%s): %s, %s milestones, version %s",
                    outcome, result.get('fiscal_quarter'),
                    result.get('total_items'), result.get('msxi_version'))
    else:
        logger.warning("U2C refresh (%s) failed: %s", outcome, result.get('error'))

    SyncStatus.mark_completed(
        'u2c_import',
        success=bool(result.get('success')),
        items_synced=result.get('total_items'),
        details=json.dumps({
            'outcome': outcome,
            'fiscal_quarter': result.get('fiscal_quarter'),
            'msxi_version': result.get('msxi_version'),
            'matched_locally': result.get('matched_locally'),
            'versions_backfilled': result.get('versions_backfilled'),
            'error': result.get('error'),
        }),
    )
    return result


def _run_u2c_import() -> dict | None:
    """Run the scheduled U2C refresh when its cadence is due."""
    return run_u2c_import()


def run_u2c_import_if_due(app):
    """Refresh the official U2C baseline from outside an app context."""
    with app.app_context():
        _run_u2c_import()


def start_milestone_sync_background(app) -> threading.Thread | None:
    """Catch up on missed sync at startup. Fires once if sync is overdue.

    Args:
        app: Flask application instance.
    """
    with app.app_context():
        from app.models import UserPreference, SyncStatus
        pref = UserPreference.query.first()
        if not pref:
            return None
        if not SyncStatus.is_complete('accounts'):
            logger.debug("Skipping milestone sync - first account sync not yet completed")
            return None
        if not pref.milestone_auto_sync:
            logger.debug("Milestone auto-sync disabled in settings")
            return None
        _ensure_sync_time(pref)
        if not _missed_sync(pref):
            logger.debug("Milestone sync not needed at startup")
            return None

    logger.info("Milestone sync overdue, starting catchup")
    thread = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    thread.start()
    return thread


def _run_daily_scheduler_cycle(app, last_sync_date: date | None) -> date | None:
    """Run one scheduler decision without coupling U2C to milestone settings."""
    should_run_milestones = False
    with app.app_context():
        from app.models import UserPreference, SyncStatus

        pref = UserPreference.query.first()
        accounts_ready = SyncStatus.is_complete('accounts')
        if pref and accounts_ready and pref.milestone_auto_sync:
            _ensure_sync_time(pref)
            today = date.today()
            if last_sync_date != today and _should_sync(pref):
                should_run_milestones = True
                last_sync_date = today

    if should_run_milestones:
        logger.info("Daily scheduler triggering milestone sync")
        _run_sync(app)
    else:
        # U2C has its own cadence and only needs configured territories. It
        # must continue even when milestone auto-sync is disabled or accounts
        # have not completed their first sync.
        try:
            run_u2c_import_if_due(app)
        except Exception:
            logger.exception("Error running the daily U2C import")
    return last_sync_date


def start_daily_milestone_scheduler(
    app,
    startup_sync_thread: threading.Thread | None = None,
):
    """Start a daemon thread that fires milestone sync at the stored time daily.

    Args:
        app: Flask application instance.
        startup_sync_thread: Optional milestone catch-up thread to wait for
            before the independent U2C scheduler begins.
    """

    def _scheduler():
        logger.info("Milestone daily scheduler started")
        last_sync_date = None
        if startup_sync_thread is not None:
            # The catch-up path runs U2C after milestones. Waiting here keeps
            # the independent scheduler from racing ahead at startup.
            startup_sync_thread.join()

        while True:
            try:
                last_sync_date = _run_daily_scheduler_cycle(
                    app, last_sync_date)
                time.sleep(300)
            except Exception:
                logger.exception("Error in milestone daily scheduler")
                time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
