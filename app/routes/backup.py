"""
Backup routes -- admin panel endpoints for file-based call log backup.

Provides:
- Status API (enabled, path, file count)
- Backup-all trigger
- DR restore from JSON file
"""

import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from app.services.backup import (
    _get_backup_root,
    _load_db_backup_config,
    backup_all_customers,
    detect_onedrive_paths,
    find_backup_folder,
    get_auto_detected_backup_path,
    restore_all_from_folder,
    restore_from_backup,
)

logger = logging.getLogger(__name__)

backup_bp = Blueprint("backup", __name__)


# -------------------------------------------------------------------------
# OneDrive detection
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/detect-onedrive")
def backup_detect_onedrive():
    """Detect OneDrive paths and return candidates for the admin panel."""
    candidates = detect_onedrive_paths()
    auto_path = get_auto_detected_backup_path()

    return jsonify({
        "candidates": candidates,
        "auto_path": auto_path,
    })


# -------------------------------------------------------------------------
# Status & config
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/status")
def backup_status():
    """Return backup status for the admin panel.

    Backups are automatically enabled when a OneDrive for Business path
    is configured in ``backup_config.json`` or auto-detected.  There is
    no separate enable/disable toggle.
    """
    backup_root = _get_backup_root()
    enabled = backup_root is not None

    # Count .json files in the notes subfolder (check both new and legacy names)
    file_count = 0
    if backup_root:
        for dirname in ("notes", "call_logs"):
            notes_dir = os.path.join(backup_root, dirname)
            if os.path.isdir(notes_dir):
                for root, _dirs, files in os.walk(notes_dir):
                    file_count += sum(1 for f in files if f.endswith(".json"))
                break  # Only count from first folder found

    return jsonify({
        "enabled": enabled,
        "backup_path": backup_root or "",
        "file_count": file_count,
    })


# -------------------------------------------------------------------------
# Backup all
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/backup-all", methods=["POST"])
def backup_all():
    """Write backup files for all customers with call logs."""
    if not _get_backup_root():
        return jsonify({"success": False, "error": "No backup location available"}), 400

    result = backup_all_customers()
    return jsonify({"success": True, **result})


@backup_bp.route("/api/backup/clear-notes", methods=["POST"])
def clear_notes():
    """Delete all JSON files from the OneDrive notes backup folder."""
    from app.services.backup import clear_backup_notes
    result = clear_backup_notes()
    if result.get("skipped"):
        return jsonify({"success": True, "message": "No backup location configured; skipped."})
    return jsonify({"success": True, **result})


# -------------------------------------------------------------------------
# DR restore
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/restore", methods=["POST"])
def backup_restore():
    """Restore call logs from a backup JSON payload.

    Expects a JSON body with the backup data structure produced by
    ``_customer_to_dict``.  Matches the customer by TPID and creates
    any missing call logs.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON payload"}), 400

    result = restore_from_backup(data)
    status_code = 200 if result.get("success") else 400
    if result.get("error") and "not found" in result["error"].lower():
        status_code = 404
    return jsonify(result), status_code


# -------------------------------------------------------------------------
# Full DR restore from backup folder
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/restore-all", methods=["POST"])
def backup_restore_all():
    """Restore all call logs from the backup folder on disk.

    Reads every .json file under ``notes/{seller}/`` in the configured
    (or auto-detected) backup folder.  Requires accounts to have been
    synced first so customer TPID matching works.
    """
    from app.models import SyncStatus

    if not SyncStatus.is_complete("accounts"):
        return jsonify({
            "success": False,
            "error": "Accounts must be synced before restoring notes. "
                     "Run the MSX import first.",
        }), 400

    result = restore_all_from_folder()
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code
