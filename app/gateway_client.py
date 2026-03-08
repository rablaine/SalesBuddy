"""
Gateway client for calling the NoteHelper AI Gateway via APIM.

All AI calls are routed through the centralized APIM → App Service →
Azure OpenAI pipeline.  The gateway URL is hardcoded — no configuration
needed.  Authentication uses the caller's ``az login`` credential to
obtain a JWT scoped to the gateway's Entra app registration.

Usage::

    from app.gateway_client import gateway_call

    result = gateway_call("/v1/suggest-topics", {"call_notes": "..."})
    # result == {"success": True, "topics": [...], "usage": {...}}
"""
import logging
from typing import Any

import requests
from azure.identity import DefaultAzureCredential, AzureCliCredential

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Hardcoded APIM gateway URL — no env-var configuration needed
_GATEWAY_URL = "https://apim-notehelper.azure-api.net/ai"

# Entra app registration client ID — the audience for JWT tokens
_GATEWAY_APP_ID = "api://0f6db4af-332c-4fd5-b894-77fadb181e5c"

# Cached credential + token
_credential = None
_cached_token: str | None = None
_token_expiry: float = 0


def is_gateway_enabled() -> bool:
    """Return True — the AI gateway is always available."""
    return True


def _get_token() -> str:
    """Acquire a JWT for the gateway audience. Caches until near expiry."""
    import time

    global _credential, _cached_token, _token_expiry

    now = time.time()
    if _cached_token and now < _token_expiry - 60:
        return _cached_token

    if _credential is None:
        # Prefer AzureCliCredential for local dev (faster, no prompts)
        try:
            cred = AzureCliCredential()
            cred.get_token(f"{_GATEWAY_APP_ID}/.default")
            _credential = cred
        except Exception:
            _credential = DefaultAzureCredential()

    token_obj = _credential.get_token(f"{_GATEWAY_APP_ID}/.default")
    _cached_token = token_obj.token
    _token_expiry = token_obj.expires_on
    return _cached_token


def gateway_call(
    endpoint: str,
    payload: dict[str, Any],
    timeout: int = 120,
) -> dict[str, Any]:
    """Call a gateway endpoint through APIM.

    Args:
        endpoint: Path like ``/v1/suggest-topics``.
        payload: JSON body to POST.
        timeout: HTTP timeout in seconds (AI calls can be slow).

    Returns:
        Parsed JSON response dict from the gateway.

    Raises:
        GatewayError: On HTTP errors or connection failures.
    """
    url = f"{_GATEWAY_URL}{endpoint}"
    token = _get_token()

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GatewayError(f"Gateway request failed: {exc}") from exc

    if resp.status_code == 401:
        # Token may have expired between cache check and request — retry once
        global _cached_token
        _cached_token = None
        token = _get_token()
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise GatewayError(f"Gateway request failed: {exc}") from exc

    if resp.status_code == 429:
        raise GatewayError("Rate limit exceeded — try again later")

    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get("error") or body.get("statusReason") or resp.text[:200]
        except Exception:
            msg = resp.text[:200]
        raise GatewayError(f"Gateway returned {resp.status_code}: {msg}")

    return resp.json()


class GatewayError(Exception):
    """Raised when a gateway call fails."""
    pass
