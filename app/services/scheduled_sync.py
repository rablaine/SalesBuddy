"""
Scheduled milestone, U2C and Fabric sync for Sales Buddy.

Runs milestone import on Mon/Wed/Fri using a background daemon thread.
The sync time is randomly assigned per-user (stored in UserPreference) in
5-minute slots between 9:30 AM and 4:30 PM to stagger MSX load across
Sales Buddy instances. If missed (server was off), catches up on startup.

No environment variables needed - everything is stored in the database.
Data SEs and sellers receive independent weekly Fabric and SQL refreshes with startup catch-up.
"""                                                                          
import random
import time
import threading
import logging
import errno
import os
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from flask import Flask

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
    return date.today().weekday() in SYNC_DAYS


def _last_sync_day() -> date:
    """Return the most recent Mon/Wed/Fri that is <= today."""
    today = date.today()
    for days_back in range(7):
        candidate = today - timedelta(days=days_back)
        if candidate.weekday() in SYNC_DAYS:
            return candidate
    return today  # fallback, shouldn't happen with MWF


def _local_now() -> datetime:
    """Return naive local wall time for comparison with the user's schedule."""
    return datetime.now(timezone.utc).astimezone().replace(tzinfo=None)


def _milestone_sync_due(since: date) -> bool:
    """Require a finished attempt on the due local date, without duplicating live work."""
    from app.models import SyncStatus

    if SyncStatus.get_status('milestones')['state'] == 'in_progress':
        return False
    completed = SyncStatus.last_completed_utc('milestones')
    return completed is None or completed.astimezone().date() < since


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

    now = _local_now()
    if now < target:
        return False

    return _milestone_sync_due(last_day)


def _should_sync(pref) -> bool:
    """Check if a milestone sync is needed now.

    Returns True if:
    - Today is a sync day (Mon/Wed/Fri), AND
    - We're past today's scheduled time, AND
    - No attempt finished today and no milestone-family sync is queued or running.

    Args:
        pref: UserPreference instance (must have sync_hour/minute set).
    """                                                                        
    if not _is_sync_day():
        return False
    hour, minute = pref.milestone_sync_hour, pref.milestone_sync_minute
    now = _local_now()
    today_sync_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Haven't reached today's sync time yet
    if now < today_sync_time:
        return False

    return _milestone_sync_due(date.today())


def _run_sync(app):
    """Admit scheduled milestone work without claiming it has already finished."""
    if not _sync_lock.acquire(blocking=False):
        logger.debug("Milestone sync already in progress, skipping")
        return
    try:
        with app.app_context():
            from app.services.sync_jobs import admit_sync

            job = admit_sync('milestones', initiator='scheduled')
            logger.info("Milestone sync admitted as job %s", job.id)

    except Exception:
        logger.exception("Error during milestone sync")
    finally:
        _sync_lock.release()


# Revenue is refreshed at most this often, regardless of how often the
# milestone sync it rides along with runs.
REVENUE_SYNC_INTERVAL_DAYS = 7
FABRIC_SYNC_INTERVAL_DAYS = 7
FABRIC_RETRY_INTERVAL_HOURS = 1
SQL_MOTIONS_SYNC_INTERVAL_DAYS = 7
SQL_MOTIONS_RETRY_INTERVAL_HOURS = 1


def _sql_motions_sync_due(now: datetime | None = None) -> bool:
    """Refresh the complete SQL snapshot weekly for Data SEs and every seller."""
    from app.models import Job, SqlMotionImport
    from app.services.se_specialty import is_data_se
    from app.services.sql_motions_sync import active_customers
    from app.services.sync_jobs import ACTIVE_STATES

    if not is_data_se() or not active_customers():
        return False
    runs = Job.query.filter_by(job_type='sync:sql-motions')
    if runs.filter(Job.status.in_(ACTIVE_STATES)).first():
        return False
    now = now or datetime.now(timezone.utc)
    latest_run = runs.order_by(Job.id.desc()).first()
    if latest_run and latest_run.status == Job.STATUS_FAILED:
        attempted = latest_run.finished_at or latest_run.started_at or latest_run.created_at
        attempted = attempted.replace(tzinfo=attempted.tzinfo or timezone.utc)
        if now - attempted < timedelta(hours=SQL_MOTIONS_RETRY_INTERVAL_HOURS):
            return False
    snapshot = SqlMotionImport.query.order_by(SqlMotionImport.id.desc()).first()
    if snapshot is None:
        return True
    imported = snapshot.imported_at.replace(tzinfo=snapshot.imported_at.tzinfo or timezone.utc)
    return now - imported >= timedelta(days=SQL_MOTIONS_SYNC_INTERVAL_DAYS)


def run_sql_motions_sync_if_due(app: Flask) -> dict | None:
    """Admit one weekly SQL operation through the shared durable resource owner."""
    from app.services.sync_jobs import admit_sync

    with app.app_context():
        if not _sql_motions_sync_due():
            return None
        job = admit_sync('sql-motions', initiator='scheduled')
        return {'success': True, 'accepted': True, 'job_id': job.id}


def start_sql_motions_sync_scheduler(app: Flask) -> threading.Thread:
    """Catch up missing SQL sources at startup, then recheck every five minutes."""
    def scheduler() -> None:
        """Recheck role, account scope and weekly freshness independently of Fabric."""
        while True:
            try:
                run_sql_motions_sync_if_due(app)
            except Exception:
                logger.exception('Error in SQL motions scheduler')
            time.sleep(300)

    thread = threading.Thread(target=scheduler, name='sql-motions-sync', daemon=True)
    thread.start()
    return thread


def _fabric_sync_due(now: datetime | None = None) -> bool:
    """Refresh stale or missing Fabric 360 sources for Data SEs and sellers."""
    from app.models import Customer, FabricConsumptionImport, PToFImport, SyncStatus
    from app.services.fabric_consumption import SYNC_TYPE
    from app.services.se_specialty import is_data_se

    if not is_data_se():
        return False
    if Customer.query.filter(Customer.tpid > 0).first() is None:
        logger.debug("Skipping automatic Fabric sync: no customers with valid TPIDs")
        return False
    status = SyncStatus.get_status(SYNC_TYPE)
    if status['state'] == 'in_progress':
        return False
    now = now or datetime.now(timezone.utc)
    last_attempt = status['completed_at'] or status['started_at']
    if status['state'] in ('failed', 'incomplete') and last_attempt:
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        if now - last_attempt < timedelta(hours=FABRIC_RETRY_INTERVAL_HOURS):
            return False
    for snapshot_model in (FabricConsumptionImport, PToFImport):
        snapshot = snapshot_model.query.order_by(
            snapshot_model.imported_at.desc(), snapshot_model.id.desc(),
        ).first()
        if snapshot is None:
            return True
        imported_at = snapshot.imported_at
        if imported_at.tzinfo is None:
            imported_at = imported_at.replace(tzinfo=timezone.utc)
        if now - imported_at >= timedelta(days=FABRIC_SYNC_INTERVAL_DAYS):
            return True
    return False


def run_fabric_sync_if_due(app: Flask) -> dict | None:
    """Run a due import using the same atomic claim and heartbeat as manual imports."""
    from app.services.sync_jobs import admit_sync

    with app.app_context():
        if not _fabric_sync_due():
            return None
        logger.info("Starting weekly Fabric 360 sync")
        job = admit_sync('fabric', initiator='scheduled')
        return {'success': True, 'accepted': True, 'job_id': job.id}


def start_fabric_sync_scheduler(app: Flask) -> threading.Thread:
    """Catch up at boot and check weekly freshness independently of milestone settings."""
    def scheduler() -> None:
        """Recheck the current specialty and saved snapshot every five minutes."""
        while True:
            try:
                run_fabric_sync_if_due(app)
            except Exception:
                logger.exception("Error in Fabric 360 scheduler")
            time.sleep(300)

    thread = threading.Thread(target=scheduler, name='fabric-sync', daemon=True)
    thread.start()
    return thread


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
        from app.services.sync_jobs import admit_sync
        logger.info("Starting revenue sync (weekly)")
        admit_sync('revenue', initiator='scheduled')
    except Exception:
        logger.exception("Error during revenue sync")


def _run_marketing_sync(app):
    """Run marketing insights sync in the current thread (already in app context)."""
    try:
        from app.services.sync_jobs import admit_sync
        logger.info("Starting marketing insights sync (post-milestone)")
        admit_sync('marketing', initiator='scheduled')
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


def _u2c_import_skip_reason() -> str | None:
    """Check readiness and cadence against the last actual source attempt."""
    from app.models import SyncStatus
    from app.services.u2c_pull import TERRITORIES_REQUIRED_ERROR, get_territory_codes

    if not get_territory_codes():
        return 'needs_territories'
    if SyncStatus.get_status('u2c_import').get('state') == 'in_progress':
        return 'in_progress'
    # Skipped queue jobs are not source attempts and must not reset the retry clock.
    status = SyncStatus.query.filter_by(sync_type='u2c_import').first()
    if status is None or status.completed_at is None:
        return None
    completed = status.completed_at
    if status.success is False and TERRITORIES_REQUIRED_ERROR in (status.details or ''):
        return None
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - completed).total_seconds() / 3600
    failed = status.success is False
    threshold = (U2C_RETRY_INTERVAL_HOURS if failed
                 else U2C_IMPORT_INTERVAL_HOURS)
    if age_hours < threshold:
        logger.debug("U2C import last ran %.1fh ago (threshold %dh), skipping",
                     age_hours, threshold)
        return 'retry_cooldown' if failed else 'up_to_date'
    return None


def _u2c_import_due() -> bool:
    """Refresh once territories are ready, respecting genuine failure cooldowns."""
    return _u2c_import_skip_reason() is None


def run_u2c_import(force: bool = False) -> dict:
    """Run one U2C refresh while holding the cross-process write lock."""
    with _u2c_process_lock() as acquired:
        if not acquired:
            return {
                'success': False,
                'outcome': 'in_progress',
                'error': 'A U2C refresh is already in progress.',
            }
        return _run_u2c_import_locked(force)


def _run_u2c_import_locked(force: bool = False) -> dict:
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
    from app.services.sync_jobs import publish_progress

    try:
        publish_progress({'phase': 'matching', 'message': 'Matching the saved U2C baseline.'})
        rematch_current_snapshot()
    except Exception:
        logger.exception("Error re-matching U2C snapshot items")
        raise

    reason = _u2c_import_skip_reason()
    if reason == 'needs_territories' or (not force and reason is not None):
        messages = {
            'needs_territories': 'Waiting for sales territories. Import accounts or configure '
                                 'your sales territories first.',
            'in_progress': 'A U2C refresh is already in progress.',
            'retry_cooldown': 'The previous U2C import failed. Automatic retry is waiting for '
                              f'the {U2C_RETRY_INTERVAL_HOURS}-hour cooldown. '
                              'You can retry manually now.',
            'up_to_date': 'The last U2C import succeeded and is not due for refresh yet.',
        }
        return {'success': True, 'skipped': True, 'reason': reason,
                'message': messages[reason]}

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

    from app.services.job_queue import CURRENT_SYNC_OWNER
    heartbeat_thread = None
    if CURRENT_SYNC_OWNER.get() is None:
        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

    try:
        publish_progress({'phase': 'query', 'message': 'Reading the official MSXI U2C baseline.'})
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
        if heartbeat_thread is not None:
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
    from app.services.sync_jobs import admit_sync

    if not _u2c_import_due():
        return None
    job = admit_sync('u2c', {'force': False}, initiator='scheduled')
    return {'success': True, 'accepted': True, 'job_id': job.id}


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


def _run_daily_scheduler_cycle(app: Flask) -> None:
    """Run one scheduler decision without coupling U2C to milestone settings."""
    should_run_milestones = False
    with app.app_context():
        from app.models import UserPreference, SyncStatus

        pref = UserPreference.query.first()
        accounts_ready = SyncStatus.is_complete('accounts')
        if pref and accounts_ready and pref.milestone_auto_sync:
            _ensure_sync_time(pref)
            if _should_sync(pref):
                should_run_milestones = True

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
        if startup_sync_thread is not None:
            # The catch-up path runs U2C after milestones. Waiting here keeps
            # the independent scheduler from racing ahead at startup.
            startup_sync_thread.join()

        while True:
            try:
                _run_daily_scheduler_cycle(app)
                time.sleep(300)
            except Exception:
                logger.exception("Error in milestone daily scheduler")
                time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
