"""
File-based backup service for call log disaster recovery.

Writes per-customer JSON files organized by seller name into a OneDrive-synced
folder.  OneDrive handles cloud sync transparently, giving us RPO=0 backups
with zero external API calls.

The backup path is derived automatically from ``data/backup_config.json``
(the DB backup config written by ``scripts/server.ps1``) or auto-detected
from OneDrive for Business.  There is no separate call-log-specific config
file -- if DB backups are configured, call log backups use the same path.

Folder structure::

    {BACKUP_ROOT}/
        call_logs/
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

from app.models import CallLog, Customer, db

logger = logging.getLogger(__name__)

# Subfolder inside the configured backup root
_CALL_LOGS_DIR = "call_logs"

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

    No separate call-log config file is needed.
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
    """Serialize a customer and all call logs to a backup dict.

    Args:
        customer: Customer with eagerly-loaded relationships.

    Returns:
        Dictionary ready for JSON serialization.
    """
    call_logs: List[CallLog] = sorted(
        customer.call_logs, key=lambda cl: cl.call_date, reverse=True
    )

    return {
        "_notehelper_backup": True,
        "_version": 2,
        "_exported_at": datetime.now(timezone.utc).isoformat(),
        "customer": {
            "name": customer.name,
            "nickname": customer.nickname,
            "tpid": customer.tpid,
            "tpid_url": customer.tpid_url,
            "notes": customer.notes,
            "seller_name": customer.seller.name if customer.seller else None,
            "territory_name": customer.territory.name if customer.territory else None,
            "verticals": [v.name for v in customer.verticals],
        },
        "call_logs": [
            {
                "call_date": cl.call_date.isoformat(),
                "content": cl.content,
                "created_at": cl.created_at.isoformat() if cl.created_at else None,
                "updated_at": cl.updated_at.isoformat() if cl.updated_at else None,
                "topics": [t.name for t in cl.topics],
                "partners": [p.name for p in cl.partners],
            }
            for cl in call_logs
        ],
    }


def backup_customer(customer_id: int) -> bool:
    """Write the backup JSON file for a single customer.

    The file is written to ``{backup_root}/call_logs/{seller}/{tpid}.json``.
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
            db.joinedload(Customer.call_logs).joinedload(CallLog.topics),
            db.joinedload(Customer.call_logs).joinedload(CallLog.partners),
        )
        .filter_by(id=customer_id)
        .first()
    )
    if not customer:
        logger.warning("Backup skipped: customer %d not found", customer_id)
        return False

    # Determine folder and filename
    seller_name = customer.seller.name if customer.seller else "Unassigned"
    folder = os.path.join(backup_root, _CALL_LOGS_DIR, _sanitize_folder_name(seller_name))
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
        .filter(Customer.call_logs.any())
        .options(
            db.joinedload(Customer.seller),
            db.joinedload(Customer.territory),
            db.joinedload(Customer.verticals),
            db.joinedload(Customer.call_logs).joinedload(CallLog.topics),
            db.joinedload(Customer.call_logs).joinedload(CallLog.partners),
        )
        .all()
    )

    backed_up = 0
    failed = 0
    for customer in customers:
        seller_name = customer.seller.name if customer.seller else "Unassigned"
        folder = os.path.join(backup_root, _CALL_LOGS_DIR, _sanitize_folder_name(seller_name))
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
    """Find the call_logs backup folder, trying config first then auto-detect.

    Returns:
        Absolute path to the ``call_logs`` subfolder, or None if not found.
    """
    # 1. Try _get_backup_root (reads backup_config.json, then auto-detects)
    backup_root = _get_backup_root()
    if backup_root:
        call_logs_dir = os.path.join(backup_root, _CALL_LOGS_DIR)
        if os.path.isdir(call_logs_dir):
            return call_logs_dir

    # 2. Walk all candidates as last resort
    for candidate in detect_onedrive_paths():
        call_logs_dir = os.path.join(candidate["suggested_path"], _CALL_LOGS_DIR)
        if os.path.isdir(call_logs_dir):
            return call_logs_dir

    return None


def restore_all_from_folder(call_logs_dir: Optional[str] = None) -> Dict[str, Any]:
    """Restore all call logs from a backup folder.

    Walks ``{call_logs_dir}/{seller}/`` subfolders, reads every ``.json``
    file, and calls ``restore_from_backup()`` for each.

    Args:
        call_logs_dir: Path to the ``call_logs`` folder.  If None, will
            auto-detect using ``find_backup_folder()``.

    Returns:
        Result dict with aggregate success/failure counts.
    """
    if call_logs_dir is None:
        call_logs_dir = find_backup_folder()

    if not call_logs_dir or not os.path.isdir(call_logs_dir):
        return {
            "success": False,
            "error": "Could not find call_logs backup folder. "
                     "Enable backup or verify OneDrive path.",
        }

    files_processed = 0
    files_failed = 0
    total_logs_created = 0
    total_logs_skipped = 0
    customers_restored = []
    errors = []

    for seller_folder in sorted(os.listdir(call_logs_dir)):
        seller_path = os.path.join(call_logs_dir, seller_folder)
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
        "backup_folder": call_logs_dir,
    }


def restore_from_backup(data: Dict[str, Any]) -> Dict[str, Any]:
    """Restore call logs from a backup JSON dict.

    Matches the customer by TPID, then creates any call logs that don't
    already exist (deduplicates by call_date).

    Args:
        data: Parsed backup JSON dict.

    Returns:
        Result dict with success status and counts.
    """
    from flask import g
    from app.models import Partner, Topic

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

    existing_dates = set()
    for cl in customer.call_logs:
        # Normalize to UTC-aware datetime then compare as isoformat
        dt = cl.call_date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        existing_dates.add(dt.isoformat())
    logs_created = 0
    logs_skipped = 0

    for cl_data in data.get("call_logs", []):
        call_date_str = cl_data.get("call_date")
        if not call_date_str:
            logs_skipped += 1
            continue

        try:
            call_date = datetime.fromisoformat(call_date_str)
        except (ValueError, TypeError):
            logs_skipped += 1
            continue

        # Normalize for dedup comparison
        normalized = call_date if call_date.tzinfo else call_date.replace(tzinfo=timezone.utc)
        if normalized.isoformat() in existing_dates:
            logs_skipped += 1
            continue

        call_log = CallLog(
            customer_id=customer.id,
            call_date=call_date,
            content=cl_data.get("content", ""),
        )

        for topic_name in cl_data.get("topics", []):
            topic = Topic.query.filter_by(name=topic_name).first()
            if not topic:
                topic = Topic(name=topic_name)
                db.session.add(topic)
                db.session.flush()
            call_log.topics.append(topic)

        for partner_name in cl_data.get("partners", []):
            partner = Partner.query.filter_by(name=partner_name).first()
            if not partner:
                partner = Partner(name=partner_name)
                db.session.add(partner)
                db.session.flush()
            call_log.partners.append(partner)

        db.session.add(call_log)
        existing_dates.add(normalized.isoformat())
        logs_created += 1

    db.session.commit()

    # Restore customer notes if present in backup and customer has no notes yet
    backup_notes = cust_data.get("notes")
    if backup_notes and not customer.notes:
        customer.notes = backup_notes
        db.session.commit()

    return {
        "success": True,
        "customer_name": customer.name,
        "logs_created": logs_created,
        "logs_skipped": logs_skipped,
    }
