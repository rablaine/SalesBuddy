"""
Local usage telemetry service for Sales Buddy.

Captures every HTTP request/response cycle and logs it to the
``usage_events`` table. No PII is stored -- no IP addresses, user-agents,
session tokens, or usernames.

Usage:
    Call ``init_telemetry(app)`` once during app creation to register the
    Flask before/after request hooks.

Endpoint categories are derived automatically from the blueprint name and
URL pattern so you can answer questions like "which features get the most
use?" or "which API calls fail most often?"
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, Request, g, request
from werkzeug.wrappers import Response


# ===========================================================================
# Category mapping -- keeps the analytics view clean
# ===========================================================================

# Blueprint -> human-friendly category.  Anything not listed falls back to
# the blueprint name itself (title-cased).
_BLUEPRINT_CATEGORIES: dict[str, str] = {
    'admin': 'Admin',
    'ai': 'AI',
    'backup': 'Backup',
    'notes': 'Call Logs',
    'connect_export': 'Connect Export',
    'customers': 'Customers',
    'main': 'General',
    'milestones': 'Milestones',
    'msx': 'MSX Integration',
    'opportunities': 'Opportunities',
    'partners': 'Partners',
    'pods': 'Pods',
    'revenue': 'Revenue',
    'sellers': 'Sellers',
    'solution_engineers': 'Solution Engineers',
    'territories': 'Territories',
    'topics': 'Topics',
}

# Paths to exclude from logging (health checks, static assets, etc.)
_EXCLUDE_PREFIXES: tuple[str, ...] = (
    '/static/',
    '/health',
    '/sw.js',
    '/manifest.json',
    '/favicon.ico',
)


def _derive_category(blueprint: Optional[str], path: str) -> str:
    """Map a request to a human-readable feature category.

    Args:
        blueprint: Flask blueprint name, or None.
        path: The URL path.

    Returns:
        A short category string suitable for grouping in reports.
    """
    if blueprint:
        return _BLUEPRINT_CATEGORIES.get(blueprint, blueprint.replace('_', ' ').title())
    # Fallback for un-blueprinted routes
    if path.startswith('/api/'):
        return 'API'
    return 'Other'


def _safe_referrer_path(req: Request) -> Optional[str]:
    """Extract just the path portion from the Referer header.

    Strips the scheme, host, and query string so no PII leaks through.
    Returns None when the header is absent or unparseable.
    """
    referrer = req.referrer
    if not referrer:
        return None
    try:
        parsed = urlparse(referrer)
        return parsed.path or None
    except Exception:
        return None


def _should_log(path: str) -> bool:
    """Decide whether a request path should be recorded."""
    return not any(path.startswith(prefix) for prefix in _EXCLUDE_PREFIXES)


# ===========================================================================
# Flask integration
# ===========================================================================

def init_telemetry(app: Flask) -> None:
    """Register before/after request hooks for telemetry capture.

    Call this once from the app factory (``create_app``).
    """

    @app.before_request
    def _telemetry_start():
        """Stamp the request start time."""
        g._telemetry_start = time.perf_counter()

    @app.after_request
    def _telemetry_log(response: Response) -> Response:
        """Log the completed request/response to the usage_events table."""
        path = request.path

        if not _should_log(path):
            return response

        elapsed_ms: Optional[float] = None
        start = getattr(g, '_telemetry_start', None)
        if start is not None:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

        blueprint = request.blueprints[0] if request.blueprints else None
        is_api = path.startswith('/api/')

        # Derive error info from the HTTP status code.  We intentionally
        # do NOT install a Flask errorhandler because that would interfere
        # with the app's own error handling (e.g. 404 pages, static files).
        error_type: Optional[str] = None
        error_message: Optional[str] = None
        if response.status_code >= 400:
            error_type = f'HTTP {response.status_code}'

        category = _derive_category(blueprint, path)

        try:
            from app.models import db, UsageEvent

            event = UsageEvent(
                method=request.method,
                endpoint=path,
                blueprint=blueprint,
                view_function=request.endpoint,
                is_api=is_api,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                referrer_path=_safe_referrer_path(request),
                error_type=error_type,
                error_message=error_message,
                category=category,
            )
            db.session.add(event)
            db.session.commit()
        except Exception:
            # Telemetry must never break the actual request.
            try:
                from app.models import db
                db.session.rollback()
            except Exception:
                pass

        # Queue event for central telemetry (App Insights).
        # This is intentionally outside the try/except above so a
        # local DB failure doesn't prevent central shipping.
        try:
            from app.services.telemetry_shipper import queue_event
            app_mode = request.cookies.get('sb_app_mode', 'unknown')
            queue_event(
                category=category,
                method=request.method,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                is_api=is_api,
                app_mode=app_mode,
            )
        except Exception:
            pass  # Central telemetry must never break the request

        return response
