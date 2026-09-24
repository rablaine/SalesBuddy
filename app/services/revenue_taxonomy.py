"""
Revenue bucket taxonomy transitions.

MSXI reworks its ``ServiceCompGrouping`` buckets at the fiscal-year boundary.
FY27 renamed and retired most of the FY26 set (``Core DBs`` and ``Modern DBs``
became ``Databases``, ``Analytics`` split into ``Fabric`` and
``Rest of Analytics``, the whole ``PRACR - *`` family retired, and
``Github Copilot`` gained a capital H).

Revenue rows stored against retired buckets cannot be reconciled, so a taxonomy
change forces a purge and re-import. This module owns the safety around that:

- detect the change and tell the user what happened to their selection
- archive user-authored review notes to JSON before anything is deleted
- restore the notes that still make sense afterwards
- purge revenue data without touching the user's configured thresholds
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.models import (
    db, Customer, CustomerRevenueData, ProductRevenueData, RevenueAnalysis,
    RevenueReviewNote, UserPreference,
)

logger = logging.getLogger(__name__)


def _norm(value: Optional[str]) -> str:
    """Case-insensitive, whitespace-tolerant key for comparing bucket names.

    Bucket renames are sometimes capitalization-only (``Github Copilot`` ->
    ``GitHub Copilot``); a case-sensitive comparison would report those as
    retired and throw away the user's notes.
    """
    return (value or "").strip().lower()


# ---------------------------------------------------------------------------
# Bucket reconciliation
# ---------------------------------------------------------------------------
def stored_buckets() -> set[str]:
    """Distinct buckets currently represented in imported revenue data."""
    rows = db.session.query(CustomerRevenueData.bucket).distinct().all()
    return {b for (b,) in rows if b and b.strip()}


def get_selected_buckets() -> list[str]:
    """The user's compensated-bucket selection (DB copy)."""
    pref = UserPreference.query.first()
    if not pref or not pref.compensated_buckets:
        return []
    try:
        value = json.loads(pref.compensated_buckets)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def reconcile_buckets(new_buckets: set[str]) -> dict[str, Any]:
    """Compare the incoming bucket list against what is stored and selected.

    Returns a notice dict describing the outcome:

    - ``unchanged``  - taxonomy matches, nothing to do
    - ``first_import`` - no prior data to compare against
    - ``review``     - taxonomy changed but the user's picks all survived
    - ``reset``      - some of the user's picks are gone; selection cleared

    Does not write anything. Call :func:`apply_bucket_notice` to persist.
    """
    new_clean = {b for b in new_buckets if b and b.strip()}
    old = stored_buckets()
    selected = get_selected_buckets()

    if not old:
        return {"status": "first_import", "added": sorted(new_clean),
                "removed": [], "missing_selected": [], "selected": selected,
                "new_buckets": sorted(new_clean)}

    new_keys = {_norm(b) for b in new_clean}
    old_keys = {_norm(b) for b in old}
    if new_keys == old_keys:
        return {"status": "unchanged", "added": [], "removed": [],
                "missing_selected": [], "selected": selected,
                "new_buckets": sorted(new_clean)}

    added = sorted(b for b in new_clean if _norm(b) not in old_keys)
    removed = sorted(b for b in old if _norm(b) not in new_keys)
    missing_selected = sorted(b for b in selected if _norm(b) not in new_keys)

    return {
        "status": "reset" if missing_selected else "review",
        "added": added,
        "removed": removed,
        "missing_selected": missing_selected,
        "selected": selected,
        "new_buckets": sorted(new_clean),
    }


def apply_bucket_notice(notice: dict[str, Any]) -> None:
    """Persist the reconciliation outcome and, on a reset, clear the selection.

    Bumping ``bucket_taxonomy_version`` is what invalidates the client-side
    ``localStorage`` copy; clearing only the DB value would let a stale cached
    selection silently win on the next page load.
    """
    if notice.get("status") in ("unchanged", "first_import"):
        return
    pref = UserPreference.query.first()
    if not pref:
        return
    if notice["status"] == "reset":
        pref.compensated_buckets = None
        pref.compensated_buckets_fiscal_year = None
        pref.compensated_buckets_confirmed_taxonomy_version = None
    pref.bucket_taxonomy_version = (pref.bucket_taxonomy_version or 0) + 1
    pref.bucket_taxonomy_notice = json.dumps({
        **notice,
        "noticed_at": datetime.now(timezone.utc).isoformat(),
    })
    db.session.commit()


# ---------------------------------------------------------------------------
# Review-note preservation
# ---------------------------------------------------------------------------
def _has_user_state(a: RevenueAnalysis) -> bool:
    return bool((a.review_notes or "").strip()) or (
        a.review_status not in (None, "", "new"))


def snapshot_review_state() -> list[dict[str, Any]]:
    """Capture every analysis row carrying user-authored review state."""
    out: list[dict[str, Any]] = []
    analyses = RevenueAnalysis.query.filter(
        db.or_(
            db.and_(RevenueAnalysis.review_notes.isnot(None), RevenueAnalysis.review_notes != ""),
            db.and_(RevenueAnalysis.review_status.isnot(None),
                    RevenueAnalysis.review_status.notin_(["new", ""])),
        )
    ).all()
    if not analyses:
        return out

    history: dict[int, list[dict[str, Any]]] = {}
    for note in RevenueReviewNote.query.filter(
            RevenueReviewNote.analysis_id.in_([a.id for a in analyses])).all():
        history.setdefault(note.analysis_id, []).append({
            "review_status": note.review_status,
            "review_notes": note.review_notes,
            "created_at": note.created_at.isoformat() if note.created_at else None,
        })

    for a in analyses:
        if not _has_user_state(a):
            continue
        out.append({
            "analysis_id": a.id,
            "customer_name": a.customer_name,
            "customer_id": a.customer_id,
            "tpid": a.tpid,
            "bucket": a.bucket,
            "review_status": a.review_status,
            "review_notes": a.review_notes,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
            "previous_review_status": a.previous_review_status,
            "previous_review_notes": a.previous_review_notes,
            "history": history.get(a.id, []),
        })
    return out


def archive_review_state(snapshot: list[dict[str, Any]], reason: str = "taxonomy_change") -> Optional[Path]:
    """Write the snapshot to JSON beside the database.

    Written unconditionally, even when everything restores cleanly, so there is
    always a rollback artifact. Lives in the resolved data dir (outside the
    install dir) so an upgrade cannot delete it.
    """
    from app.db_paths import resolve_data_dir

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        path = Path(resolve_data_dir()) / f"revenue_review_archive_{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "count": len(snapshot),
            "entries": snapshot,
        }, indent=2), encoding="utf-8")
        logger.info("revenue: archived %d review entries to %s", len(snapshot), path)
        return path
    except Exception as exc:  # noqa: BLE001 - archiving must never abort a sync
        logger.error("revenue: failed to archive review state (%s)", exc)
        return None


def classify_review_state(snapshot: list[dict[str, Any]],
                          valid_buckets: set[str]) -> dict[str, Any]:
    """Split archived review entries into keep vs drop.

    An entry is kept when the customer and the bucket both still resolve, which
    is the user-facing rule: if we can still show it, we keep it.

    Deliberately does **not** require the account to have revenue in the current
    pull. Coverage lapses (an account temporarily missing from our MSX
    territory) must not destroy hand-written notes.
    """
    bucket_keys = {_norm(b) for b in valid_buckets}
    customer_ids = {cid for (cid,) in Customer.query.with_entities(Customer.id).all()}
    customer_names = {
        _norm(name) for (name,) in Customer.query.with_entities(Customer.name).all()
    }

    keep_ids: set[int] = set()
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for entry in snapshot:
        bucket_ok = _norm(entry.get("bucket")) in bucket_keys
        cid = entry.get("customer_id")
        customer_ok = (cid in customer_ids) if cid else (_norm(entry.get("customer_name")) in customer_names)
        if bucket_ok and customer_ok:
            keep_ids.add(entry["analysis_id"])
            kept.append(entry)
        else:
            dropped.append({
                "customer_name": entry.get("customer_name"),
                "bucket": entry.get("bucket"),
                "reason": "bucket_retired" if not bucket_ok else "customer_missing",
            })
    return {"keep_analysis_ids": keep_ids, "kept": kept, "dropped": dropped,
            "kept_count": len(kept), "dropped_count": len(dropped)}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------
def purge_revenue_data(keep_analysis_ids: Optional[set[int]] = None) -> dict[str, int]:
    """Delete imported revenue data and its analyses.

    Analyses listed in ``keep_analysis_ids`` (and their note history) survive, so
    the user's review state stays attached to its original row. The subsequent
    analysis run upserts on ``(customer_name, bucket)`` and refreshes those rows
    in place, which is why re-attaching afterwards is unnecessary.

    Deliberately narrower than the admin "clear revenue" action: ``RevenueConfig``
    holds the user's own thresholds and is left alone, as are Customer rows and
    the ``RevenueImport`` log, which is the user's visible import history.
    """
    keep = keep_analysis_ids or set()

    note_q = RevenueReviewNote.query
    analysis_q = RevenueAnalysis.query
    if keep:
        note_q = note_q.filter(RevenueReviewNote.analysis_id.notin_(keep))
        analysis_q = analysis_q.filter(RevenueAnalysis.id.notin_(keep))

    counts = {
        "review_notes": note_q.delete(synchronize_session=False),
        "analyses": analysis_q.delete(synchronize_session=False),
        "product_rows": ProductRevenueData.query.delete(synchronize_session=False),
        "customer_rows": CustomerRevenueData.query.delete(synchronize_session=False),
        "analyses_kept": len(keep),
    }
    db.session.commit()
    logger.info("revenue: purged %s", counts)
    return counts
