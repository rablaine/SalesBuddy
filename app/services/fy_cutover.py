"""
Fiscal Year Cutover Service.

Manages the transition between fiscal years:
- Archive creation (local + optional OneDrive)
- FY transition mode (banner state)
- Alignment finalization (orphan purge)
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from app.models import (
    Customer,
    Engagement,
    Milestone,
    MsxTask,
    Note,
    Opportunity,
    POD,
    Seller,
    Territory,
    UserPreference,
    db,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transition state helpers
# ---------------------------------------------------------------------------


def get_transition_state() -> dict:
    """Return current FY transition state from UserPreference.

    Returns:
        Dict with keys: in_transition, fy_label, started_at, sync_complete.
    """
    pref = UserPreference.query.first()
    if not pref:
        return {"in_transition": False, "fy_label": None, "started_at": None, "sync_complete": False}
    return {
        "in_transition": pref.fy_transition_active,
        "fy_label": pref.fy_transition_label,
        "started_at": pref.fy_transition_started.isoformat() if pref.fy_transition_started else None,
        "sync_complete": pref.fy_sync_complete,
    }


def enter_transition_mode(fy_label: str) -> None:
    """Set the app into FY transition mode."""
    pref = UserPreference.query.first()
    if pref:
        pref.fy_transition_active = True
        pref.fy_transition_label = fy_label
        pref.fy_transition_started = datetime.now(timezone.utc)
        pref.fy_sync_complete = False
        db.session.commit()


def mark_fy_sync_complete() -> None:
    """Mark that the FY account sync has finished."""
    pref = UserPreference.query.first()
    if pref:
        pref.fy_sync_complete = True
        db.session.commit()


def exit_transition_mode() -> None:
    """Clear FY transition state after finalization."""
    pref = UserPreference.query.first()
    if pref:
        # Record which FY was completed before clearing
        if pref.fy_transition_label:
            pref.fy_last_completed = pref.fy_transition_label
        pref.fy_transition_active = False
        pref.fy_transition_label = None
        pref.fy_transition_started = None
        pref.fy_sync_complete = False
        db.session.commit()


# ---------------------------------------------------------------------------
# Archive creation
# ---------------------------------------------------------------------------


def _get_data_dir() -> Path:
    """Return the directory where notehelper.db lives."""
    db_url = str(db.engine.url)
    # sqlite:///data/notehelper.db -> data/notehelper.db
    if ":///" in db_url:
        db_path = db_url.split(":///", 1)[1]
    else:
        db_path = "data/notehelper.db"
    return Path(db_path).parent


def _get_onedrive_backup_root() -> Optional[str]:
    """Return OneDrive backup root if configured, else None."""
    try:
        from app.services.backup import _get_backup_root
        return _get_backup_root()
    except Exception:
        return None


def get_fiscal_year_labels() -> dict:
    """Compute current and next FY labels from today's date.

    MS fiscal year runs July 1 – June 30.
    FY is named for the calendar year it ends in (e.g. Jul 2025–Jun 2026 = FY26).

    For cutover naming, we give a grace period through September so that
    someone starting the process late still gets the correct labels
    (e.g. doing it in Sep 2026 still shows "Archive FY26 & Start FY27").

    Returns:
        Dict with current_fy and next_fy (e.g. {"current_fy": "FY26", "next_fy": "FY27"}).
    """
    now = datetime.now()
    # Oct–Dec: we're solidly in the new FY. Jan–Sep: still the prior FY for naming.
    current_fy = now.year + 1 if now.month >= 10 else now.year
    return {
        "current_fy": f"FY{current_fy % 100:02d}",
        "next_fy": f"FY{(current_fy + 1) % 100:02d}",
    }


def start_new_fiscal_year() -> dict:
    """Create archive of current DB and enter transition mode.

    FY labels are computed from today's date. The archive is named for the
    current (ending) FY, and transition mode is entered for the next FY.

    Returns:
        Dict with archive_path, onedrive_path, stats, current_fy, and next_fy.
    """
    labels = get_fiscal_year_labels()
    archive_label = labels["current_fy"]
    next_label = labels["next_fy"]

    data_dir = _get_data_dir()
    db_path = data_dir / "notehelper.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    # Create local archive named for the ending FY
    archive_path = data_dir / f"{archive_label}.db"
    shutil.copy2(str(db_path), str(archive_path))
    logger.info(f"Created local archive: {archive_path}")

    # Copy to OneDrive if available
    onedrive_path = None
    onedrive_root = _get_onedrive_backup_root()
    if onedrive_root:
        dest_dir = Path(onedrive_root) / "previous_years"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{archive_label}.db"
        shutil.copy2(str(db_path), str(dest))
        onedrive_path = str(dest)
        logger.info(f"Copied archive to OneDrive: {onedrive_path}")

    # Gather stats
    stats = {
        "customers": Customer.query.count(),
        "notes": Note.query.count(),
        "archive_size_mb": round(archive_path.stat().st_size / (1024 * 1024), 1),
    }

    # Enter transition mode for the new FY
    enter_transition_mode(next_label)

    return {
        "archive_path": str(archive_path),
        "onedrive_path": onedrive_path,
        "stats": stats,
        "current_fy": archive_label,
        "next_fy": next_label,
    }


# ---------------------------------------------------------------------------
# Archive listing
# ---------------------------------------------------------------------------


def list_archives() -> list[dict]:
    """List available FY archive files in the data directory.

    Returns:
        List of dicts with fy_label, path, size_mb, archived_at.
    """
    data_dir = _get_data_dir()
    archives = []
    for f in sorted(data_dir.glob("FY*.db")):
        stat = f.stat()
        archives.append({
            "fy_label": f.stem,
            "path": str(f),
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "archived_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return archives


# ---------------------------------------------------------------------------
# Finalize alignments (orphan purge)
# ---------------------------------------------------------------------------


def preview_purge(synced_tpids: list[int]) -> dict:
    """Preview what would be purged if we finalize with these TPIDs.

    Args:
        synced_tpids: List of TPIDs that are in the finalized alignment.

    Returns:
        Dict with counts of what would be kept vs purged.
    """
    all_customers = Customer.query.all()
    keep_ids = set()
    purge_ids = set()

    for c in all_customers:
        if c.tpid and c.tpid in synced_tpids:
            keep_ids.add(c.id)
        elif c.tpid:
            purge_ids.add(c.id)
        else:
            # Customers without TPID are kept (manually created)
            keep_ids.add(c.id)

    # Count related records that would be purged
    purge_notes = Note.query.filter(Note.customer_id.in_(purge_ids)).count() if purge_ids else 0
    purge_engagements = Engagement.query.filter(Engagement.customer_id.in_(purge_ids)).count() if purge_ids else 0
    purge_milestones = Milestone.query.filter(Milestone.customer_id.in_(purge_ids)).count() if purge_ids else 0

    return {
        "kept_customers": len(keep_ids),
        "purge_customers": len(purge_ids),
        "purge_notes": purge_notes,
        "purge_engagements": purge_engagements,
        "purge_milestones": purge_milestones,
    }


def finalize_alignments(synced_tpids: list[int]) -> dict:
    """Purge orphaned customers and their related data.

    Customers whose TPID is NOT in synced_tpids are deleted along with
    all their cascade-dependent records. Customers without a TPID
    (manually created) are kept.

    After customer purge, clean up empty org entities:
    sellers, territories, and PODs with zero remaining customers.

    Args:
        synced_tpids: List of TPIDs from the finalized MSX sync.

    Returns:
        Summary dict with counts of kept/purged records.
    """
    all_customers = Customer.query.all()
    keep_ids = set()
    purge_customers = []

    for c in all_customers:
        if c.tpid and c.tpid not in synced_tpids:
            purge_customers.append(c)
        else:
            keep_ids.add(c.id)

    purge_count = len(purge_customers)
    purge_notes = 0
    purge_engagements = 0
    purge_milestones = 0
    purge_opportunities = 0
    purge_tasks = 0
    purge_revenue = 0

    # Delete orphaned customers and cascade
    for customer in purge_customers:
        # Count before deleting
        purge_notes += Note.query.filter_by(customer_id=customer.id).count()
        purge_engagements += Engagement.query.filter_by(customer_id=customer.id).count()
        purge_opportunities += Opportunity.query.filter_by(customer_id=customer.id).count()

        milestones = Milestone.query.filter_by(customer_id=customer.id).all()
        purge_milestones += len(milestones)
        for m in milestones:
            purge_tasks += MsxTask.query.filter_by(milestone_id=m.id).count()
            MsxTask.query.filter_by(milestone_id=m.id).delete()

        Milestone.query.filter_by(customer_id=customer.id).delete()
        Engagement.query.filter_by(customer_id=customer.id).delete()
        Note.query.filter_by(customer_id=customer.id).delete()
        Opportunity.query.filter_by(customer_id=customer.id).delete()

        # Delete revenue data via raw SQL (tables may not exist in all environments)
        cid = customer.id
        try:
            re_count = db.session.execute(text("SELECT COUNT(*) FROM revenue_engagements WHERE analysis_id IN (SELECT id FROM revenue_analysis WHERE customer_id = :cid)"), {"cid": cid}).scalar() or 0
            ra_count = db.session.execute(text("SELECT COUNT(*) FROM revenue_analysis WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            pr_count = db.session.execute(text("SELECT COUNT(*) FROM product_revenue_data WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            cr_count = db.session.execute(text("SELECT COUNT(*) FROM customer_revenue_data WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            purge_revenue += re_count + ra_count + pr_count + cr_count

            db.session.execute(text("DELETE FROM revenue_engagements WHERE analysis_id IN (SELECT id FROM revenue_analysis WHERE customer_id = :cid)"), {"cid": cid})
            db.session.execute(text("DELETE FROM revenue_analysis WHERE customer_id = :cid"), {"cid": cid})
            db.session.execute(text("DELETE FROM product_revenue_data WHERE customer_id = :cid"), {"cid": cid})
            db.session.execute(text("DELETE FROM customer_revenue_data WHERE customer_id = :cid"), {"cid": cid})
        except Exception:
            pass  # Revenue tables may not exist yet

        db.session.delete(customer)

    # Clean up empty org entities
    purge_sellers = 0
    for seller in Seller.query.all():
        if len(list(seller.customers)) == 0:
            db.session.delete(seller)
            purge_sellers += 1

    purge_territories = 0
    for territory in Territory.query.all():
        if len(list(territory.customers)) == 0:
            db.session.delete(territory)
            purge_territories += 1

    purge_pods = 0
    for pod in POD.query.all():
        if len(list(pod.territories)) == 0:
            db.session.delete(pod)
            purge_pods += 1

    db.session.commit()

    # Exit transition mode
    exit_transition_mode()

    summary = {
        "kept_customers": len(keep_ids),
        "purged_customers": purge_count,
        "purged_notes": purge_notes,
        "purged_engagements": purge_engagements,
        "purged_milestones": purge_milestones,
        "purged_opportunities": purge_opportunities,
        "purged_tasks": purge_tasks,
        "purged_revenue": purge_revenue,
        "purged_sellers": purge_sellers,
        "purged_territories": purge_territories,
        "purged_pods": purge_pods,
    }
    logger.info(f"FY finalization complete: {summary}")
    return summary
