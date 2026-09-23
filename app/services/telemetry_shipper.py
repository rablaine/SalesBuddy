"""
Central telemetry shipper for Sales Buddy.

Buffers anonymous, category-level custom events in memory and flushes them
to a shared Application Insights workspace every ~30 seconds.  This lets
App Insights handle all aggregation via Kusto queries, and avoids relying
on a long-running daemon thread that might miss windows when the app is
shut down.

**What is sent (and what is NOT):**

Sent (per request):
    - instance_id  (random UUID generated on first run -- not tied to any user)
    - app_version  (git commit hash)
    - category     (e.g. "Notes", "Revenue", "AI")
    - feature      (Flask view function name, e.g. "reports.whitespace")
    - method       (HTTP verb)
    - status_code
    - response_time_ms
    - is_api       (bool)

Sent (per install, ~once per day):
    - instance_id, app_version
    - user_role        ("se", "dss", or "unknown")
    - feature flags    (msx_auto_writeback, copilot_actions_enabled,
                       show_stale_milestones, show_hygiene_tasks,
                       milestone_auto_sync, workiq_connect_impact, dark_mode)
    - entity counts    (notes, customers, engagements, milestones,
                       projects, partners) - rough usage signal

Sent (per WorkIQ failure):
    - instance_id, app_version
    - operation     (e.g. "query", "meeting_summary", "meeting_list",
                     "attendee_scrape")
    - failure_type  (taxonomy: "npx_missing", "subprocess_timeout",
                     "nonzero_exit", "eula_failed", "server_error",
                     "planning_narration", "refusal", "too_short", "empty",
                     "json_parse_failed", "calendar_lookup_failed")
    - duration_ms   (optional, rounded)

NOT sent (ever):
    - IP addresses, usernames, email addresses, session tokens
    - Customer names, TPID, meeting titles, or any business data
    - Full endpoint paths (only the feature category)
    - User-agent strings
    - Raw stderr / stdout from WorkIQ (only the failure_type bucket)

**Opt-out:**
    Set the environment variable ``SALESBUDDY_TELEMETRY_OPT_OUT=true`` to
    disable all central telemetry shipping.  Local telemetry still works.

Usage::

    from app.services.telemetry_shipper import start_flush_thread, queue_event
    start_flush_thread(app)           # once in app factory
    queue_event(category, ...)        # in after_request hook
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests as http_requests

logger = logging.getLogger(__name__)

# ===========================================================================
# App Insights connection details (NOT a secret -- see docs)
# ===========================================================================
_CONNECTION_STRING = (
    'InstrumentationKey=56e582af-b491-4808-9641-bbb302c62948;'
    'IngestionEndpoint=https://centralus-2.in.applicationinsights.azure.com/;'
    'LiveEndpoint=https://centralus.livediagnostics.monitor.azure.com/;'
    'ApplicationId=84a49533-d8f7-4720-a3cf-9da762f11a64'
)

# Parse the connection string once
_PARSED_CS: dict[str, str] = {}
for _part in _CONNECTION_STRING.split(';'):
    if '=' in _part:
        _k, _v = _part.split('=', 1)
        _PARSED_CS[_k.strip()] = _v.strip()

_INSTRUMENTATION_KEY = _PARSED_CS.get('InstrumentationKey', '')
_INGESTION_ENDPOINT = _PARSED_CS.get('IngestionEndpoint', '').rstrip('/')

# App Insights Track API (v2)
_TRACK_URL = f'{_INGESTION_ENDPOINT}/v2/track'

# Flush interval in seconds.
FLUSH_INTERVAL_SECONDS = 30

# Max events to buffer before forcing an early flush.
MAX_BUFFER_SIZE = 200

# File to persist the random instance ID across restarts.
_INSTANCE_ID_FILENAME = '.salesbuddy_instance_id'


# ===========================================================================
# Instance identity (anonymous)
# ===========================================================================

def _get_data_dir() -> Path:
    """Return the data directory (same parent as the database)."""
    from app.db_paths import resolve_data_dir
    return resolve_data_dir()


def get_instance_id() -> str:
    """Return (or create) a stable anonymous instance ID.

    Stored as a plain UUID in ``data/.salesbuddy_instance_id``.
    """
    data_dir = _get_data_dir()
    id_file = data_dir / _INSTANCE_ID_FILENAME

    if id_file.exists():
        stored = id_file.read_text().strip()
        if stored:
            return stored

    new_id = str(uuid.uuid4())
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        id_file.write_text(new_id)
    except OSError:
        pass  # Non-fatal -- we'll just generate a new one next time
    return new_id


def is_telemetry_enabled() -> bool:
    """Check whether central telemetry shipping is enabled.

    Disabled when ``SALESBUDDY_TELEMETRY_OPT_OUT`` is set to a truthy
    value, or when ``FLASK_ENV=development`` (so dev work doesn't pollute
    production telemetry by default).
    """
    opt_out = os.environ.get('SALESBUDDY_TELEMETRY_OPT_OUT', '').lower()
    if opt_out in ('true', '1', 'yes'):
        return False
    if os.environ.get('FLASK_ENV', '').lower() == 'development':
        return False
    return True


# ===========================================================================
# App Insights envelope builder
# ===========================================================================

def _build_custom_event(
    name: str,
    properties: dict[str, str],
    measurements: dict[str, float],
) -> dict[str, Any]:
    """Build an App Insights custom event envelope."""
    return {
        'name': 'Microsoft.ApplicationInsights.Event',
        'time': datetime.now(timezone.utc).isoformat(),
        'iKey': _INSTRUMENTATION_KEY,
        'tags': {
            'ai.cloud.roleInstance': 'salesbuddy',
        },
        'data': {
            'baseType': 'EventData',
            'baseData': {
                'ver': 2,
                'name': name,
                'properties': properties,
                'measurements': measurements,
            },
        },
    }


# ===========================================================================
# In-memory event buffer
# ===========================================================================

_buffer: list[dict] = []
_buffer_lock = threading.Lock()
_app_version: str = 'unknown'
_instance_id: str = ''

# Stats for the admin panel
_stats_lock = threading.Lock()
_stats: dict[str, Any] = {
    'events_queued': 0,
    'events_flushed': 0,
    'flush_count': 0,
    'flush_errors': 0,
    'last_flush_time': None,
    'last_flush_events': 0,
    'last_error': None,
}


def queue_event(
    category: str,
    method: str,
    status_code: int,
    response_time_ms: Optional[float],
    is_api: bool,
    app_mode: str = 'unknown',
    feature: str = '',
) -> None:
    """Add an event to the in-memory buffer for the next flush.

    Called from the ``after_request`` hook in ``telemetry.py``.  This is
    intentionally lightweight -- just appends to a list under a lock.

    Args:
        category: Feature category (e.g. "Notes", "Admin").
        method: HTTP method (GET, POST, etc.).
        status_code: HTTP response status code.
        response_time_ms: Request duration in milliseconds.
        is_api: Whether the request was to an API endpoint.
        app_mode: Which shell served the request - "electron" or "browser".
    """
    if not is_telemetry_enabled():
        return

    envelope = _build_custom_event(
        name='SalesBuddy.FeatureUsage',
        properties={
            'instance_id': _instance_id or get_instance_id(),
            'app_version': _app_version,
            'category': category,
            'feature': feature or '',
            'method': method,
            'is_api': str(is_api),
            'app_mode': app_mode,
        },
        measurements={
            'status_code': float(status_code),
            'response_time_ms': round(float(response_time_ms), 1) if response_time_ms else 0.0,
            'is_error': 1.0 if status_code >= 400 else 0.0,
        },
    )

    with _buffer_lock:
        _buffer.append(envelope)

    with _stats_lock:
        _stats['events_queued'] += 1

    # If buffer is getting large, flush early in a background thread
    if len(_buffer) >= MAX_BUFFER_SIZE:
        threading.Thread(target=flush_buffer, daemon=True).start()


# Allowed WorkIQ failure_type values. Anything outside this set is rejected
# so we don't accidentally leak free-form error text into telemetry.
# Two flavors:
#   - server_down failures: WorkIQ itself didn't respond properly
#   - parse_failed failures: WorkIQ responded but our parser couldn't
#     handle the output, identified per call site so a regression in one
#     parser is easy to spot
_WORKIQ_FAILURE_TYPES = frozenset({
    # server_down flavors
    'npx_missing',
    'subprocess_timeout',
    'nonzero_exit',
    'eula_failed',
    'server_error',
    'calendar_lookup_failed',
    # parse_failed flavors, one per query_workiq() caller
    'parse_attendee_json',
    'parse_customer_json',
    'parse_partner_json',
    'parse_meeting_list_json',
    'parse_copilot_actions_json',
})

# Allowed WorkIQ operation values. Mirrors the operation= argument
# threaded through query_workiq() for telemetry tagging.
_WORKIQ_OPERATIONS = frozenset({
    'query',
    'meeting_summary',
    'meeting_list',
    'attendee_scrape',
    'customer_scrape',
    'partner_scrape',
    'copilot_actions',
})

# Allowed status values for SalesBuddy.WorkIQCall.
#   ok          - WorkIQ subprocess returned and downstream parser succeeded
#   server_down - WorkIQ subprocess failed (npx, exit code, server_error, ...)
#   parse_failed - WorkIQ responded but our parser couldn't read it
_WORKIQ_STATUSES = frozenset({'ok', 'server_down', 'parse_failed'})


def queue_workiq_call(
    operation: str,
    status: str,
    failure_type: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> None:
    """Record a WorkIQ call outcome as a custom telemetry event.

    Emits ``SalesBuddy.WorkIQCall`` with anonymous instance/app metadata
    plus a small fixed taxonomy of operation, status, and (for failures)
    failure_type. No raw error text, meeting titles, or stderr is
    included.

    Used by the /metrics WorkIQ uptime card to compute uptime % and to
    surface which parser is breaking when WorkIQ output drifts.

    Args:
        operation: One of :data:`_WORKIQ_OPERATIONS`.
        status: One of :data:`_WORKIQ_STATUSES`.
        failure_type: For non-ok statuses, one of :data:`_WORKIQ_FAILURE_TYPES`.
        duration_ms: Optional elapsed time before the call resolved.
    """
    if not is_telemetry_enabled():
        return

    # Defensive: clamp to known taxonomy so a future caller can't
    # accidentally ship free-form text.
    if operation not in _WORKIQ_OPERATIONS:
        operation = 'query'
    if status not in _WORKIQ_STATUSES:
        status = 'server_down'
    if status == 'ok':
        failure_type = None
    elif failure_type not in _WORKIQ_FAILURE_TYPES:
        failure_type = 'nonzero_exit'

    measurements: dict[str, float] = {'count': 1.0}
    if duration_ms is not None:
        measurements['duration_ms'] = round(float(duration_ms), 1)

    properties = {
        'instance_id': _instance_id or get_instance_id(),
        'app_version': _app_version,
        'operation': operation,
        'status': status,
    }
    if failure_type:
        properties['failure_type'] = failure_type

    envelope = _build_custom_event(
        name='SalesBuddy.WorkIQCall',
        properties=properties,
        measurements=measurements,
    )

    with _buffer_lock:
        _buffer.append(envelope)

    with _stats_lock:
        _stats['events_queued'] += 1

    if len(_buffer) >= MAX_BUFFER_SIZE:
        threading.Thread(target=flush_buffer, daemon=True).start()


# Allowed result values for the MSX Account Teams probe. See
# ``queue_msx_outage`` below for semantics.
_MSX_PROBE_RESULTS = frozenset({
    'ok',
    'outage',
    'skipped_no_token',
    'skipped_no_vpn',
    'error',
})

# In-memory dedupe state for queue_msx_outage. Keyed by result value, holds
# the UTC hour bucket (YYYYMMDDHH) the result was last emitted in. Same
# (result, hour) only emits once per process, so the startup probe + hourly
# probe colliding in the same hour can't double-count.
_msx_probe_dedupe_lock = threading.Lock()
_msx_probe_last_hour: dict[str, str] = {}


def _current_hour_bucket() -> str:
    """Return the current UTC hour as a string like ``2026050514``."""
    return datetime.now(timezone.utc).strftime('%Y%m%d%H')


def queue_msx_outage(result: str, error_code: Optional[str] = None) -> bool:
    """Record the outcome of an MSX Account Teams probe.

    Emits a ``SalesBuddy.MsxAccountTeamProbe`` custom event so the metrics
    dashboard can chart real coverage (absence of data does not imply
    outage). Same ``result`` value within the same UTC hour from this
    process is deduped to a single emission.

    Args:
        result: One of ``ok``, ``outage``, ``skipped_no_token``,
            ``skipped_no_vpn``, ``error``. Any unknown value is mapped to
            ``error``.
        error_code: Optional short error code (e.g. ``"0x80040224"``) for
            outage / error results.

    Returns:
        True if the event was queued, False if it was deduped or telemetry
        is disabled.
    """
    if not is_telemetry_enabled():
        return False

    if result not in _MSX_PROBE_RESULTS:
        result = 'error'

    bucket = _current_hour_bucket()
    with _msx_probe_dedupe_lock:
        if _msx_probe_last_hour.get(result) == bucket:
            return False
        _msx_probe_last_hour[result] = bucket

    properties: dict[str, str] = {
        'instance_id': _instance_id or get_instance_id(),
        'app_version': _app_version,
        'result': result,
    }
    if error_code:
        properties['error_code'] = str(error_code)[:64]

    envelope = _build_custom_event(
        name='SalesBuddy.MsxAccountTeamProbe',
        properties=properties,
        measurements={'count': 1.0},
    )

    with _buffer_lock:
        _buffer.append(envelope)

    with _stats_lock:
        _stats['events_queued'] += 1

    if len(_buffer) >= MAX_BUFFER_SIZE:
        threading.Thread(target=flush_buffer, daemon=True).start()

    return True


def flush_buffer() -> dict[str, Any]:
    """Flush all buffered events to App Insights.

    Returns a summary dict with ``flushed``, ``events_sent``, or ``error``.
    Safe to call from any thread.
    """
    if not _INSTRUMENTATION_KEY or not _INGESTION_ENDPOINT:
        return {'flushed': False, 'reason': 'no connection string'}

    # Swap the buffer under the lock (minimal lock time)
    with _buffer_lock:
        if not _buffer:
            return {'flushed': False, 'reason': 'buffer empty', 'events_sent': 0}
        batch = list(_buffer)
        _buffer.clear()

    # Ship to App Insights (newline-delimited JSON)
    payload = '\n'.join(json.dumps(e) for e in batch)

    try:
        resp = http_requests.post(
            _TRACK_URL,
            data=payload,
            headers={'Content-Type': 'application/x-json-stream'},
            timeout=10,
        )
        resp.raise_for_status()
        logger.debug(
            'Telemetry flushed: %d events (status %d)', len(batch), resp.status_code,
        )
        with _stats_lock:
            _stats['events_flushed'] += len(batch)
            _stats['flush_count'] += 1
            _stats['last_flush_time'] = datetime.now(timezone.utc).isoformat()
            _stats['last_flush_events'] = len(batch)

        return {
            'flushed': True,
            'events_sent': len(batch),
            'status_code': resp.status_code,
        }
    except Exception as e:
        logger.warning('Telemetry flush failed: %s', e)
        # Put events back so they are not lost (best-effort)
        with _buffer_lock:
            _buffer.extend(batch)
        with _stats_lock:
            _stats['flush_errors'] += 1
            _stats['last_error'] = str(e)
        return {'flushed': False, 'error': str(e)}


def get_flush_stats() -> dict[str, Any]:
    """Return a snapshot of flush statistics for the admin panel."""
    with _stats_lock:
        stats = dict(_stats)
    with _buffer_lock:
        stats['buffer_size'] = len(_buffer)
    stats['enabled'] = is_telemetry_enabled()
    stats['instance_id'] = _instance_id or get_instance_id()
    return stats


# ===========================================================================
# Install profile event (role + feature flags + entity counts)
# ===========================================================================

# Re-emit the install profile this often. Daily is plenty - it lets us see
# install-level config drift over time without spamming events.
PROFILE_INTERVAL_SECONDS = 24 * 60 * 60

_last_profile_sent: float = 0.0
_profile_lock = threading.Lock()


def _collect_install_profile(app) -> Optional[dict[str, Any]]:
    """Build the install profile envelope from UserPreference + entity counts.

    Returns None if we can't read the database (e.g. during early startup
    before migrations finish). Safe to call from any thread that has an
    app context available.
    """
    try:
        with app.app_context():
            from app.models import (
                db, UserPreference, Note, Customer, Engagement,
                Milestone, Project, Partner,
            )

            prefs = UserPreference.query.first()
            if prefs is None:
                role = 'unknown'
                flags = {
                    'msx_auto_writeback': False,
                    'copilot_actions_enabled': False,
                    'show_stale_milestones': False,
                    'show_hygiene_tasks': False,
                    'milestone_auto_sync': False,
                    'workiq_connect_impact': False,
                    'dark_mode': False,
                    'has_workiq_prompt': False,
                    'has_default_template_customer': False,
                    'has_default_template_noncustomer': False,
                }
            else:
                role = prefs.user_role or 'unknown'
                flags = {
                    'msx_auto_writeback': bool(prefs.msx_auto_writeback),
                    'copilot_actions_enabled': bool(prefs.copilot_actions_enabled),
                    'show_stale_milestones': bool(prefs.show_stale_milestones),
                    'show_hygiene_tasks': bool(prefs.show_hygiene_tasks),
                    'milestone_auto_sync': bool(prefs.milestone_auto_sync),
                    'workiq_connect_impact': bool(prefs.workiq_connect_impact),
                    'dark_mode': bool(prefs.dark_mode) if prefs.dark_mode is not None else False,
                    'has_workiq_prompt': bool(prefs.workiq_summary_prompt),
                    'has_default_template_customer': prefs.default_template_customer_id is not None,
                    'has_default_template_noncustomer': prefs.default_template_noncustomer_id is not None,
                }

            counts = {
                'note_count': float(db.session.query(Note).count()),
                'customer_count': float(db.session.query(Customer).count()),
                'engagement_count': float(db.session.query(Engagement).count()),
                'milestone_count': float(db.session.query(Milestone).count()),
                'project_count': float(db.session.query(Project).count()),
                'partner_count': float(db.session.query(Partner).count()),
            }

        properties = {
            'instance_id': _instance_id or get_instance_id(),
            'app_version': _app_version,
            'user_role': role,
        }
        # Stringify flags - App Insights properties are strings only
        for k, v in flags.items():
            properties[k] = 'true' if v else 'false'

        return _build_custom_event(
            name='SalesBuddy.InstallProfile',
            properties=properties,
            measurements=counts,
        )
    except Exception as e:
        logger.debug('Could not collect install profile: %s', e)
        return None


def queue_install_profile(app, force: bool = False) -> bool:
    """Queue an install profile event if one hasn't been sent in 24h.

    Args:
        app: Flask application (needed for app_context).
        force: If True, send regardless of last-sent time.

    Returns:
        True if an event was queued, False if skipped.
    """
    global _last_profile_sent

    if not is_telemetry_enabled():
        return False

    with _profile_lock:
        now = time.time()
        if not force and (now - _last_profile_sent) < PROFILE_INTERVAL_SECONDS:
            return False
        # Optimistically mark as sent; if collection fails we'll retry next interval
        _last_profile_sent = now

    envelope = _collect_install_profile(app)
    if envelope is None:
        return False

    with _buffer_lock:
        _buffer.append(envelope)
    with _stats_lock:
        _stats['events_queued'] += 1
    return True


# ===========================================================================
# Background flush thread
# ===========================================================================

_flush_thread: threading.Thread | None = None


def start_flush_thread(
    app=None,
    interval_seconds: int = FLUSH_INTERVAL_SECONDS,
) -> None:
    """Start a daemon thread that flushes the buffer on a schedule.

    Args:
        app: Flask application (used to read BOOT_COMMIT config).
        interval_seconds: How often to flush (default 30s).
    """
    global _flush_thread, _app_version, _instance_id

    if not is_telemetry_enabled():
        logger.info('Central telemetry disabled (SALESBUDDY_TELEMETRY_OPT_OUT)')
        return

    if _flush_thread is not None and _flush_thread.is_alive():
        return  # Already running

    # Cache instance ID and app version so we don't hit disk on every request
    _instance_id = get_instance_id()
    if app:
        _app_version = app.config.get('BOOT_COMMIT') or 'unknown'

    # Send an install profile on startup (once per day max)
    if app:
        try:
            queue_install_profile(app)
        except Exception as e:
            logger.debug('Initial install profile queue failed: %s', e)

    def _flush_loop():
        while True:
            time.sleep(interval_seconds)
            try:
                flush_buffer()
            except Exception as e:
                logger.warning('Telemetry flush thread error: %s', e)
            # Re-emit the install profile if the 24h window has passed.
            # Cheap no-op when not yet due.
            if app:
                try:
                    queue_install_profile(app)
                except Exception as e:
                    logger.debug('Install profile re-emit failed: %s', e)

    _flush_thread = threading.Thread(
        target=_flush_loop,
        name='telemetry-flush',
        daemon=True,
    )
    _flush_thread.start()
    logger.info(
        'Telemetry flush thread started (interval=%ds)', interval_seconds,
    )
