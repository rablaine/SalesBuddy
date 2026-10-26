"""
Thin wrapper over the Application Insights REST API.

Used by the unadvertised ``/metrics`` dashboard to run KQL queries
against the shared ``NoteHelper_Telemetry`` workspace. Authentication
reuses existing CLI and Azure SDK credentials without interactive authentication.
Users without read access on the App Insights resource
will get HTTP 403 responses; the dashboard renders that as a per-card
error rather than crashing the page.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class AppInsightsError(Exception):
    """Raised on auth or HTTP failures from the App Insights REST API."""

    def __init__(
        self, message: str, status_code: Optional[int] = None,
        auth_required: Optional[bool] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.auth_required = auth_required


# App ID for the NoteHelper_Telemetry component (parsed from the
# connection string in telemetry_shipper.py - same App Insights resource
# we ship to).
APP_ID = '84a49533-d8f7-4720-a3cf-9da762f11a64'

# Prefer the resource tenant for guest accounts, while retaining existing
# default-account and SDK sign-ins if that CLI tenant session is unavailable.
TENANT_ID = '96d12531-723e-46c1-842b-0480739c7419'

_API_BASE = 'https://api.applicationinsights.io/v1/apps'
_TOKEN_SCOPE = 'https://api.applicationinsights.io/.default'

_cached_token: Optional[str] = None
_token_expiry: float = 0
_token_lock = threading.Lock()
_auth_retry_after: float = 0
_cached_auth_error: Optional[str] = None
_cached_auth_required: bool = False
_DEFAULT_TIMEOUT_SECONDS = 10

# Shared HTTP session with connection pooling + transient-error retries.
# The App Insights edge occasionally drops TLS handshakes mid-flight
# (observed as ``SSLEOFError: UNEXPECTED_EOF_WHILE_READING``). Without
# retries even a ~20% drop rate paints the whole metrics dashboard red,
# because the page fans out one request per card in parallel. We retry
# on connection-level failures only - never on 4xx, since those are
# real auth/permission problems that won't fix themselves.
_session: Optional[requests.Session] = None


def _build_session() -> requests.Session:
    sess = requests.Session()
    # Total of 6 attempts with exponential backoff:
    #   0.5s, 1s, 2s, 4s, 8s -> ~15.5s of waiting + 6 actual requests.
    # The App Insights edge sometimes drops TLS handshakes for a stretch
    # of 30-60s; with 6 attempts plus a 10s per-request timeout we cover
    # roughly the full bad-minute window before surfacing the error to
    # the user. urllib3 caps the per-sleep at BACKOFF_MAX (120s default).
    retry = Retry(
        total=6,
        connect=6,
        read=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET', 'POST']),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=16)
    sess.mount('https://', adapter)
    sess.mount('http://', adapter)
    return sess


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
    return _session


def cli_auth_environment() -> dict[str, str]:
    """Disable broker UI only in metrics CLI children, preserving the app's Azure profile."""
    return {
        **os.environ,
        'AZURE_CORE_ENABLE_BROKER_ON_WINDOWS': 'false',
        'AZURE_CORE_ENABLE_BROKER_ON_MAC': 'false',
        'AZURE_CORE_LOGIN_EXPERIENCE_V2': 'off',
        'AZURE_CORE_NO_COLOR': 'true',
    }


def _request_cli_token(tenant_id: Optional[str] = TENANT_ID) -> tuple[str, float]:
    """Read an existing CLI credential without WAM, browser fallback or standard input."""
    az = shutil.which('az') or shutil.which('az.cmd')
    if not az:
        raise AppInsightsError('Failed to acquire App Insights token: Azure CLI is unavailable.')
    command = [az]
    cli_python = Path(az).parent.parent / 'python.exe'
    if Path(az).name.lower() == 'az.cmd' and cli_python.is_file():
        # Terminate the actual MSI CLI host on timeout, not just its batch wrapper.
        command = [str(cli_python), '-IBm', 'azure.cli']
    result = subprocess.run(
        [
            *command, 'account', 'get-access-token',
            *(['--tenant', tenant_id] if tenant_id else []),
            '--scope', _TOKEN_SCOPE, '--output', 'json', '--only-show-errors',
        ],
        env=cli_auth_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=10,
        check=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError('Azure CLI returned an invalid token response')
    token = payload.get('accessToken')
    expires_on = float(payload['expires_on'])
    if not isinstance(token, str) or not token or not math.isfinite(expires_on):
        raise ValueError('Azure CLI returned an invalid token response')
    if expires_on <= time.time() + 60:
        raise ValueError('Azure CLI returned a token that is already expiring')
    return token, expires_on


def _request_existing_token() -> tuple[str, float]:
    """Preserve tenant-specific, default CLI and SDK sign-ins without opening sign-in UI."""
    cli_requires_sign_in = True
    for tenant_id in (TENANT_ID, None):
        try:
            return _request_cli_token(tenant_id=tenant_id)
        except Exception as exc:
            stderr = str(exc.stderr or '').lower() if isinstance(
                exc, subprocess.CalledProcessError,
            ) else ''
            cli_requires_sign_in = cli_requires_sign_in and any(
                marker in stderr for marker in ('az login', 'interaction_required', 'invalid_grant')
            )
            logger.info(
                'App Insights %s CLI credential unavailable: %s',
                'resource-tenant' if tenant_id else 'default-account', type(exc).__name__,
            )

    try:
        # SDK CLI is replaced by the controlled reads above. Other existing sign-ins
        # retain the original default-tenant behavior, with automatic interaction disabled.
        with DefaultAzureCredential(
            additionally_allowed_tenants=[TENANT_ID],
            exclude_cli_credential=True,
            exclude_interactive_browser_credential=True,
            disable_automatic_authentication=True,
        ) as credential:
            token = credential.get_token(_TOKEN_SCOPE)
            return token.token, token.expires_on
    except Exception as exc:
        auth_required = cli_requires_sign_in and isinstance(exc, ClientAuthenticationError)
        message = (
            'Existing App Insights credentials require sign-in.'
            if auth_required else
            'Unable to acquire an App Insights token from existing credentials. '
            'No sign-in was started.'
        )
        raise AppInsightsError(message, auth_required=auth_required) from exc


def _get_token() -> str:
    """Share one silent credential acquisition across concurrent dashboard cards."""
    global _cached_token, _token_expiry, _auth_retry_after, _cached_auth_error
    global _cached_auth_required

    with _token_lock:
        now = time.time()
        if _cached_token and now < _token_expiry - 60:
            return _cached_token
        if _cached_auth_error and now < _auth_retry_after:
            raise AppInsightsError(_cached_auth_error, auth_required=_cached_auth_required)
        try:
            token, expires_on = _request_existing_token()
        except Exception as exc:
            _cached_auth_required = isinstance(exc, AppInsightsError) and is_auth_error(exc)
            _cached_auth_error = (
                str(exc) if isinstance(exc, AppInsightsError) else
                'Unable to acquire an App Insights token from existing credentials.'
            )
            _auth_retry_after = time.time() + 30
            logger.warning(
                'Silent App Insights authentication failed: %s',
                type(exc.__cause__ or exc).__name__,
            )
            raise AppInsightsError(
                _cached_auth_error, auth_required=_cached_auth_required,
            ) from exc

        _cached_token = token
        _token_expiry = expires_on
        _cached_auth_error = None
        _cached_auth_required = False
        _auth_retry_after = 0
        return _cached_token


def reset_token_cache() -> None:
    """Drop the in-process token and authentication failure cache.

    Call this after the user has done a fresh ``az login`` so the next
    request acquires a token from the new disk cache instead of reusing
    the stale in-memory one.
    """
    global _cached_token, _token_expiry, _cached_auth_error, _auth_retry_after
    global _cached_auth_required
    with _token_lock:
        _cached_token = None
        _token_expiry = 0
        _cached_auth_error = None
        _cached_auth_required = False
        _auth_retry_after = 0


def is_auth_error(exc: 'AppInsightsError') -> bool:
    """True if the error looks like 'user is not signed in to BlaineCorp'.

    Used by the metrics UI to decide whether to surface the
    "sign in to BlaineCorp" button. We treat any 401/403 as an auth
    issue. Credential reads can explicitly distinguish missing sign-ins from
    timeouts and other acquisition errors.
    """
    if exc.auth_required is not None:
        return exc.auth_required
    if exc.status_code in (401, 403):
        return True
    msg = str(exc).lower()
    return (
        'failed to acquire' in msg
        or 'invalid_grant' in msg
        or 'interaction_required' in msg
        or 'aadsts' in msg
    )


def query(
    kql: str,
    timespan: str = 'P7D',
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a KQL query against App Insights and return the raw response.

    Args:
        kql: The KQL query string.
        timespan: ISO 8601 duration (e.g. ``"P1D"``, ``"P7D"``, ``"P30D"``).
        timeout_seconds: HTTP timeout.

    Returns:
        A dict with at least ``columns`` (list of ``{name, type}``) and
        ``rows`` (list of lists). The first table from the API response
        is unwrapped for callers who only care about the primary result.

    Raises:
        AppInsightsError: On token acquisition, network, or HTTP errors.
    """
    token = _get_token()
    url = f'{_API_BASE}/{APP_ID}/query'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    body = {'query': kql, 'timespan': timespan}

    try:
        resp = _get_session().post(
            url, headers=headers, json=body, timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise AppInsightsError(
            f'App Insights request failed: {exc}'
        ) from exc

    if resp.status_code != 200:
        body_text = (resp.text or '')[:500]
        raise AppInsightsError(
            f'App Insights HTTP {resp.status_code}: {body_text}',
            status_code=resp.status_code,
        )

    payload = resp.json() or {}
    if payload.get('error'):
        error = payload['error']
        message = error.get('message', 'Query failed') if isinstance(error, dict) else str(error)
        raise AppInsightsError(f'App Insights query failed: {message}')
    tables = payload.get('tables') or []
    if not tables:
        return {'columns': [], 'rows': []}

    table = tables[0]
    return {
        'columns': table.get('columns') or [],
        'rows': table.get('rows') or [],
    }


def query_to_dicts(
    kql: str,
    timespan: str = 'P7D',
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Run a query and return rows as a list of dicts keyed by column name."""
    result = query(kql, timespan=timespan, timeout_seconds=timeout_seconds)
    columns = [c.get('name') for c in result['columns']]
    return [dict(zip(columns, row)) for row in result['rows']]
