"""
MSX (Dynamics 365) Authentication Service using Azure CLI.

This module handles authentication to Microsoft Sales Experience (MSX) CRM
using az login tokens. It provides:
- Token acquisition via `az account get-access-token`
- Device code flow for browser-based authentication
- Token caching to avoid repeated CLI calls
- Background refresh to keep tokens fresh
- Status checking for the admin panel

Usage:
    from app.services.msx_auth import get_msx_token, get_msx_auth_status, start_device_code_flow

Prerequisites:
    - Azure CLI installed and in PATH
"""

import subprocess
import json
import threading
import time
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# CRM constants
CRM_RESOURCE = "https://microsoftsales.crm.dynamics.com"
CRM_BASE_URL = "https://microsoftsales.crm.dynamics.com/api/data/v9.2"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"  # Microsoft corporate tenant

# On Windows, we need shell=True for subprocess to find az in PATH
IS_WINDOWS = sys.platform == "win32"

# Cache for az CLI installed check - once confirmed installed, don't recheck
_az_cli_installed_cache: Dict[str, Any] = {
    "installed": None,  # None = unknown, True = confirmed installed, False = confirmed not found
    "last_error": None,  # Stores error type for transient failures
}

# Token cache
_token_cache: Dict[str, Any] = {
    "access_token": None,
    "expires_on": None,
    "user": None,
    "last_refresh": None,
    "error": None,
}

# Lock to prevent concurrent az CLI token refresh calls
_token_lock = threading.Lock()

# Device code flow state
_device_code_state: Dict[str, Any] = {
    "active": False,
    "process": None,
    "user_code": None,
    "verification_uri": None,
    "message": None,
    "started_at": None,
    "completed": False,
    "success": False,
    "error": None,
}

# Refresh job control
_refresh_thread: Optional[threading.Thread] = None
_refresh_running = False

# VPN / IP-blocked state
_vpn_state: Dict[str, Any] = {
    "blocked": False,
    "blocked_at": None,
    "last_check": None,
    "error_message": None,
}


def is_vpn_blocked() -> bool:
    """Check if MSX is currently blocked due to VPN/IP issues."""
    return _vpn_state["blocked"]


def get_vpn_state() -> Dict[str, Any]:
    """Get current VPN blocked state for UI display."""
    return dict(_vpn_state)


def set_vpn_blocked(error_message: str = "") -> None:
    """Mark MSX as blocked due to VPN/IP issues. Clears the token cache."""
    global _vpn_state
    _vpn_state["blocked"] = True
    _vpn_state["blocked_at"] = datetime.now(timezone.utc)
    _vpn_state["error_message"] = error_message or "IP address blocked by MSX"
    logger.warning(f"MSX VPN block detected: {error_message}")
    clear_token_cache()


def clear_vpn_block() -> None:
    """Clear the VPN blocked state (e.g. after successful reconnection)."""
    global _vpn_state
    _vpn_state = {
        "blocked": False,
        "blocked_at": None,
        "last_check": None,
        "error_message": None,
    }
    logger.info("MSX VPN block cleared")


def check_vpn_recovery() -> Dict[str, Any]:
    """Try to reach MSX and clear VPN block if successful.
    
    Returns:
        Dict with success, message.
    """
    global _vpn_state
    _vpn_state["last_check"] = datetime.now(timezone.utc)
    
    # Import here to avoid circular import
    from app.services.msx_api import test_connection
    result = test_connection()
    
    if result.get("success"):
        clear_vpn_block()
        return {"success": True, "message": "VPN connection restored! MSX is accessible."}
    
    error = result.get("error", "")
    if "IP address" in error or "0x80095ffe" in error:
        return {"success": False, "vpn_blocked": True, "message": "Still blocked. Check your VPN connection."}
    
    return {"success": False, "message": f"MSX check failed: {error}"}


def _run_az_command() -> Dict[str, Any]:
    """
    Run az account get-access-token to get a fresh CRM token.
    
    Returns:
        Dict with accessToken, expiresOn, and other az CLI output fields.
        
    Raises:
        RuntimeError: If az CLI fails or is not installed.
    """
    # Build command - on Windows with shell=True, we need a string
    if IS_WINDOWS:
        cmd = f'az account get-access-token --resource "{CRM_RESOURCE}" --tenant "{TENANT_ID}" --output json'
    else:
        cmd = [
            "az", "account", "get-access-token",
            "--resource", CRM_RESOURCE,
            "--tenant", TENANT_ID,
            "--output", "json"
        ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            shell=IS_WINDOWS  # Required on Windows to find az in PATH
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error from az CLI"
            # Common errors
            if "AADSTS" in error_msg:
                raise RuntimeError(f"Azure AD error: {error_msg}")
            if "az login" in error_msg.lower() or "please run" in error_msg.lower():
                raise RuntimeError("Not logged in. Run 'az login' in a terminal first.")
            if "not recognized" in error_msg.lower() or "not found" in error_msg.lower():
                raise RuntimeError("Azure CLI not installed or not in PATH.")
            raise RuntimeError(f"az CLI error: {error_msg}")
        
        return json.loads(result.stdout)
        
    except subprocess.TimeoutExpired:
        raise RuntimeError("az CLI timed out after 30 seconds")
    except FileNotFoundError:
        raise RuntimeError("Azure CLI not installed or not in PATH. Install from https://aka.ms/installazurecli")
    except json.JSONDecodeError:
        raise RuntimeError("Invalid JSON response from az CLI")


def _parse_expiry(expires_on: str) -> datetime:
    """Parse the expiresOn field from az CLI output."""
    # az CLI returns ISO format like "2024-01-15 10:30:00.000000"
    try:
        # Try parsing with microseconds
        return datetime.fromisoformat(expires_on.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        # Try without microseconds
        try:
            return datetime.strptime(expires_on, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            # Fallback: assume it expires in 1 hour
            return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def refresh_token() -> bool:
    """
    Refresh the MSX token by calling az CLI.
    
    Returns:
        True if refresh succeeded, False otherwise.
    
    Note:
        Acquires _token_lock to prevent concurrent az CLI subprocess calls.
    """
    global _token_cache

    with _token_lock:
        # Double-check: another thread may have refreshed while we waited
        if _token_cache["access_token"]:
            now = datetime.now(timezone.utc)
            expires_on = _token_cache["expires_on"]
            if expires_on and (expires_on - now).total_seconds() > 300:
                return True

        try:
            result = _run_az_command()

            # Use expires_on (Unix timestamp) if available, otherwise parse expiresOn string
            expires_on_unix = result.get("expires_on")
            if expires_on_unix:
                expires_on = datetime.fromtimestamp(expires_on_unix, tz=timezone.utc)
            else:
                expires_on = _parse_expiry(result.get("expiresOn", ""))

            _token_cache = {
                "access_token": result.get("accessToken"),
                "expires_on": expires_on,
                "user": result.get("subscription", "Unknown"),
                "last_refresh": datetime.now(timezone.utc),
                "error": None,
            }

            logger.info(f"MSX token refreshed, expires at {_token_cache['expires_on']}")
            return True

        except RuntimeError as e:
            _token_cache["error"] = str(e)
            _token_cache["last_refresh"] = datetime.now(timezone.utc)
            logger.warning(f"MSX token refresh failed: {e}")
            return False


def get_msx_token() -> Optional[str]:
    """
    Get a valid MSX access token.
    
    Returns:
        The access token string, or None if not authenticated.
        
    Note:
        This will attempt to refresh if the token is expired or missing.
        Token refresh is serialized via _token_lock in refresh_token().
    """
    global _token_cache

    # Check if we have a valid cached token
    if _token_cache["access_token"]:
        now = datetime.now(timezone.utc)
        expires_on = _token_cache["expires_on"]

        # If token has > 5 minutes remaining, use it
        if expires_on and (expires_on - now).total_seconds() > 300:
            return _token_cache["access_token"]

    # Need to refresh (refresh_token holds _token_lock internally)
    if refresh_token():
        return _token_cache["access_token"]

    return None


def get_msx_auth_status() -> Dict[str, Any]:
    """
    Get current MSX authentication status for displaying in the UI.
    
    Returns:
        Dict with:
        - authenticated: bool
        - user: str or None
        - expires_on: datetime or None
        - expires_in_minutes: int or None
        - last_refresh: datetime or None
        - error: str or None
        - refresh_job_running: bool
    """
    global _token_cache, _refresh_running
    
    now = datetime.now(timezone.utc)
    expires_on = _token_cache.get("expires_on")
    
    status = {
        "authenticated": False,
        "user": _token_cache.get("user"),
        "expires_on": expires_on,
        "expires_in_minutes": None,
        "last_refresh": _token_cache.get("last_refresh"),
        "error": _token_cache.get("error"),
        "refresh_job_running": _refresh_running,
        "vpn_blocked": _vpn_state["blocked"],
    }
    
    if _token_cache.get("access_token") and expires_on:
        remaining = (expires_on - now).total_seconds()
        if remaining > 0:
            status["authenticated"] = True
            status["expires_in_minutes"] = int(remaining / 60)
    
    return status


def start_token_refresh_job(interval_seconds: int = 300):
    """
    Start a background thread that refreshes the MSX token periodically.
    
    Args:
        interval_seconds: How often to check/refresh (default 5 minutes).
                         Token will only be refreshed if < 10 minutes remaining.
    """
    global _refresh_thread, _refresh_running
    
    if _refresh_running:
        logger.info("MSX token refresh job already running")
        return
    
    def _refresh_loop():
        global _refresh_running
        _refresh_running = True
        logger.info(f"MSX token refresh job started (interval: {interval_seconds}s)")
        
        while _refresh_running:
            try:
                # Skip refresh if VPN is blocked — no point refreshing a token
                # that will just get rejected by the IP firewall
                if _vpn_state["blocked"]:
                    logger.debug("Skipping token refresh — VPN blocked")
                else:
                    # Check if token needs refresh (< 10 minutes remaining)
                    expires_on = _token_cache.get("expires_on")
                    if expires_on:
                        now = datetime.now(timezone.utc)
                        remaining = (expires_on - now).total_seconds()
                        
                        if remaining < 600:  # Less than 10 minutes
                            logger.info("MSX token expiring soon, refreshing...")
                            refresh_token()
                    # If no token cached, don't auto-acquire. The user must
                    # explicitly sign in via the wizard first. Once they do,
                    # expires_on gets set and the background job keeps it alive.
                    
            except Exception as e:
                logger.error(f"Error in MSX token refresh job: {e}")
            
            # Sleep in small increments so we can stop quickly
            for _ in range(interval_seconds):
                if not _refresh_running:
                    break
                time.sleep(1)
        
        logger.info("MSX token refresh job stopped")
    
    _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
    _refresh_thread.start()


def stop_token_refresh_job():
    """Stop the background token refresh job."""
    global _refresh_running
    _refresh_running = False
    logger.info("MSX token refresh job stop requested")


def clear_token_cache():
    """Clear the cached token (forces re-authentication on next request)."""
    global _token_cache
    _token_cache = {
        "access_token": None,
        "expires_on": None,
        "user": None,
        "last_refresh": None,
        "error": None,
    }
    logger.info("MSX token cache cleared")


# ---------------------------------------------------------------------------
# Azure CLI status & browser-based az login flow
# ---------------------------------------------------------------------------

# Subscription ID to set after login
SUBSCRIPTION_ID = "0832b3b6-22b3-4c47-8d8b-572054b97257"

# Login process tracking
_az_login_state: Dict[str, Any] = {
    "active": False,
    "process": None,
    "started_at": None,
}


def check_az_cli_installed() -> tuple[bool, str | None]:
    """Check if Azure CLI is installed and in PATH.

    Uses ``az --version`` but checks stdout for 'azure-cli' rather than
    relying solely on the exit code, since ``az --version`` can return
    non-zero when extensions have warnings or updates are available.

    Results are cached: once we confirm CLI is installed, we don't recheck.
    This prevents false negatives when system is waking from sleep/lock.

    Returns:
        Tuple of (is_installed, error_type).
        error_type is None if installed, or one of:
        - "not_found": FileNotFoundError - CLI truly not installed
        - "timeout": subprocess timed out (system busy)
        - "check_failed": other subprocess error
    """
    global _az_cli_installed_cache

    # If we've previously confirmed it's installed, return cached result
    if _az_cli_installed_cache["installed"] is True:
        return (True, None)

    try:
        result = subprocess.run(
            "az --version" if IS_WINDOWS else ["az", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=IS_WINDOWS,
        )
        # Check returncode first, but also accept non-zero if stdout
        # contains version info (az --version sometimes exits non-zero
        # due to extension warnings or update notices).
        if result.returncode == 0 or "azure-cli" in (result.stdout or "").lower():
            _az_cli_installed_cache["installed"] = True
            _az_cli_installed_cache["last_error"] = None
            return (True, None)
        # Got output but no azure-cli string - weird state
        _az_cli_installed_cache["last_error"] = "check_failed"
        return (False, "check_failed")
    except FileNotFoundError:
        # Definitely not installed
        _az_cli_installed_cache["installed"] = False
        _az_cli_installed_cache["last_error"] = "not_found"
        return (False, "not_found")
    except subprocess.TimeoutExpired:
        # System might be busy (waking from sleep, etc.)
        _az_cli_installed_cache["last_error"] = "timeout"
        return (False, "timeout")
    except subprocess.SubprocessError:
        # Other subprocess error
        _az_cli_installed_cache["last_error"] = "check_failed"
        return (False, "check_failed")


def check_az_logged_in() -> tuple[bool, Optional[str], Optional[str]]:
    """Check if user is logged in to Azure CLI.

    Returns:
        Tuple of (is_logged_in, user_email, tenant_id).
    """
    try:
        cmd = "az account show --output json" if IS_WINDOWS else [
            "az", "account", "show", "--output", "json"
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            shell=IS_WINDOWS,
        )
        if result.returncode == 0:
            account = json.loads(result.stdout)
            user = account.get("user", {})
            tenant_id = account.get("tenantId")
            return True, user.get("name"), tenant_id
        return False, None, None
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return False, None, None


def get_az_cli_status() -> Dict[str, Any]:
    """Get current Azure CLI authentication status (no CRM token needed).

    Checks:
    1. Whether Azure CLI is installed
    2. Whether user is logged in (via ``az account show``)
    3. Whether the tenant matches the expected Microsoft tenant

    Returns:
        Dict with az_installed, logged_in, wrong_tenant, user_email, message, cli_error.
    """
    installed, error_type = check_az_cli_installed()
    if not installed:
        if error_type == "not_found":
            message = "Azure CLI not installed"
        elif error_type == "timeout":
            message = "Azure CLI check timed out (system may still be waking up)"
        else:
            message = "Azure CLI check failed"
        return {
            "az_installed": False,
            "logged_in": False,
            "wrong_tenant": False,
            "user_email": None,
            "message": message,
            "cli_error": error_type,
        }

    logged_in, user_email, tenant_id = check_az_logged_in()

    wrong_tenant = False
    if logged_in and tenant_id and tenant_id != TENANT_ID:
        wrong_tenant = True

    if wrong_tenant:
        message = (f"Signed in as {user_email} but on the wrong tenant. "
                   "Please sign in with your Microsoft corporate account.")
    elif logged_in:
        message = f"Logged in as {user_email}"
    else:
        message = "Not logged in"

    return {
        "az_installed": True,
        "logged_in": logged_in,
        "wrong_tenant": wrong_tenant,
        "user_email": user_email,
        "message": message,
        "cli_error": None,
    }


def az_logout() -> Dict[str, Any]:
    """Clear ALL Azure CLI cached accounts and tokens.

    Uses ``az account clear`` instead of ``az logout`` so that non-default
    accounts (e.g. a personal tenant) don't survive and interfere with
    the next ``az login --tenant``.

    Returns:
        Dict with success, message.
    """
    try:
        cmd = "az account clear" if IS_WINDOWS else ["az", "account", "clear"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            shell=IS_WINDOWS,
        )
        if result.returncode == 0:
            logger.info("Azure CLI accounts cleared")
            return {"success": True, "message": "All accounts cleared"}
        logger.warning(f"az account clear failed: {result.stderr}")
        return {"success": True, "message": "Logout completed"}
    except Exception as e:
        logger.warning(f"Error during az account clear: {e}")
        return {"success": False, "error": str(e)}


def kill_az_login_process() -> None:
    """Kill any running ``az login`` process spawned by :func:`start_az_login`.

    Called after we've confirmed the token is valid and set the subscription,
    because ``az login`` keeps running indefinitely waiting for the user to
    pick a subscription in the console window.  We don't need it anymore.
    """
    global _az_login_state
    proc = _az_login_state.get("process")
    if proc is not None:
        try:
            proc.kill()
            logger.info("Killed lingering az login process (pid=%s)", proc.pid)
        except OSError:
            pass  # already dead
    _az_login_state = {"active": False, "process": None, "started_at": None}


def start_az_login(scope: str | None = None) -> Dict[str, Any]:
    """Launch ``az login --tenant <TENANT>`` in a visible console window.

    The ``--tenant`` flag scopes the browser auth to the Microsoft
    corporate tenant.  If the user picks a non-Microsoft account the
    browser will reject it.  The frontend polls ``az account show`` and
    uses a short timeout to catch any failures.

    Args:
        scope: Optional OAuth scope to include in the login command.
            When provided (e.g. ``https://management.azure.com/.default``),
            ``az login`` will also request that scope.

    Returns:
        Dict with success, message, error.
    """
    global _az_login_state

    # Kill any lingering az login process from a previous attempt
    prev = _az_login_state.get("process")
    if prev is not None:
        try:
            prev.kill()
            logger.info("Killed previous az login process (pid=%s)", prev.pid)
        except OSError:
            pass  # already dead

    try:
        # Login with optional scope.
        cmd = f'az login --tenant {TENANT_ID}'
        args = ["az", "login", "--tenant", TENANT_ID]
        if scope:
            cmd += f' --scope {scope}'
            args.extend(["--scope", scope])

        if IS_WINDOWS:
            import subprocess as _sp
            process = _sp.Popen(
                cmd,
                shell=True,
                creationflags=_sp.CREATE_NEW_CONSOLE,
            )
        else:
            # On Linux/Mac, launch in background
            process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        _az_login_state = {
            "active": True,
            "process": process,
            "started_at": time.time(),
        }

        logger.info("Launched az login in console window")
        return {
            "success": True,
            "message": "Browser will open. Complete sign-in to continue.",
        }

    except FileNotFoundError:
        return {
            "success": False,
            "error": "Azure CLI not installed. Install from https://aka.ms/installazurecli",
        }
    except Exception as e:
        logger.exception("Failed to launch az login")
        return {"success": False, "error": str(e)}


def get_az_login_process_status() -> Dict[str, Any]:
    """Check the status of the launched az login process.

    Uses ``process.poll()`` which is instant (no subprocess calls).

    Returns:
        Dict with:
        - active: bool (was a login launched)
        - running: bool (process still running)
        - exit_code: int or None
        - elapsed_seconds: float
    """
    global _az_login_state

    if not _az_login_state.get("active") or not _az_login_state.get("process"):
        return {"active": False, "running": False, "exit_code": None, "elapsed_seconds": 0}

    process = _az_login_state["process"]
    started_at = _az_login_state.get("started_at") or time.time()
    elapsed = time.time() - started_at
    exit_code = process.poll()  # None if still running, int if exited

    return {
        "active": True,
        "running": exit_code is None,
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed, 1),
    }


def set_subscription() -> bool:
    """Set the Azure subscription after successful login."""
    try:
        cmd = (
            f"az account set -s {SUBSCRIPTION_ID}"
            if IS_WINDOWS
            else ["az", "account", "set", "-s", SUBSCRIPTION_ID]
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            shell=IS_WINDOWS,
        )
        if result.returncode == 0:
            logger.info(f"Subscription set to {SUBSCRIPTION_ID}")
            return True
        logger.warning(f"Failed to set subscription: {result.stderr}")
        return False
    except Exception as e:
        logger.warning(f"Error setting subscription: {e}")
        return False


def start_device_code_flow() -> Dict[str, Any]:
    """
    Start the Azure CLI device code login flow.
    
    This runs `az login --use-device-code` which outputs a message like:
    "To sign in, use a web browser to open the page https://microsoft.com/devicelogin 
    and enter the code ABCD1234 to authenticate."
    
    Returns:
        Dict with:
        - success: bool
        - user_code: str (the code to enter)
        - verification_uri: str (the URL to visit)
        - message: str (full message from az login)
        - error: str if failed
    """
    global _device_code_state
    
    # Check if already running
    if _device_code_state.get("active") and _device_code_state.get("process"):
        proc = _device_code_state["process"]
        if proc.poll() is None:  # Still running
            return {
                "success": True,
                "already_active": True,
                "user_code": _device_code_state.get("user_code"),
                "verification_uri": _device_code_state.get("verification_uri"),
                "message": _device_code_state.get("message"),
            }
    
    # Reset state
    _device_code_state = {
        "active": True,
        "process": None,
        "user_code": None,
        "verification_uri": None,
        "message": None,
        "started_at": datetime.now(timezone.utc),
        "completed": False,
        "success": False,
        "error": None,
    }
    
    cmd = [
        "az", "login",
        "--use-device-code",
        "--tenant", TENANT_ID,
        "--allow-no-subscriptions",
        "--output", "json"
    ]
    
    try:
        # Start the process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=IS_WINDOWS  # Required on Windows to find az in PATH
        )
        _device_code_state["process"] = process
        
        # Read stderr to get the device code message (az login outputs to stderr)
        # We need to read until we get the device code, but not block forever
        import select
        import sys
        
        message_lines = []
        start_time = time.time()
        
        # On Windows, select doesn't work with pipes, so we use a thread
        def read_stderr():
            for line in process.stderr:
                message_lines.append(line)
                # Stop once we've likely got the device code message
                if "devicelogin" in line.lower() or "code" in line.lower():
                    break
        
        reader_thread = threading.Thread(target=read_stderr, daemon=True)
        reader_thread.start()
        reader_thread.join(timeout=10)  # Wait up to 10s for device code
        
        full_message = "".join(message_lines).strip()
        
        if not full_message:
            _device_code_state["active"] = False
            _device_code_state["error"] = "No output from az login. Is Azure CLI installed?"
            return {"success": False, "error": _device_code_state["error"]}
        
        # Parse the message to extract the code and URL
        # Format: "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD1234 to authenticate."
        code_match = re.search(r'enter the code\s+([A-Z0-9]+)', full_message, re.IGNORECASE)
        url_match = re.search(r'(https://[^\s]+devicelogin[^\s]*)', full_message, re.IGNORECASE)
        
        user_code = code_match.group(1) if code_match else None
        verification_uri = url_match.group(1) if url_match else "https://microsoft.com/devicelogin"
        
        _device_code_state["user_code"] = user_code
        _device_code_state["verification_uri"] = verification_uri
        _device_code_state["message"] = full_message
        
        # Start a background thread to wait for completion
        def wait_for_completion():
            global _device_code_state, _token_cache
            try:
                stdout, stderr = process.communicate(timeout=300)  # 5 minute timeout
                
                if process.returncode == 0:
                    _device_code_state["completed"] = True
                    _device_code_state["success"] = True
                    _device_code_state["active"] = False
                    logger.info("Device code flow completed successfully")
                    
                    # Now get the CRM token
                    refresh_token()
                else:
                    _device_code_state["completed"] = True
                    _device_code_state["success"] = False
                    _device_code_state["error"] = stderr or "Login failed"
                    _device_code_state["active"] = False
                    logger.warning(f"Device code flow failed: {stderr}")
                    
            except subprocess.TimeoutExpired:
                process.kill()
                _device_code_state["completed"] = True
                _device_code_state["success"] = False
                _device_code_state["error"] = "Login timed out (5 minutes)"
                _device_code_state["active"] = False
                logger.warning("Device code flow timed out")
            except Exception as e:
                _device_code_state["completed"] = True
                _device_code_state["success"] = False
                _device_code_state["error"] = str(e)
                _device_code_state["active"] = False
                logger.exception("Device code flow error")
        
        completion_thread = threading.Thread(target=wait_for_completion, daemon=True)
        completion_thread.start()
        
        return {
            "success": True,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "message": full_message,
        }
        
    except FileNotFoundError:
        _device_code_state["active"] = False
        _device_code_state["error"] = "Azure CLI not installed"
        return {"success": False, "error": "Azure CLI not installed. Install from https://aka.ms/installazurecli"}
    except Exception as e:
        _device_code_state["active"] = False
        _device_code_state["error"] = str(e)
        logger.exception("Failed to start device code flow")
        return {"success": False, "error": str(e)}


def get_device_code_status() -> Dict[str, Any]:
    """
    Check the status of an active device code flow.
    
    Returns:
        Dict with:
        - active: bool (is a flow in progress)
        - completed: bool
        - success: bool (if completed)
        - user_code: str
        - verification_uri: str
        - error: str if failed
    """
    global _device_code_state
    
    return {
        "active": _device_code_state.get("active", False),
        "completed": _device_code_state.get("completed", False),
        "success": _device_code_state.get("success", False),
        "user_code": _device_code_state.get("user_code"),
        "verification_uri": _device_code_state.get("verification_uri"),
        "message": _device_code_state.get("message"),
        "error": _device_code_state.get("error"),
        "started_at": _device_code_state.get("started_at").isoformat() if _device_code_state.get("started_at") else None,
    }


def cancel_device_code_flow():
    """Cancel any active device code flow."""
    global _device_code_state
    
    if _device_code_state.get("process"):
        try:
            _device_code_state["process"].kill()
        except Exception:
            pass
    
    _device_code_state = {
        "active": False,
        "process": None,
        "user_code": None,
        "verification_uri": None,
        "message": None,
        "started_at": None,
        "completed": False,
        "success": False,
        "error": None,
    }
    logger.info("Device code flow cancelled")
