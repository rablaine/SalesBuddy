"""Milestone audit history service.

Fetches audit records from MSX (Dynamics 365) to determine when milestones
transitioned to Committed or Completed status. Stores the dates in the
committed_at and completed_at columns on the Milestone model.

This is NOT part of the core milestone sync - it runs on-demand from the
1:1 report page because it requires an extra API call per milestone.

Audit changedata format (JSON, verified against real MSX data 2026-03-24):
    {"changedAttributes": [
        {"logicalName": "msp_milestonestatus",
         "oldValue": "861980000", "newValue": "861980003",
         "oldName": "On Track", "newName": "Completed"},
        ...
    ]}

Key fields:
    msp_milestonestatus - status changes (newName: "Completed", "On Track", etc.)
    msp_commitmentrecommendation - commitment (newName: "Committed", "Uncommitted")
    msp_completedon - explicit completed timestamp (newValue: "03/23/2026 20:42:14")
    msp_committedon - explicit committed timestamp (newValue: "11/11/2025 20:55:08")
"""
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from app.models import db, Milestone
from app.services.msx_api import get_milestone_audit_history

logger = logging.getLogger(__name__)

# Dynamics date format used in msp_committedon / msp_completedon values
MSX_DATE_FORMAT = "%m/%d/%Y %H:%M:%S"


def _parse_audit_changedata(changedata: str) -> List[Dict[str, Any]]:
    """Parse the changedata JSON from a Dynamics 365 audit record.

    Args:
        changedata: JSON string from the audit record's changedata field.

    Returns:
        List of dicts, each with 'logicalName', 'oldValue', 'newValue',
        and optionally 'oldName', 'newName' (human-readable option labels).
    """
    if not changedata:
        return []

    try:
        data = json.loads(changedata)
    except (json.JSONDecodeError, TypeError):
        logger.debug("Could not parse audit changedata as JSON: %s", changedata[:200])
        return []

    return data.get("changedAttributes", [])


def _parse_msx_date(value: str) -> Optional[datetime]:
    """Parse an MSX timestamp like '03/23/2026 20:42:14' to a UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, MSX_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _parse_iso_date(value: str) -> Optional[datetime]:
    """Parse an ISO 8601 datetime like '2026-03-23T20:42:15.843Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_dates_from_audit(
    audit_records: List[Dict[str, Any]],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Walk audit records (newest first) and find the most recent transition dates.

    Strategy (in priority order):
    1. Look for msp_committedon / msp_completedon fields being set - these contain
       the exact timestamps MSX recorded for the transition.
    2. Fall back to msp_commitmentrecommendation / msp_milestonestatus changes and
       use the audit record's createdon as the transition timestamp.

    Since status can flip back and forth, we only care about the MOST RECENT
    transition TO the target status.

    Returns:
        (committed_at, completed_at) as UTC datetimes or None.
    """
    committed_at = None
    completed_at = None

    # Records are sorted newest-first from the API
    for record in audit_records:
        changedata = record.get("changedata", "")
        changes = _parse_audit_changedata(changedata)
        record_date = _parse_iso_date(record.get("createdon"))

        for attr in changes:
            field = attr.get("logicalName", "")

            # Priority 1: explicit timestamp fields
            if not committed_at and field == "msp_committedon":
                new_val = attr.get("newValue")
                if new_val:
                    committed_at = _parse_msx_date(new_val)

            if not completed_at and field == "msp_completedon":
                new_val = attr.get("newValue")
                if new_val:
                    completed_at = _parse_msx_date(new_val)

            # Priority 2: status/commitment option-set changes
            if not committed_at and field == "msp_commitmentrecommendation":
                if attr.get("newName") == "Committed":
                    committed_at = record_date

            if not completed_at and field == "msp_milestonestatus":
                if attr.get("newName") == "Completed":
                    completed_at = record_date

        # Stop early if we found both
        if committed_at and completed_at:
            break

    return committed_at, completed_at


def sync_milestone_audit_dates(
    progress_callback=None,
) -> Dict[str, Any]:
    """Fetch audit history for all on-my-team milestones and update dates.

    Queries MSX audit records for each milestone where on_my_team=True,
    extracts committed_at and completed_at from the change history,
    and stores them in the database.

    Only processes milestones that are currently Committed or Completed,
    since those are the only ones that would have relevant transitions.
    Clears dates on milestones that are no longer in those states (handles
    status flip-back).

    Args:
        progress_callback: Optional callable(current, total) for progress updates.

    Returns:
        Dict with success, counts, and any errors.
    """
    milestones = (
        Milestone.query
        .filter(Milestone.on_my_team == True)  # noqa: E712
        .filter(Milestone.msx_milestone_id.isnot(None))
        .all()
    )

    total = len(milestones)
    updated = 0
    cleared = 0
    errors = []

    for i, ms in enumerate(milestones):
        if progress_callback:
            progress_callback(i + 1, total)

        is_completed = ms.msx_status == 'Completed'
        is_committed = ms.customer_commitment == 'Committed'

        # If not currently in a target state, clear any stale dates
        if not is_completed and ms.completed_at:
            ms.completed_at = None
            cleared += 1
        if not is_committed and ms.committed_at:
            ms.committed_at = None
            cleared += 1

        # Only fetch audit history if currently in a target state and missing date
        needs_completed = is_completed and not ms.completed_at
        needs_committed = is_committed and not ms.committed_at

        if not needs_completed and not needs_committed:
            continue

        result = get_milestone_audit_history(ms.msx_milestone_id)
        if not result.get("success"):
            error_msg = result.get("error", "Unknown error")
            errors.append(f"{ms.display_text}: {error_msg}")
            logger.warning(
                "Audit fetch failed for milestone %s (%s): %s",
                ms.id, ms.msx_milestone_id, error_msg,
            )
            continue

        committed_date, completed_date = _extract_dates_from_audit(
            result.get("records", [])
        )

        if needs_committed and committed_date:
            ms.committed_at = committed_date
            updated += 1
        if needs_completed and completed_date:
            ms.completed_at = completed_date
            updated += 1

    db.session.commit()

    return {
        "success": True,
        "total_milestones": total,
        "dates_found": updated,
        "dates_cleared": cleared,
        "errors": errors,
    }
