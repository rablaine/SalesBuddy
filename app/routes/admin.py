"""
Admin routes for Sales Buddy.
Handles admin panel, user management, and domain whitelisting.
"""
import base64
import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify, g, Response, stream_with_context

from app.models import (
    db, User, POD, Territory, Seller, Customer, Topic, Note, AIQueryLog,
    RevenueImport, CustomerRevenueData, ProductRevenueData, RevenueAnalysis,
    RevenueConfig, RevenueReviewNote, Milestone, Opportunity, MsxTask,
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
        'stale_customers': Customer.query.filter(Customer.stale_since.isnot(None)).count(),
    }
    
    # FY season: promote the FY card Jul 1 - Aug 31, unless this year's
    # transition is already done. Key off fy_last_completed (set + persisted by
    # finalize) - not fy_transition_started, which finalize clears to None.
    now = datetime.now(timezone.utc)
    fy_season = (7 <= now.month <= 8)
    pref = UserPreference.query.first()
    if fy_season and pref:
        from app.services.fy_cutover import get_fiscal_year_labels
        next_fy = get_fiscal_year_labels()["next_fy"]
        if pref.fy_last_completed == next_fy:
            fy_season = False

    return render_template('admin_panel.html', stats=stats, fy_season=fy_season,
                           pref=pref, is_electron=_is_electron())


@admin_bp.route('/admin/ai-logs')
def admin_ai_logs():
    """View AI query logs for debugging."""
    # Get recent logs (last 50)
    logs = AIQueryLog.query.order_by(AIQueryLog.timestamp.desc()).limit(50).all()
    
    return render_template('admin_ai_logs.html', logs=logs)


@admin_bp.route('/api/admin/clear-revenue', methods=['POST'])
def api_clear_revenue_data():
    """Delete all revenue data (imports, records, analyses, review notes, config)."""
    try:
        deleted = {}
        deleted['review_notes'] = RevenueReviewNote.query.delete()
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
    from app.gateway_client import gateway_call, GatewayError

    try:
        result = gateway_call("/v1/ping", {})
        return jsonify({
            'success': True,
            'message': 'Gateway connection successful!',
            'response': result.get('response', ''),
            'mode': 'gateway',
        })
    except GatewayError as e:
        return jsonify({'success': False, 'error': f'Gateway test failed: {e}'}), 400


@admin_bp.route('/api/admin/ai-clear-cache', methods=['POST'])
def api_admin_ai_clear_cache():
    """Clear the gateway token cache so the next call uses fresh credentials."""
    from app.gateway_client import clear_token_cache
    clear_token_cache()
    return jsonify({'success': True})


@admin_bp.route('/api/admin/update-check', methods=['GET'])
def api_update_check():
    """Check for available updates and return current state.

    Behaviour:
      - Always re-fetches CHANGELOG.md from GitHub (cheap HTTP call).
      - If ``?refresh=1`` is set, also runs ``git fetch origin main`` to
        recompute the local-vs-remote diff. Otherwise returns the cached
        diff from the hourly background poll. Page-load and Check-Now
        both pass ``refresh=1``; the navbar dot uses the cached value.

    Returns the standard update state plus a ``changelog`` block with two
    pre-filtered lists:
      - ``pending``: entries between the running commit and the remote
        (i.e. "What you'll get").
      - ``last_deployed``: entries between ``previous_commit`` and
        ``current_commit`` from UserPreference (i.e. "What just landed"
        / "View last update"). DB-backed so it survives any restart, any
        tab, any browser - replaces the old per-tab sessionStorage hack.
    """
    from flask import current_app
    from app.services.update_checker import (
        get_update_state, check_for_updates,
        get_changelog_state, fetch_changelog,
        get_local_head_date, get_commits_in_range, entries_in_commits,
        entries_require_shell_rebuild,
    )

    # Always pull a fresh changelog. Cheap HTTP call, removes the entire
    # "stale until you click Check Now" class of bugs. ``refresh=1`` adds
    # a git fetch on top so we also catch newly-pushed commits.
    if request.args.get('refresh') == '1':
        state = check_for_updates()
    else:
        state = get_update_state()
    fetch_changelog()

    # Boot commit (what the running server actually loaded) - frozen at
    # startup, immune to git pulls happening underneath us.
    boot_commit = current_app.config.get('BOOT_COMMIT')
    boot_commit_date = current_app.config.get('BOOT_COMMIT_DATE')
    state['boot_commit'] = boot_commit
    state['boot_commit_date'] = boot_commit_date
    disk_commit = state.get('local_commit')
    state['restart_needed'] = (
        boot_commit is not None
        and disk_commit is not None
        and boot_commit != disk_commit
    )

    # Dismissal: the navbar dot hides if the user clicked "dismiss" on the
    # current remote commit. Doesn't affect the card itself.
    pref = UserPreference.query.first()
    dismissed = pref.dismissed_update_commit if pref else None
    state['dismissed'] = dismissed == state.get('remote_commit')
    state['show_badge'] = state.get('available', False) and not state['dismissed']

    # Build the "What you'll get" list from the local..remote git range.
    changelog = get_changelog_state()
    local_head_date = get_local_head_date()
    pending_commits = get_commits_in_range(
        state.get('local_commit'), state.get('remote_commit')
    )

    # Build the "Last deployed" list from previous_commit..current_commit
    # in the DB. These are persisted at boot whenever BOOT_COMMIT changes,
    # so this works even if the user switched browsers or restarted via
    # update.bat instead of the admin button.
    last_deployed = []
    if pref and pref.previous_commit and pref.current_commit \
            and pref.previous_commit != pref.current_commit:
        deployed_commits = get_commits_in_range(
            pref.previous_commit, pref.current_commit
        )
        last_deployed = entries_in_commits(
            changelog['entries'], deployed_commits, None
        )

    state['changelog'] = {
        'entries': changelog['entries'],
        'last_fetched': changelog['last_fetched'],
        'error': changelog['error'],
        'local_head_date': local_head_date,
        'pending': entries_in_commits(
            changelog['entries'], pending_commits, local_head_date
        ),
        'last_deployed': last_deployed,
    }
    state['previous_commit'] = pref.previous_commit if pref else None
    state['current_commit'] = pref.current_commit if pref else None

    # Whether the pending update includes an Electron shell change that needs a
    # rebuild (not just a git pull) to take effect. The front-end uses this to
    # warn the user and to request the auto-chained rebuild on Update. Only
    # meaningful under the desktop shell.
    state['pending_rebuild'] = (
        _is_electron()
        and entries_require_shell_rebuild(changelog['entries'], pending_commits)
    )

    return jsonify(state)


def _is_supervised() -> bool:
    """True when a supervisor (or Electron main) is managing this process."""
    return os.environ.get('SALESBUDDY_SUPERVISED', '').strip().lower() in ('1', 'true', 'yes')


def _is_electron() -> bool:
    """True when the stack was launched by the Electron desktop shell."""
    return os.environ.get('SALESBUDDY_ELECTRON', '').strip().lower() in ('1', 'true', 'yes')


def _spawn_server_script(*args: str) -> None:
    """Spawn scripts/server.ps1 detached with the given args (e.g. -StopOnly).

    Detached (CREATE_NO_WINDOW) so it survives this process - and, under a
    supervisor, the whole process tree - being torn down.
    """
    CREATE_NO_WINDOW = 0x08000000
    repo_root = Path(__file__).resolve().parent.parent.parent
    server_script = repo_root / 'scripts' / 'server.ps1'
    cmd = ['powershell.exe', '-ExecutionPolicy', 'Bypass',
           '-File', str(server_script), *args]
    subprocess.Popen(cmd, cwd=str(repo_root), creationflags=CREATE_NO_WINDOW)


@admin_bp.route('/api/admin/shutdown', methods=['POST'])
def api_shutdown_server():
    """Shut down the running server process.

    Sends the response first, then terminates the process after a short
    delay so the client receives a clean JSON reply.
    """
    port = os.environ.get('PORT', '5151')

    def _shutdown():
        try:
            from app.services.lifecycle import record_clean_shutdown
            record_clean_shutdown(reason='admin_shutdown')
        except Exception:
            pass
        if _is_supervised():
            # Killing just this web process would only make the supervisor
            # respawn it. Tell server.ps1 to stop the whole tree instead.
            try:
                _spawn_server_script('-StopOnly')
            except Exception:
                os.kill(os.getpid(), signal.SIGTERM)
        else:
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

    # Record what we're updating FROM. previous_commit is only ever written
    # here (and one mirror in update.bat / scripts), never on plain restart -
    # that way the "View last update" panel keeps showing the most recent
    # real deployment indefinitely, even if the server restarts later for
    # unrelated reasons. After server.ps1 finishes the pull and the new
    # process boots, app/__init__.py records the new current_commit, and
    # the pair previous..current describes exactly what shipped.
    try:
        boot_commit = current_app.config.get('BOOT_COMMIT')
        if boot_commit:
            pref = UserPreference.query.first()
            if pref is None:
                pref = UserPreference(current_commit=boot_commit, previous_commit=boot_commit)
                db.session.add(pref)
            else:
                pref.previous_commit = boot_commit
            db.session.commit()
    except Exception:
        # Don't block the deploy on bookkeeping. Worst case: the next
        # "View last update" is empty until the user updates again.
        db.session.rollback()

    # Under the Electron shell, Electron owns the update: it owns the supervisor
    # process, so letting server.ps1 tear the tree down here would race with
    # Electron's own restart handler. Instead, drop a sentinel file the shell
    # watches for; it runs git pull + pip install and relaunches itself. This
    # works whether the click came from the Electron window or a browser tab.
    if _is_electron():
        try:
            sentinel = repo_root / 'data' / 'electron-update.request'
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            # If the incoming update carries an Electron shell change, ask the
            # shell to chain a rebuild after the pull (content = 'rebuild').
            # Otherwise a plain timestamp = pull + relaunch, today's behavior.
            data = request.get_json(silent=True) or {}
            want_rebuild = bool(data.get('rebuild', False))
            sentinel.write_text(
                'rebuild' if want_rebuild
                else datetime.now(timezone.utc).isoformat(),
                encoding='utf-8'
            )
        except Exception as e:
            return jsonify({'error': f'Failed to request update: {e}'}), 500
        return jsonify({
            'success': True,
            'message': 'Update started. Sales Buddy will restart momentarily.',
        })

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
        try:
            from app.services.lifecycle import record_clean_shutdown
            record_clean_shutdown(reason='admin_update')
        except Exception:
            pass
        # Under a supervisor, the update's Stop-Server tree-kills the supervisor
        # (and this web process); self-terminating would just cause a respawn the
        # updater then has to kill. Only free the port ourselves when monolithic.
        if not _is_supervised():
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.5, _self_terminate).start()

    return jsonify({
        'success': True,
        'message': 'Update started. The server will restart momentarily.',
    })


# The shell polls for the rebuild sentinel every 1.5s. If it is still on disk
# after this many seconds, the running shell has no rebuild watcher (pre-7/2026
# build) and never will consume it.
REBUILD_SENTINEL_STALE_SECONDS = 15


@admin_bp.route('/api/admin/rebuild-shell', methods=['POST'])
def api_rebuild_shell():
    """Manually rebuild the Electron desktop shell (failsafe).

    Drops the electron-rebuild.request sentinel the shell watches for; the shell
    builds the new shell from the current repo source, stages it, and relaunches.
    This is the always-available Danger Zone fallback for the transition window
    where auto-detection can't fire (the bootstrap change, an update.bat pull, or
    an interrupted auto-rebuild). No git pull - it rebuilds whatever is on disk.
    """
    if not _is_electron():
        return jsonify({'error': 'The desktop app is not running'}), 400

    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        sentinel = repo_root / 'data' / 'electron-rebuild.request'
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            datetime.now(timezone.utc).isoformat(), encoding='utf-8'
        )
    except Exception as e:
        return jsonify({'error': f'Failed to request rebuild: {e}'}), 500

    return jsonify({
        'success': True,
        'message': ('Rebuilding the desktop app. This takes a minute or two, '
                    'then Sales Buddy will restart itself.'),
    })


@admin_bp.route('/api/admin/rebuild-status', methods=['GET'])
def api_rebuild_status():
    """Report whether the running shell picked up a pending rebuild request.

    The shell polls for the sentinel every 1.5s and deletes it the moment it
    starts. A sentinel that is still sitting there after a few seconds means the
    running shell predates the rebuild watcher and will never consume it, so the
    rebuild would hang forever. In that case we clean the sentinel up (otherwise
    a future shell would rebuild unexpectedly on its first boot) and tell the
    caller to show the reinstall fallback.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    sentinel = repo_root / 'data' / 'electron-rebuild.request'

    if not sentinel.exists():
        return jsonify({'pending': False, 'stale': False})

    try:
        age = datetime.now(timezone.utc).timestamp() - sentinel.stat().st_mtime
    except OSError:
        return jsonify({'pending': False, 'stale': False})

    if age < REBUILD_SENTINEL_STALE_SECONDS:
        return jsonify({'pending': True, 'stale': False})

    try:
        sentinel.unlink()
    except OSError:
        pass
    return jsonify({'pending': False, 'stale': True})


@admin_bp.route('/api/admin/vacuum-database', methods=['POST'])
def api_vacuum_database():
    """Compact the live SQLite database and report reclaimed disk space."""
    from app.db_paths import resolve_db_path
    from app.services.database_maintenance import (
        VacuumInProgressError,
        vacuum_database,
    )

    try:
        db.session.remove()
        db.engine.dispose()
        result = vacuum_database(resolve_db_path())
        return jsonify({'success': True, **result})
    except VacuumInProgressError as error:
        return jsonify({'success': False, 'error': str(error)}), 409
    except Exception as error:
        current_app.logger.exception('Manual database compaction failed')
        message = str(error)
        if 'locked' in message.lower() or 'busy' in message.lower():
            message = (
                'Database is busy. Wait for current syncs or saves to finish, '
                'then try again.'
            )
        return jsonify({'success': False, 'error': message}), 500


@admin_bp.route('/api/admin/migrate-to-electron', methods=['POST'])
def api_migrate_to_electron():
    """Launch the Flask -> Electron desktop migration in a visible console.

    Mirrors the Update button, but opens a real console window so the user can
    watch the (longer, one-time) desktop-app build and read any error directly,
    instead of the web page having to poll through its own Flask stack being
    torn down. The migration script builds the shell BEFORE stopping the stack,
    so a build failure leaves the current app running.
    """
    if sys.platform != 'win32':
        return jsonify({'error': 'The desktop app is only supported on Windows'}), 400
    if _is_electron():
        return jsonify({'error': 'Already running as the desktop app'}), 400

    repo_root = Path(__file__).resolve().parent.parent.parent

    # In development, launch a harmless console test script instead of the real
    # migration. This lets you verify the button -> endpoint -> visible console
    # wiring without building anything or hijacking your dev machine's autostart.
    # Err safe: treat as dev if the app is in debug OR FLASK_ENV=development, so
    # a misconfigured env never fires a real migration on a dev box.
    is_dev = bool(current_app.debug) or \
        os.environ.get('FLASK_ENV', '').strip().lower() == 'development'
    script_name = '_migrate_console_test.ps1' if is_dev else 'migrate-to-electron.ps1'
    script = repo_root / 'scripts' / script_name
    if not script.exists():
        return jsonify({'error': f'{script_name} not found'}), 500

    # CREATE_NEW_CONSOLE gives the child its own visible window on the user's
    # desktop (the autostart task runs in the interactive session). Wrap the
    # script in a Read-Host so the window stays up after it finishes or fails.
    CREATE_NEW_CONSOLE = 0x00000010
    inner = (
        f"& '{script}'; Write-Host ''; "
        "Read-Host 'Setup finished - press Enter to close this window'"
    )
    cmd = ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-NoProfile',
           '-Command', inner]
    try:
        subprocess.Popen(cmd, cwd=str(repo_root), creationflags=CREATE_NEW_CONSOLE)
    except Exception as e:
        return jsonify({'error': f'Failed to start setup: {e}'}), 500

    if is_dev:
        message = ('Dev test: a console window opened running the wiring test '
                   'script (no real migration performed).')
    else:
        message = ('A setup window has opened - follow it there. Sales Buddy '
                   'will reopen as a desktop app when it finishes.')
    return jsonify({'success': True, 'message': message})


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

# Hardcoded backup subdirectory under OneDrive for Business
_BACKUP_SUBDIR = os.path.join('Backups', 'SalesBuddy')


def _get_onedrive_path() -> str:
    """Return the OneDrive for Business path from UserPreference, or empty string."""
    prefs = UserPreference.query.first()
    return (prefs.onedrive_path or '') if prefs else ''


def _get_backup_dir() -> str:
    """Derive the backup directory from the stored OneDrive path.

    Returns ``{onedrive_path}/Backups/SalesBuddy`` or empty string.
    """
    onedrive = _get_onedrive_path()
    if not onedrive:
        return ''
    return os.path.join(onedrive, _BACKUP_SUBDIR)


def _get_retention() -> dict:
    """Return backup retention policy from UserPreference."""
    prefs = UserPreference.query.first()
    if prefs:
        return {
            'daily': prefs.backup_retention_daily,
            'weekly': prefs.backup_retention_weekly,
            'monthly': prefs.backup_retention_monthly,
        }
    return {'daily': 7, 'weekly': 4, 'monthly': 3}


def _get_last_backup(backup_dir: str) -> str | None:
    """Deduce the last backup timestamp from the newest file in backup_dir."""
    if not backup_dir or not os.path.isdir(backup_dir):
        return None
    files = sorted(Path(backup_dir).glob('salesbuddy_*.db'), reverse=True)
    if not files:
        return None
    return datetime.fromtimestamp(
        files[0].stat().st_mtime, tz=timezone.utc
    ).isoformat()


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

    for f in sorted(Path(backup_dir).glob('salesbuddy_*.db'), reverse=True):
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


# Scheduled task names (must match scripts/backup.ps1 and scripts/server.ps1)
_BACKUP_TASK_NAME = 'SalesBuddy-DailyBackup'
_AUTOSTART_TASK_NAME = 'SalesBuddy-AutoStart'


def _check_scheduled_task(task_name: str) -> dict:
    """Query Windows Task Scheduler for a task's real status.

    Args:
        task_name: The scheduled task name to query.

    Returns:
        dict with ``exists`` (bool), ``next_run`` (str or None), and
        ``status`` (str or None, e.g. 'Ready', 'Running', 'Disabled').
    """
    result = {'exists': False, 'next_run': None, 'status': None}
    if sys.platform != 'win32':
        return result
    try:
        proc = subprocess.run(
            ['schtasks', '/Query', '/TN', task_name, '/FO', 'CSV', '/NH'],
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
        elif proc.returncode != 0 and 'Access is denied' in (proc.stderr or ''):
            # Task exists but is owned by SYSTEM - not queryable without elevation
            result['exists'] = True
            result['status'] = 'Registered (requires admin to query details)'
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return result


def _set_scheduled_task_enabled(task_name: str, enabled: bool) -> dict:
    """Enable or disable a Windows scheduled task.

    Args:
        task_name: The scheduled task name.
        enabled: True to enable, False to disable.

    Returns:
        dict with ``success`` (bool) and optional ``error`` (str).
    """
    if sys.platform != 'win32':
        return {'success': False, 'error': 'Scheduled tasks are only supported on Windows.'}
    verb = 'Enable' if enabled else 'Disable'
    try:
        proc = subprocess.run(
            ['schtasks', '/Change', '/TN', task_name,
             '/ENABLE' if enabled else '/DISABLE'],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return {'success': True}
        return {
            'success': False,
            'error': proc.stderr.strip() or f'Failed to {verb.lower()} task.',
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': f'{verb} task timed out.'}
    except (FileNotFoundError, OSError) as e:
        return {'success': False, 'error': str(e)}


def _register_scheduled_task(task_key: str) -> dict:
    """Register a missing Sales Buddy scheduled task via schtasks.exe.

    Used to recover from installs where the MSI didn't register one of the
    tasks (or the user removed it manually). Mirrors what the MSI custom
    actions do, so the task ends up identical regardless of how it got
    created.

    Args:
        task_key: 'backup' or 'autostart'.

    Returns:
        dict with ``success`` and optional ``error``.
    """
    if sys.platform != 'win32':
        return {'success': False, 'error': 'Scheduled tasks are only supported on Windows.'}

    # The repo root is the parent of app/ - same layout as the MSI install dir.
    install_dir = Path(current_app.root_path).parent
    vbs_launcher = install_dir / 'scripts' / 'run-hidden.vbs'

    if task_key == 'backup':
        target_script = install_dir / 'scripts' / 'backup.ps1'
        task_name = _BACKUP_TASK_NAME
        # Daily at 11:00 - matches MSI + scripts/backup.ps1 -Setup.
        schedule_args = ['/sc', 'DAILY', '/st', '11:00']
        script_args = ' -Silent'
    elif task_key == 'autostart':
        target_script = install_dir / 'scripts' / 'server.ps1'
        task_name = _AUTOSTART_TASK_NAME
        schedule_args = ['/sc', 'ONLOGON']
        script_args = ''
    else:
        return {'success': False, 'error': 'Unknown task key.'}

    if not target_script.is_file() or not vbs_launcher.is_file():
        return {
            'success': False,
            'error': f'Required script not found: {target_script.name} or run-hidden.vbs.',
        }

    # /tr value is one big quoted string; schtasks parses it as the command
    # to execute. Same shape the MSI custom actions emit.
    tr_value = (
        f'wscript.exe "{vbs_launcher}" "{target_script}"{script_args}'
    )

    try:
        proc = subprocess.run(
            ['schtasks', '/Create', '/TN', task_name, '/TR', tr_value,
             *schedule_args, '/RL', 'LIMITED', '/F'],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return {'success': True}
        return {
            'success': False,
            'error': (proc.stderr or proc.stdout or 'schtasks failed').strip(),
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'schtasks /Create timed out.'}
    except (FileNotFoundError, OSError) as e:
        return {'success': False, 'error': str(e)}


# Electron desktop autostart lives in the per-user Run key (written by
# scripts/migrate-to-electron.ps1), NOT a scheduled task. The web app runs
# non-elevated, so HKCU is the only hive it can touch - which is exactly
# where the Electron launcher registers itself.
_ELECTRON_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_ELECTRON_RUN_VALUE = 'SalesBuddy'


def _electron_exe_path() -> Path:
    """Path to the staged Electron executable used for login autostart."""
    install_dir = Path(current_app.root_path).parent
    return install_dir / 'electron-dist' / 'Sales Buddy.exe'


def _check_electron_autostart() -> dict:
    """Query the HKCU Run entry that launches the Electron app at login.

    Returns the same shape as ``_check_scheduled_task`` so the autostart
    endpoints can treat both mechanisms uniformly. HKCU Run has no
    enabled/disabled state - presence means "runs at login".
    """
    result = {'exists': False, 'next_run': None, 'status': None}
    if sys.platform != 'win32':
        return result
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ELECTRON_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _ELECTRON_RUN_VALUE)
            if value:
                result['exists'] = True
                result['status'] = 'Active'
    except FileNotFoundError:
        pass  # Key or value absent - autostart not registered.
    except OSError:
        pass
    return result


def _set_electron_autostart(enabled: bool) -> dict:
    """Add or remove the HKCU Run entry for the Electron app.

    Enabling writes the quoted exe path (matching migrate-to-electron.ps1);
    disabling deletes the value, since HKCU Run has no disabled state.
    """
    if sys.platform != 'win32':
        return {'success': False, 'error': 'Autostart is only supported on Windows.'}
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ELECTRON_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                exe = _electron_exe_path()
                if not exe.is_file():
                    return {
                        'success': False,
                        'error': f'Desktop app not found at {exe}.',
                    }
                winreg.SetValueEx(
                    key, _ELECTRON_RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}"'
                )
            else:
                try:
                    winreg.DeleteValue(key, _ELECTRON_RUN_VALUE)
                except FileNotFoundError:
                    pass  # Already absent - nothing to disable.
        return {'success': True}
    except OSError as e:
        return {'success': False, 'error': str(e)}


@admin_bp.route('/api/admin/backup/status', methods=['GET'])
def api_backup_status():
    """Return backup configuration and recent backup list."""
    backup_dir = _get_backup_dir()
    backups = _list_backup_files(backup_dir)
    task_info = _check_scheduled_task(_BACKUP_TASK_NAME)
    return jsonify({
        'configured': bool(backup_dir),
        'backup_dir': backup_dir,
        'onedrive_path': _get_onedrive_path(),
        'last_backup': _get_last_backup(backup_dir),
        'task_exists': task_info['exists'],
        'task_next_run': task_info['next_run'],
        'task_status': task_info['status'],
        'retention': _get_retention(),
        'recent_backups': backups,
    })


@admin_bp.route('/api/admin/backup/run', methods=['POST'])
def api_backup_run():
    """Run a database backup now (copies DB to the configured backup dir).

    This performs the copy directly in Python rather than shelling out to
    PowerShell, so it works regardless of OS.
    """
    backup_dir = _get_backup_dir()

    if not backup_dir:
        return jsonify({
            'success': False,
            'error': 'Backups are not configured. Run start.bat to auto-detect OneDrive.',
        }), 400

    app_root = Path(current_app.root_path).parent
    from app.db_paths import resolve_db_path, backup_database
    db_path = resolve_db_path()
    if not db_path.exists():
        return jsonify({'success': False, 'error': 'Database file not found.'}), 404

    try:
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')
        dest = os.path.join(backup_dir, f'salesbuddy_{timestamp}.db')
        # WAL-safe snapshot via the SQLite online-backup API (folds the WAL so a
        # live write can't produce a torn or stale copy). A plain file copy is
        # unsafe while the DB is in WAL mode.
        if not backup_database(dest, src=db_path):
            return jsonify({
                'success': False,
                'error': 'Backup failed (see logs for details).',
            }), 500

        size_mb = os.path.getsize(dest) / (1024 * 1024)
        return jsonify({
            'success': True,
            'message': f'Backup created: salesbuddy_{timestamp}.db ({size_mb:.1f} MB)',
            'file': f'salesbuddy_{timestamp}.db',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/api/admin/tasks/<task_key>/toggle', methods=['POST'])
def api_task_toggle(task_key: str):
    """Enable or disable a scheduled task.

    Args:
        task_key: 'backup' or 'autostart'.

    Request JSON: {"enabled": true/false}
    """
    task_map = {
        'backup': _BACKUP_TASK_NAME,
        'autostart': _AUTOSTART_TASK_NAME,
    }
    task_name = task_map.get(task_key)
    if not task_name:
        return jsonify({'success': False, 'error': 'Unknown task key.'}), 400

    body = request.get_json(silent=True) or {}
    enabled = body.get('enabled')
    if enabled is None:
        return jsonify({'success': False, 'error': 'Missing enabled field.'}), 400

    # Under the Electron shell, autostart is an HKCU Run entry, not a task.
    if task_key == 'autostart' and _is_electron():
        result = _set_electron_autostart(bool(enabled))
        if result['success']:
            info = _check_electron_autostart()
            return jsonify({
                'success': True,
                'task_status': info['status'],
                'task_exists': info['exists'],
            })
        return jsonify(result), 500

    result = _set_scheduled_task_enabled(task_name, bool(enabled))
    if result['success']:
        task_info = _check_scheduled_task(task_name)
        return jsonify({
            'success': True,
            'task_status': task_info['status'],
            'task_exists': task_info['exists'],
        })
    return jsonify(result), 500


@admin_bp.route('/api/admin/tasks/<task_key>/register', methods=['POST'])
def api_task_register(task_key: str):
    """Register a missing scheduled task.

    Used when the MSI didn't create the task (older installer, partial
    install, user removed it manually, etc.). Mirrors the MSI's schtasks
    invocation so the result is identical regardless of code path.

    Args:
        task_key: 'backup' or 'autostart'.
    """
    if task_key not in ('backup', 'autostart'):
        return jsonify({'success': False, 'error': 'Unknown task key.'}), 400

    # Under the Electron shell, autostart is an HKCU Run entry, not a task.
    if task_key == 'autostart' and _is_electron():
        existing = _check_electron_autostart()
        if existing['exists']:
            return jsonify({
                'success': True,
                'task_exists': True,
                'task_status': existing['status'],
                'already_existed': True,
            })
        result = _set_electron_autostart(True)
        if not result['success']:
            return jsonify(result), 500
        info = _check_electron_autostart()
        return jsonify({
            'success': True,
            'task_exists': info['exists'],
            'task_status': info['status'],
            'task_next_run': info['next_run'],
        })

    # If the task already exists, treat as success (idempotent UX).
    task_name = _BACKUP_TASK_NAME if task_key == 'backup' else _AUTOSTART_TASK_NAME
    existing = _check_scheduled_task(task_name)
    if existing['exists']:
        return jsonify({
            'success': True,
            'task_exists': True,
            'task_status': existing['status'],
            'already_existed': True,
        })

    result = _register_scheduled_task(task_key)
    if not result['success']:
        return jsonify(result), 500

    info = _check_scheduled_task(task_name)
    return jsonify({
        'success': True,
        'task_exists': info['exists'],
        'task_status': info['status'],
        'task_next_run': info['next_run'],
    })


@admin_bp.route('/api/admin/tasks/autostart/status', methods=['GET'])
def api_autostart_status():
    """Return login-autostart status (Electron Run entry or Flask scheduled task)."""
    if _is_electron():
        task_info = _check_electron_autostart()
    else:
        task_info = _check_scheduled_task(_AUTOSTART_TASK_NAME)
    return jsonify({
        'task_exists': task_info['exists'],
        'task_next_run': task_info['next_run'],
        'task_status': task_info['status'],
    })


@admin_bp.route('/api/admin/tasks/milestone-sync/status', methods=['GET'])
def api_milestone_sync_status():
    """Return the in-process milestone sync scheduler status."""
    pref = UserPreference.query.first()
    if not pref:
        return jsonify({'enabled': False, 'sync_time': None, 'last_sync': None})

    sync_time = None
    if pref.milestone_sync_hour is not None and pref.milestone_sync_minute is not None:
        sync_time = f"{pref.milestone_sync_hour:02d}:{pref.milestone_sync_minute:02d}"

    last_sync = None
    if pref.last_milestone_sync:
        last_sync = pref.last_milestone_sync.isoformat()

    return jsonify({
        'enabled': pref.milestone_auto_sync,
        'sync_time': sync_time,
        'last_sync': last_sync,
    })


@admin_bp.route('/api/admin/sync-status/<sync_type>', methods=['GET'])
def api_sync_status(sync_type):
    """Return the last sync status for a given sync type (accounts, marketing, etc.)."""
    allowed = {'accounts', 'marketing', 'milestones', 'favicons'}
    if sync_type not in allowed:
        return jsonify({'error': 'Unknown sync type'}), 400
    status = SyncStatus.get_status(sync_type)
    result = {
        'state': status['state'],
        'completed_at': status['completed_at'].isoformat() if status['completed_at'] else None,
        'items_synced': status['items_synced'],
    }
    # Marketing sync runs automatically after milestone sync (same MWF schedule)
    if sync_type == 'marketing':
        pref = UserPreference.query.first()
        if pref:
            sync_time = None
            if pref.milestone_sync_hour is not None and pref.milestone_sync_minute is not None:
                # Marketing runs ~10 min after milestone sync
                total_min = pref.milestone_sync_hour * 60 + pref.milestone_sync_minute + 10
                h, m = divmod(total_min, 60)
                sync_time = f"{h:02d}:{m:02d}"
            result['auto_sync_info'] = {
                'enabled': bool(pref.milestone_auto_sync),
                'sync_time': sync_time,
            }
    return jsonify(result)


@admin_bp.route('/api/admin/tasks/milestone-sync/toggle', methods=['POST'])
def api_milestone_sync_toggle():
    """Toggle milestone auto-sync on/off."""
    pref = UserPreference.query.first()
    if not pref:
        return jsonify({'success': False, 'error': 'No preferences found'}), 404
    pref.milestone_auto_sync = not pref.milestone_auto_sync
    if pref.milestone_auto_sync:
        # Assign a fresh random sync time within the valid range (9:30 AM - 4:30 PM)
        import random
        SYNC_START_HOUR = 9
        SYNC_START_MINUTE = 30
        SYNC_SLOT_COUNT = 84
        slot = random.randint(0, SYNC_SLOT_COUNT - 1)
        total_minutes = (SYNC_START_HOUR * 60 + SYNC_START_MINUTE) + slot * 5
        pref.milestone_sync_hour = total_minutes // 60
        pref.milestone_sync_minute = total_minutes % 60
    db.session.commit()
    return jsonify({'success': True, 'enabled': pref.milestone_auto_sync})


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
        endpoint: Filter by endpoint (substring match).
        is_api: Filter (true/false) for API vs page requests.
        errors_only: If 'true', only return 4xx/5xx events.
        from_dt: ISO datetime lower bound (inclusive).
        to_dt: ISO datetime upper bound (inclusive).
    """
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(200, max(1, int(request.args.get('per_page', 50))))

    q = UsageEvent.query.order_by(UsageEvent.timestamp.desc())

    category = request.args.get('category')
    if category:
        q = q.filter(UsageEvent.category == category)

    endpoint_filter = request.args.get('endpoint', '').strip()
    if endpoint_filter:
        q = q.filter(UsageEvent.endpoint.contains(endpoint_filter))

    is_api = request.args.get('is_api')
    if is_api == 'true':
        q = q.filter(UsageEvent.is_api.is_(True))
    elif is_api == 'false':
        q = q.filter(UsageEvent.is_api.is_(False))

    if request.args.get('errors_only') == 'true':
        q = q.filter(UsageEvent.status_code >= 400)

    from_dt = request.args.get('from_dt')
    if from_dt:
        try:
            q = q.filter(UsageEvent.timestamp >= datetime.fromisoformat(from_dt))
        except ValueError:
            pass

    to_dt = request.args.get('to_dt')
    if to_dt:
        try:
            q = q.filter(UsageEvent.timestamp <= datetime.fromisoformat(to_dt))
        except ValueError:
            pass

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
            'reason': 'Telemetry shipping is disabled (SALESBUDDY_TELEMETRY_OPT_OUT)',
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


@admin_bp.route('/api/admin/fy/finalize-stream', methods=['POST'])
def api_fy_finalize_stream():
    """Finalize alignments with SSE progress for the (long, destructive) purge."""
    from app.services.fy_cutover import finalize_alignments_stream

    tpid_file = Path(current_app.instance_path).parent / 'data' / 'last_sync_tpids.json'
    if not tpid_file.exists():
        return jsonify({
            'success': False,
            'error': 'No sync data found. Run the Final Sync first.'
        }), 400
    try:
        synced_tpids = json.loads(tpid_file.read_text())
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not read sync data: {e}'}), 400
    if not synced_tpids:
        return jsonify({
            'success': False,
            'error': 'Last sync returned no TPIDs. Run the Final Sync again.'
        }), 400

    tpids = [int(t) for t in synced_tpids]

    def _sse(d):
        return "data: " + json.dumps(d) + "\n\n"

    def generate():
        try:
            for evt in finalize_alignments_stream(tpids):
                if 'summary' in evt:
                    tpid_file.unlink(missing_ok=True)
                    yield _sse({'complete': True, 'success': True, 'summary': evt['summary']})
                else:
                    yield _sse(evt)
        except Exception as e:
            current_app.logger.exception("FY finalize-stream failed")
            db.session.rollback()
            yield _sse({'error': str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@admin_bp.route('/api/admin/fy/exit-transition', methods=['POST'])
def api_fy_exit_transition():
    """Exit FY transition mode without purging (cancel)."""
    from app.services.fy_cutover import exit_transition_mode
    exit_transition_mode()
    return jsonify({'success': True})


@admin_bp.route('/api/admin/fy/archive/<label>/tree')
def api_fy_archive_tree(label):
    """Return the full tree skeleton for an archive."""
    from app.services.fy_cutover import get_archive_tree, list_archives

    # Validate label exists in known archives
    known = {a['fy_label'] for a in list_archives()}
    if label not in known:
        return jsonify({'error': 'Archive not found'}), 404

    try:
        tree = get_archive_tree(label)
        return jsonify(tree)
    except FileNotFoundError:
        return jsonify({'error': 'Archive file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/admin/fy/archive/<label>/customer/<int:cid>')
def api_fy_archive_customer(label, cid):
    """Return full customer detail from an archive."""
    from app.services.fy_cutover import get_archive_customer, list_archives

    known = {a['fy_label'] for a in list_archives()}
    if label not in known:
        return jsonify({'error': 'Archive not found'}), 404

    try:
        customer = get_archive_customer(label, cid)
        if not customer:
            return jsonify({'error': 'Customer not found in archive'}), 404
        return jsonify(customer)
    except FileNotFoundError:
        return jsonify({'error': 'Archive file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/admin/fy/archive/<label>/detail/<item_type>/<int:item_id>')
def api_fy_archive_detail(label, item_type, item_id):
    """Return a single note, engagement, or milestone from an archive."""
    from app.services.fy_cutover import get_archive_detail, list_archives

    if item_type not in ('note', 'engagement', 'milestone'):
        return jsonify({'error': 'Invalid type'}), 400

    known = {a['fy_label'] for a in list_archives()}
    if label not in known:
        return jsonify({'error': 'Archive not found'}), 404

    try:
        detail = get_archive_detail(label, item_type, item_id)
        if not detail:
            return jsonify({'error': f'{item_type.title()} not found in archive'}), 404
        return jsonify(detail)
    except FileNotFoundError:
        return jsonify({'error': 'Archive file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Diagnostic log endpoints
# ---------------------------------------------------------------------------


@admin_bp.route('/api/admin/diagnostic-log/stats')
def api_diagnostic_log_stats():
    """Return stats about the diagnostic log file."""
    from app.services.diagnostic_log import get_log_stats
    return jsonify(get_log_stats())


@admin_bp.route('/api/admin/diagnostic-log/download')
def api_diagnostic_log_download():
    """Download the diagnostic log as a zip file."""
    from app.services.diagnostic_log import get_log_path
    import zipfile
    import io

    log_path = get_log_path()
    if not log_path:
        return jsonify({'error': 'No diagnostic log file found'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(log_path, 'diagnostic.jsonl')
    buf.seek(0)

    from flask import send_file
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name='salesbuddy-diagnostic-log.zip',
    )


@admin_bp.route('/api/admin/diagnostic-log/clear', methods=['POST'])
def api_diagnostic_log_clear():
    """Delete the diagnostic log file."""
    from app.services.diagnostic_log import get_log_path
    import os

    log_path = get_log_path()
    if log_path:
        os.remove(log_path)
    return jsonify({'success': True})


# -------------------------------------------------------------------------
# Customer M&A: Stale Customers + Merge Tool
# -------------------------------------------------------------------------

@admin_bp.route('/api/admin/stale-customers')
def api_stale_customers():
    """Get customers flagged as stale (TPID disappeared from MSX sync)."""
    from app.services.customer_merge import _count_linked_records

    stale = Customer.query.filter(
        Customer.stale_since.isnot(None)
    ).order_by(Customer.stale_since).all()

    result = []
    for c in stale:
        counts = _count_linked_records(c.id)
        # Marketing data is auto-imported and cascade-deleted, so exclude from has_data
        auto_imported = {'marketing_summary', 'marketing_contacts', 'marketing_interactions'}
        user_data = sum(v for k, v in counts.items() if k not in auto_imported)

        # SQLite strips tzinfo on read; stale_since is UTC by convention.
        # Emit a 'Z'-suffixed ISO string so JS parses it as UTC, not local time
        # (otherwise users west of UTC see negative "days ago" values).
        if c.stale_since:
            stale_iso = c.stale_since.isoformat()
            if not stale_iso.endswith('Z') and '+' not in stale_iso:
                stale_iso += 'Z'
        else:
            stale_iso = None

        result.append({
            "id": c.id,
            "name": c.get_display_name(),
            "tpid": c.tpid,
            "stale_since": stale_iso,
            "notes_count": counts.get('notes', 0),
            "engagements_count": counts.get('engagements', 0),
            "has_data": user_data > 0,
            "seller_id": c.seller.id if c.seller else None,
            "seller_name": c.seller.name if c.seller else None,
            "territory_id": c.territory.id if c.territory else None,
            "territory_name": c.territory.name if c.territory else None,
        })

    return jsonify(result)


@admin_bp.route('/api/admin/stale-customers/<int:customer_id>/dismiss', methods=['POST'])
def api_dismiss_stale(customer_id: int):
    """Clear the stale flag on a customer (user confirmed it's fine)."""
    customer = Customer.query.get_or_404(customer_id)
    customer.stale_since = None
    db.session.commit()
    return jsonify({"success": True, "name": customer.get_display_name()})


@admin_bp.route('/api/admin/stale-customers/<int:customer_id>', methods=['DELETE'])
def api_delete_stale_customer(customer_id: int):
    """Delete a stale customer, cascading auto-imported marketing data."""
    from app.services.customer_merge import _count_linked_records
    from app.models import MarketingSummary, MarketingContact, MarketingInteraction
    customer = Customer.query.get_or_404(customer_id)
    counts = _count_linked_records(customer_id)
    # Marketing data is auto-imported and safe to cascade-delete
    auto_imported = {'marketing_summary', 'marketing_contacts', 'marketing_interactions'}
    user_data = sum(v for k, v in counts.items() if k not in auto_imported)
    if user_data > 0:
        return jsonify({"error": "Customer has linked data and cannot be deleted directly. Use merge instead."}), 400
    # Clean up auto-imported marketing data first
    MarketingInteraction.query.filter_by(customer_id=customer_id).delete()
    MarketingContact.query.filter_by(customer_id=customer_id).delete()
    MarketingSummary.query.filter_by(customer_id=customer_id).delete()
    db.session.delete(customer)
    db.session.commit()
    return jsonify({"success": True, "name": customer.get_display_name()})


@admin_bp.route('/api/admin/merge-preview')
def api_merge_preview():
    """Preview a customer merge before executing it."""
    source_id = request.args.get('source_id', type=int)
    dest_id = request.args.get('dest_id', type=int)
    if not source_id or not dest_id:
        return jsonify({"error": "source_id and dest_id are required"}), 400

    from app.services.customer_merge import get_merge_preview
    preview = get_merge_preview(source_id, dest_id)
    if "error" in preview:
        return jsonify(preview), 400
    return jsonify(preview)


@admin_bp.route('/api/admin/merge-customer', methods=['POST'])
def api_merge_customer():
    """Execute a customer merge (source absorbed into destination)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    source_id = data.get('source_id')
    dest_id = data.get('dest_id')
    if not source_id or not dest_id:
        return jsonify({"error": "source_id and dest_id are required"}), 400

    from app.services.customer_merge import merge_customer
    try:
        result = merge_customer(source_id, dest_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Merge failed: {str(e)}"}), 500


@admin_bp.route('/api/admin/customers-search')
def api_customers_search():
    """Search customers by name for merge destination picker."""
    q = request.args.get('q', '').strip()
    exclude_id = request.args.get('exclude_id', type=int)
    if len(q) < 2:
        return jsonify([])

    query = Customer.query.filter(
        db.or_(
            Customer.name.ilike(f'%{q}%'),
            Customer.nickname.ilike(f'%{q}%'),
        )
    )
    if exclude_id:
        query = query.filter(Customer.id != exclude_id)
    customers = query.order_by(Customer.name).limit(20).all()

    return jsonify([
        {"id": c.id, "name": c.get_display_name(), "tpid": c.tpid}
        for c in customers
    ])

