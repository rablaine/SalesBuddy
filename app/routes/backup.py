"""
Backup routes -- admin panel endpoints for file-based call log backup.

Provides:
- Status API (enabled, path, file count)
- Enable / disable / configure path
- Backup-all trigger
- DR restore from JSON file
"""

import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from app.services.backup import (
    backup_all_customers,
    detect_onedrive_paths,
    find_backup_folder,
    get_auto_detected_backup_path,
    load_config,
    restore_all_from_folder,
    restore_from_backup,
    save_config,
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
    """Return backup status for the admin panel."""
    config = load_config()

    # Count .json files in the call_logs subfolder
    file_count = 0
    call_logs_dir = os.path.join(config.get("backup_path", ""), "call_logs")
    if os.path.isdir(call_logs_dir):
        for root, _dirs, files in os.walk(call_logs_dir):
            file_count += sum(1 for f in files if f.endswith(".json"))

    return jsonify({
        "enabled": config.get("enabled", False),
        "backup_path": config.get("backup_path", ""),
        "file_count": file_count,
    })


@backup_bp.route("/api/backup/enable", methods=["POST"])
def backup_enable():
    """Enable file backup with the given path."""
    data = request.get_json(silent=True) or {}
    backup_path = data.get("backup_path", "").strip()

    if not backup_path:
        return jsonify({"success": False, "error": "Backup path is required"}), 400

    # Validate the path exists (or can be created)
    try:
        os.makedirs(backup_path, exist_ok=True)
    except OSError as exc:
        return jsonify({"success": False, "error": f"Invalid path: {exc}"}), 400

    config = load_config()
    config["backup_path"] = backup_path
    config["enabled"] = True
    save_config(config)

    return jsonify({"success": True, "backup_path": backup_path})


@backup_bp.route("/api/backup/disable", methods=["POST"])
def backup_disable():
    """Disable file backup (leaves existing files intact)."""
    config = load_config()
    config["enabled"] = False
    save_config(config)
    return jsonify({"success": True})


# -------------------------------------------------------------------------
# Backup all
# -------------------------------------------------------------------------

@backup_bp.route("/api/backup/backup-all", methods=["POST"])
def backup_all():
    """Write backup files for all customers with call logs."""
    config = load_config()
    if not config.get("enabled"):
        return jsonify({"success": False, "error": "Backup is not enabled"}), 400

    result = backup_all_customers()
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

    Reads every .json file under ``call_logs/{seller}/`` in the configured
    (or auto-detected) backup folder.  Requires accounts to have been
    synced first so customer TPID matching works.
    """
    from app.models import SyncStatus

    if not SyncStatus.is_complete("accounts"):
        return jsonify({
            "success": False,
            "error": "Accounts must be synced before restoring call logs. "
                     "Run the MSX import first.",
        }), 400

    result = restore_all_from_folder()
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code
