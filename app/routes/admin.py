"""
Admin routes for NoteHelper.
Handles admin panel, user management, and domain whitelisting.
"""
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify, g

from app.models import (
    db, User, POD, Territory, Seller, Customer, Topic, Note, AIQueryLog,
    RevenueImport, CustomerRevenueData, ProductRevenueData, RevenueAnalysis,
    RevenueConfig, RevenueEngagement, Milestone, Opportunity, MsxTask,
    SolutionEngineer, SyncStatus, UserPreference, UsageEvent, DailyFeatureStats,
    notes_milestones, utc_now
)

# Create blueprint
admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
def admin_panel():
    """Admin control panel for system-wide operations."""
    import os
    from datetime import datetime, timedelta, timezone
    
    # Get favicon sync status and check if actively in progress
    favicon_sync = SyncStatus.get_status('favicons')
    favicon_sync['in_progress'] = False
    if favicon_sync['state'] == 'incomplete' and favicon_sync['started_at']:
        # Consider "in progress" if started within last 10 minutes
        # Make started_at timezone-aware if it's naive (SQLite returns naive datetimes)
        started_at = favicon_sync['started_at']
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age = utc_now() - started_at
        favicon_sync['in_progress'] = age < timedelta(minutes=10)
    
    # Get system-wide statistics
    stats = {
        'total_notes': Note.query.count(),
        'total_customers': Customer.query.count(),
        'total_sellers': Seller.query.count(),
        'total_solution_engineers': SolutionEngineer.query.count(),
        'total_territories': Territory.query.count(),
        'total_pods': POD.query.count(),
        'total_topics': Topic.query.count(),
        'total_milestones': Milestone.query.count(),
        'total_opportunities': Opportunity.query.count(),
        'total_msx_tasks': MsxTask.query.count(),
        'total_revenue_records': CustomerRevenueData.query.count() + ProductRevenueData.query.count(),
        'total_revenue_analyses': RevenueAnalysis.query.count(),
        'total_revenue_imports': RevenueImport.query.count(),
        'customers_with_website': Customer.query.filter(
            Customer.website.isnot(None), Customer.website != '').count(),
        'customers_with_favicon': Customer.query.filter(
            Customer.favicon_b64.isnot(None), Customer.favicon_b64 != '').count(),
        'favicon_sync_status': favicon_sync,
    }
    
    # FY season: promote the FY card Jul 1 – Aug 31, unless already transitioned
    now = datetime.now()
    fy_season = (7 <= now.month <= 8)
    pref = UserPreference.query.first()
    # If they've already completed this year's transition, don't promote
    if pref and pref.fy_transition_started:
        started = pref.fy_transition_started
        # If transition was started in the current FY window, season is over for them
        if started.year == now.year and started.month >= 7:
            fy_season = False

    return render_template('admin_panel.html', stats=stats, fy_season=fy_season)


@admin_bp.route('/admin/ai-logs')
def admin_ai_logs():
    """View AI query logs for debugging."""
    # Get recent logs (last 50)
    logs = AIQueryLog.query.order_by(AIQueryLog.timestamp.desc()).limit(50).all()
    
    return render_template('admin_ai_logs.html', logs=logs)


@admin_bp.route('/api/admin/clear-revenue', methods=['POST'])
def api_clear_revenue_data():
    """Delete all revenue data (imports, records, analyses, engagements, config)."""
    try:
        deleted = {}
        deleted['engagements'] = RevenueEngagement.query.delete()
        deleted['analyses'] = RevenueAnalysis.query.delete()
        deleted['product_records'] = ProductRevenueData.query.delete()
        deleted['bucket_records'] = CustomerRevenueData.query.delete()
        deleted['imports'] = RevenueImport.query.delete()
        deleted['configs'] = RevenueConfig.query.delete()
        # Reset sync statuses so wizard/UI returns to clean state
        SyncStatus.reset('revenue_import')
        SyncStatus.reset('revenue_analysis')
        db.session.commit()
        total = sum(deleted.values())
        return jsonify({
            'success': True,
            'message': f'Deleted {total} revenue records.',
            'details': deleted
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/clear-milestones', methods=['POST'])
def api_clear_milestone_data():
    """Delete all milestone and opportunity data (milestones, opportunities, tasks, associations)."""
    try:
        deleted = {}
        # Clear associations first (FK constraints)
        deleted['note_links'] = db.session.execute(
            notes_milestones.delete()
        ).rowcount
        deleted['tasks'] = MsxTask.query.delete()
        deleted['milestones'] = Milestone.query.delete()
        deleted['opportunities'] = Opportunity.query.delete()
        # Reset sync status so wizard/UI returns to clean state
        SyncStatus.reset('milestones')
        db.session.commit()
        total = sum(deleted.values())
        return jsonify({
            'success': True,
            'message': f'Deleted {total} milestone/opportunity records.',
            'details': deleted
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# API routes
@admin_bp.route('/api/admin/domain/add', methods=['POST'])
def api_admin_domain_add():
    """Placeholder for domain add (no longer used but route kept for compatibility)."""
    return jsonify({'success': False, 'error': 'This endpoint is no longer available'}), 410


@admin_bp.route('/api/admin/ai-config/test', methods=['POST'])
def api_admin_ai_config_test():
    """Test AI configuration by pinging the APIM gateway."""
    from app.gateway_client import gateway_call, GatewayError, GatewayConsentError

    try:
        result = gateway_call("/v1/ping", {})
        return jsonify({
            'success': True,
            'message': 'Gateway connection successful!',
            'response': result.get('response', ''),
            'mode': 'gateway',
        })
    except GatewayConsentError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'needs_relogin': True,
        }), 403
    except GatewayError as e:
        return jsonify({'success': False, 'error': f'Gateway test failed: {e}'}), 400


@admin_bp.route('/api/admin/ai-consent-check', methods=['GET'])
def api_admin_ai_consent_check():
    """Check if the user has consented to the AI gateway app.

    Returns JSON with ``consented``, ``error``, ``needs_relogin``,
    and ``ai_enabled`` (whether AI features are active).
    """
    from app.gateway_client import check_ai_consent
    from app.models import UserPreference
    result = check_ai_consent()
    prefs = UserPreference.query.first()
    result['ai_enabled'] = bool(prefs and prefs.ai_enabled)
    return jsonify(result)


@admin_bp.route('/api/admin/ai-enable', methods=['POST'])
def api_admin_ai_enable():
    """Validate AI gateway consent and enable AI features.

    Checks that the user has a valid gateway token (consent granted),
    then sets ``ai_enabled = True`` on UserPreference.
    """
    from app.gateway_client import check_ai_consent
    from app.models import UserPreference

    consent = check_ai_consent()
    if not consent.get('consented'):
        return jsonify({
            'success': False,
            'ai_enabled': False,
            'error': consent.get('error', 'AI consent not granted'),
            'needs_relogin': consent.get('needs_relogin', False),
        }), 403

    # Consent verified — flip ai_enabled on
    prefs = UserPreference.query.first()
    if not prefs:
        prefs = UserPreference(ai_enabled=True)
        db.session.add(prefs)
    else:
        prefs.ai_enabled = True
    db.session.commit()

    return jsonify({
        'success': True,
        'ai_enabled': True,
        'message': 'AI features enabled!',
    })


@admin_bp.route('/api/admin/ai-clear-cache', methods=['POST'])
def api_admin_ai_clear_cache():
    """Clear the gateway token cache so the next consent check uses fresh credentials."""
    from app.gateway_client import clear_token_cache
    clear_token_cache()
    return jsonify({'success': True})


@admin_bp.route('/api/admin/ai-disable', methods=['POST'])
def api_admin_ai_disable():
    """Disable AI features by setting ``ai_enabled = False``."""
    from app.models import UserPreference

    prefs = UserPreference.query.first()
    if prefs:
        prefs.ai_enabled = False
        db.session.commit()

    return jsonify({
        'success': True,
        'ai_enabled': False,
        'message': 'AI features disabled.',
    })


@admin_bp.route('/api/admin/update-check', methods=['GET'])
def api_update_check():
    """Check for available updates and return current state."""
    from flask import current_app
    from app.services.update_checker import get_update_state, check_for_updates
    
    # If force refresh requested, run the check now
    if request.args.get('refresh') == '1':
        state = check_for_updates()
    else:
        state = get_update_state()
    
    # Include the boot-time commit (what the running server loaded)
    boot_commit = current_app.config.get('BOOT_COMMIT')
    state['boot_commit'] = boot_commit
    disk_commit = state.get('local_commit')
    state['restart_needed'] = (
        boot_commit is not None
        and disk_commit is not None
        and boot_commit != disk_commit
    )

    # Include dismissed commit from user prefs
    pref = UserPreference.query.first()
    dismissed = pref.dismissed_update_commit if pref else None
    
    # Update is "new" (show badge) if available and not dismissed for this remote commit
    state['dismissed'] = dismissed == state.get('remote_commit')
    state['show_badge'] = state.get('available', False) and not state['dismissed']
    
    return jsonify(state)


@admin_bp.route('/api/admin/shutdown', methods=['POST'])
def api_shutdown_server():
    """Shut down the running server process.

    Sends the response first, then terminates the process after a short
    delay so the client receives a clean JSON reply.
    """
    port = os.environ.get('PORT', '5151')

    def _shutdown():
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.0, _shutdown).start()
    return jsonify({
        'success': True,
        'message': f'Server on port {port} is shutting down...'
    })


@admin_bp.route('/api/admin/update-apply', methods=['POST'])
def api_update_apply():
    """Trigger a full update cycle (stop → pull → install → migrate → restart).

    Spawns server.ps1 -Force as a fully detached process so it survives
    this server instance being killed.  Returns immediately; the client
    should poll /health to detect when the new server is back up.
    """
    if sys.platform != 'win32':
        return jsonify({'error': 'Update is only supported on Windows'}), 400

    repo_root = Path(__file__).resolve().parent.parent.parent
    server_script = repo_root / 'scripts' / 'server.ps1'

    if not server_script.exists():
        return jsonify({'error': 'server.ps1 not found'}), 500

    # Read PORT from .env so we can pass through elevation if needed
    port = int(os.environ.get('PORT', '5151'))

    # Strategy: We need server.ps1 -Force to run in a process that:
    #   1. Has a real console host (so Write-Host works)
    #   2. Survives this Flask process being killed (SIGTERM)
    #   3. Runs invisibly (no console window flash)
    #
    # CREATE_NO_WINDOW (0x08000000) is the key: unlike DETACHED_PROCESS, it
    # gives the child a real (but invisible) console, so Write-Host works.
    # The child also survives parent SIGTERM.
    CREATE_NO_WINDOW = 0x08000000

    cmd = [
        'powershell.exe', '-ExecutionPolicy', 'Bypass',
        '-File', str(server_script), '-Force'
    ]

    try:
        log_file = repo_root / 'data' / 'update.log'
        with open(log_file, 'w') as lf:
            # Write-Host goes to console host, not stdout, so the log will
            # only capture stdout/stderr (pip, git, etc.).  That's fine —
            # the important thing is the process actually runs.
            subprocess.Popen(
                cmd,
                cwd=str(repo_root),
                creationflags=CREATE_NO_WINDOW,
                stdout=lf,
                stderr=subprocess.STDOUT,
            )
    except Exception as e:
        return jsonify({'error': f'Failed to launch update: {e}'}), 500

    # Cooperatively shut ourselves down after a short delay so server.ps1
    # finds the port free. The detached child will start a fresh server.
    def _self_terminate():
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.5, _self_terminate).start()

    return jsonify({
        'success': True,
        'message': 'Update started. The server will restart momentarily.',
    })


@admin_bp.route('/api/admin/update-dismiss', methods=['POST'])
def api_update_dismiss():
    """Dismiss the current update notification."""
    from app.services.update_checker import get_update_state
    
    state = get_update_state()
    remote_commit = state.get('remote_commit')
    
    if not remote_commit:
        return jsonify({'error': 'No update to dismiss'}), 400
    
    pref = UserPreference.query.first()
    if pref:
        pref.dismissed_update_commit = remote_commit
        db.session.commit()
    
    return jsonify({'dismissed': True, 'commit': remote_commit})


# ==============================================================================
# Favicon Fetch API
# ==============================================================================

# Google's generic globe favicon (1x1 or known hash) to detect "no real favicon"
_GOOGLE_GLOBE_SIZES = {726, 762, 764, 766, 768, 770, 786, 822, 834, 894, 896, 937}

GOOGLE_FAVICON_URL = "https://www.google.com/s2/favicons"
FAVICON_SIZE = 32


def _is_generic_globe(image_bytes: bytes) -> bool:
    """Detect if the fetched image is Google's generic globe icon.

    Checks if the response size matches known globe icon sizes (which vary
    slightly by format but are always small). Real favicons are typically
    larger or have distinct sizes.

    Args:
        image_bytes: Raw bytes from Google's favicon API.

    Returns:
        True if the image appears to be the generic globe.
    """
    return len(image_bytes) in _GOOGLE_GLOBE_SIZES


def fetch_favicon_for_domain(domain: str, timeout: int = 5) -> str | None:
    """Fetch a favicon from Google's API and return base64-encoded PNG.

    Args:
        domain: Clean domain string (e.g. 'example.com').
        timeout: HTTP request timeout in seconds.

    Returns:
        Base64-encoded PNG string, or None if unavailable/generic.
    """
    if not domain:
        return None
    try:
        resp = requests.get(
            GOOGLE_FAVICON_URL,
            params={"domain": domain, "sz": FAVICON_SIZE},
            timeout=timeout,
        )
        resp.raise_for_status()
        if _is_generic_globe(resp.content):
            return None
        return base64.b64encode(resp.content).decode("ascii")
    except Exception:
        return None


@admin_bp.route('/api/admin/fetch-favicons', methods=['POST'])
def api_fetch_favicons():
    """Fetch favicons for all customers that have a website but no favicon.

    Uses 4 parallel workers to speed up the Google favicon API calls.
    Skips customers that already have a favicon or have no website set.

    Returns:
        JSON with counts of fetched, skipped, and failed favicons.
    """
    try:
        SyncStatus.mark_started('favicons')

        customers = Customer.query.filter(
            Customer.website.isnot(None),
            Customer.website != '',
            (Customer.favicon_b64.is_(None)) | (Customer.favicon_b64 == ''),
        ).all()

        if not customers:
            SyncStatus.mark_completed('favicons', success=True, items_synced=0,
                                      details='No customers need favicon updates')
            return jsonify({
                'success': True,
                'message': 'No customers need favicon updates.',
                'fetched': 0, 'skipped': 0, 'failed': 0,
            })

        # Build work items: (customer_id, domain)
        work = [(c.id, c.website) for c in customers]

        # Parallel fetch (I/O bound - threads are ideal)
        results: dict[int, str | None] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_id = {
                pool.submit(fetch_favicon_for_domain, domain): cid
                for cid, domain in work
            }
            for future in as_completed(future_to_id):
                cid = future_to_id[future]
                results[cid] = future.result()

        # Apply results to DB
        fetched = 0
        failed = 0
        for cid, b64 in results.items():
            if b64:
                cust = db.session.get(Customer, cid)
                if cust:
                    cust.favicon_b64 = b64
                    fetched += 1
            else:
                failed += 1

        db.session.commit()
        SyncStatus.mark_completed(
            'favicons', success=True, items_synced=fetched,
            details=f'{fetched} fetched, {failed} unavailable',
        )
        return jsonify({
            'success': True,
            'message': f'Fetched {fetched} favicons, {failed} unavailable.',
            'fetched': fetched,
            'skipped': 0,
            'failed': failed,
        })
    except Exception as e:
        db.session.rollback()
        SyncStatus.mark_completed('favicons', success=False, details=str(e))
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/refresh-favicons', methods=['POST'])
def api_refresh_favicons():
    """Re-fetch ALL favicons, including ones already fetched.

    Uses 4 parallel workers. Useful when favicon quality improves or domains change.
    """
    try:
        SyncStatus.mark_started('favicons')

        customers = Customer.query.filter(
            Customer.website.isnot(None),
            Customer.website != '',
        ).all()

        if not customers:
            SyncStatus.mark_completed('favicons', success=True, items_synced=0,
                                      details='No customers with websites')
            return jsonify({
                'success': True,
                'message': 'No customers with websites.',
                'fetched': 0, 'failed': 0,
            })

        work = [(c.id, c.website) for c in customers]

        results: dict[int, str | None] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_id = {
                pool.submit(fetch_favicon_for_domain, domain): cid
                for cid, domain in work
            }
            for future in as_completed(future_to_id):
                cid = future_to_id[future]
                results[cid] = future.result()

        fetched = 0
        failed = 0
        for cid, b64 in results.items():
            cust = db.session.get(Customer, cid)
            if not cust:
                continue
            if b64:
                cust.favicon_b64 = b64
                fetched += 1
            else:
                cust.favicon_b64 = None
                failed += 1

        db.session.commit()
        SyncStatus.mark_completed(
            'favicons', success=True, items_synced=fetched,
            details=f'{fetched} refreshed, {failed} unavailable',
        )
        return jsonify({
            'success': True,
            'message': f'Refreshed {fetched} favicons, {failed} unavailable.',
            'fetched': fetched,
            'failed': failed,
        })
    except Exception as e:
        db.session.rollback()
        SyncStatus.mark_completed('favicons', success=False, details=str(e))
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/admin/favicons')
def admin_favicon_gallery():
    """Gallery page showing all customer favicons for debugging/preview."""
    customers = Customer.query.filter(
        Customer.website.isnot(None),
        Customer.website != '',
    ).order_by(Customer.name).all()

    total = len(customers)
    with_favicon = sum(1 for c in customers if c.favicon_b64)
    without_favicon = [c for c in customers if not c.favicon_b64]
    with_favicon_list = [c for c in customers if c.favicon_b64]

    return render_template('admin_favicons.html',
                           customers_with=with_favicon_list,
                           customers_without=without_favicon,
                           total=total,
                           fetched_count=with_favicon)


# ==============================================================================
# Backup API Endpoints
# ==============================================================================

def _get_backup_config() -> dict:
    """Load backup configuration from data/backup_config.json.

    Returns:
        dict with backup config, or defaults if file missing/invalid.
    """
    app_root = Path(current_app.root_path).parent
    config_path = app_root / 'data' / 'backup_config.json'
    defaults = {
        'enabled': False,
        'onedrive_path': '',
        'backup_dir': '',
        'retention': {'daily': 7, 'weekly': 4, 'monthly': 3},
        'last_backup': None,
        'task_registered': False,
    }
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)
            # Merge with defaults for missing keys
            for key, val in defaults.items():
                if key not in config:
                    config[key] = val
            return config
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _save_backup_config(config: dict) -> None:
    """Save backup configuration to data/backup_config.json."""
    app_root = Path(current_app.root_path).parent
    config_path = app_root / 'data' / 'backup_config.json'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)


def _list_backup_files(backup_dir: str, limit: int = 10) -> list[dict]:
    """List recent backup files from the backup directory.

    Args:
        backup_dir: Path to the backup directory.
        limit: Maximum number of files to return.

    Returns:
        List of dicts with name, size_bytes, size_display, modified_iso.
    """
    backups = []
    if not backup_dir or not os.path.isdir(backup_dir):
        return backups

    for f in sorted(Path(backup_dir).glob('notehelper_*.db'), reverse=True):
        stat = f.stat()
        size_mb = stat.st_size / (1024 * 1024)
        backups.append({
            'name': f.name,
            'size_bytes': stat.st_size,
            'size_display': f'{size_mb:.1f} MB',
            'modified_iso': datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        })
        if len(backups) >= limit:
            break
    return backups


# Scheduled task name (must match scripts/backup.ps1 and scripts/server.ps1)
_BACKUP_TASK_NAME = 'NoteHelper-DailyBackup'


def _check_scheduled_task() -> dict:
    """Query Windows Task Scheduler for the backup task's real status.

    Returns:
        dict with ``exists`` (bool), ``next_run`` (str or None), and
        ``status`` (str or None, e.g. 'Ready', 'Running', 'Disabled').
    """
    result = {'exists': False, 'next_run': None, 'status': None}
    if sys.platform != 'win32':
        return result
    try:
        proc = subprocess.run(
            ['schtasks', '/Query', '/TN', _BACKUP_TASK_NAME, '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # CSV columns: "TaskName","Next Run Time","Status"
            line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip('"') for p in line.split(',')]
            result['exists'] = True
            if len(parts) >= 2 and parts[1] not in ('N/A', ''):
                result['next_run'] = parts[1]
            if len(parts) >= 3:
                result['status'] = parts[2]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return result


@admin_bp.route('/api/admin/backup/status', methods=['GET'])
def api_backup_status():
    """Return backup configuration and recent backup list."""
    config = _get_backup_config()
    backups = _list_backup_files(config.get('backup_dir', ''))
    task_info = _check_scheduled_task()
    return jsonify({
        'enabled': config.get('enabled', False),
        'backup_dir': config.get('backup_dir', ''),
        'onedrive_path': config.get('onedrive_path', ''),
        'last_backup': config.get('last_backup'),
        'task_registered': config.get('task_registered', False),
        'task_exists': task_info['exists'],
        'task_next_run': task_info['next_run'],
        'task_status': task_info['status'],
        'retention': config.get('retention', {}),
        'recent_backups': backups,
    })


@admin_bp.route('/api/admin/backup/run', methods=['POST'])
def api_backup_run():
    """Run a database backup now (copies DB to the configured backup dir).

    This performs the copy directly in Python rather than shelling out to
    PowerShell, so it works regardless of OS.
    """
    config = _get_backup_config()
    backup_dir = config.get('backup_dir', '')

    if not config.get('enabled') or not backup_dir:
        return jsonify({
            'success': False,
            'error': 'Backups are not configured. Run backup.bat -Setup first.',
        }), 400

    app_root = Path(current_app.root_path).parent
    db_path = app_root / 'data' / 'notehelper.db'
    if not db_path.exists():
        return jsonify({'success': False, 'error': 'Database file not found.'}), 404

    try:
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')
        dest = os.path.join(backup_dir, f'notehelper_{timestamp}.db')
        shutil.copy2(str(db_path), dest)

        # Update last_backup in config
        config['last_backup'] = datetime.now(timezone.utc).isoformat()
        _save_backup_config(config)

        size_mb = os.path.getsize(dest) / (1024 * 1024)
        return jsonify({
            'success': True,
            'message': f'Backup created: notehelper_{timestamp}.db ({size_mb:.1f} MB)',
            'file': f'notehelper_{timestamp}.db',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# Usage Telemetry API Endpoints
# ==============================================================================

@admin_bp.route('/api/admin/telemetry/stats', methods=['GET'])
def api_telemetry_stats():
    """Return aggregated usage telemetry statistics.

    Query params:
        days: Number of days to look back (default 30, max 365).

    Returns JSON with:
        summary: total events, unique endpoints, date range
        by_category: event counts grouped by feature category
        top_endpoints: most-hit endpoints
        top_api_endpoints: most-hit API endpoints specifically
        errors: recent error summary
        daily_activity: events per day for charting
        avg_response_time: average response time by category
    """
    from sqlalchemy import func, case

    days = min(int(request.args.get('days', 30)), 365)
    cutoff = datetime.now(timezone.utc) - __import__('datetime').timedelta(days=days)

    base_q = UsageEvent.query.filter(UsageEvent.timestamp >= cutoff)

    # Summary
    total_events = base_q.count()
    unique_endpoints = base_q.with_entities(
        func.count(func.distinct(UsageEvent.endpoint))
    ).scalar()
    api_events = base_q.filter(UsageEvent.is_api.is_(True)).count()
    page_events = base_q.filter(UsageEvent.is_api.is_(False)).count()
    error_events = base_q.filter(UsageEvent.status_code >= 400).count()

    # By category
    by_category = (
        base_q.with_entities(
            UsageEvent.category,
            func.count().label('count'),
            func.avg(UsageEvent.response_time_ms).label('avg_ms'),
        )
        .group_by(UsageEvent.category)
        .order_by(func.count().desc())
        .all()
    )

    # Top endpoints (all)
    top_endpoints = (
        base_q.with_entities(
            UsageEvent.method,
            UsageEvent.endpoint,
            UsageEvent.category,
            func.count().label('count'),
            func.avg(UsageEvent.response_time_ms).label('avg_ms'),
        )
        .group_by(UsageEvent.method, UsageEvent.endpoint, UsageEvent.category)
        .order_by(func.count().desc())
        .limit(25)
        .all()
    )

    # Top API endpoints
    top_api = (
        base_q.filter(UsageEvent.is_api.is_(True))
        .with_entities(
            UsageEvent.method,
            UsageEvent.endpoint,
            UsageEvent.category,
            func.count().label('count'),
            func.avg(UsageEvent.response_time_ms).label('avg_ms'),
            func.sum(case((UsageEvent.status_code >= 400, 1), else_=0)).label('errors'),
        )
        .group_by(UsageEvent.method, UsageEvent.endpoint, UsageEvent.category)
        .order_by(func.count().desc())
        .limit(25)
        .all()
    )

    # Recent errors
    recent_errors = (
        base_q.filter(UsageEvent.status_code >= 400)
        .with_entities(
            UsageEvent.method,
            UsageEvent.endpoint,
            UsageEvent.status_code,
            UsageEvent.error_type,
            UsageEvent.error_message,
            UsageEvent.referrer_path,
            UsageEvent.timestamp,
        )
        .order_by(UsageEvent.timestamp.desc())
        .limit(50)
        .all()
    )

    # Daily activity (for charting)
    daily = (
        base_q.with_entities(
            func.date(UsageEvent.timestamp).label('day'),
            func.count().label('count'),
            func.sum(case((UsageEvent.is_api.is_(True), 1), else_=0)).label('api_count'),
            func.sum(case((UsageEvent.status_code >= 400, 1), else_=0)).label('errors'),
        )
        .group_by(func.date(UsageEvent.timestamp))
        .order_by(func.date(UsageEvent.timestamp))
        .all()
    )

    # Feature flow: which pages trigger which API calls
    flows = (
        base_q.filter(
            UsageEvent.is_api.is_(True),
            UsageEvent.referrer_path.isnot(None),
        )
        .with_entities(
            UsageEvent.referrer_path,
            UsageEvent.endpoint,
            func.count().label('count'),
        )
        .group_by(UsageEvent.referrer_path, UsageEvent.endpoint)
        .order_by(func.count().desc())
        .limit(30)
        .all()
    )

    return jsonify({
        'days': days,
        'summary': {
            'total_events': total_events,
            'unique_endpoints': unique_endpoints,
            'api_events': api_events,
            'page_events': page_events,
            'error_events': error_events,
        },
        'by_category': [
            {
                'category': row.category or 'Unknown',
                'count': row.count,
                'avg_response_ms': round(row.avg_ms, 1) if row.avg_ms else None,
            }
            for row in by_category
        ],
        'top_endpoints': [
            {
                'method': row.method,
                'endpoint': row.endpoint,
                'category': row.category,
                'count': row.count,
                'avg_response_ms': round(row.avg_ms, 1) if row.avg_ms else None,
            }
            for row in top_endpoints
        ],
        'top_api_endpoints': [
            {
                'method': row.method,
                'endpoint': row.endpoint,
                'category': row.category,
                'count': row.count,
                'avg_response_ms': round(row.avg_ms, 1) if row.avg_ms else None,
                'errors': row.errors,
            }
            for row in top_api
        ],
        'recent_errors': [
            {
                'method': row.method,
                'endpoint': row.endpoint,
                'status_code': row.status_code,
                'error_type': row.error_type,
                'error_message': row.error_message,
                'referrer_path': row.referrer_path,
                'timestamp': row.timestamp.isoformat() if row.timestamp else None,
            }
            for row in recent_errors
        ],
        'daily_activity': [
            {
                'date': str(row.day),
                'count': row.count,
                'api_count': row.api_count,
                'errors': row.errors,
            }
            for row in daily
        ],
        'feature_flows': [
            {
                'from_page': row.referrer_path,
                'to_api': row.endpoint,
                'count': row.count,
            }
            for row in flows
        ],
    })


@admin_bp.route('/api/admin/telemetry/clear', methods=['POST'])
def api_telemetry_clear():
    """Delete all telemetry data."""
    try:
        deleted = UsageEvent.query.delete()
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'Deleted {deleted} telemetry events.',
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/telemetry/events', methods=['GET'])
def api_telemetry_events():
    """Return raw telemetry events with pagination.

    Query params:
        page: Page number (default 1).
        per_page: Events per page (default 50, max 200).
        category: Filter by category.
        is_api: Filter (true/false) for API vs page requests.
        errors_only: If 'true', only return 4xx/5xx events.
    """
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(200, max(1, int(request.args.get('per_page', 50))))

    q = UsageEvent.query.order_by(UsageEvent.timestamp.desc())

    category = request.args.get('category')
    if category:
        q = q.filter(UsageEvent.category == category)

    is_api = request.args.get('is_api')
    if is_api == 'true':
        q = q.filter(UsageEvent.is_api.is_(True))
    elif is_api == 'false':
        q = q.filter(UsageEvent.is_api.is_(False))

    if request.args.get('errors_only') == 'true':
        q = q.filter(UsageEvent.status_code >= 400)

    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'events': [
            {
                'id': e.id,
                'timestamp': e.timestamp.isoformat(),
                'method': e.method,
                'endpoint': e.endpoint,
                'blueprint': e.blueprint,
                'view_function': e.view_function,
                'is_api': e.is_api,
                'status_code': e.status_code,
                'response_time_ms': e.response_time_ms,
                'referrer_path': e.referrer_path,
                'error_type': e.error_type,
                'error_message': e.error_message,
                'category': e.category,
            }
            for e in pagination.items
        ],
        'page': pagination.page,
        'per_page': pagination.per_page,
        'total': pagination.total,
        'pages': pagination.pages,
    })


@admin_bp.route('/api/admin/telemetry/feature-health', methods=['GET'])
def api_telemetry_feature_health():
    """Return a feature-health report: popularity ranking, dead features, trends.

    Query params:
        days: Number of days to analyse (default 30, max 365).

    Combines aggregated DailyFeatureStats (for completed days) with today's
    live UsageEvent data so the report is always current.
    """
    from app.services.telemetry_aggregation import get_feature_health

    days = min(int(request.args.get('days', 30)), 365)
    return jsonify(get_feature_health(days=days))


@admin_bp.route('/api/admin/telemetry/aggregate', methods=['POST'])
def api_telemetry_aggregate():
    """Manually trigger aggregation of raw events into daily stats.

    JSON body (all optional):
        days_back: How many days to aggregate (default 7).
        prune_raw: If true, prune raw events beyond retention window.
        raw_retention_days: Days of raw events to keep (default 90).
    """
    from app.services.telemetry_aggregation import aggregate_daily_stats

    data = request.get_json(silent=True) or {}
    try:
        result = aggregate_daily_stats(
            days_back=min(int(data.get('days_back', 7)), 365),
            prune_raw=bool(data.get('prune_raw', False)),
            raw_retention_days=int(data.get('raw_retention_days', 90)),
        )
        return jsonify({'success': True, **result})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/telemetry/flush', methods=['POST'])
def api_telemetry_flush():
    """Manually flush the central telemetry buffer to App Insights."""
    from app.services.telemetry_shipper import flush_buffer, is_telemetry_enabled

    if not is_telemetry_enabled():
        return jsonify({
            'success': False,
            'reason': 'Telemetry shipping is disabled (NOTEHELPER_TELEMETRY_OPT_OUT)',
        })

    result = flush_buffer()
    return jsonify({'success': result.get('flushed', False), **result})


@admin_bp.route('/api/admin/telemetry/shipping-status', methods=['GET'])
def api_telemetry_shipping_status():
    """Return the current central telemetry shipping status and stats."""
    from app.services.telemetry_shipper import get_flush_stats

    return jsonify(get_flush_stats())


# ---------------------------------------------------------------------------
# Fiscal Year Cutover
# ---------------------------------------------------------------------------


@admin_bp.route('/api/admin/fy/status', methods=['GET'])
def api_fy_status():
    """Return FY transition state, FY labels, and list of archives."""
    from app.services.fy_cutover import get_fiscal_year_labels, get_transition_state, list_archives
    return jsonify({
        'transition': get_transition_state(),
        'archives': list_archives(),
        'fy_labels': get_fiscal_year_labels(),
    })


@admin_bp.route('/api/admin/fy/start', methods=['POST'])
def api_fy_start():
    """Start a new fiscal year: archive current DB, enter transition mode."""
    from app.services.fy_cutover import start_new_fiscal_year

    try:
        result = start_new_fiscal_year()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/fy/sync-complete', methods=['POST'])
def api_fy_sync_complete():
    """Mark FY account sync as finished."""
    from app.services.fy_cutover import mark_fy_sync_complete
    mark_fy_sync_complete()
    return jsonify({'success': True})


@admin_bp.route('/api/admin/fy/preview-purge', methods=['POST'])
def api_fy_preview_purge():
    """Preview what would be purged with the given TPIDs."""
    from app.services.fy_cutover import preview_purge

    data = request.get_json(silent=True) or {}
    synced_tpids = data.get('synced_tpids', [])
    if not synced_tpids:
        return jsonify({'success': False, 'error': 'synced_tpids list is required'}), 400

    try:
        preview = preview_purge([int(t) for t in synced_tpids])
        return jsonify({'success': True, **preview})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/fy/finalize', methods=['POST'])
def api_fy_finalize():
    """Finalize alignments: purge orphaned customers and exit transition mode."""
    import json as _json
    from pathlib import Path
    from app.services.fy_cutover import finalize_alignments

    tpid_file = Path(current_app.instance_path).parent / 'data' / 'last_sync_tpids.json'
    if not tpid_file.exists():
        return jsonify({
            'success': False,
            'error': 'No sync data found. Run the Final Sync first.'
        }), 400

    try:
        synced_tpids = _json.loads(tpid_file.read_text())
        if not synced_tpids:
            return jsonify({
                'success': False,
                'error': 'Last sync returned no TPIDs. Run the Final Sync again.'
            }), 400
        summary = finalize_alignments([int(t) for t in synced_tpids])
        # Clean up the TPID file after successful finalization
        tpid_file.unlink(missing_ok=True)
        return jsonify({'success': True, **summary})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/fy/exit-transition', methods=['POST'])
def api_fy_exit_transition():
    """Exit FY transition mode without purging (cancel)."""
    from app.services.fy_cutover import exit_transition_mode
    exit_transition_mode()
    return jsonify({'success': True})

