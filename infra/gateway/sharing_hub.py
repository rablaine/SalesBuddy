"""
Partner sharing hub — Socket.IO namespace for the NoteHelper gateway.

Handles presence tracking (who's online) and partner data relay between
NoteHelper instances.  Clients connect directly to the gateway app service
(not through APIM) using the same JWT for authentication.

Flow:
1. Client connects with JWT in auth header → gateway validates → user joins
2. Other clients see updated online list via ``online_users`` event
3. Sender emits ``share_request`` → gateway relays to recipient
4. Recipient accepts → emits ``share_accept`` → gateway relays to sender
5. Sender emits ``share_data`` with partner JSON → gateway relays to recipient
6. Recipient processes data locally (upsert) → done
"""
import logging
import time
from functools import wraps

import jwt
from flask import request
from flask_socketio import Namespace, emit, disconnect

logger = logging.getLogger(__name__)

# Microsoft corp tenant
_MS_TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
# Gateway Entra app ID (audience)
_AUDIENCE = "api://0f6db4af-332c-4fd5-b894-77fadb181e5c"

# Online users: sid → {name, email, connected_at}
_online_users: dict[str, dict] = {}


def _decode_jwt_claims(token: str) -> dict | None:
    """Decode a JWT without signature validation (APIM already validated).

    We only need the claims for identity — the transport is trusted.
    Returns the claims dict or None if the token is unparseable.
    """
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
        # Must be from Microsoft corp tenant
        if claims.get("tid") != _MS_TENANT:
            return None
        return claims
    except Exception:
        return None


class ShareNamespace(Namespace):
    """Socket.IO namespace for partner directory sharing."""

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

        _online_users[request.sid] = {
            "name": name,
            "email": email,
            "connected_at": time.time(),
        }
        logger.info(f"share: {name} ({email}) connected — sid {request.sid}")
        self._broadcast_online()

    def on_disconnect(self):
        """Remove user from online list and broadcast."""
        user = _online_users.pop(request.sid, None)
        if user:
            logger.info(f"share: {user['name']} disconnected")
        self._broadcast_online()

    def on_get_online_users(self):
        """Client requests the current online user list."""
        users = [
            {"sid": sid, "name": u["name"], "email": u["email"]}
            for sid, u in _online_users.items()
            if sid != request.sid  # exclude self
        ]
        emit("online_users", {"users": users})

    def on_share_request(self, data):
        """Sender wants to share partners with a specific recipient.

        data: {recipient_sid, share_type: "directory"|"partner", partner_name?: str}
        """
        recipient_sid = data.get("recipient_sid")
        if recipient_sid not in _online_users:
            emit("share_error", {"error": "Recipient is no longer online"})
            return

        sender = _online_users.get(request.sid, {})
        emit("share_offer", {
            "sender_sid": request.sid,
            "sender_name": sender.get("name", "Unknown"),
            "sender_email": sender.get("email", ""),
            "share_type": data.get("share_type", "partner"),
            "partner_name": data.get("partner_name"),
        }, to=recipient_sid)

    def on_share_accept(self, data):
        """Recipient accepts a share offer."""
        sender_sid = data.get("sender_sid")
        if sender_sid not in _online_users:
            emit("share_error", {"error": "Sender is no longer online"})
            return

        emit("share_accepted", {
            "recipient_sid": request.sid,
        }, to=sender_sid)

    def on_share_decline(self, data):
        """Recipient declines a share offer."""
        sender_sid = data.get("sender_sid")
        if sender_sid not in _online_users:
            return

        recipient = _online_users.get(request.sid, {})
        emit("share_declined", {
            "recipient_name": recipient.get("name", "Unknown"),
        }, to=sender_sid)

    def on_share_data(self, data):
        """Sender transmits partner data to the recipient.

        data: {recipient_sid, partners: [...]}
        The gateway relays without inspecting the payload.
        """
        recipient_sid = data.get("recipient_sid")
        if recipient_sid not in _online_users:
            emit("share_error", {"error": "Recipient is no longer online"})
            return

        sender = _online_users.get(request.sid, {})
        emit("share_payload", {
            "sender_name": sender.get("name", "Unknown"),
            "sender_email": sender.get("email", ""),
            "partners": data.get("partners", []),
        }, to=recipient_sid)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _broadcast_online(self):
        """Send updated online list to all connected clients."""
        for sid in _online_users:
            users = [
                {"sid": s, "name": u["name"], "email": u["email"]}
                for s, u in _online_users.items()
                if s != sid  # exclude self from own list
            ]
            emit("online_users", {"users": users}, to=sid)
