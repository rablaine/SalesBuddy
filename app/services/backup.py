"""
File-based backup service for call log disaster recovery.

Writes per-customer JSON files organized by seller name into a OneDrive-synced
folder.  OneDrive handles cloud sync transparently, giving us RPO=0 backups
with zero external API calls.

The backup path is derived automatically from the ``onedrive_path`` stored
in ``UserPreference`` (populated on first boot) or auto-detected from OneDrive
for Business.  There is no separate note-specific config file - if DB backups

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

from app.models import (
    ConnectExport,
    Customer,
    Engagement,
    Note,
    NoteTemplate,
    Partner,
    PartnerContact,
    RevenueConfig,
    Specialty,
    Topic,
    UserPreference,
    db,
)

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
# Changed from "SalesBuddy_Backups" to "Backups/SalesBuddy" for cleaner
# organization under a shared Backups umbrella.
_SALESBUDDY_BACKUPS_DIR = os.path.join("Backups", "SalesBuddy")


def _get_onedrive_path_from_db() -> str:
    """Return the cached OneDrive path from UserPreference, or empty string.

    Must be called inside an app context.
    """
    from app.models import UserPreference
    prefs = UserPreference.query.first()
    return (prefs.onedrive_path or '') if prefs else ''


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

    Each returned entry includes whether a ``Backups/SalesBuddy`` folder
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
        suggested = os.path.join(normalized, _SALESBUDDY_BACKUPS_DIR)
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

    # Sort: paths with existing Backups/SalesBuddy folder first
    candidates.sort(key=lambda c: (not c["has_backups"], c["source"]))

    return candidates


def get_auto_detected_backup_path() -> Optional[str]:
    """Return the best auto-detected backup path, or None.

    If exactly one candidate has an existing ``Backups/SalesBuddy`` folder
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
    1. ``UserPreference.onedrive_path`` in the database - derive
       ``{onedrive_path}/Backups/SalesBuddy``.
    2. Auto-detect from OneDrive for Business (``get_auto_detected_backup_path``).
    """
    onedrive = _get_onedrive_path_from_db()
    if onedrive:
        return os.path.join(onedrive, "Backups", "SalesBuddy")

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


def _parse_acr_value(val) -> Optional[int]:
    """Parse an estimated_acr value to int, handling old string formats.

    Handles values like '$500/mo', '$50K', '500', 50000, or None.
    """
    if val is None:
        return None
    if isinstance(val, int):
        return val
    val_str = str(val).strip()
    if not val_str:
        return None
    # Already a clean integer
    try:
        return int(val_str)
    except (ValueError, TypeError):
        pass
    # Strip $, commas, whitespace, /mo, /month
    cleaned = re.sub(r'[$,\s]', '', val_str)
    cleaned = re.sub(r'/(mo(nth)?)?$', '', cleaned, flags=re.IGNORECASE)
    # Handle K/k suffix (e.g. 50K -> 50000)
    k_match = re.match(r'^(\d+(?:\.\d+)?)[kK]$', cleaned)
    if k_match:
        return int(float(k_match.group(1)) * 1000)
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _customer_to_dict(customer: Customer) -> Dict[str, Any]:
    """Serialize a customer and all related data to a backup dict.

    Includes notes (with milestone links), engagements (with story fields
    and links to notes/opportunities/milestones), full partner records
    (with contacts and specialties), full topic records (with descriptions),
    and customer metadata.

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

    # Collect all unique partners referenced by this customer's notes
    seen_partner_ids = set()
    partner_list = []
    for note in notes:
        for p in note.partners:
            if p.id not in seen_partner_ids:
                seen_partner_ids.add(p.id)
                # Access the text notes column via the underlying table
                # column (the 'notes' attribute is shadowed by the Note
                # relationship).  The column may not exist if migrations
                # haven't run (db.create_all skips it due to the shadow).
                try:
                    text_notes = db.session.execute(
                        db.text("SELECT notes FROM partners WHERE id = :id"),
                        {"id": p.id},
                    ).scalar()
                except Exception:
                    text_notes = None
                partner_list.append({
                    "name": p.name,
                    "notes": text_notes,
                    "rating": p.rating,
                    "contacts": [
                        {
                            "name": c.name,
                            "email": c.email,
                            "is_primary": c.is_primary,
                        }
                        for c in p.contacts
                    ],
                    "specialties": [s.name for s in p.specialties],
                })

    # Collect all unique topics referenced by this customer's notes
    seen_topic_ids = set()
    topic_list = []
    for note in notes:
        for t in note.topics:
            if t.id not in seen_topic_ids:
                seen_topic_ids.add(t.id)
                topic_list.append({
                    "name": t.name,
                    "description": t.description,
                })

    return {
        "_salesbuddy_backup": True,
        "_version": 4,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "customer": {
            "name": customer.name,
            "nickname": customer.nickname,
            "tpid": customer.tpid,
            "tpid_url": customer.tpid_url,
            "account_context": customer.account_context,
            "website": customer.website,
            "favicon_b64": customer.favicon_b64,
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
        "partners": partner_list,
        "topics": topic_list,
    }


def _partner_to_dict(partner: Partner) -> Dict[str, Any]:
    """Serialize a partner to a standalone backup dict."""
    return {
        "_salesbuddy_partner_backup": True,
        "_version": 1,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "partner": {
            "name": partner.name,
            "overview": partner.overview,
            "rating": partner.rating,
            "website": partner.website,
            "contacts": [
                {
                    "name": c.name,
                    "email": c.email,
                    "is_primary": c.is_primary,
                }
                for c in partner.contacts
            ],
            "specialties": [s.name for s in partner.specialties],
        },
    }


def _template_to_dict(template: NoteTemplate) -> Dict[str, Any]:
    """Serialize a note template to a standalone backup dict."""
    return {
        "_salesbuddy_template_backup": True,
        "_version": 1,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "template": {
            "name": template.name,
            "content": template.content,
            "is_builtin": template.is_builtin,
        },
    }


_PARTNERS_DIR = "partners"
_TEMPLATES_DIR = "templates"


def backup_partner(partner_id: int) -> bool:
    """Write the backup JSON file for a single partner.

    The file is written to ``{backup_root}/notes/partners/{partner_id}.json``.

    Args:
        partner_id: Primary key of the partner to back up.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    partner = (
        Partner.query
        .options(
            db.joinedload(Partner.contacts),
            db.joinedload(Partner.specialties),
        )
        .filter_by(id=partner_id)
        .first()
    )
    if not partner:
        logger.warning("Backup skipped: partner %d not found", partner_id)
        return False

    folder = os.path.join(backup_root, _NOTES_DIR, _PARTNERS_DIR)
    filepath = os.path.join(folder, f"{partner_id}.json")

    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        data = _partner_to_dict(partner)
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
        logger.debug("Partner backup written: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to write backup for partner %d", partner_id)
        return False


def delete_partner_backup(partner_id: int) -> bool:
    """Remove the backup JSON file for a deleted partner.

    Args:
        partner_id: Primary key of the partner whose backup to remove.

    Returns:
        True if the file was removed (or didn't exist), False on error.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    filepath = os.path.join(backup_root, _NOTES_DIR, _PARTNERS_DIR, f"{partner_id}.json")
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Partner backup deleted: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to delete backup for partner %d", partner_id)
        return False


def backup_template(template_id: int) -> bool:
    """Write the backup JSON file for a single note template.

    The file is written to ``{backup_root}/notes/templates/{template_id}.json``.

    Args:
        template_id: Primary key of the template to back up.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    template = NoteTemplate.query.filter_by(id=template_id).first()
    if not template:
        logger.warning("Backup skipped: template %d not found", template_id)
        return False

    folder = os.path.join(backup_root, _NOTES_DIR, _TEMPLATES_DIR)
    filepath = os.path.join(folder, f"{template_id}.json")

    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        data = _template_to_dict(template)
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
        logger.debug("Template backup written: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to write backup for template %d", template_id)
        return False


def delete_template_backup(template_id: int) -> bool:
    """Remove the backup JSON file for a deleted template.

    Args:
        template_id: Primary key of the template whose backup to remove.

    Returns:
        True if the file was removed (or didn't exist), False on error.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    filepath = os.path.join(backup_root, _NOTES_DIR, _TEMPLATES_DIR, f"{template_id}.json")
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Template backup deleted: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to delete backup for template %d", template_id)
        return False


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
            db.joinedload(Customer.notes).joinedload(Note.partners).joinedload(Partner.contacts),
            db.joinedload(Customer.notes).joinedload(Note.partners).joinedload(Partner.specialties),
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
            db.joinedload(Customer.notes).joinedload(Note.partners).joinedload(Partner.contacts),
            db.joinedload(Customer.notes).joinedload(Note.partners).joinedload(Partner.specialties),
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

    # Also back up global (non-customer-specific) data
    if not backup_global_data():
        logger.warning("Global data backup failed during backup_all_customers")

    # Back up all partners individually
    for partner in Partner.query.all():
        if not backup_partner(partner.id):
            logger.warning("Partner backup failed for %d", partner.id)

    # Back up all templates individually
    for template in NoteTemplate.query.all():
        if not backup_template(template.id):
            logger.warning("Template backup failed for %d", template.id)

    return {"backed_up": backed_up, "failed": failed}


def clear_backup_notes() -> dict:
    """Remove all JSON backup files and their seller folders from OneDrive.

    Walks the ``notes/`` tree (and legacy ``call_logs/`` if present),
    deleting files individually then removing empty directories bottom-up.
    Individual file deletion works reliably with OneDrive Files On-Demand
    where ``shutil.rmtree`` may fail on cloud-only placeholders.

    The ``previous_years/`` folder is untouched.

    Returns:
        Dict with ``deleted`` file count and ``errors`` count.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return {"deleted": 0, "errors": 0, "skipped": True}

    deleted = 0
    errors = 0

    for dirname in (_NOTES_DIR, _LEGACY_NOTES_DIR):
        target = os.path.join(backup_root, dirname)
        if not os.path.isdir(target):
            continue

        # Delete files first (bottom-up walk so we can rmdir after)
        for root, dirs, files in os.walk(target, topdown=False):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    os.remove(fpath)
                    deleted += 1
                except Exception:
                    logger.exception("Failed to remove file %s", fpath)
                    errors += 1
            # Remove now-empty subdirectories
            for dname in dirs:
                dpath = os.path.join(root, dname)
                try:
                    os.rmdir(dpath)
                except OSError:
                    pass  # not empty or already gone

        # Remove the top-level directory itself
        try:
            os.rmdir(target)
        except OSError:
            pass

    # Ensure the notes dir exists for the upcoming backup
    Path(os.path.join(backup_root, _NOTES_DIR)).mkdir(parents=True, exist_ok=True)

    return {"deleted": deleted, "errors": errors}


def _global_data_to_dict() -> Dict[str, Any]:
    """Serialize non-customer-specific user data for backup.

    Includes note templates, user preferences, specialties, and revenue config.
    """
    from app.models import NoteTemplate, UserPreference, RevenueConfig, ConnectExport

    # Note templates (user-created only, skip builtins)
    templates = NoteTemplate.query.order_by(NoteTemplate.id).all()
    templates_data = [
        {
            "name": t.name,
            "content": t.content,
            "is_builtin": t.is_builtin,
        }
        for t in templates
    ]

    # User preferences (single-user app, so just grab first row if exists)
    pref = UserPreference.query.first()
    prefs_data = None
    if pref:
        # Store default template references by name for portability
        cust_tmpl = pref.default_template_customer
        noncust_tmpl = pref.default_template_noncustomer
        prefs_data = {
            "dark_mode": pref.dark_mode,
            "customer_view_grouped": pref.customer_view_grouped,
            "customer_sort_by": pref.customer_sort_by,
            "topic_sort_by_calls": pref.topic_sort_by_calls,
            "territory_view_accounts": pref.territory_view_accounts,
            "show_customers_without_calls": pref.show_customers_without_calls,
            "workiq_summary_prompt": pref.workiq_summary_prompt,
            "workiq_connect_impact": pref.workiq_connect_impact,
            "default_template_customer_name": cust_tmpl.name if cust_tmpl else None,
            "default_template_noncustomer_name": noncust_tmpl.name if noncust_tmpl else None,
        }

    # Specialties (standalone entities not tied to a customer)
    specialties = Specialty.query.order_by(Specialty.name).all()
    specialties_data = [
        {
            "name": s.name,
            "description": s.description,
        }
        for s in specialties
    ]

    # Revenue config (user-tuned thresholds)
    rev_config = RevenueConfig.query.first()
    rev_config_data = None
    if rev_config:
        rev_config_data = {
            "min_revenue_for_outreach": rev_config.min_revenue_for_outreach,
            "min_dollar_impact": rev_config.min_dollar_impact,
            "dollar_at_risk_override": rev_config.dollar_at_risk_override,
            "dollar_opportunity_override": rev_config.dollar_opportunity_override,
            "high_value_threshold": rev_config.high_value_threshold,
            "strategic_threshold": rev_config.strategic_threshold,
            "volatile_min_revenue": rev_config.volatile_min_revenue,
            "recent_drop_threshold": rev_config.recent_drop_threshold,
            "expansion_growth_threshold": rev_config.expansion_growth_threshold,
        }

    # Connect exports (AI-generated summaries are user work product)
    connect_exports = ConnectExport.query.order_by(ConnectExport.created_at).all()
    connect_data = [
        {
            "name": ce.name,
            "start_date": ce.start_date.isoformat(),
            "end_date": ce.end_date.isoformat(),
            "note_count": ce.note_count,
            "customer_count": ce.customer_count,
            "ai_summary": ce.ai_summary,
            "created_at": ce.created_at.isoformat() if ce.created_at else None,
        }
        for ce in connect_exports
    ]

    # Partners (standalone entities that span customers and fiscal years)
    partners = Partner.query.order_by(Partner.name).all()
    partners_data = [
        {
            "name": p.name,
            "overview": p.overview,
            "rating": p.rating,
            "website": p.website,
            "contacts": [
                {
                    "name": c.name,
                    "email": c.email,
                    "is_primary": c.is_primary,
                }
                for c in p.contacts
            ],
            "specialties": [s.name for s in p.specialties],
        }
        for p in partners
    ]

    return {
        "_salesbuddy_global_backup": True,
        "_version": 5,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "note_templates": templates_data,
        "user_preferences": prefs_data,
        "specialties": specialties_data,
        "revenue_config": rev_config_data,
        "connect_exports": connect_data,
        "partners": partners_data,
    }


def backup_global_data() -> bool:
    """Write global (non-customer-specific) data to a JSON backup file.

    The file is written to ``{backup_root}/notes/_global.json``.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    backup_root = _get_backup_root()
    if not backup_root:
        return False

    folder = os.path.join(backup_root, _NOTES_DIR)
    filepath = os.path.join(folder, "_global.json")

    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        data = _global_data_to_dict()
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
        logger.debug("Global backup written: %s", filepath)
        return True
    except Exception:
        logger.exception("Failed to write global backup")
        return False


def find_backup_folder() -> Optional[str]:
    """Find the notes backup folder, trying config first then auto-detect.

    Checks both the new folder name (``notes``) and the legacy name
    (``call_logs``) for backward compatibility.

    Returns:
        Absolute path to the backup subfolder, or None if not found.
    """
    # 1. Try _get_backup_root (reads DB, then auto-detects)
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

    # Restore global data if present
    global_path = os.path.join(notes_dir, "_global.json")
    if os.path.isfile(global_path):
        try:
            with open(global_path, "r", encoding="utf-8-sig") as f:
                global_data = json.load(f)
            if global_data.get("_salesbuddy_global_backup"):
                restore_global_data(global_data)
                logger.info("Global data restored from %s", global_path)
        except Exception as exc:
            logger.warning("Failed to restore global data: %s", exc)
            errors.append(f"_global.json: {exc}")

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

    if not data.get("_salesbuddy_backup"):
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
            estimated_acr=_parse_acr_value(eng_data.get("estimated_acr")),
            target_date=target_date,
        )
        db.session.add(eng)
        db.session.flush()

        _restore_engagement_links(eng, eng_data, note_index, Milestone, Opportunity)
        engagements_created += 1

    db.session.commit()

    # ------------------------------------------------------------------
    # Restore customer metadata (v4+)
    # ------------------------------------------------------------------
    backup_context = (
        cust_data.get("account_context")
        or cust_data.get("overview")
        or cust_data.get("notes")
    )
    if backup_context and not customer.account_context:
        customer.account_context = backup_context

    if cust_data.get("website") and not customer.website:
        customer.website = cust_data["website"]
    if cust_data.get("favicon_b64") and not customer.favicon_b64:
        customer.favicon_b64 = cust_data["favicon_b64"]

    # ------------------------------------------------------------------
    # Restore full partner records (v4+)
    # ------------------------------------------------------------------
    for p_data in data.get("partners", []):
        p_name = p_data.get("name")
        if not p_name:
            continue
        partner = Partner.query.filter_by(name=p_name).first()
        if not partner:
            partner = Partner(name=p_name)
            db.session.add(partner)
            db.session.flush()

        # Enrich existing partner with backup data if fields are empty
        p_text_notes = p_data.get("notes")
        if p_text_notes:
            # Use raw SQL to set the text 'notes' column (shadowed by
            # relationship).  The column may not exist if migrations haven't
            # run, so we silently skip on error.
            try:
                db.session.execute(
                    db.text("UPDATE partners SET notes = :notes WHERE id = :id AND (notes IS NULL OR notes = '')"),
                    {"notes": p_text_notes, "id": partner.id},
                )
            except Exception:
                pass
        if p_data.get("rating") is not None and partner.rating is None:
            partner.rating = p_data["rating"]

        # Restore contacts (dedup by name)
        existing_contact_names = {c.name.lower() for c in partner.contacts}
        for c_data in p_data.get("contacts", []):
            c_name = c_data.get("name")
            if not c_name or c_name.lower() in existing_contact_names:
                continue
            contact = PartnerContact(
                partner_id=partner.id,
                name=c_name,
                email=c_data.get("email"),
                is_primary=c_data.get("is_primary", False),
            )
            db.session.add(contact)
            existing_contact_names.add(c_name.lower())

        # Restore specialty links
        existing_spec_names = {s.name.lower() for s in partner.specialties}
        for spec_name in p_data.get("specialties", []):
            if spec_name.lower() in existing_spec_names:
                continue
            spec = Specialty.query.filter_by(name=spec_name).first()
            if not spec:
                spec = Specialty(name=spec_name)
                db.session.add(spec)
                db.session.flush()
            partner.specialties.append(spec)
            existing_spec_names.add(spec_name.lower())

    # ------------------------------------------------------------------
    # Restore topic descriptions (v4+)
    # ------------------------------------------------------------------
    for t_data in data.get("topics", []):
        t_name = t_data.get("name")
        if not t_name:
            continue
        topic = Topic.query.filter_by(name=t_name).first()
        if topic and t_data.get("description") and not topic.description:
            topic.description = t_data["description"]

    db.session.commit()

    return {
        "success": True,
        "customer_name": customer.name,
        "logs_created": logs_created,
        "logs_skipped": logs_skipped,
        "engagements_created": engagements_created,
        "engagements_skipped": engagements_skipped,
    }


def restore_global_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Restore global (non-customer) data from a ``_global.json`` payload.

    Handles: NoteTemplates, Specialties, UserPreference, RevenueConfig,
    and ConnectExports.  All operations are idempotent — existing records
    are left unchanged.

    Returns a dict summarising what was created/skipped per section.
    """
    results: Dict[str, Any] = {}

    # --- Note Templates ---
    templates_data = data.get("note_templates", [])
    created = skipped = 0
    for t in templates_data:
        name = t.get("name")
        if not name:
            continue
        existing = NoteTemplate.query.filter_by(name=name).first()
        if existing:
            # Enrich: update content if backup has newer/different content
            if t.get("content") and existing.content != t["content"]:
                existing.content = t["content"]
            skipped += 1
            continue
        tmpl = NoteTemplate(
            name=name,
            content=t.get("content", ""),
            is_builtin=t.get("is_builtin", False),
        )
        db.session.add(tmpl)
        created += 1
    db.session.flush()
    results["note_templates"] = {"created": created, "skipped": skipped}

    # --- Specialties ---
    specs_data = data.get("specialties", [])
    created = skipped = 0
    for s in specs_data:
        name = s.get("name")
        if not name:
            continue
        existing = Specialty.query.filter_by(name=name).first()
        if existing:
            if s.get("description") and not existing.description:
                existing.description = s["description"]
            skipped += 1
            continue
        spec = Specialty(name=name, description=s.get("description"))
        db.session.add(spec)
        created += 1
    db.session.flush()
    results["specialties"] = {"created": created, "skipped": skipped}

    # --- User Preference ---
    pref_data = data.get("user_preferences") or data.get("user_preference")
    if pref_data:
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference()
            db.session.add(pref)
            db.session.flush()

        # Only set fields that have values in the backup and are still default
        pref_fields = [
            "dark_mode",
            "customer_view_grouped",
            "customer_sort_by",
            "topic_sort_by_calls",
            "territory_view_accounts",
            "show_customers_without_calls",
            "workiq_summary_prompt",
            "workiq_connect_impact",
            "ai_enabled",
        ]
        for field in pref_fields:
            if field in pref_data and pref_data[field] is not None:
                setattr(pref, field, pref_data[field])

        # Resolve default templates by name
        for attr, key in [
            ("default_template_customer_id", "default_template_customer_name"),
            ("default_template_noncustomer_id", "default_template_noncustomer_name"),
        ]:
            tmpl_name = pref_data.get(key)
            if tmpl_name:
                tmpl = NoteTemplate.query.filter_by(name=tmpl_name).first()
                if tmpl:
                    setattr(pref, attr, tmpl.id)

        results["user_preference"] = "restored"
    else:
        results["user_preference"] = "not_in_backup"

    # --- Revenue Config ---
    rc_data = data.get("revenue_config")
    if rc_data:
        rc = RevenueConfig.query.first()
        if not rc:
            rc = RevenueConfig()
            db.session.add(rc)
        rc_fields = [
            "min_revenue_for_outreach",
            "min_dollar_impact",
            "dollar_at_risk_override",
            "dollar_opportunity_override",
            "high_value_threshold",
            "strategic_threshold",
            "volatile_min_revenue",
            "recent_drop_threshold",
            "expansion_growth_threshold",
        ]
        for field in rc_fields:
            if field in rc_data and rc_data[field] is not None:
                setattr(rc, field, rc_data[field])
        results["revenue_config"] = "restored"
    else:
        results["revenue_config"] = "not_in_backup"

    # --- Connect Exports ---
    ce_data = data.get("connect_exports", [])
    created = skipped = 0
    for ce in ce_data:
        name = ce.get("name")
        start_date = ce.get("start_date")
        if not name:
            continue
        # Dedup by name + start_date
        existing_q = ConnectExport.query.filter_by(name=name)
        if start_date:
            existing_q = existing_q.filter_by(start_date=start_date)
        if existing_q.first():
            skipped += 1
            continue
        # Parse ISO date strings to Python date objects for SQLite
        from datetime import date as _date
        parsed_start = None
        if start_date:
            try:
                parsed_start = _date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
            except (ValueError, TypeError):
                parsed_start = None
        end_date_raw = ce.get("end_date")
        parsed_end = None
        if end_date_raw:
            try:
                parsed_end = _date.fromisoformat(end_date_raw) if isinstance(end_date_raw, str) else end_date_raw
            except (ValueError, TypeError):
                parsed_end = None
        export = ConnectExport(
            name=name,
            start_date=parsed_start,
            end_date=parsed_end,
            note_count=ce.get("note_count", 0),
            customer_count=ce.get("customer_count", 0),
            ai_summary=ce.get("ai_summary"),
        )
        db.session.add(export)
        created += 1
    results["connect_exports"] = {"created": created, "skipped": skipped}

    # --- Partners ---
    partners_data = data.get("partners", [])
    created = skipped = 0
    for p in partners_data:
        name = p.get("name")
        if not name:
            continue
        existing = Partner.query.filter_by(name=name).first()
        if existing:
            # Enrich existing partner with missing fields
            if p.get("overview") and not existing.overview:
                existing.overview = p["overview"]
            if p.get("rating") is not None and existing.rating is None:
                existing.rating = p["rating"]
            if p.get("website") and not existing.website:
                existing.website = p["website"]
            # Add missing contacts
            existing_emails = {c.email for c in existing.contacts if c.email}
            existing_names = {c.name for c in existing.contacts}
            for cd in p.get("contacts", []):
                if cd.get("email") and cd["email"] in existing_emails:
                    continue
                if cd.get("name") in existing_names:
                    continue
                contact = PartnerContact(
                    partner_id=existing.id,
                    name=cd["name"],
                    email=cd.get("email"),
                    is_primary=cd.get("is_primary", False),
                )
                db.session.add(contact)
            # Link specialties that exist
            existing_spec_names = {s.name for s in existing.specialties}
            for spec_name in p.get("specialties", []):
                if spec_name not in existing_spec_names:
                    spec = Specialty.query.filter_by(name=spec_name).first()
                    if spec:
                        existing.specialties.append(spec)
            skipped += 1
            continue
        partner = Partner(
            name=name,
            overview=p.get("overview"),
            rating=p.get("rating"),
            website=p.get("website"),
        )
        db.session.add(partner)
        db.session.flush()
        for cd in p.get("contacts", []):
            if not cd.get("name"):
                continue
            contact = PartnerContact(
                partner_id=partner.id,
                name=cd["name"],
                email=cd.get("email"),
                is_primary=cd.get("is_primary", False),
            )
            db.session.add(contact)
        for spec_name in p.get("specialties", []):
            spec = Specialty.query.filter_by(name=spec_name).first()
            if spec:
                partner.specialties.append(spec)
        created += 1
    results["partners"] = {"created": created, "skipped": skipped}

    db.session.commit()
    return results


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
