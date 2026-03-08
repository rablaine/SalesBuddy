"""
Gateway client for calling the NoteHelper AI Gateway via APIM.

All AI calls are routed through the centralized APIM → App Service →
Azure OpenAI pipeline.  The gateway URL is hardcoded — no configuration
needed.  Authentication uses an APIM **subscription key** which is
fetched automatically via the Azure Management API using the caller's
existing ``az login`` credential.

Usage::

    from app.gateway_client import gateway_call

    result = gateway_call("/v1/suggest-topics", {"call_notes": "..."})
    # result == {"success": True, "topics": [...], "usage": {...}}
"""
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (hardcoded — zero env-var config needed)
# ---------------------------------------------------------------------------
_GATEWAY_URL = "https://apim-notehelper.azure-api.net/ai"

# Azure resource coordinates for fetching the subscription key
_AZURE_SUBSCRIPTION_ID = "97aba503-25b6-46a2-8ed2-a8afc3bfbd23"
_RESOURCE_GROUP = "NoteHelper_Resources"
_APIM_SERVICE_NAME = "apim-notehelper"
_APIM_SUBSCRIPTION_NAME = "notehelper-client"

# Azure Management API endpoint for listing subscription secrets
_LIST_SECRETS_URL = (
    f"https://management.azure.com/subscriptions/{_AZURE_SUBSCRIPTION_ID}"
    f"/resourceGroups/{_RESOURCE_GROUP}"
    f"/providers/Microsoft.ApiManagement/service/{_APIM_SERVICE_NAME}"
    f"/subscriptions/{_APIM_SUBSCRIPTION_NAME}"
    f"/listSecrets?api-version=2022-08-01"
)

# Cached subscription key
_cached_sub_key: str | None = None
_key_fetched_at: float = 0
# Re-fetch the key every 30 minutes (keys rarely rotate, but just in case)
_KEY_TTL_SECONDS = 1800


def is_gateway_enabled() -> bool:
    """Return True — the AI gateway is always available."""
    return True


def clear_key_cache() -> None:
    """Clear the cached subscription key.

    Called after ``az login`` completes so a fresh key can be fetched.
    """
    global _cached_sub_key, _key_fetched_at
    _cached_sub_key = None
    _key_fetched_at = 0


def _get_subscription_key() -> str:
    """Fetch the APIM subscription key via the Azure Management API.

    Uses ``AzureCliCredential`` — the same ``az login`` session already
    needed for MSX.  The key is cached for ``_KEY_TTL_SECONDS``.

    Raises:
        GatewayAuthError: If the user isn't logged in via ``az login``
            or lacks permission to read the APIM subscription.
        GatewayError: On unexpected failures.
    """
    global _cached_sub_key, _key_fetched_at

    now = time.time()
    if _cached_sub_key and (now - _key_fetched_at) < _KEY_TTL_SECONDS:
        return _cached_sub_key

    try:
        from azure.identity import AzureCliCredential
        cred = AzureCliCredential()
        token = cred.get_token("https://management.azure.com/.default")
    except Exception as exc:
        raise GatewayAuthError(
            "Not signed in to Azure CLI. Please sign in via the Admin Panel "
            "(MSX Integration → Sign In) to enable AI features."
        ) from exc

    try:
        resp = requests.post(
            _LIST_SECRETS_URL,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise GatewayError(f"Failed to reach Azure Management API: {exc}") from exc

    if resp.status_code == 401 or resp.status_code == 403:
        raise GatewayAuthError(
            "Azure CLI session lacks permission to read APIM subscription keys. "
            "Please sign out and sign back in via the Admin Panel."
        )

    if resp.status_code >= 400:
        raise GatewayError(
            f"Failed to fetch APIM subscription key (HTTP {resp.status_code}): "
            f"{resp.text[:200]}"
        )

    data = resp.json()
    key = data.get("primaryKey")
    if not key:
        raise GatewayError("APIM subscription key response missing primaryKey")

    _cached_sub_key = key
    _key_fetched_at = now
    return key


def check_gateway_auth() -> dict[str, Any]:
    """Check whether the APIM subscription key can be fetched.

    Returns:
        Dict with ``status`` ('ok' or 'error'), ``authenticated`` (bool),
        and ``error`` (str or None).
    """
    try:
        _get_subscription_key()
        return {"status": "ok", "authenticated": True, "error": None}
    except GatewayAuthError as exc:
        return {"status": "error", "authenticated": False, "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "authenticated": False, "error": str(exc)}


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
        GatewayAuthError: If not signed in or key cannot be fetched.
    """
    url = f"{_GATEWAY_URL}{endpoint}"
    sub_key = _get_subscription_key()

    headers = {
        "Ocp-Apim-Subscription-Key": sub_key,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise GatewayError(f"Gateway request failed: {exc}") from exc

    if resp.status_code == 401:
        # Key may have been rotated — clear cache and retry once
        clear_key_cache()
        sub_key = _get_subscription_key()
        headers["Ocp-Apim-Subscription-Key"] = sub_key
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
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


class GatewayAuthError(GatewayError):
    """Raised when the user isn't signed in or can't authenticate to APIM."""
    pass
