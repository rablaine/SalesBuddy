"""
Fiscal Year Cutover Service.

Manages the transition between fiscal years:
- Archive creation (local + optional OneDrive)
- FY transition mode (banner state)
- Alignment finalization (orphan purge)
"""

import logging
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text

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
    """Return the directory where salesbuddy.db lives."""
    db_url = str(db.engine.url)
    # sqlite:///data/salesbuddy.db -> data/salesbuddy.db
    if ":///" in db_url:
        db_path = db_url.split(":///", 1)[1]
    else:
        db_path = "data/salesbuddy.db"
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
    now = datetime.now(timezone.utc)
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
    db_path = data_dir / "salesbuddy.db"

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
        try:
            stat = f.stat()
        except FileNotFoundError:
            # File was deleted between the glob and the stat (e.g. by a
            # parallel test or concurrent cleanup). Skip it.
            continue
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
            rn_count = db.session.execute(text("SELECT COUNT(*) FROM revenue_review_notes WHERE analysis_id IN (SELECT id FROM revenue_analysis WHERE customer_id = :cid)"), {"cid": cid}).scalar() or 0
            ra_count = db.session.execute(text("SELECT COUNT(*) FROM revenue_analysis WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            pr_count = db.session.execute(text("SELECT COUNT(*) FROM product_revenue_data WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            cr_count = db.session.execute(text("SELECT COUNT(*) FROM customer_revenue_data WHERE customer_id = :cid"), {"cid": cid}).scalar() or 0
            purge_revenue += rn_count + ra_count + pr_count + cr_count

            db.session.execute(text("DELETE FROM revenue_review_notes WHERE analysis_id IN (SELECT id FROM revenue_analysis WHERE customer_id = :cid)"), {"cid": cid})
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


# ---------------------------------------------------------------------------
# Archive Explorer
# ---------------------------------------------------------------------------

@contextmanager
def open_archive(label: str):
    """Open an archive database read-only and yield a connection.

    Args:
        label: FY label like 'FY25'. Must match an existing archive file.

    Yields:
        A SQLAlchemy Connection bound to the read-only archive.
    """
    data_dir = _get_data_dir()
    archive_path = data_dir / f"{label}.db"
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive {label}.db not found")

    # Open read-only via SQLite URI
    uri = f"sqlite:///file:{archive_path.as_posix()}?mode=ro&uri=true"
    engine = create_engine(uri)
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()
        engine.dispose()


def get_archive_tree(label: str) -> dict:
    """Return the full tree skeleton for an archive.

    Returns summary stats, sellers with customers, and per-customer
    lists of note/engagement/milestone titles for search + tree rendering.
    """
    with open_archive(label) as conn:
        # Summary stats
        summary = {}
        for table, key in [
            ('sellers', 'sellers'), ('customers', 'customers'),
            ('notes', 'notes'), ('engagements', 'engagements'),
            ('milestones', 'milestones'), ('territories', 'territories'),
            ('opportunities', 'opportunities'),
        ]:
            try:
                row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
                summary[key] = row[0]
            except Exception:
                summary[key] = 0

        # Sellers with customer counts
        sellers_rows = conn.execute(text(
            "SELECT s.id, s.name, s.alias, COUNT(c.id) as customer_count "
            "FROM sellers s LEFT JOIN customers c ON c.seller_id = s.id "
            "GROUP BY s.id ORDER BY s.name"
        )).fetchall()

        sellers = []
        for s in sellers_rows:
            seller_id, name, alias, count = s[0], s[1], s[2], s[3]
            # Get customers for this seller
            customers = _get_seller_customers(conn, seller_id)
            sellers.append({
                'id': seller_id,
                'name': name,
                'alias': alias,
                'customer_count': count,
                'customers': customers,
            })

        # Unassigned customers (no seller_id)
        unassigned = _get_seller_customers(conn, None)

        return {
            'summary': summary,
            'sellers': sellers,
            'unassigned': unassigned,
        }


def _get_seller_customers(conn, seller_id) -> list[dict]:
    """Get customers for a seller (or unassigned if seller_id is None)."""
    if seller_id is None:
        rows = conn.execute(text(
            "SELECT id, name, tpid FROM customers "
            "WHERE seller_id IS NULL ORDER BY name"
        )).fetchall()
    else:
        rows = conn.execute(text(
            "SELECT id, name, tpid FROM customers "
            "WHERE seller_id = :sid ORDER BY name"
        ), {'sid': seller_id}).fetchall()

    customers = []
    for r in rows:
        cid, name, tpid = r[0], r[1], r[2]

        # Note titles + dates
        notes = conn.execute(text(
            "SELECT id, content, call_date FROM notes "
            "WHERE customer_id = :cid ORDER BY call_date DESC"
        ), {'cid': cid}).fetchall()
        note_items = []
        for n in notes:
            # Extract first line or first 60 chars as title
            body = n[1] or ''
            title = body.strip().split('\n')[0][:80] if body.strip() else 'Untitled'
            # Strip HTML tags for display
            title = re.sub(r'<[^>]+>', '', title).strip() or 'Untitled'
            note_items.append({
                'id': n[0],
                'title': title,
                'call_date': n[2],
            })

        # Engagement names
        engs = conn.execute(text(
            "SELECT id, title, status FROM engagements "
            "WHERE customer_id = :cid ORDER BY title"
        ), {'cid': cid}).fetchall()
        eng_items = [{'id': e[0], 'title': e[1], 'status': e[2]} for e in engs]

        # Milestone names
        mils = conn.execute(text(
            "SELECT id, title, msx_status FROM milestones "
            "WHERE customer_id = :cid ORDER BY title"
        ), {'cid': cid}).fetchall()
        mil_items = [{'id': m[0], 'title': m[1], 'status': m[2]} for m in mils]

        customers.append({
            'id': cid,
            'name': name,
            'tpid': tpid,
            'notes': note_items,
            'engagements': eng_items,
            'milestones': mil_items,
        })

    return customers


def get_archive_customer(label: str, customer_id: int) -> dict:
    """Return full customer detail from an archive."""
    with open_archive(label) as conn:
        row = conn.execute(text(
            "SELECT c.id, c.name, c.tpid, c.nickname, "
            "t.name as territory_name, s.name as seller_name "
            "FROM customers c "
            "LEFT JOIN territories t ON c.territory_id = t.id "
            "LEFT JOIN sellers s ON c.seller_id = s.id "
            "WHERE c.id = :cid"
        ), {'cid': customer_id}).fetchone()
        if not row:
            return None

        customer = {
            'id': row[0], 'name': row[1], 'tpid': row[2],
            'nickname': row[3], 'territory': row[4], 'seller': row[5],
        }

        # Verticals
        try:
            verts = conn.execute(text(
                "SELECT v.name FROM verticals v "
                "JOIN customers_verticals cv ON cv.vertical_id = v.id "
                "WHERE cv.customer_id = :cid"
            ), {'cid': customer_id}).fetchall()
            customer['verticals'] = [v[0] for v in verts]
        except Exception:
            customer['verticals'] = []

        # Notes with topics and body
        notes = conn.execute(text(
            "SELECT id, content, call_date, created_at FROM notes "
            "WHERE customer_id = :cid ORDER BY call_date DESC"
        ), {'cid': customer_id}).fetchall()
        customer['notes'] = []
        for n in notes:
            note = {'id': n[0], 'content': n[1], 'call_date': n[2], 'created_at': n[3]}
            # Topics for this note
            try:
                topics = conn.execute(text(
                    "SELECT t.name FROM topics t "
                    "JOIN notes_topics nt ON nt.topic_id = t.id "
                    "WHERE nt.note_id = :nid"
                ), {'nid': n[0]}).fetchall()
                note['topics'] = [t[0] for t in topics]
            except Exception:
                note['topics'] = []
            customer['notes'].append(note)

        # Engagements with linked notes
        engs = conn.execute(text(
            "SELECT id, title, status, technical_problem, business_impact "
            "FROM engagements WHERE customer_id = :cid ORDER BY title"
        ), {'cid': customer_id}).fetchall()
        customer['engagements'] = []
        for e in engs:
            eng = {
                'id': e[0], 'title': e[1], 'status': e[2],
                'technical_problem': e[3], 'business_impact': e[4],
            }
            try:
                linked = conn.execute(text(
                    "SELECT n.id, n.call_date FROM notes n "
                    "JOIN notes_engagements ne ON ne.note_id = n.id "
                    "WHERE ne.engagement_id = :eid"
                ), {'eid': e[0]}).fetchall()
                eng['linked_notes'] = [{'id': ln[0], 'call_date': ln[1]} for ln in linked]
            except Exception:
                eng['linked_notes'] = []
            customer['engagements'].append(eng)

        # Milestones with tasks and linked notes
        mils = conn.execute(text(
            "SELECT id, title, msx_status, dollar_value, due_date, workload "
            "FROM milestones WHERE customer_id = :cid ORDER BY title"
        ), {'cid': customer_id}).fetchall()
        customer['milestones'] = []
        for m in mils:
            mil = {
                'id': m[0], 'title': m[1], 'status': m[2],
                'dollar_value': m[3], 'due_date': m[4], 'workload': m[5],
            }
            try:
                tasks = conn.execute(text(
                    "SELECT subject, task_category, duration_minutes "
                    "FROM msx_tasks WHERE milestone_id = :mid"
                ), {'mid': m[0]}).fetchall()
                mil['tasks'] = [{'subject': t[0], 'category': t[1], 'duration': t[2]} for t in tasks]
            except Exception:
                mil['tasks'] = []
            try:
                linked = conn.execute(text(
                    "SELECT n.id, n.call_date FROM notes n "
                    "JOIN notes_milestones nm ON nm.note_id = n.id "
                    "WHERE nm.milestone_id = :mid"
                ), {'mid': m[0]}).fetchall()
                mil['linked_notes'] = [{'id': ln[0], 'call_date': ln[1]} for ln in linked]
            except Exception:
                mil['linked_notes'] = []
            customer['milestones'].append(mil)

        return customer


def get_archive_detail(label: str, item_type: str, item_id: int) -> dict:
    """Return a single note, engagement, or milestone from an archive."""
    with open_archive(label) as conn:
        if item_type == 'note':
            row = conn.execute(text(
                "SELECT n.id, n.content, n.call_date, n.created_at, "
                "c.name as customer_name, c.id as customer_id "
                "FROM notes n "
                "LEFT JOIN customers c ON n.customer_id = c.id "
                "WHERE n.id = :id"
            ), {'id': item_id}).fetchone()
            if not row:
                return None
            detail = {
                'type': 'note', 'id': row[0], 'content': row[1],
                'call_date': row[2], 'created_at': row[3],
                'customer_name': row[4], 'customer_id': row[5],
            }
            try:
                topics = conn.execute(text(
                    "SELECT t.name FROM topics t "
                    "JOIN notes_topics nt ON nt.topic_id = t.id "
                    "WHERE nt.note_id = :nid"
                ), {'nid': item_id}).fetchall()
                detail['topics'] = [t[0] for t in topics]
            except Exception:
                detail['topics'] = []
            try:
                partners = conn.execute(text(
                    "SELECT p.name FROM partners p "
                    "JOIN notes_partners np ON np.partner_id = p.id "
                    "WHERE np.note_id = :nid"
                ), {'nid': item_id}).fetchall()
                detail['partners'] = [p[0] for p in partners]
            except Exception:
                detail['partners'] = []
            return detail

        elif item_type == 'engagement':
            row = conn.execute(text(
                "SELECT e.id, e.title, e.status, e.technical_problem, "
                "e.business_impact, e.solution_resources, e.estimated_acr, "
                "c.name as customer_name, c.id as customer_id "
                "FROM engagements e "
                "LEFT JOIN customers c ON e.customer_id = c.id "
                "WHERE e.id = :id"
            ), {'id': item_id}).fetchone()
            if not row:
                return None
            detail = {
                'type': 'engagement', 'id': row[0], 'title': row[1],
                'status': row[2], 'technical_problem': row[3],
                'business_impact': row[4], 'solution_resources': row[5],
                'estimated_acr': row[6], 'customer_name': row[7],
                'customer_id': row[8],
            }
            try:
                linked = conn.execute(text(
                    "SELECT n.id, n.call_date, n.content FROM notes n "
                    "JOIN notes_engagements ne ON ne.note_id = n.id "
                    "WHERE ne.engagement_id = :eid ORDER BY n.call_date DESC"
                ), {'eid': item_id}).fetchall()
                detail['linked_notes'] = [{
                    'id': ln[0], 'call_date': ln[1],
                    'title': re.sub(r'<[^>]+>', '', (ln[2] or '').strip().split('\n')[0][:80]).strip() or 'Untitled',
                } for ln in linked]
            except Exception:
                detail['linked_notes'] = []
            return detail

        elif item_type == 'milestone':
            row = conn.execute(text(
                "SELECT m.id, m.title, m.msx_status, m.dollar_value, "
                "m.due_date, m.workload, m.monthly_usage, "
                "c.name as customer_name, c.id as customer_id "
                "FROM milestones m "
                "LEFT JOIN customers c ON m.customer_id = c.id "
                "WHERE m.id = :id"
            ), {'id': item_id}).fetchone()
            if not row:
                return None
            detail = {
                'type': 'milestone', 'id': row[0], 'title': row[1],
                'status': row[2], 'dollar_value': row[3],
                'due_date': row[4], 'workload': row[5],
                'monthly_usage': row[6], 'customer_name': row[7],
                'customer_id': row[8],
            }
            try:
                tasks = conn.execute(text(
                    "SELECT subject, task_category, duration_minutes, is_hok "
                    "FROM msx_tasks WHERE milestone_id = :mid"
                ), {'mid': item_id}).fetchall()
                detail['tasks'] = [{
                    'subject': t[0], 'category': t[1],
                    'duration': t[2], 'is_hok': t[3],
                } for t in tasks]
            except Exception:
                detail['tasks'] = []
            try:
                linked = conn.execute(text(
                    "SELECT n.id, n.call_date FROM notes n "
                    "JOIN notes_milestones nm ON nm.note_id = n.id "
                    "WHERE nm.milestone_id = :mid ORDER BY n.call_date DESC"
                ), {'mid': item_id}).fetchall()
                detail['linked_notes'] = [{'id': ln[0], 'call_date': ln[1]} for ln in linked]
            except Exception:
                detail['linked_notes'] = []
            return detail

        else:
            raise ValueError(f"Unknown item type: {item_type}")
