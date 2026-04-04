"""Sharing hub -- Socket.IO namespace for the NoteHelper gateway.

Handles presence tracking (who's online) and data relay between
NoteHelper instances.  Supports partner sharing and note sharing.
Clients connect directly to the gateway app service (not through APIM)
using the same JWT for authentication.

Flow:
1. Client connects with JWT in auth header → gateway validates → user joins
2. Other clients see updated online list via ``online_users`` event
3. Sender emits ``share_request`` → gateway relays to recipient
4. Recipient accepts → emits ``share_accept`` → gateway relays to sender
5. Sender emits ``share_data`` with payload → gateway relays to recipient
6. Recipient processes data locally (upsert) → done
"""
import logging
import os
import threading
import time

import jwt
import requests
from jwt import PyJWKClient
from flask import request
from flask_socketio import Namespace, emit, disconnect

logger = logging.getLogger(__name__)

# Microsoft corp tenant
_MS_TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
# Token audience - Azure Management plane (no app registration needed)
_AUDIENCE = "https://management.azure.com"
# Microsoft OIDC JWKS endpoint for key rotation (v1 - matches management tokens)
_JWKS_URL = f"https://login.microsoftonline.com/{_MS_TENANT}/discovery/keys"

# Online users: sid → {name, email, connected_at}
_online_users: dict[str, dict] = {}

# Allow users to see/share with themselves (for staging/dev testing).
# Set ALLOW_SELF_SHARE=true on the staging slot's app settings.
_ALLOW_SELF_SHARE = os.environ.get("ALLOW_SELF_SHARE", "").strip().lower() == "true"

# Pending share sessions: (sender_email, recipient_email) → {sender_sid, recipient_sid}
# Gateway tracks both sides so the client never needs to handle SIDs.
_pending_shares: dict[tuple[str, str], dict] = {}

# Sharing allowlist — if set, only these emails can connect.
# Env var: comma-separated emails. Empty/unset = everyone allowed.
_ALLOWED_EMAILS: set[str] = set(
    e.strip().lower()
    for e in os.environ.get("SHARE_ALLOWED_EMAILS", "").split(",")
    if e.strip()
)

# JWKS client — caches signing keys, thread-safe
_jwks_client: PyJWKClient | None = None
_jwks_lock = threading.Lock()


def _get_jwks_client() -> PyJWKClient:
    """Lazily initialize and cache the JWKS client."""
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                _jwks_client = PyJWKClient(_JWKS_URL, cache_keys=True)
    return _jwks_client


def _decode_jwt_claims(token: str) -> dict | None:
    """Decode and verify a JWT using Microsoft's JWKS signing keys.

    Validates:
    - Signature (RSA, from Microsoft's published JWKS keys)
    - Audience (must match our Entra app registration)
    - Expiration (must not be expired)
    - Issuer (must be from Microsoft corp tenant)
    - Tenant ID (must match _MS_TENANT)

    Returns the claims dict or None if validation fails.
    """
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_AUDIENCE,
            issuer=f"https://sts.windows.net/{_MS_TENANT}/",
        )
        # Belt-and-suspenders: verify tenant claim matches
        if claims.get("tid") != _MS_TENANT:
            return None
        return claims
    except Exception as e:
        logger.warning(f"share: JWT validation failed — {e}")
        return None


class ShareNamespace(Namespace):
    """Socket.IO namespace for sharing (partners, notes, etc.)."""

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _sids_for_email(email: str) -> list[str]:
        """Return all SIDs belonging to a given email address."""
        return [
            sid for sid, u in _online_users.items()
            if u["email"].lower() == email.lower()
        ]

    @staticmethod
    def _emit_to_user(event: str, data: dict, email: str):
        """Emit an event to ALL SIDs for a given email."""
        for sid in ShareNamespace._sids_for_email(email):
            emit(event, data, to=sid)

    @staticmethod
    def _unique_online_users(exclude_email: str) -> list[dict]:
        """Build a deduplicated online user list, excluding a given email.

        When ALLOW_SELF_SHARE is true (staging/dev), the requesting user
        is included in the list so they can test sharing with themselves.
        """
        seen = set()
        users = []
        skip_self = not _ALLOW_SELF_SHARE
        for u in _online_users.values():
            email_lower = u["email"].lower()
            if skip_self and email_lower == exclude_email.lower():
                continue
            if email_lower in seen:
                continue
            seen.add(email_lower)
            users.append({"name": u["name"], "email": u["email"]})
        return users

    def _broadcast_online(self):
        """Send updated online list to all connected clients."""
        for sid, me in _online_users.items():
            users = self._unique_online_users(me["email"])
            emit("online_users", {"users": users}, to=sid)

    # ── Connection lifecycle ─────────────────────────────────────────────

    def on_connect(self, auth=None):
        """Authenticate the user via JWT and track them as online."""
        token = None
        if auth and isinstance(auth, dict):
            token = auth.get("token")

        if not token:
            logger.warning("share: connection rejected — no token")
            disconnect()
            return False

        claims = _decode_jwt_claims(token)
        if not claims:
            logger.warning("share: connection rejected — invalid token")
            disconnect()
            return False

        name = claims.get("name", "Unknown")
        email = (
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("email")
            or "unknown"
        )

        # Allowlist check (if configured)
        if _ALLOWED_EMAILS and email.lower() not in _ALLOWED_EMAILS:
            logger.info(f"share: {email} not in allowlist — rejecting")
            emit("not_allowed", {})
            disconnect()
            return False

        _online_users[request.sid] = {
            "name": name,
            "email": email,
            "connected_at": time.time(),
        }
        logger.info(f"share: {name} ({email}) connected — sid {request.sid}")
        logger.info(f"share: online_users keys = {list(_online_users.keys())}")
        self._broadcast_online()

    def on_disconnect(self):
        """Remove user from online list and broadcast."""
        user = _online_users.pop(request.sid, None)
        if user:
            logger.info(f"share: {user['name']} disconnected — sid {request.sid}")
            logger.info(f"share: online_users keys after disconnect = {list(_online_users.keys())}")
        self._broadcast_online()

    def on_get_online_users(self):
        """Client requests the current online user list."""
        my_email = _online_users.get(request.sid, {}).get("email", "")
        users = self._unique_online_users(my_email)
        emit("online_users", {"users": users})

    # ── Share flow (all email-based) ─────────────────────────────────────

    def on_share_request(self, data):
        """Sender wants to share with a specific recipient.

        data: {recipient_email, share_type: "directory"|"partner"|"note",
               item_name?: str}
        """
        recipient_email = data.get("recipient_email", "")
        recipient_sids = self._sids_for_email(recipient_email)
        if not recipient_sids:
            emit("share_error", {"error": "Recipient is no longer online"})
            return

        sender = _online_users.get(request.sid, {})
        sender_email = sender.get("email", "")

        # Store sender's SID — recipient_sid filled in when they accept
        key = (sender_email.lower(), recipient_email.lower())
        _pending_shares[key] = {"sender_sid": request.sid}
        logger.info(f"share: request stored — key={key}, sender_sid={request.sid}")

        # Notify ALL of recipient's tabs (any tab can accept)
        self._emit_to_user("share_offer", {
            "sender_email": sender_email,
            "sender_name": sender.get("name", "Unknown"),
            "share_type": data.get("share_type", "partner"),
            "item_name": data.get("item_name"),
        }, recipient_email)

    def on_share_accept(self, data):
        """Recipient accepts a share offer."""
        sender_email = data.get("sender_email", "")
        recipient = _online_users.get(request.sid, {})
        recipient_email = recipient.get("email", "")

        # Look up the pending share and store the accepter's SID
        key = (sender_email.lower(), recipient_email.lower())
        pending = _pending_shares.get(key)
        if not pending:
            emit("share_error", {"error": "Share session not found"})
            return

        sender_sid = pending["sender_sid"]
        if sender_sid not in _online_users:
            _pending_shares.pop(key, None)
            emit("share_error", {"error": "Sender is no longer online"})
            return

        # Store recipient's accepting SID for when sender sends data
        pending["recipient_sid"] = request.sid
        logger.info(f"share: accept — key={key}, sender_sid={sender_sid}, "
                    f"recipient_sid={request.sid}")

        # Notify ONLY the sender's originating tab (no SIDs in payload)
        emit("share_accepted", {
            "recipient_email": recipient_email,
            "recipient_name": recipient.get("name", "Unknown"),
        }, to=sender_sid)

        # Dismiss the offer on other recipient tabs
        for sid in self._sids_for_email(recipient_email):
            if sid != request.sid:
                emit("share_offer_handled", {}, to=sid)

    def on_share_decline(self, data):
        """Recipient declines a share offer."""
        sender_email = data.get("sender_email", "")
        recipient = _online_users.get(request.sid, {})
        recipient_email = recipient.get("email", "")

        # Look up and clean up the pending share
        key = (sender_email.lower(), recipient_email.lower())
        pending = _pending_shares.pop(key, None)

        if pending:
            sender_sid = pending["sender_sid"]
            if sender_sid in _online_users:
                emit("share_declined", {
                    "recipient_name": recipient.get("name", "Unknown"),
                }, to=sender_sid)

        # Dismiss the offer on other recipient tabs
        for sid in self._sids_for_email(recipient_email):
            if sid != request.sid:
                emit("share_offer_handled", {}, to=sid)

    def on_share_data(self, data):
        """Sender transmits share payload to the recipient.

        data: {recipient_email, share_type, ...payload fields}
        The gateway looks up the recipient SID from the pending share
        session — the client never handles SIDs.
        """
        sender = _online_users.get(request.sid, {})
        sender_email = sender.get("email", "")
        recipient_email = data.get("recipient_email", "")

        # Look up the pending share to find the recipient's accepting SID
        key = (sender_email.lower(), recipient_email.lower())
        pending = _pending_shares.pop(key, None)
        logger.info(f"share: share_data — key={key}, pending={pending}, "
                    f"online={list(_online_users.keys())}")

        if not pending or "recipient_sid" not in pending:
            emit("share_error", {"error": "Share session expired or not accepted"})
            return

        recipient_sid = pending["recipient_sid"]
        if recipient_sid not in _online_users:
            emit("share_error", {"error": "Recipient is no longer online"})
            return

        # Forward everything except routing field, add sender info
        payload = {k: v for k, v in data.items() if k != "recipient_email"}
        payload["sender_name"] = sender.get("name", "Unknown")
        payload["sender_email"] = sender_email

        # Send ONLY to the tab that accepted
        emit("share_payload", payload, to=recipient_sid)
        logger.info(f"share: payload delivered to {recipient_sid}")
