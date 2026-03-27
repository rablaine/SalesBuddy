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
    - category     (e.g. "Call Logs", "Revenue", "AI")
    - method       (HTTP verb)
    - status_code
    - response_time_ms
    - is_api       (bool)

NOT sent (ever):
    - IP addresses, usernames, email addresses, session tokens
    - Customer names, TPID, or any business data
    - Full endpoint paths (only the feature category)
    - User-agent strings

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
    db_url = os.environ.get('DATABASE_URL') or 'sqlite:///data/salesbuddy.db'
    # Extract path from sqlite:///path
    if db_url.startswith('sqlite:///'):
        db_path = Path(db_url.replace('sqlite:///', ''))
        return db_path.parent
    return Path('data')


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

    Disabled when ``SALESBUDDY_TELEMETRY_OPT_OUT`` is set to a truthy value.
    """
    opt_out = os.environ.get('SALESBUDDY_TELEMETRY_OPT_OUT', '').lower()
    return opt_out not in ('true', '1', 'yes')


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
) -> None:
    """Add an event to the in-memory buffer for the next flush.

    Called from the ``after_request`` hook in ``telemetry.py``.  This is
    intentionally lightweight -- just appends to a list under a lock.

    Args:
        category: Feature category (e.g. "Call Logs", "Admin").
        method: HTTP method (GET, POST, etc.).
        status_code: HTTP response status code.
        response_time_ms: Request duration in milliseconds.
        is_api: Whether the request was to an API endpoint.
        app_mode: Client app mode - "standalone" (PWA), "browser", or "unknown".
    """
    if not is_telemetry_enabled():
        return

    envelope = _build_custom_event(
        name='SalesBuddy.FeatureUsage',
        properties={
            'instance_id': _instance_id or get_instance_id(),
            'app_version': _app_version,
            'category': category,
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

    def _flush_loop():
        while True:
            time.sleep(interval_seconds)
            try:
                flush_buffer()
            except Exception as e:
                logger.warning('Telemetry flush thread error: %s', e)

    _flush_thread = threading.Thread(
        target=_flush_loop,
        name='telemetry-flush',
        daemon=True,
    )
    _flush_thread.start()
    logger.info(
        'Telemetry flush thread started (interval=%ds)', interval_seconds,
    )
