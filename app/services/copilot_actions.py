"""Copilot daily action items service.

Queries WorkIQ for the user's top action items based on emails, chats, and
meetings. Parses the markdown response into structured action items and
stores them in the ActionItem table with source='copilot'.

Lifecycle:
- On sync, all existing copilot action items are deleted (completed or not)
- New items from the WorkIQ response replace them
- Sync runs at 6 AM local time daily, or on first startup after 6 AM
"""
import logging
import re
import threading
from datetime import datetime, date, time, timedelta, timezone

from app.models import db, ActionItem, UserPreference

logger = logging.getLogger(__name__)

_COPILOT_PROMPT_BASE = (
    "Look through all my emails, chats, and meetings from the last 7 days, "
    "and let me know the top three things I still need to get done. "
    "Only include items that are still unresolved or have not been responded to yet. "
    "{exclusions}"
    "Return ONLY a JSON array with objects containing: "
    '"title", "description", "source_url" (a Teams or Outlook link if available), '
    '"last_activity_date" (YYYY-MM-DD of the most recent email/chat/meeting about this). '
    "No markdown, no explanation, just the JSON array."
)

# Hour (local time) when the daily sync fires
SYNC_HOUR = 6

# Prevent concurrent syncs (startup + scheduler race)
_sync_lock = threading.Lock()


def _build_prompt() -> str:
    """Build the Copilot prompt, excluding recently completed items.

    Queries ActionItems completed in the last 7 days with source='copilot'
    and appends them as exclusions to the prompt.
    """
    exclusions = ""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        completed = ActionItem.query.filter(
            ActionItem.source == 'copilot',
            ActionItem.status == 'completed',
            ActionItem.completed_at >= cutoff,
        ).all()
        if completed:
            titles = [f'"{item.title}"' for item in completed]
            exclusions = (
                "Do NOT include these items because I already completed them: "
                + ", ".join(titles) + ". "
            )
    except Exception:
        logger.debug("Could not query completed items for exclusion", exc_info=True)

    return _COPILOT_PROMPT_BASE.format(exclusions=exclusions)


def parse_action_items(response: str) -> list[dict]:
    """Parse WorkIQ response into structured action items.

    Expects a JSON array from the prompt. Falls back to extracting
    JSON from markdown code blocks if WorkIQ wraps it.

    Args:
        response: Raw string from WorkIQ (should be JSON).

    Returns:
        List of dicts with keys: title, description, source_url.
    """
    if not response or not response.strip():
        return []

    text = response.strip()

    # Try to extract JSON from markdown code block if wrapped
    code_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if code_match:
        text = code_match.group(1)

    # Find the JSON array in the response
    arr_start = text.find('[')
    arr_end = text.rfind(']')
    if arr_start == -1 or arr_end == -1 or arr_end <= arr_start:
        return []

    try:
        import json
        items = json.loads(text[arr_start:arr_end + 1])
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(items, list):
        return []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get('title') or '').strip()
        if not title:
            continue
        result.append({
            'title': title[:300],
            'description': (item.get('description') or '').strip() or None,
            'source_url': (item.get('source_url') or '').strip() or None,
        })

    return result


def sync_copilot_action_items(app=None) -> dict:
    """Run the Copilot daily action items sync.

    1. Query WorkIQ for top action items
    2. Parse the response
    3. Delete all existing copilot action items
    4. Create new ones from the parsed response
    5. Update last_copilot_sync timestamp

    Args:
        app: Flask app instance (required for background thread context).

    Returns:
        Dict with success, items_created, and optional error.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.info("Copilot sync already in progress, skipping")
        return {"success": False, "error": "Sync already in progress"}

    from app.services.workiq_service import query_workiq

    def _do_sync():
        logger.info("Starting Copilot action items sync")
        try:
            # Build prompt with exclusions from recently completed items
            prompt = _build_prompt()
            logger.info("Copilot prompt: %s", prompt[:200])

            # Query WorkIQ (this takes ~30-60 seconds)
            response = query_workiq(prompt, timeout=120)
            if not response or not response.strip():
                logger.warning("WorkIQ returned empty response for Copilot action items")
                return {"success": False, "error": "Empty response from WorkIQ"}

            # Parse response
            parsed = parse_action_items(response)
            if not parsed:
                logger.warning("Could not parse any action items from WorkIQ response. "
                               "First 500 chars: %s", response[:500])
                return {"success": False, "error": "No action items parsed"}

            # Delete open copilot items (keep completed ones for exclusion list)
            deleted = ActionItem.query.filter_by(source='copilot', status='open').delete()
            # Also clean up completed copilot items older than 7 days
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            stale_completed = ActionItem.query.filter(
                ActionItem.source == 'copilot',
                ActionItem.status == 'completed',
                ActionItem.completed_at < cutoff,
            ).delete()
            logger.info("Deleted %d open + %d stale completed copilot items", deleted, stale_completed)

            # Create new ones
            for item in parsed:
                ai = ActionItem(
                    title=item['title'],
                    description=item['description'],
                    source='copilot',
                    source_url=item.get('source_url'),
                    status='open',
                    priority='normal',
                )
                db.session.add(ai)

            # Update sync timestamp
            pref = UserPreference.query.first()
            if pref:
                pref.last_copilot_sync = datetime.now(timezone.utc)

            db.session.commit()
            logger.info("Copilot action items sync complete: %d items created", len(parsed))
            return {"success": True, "items_created": len(parsed)}

        except Exception as e:
            db.session.rollback()
            logger.exception("Copilot action items sync failed")
            return {"success": False, "error": str(e)}

    if app:
        with app.app_context():
            try:
                return _do_sync()
            finally:
                _sync_lock.release()
    else:
        try:
            return _do_sync()
        finally:
            _sync_lock.release()


def should_sync() -> bool:
    """Check if a Copilot action items sync is needed.

    Returns True if:
    - Never synced before, OR
    - Last sync was before 6 AM today (local time)

    Must be called inside an app context.
    """
    pref = UserPreference.query.first()
    if not pref or not pref.last_copilot_sync:
        return True

    now = datetime.now()
    today_sync_time = datetime.combine(now.date(), time(SYNC_HOUR, 0))

    # If it's before 6 AM, the sync window hasn't opened yet today
    if now < today_sync_time:
        return False

    # last_copilot_sync is stored as UTC - convert to local for comparison
    last_sync = pref.last_copilot_sync
    if last_sync.tzinfo:
        last_sync_local = last_sync.astimezone().replace(tzinfo=None)
    else:
        # Stored as naive UTC - assume UTC and convert
        last_sync_local = last_sync.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

    return last_sync_local < today_sync_time


def start_copilot_sync_background(app) -> None:
    """Trigger a Copilot action items sync in a background thread.

    Checks should_sync() first. Does nothing if sync is not needed.

    Args:
        app: Flask app instance.
    """
    with app.app_context():
        pref = UserPreference.query.first()
        if pref and not pref.copilot_actions_enabled:
            logger.debug("Copilot action items disabled in settings")
            return
        if not should_sync():
            logger.debug("Copilot action items sync not needed")
            return

    logger.info("Copilot action items sync needed, starting background thread")
    thread = threading.Thread(
        target=sync_copilot_action_items,
        kwargs={'app': app},
        daemon=True,
    )
    thread.start()


def start_daily_scheduler(app) -> None:
    """Start a background thread that fires the Copilot sync at SYNC_HOUR daily.

    Runs in a loop, sleeping until the next sync window. Safe to call once
    at app startup.

    Args:
        app: Flask app instance.
    """
    import time as _time

    def _scheduler():
        logger.info("Copilot daily scheduler started (sync hour: %d:00 local)", SYNC_HOUR)
        last_sync_date = None

        while True:
            try:
                now = datetime.now()
                today = now.date()

                if now.hour >= SYNC_HOUR and last_sync_date != today:
                    with app.app_context():
                        pref = UserPreference.query.first()
                        if pref and not pref.copilot_actions_enabled:
                            last_sync_date = today
                            continue
                        if should_sync():
                            logger.info("Daily scheduler triggering Copilot sync")
                            sync_copilot_action_items(app=app)
                    last_sync_date = today

                # Sleep 5 minutes between checks
                _time.sleep(300)
            except Exception:
                logger.exception("Error in Copilot daily scheduler")
                _time.sleep(300)

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()
