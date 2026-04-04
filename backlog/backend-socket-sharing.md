# Backend Socket.IO Connection for Always-On Sharing

## Overview

Move the Socket.IO connection from the browser (client-side JS) to the Flask backend so that Sales Buddy instances are "online" whenever the server is running, not just when a browser tab is open. This enables fire-and-forget sharing: a user can initiate a share, close their browser, and the data transfers automatically when the recipient comes online and accepts.

## Current Architecture (Browser-Only)

```
Browser A ──Socket.IO──→ Gateway ←──Socket.IO── Browser B
Browser A ──HTTP──→ Flask A              Flask B ←──HTTP── Browser B
```

- Socket.IO lives in `static/js/share.js` (IIFE module)
- Connection drops on page navigation or browser close
- Share state (pending type, partner ID) lives in JS closure — lost on navigation
- Flask backend has no awareness of the Socket.IO connection

## Proposed Architecture (Backend Connection)

```
Flask A ──Socket.IO──→ Gateway ←──Socket.IO── Flask B
Browser A ──HTTP──→ Flask A              Flask B ──HTTP──→ Browser B
```

- Flask backend maintains a persistent Socket.IO connection via `python-socketio` client
- Browser still handles UI (initiate share, accept/decline) via local HTTP calls to Flask
- Flask relays events to/from the gateway on behalf of the browser
- Share state lives in Python process memory — survives browser navigation

## Key Constraints

- **No data stored on gateway** — data passes through as relay only, nothing persisted. This is a policy requirement.
- **Single connection per Flask instance** — only one Socket.IO client per running server, regardless of how many browser tabs are open
- **Token refresh** — `az login` JWT expires (~1 hour). Must disconnect/reconnect to re-authenticate. If user hasn't authed, defer connection until first successful `az login`.

## Implementation Plan

### 1. Add `python-socketio` client to Flask app

```
pip install "python-socketio[client]>=5.10"
```

New file: `app/services/sharing_client.py`

```python
import socketio
import threading

sio = socketio.Client(reconnection=True, reconnection_delay=5)
_connected = False
_lock = threading.Lock()

def connect_to_gateway():
    """Connect to gateway sharing hub. Call after successful az login."""
    from app.services.partner_sharing import get_share_gateway_url, get_share_token
    token = get_share_token()
    if not token:
        return False
    url = get_share_gateway_url()
    sio.connect(url, namespaces=['/share'], auth={'token': token})
    return True

def disconnect():
    if sio.connected:
        sio.disconnect()

def is_online():
    return sio.connected
```

### 2. Background thread for Socket.IO event loop

Start the Socket.IO client in a daemon thread during Flask app initialization (after first successful auth):

```python
# In app/__init__.py or triggered after successful auth
def start_sharing_client(app):
    def _run():
        with app.app_context():
            from app.services.sharing_client import connect_to_gateway
            connect_to_gateway()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
```

### 3. Event handlers on the Flask-side client

Register handlers on the `sio` client for incoming events:

```python
@sio.on('share_offer', namespace='/share')
def on_share_offer(data):
    """Store incoming offer for the browser to pick up."""
    # Queue the offer in memory (or a simple list)
    # Browser polls or uses SSE to get pending offers

@sio.on('share_accepted', namespace='/share')
def on_share_accepted(data):
    """Recipient accepted — serialize and send partner data."""
    # Access Flask app context for DB queries
    # Serialize partners and emit share_data to gateway

@sio.on('share_payload', namespace='/share')
def on_share_payload(data):
    """Incoming partner data — upsert into local DB."""
    # Run upsert_partners() with app context
    # Notify browser of completion (SSE or polling flag)
```

### 4. Browser ↔ Flask communication

Replace direct Socket.IO in the browser with HTTP calls to local Flask endpoints:

| Browser Action | Current (Socket.IO) | New (HTTP to Flask) |
|---|---|---|
| See online users | `socket.on('online_users')` | `GET /api/share/online` |
| Send share request | `socket.emit('share_request')` | `POST /api/share/request` |
| Accept share | `socket.emit('share_accept')` | `POST /api/share/accept` |
| Decline share | `socket.emit('share_decline')` | `POST /api/share/decline` |
| Get pending offers | (toast from socket event) | `GET /api/share/offers` (polling or SSE) |
| Share completed | (toast from socket event) | `GET /api/share/status` (polling or SSE) |

### 5. Token refresh strategy

```python
# Periodic check in the background thread
import time

def _token_refresh_loop():
    while True:
        time.sleep(3000)  # Check every 50 minutes
        if sio.connected:
            token = get_share_token()
            if token:
                sio.disconnect()
                sio.connect(url, namespaces=['/share'], auth={'token': token})
```

Alternatively, the gateway could emit a `token_expiring` event before disconnecting.

### 6. Gateway changes

Minimal — the gateway `sharing_hub.py` doesn't care whether the client is a browser or a Python process. The protocol is the same. The only change might be adding a `client_type` field to `_online_users` so the gateway knows it's a persistent backend connection vs. a browser tab.

## Migration Path

1. Keep the current browser-only implementation working
2. Add the Flask Socket.IO client alongside it
3. Migrate share flow from browser→gateway to browser→Flask→gateway
4. Remove Socket.IO CDN and `share.js` Socket.IO code (keep the UI parts, just change the transport)
5. The `share.js` becomes a thin HTTP client instead of a Socket.IO client

## Testing Considerations

- Mock `socketio.Client` in unit tests — never connect to real gateway
- Test event handlers independently with fake event data
- Integration test: two Flask test servers with mocked gateway
- Token expiry: test reconnect behavior when token refresh fails

## Effort Estimate

- `sharing_client.py` + event handlers: ~100 lines
- New/modified Flask API endpoints: ~80 lines
- Modify `share.js` to use HTTP instead of Socket.IO: ~60 lines changed
- Token refresh loop: ~20 lines
- Tests: ~100 lines
- Total: ~360 lines of code, plus removing ~150 lines of browser Socket.IO code

## Dependencies

- `python-socketio[client]>=5.10` (new pip dependency for the Flask app)
- No new gateway dependencies
- No infrastructure changes needed
