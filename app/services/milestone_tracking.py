"""
Milestone comment tracking service.

Posts two types of comments to MSX milestones:

1. **Engagement Story** (pinned) — One per engagement-milestone link.
   Assembled from the engagement's 6 narrative fields.  Pinned to the top
   of the comments list via a far-future modifiedOn date (2099-01-01).
   Updated in-place whenever the engagement is saved.

2. **Call Summary** — One per note-milestone link.  AI-summarized to 2-4
   sentences via the gateway, including only new information not already
   present in the other milestone comments.  Updated in-place when the
   note is edited.  The comment's modifiedOn is set to the note's call_date
   so comments appear in chronological order.

Both comment types are matched for updates via a ref tag in the footer
(e.g. ``· note-42 ·`` or ``· eng-15 ·``).  The userId is set to
``{display name} via Sales Buddy`` so multiple Sales Buddy users can each
maintain their own comments on the same milestone.

All MSX and AI calls run in a background thread so the user isn't blocked.
Failures are queued as flash notifications shown on the next page load.
"""
import re
import logging
import threading
from collections import deque
from datetime import date, datetime, timezone

from markupsafe import Markup, escape

from app.models import db

logger = logging.getLogger(__name__)

# Ref-tag format embedded in comment footers for upsert matching
_NOTE_REF = "note-{id}"
_ENG_REF = "eng-{id}"

# Thread-safe queue for background task failure notifications.
# Drained by the before_request hook registered in app/__init__.py.
_notification_queue: deque[tuple[str, str]] = deque()


def drain_notifications() -> list[tuple[str, str]]:
    """Drain pending background notifications.

    Returns:
        List of (category, message) tuples suitable for ``flash()``.
    """
    notifications = []
    while _notification_queue:
        try:
            notifications.append(_notification_queue.popleft())
        except IndexError:
            break
    return notifications


def _notify_error(message: str, note_id: int | None = None) -> None:
    """Queue a warning notification for the next page load.

    If note_id is provided, appends a retry button link.
    """
    if note_id:
        retry_url = f"/notes/{int(note_id)}/retry-msx"
        html_msg = Markup(
            '{msg} <a href="{url}" class="alert-link" '
            'onclick="fetch(this.href,{{method:\'POST\'}})'
            '.then(()=>this.closest(\'.alert\').remove());return false;">'
            'Retry MSX sync</a>'
        ).format(msg=escape(message), url=retry_url)
        _notification_queue.append(("warning", html_msg))
    else:
        _notification_queue.append(("warning", message))


def _strip_html(html: str) -> str:
    """Strip HTML tags and return plain text."""
    return re.sub(r'<[^>]+>', '', html or '').strip()


# ── MSX upsert helper ───────────────────────────────────────────────────────

def _upsert_to_msx(
    msx_milestone_id: str,
    content: str,
    ref_tag: str,
    pin_to_top: bool = False,
    comment_date: str | None = None,
) -> dict | None:
    """Upsert a comment on an MSX milestone.

    Returns the result dict from upsert_milestone_comment, or None if no
    MSX ID is available.
    """
    if not msx_milestone_id:
        return None

    try:
        from app.services.msx_api import upsert_milestone_comment
        result = upsert_milestone_comment(
            msx_milestone_id, content, ref_tag,
            pin_to_top=pin_to_top, comment_date=comment_date,
        )
        if not result.get("success"):
            logger.warning(
                f"MSX comment upsert failed for milestone {msx_milestone_id}: "
                f"{result.get('error')}"
            )
        return result
    except Exception as e:
        logger.warning(f"MSX comment upsert failed for {msx_milestone_id}: {e}")
        return {"success": False, "error": str(e)}


# ── AI summarization ────────────────────────────────────────────────────────

def _ai_summarize_note(
    plain_text: str,
    customer_name: str,
    topics: str,
    existing_comments: list[str],
    note_id: int | None = None,
) -> str | None:
    """Call the AI gateway to summarize a call log for a milestone comment.

    Returns the summary string, or None if AI is unavailable or the note
    contains no new information beyond existing comments.
    """
    try:
        from app.gateway_client import gateway_call
        print(f"[milestone-tracking] AI: calling gateway /v1/summarize-note")
        result = gateway_call("/v1/summarize-note", {
            "call_notes": plain_text,
            "customer_name": customer_name,
            "topics": topics,
            "existing_comments": existing_comments,
        })
        print(f"[milestone-tracking] AI: gateway returned: {result}")
        if result.get("no_new_info"):
            print("[milestone-tracking] AI: note contains no new info for milestone comment")
            return None
        summary = result.get("summary", "").strip()
        return summary if summary else None
    except Exception as e:
        print(f"[milestone-tracking] AI FAILED: {e}")
        # Build a user-friendly message based on the error type
        from app.gateway_client import GatewayError
        if isinstance(e, GatewayError) and e.status_code:
            code = e.status_code
            if code == 503:
                friendly = "AI gateway is temporarily unavailable (restarting). Your note was saved but was not synced to MSX."
            elif code == 429:
                friendly = "AI gateway rate limit reached. Your note was saved but was not synced to MSX."
            elif code == 502:
                friendly = "AI gateway failed to start. Your note was saved but was not synced to MSX."
            elif code == 401 or code == 403:
                friendly = "AI gateway authentication failed. Check your az login session."
            else:
                friendly = f"AI gateway returned an error ({code}). Your note was saved but was not synced to MSX."
        else:
            friendly = f"Could not reach AI gateway: {type(e).__name__}. Your note was saved but was not synced to MSX."
        _notify_error(friendly, note_id=note_id)
        return None


def _build_note_fallback(topics: str) -> str:
    """Build a metadata-only comment when AI is unavailable."""
    if topics and topics != 'None':
        return f"Topics: {topics}"
    return "(Note linked — summary pending)"


# ── Engagement story template ───────────────────────────────────────────────

def _build_engagement_story(engagement) -> str:
    """Assemble the engagement story comment from structured fields.

    Uses the engagement's 6 narrative fields to build a readable summary
    that mirrors the story prompts from the engagement view page.
    """
    parts = [f"Engagement Overview: {engagement.title} [{engagement.status}]"]

    if engagement.key_individuals:
        parts.append(f"\nI've been working with {_strip_html(engagement.key_individuals)}.")

    if engagement.technical_problem:
        parts.append(
            f"They have run into {_strip_html(engagement.technical_problem)}"
        )

    if engagement.business_impact:
        parts.append(
            f"It's impacting {_strip_html(engagement.business_impact)}"
        )

    if engagement.solution_resources:
        parts.append(
            f"We are addressing the opportunity with "
            f"{_strip_html(engagement.solution_resources)}."
        )

    acr = engagement.estimated_acr
    target = engagement.target_date
    if acr and target:
        target_str = target.strftime('%b %Y') if isinstance(target, date) else str(target)
        parts.append(f"This will result in {acr} by {target_str}.")
    elif acr:
        parts.append(f"This will result in {acr}.")
    elif target:
        target_str = target.strftime('%b %Y') if isinstance(target, date) else str(target)
        parts.append(f"Target date: {target_str}.")

    return "\n".join(parts)


def _add_footer(content: str, ref_tag: str) -> str:
    """Append a Date Updated line and Sales Buddy ref-tag footer."""
    updated = datetime.now(timezone.utc).strftime('%b %d, %Y')
    return f"{content}\n\nDate Updated: {updated}\n· {ref_tag} ·"


# ── Background workers ──────────────────────────────────────────────────────

def _track_note_worker(
    milestones_data: list[dict],
    plain: str,
    customer_name: str,
    topics: str,
    ref_tag: str,
    call_date_iso: str,
    note_id: int | None = None,
) -> None:
    """Background thread worker for note milestone tracking."""
    print(f"[milestone-tracking] worker started: {ref_tag}, {len(milestones_data)} milestone(s)")
    for ms in milestones_data:
        msx_id = ms["msx_milestone_id"]
        try:
            # Read existing comments for AI dedup context (no write)
            from app.services.msx_api import get_milestone_comments
            print(f"[milestone-tracking] reading existing comments from {msx_id}")
            read_result = get_milestone_comments(msx_id)
            raw_comments = (read_result or {}).get("comments", [])
            existing_comments = [c.get("comment", "") for c in raw_comments]

            # Check if this note already has a comment on this milestone
            ref_marker = f"· {ref_tag} ·"
            has_existing_post = any(ref_marker in c for c in existing_comments)

            # AI summarization with dedup context
            print(f"[milestone-tracking] calling AI summarize...")
            ai_summary = _ai_summarize_note(
                plain, customer_name, topics, existing_comments,
                note_id=note_id,
            )
            print(f"[milestone-tracking] AI result: {'got summary' if ai_summary else 'None (skip MSX write)'}")

            if ai_summary:
                content_with_footer = _add_footer(ai_summary, ref_tag)
                _upsert_to_msx(
                    msx_id, content_with_footer, ref_tag,
                    comment_date=call_date_iso,
                )
                print(f"[milestone-tracking] AI summary upserted to {msx_id}")
            elif not has_existing_post:
                # First-time sync for this note - AI said no new info but we
                # have never posted for this note before. Create a minimal
                # comment so the note is represented on the milestone.
                print(
                    f"[milestone-tracking] no AI summary but no existing post "
                    f"for {ref_tag} on {msx_id}, creating initial comment"
                )
                fallback = (
                    f"Call log: {customer_name}\n"
                    f"Topics: {topics}\n\n"
                    f"{plain[:500]}"
                )
                content_with_footer = _add_footer(fallback, ref_tag)
                _upsert_to_msx(
                    msx_id, content_with_footer, ref_tag,
                    comment_date=call_date_iso,
                )
                print(f"[milestone-tracking] fallback comment created on {msx_id}")
            else:
                print(
                    f"[milestone-tracking] no AI summary for {ref_tag} on {msx_id}, "
                    "existing post found - no update needed"
                )
        except Exception as e:
            print(f"[milestone-tracking] EXCEPTION for {msx_id}: {e}")
            _notify_error(
                "Failed to update milestone comment in MSX.",
                note_id=note_id,
            )


def _track_engagement_worker(
    milestones_data: list[dict],
    content: str,
    ref_tag: str,
) -> None:
    """Background thread worker for engagement milestone tracking."""
    print(f"[milestone-tracking] engagement worker started: {ref_tag}, {len(milestones_data)} milestone(s)")
    for ms in milestones_data:
        msx_id = ms["msx_milestone_id"]
        try:
            print(f"[milestone-tracking] upserting engagement story to {msx_id}")
            result = _upsert_to_msx(msx_id, content, ref_tag, pin_to_top=True)
            print(f"[milestone-tracking] engagement upsert result: success={result and result.get('success')}")
        except Exception as e:
            print(f"[milestone-tracking] EXCEPTION for engagement on {msx_id}: {e}")
            _notify_error(f"Background engagement sync: Failed to update milestone story in MSX.")


# ── Public API ───────────────────────────────────────────────────────────────

def track_note_on_milestones(note, background: bool = True) -> list[dict] | None:
    """Post or update a call summary comment on each linked milestone.

    For each milestone linked to the note:
    1. Reads existing comments from MSX for AI dedup context
    2. Calls AI to summarize only new info from this call
    3. Only writes to MSX if AI produces a summary
    4. If AI fails, notifies user with a retry option — nothing is written

    When ``background=True`` (default), work runs in a daemon thread
    and this function returns immediately.  Set ``background=False``
    for synchronous execution (used in tests).

    Returns:
        None when background=True, or list of result dicts when synchronous.
    """
    if not note.milestones:
        print(f"[milestone-tracking] note {note.id}: no milestones linked, skipping")
        return [] if not background else None

    # Extract all ORM data before potentially leaving the request context
    customer_name = note.customer.name if note.customer else 'General'
    topics = ', '.join(t.name for t in note.topics[:5]) if note.topics else 'None'
    plain = _strip_html(note.content)
    ref_tag = _NOTE_REF.format(id=note.id)
    call_date_iso = note.call_date.strftime('%Y-%m-%dT00:00:00.000Z')

    milestones_data = [
        {"msx_milestone_id": m.msx_milestone_id, "milestone_id": m.id}
        for m in note.milestones
        if m.msx_milestone_id
    ]
    print(f"[milestone-tracking] note {note.id}: {len(milestones_data)} milestones with MSX IDs")

    if not milestones_data:
        print(f"[milestone-tracking] note {note.id}: milestones exist but none have MSX IDs")
        if not background:
            return [{"milestone_id": m.id, "msx_result": None, "ai_used": False}
                    for m in note.milestones]
        return None

    note_id = note.id

    if background:
        print(f"[milestone-tracking] note {note_id}: spawning background thread")
        thread = threading.Thread(
            target=_track_note_worker,
            args=(milestones_data, plain, customer_name, topics, ref_tag, call_date_iso, note_id),
            daemon=True,
        )
        thread.start()
        return None

    # Synchronous path (for testing)
    _track_note_worker(milestones_data, plain, customer_name, topics, ref_tag, call_date_iso, note_id)
    return [{"milestone_id": ms["milestone_id"], "msx_result": None, "ai_used": False}
            for ms in milestones_data]


def track_engagement_on_milestones(engagement, background: bool = True) -> list[dict] | None:
    """Post or update the engagement story comment on each linked milestone.

    The story is pinned to the top (modifiedOn = 2099-01-01) and updated
    in-place when engagement fields change.

    When ``background=True`` (default), work runs in a daemon thread.

    Returns:
        None when background=True, or list of result dicts when synchronous.
    """
    if not engagement.milestones:
        print(f"[milestone-tracking] engagement {engagement.id}: no milestones linked, skipping")
        return [] if not background else None

    # Skip if no story fields are filled out yet (just title/status isn't enough)
    has_story = any([
        engagement.key_individuals,
        engagement.technical_problem,
        engagement.business_impact,
        engagement.solution_resources,
        engagement.estimated_acr,
        engagement.target_date,
    ])
    if not has_story:
        print(f"[milestone-tracking] engagement {engagement.id}: no story fields populated, skipping MSX write")
        return [] if not background else None

    story = _build_engagement_story(engagement)
    ref_tag = _ENG_REF.format(id=engagement.id)
    print(f"[milestone-tracking] engagement {engagement.id}: {len(engagement.milestones)} milestones, ref={ref_tag}")
    content = _add_footer(story, ref_tag)

    milestones_data = [
        {"msx_milestone_id": m.msx_milestone_id, "milestone_id": m.id}
        for m in engagement.milestones
        if m.msx_milestone_id
    ]

    if not milestones_data:
        if not background:
            return [{"milestone_id": m.id, "msx_result": None}
                    for m in engagement.milestones]
        return None

    if background:
        thread = threading.Thread(
            target=_track_engagement_worker,
            args=(milestones_data, content, ref_tag),
            daemon=True,
        )
        thread.start()
        return None

    # Synchronous path (for testing)
    _track_engagement_worker(milestones_data, content, ref_tag)
    return [{"milestone_id": ms["milestone_id"], "msx_result": None}
            for ms in milestones_data]
