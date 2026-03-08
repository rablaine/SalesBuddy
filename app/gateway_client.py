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

# Microsoft corporate tenant — must match the APIM JWT policy
_REQUIRED_TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"

# Cached credential + token
_credential = None
_cached_token: str | None = None
_token_expiry: float = 0


def is_gateway_enabled() -> bool:
    """Return True — the AI gateway is always available."""
    return True


def clear_token_cache() -> None:
    """Clear the cached credential and token.

    Called after ``az login`` completes so the new consent is picked up.
    """
    global _credential, _cached_token, _token_expiry
    _credential = None
    _cached_token = None
    _token_expiry = 0


def _get_token() -> str:
    """Acquire a JWT for the gateway audience. Caches until near expiry.

    Raises:
        GatewayConsentError: If the user hasn't consented to the gateway app.
        GatewayError: On other authentication failures.
    """
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
        except Exception as exc:
            if _is_consent_error(exc):
                raise GatewayConsentError(
                    "AI gateway consent required. Please sign out and sign "
                    "back in via the Admin Panel to grant permission."
                ) from exc
            _credential = DefaultAzureCredential()

    try:
        token_obj = _credential.get_token(f"{_GATEWAY_APP_ID}/.default")
    except Exception as exc:
        if _is_consent_error(exc):
            raise GatewayConsentError(
                "AI gateway consent required. Please sign out and sign "
                "back in via the Admin Panel to grant permission."
            ) from exc
        raise GatewayError(f"Failed to acquire gateway token: {exc}") from exc

    _cached_token = token_obj.token
    _token_expiry = token_obj.expires_on

    # Verify the token comes from the expected Microsoft tenant
    _verify_tenant(_cached_token)

    return _cached_token


def _verify_tenant(token: str) -> None:
    """Decode the JWT and verify the ``tid`` claim matches Microsoft corp.

    Raises:
        GatewayConsentError: If the user is signed in with a non-Microsoft account.
    """
    import base64
    import json as _json

    try:
        # JWT is header.payload.signature — we only need the payload
        payload_b64 = token.split(".")[1]
        # Add padding if needed
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        tid = payload.get("tid", "")
    except Exception:
        # If we can't decode, let APIM decide
        return

    if tid and tid != _REQUIRED_TENANT_ID:
        # Clear the cached token so the next attempt can try again
        global _cached_token, _token_expiry, _credential
        _cached_token = None
        _token_expiry = 0
        _credential = None
        raise GatewayConsentError(
            "You are signed in with a non-Microsoft account. "
            "Please sign out and sign back in with your "
            "Microsoft corporate (@microsoft.com) account."
        )


def _is_consent_error(exc: Exception) -> bool:
    """Return True if the exception indicates a missing consent."""
    msg = str(exc).lower()
    return "consent_required" in msg or "aadsts65001" in msg


def check_ai_consent() -> dict[str, Any]:
    """Check whether the current user has consented to the AI gateway.

    Returns:
        Dict with ``status`` ('ok', 'needs_relogin', or 'error'),
        ``consented`` (bool), ``error`` (str or None),
        and ``needs_relogin`` (bool).
    """
    try:
        _get_token()
        return {"status": "ok", "consented": True, "error": None, "needs_relogin": False}
    except GatewayConsentError as exc:
        return {
            "status": "needs_relogin",
            "consented": False,
            "error": str(exc),
            "needs_relogin": True,
        }
    except Exception as exc:
        return {"status": "error", "consented": False, "error": str(exc), "needs_relogin": False}


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


class GatewayConsentError(GatewayError):
    """Raised when the user hasn't consented to the AI gateway app."""
    pass
