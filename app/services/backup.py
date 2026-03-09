"""
File-based backup service for call log disaster recovery.

Writes per-customer JSON files organized by seller name into a OneDrive-synced
folder.  OneDrive handles cloud sync transparently, giving us RPO=0 backups
with zero external API calls.

The backup path is derived automatically from ``data/backup_config.json``
(the DB backup config written by ``scripts/server.ps1``) or auto-detected
from OneDrive for Business.  There is no separate note-specific config
file -- if DB backups are configured, call log backups use the same path.

Folder structure::

    {BACKUP_ROOT}/
        notes/
            {seller_name}/
                {tpid}.json
            Unassigned/
                {tpid}.json
"""

import json
import logging
import os
import re
import sys
import winreg
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models import Note, Customer, Engagement, db

logger = logging.getLogger(__name__)

# Subfolder inside the configured backup root
_NOTES_DIR = "notes"
# Legacy subfolder name (checked during read operations for backward compatibility)
_LEGACY_NOTES_DIR = "call_logs"

# The specific org name to match for OneDrive for Business.  Employees may
# have multiple OneDrive for Business accounts (e.g. from consultancies or
# partner orgs); we only want the Microsoft corporate one.
_ONEDRIVE_ORG_NAME = "Microsoft"

# Subfolder path we create/look for inside the OneDrive root.
# Changed from "NoteHelper_Backups" to "Backups/NoteHelper" for cleaner
# organization under a shared Backups umbrella.
_NOTEHELPER_BACKUPS_DIR = os.path.join("Backups", "NoteHelper")

# DB backup config filename (written by scripts/server.ps1 and backup.ps1)
_DB_BACKUP_CONFIG = "backup_config.json"


def _db_backup_config_path() -> Path:
    """Return the absolute path to the DB backup config file."""
    project_root = Path(os.path.abspath(__file__)).parent.parent.parent
    return project_root / "data" / _DB_BACKUP_CONFIG


def _load_db_backup_config() -> Dict[str, Any]:
    """Load the DB backup config (written by scripts/server.ps1).

    Returns a dict with at least ``enabled`` and ``backup_dir`` keys.
    """
    path = _db_backup_config_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"enabled": False, "backup_dir": ""}


# ---------------------------------------------------------------------------
# OneDrive auto-detection
# ---------------------------------------------------------------------------

def _is_business_path(path: str, source: str) -> bool:
    """Return True if *path* is the target OneDrive for Business folder.

    Specifically matches ``OneDrive - Microsoft`` regardless of how the
    path was discovered.  Employees may have multiple OneDrive for Business
    accounts (e.g. "OneDrive - Contoso" from a partner org) and we only
    want the Microsoft corporate one.
    """
    basename = os.path.basename(path).lower().strip()
    expected = f"onedrive - {_ONEDRIVE_ORG_NAME.lower()}"
    return basename == expected


def detect_onedrive_paths(*, business_only: bool = True) -> List[Dict[str, Any]]:
    """Detect OneDrive folder paths on this machine.

    Uses the same priority order as ``scripts/backup.ps1``:
    1. ``%OneDriveCommercial%`` env var (corporate accounts)
    2. ``%OneDrive%`` env var
    3. Registry ``HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\Business1``
    4. Folder scan of ``%USERPROFILE%`` for ``OneDrive*`` directories

    Each returned entry includes whether a ``Backups/NoteHelper`` folder
    already exists inside it, which lets the caller auto-select if there's
    an obvious winner.

    Args:
        business_only: When True (default), only return OneDrive for Business
            paths.  Personal OneDrive folders are excluded.

    Returns:
        List of dicts with keys: ``path``, ``source``, ``is_business``,
        ``has_backups``, ``suggested_path``.  Sorted so paths with existing
        backups come first.
    """
    seen: set[str] = set()
    candidates: List[Dict[str, Any]] = []

    def _add(path: str, source: str) -> None:
        normalized = os.path.normpath(path)
        if normalized in seen:
            return
        if not os.path.isdir(normalized):
            return
        seen.add(normalized)
        is_biz = _is_business_path(normalized, source)
        if business_only and not is_biz:
            return
        suggested = os.path.join(normalized, _NOTEHELPER_BACKUPS_DIR)
        candidates.append({
            "path": normalized,
            "source": source,
            "is_business": is_biz,
            "has_backups": os.path.isdir(suggested),
            "suggested_path": suggested,
        })

    # Priority 1: OneDriveCommercial env var
    odc = os.environ.get("OneDriveCommercial", "")
    if odc:
        _add(odc, "OneDriveCommercial env var")

    # Priority 2: OneDrive env var
    od = os.environ.get("OneDrive", "")
    if od:
        _add(od, "OneDrive env var")

    # Priority 3: Registry (Windows only)
    if sys.platform == "win32":
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\OneDrive\Accounts\Business1",
            )
            folder, _ = winreg.QueryValueEx(key, "UserFolder")
            winreg.CloseKey(key)
            if folder:
                _add(folder, "Registry (Business1)")
        except (OSError, FileNotFoundError):
            pass

    # Priority 4: Folder scan
    user_profile = os.environ.get("USERPROFILE", "")
    if user_profile and os.path.isdir(user_profile):
        try:
            for entry in sorted(os.listdir(user_profile), key=len, reverse=True):
                if entry.lower().startswith("onedrive"):
                    full = os.path.join(user_profile, entry)
                    if os.path.isdir(full):
                        _add(full, "Folder scan")
        except OSError:
            pass

    # Sort: paths with existing Backups/NoteHelper folder first
    candidates.sort(key=lambda c: (not c["has_backups"], c["source"]))

    return candidates


def get_auto_detected_backup_path() -> Optional[str]:
    """Return the best auto-detected backup path, or None.

    If exactly one candidate has an existing ``Backups/NoteHelper`` folder
    inside it, return that path directly.  Otherwise return None (caller
    should present choices to the user).
    """
    candidates = detect_onedrive_paths()
    if not candidates:
        return None

    # If one already has the backups folder, pick it
    with_backups = [c for c in candidates if c["has_backups"]]
    if len(with_backups) == 1:
        return with_backups[0]["suggested_path"]

    return None


def is_business_onedrive_path(path: str) -> bool:
    """Return True if *path* lives under a OneDrive for Business folder.

    Walks up the directory tree looking for a folder whose name matches
    ``OneDrive - Microsoft``, or checks whether the path matches any
    known business candidate from detection.
    """
    normalized = os.path.normpath(path)

    # Quick check: does the path sit under any detected business candidate?
    for candidate in detect_onedrive_paths(business_only=True):
        if normalized.lower().startswith(candidate["path"].lower()):
            return True

    # Walk up to check folder names (covers manual input)
    parts = Path(normalized).parts
    for part in parts:
        lower = part.lower().strip()
        if lower.startswith("onedrive"):
            return _is_business_path(
                os.path.join(*parts[:parts.index(part) + 1]),
                "Path inspection",
            )

    return False


def _get_backup_root() -> Optional[str]:
    """Return the backup root path, or None if no backup location is available.

    Resolution order:
    1. ``backup_config.json`` -- the DB backup config written by
       ``scripts/server.ps1``.  If ``enabled`` is true and ``backup_dir``
       is set, use that.
    2. Auto-detect from OneDrive for Business (``get_auto_detected_backup_path``).

    No separate note config file is needed.
    """
    db_cfg = _load_db_backup_config()
    if db_cfg.get("enabled") and db_cfg.get("backup_dir"):
        return db_cfg["backup_dir"]

    # Fallback: auto-detect
    return get_auto_detected_backup_path()


def _sanitize_folder_name(name: str) -> str:
    """Make a string safe for use as a folder name.

    Replaces characters that are invalid on Windows/macOS/Linux and
    collapses whitespace.
    """
    # Replace invalid path characters with underscore
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Collapse whitespace
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    # Avoid empty or dot-only names
    if not sanitized or sanitized in ('.', '..'):
        sanitized = '_unnamed'
    return sanitized


def _customer_to_dict(customer: Customer) -> Dict[str, Any]:
    """Serialize a customer and all related data to a backup dict.

    Includes notes (with milestone links), engagements (with story fields
    and links to notes/opportunities/milestones), and customer metadata.

    Args:
        customer: Customer with eagerly-loaded relationships.

    Returns:
        Dictionary ready for JSON serialization.
    """
    notes: List[Note] = sorted(
        customer.notes, key=lambda cl: cl.call_date, reverse=True
    )

    engagements: List[Engagement] = sorted(
        customer.engagements, key=lambda e: e.created_at, reverse=True
    )

    return {
        "_notehelper_backup": True,
        "_version": 3,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "customer": {
            "name": customer.name,
            "nickname": customer.nickname,
            "tpid": customer.tpid,
            "tpid_url": customer.tpid_url,
            "account_context": customer.account_context,
            "seller_name": customer.seller.name if customer.seller else None,
            "territory_name": customer.territory.name if customer.territory else None,
            "verticals": [v.name for v in customer.verticals],
        },
        "notes": [
            {
                "call_date": cl.call_date.isoformat(),
                "content": cl.content,
                "created_at": cl.created_at.isoformat() if cl.created_at else None,
                "updated_at": cl.updated_at.isoformat() if cl.updated_at else None,
                "topics": [t.name for t in cl.topics],
                "partners": [p.name for p in cl.partners],
                "milestones": [
                    m.msx_milestone_id for m in cl.milestones
                    if m.msx_milestone_id
                ],
            }
            for cl in notes
        ],
        "engagements": [
            {
                "title": eng.title,
                "status": eng.status,
                "key_individuals": eng.key_individuals,
                "technical_problem": eng.technical_problem,
                "business_impact": eng.business_impact,
                "solution_resources": eng.solution_resources,
                "estimated_acr": eng.estimated_acr,
                "target_date": eng.target_date.isoformat() if eng.target_date else None,
                "created_at": eng.created_at.isoformat() if eng.created_at else None,
                "updated_at": eng.updated_at.isoformat() if eng.updated_at else None,
                "linked_notes": [
                    n.call_date.isoformat() for n in eng.notes
                ],
                "linked_opportunities": [
                    o.msx_opportunity_id for o in eng.opportunities
                    if o.msx_opportunity_id
                ],
                "linked_milestones": [
                    m.msx_milestone_id for m in eng.milestones
                    if m.msx_milestone_id
                ],
            }
            for eng in engagements
        ],
    }


def backup_customer(customer_id: int) -> bool:
    """Write the backup JSON file for a single customer.

    The file is written to ``{backup_root}/notes/{seller}/{tpid}.json``.
    If the customer has no TPID, the customer's DB id is used as the filename.

    Args:
        customer_id: Primary key of the customer to back up.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    customer = (
        Customer.query
        .options(
            db.joinedload(Customer.seller),
            db.joinedload(Customer.territory),
            db.joinedload(Customer.verticals),
            db.joinedload(Customer.notes).joinedload(Note.topics),
            db.joinedload(Customer.notes).joinedload(Note.partners),
            db.joinedload(Customer.notes).joinedload(Note.milestones),
            db.joinedload(Customer.engagements).joinedload(Engagement.notes),
            db.joinedload(Customer.engagements).joinedload(Engagement.opportunities),
            db.joinedload(Customer.engagements).joinedload(Engagement.milestones),
        )
        .filter_by(id=customer_id)
        .first()
    )
    if not customer:
        logger.warning("Backup skipped: customer %d not found", customer_id)
        return False

    # Determine folder and filename
    seller_name = customer.seller.name if customer.seller else "Unassigned"
    folder = os.path.join(backup_root, _NOTES_DIR, _sanitize_folder_name(seller_name))
    filename = f"{customer.tpid}.json" if customer.tpid else f"id_{customer.id}.json"
    filepath = os.path.join(folder, filename)

    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        data = _customer_to_dict(customer)
        # Atomic-ish write: write to temp file then rename
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
        logger.debug("Backup written: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to write backup for customer %d", customer_id)
        return False


def backup_all_customers() -> Dict[str, int]:
    """Back up all customers that have at least one call log.

    Returns:
        Dict with ``backed_up`` and ``failed`` counts.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return {"backed_up": 0, "failed": 0, "error": "Backup not configured"}

    customers = (
        Customer.query
        .filter(db.or_(Customer.notes.any(), Customer.engagements.any()))
        .options(
            db.joinedload(Customer.seller),
            db.joinedload(Customer.territory),
            db.joinedload(Customer.verticals),
            db.joinedload(Customer.notes).joinedload(Note.topics),
            db.joinedload(Customer.notes).joinedload(Note.partners),
            db.joinedload(Customer.notes).joinedload(Note.milestones),
            db.joinedload(Customer.engagements).joinedload(Engagement.notes),
            db.joinedload(Customer.engagements).joinedload(Engagement.opportunities),
            db.joinedload(Customer.engagements).joinedload(Engagement.milestones),
        )
        .all()
    )

    backed_up = 0
    failed = 0
    for customer in customers:
        seller_name = customer.seller.name if customer.seller else "Unassigned"
        folder = os.path.join(backup_root, _NOTES_DIR, _sanitize_folder_name(seller_name))
        filename = f"{customer.tpid}.json" if customer.tpid else f"id_{customer.id}.json"
        filepath = os.path.join(folder, filename)

        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            data = _customer_to_dict(customer)
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, filepath)
            backed_up += 1
        except Exception:
            logger.exception("Failed to back up customer %d", customer.id)
            failed += 1

    return {"backed_up": backed_up, "failed": failed}


def find_backup_folder() -> Optional[str]:
    """Find the notes backup folder, trying config first then auto-detect.

    Checks both the new folder name (``notes``) and the legacy name
    (``call_logs``) for backward compatibility.

    Returns:
        Absolute path to the backup subfolder, or None if not found.
    """
    # 1. Try _get_backup_root (reads backup_config.json, then auto-detects)
    backup_root = _get_backup_root()
    if backup_root:
        for dirname in (_NOTES_DIR, _LEGACY_NOTES_DIR):
            notes_dir = os.path.join(backup_root, dirname)
            if os.path.isdir(notes_dir):
                return notes_dir

    # 2. Walk all candidates as last resort
    for candidate in detect_onedrive_paths():
        for dirname in (_NOTES_DIR, _LEGACY_NOTES_DIR):
            notes_dir = os.path.join(candidate["suggested_path"], dirname)
            if os.path.isdir(notes_dir):
                return notes_dir

    return None


def restore_all_from_folder(notes_dir: Optional[str] = None) -> Dict[str, Any]:
    """Restore all call logs from a backup folder.

    Walks ``{notes_dir}/{seller}/`` subfolders, reads every ``.json``
    file, and calls ``restore_from_backup()`` for each.

    Args:
        notes_dir: Path to the ``notes`` folder.  If None, will
            auto-detect using ``find_backup_folder()``.

    Returns:
        Result dict with aggregate success/failure counts.
    """
    if notes_dir is None:
        notes_dir = find_backup_folder()

    if not notes_dir or not os.path.isdir(notes_dir):
        return {
            "success": False,
            "error": "Could not find notes backup folder. "
                     "Enable backup or verify OneDrive path.",
        }

    files_processed = 0
    files_failed = 0
    total_logs_created = 0
    total_logs_skipped = 0
    customers_restored = []
    errors = []

    for seller_folder in sorted(os.listdir(notes_dir)):
        seller_path = os.path.join(notes_dir, seller_folder)
        if not os.path.isdir(seller_path):
            continue

        for filename in sorted(os.listdir(seller_path)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(seller_path, filename)
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable file %s: %s", filepath, exc)
                files_failed += 1
                errors.append(f"{seller_folder}/{filename}: {exc}")
                continue

            result = restore_from_backup(data)
            if result.get("success"):
                files_processed += 1
                total_logs_created += result.get("logs_created", 0)
                total_logs_skipped += result.get("logs_skipped", 0)
                if result.get("logs_created", 0) > 0:
                    customers_restored.append(result.get("customer_name", filename))
            else:
                files_failed += 1
                errors.append(
                    f"{seller_folder}/{filename}: {result.get('error', 'Unknown')}"
                )

    return {
        "success": True,
        "files_processed": files_processed,
        "files_failed": files_failed,
        "total_logs_created": total_logs_created,
        "total_logs_skipped": total_logs_skipped,
        "customers_restored": customers_restored,
        "errors": errors[:20],  # Cap error list to avoid huge response
        "backup_folder": notes_dir,
    }


def _normalize_date_iso(dt: datetime) -> str:
    """Return an isoformat string with UTC timezone for dedup comparisons."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _build_note_date_index(customer: Customer) -> Dict[str, "Note"]:
    """Build a mapping of normalized isoformat date → Note for a customer."""
    index: Dict[str, Note] = {}
    for note in customer.notes:
        key = _normalize_date_iso(note.call_date)
        index[key] = note
    return index


def restore_from_backup(data: Dict[str, Any]) -> Dict[str, Any]:
    """Restore notes and engagements from a backup JSON dict.

    Matches the customer by TPID, then restores:
    - Notes (deduped by call_date) with topic, partner, and milestone links
    - Engagements (deduped by title) with story fields and links to
      notes, opportunities, and milestones

    Handles v2 backups (no engagements/milestone data) gracefully.

    Args:
        data: Parsed backup JSON dict.

    Returns:
        Result dict with success status and counts.
    """
    from app.models import Milestone, Opportunity, Partner, Topic

    if not data.get("_notehelper_backup"):
        return {"success": False, "error": "Invalid backup payload"}

    cust_data = data.get("customer", {})
    tpid = cust_data.get("tpid")
    if not tpid:
        return {"success": False, "error": "Backup has no TPID"}

    customer = Customer.query.filter_by(tpid=tpid).first()
    if not customer:
        return {
            "success": False,
            "error": f"Customer with TPID {tpid} not found. Import accounts first.",
        }

    # ------------------------------------------------------------------
    # Restore notes
    # ------------------------------------------------------------------
    note_index = _build_note_date_index(customer)
    existing_dates = set(note_index.keys())

    logs_created = 0
    logs_skipped = 0

    for cl_data in data.get("notes") or data.get("call_logs", []):
        call_date_str = cl_data.get("call_date")
        if not call_date_str:
            logs_skipped += 1
            continue

        try:
            call_date = datetime.fromisoformat(call_date_str)
        except (ValueError, TypeError):
            logs_skipped += 1
            continue

        normalized = _normalize_date_iso(call_date)

        if normalized in existing_dates:
            # Note already exists — still re-link milestones if missing
            existing_note = note_index[normalized]
            _restore_note_milestones(existing_note, cl_data.get("milestones", []),
                                     Milestone)
            logs_skipped += 1
            continue

        note = Note(
            customer_id=customer.id,
            call_date=call_date,
            content=cl_data.get("content", ""),
        )
        db.session.add(note)

        for topic_name in cl_data.get("topics", []):
            topic = Topic.query.filter_by(name=topic_name).first()
            if not topic:
                topic = Topic(name=topic_name)
                db.session.add(topic)
                db.session.flush()
            note.topics.append(topic)

        for partner_name in cl_data.get("partners", []):
            partner = Partner.query.filter_by(name=partner_name).first()
            if not partner:
                partner = Partner(name=partner_name)
                db.session.add(partner)
                db.session.flush()
            note.partners.append(partner)

        db.session.flush()  # Get note.id for milestone linking

        _restore_note_milestones(note, cl_data.get("milestones", []), Milestone)

        note_index[normalized] = note
        existing_dates.add(normalized)
        logs_created += 1

    db.session.flush()

    # ------------------------------------------------------------------
    # Restore engagements (v3+ backups only)
    # ------------------------------------------------------------------
    engagements_created = 0
    engagements_skipped = 0

    for eng_data in data.get("engagements", []):
        title = eng_data.get("title")
        if not title:
            engagements_skipped += 1
            continue

        # Dedup by title within this customer
        existing_eng = Engagement.query.filter_by(
            customer_id=customer.id, title=title
        ).first()

        if existing_eng:
            # Update links on existing engagement even if it already exists
            _restore_engagement_links(
                existing_eng, eng_data, note_index, Milestone, Opportunity
            )
            engagements_skipped += 1
            continue

        target_date = None
        td_str = eng_data.get("target_date")
        if td_str:
            try:
                from datetime import date as date_type
                target_date = date_type.fromisoformat(td_str)
            except (ValueError, TypeError):
                pass

        eng = Engagement(
            customer_id=customer.id,
            title=title,
            status=eng_data.get("status", "Active"),
            key_individuals=eng_data.get("key_individuals"),
            technical_problem=eng_data.get("technical_problem"),
            business_impact=eng_data.get("business_impact"),
            solution_resources=eng_data.get("solution_resources"),
            estimated_acr=eng_data.get("estimated_acr"),
            target_date=target_date,
        )
        db.session.add(eng)
        db.session.flush()

        _restore_engagement_links(eng, eng_data, note_index, Milestone, Opportunity)
        engagements_created += 1

    db.session.commit()

    # ------------------------------------------------------------------
    # Restore customer account context if missing
    # ------------------------------------------------------------------
    backup_context = (
        cust_data.get("account_context")
        or cust_data.get("overview")
        or cust_data.get("notes")
    )
    if backup_context and not customer.account_context:
        customer.account_context = backup_context
        db.session.commit()

    return {
        "success": True,
        "customer_name": customer.name,
        "logs_created": logs_created,
        "logs_skipped": logs_skipped,
        "engagements_created": engagements_created,
        "engagements_skipped": engagements_skipped,
    }


def _restore_note_milestones(
    note: Note,
    milestone_ids: List[str],
    milestone_cls: type,
) -> None:
    """Link a note to milestones by msx_milestone_id, skipping duplicates."""
    if not milestone_ids:
        return
    existing_ids = {m.msx_milestone_id for m in note.milestones}
    for ms_id in milestone_ids:
        if ms_id in existing_ids:
            continue
        milestone = milestone_cls.query.filter_by(msx_milestone_id=ms_id).first()
        if milestone:
            note.milestones.append(milestone)
            existing_ids.add(ms_id)


def _restore_engagement_links(
    eng: Engagement,
    eng_data: Dict[str, Any],
    note_index: Dict[str, Note],
    milestone_cls: type,
    opportunity_cls: type,
) -> None:
    """Restore links from an engagement to notes, opportunities, and milestones."""
    # Link to notes by call_date
    existing_note_ids = {n.id for n in eng.notes}
    for date_str in eng_data.get("linked_notes", []):
        try:
            dt = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        key = _normalize_date_iso(dt)
        note = note_index.get(key)
        if note and note.id not in existing_note_ids:
            eng.notes.append(note)
            existing_note_ids.add(note.id)

    # Link to opportunities by msx_opportunity_id
    existing_opp_ids = {o.msx_opportunity_id for o in eng.opportunities}
    for opp_id in eng_data.get("linked_opportunities", []):
        if opp_id in existing_opp_ids:
            continue
        opp = opportunity_cls.query.filter_by(msx_opportunity_id=opp_id).first()
        if opp:
            eng.opportunities.append(opp)
            existing_opp_ids.add(opp_id)

    # Link to milestones by msx_milestone_id
    existing_ms_ids = {m.msx_milestone_id for m in eng.milestones}
    for ms_id in eng_data.get("linked_milestones", []):
        if ms_id in existing_ms_ids:
            continue
        milestone = milestone_cls.query.filter_by(msx_milestone_id=ms_id).first()
        if milestone:
            eng.milestones.append(milestone)
            existing_ms_ids.add(ms_id)
