"""
Unadvertised metrics dashboard, backed by App Insights.

This route is intentionally NOT linked from the nav. There's no identity
gate in code - whoever opens it queries App Insights with their own
existing CLI or Azure SDK credentials. Users without
read access on the ``NoteHelper_Telemetry`` resource get 403 responses,
which the page renders as per-card error states.

Activity counts successful page opens from anonymous installs, not background requests.
Install snapshots and operational health remain separate from active usage.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading

from flask import Blueprint, abort, jsonify, render_template, request

from app.services.app_insights import (
    TENANT_ID as APP_INSIGHTS_TENANT_ID,
    cli_auth_environment,
    reset_token_cache,
)

from app.services.usage_metrics import _coerce_days, get_metric_card

logger = logging.getLogger(__name__)

metrics_bp = Blueprint('metrics', __name__)
_login_lock = threading.Lock()
_login_process: subprocess.Popen | None = None


@metrics_bp.route('/metrics')
def metrics_dashboard():
    """Render the (unadvertised) dashboard shell. Cards load via /api/metrics/<card>."""
    days = _coerce_days(request.args.get('days', 7))
    return render_template('metrics.html', days=days)


@metrics_bp.route('/api/metrics/card/<card_id>')
def api_metrics_card(card_id: str):
    """Run a single dashboard card's KQL and return JSON.

    Each card is fetched independently so the page can paint immediately
    and stream results as they arrive. Errors are returned in the JSON
    body (with status 200) so the client can render a per-card error
    state without triggering generic browser error UI.
    """
    days = _coerce_days(request.args.get('days', 7))
    try:
        return jsonify(get_metric_card(card_id, days))
    except ValueError:
        abort(404)


@metrics_bp.route('/api/metrics/sign-in', methods=['POST'])
def api_metrics_sign_in():
    """Pop an interactive ``az login`` for the App Insights tenant.

    Used by the metrics dashboard when the user lands on a 403 because
    they cleared their ``az`` cache (``az account clear``) or have never
    completed the BlaineCorp guest MFA. Sales Buddy is single-user and
    runs on the user's own machine, so the browser popup goes straight
    to them.

    The route returns immediately after spawning the process. The client
    polls a card endpoint to detect when authentication completes.
    """
    global _login_process

    az = shutil.which('az') or shutil.which('az.cmd')
    if not az:
        return jsonify({'error': 'Azure CLI (az) not found on PATH'}), 500

    cmd = [
        az, 'login',
        '--tenant', APP_INSIGHTS_TENANT_ID,
        '--scope', 'https://api.applicationinsights.io/.default',
        '--allow-no-subscriptions',
    ]
    try:
        # We want the popup to be visible so the user can complete MFA.
        # Don't capture stdio - that would block on the prompt. Don't
        # wait either - we return immediately.
        creationflags = 0
        if sys.platform == 'win32':
            CREATE_NEW_CONSOLE = 0x00000010
            creationflags = CREATE_NEW_CONSOLE
        with _login_lock:
            if _login_process is not None and _login_process.poll() is None:
                return jsonify({
                    'success': True,
                    'message': 'A sign-in attempt is already running.',
                })
            _login_process = subprocess.Popen(
                cmd, creationflags=creationflags, close_fds=True, env=cli_auth_environment(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Failed to spawn az login')
        return jsonify({'error': f'Failed to spawn az login: {exc}'}), 500

    # Drop the in-process cache so the next card request acquires a
    # fresh token from the new disk cache.
    reset_token_cache()
    return jsonify({
        'success': True,
        'message': 'A browser window should open to complete sign-in.',
    })
