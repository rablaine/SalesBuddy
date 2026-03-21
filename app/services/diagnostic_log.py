"""Diagnostic logging service for troubleshooting integration issues.

Writes structured JSON Lines to data/diagnostic.jsonl with a rolling
48-hour retention window.  Captures MSX API calls, gateway/AI calls,
milestone writeback payloads, and 500 errors with stack traces.

Usage:
    from app.services.diagnostic_log import diag_log
    diag_log('msx_api', method='POST', url=url, status=200, ...)
"""

import json
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RETENTION_HOURS = 48
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'data')
LOG_FILE = os.path.join(LOG_DIR, 'diagnostic.jsonl')
MAX_PAYLOAD_CHARS = 5000
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Per-request correlation ID (set by Flask before_request hook)
# ---------------------------------------------------------------------------

_correlation_id = threading.local()


def set_correlation_id(cid: str | None = None) -> str:
    """Set or generate a correlation ID for the current request."""
    cid = cid or uuid.uuid4().hex[:12]
    _correlation_id.value = cid
    return cid


def get_correlation_id() -> str:
    """Return the current correlation ID, or 'none' if unset."""
    return getattr(_correlation_id, 'value', 'none')


# ---------------------------------------------------------------------------
# Core log function
# ---------------------------------------------------------------------------

def diag_log(category: str, **fields) -> None:
    """Append a single diagnostic log entry to the JSONL file.

    Args:
        category: Event type (e.g. 'msx_api', 'gateway', 'writeback', 'error').
        **fields: Arbitrary key-value pairs for the event.
    """
    try:
        entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'cid': get_correlation_id(),
            'cat': category,
        }
        # Truncate any large string values
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > MAX_PAYLOAD_CHARS:
                entry[k] = v[:MAX_PAYLOAD_CHARS] + '...[truncated]'
            else:
                entry[k] = v
        line = json.dumps(entry, default=str, ensure_ascii=False) + '\n'
        with _write_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
    except Exception:
        # Never let diagnostic logging break the app
        logger.debug('Failed to write diagnostic log entry', exc_info=True)


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------

def prune_old_entries() -> int:
    """Remove log entries older than RETENTION_HOURS.

    Returns:
        Number of entries removed.
    """
    if not os.path.exists(LOG_FILE):
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)
    cutoff_iso = cutoff.isoformat()
    kept = []
    removed = 0

    try:
        with _write_lock:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get('ts', '') >= cutoff_iso:
                            kept.append(line)
                        else:
                            removed += 1
                    except json.JSONDecodeError:
                        removed += 1

            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                for line in kept:
                    f.write(line + '\n')
    except Exception:
        logger.debug('Failed to prune diagnostic log', exc_info=True)

    return removed


# ---------------------------------------------------------------------------
# Export for download
# ---------------------------------------------------------------------------

def get_log_path() -> str | None:
    """Return the log file path if it exists, else None."""
    if os.path.exists(LOG_FILE):
        return LOG_FILE
    return None


def get_log_stats() -> dict:
    """Return basic stats about the diagnostic log."""
    if not os.path.exists(LOG_FILE):
        return {'exists': False, 'size_bytes': 0, 'entry_count': 0}

    size = os.path.getsize(LOG_FILE)
    count = 0
    oldest = None
    newest = None
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    entry = json.loads(line)
                    ts = entry.get('ts', '')
                    if oldest is None or ts < oldest:
                        oldest = ts
                    if newest is None or ts > newest:
                        newest = ts
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    return {
        'exists': True,
        'size_bytes': size,
        'size_human': _human_size(size),
        'entry_count': count,
        'oldest': oldest,
        'newest': newest,
    }


def _human_size(nbytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ('B', 'KB', 'MB'):
        if nbytes < 1024:
            return f'{nbytes:.1f} {unit}'
        nbytes /= 1024
    return f'{nbytes:.1f} GB'


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------

def init_diagnostic_log(app) -> None:
    """Register before/after request hooks for correlation IDs and error
    logging.  Also prune old entries on startup."""

    # Prune on startup (non-blocking)
    try:
        removed = prune_old_entries()
        if removed > 0:
            logger.info('Diagnostic log: pruned %d old entries', removed)
    except Exception:
        pass

    @app.before_request
    def _diag_set_correlation_id():
        from flask import request, g
        g._diag_cid = set_correlation_id()
        g._diag_start = time.perf_counter()

    @app.after_request
    def _diag_log_errors(response):
        from flask import g, request
        # Log all 500 errors
        if response.status_code >= 500:
            elapsed = (time.perf_counter() - getattr(g, '_diag_start', 0)) * 1000
            diag_log('error',
                     method=request.method,
                     path=request.path,
                     status=response.status_code,
                     elapsed_ms=round(elapsed, 1))
        return response

    # Log unhandled exceptions with stack traces via signal
    from flask import got_request_exception

    def _diag_log_exception(sender, exception, **kwargs):
        from werkzeug.exceptions import HTTPException
        if isinstance(exception, HTTPException):
            return
        try:
            from flask import request
            diag_log('error',
                     method=request.method,
                     path=request.path,
                     error=str(exception)[:1000],
                     traceback=traceback.format_exc()[:3000])
        except Exception:
            pass

    got_request_exception.connect(_diag_log_exception, app)
