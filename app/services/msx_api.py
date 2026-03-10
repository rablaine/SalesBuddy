"""
MSX (Dynamics 365) API Client.

This module provides functions to call the MSX CRM API for:
- Account lookup by TPID
- Connection testing (WhoAmI)

Uses msx_auth for token management.
"""

import requests
import logging
from datetime import datetime as dt, timezone as tz
from typing import Optional, Dict, Any, List, Callable

from app.services.msx_auth import (
    get_msx_token, refresh_token, CRM_BASE_URL,
    is_vpn_blocked, set_vpn_blocked, clear_vpn_block,
)

logger = logging.getLogger(__name__)

# Error code / message patterns for IP-blocked responses
IP_BLOCKED_CODE = "0x80095ffe"
IP_BLOCKED_MESSAGE = "IP address is blocked"

# MSX app ID for account URLs
MSX_APP_ID = "fe0c3504-3700-e911-a849-000d3a10b7cc"

# Request timeout (seconds per attempt)
REQUEST_TIMEOUT = 45

# Retry settings for transient failures (timeouts, connection errors)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [1, 3, 5]  # Wait between retries

# Standard headers for OData requests
def _get_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Prefer": "odata.include-annotations=\"*\"",
    }


def _msx_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict] = None,
    retry_on_auth_failure: bool = True
) -> requests.Response:
    """
    Make an MSX API request with automatic retries.
    
    Retries on:
    - 401/403: Forces token refresh and retries once
    - Timeout/ConnectionError: Retries up to MAX_RETRIES times with backoff
    
    Args:
        method: HTTP method ('GET', 'POST', 'PATCH')
        url: Full URL to request
        headers: Request headers (if None, will get fresh token and build headers)
        json_data: JSON body for POST/PATCH requests
        retry_on_auth_failure: Whether to auto-retry on 401/403 (default True)
        
    Returns:
        requests.Response object
        
    Raises:
        requests.exceptions.Timeout if all retries exhausted
        requests.exceptions.ConnectionError if all retries exhausted
    """
    import time
    
    # Get token and build headers if not provided
    if headers is None:
        token = get_msx_token()
        if not token:
            # Create a fake response for "not authenticated"
            response = requests.models.Response()
            response.status_code = 401
            response._content = b'{"error": "Not authenticated"}'
            return response
        headers = _get_headers(token)
    
    def _do_request(hdrs):
        """Execute the HTTP request with the given headers."""
        if method.upper() == 'GET':
            return requests.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT)
        elif method.upper() == 'POST':
            return requests.post(url, headers=hdrs, json=json_data, timeout=REQUEST_TIMEOUT)
        elif method.upper() == 'PATCH':
            return requests.patch(url, headers=hdrs, json=json_data, timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    
    def _is_ip_blocked(resp):
        """Check if a response indicates IP-based blocking (off-VPN)."""
        if resp.status_code != 403:
            return False
        try:
            body = resp.text
            return IP_BLOCKED_CODE in body or IP_BLOCKED_MESSAGE in body
        except Exception:
            return False
    
    # Retry loop for transient failures (timeouts, connection errors)
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            response = _do_request(headers)
            last_exception = None
            break  # Success — got a response (even if it's an error status)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    f"MSX request {method} attempt {attempt + 1}/{MAX_RETRIES} failed "
                    f"({type(e).__name__}), retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"MSX request {method} failed after {MAX_RETRIES} attempts: {e}"
                )
                raise
    
    # Check for auth failures that might be due to stale token
    # Skip token-refresh retry if it's an IP block — fresh token won't help
    if response.status_code in (401, 403) and retry_on_auth_failure and not _is_ip_blocked(response):
        logger.info(f"Got {response.status_code} from MSX, forcing token refresh and retrying...")
        
        # Force token refresh
        refresh_success = refresh_token()
        if not refresh_success:
            logger.warning("Token refresh failed, returning original error response")
            return response
        
        # Get fresh token and rebuild headers
        fresh_token = get_msx_token()
        if not fresh_token:
            logger.warning("No token after refresh, returning original error response")
            return response
        
        fresh_headers = _get_headers(fresh_token)
        
        # Retry the request with fresh headers (also with timeout retry)
        logger.info("Retrying MSX request with fresh token...")
        for attempt in range(MAX_RETRIES):
            try:
                response = _do_request(fresh_headers)
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                    logger.warning(
                        f"MSX retry (fresh token) attempt {attempt + 1}/{MAX_RETRIES} failed "
                        f"({type(e).__name__}), retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"MSX retry (fresh token) failed after {MAX_RETRIES} attempts: {e}")
                    raise
        
        if response.status_code in (401, 403):
            logger.warning(f"Still got {response.status_code} after token refresh - likely a real permission issue")
    
    # --- VPN / IP-blocked detection ---
    if _is_ip_blocked(response):
        set_vpn_blocked("MSX rejected request — IP address not on corpnet/VPN.")
        return response
    
    # If we got a successful response and VPN was previously blocked, clear it
    if response.ok and is_vpn_blocked():
        logger.info("MSX request succeeded — clearing VPN block state")
        clear_vpn_block()
    
    return response


def test_connection() -> Dict[str, Any]:
    """
    Test the MSX connection by calling WhoAmI.
    
    Returns:
        Dict with:
        - success: bool
        - user_id: str (GUID) if successful
        - error: str if failed
    """
    try:
        response = _msx_request('GET', f"{CRM_BASE_URL}/WhoAmI")
        
        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "user_id": data.get("UserId"),
                "business_unit_id": data.get("BusinessUnitId"),
                "organization_id": data.get("OrganizationId"),
            }
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        else:
            result: Dict[str, Any] = {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }
            if is_vpn_blocked():
                result["error"] = "IP address is blocked - connect to VPN and retry."
                result["vpn_blocked"] = True
            return result
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _normalize_name(name: str) -> str:
    """Normalize company name for comparison."""
    if not name:
        return ""
    # Lowercase, remove common suffixes, extra whitespace
    normalized = name.lower().strip()
    for suffix in [" inc", " inc.", " llc", " llc.", " corp", " corp.", " co", " co.", " ltd", " ltd."]:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)].strip()
    return normalized


def _names_similar(name1: str, name2: str) -> bool:
    """
    Check if two company names are similar enough to be confident they match.
    Returns True if names are similar, False if they're too different.
    """
    n1 = _normalize_name(name1)
    n2 = _normalize_name(name2)
    
    if not n1 or not n2:
        return False
    
    # Exact match after normalization
    if n1 == n2:
        return True
    
    # One contains the other (handles "ABC" vs "ABC Company")
    if n1 in n2 or n2 in n1:
        return True
    
    # Check if first word matches (handles "Acme Corp" vs "Acme Industries")
    words1 = n1.split()
    words2 = n2.split()
    if words1 and words2 and words1[0] == words2[0] and len(words1[0]) >= 4:
        return True
    
    return False


def lookup_account_by_tpid(tpid: str, customer_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Look up an MSX account by TPID (msp_mstopparentid).
    
    Args:
        tpid: The Top Parent ID to search for.
        customer_name: Optional customer name to match against (for better auto-selection).
        
    Returns:
        Dict with:
        - success: bool
        - accounts: List of matching accounts (each with accountid, name, msp_mstopparentid)
        - url: Direct MSX URL if exactly one match
        - error: str if failed
    """
    # Sanitize TPID - should be numeric
    tpid_clean = str(tpid).strip()
    
    try:
        # Build OData query - include parenting level to identify "Top" parent
        url = (
            f"{CRM_BASE_URL}/accounts"
            f"?$filter=msp_mstopparentid eq '{tpid_clean}'"
            f"&$select=accountid,name,msp_mstopparentid,msp_parentinglevelcode"
        )
        
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            data = response.json()
            raw_accounts = data.get("value", [])
            
            # Process accounts - extract parenting level from OData annotations
            accounts = []
            top_accounts = []  # Track ALL top-level accounts
            name_match = None  # Account matching customer name
            normalized_customer = _normalize_name(customer_name) if customer_name else None
            
            for raw in raw_accounts:
                account = {
                    "accountid": raw.get("accountid"),
                    "name": raw.get("name"),
                    "msp_mstopparentid": raw.get("msp_mstopparentid"),
                    "parenting_level": raw.get(
                        "msp_parentinglevelcode@OData.Community.Display.V1.FormattedValue",
                        raw.get("msp_parentinglevelcode", "Unknown")
                    ),
                    "url": build_account_url(raw.get("accountid")),
                }
                accounts.append(account)
                
                # Track all "Top" parent accounts
                if account["parenting_level"] and "top" in str(account["parenting_level"]).lower():
                    top_accounts.append(account)
                
                # Check for name match (if customer_name provided)
                if normalized_customer and not name_match:
                    normalized_account = _normalize_name(account["name"])
                    if normalized_account == normalized_customer:
                        name_match = account
            
            result = {
                "success": True,
                "accounts": accounts,
                "count": len(accounts),
            }
            
            # Selection priority:
            # 1. If customer name matches an account, use that (most confident)
            # 2. If only one account AND names are similar, use it
            # 3. If exactly ONE Top parent AND names similar, use it
            # 4. Otherwise, don't auto-select (let user choose)
            
            if name_match:
                # Found matching customer name - most confident
                result["url"] = name_match["url"]
                result["account_name"] = name_match["name"]
                result["name_match"] = True
            elif len(accounts) == 1:
                # Single account - check if name is similar before auto-selecting
                if customer_name and _names_similar(customer_name, accounts[0]["name"]):
                    result["url"] = accounts[0]["url"]
                    result["account_name"] = accounts[0]["name"]
                elif not customer_name:
                    # No customer name provided - can't verify, still auto-select
                    result["url"] = accounts[0]["url"]
                    result["account_name"] = accounts[0]["name"]
                else:
                    # Name mismatch warning - don't auto-select
                    result["name_mismatch"] = True
                    result["msx_account_name"] = accounts[0]["name"]
            elif len(top_accounts) == 1:
                # Single Top parent - check name similarity
                if customer_name and _names_similar(customer_name, top_accounts[0]["name"]):
                    result["url"] = top_accounts[0]["url"]
                    result["account_name"] = top_accounts[0]["name"]
                    result["top_parent"] = True
                elif not customer_name:
                    result["url"] = top_accounts[0]["url"]
                    result["account_name"] = top_accounts[0]["name"]
                    result["top_parent"] = True
                else:
                    # Name mismatch warning
                    result["name_mismatch"] = True
                    result["msx_account_name"] = top_accounts[0]["name"]
                    result["multiple_tops"] = 0  # Flag to show options
            elif len(top_accounts) > 1:
                # Exactly one Top parent - safe to auto-select
                result["url"] = top_accounts[0]["url"]
                result["account_name"] = top_accounts[0]["name"]
                result["top_parent"] = True
            elif len(top_accounts) > 1:
                # Multiple Top parents - show them for selection
                result["multiple_tops"] = len(top_accounts)
            
            return result
            
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        elif response.status_code == 403:
            if is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
            return {"success": False, "error": "Access denied. You may not have permission to query accounts."}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error looking up TPID {tpid}")
        return {"success": False, "error": str(e)}


def build_account_url(account_id: str) -> str:
    """
    Build a direct MSX URL for an account.
    
    Args:
        account_id: The account GUID.
        
    Returns:
        Full MSX URL to open the account record.
    """
    return (
        f"https://microsoftsales.crm.dynamics.com/main.aspx"
        f"?appid={MSX_APP_ID}"
        f"&pagetype=entityrecord"
        f"&etn=account"
        f"&id={account_id}"
    )


def build_milestone_url(milestone_id: str) -> str:
    """
    Build a direct MSX URL for a milestone.
    
    Args:
        milestone_id: The milestone GUID (msp_engagementmilestoneid).
        
    Returns:
        Full MSX URL to open the milestone record.
    """
    return (
        f"https://microsoftsales.crm.dynamics.com/main.aspx"
        f"?appid={MSX_APP_ID}"
        f"&pagetype=entityrecord"
        f"&etn=msp_engagementmilestone"
        f"&id={milestone_id}"
    )


def build_task_url(task_id: str) -> str:
    """
    Build a direct MSX URL for a task.
    
    Args:
        task_id: The task GUID (activityid).
        
    Returns:
        Full MSX URL to open the task record.
    """
    return (
        f"https://microsoftsales.crm.dynamics.com/main.aspx"
        f"?appid={MSX_APP_ID}"
        f"&pagetype=entityrecord"
        f"&etn=task"
        f"&id={task_id}"
    )


# Milestone status sort order (lower = more important in UI)
MILESTONE_STATUS_ORDER = {
    'On Track': 1,
    'At Risk': 2,
    'Blocked': 3,
    'Completed': 4,
    'Cancelled': 5,
    'Lost to Competitor': 6,
    'Hygiene/Duplicate': 7,
}

# HOK task categories (eligible for hands-on-keyboard credit)
HOK_TASK_CATEGORIES = {
    861980004,  # Architecture Design Session
    861980006,  # Blocker Escalation
    861980008,  # Briefing
    861980007,  # Consumption Plan
    861980002,  # Demo
    861980005,  # PoC/Pilot
    606820005,  # Technical Close/Win Plan
    861980001,  # Workshop
}

# All task categories
TASK_CATEGORIES = [
    # HOK categories (sorted first)
    {"label": "Architecture Design Session", "value": 861980004, "is_hok": True},
    {"label": "Blocker Escalation", "value": 861980006, "is_hok": True},
    {"label": "Briefing", "value": 861980008, "is_hok": True},
    {"label": "Consumption Plan", "value": 861980007, "is_hok": True},
    {"label": "Demo", "value": 861980002, "is_hok": True},
    {"label": "PoC/Pilot", "value": 861980005, "is_hok": True},
    {"label": "Technical Close/Win Plan", "value": 606820005, "is_hok": True},
    {"label": "Workshop", "value": 861980001, "is_hok": True},
    # Non-HOK categories
    {"label": "ACE", "value": 606820000, "is_hok": False},
    {"label": "Call Back Requested", "value": 861980010, "is_hok": False},
    {"label": "Cross Segment", "value": 606820001, "is_hok": False},
    {"label": "Cross Workload", "value": 606820002, "is_hok": False},
    {"label": "Customer Engagement", "value": 861980000, "is_hok": False},
    {"label": "External (Co-creation of Value)", "value": 861980013, "is_hok": False},
    {"label": "Internal", "value": 861980012, "is_hok": False},
    {"label": "Negotiate Pricing", "value": 861980003, "is_hok": False},
    {"label": "New Partner Request", "value": 861980011, "is_hok": False},
    {"label": "Post Sales", "value": 606820003, "is_hok": False},
    {"label": "RFP/RFI", "value": 861980009, "is_hok": False},
    {"label": "Tech Support", "value": 606820004, "is_hok": False},
]


def extract_account_id_from_url(tpid_url: str) -> Optional[str]:
    """
    Extract the account GUID from an MSX account URL.
    
    Args:
        tpid_url: MSX URL like https://microsoftsales.crm.dynamics.com/main.aspx?...&id={guid}
        
    Returns:
        The account GUID if found, None otherwise.
    """
    if not tpid_url:
        return None
    
    import re
    # Look for id= parameter (GUID format)
    match = re.search(r'[&?]id=([a-f0-9-]{36})', tpid_url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Also try %7B and %7D encoded braces
    match = re.search(r'[&?]id=%7B([a-f0-9-]{36})%7D', tpid_url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def get_milestones_by_account(
    account_id: str,
    active_only: bool = False,
    open_opportunities_only: bool = False,
    current_fy_only: bool = False,
) -> Dict[str, Any]:
    """
    Get all milestones for an account.
    
    Args:
        account_id: The account GUID.
        active_only: If True, only return active (uncommitted) milestones
                     (On Track, At Risk, Blocked).
        open_opportunities_only: If True, only return milestones whose parent
                                 opportunity is still Open (statecode=0).
                                 Filters out milestones on Won/Lost/Cancelled opps.
        current_fy_only: If True, only return milestones with a due date in the
                         current Microsoft fiscal year (July 1 - June 30).
        
    Returns:
        Dict with:
        - success: bool
        - milestones: List of milestone dicts with id, name, status, number, url,
          opportunity, due_date, dollar_value, workload, monthly_usage
        - error: str if failed
    """
    try:
        # Build filter - always filter by account, optionally by active status
        filters = [f"_msp_parentaccount_value eq '{account_id}'"]
        if active_only:
            # Active statuses: On Track (861980000), At Risk (861980001), Blocked (861980002)
            filters.append(
                "(msp_milestonestatus eq 861980000"
                " or msp_milestonestatus eq 861980001"
                " or msp_milestonestatus eq 861980002)"
            )
        if open_opportunities_only:
            # Only milestones on Open opportunities (statecode: 0=Open, 1=Won, 2=Lost)
            filters.append("msp_OpportunityId/statecode eq 0")
        if current_fy_only:
            # Microsoft fiscal year starts July 1. FY2026 = July 2025 - June 2026.
            now = dt.now(tz.utc)
            fy_start_year = now.year if now.month >= 7 else now.year - 1
            fy_start = f"{fy_start_year}-07-01"
            fy_end = f"{fy_start_year + 1}-06-30"
            filters.append(
                f"msp_milestonedate ge {fy_start}"
                f" and msp_milestonedate le {fy_end}"
            )
        filter_str = " and ".join(filters)
        
        # Query milestones by parent account — include due date and dollar value fields
        # Field names discovered via EntityDefinitions metadata:
        #   msp_milestonedate = "Milestone Est. Date" (DateTime)
        #   msp_bacvrate = "BACV" - Business Annualized Customer Value (Decimal)
        #   msp_monthlyuse = "Est. Change in Monthly Usage" (Money)
        url = (
            f"{CRM_BASE_URL}/msp_engagementmilestones"
            f"?$filter={filter_str}"
            f"&$select=msp_engagementmilestoneid,msp_name,msp_milestonestatus,"
            f"msp_milestonenumber,_msp_opportunityid_value,msp_monthlyuse,"
            f"_msp_workloadlkid_value,msp_milestonedate,msp_bacvrate"
            f"&$orderby=msp_name"
        )
        
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            data = response.json()
            raw_milestones = data.get("value", [])
            
            milestones = []
            for raw in raw_milestones:
                milestone_id = raw.get("msp_engagementmilestoneid")
                status = raw.get(
                    "msp_milestonestatus@OData.Community.Display.V1.FormattedValue",
                    "Unknown"
                )
                status_code = raw.get("msp_milestonestatus")
                opp_name = raw.get(
                    "_msp_opportunityid_value@OData.Community.Display.V1.FormattedValue",
                    ""
                )
                workload = raw.get(
                    "_msp_workloadlkid_value@OData.Community.Display.V1.FormattedValue",
                    ""
                )
                monthly_usage = raw.get("msp_monthlyuse")
                
                # Tracker fields (actual MSX field names from metadata)
                due_date_str = raw.get("msp_milestonedate")
                dollar_value = raw.get("msp_bacvrate")  # BACV
                
                milestones.append({
                    "id": milestone_id,
                    "name": raw.get("msp_name", ""),
                    "number": raw.get("msp_milestonenumber", ""),
                    "status": status,
                    "status_code": status_code,
                    "status_sort": MILESTONE_STATUS_ORDER.get(status, 99),
                    "msx_opportunity_id": raw.get("_msp_opportunityid_value"),
                    "opportunity_name": opp_name,
                    "workload": workload,
                    "monthly_usage": monthly_usage,
                    "due_date": due_date_str,
                    "dollar_value": dollar_value,
                    "url": build_milestone_url(milestone_id),
                })
            
            # Sort by status (active first), then by name
            milestones.sort(key=lambda m: (m["status_sort"], m["name"].lower()))
            
            return {
                "success": True,
                "milestones": milestones,
                "count": len(milestones),
            }
            
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        elif response.status_code == 403:
            if is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
            return {"success": False, "error": "Access denied. You may not have permission to query milestones."}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error getting milestones for account {account_id}")
        return {"success": False, "error": str(e)}


def build_opportunity_url(opportunity_id: str) -> str:
    """
    Build a direct MSX URL for an opportunity.
    
    Args:
        opportunity_id: The opportunity GUID.
        
    Returns:
        Full MSX URL to open the opportunity record.
    """
    return (
        f"https://microsoftsales.crm.dynamics.com/main.aspx"
        f"?appid={MSX_APP_ID}"
        f"&pagetype=entityrecord"
        f"&etn=opportunity"
        f"&id={opportunity_id}"
    )


def get_opportunity(opportunity_id: str) -> Dict[str, Any]:
    """
    Fetch a single opportunity from MSX by GUID.
    
    Fetches fresh details every time (no caching). Includes the forecast
    comments JSON field for reading/writing comments.
    
    Args:
        opportunity_id: The opportunity GUID.
        
    Returns:
        Dict with:
        - success: bool
        - opportunity: Dict with name, number, status, value, comments, etc.
        - error: str if failed
    """
    try:
        url = (
            f"{CRM_BASE_URL}/opportunities({opportunity_id})"
            f"?$select=name,msp_opportunitynumber,statecode,statuscode,"
            f"estimatedvalue,estimatedclosedate,customerneed,description,"
            f"msp_forecastcomments,msp_forecastcommentsjsonfield,"
            f"msp_forecastcomments_lastmodifiedon,msp_competethreatlevel,"
            f"_parentaccountid_value,_ownerid_value"
        )
        
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            raw = response.json()
            
            # Parse comments from JSON field
            import json as json_lib
            comments = []
            json_str = raw.get("msp_forecastcommentsjsonfield")
            if json_str:
                try:
                    comments = json_lib.loads(json_str)
                except (json_lib.JSONDecodeError, TypeError):
                    logger.warning(f"Could not parse comments JSON for opp {opportunity_id}")
            
            # Resolve userId GUIDs to display names
            if comments:
                unique_ids = set()
                for c in comments:
                    uid = c.get("userId", "").strip("{} ")
                    if uid:
                        unique_ids.add(uid)
                
                name_cache = {}
                for uid in unique_ids:
                    try:
                        user_url = f"{CRM_BASE_URL}/systemusers({uid})?$select=fullname"
                        user_resp = _msx_request('GET', user_url)
                        if user_resp.status_code == 200:
                            name_cache[uid] = user_resp.json().get("fullname", "Unknown")
                    except Exception:
                        pass
                
                for c in comments:
                    uid = c.get("userId", "").strip("{} ")
                    c["displayName"] = name_cache.get(uid, "Unknown")
            
            # Build structured response
            state_formatted = raw.get(
                "statecode@OData.Community.Display.V1.FormattedValue", "Unknown"
            )
            status_formatted = raw.get(
                "statuscode@OData.Community.Display.V1.FormattedValue", ""
            )
            owner = raw.get(
                "_ownerid_value@OData.Community.Display.V1.FormattedValue", ""
            )
            compete = raw.get(
                "msp_competethreatlevel@OData.Community.Display.V1.FormattedValue", ""
            )
            
            opportunity = {
                "id": opportunity_id,
                "name": raw.get("name", ""),
                "number": raw.get("msp_opportunitynumber", ""),
                "state": state_formatted,
                "status": status_formatted,
                "statecode": raw.get("statecode"),
                "estimated_value": raw.get("estimatedvalue"),
                "estimated_close_date": raw.get("estimatedclosedate"),
                "customer_need": raw.get("customerneed", ""),
                "description": raw.get("description", ""),
                "owner": owner,
                "compete_threat": compete,
                "comments": comments,
                "comments_plain": raw.get("msp_forecastcomments", ""),
                "comments_last_modified": raw.get("msp_forecastcomments_lastmodifiedon", ""),
                "url": build_opportunity_url(opportunity_id),
            }
            
            return {"success": True, "opportunity": opportunity}
        
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        elif response.status_code == 403:
            if is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
            return {"success": False, "error": "Access denied. You may not have permission to view this opportunity."}
        elif response.status_code == 404:
            return {"success": False, "error": "Opportunity not found in MSX."}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }
    
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error getting opportunity {opportunity_id}")
        return {"success": False, "error": str(e)}


def add_opportunity_comment(
    opportunity_id: str,
    comment_text: str,
) -> Dict[str, Any]:
    """
    Append a comment to an opportunity's forecast comments field.
    
    Reads the current comments JSON, appends the new comment, and PATCHes
    the updated JSON back. Uses the current MSX user's GUID as the author.
    
    Args:
        opportunity_id: The opportunity GUID.
        comment_text: The comment text to add.
        
    Returns:
        Dict with:
        - success: bool
        - comment_count: int (total comments after adding)
        - error: str if failed
    """
    import json as json_lib
    
    try:
        # Step 1: Get current comments
        read_url = (
            f"{CRM_BASE_URL}/opportunities({opportunity_id})"
            f"?$select=msp_forecastcommentsjsonfield"
        )
        read_response = _msx_request('GET', read_url)
        
        if read_response.status_code != 200:
            return {
                "success": False,
                "error": f"Failed to read comments: HTTP {read_response.status_code}"
            }
        
        current_json_str = read_response.json().get("msp_forecastcommentsjsonfield") or "[]"
        try:
            current_comments = json_lib.loads(current_json_str)
        except (json_lib.JSONDecodeError, TypeError):
            current_comments = []
        
        # Step 2: Get current user GUID for the comment author
        user_id = get_current_user_id()
        if not user_id:
            return {"success": False, "error": "Could not get current user ID"}
        
        # Step 3: Build new comment (matching MSX UI format)
        new_comment = {
            "userId": f"{{{user_id.upper()}}}",
            "modifiedOn": dt.now(tz.utc).strftime("%m/%d/%Y, %I:%M:%S %p"),
            "comment": comment_text,
        }
        current_comments.append(new_comment)
        
        # Step 4: PATCH back to MSX
        patch_url = f"{CRM_BASE_URL}/opportunities({opportunity_id})"
        payload = {
            "msp_forecastcommentsjsonfield": json_lib.dumps(current_comments),
        }
        
        patch_response = _msx_request('PATCH', patch_url, json_data=payload)
        
        if patch_response.status_code < 400:
            return {
                "success": True,
                "comment_count": len(current_comments),
            }
        elif patch_response.status_code == 403 and is_vpn_blocked():
            return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
        else:
            return {
                "success": False,
                "error": f"PATCH failed: HTTP {patch_response.status_code} - {patch_response.text[:200]}"
            }
    
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error adding comment to opportunity {opportunity_id}")
        return {"success": False, "error": str(e)}


# Milestone Access Team template ID (from MSX EntityDefinitions)
MILESTONE_TEAM_TEMPLATE_ID = "316e4735-9e83-eb11-a812-0022481e1be0"

# Opportunity (Deal) Team template ID (from MSX EntityDefinitions)
OPPORTUNITY_TEAM_TEMPLATE_ID = "cc923a9d-7651-e311-9405-00155db3ba1e"


def get_my_milestone_team_ids() -> Dict[str, Any]:
    """
    Get the set of milestone IDs the current user is on the access team for.

    Queries the user's access team memberships (teamtype=1) and filters to
    milestone teams by checking the team name suffix against the milestone
    team template ID.

    Returns:
        Dict with:
        - success: bool
        - milestone_ids: set of lowercase milestone GUID strings
        - team_count: int (total access teams found)
        - error: str if failed
    """
    try:
        # Get current user ID
        user_id = get_current_user_id()
        if not user_id:
            return {
                "success": False,
                "error": "Could not get current user ID",
                "milestone_ids": set(),
            }

        # Query all access teams (teamtype=1) for this user
        all_teams = []
        url = (
            f"{CRM_BASE_URL}/systemusers({user_id})/teammembership_association"
            f"?$select=_regardingobjectid_value,teamid,name,teamtype"
            f"&$filter=teamtype eq 1"
            f"&$top=5000"
        )
        response = _msx_request('GET', url)
        if response.status_code != 200:
            if response.status_code == 403 and is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True, "milestone_ids": set()}
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
                "milestone_ids": set(),
            }

        data = response.json()
        all_teams = data.get("value", [])

        # Follow pagination if needed
        next_link = data.get("@odata.nextLink")
        while next_link:
            resp = _msx_request('GET', next_link)
            if resp.status_code == 200:
                page_data = resp.json()
                all_teams.extend(page_data.get("value", []))
                next_link = page_data.get("@odata.nextLink")
            else:
                break

        # Filter to milestone teams by the template ID suffix in team name
        # Team names are formatted as: "{regardingobjectid}+{teamtemplateid}"
        milestone_ids = set()
        template_suffix = f"+{MILESTONE_TEAM_TEMPLATE_ID}"
        for team in all_teams:
            name = team.get("name", "")
            if template_suffix in name:
                regard_id = team.get("_regardingobjectid_value")
                if regard_id:
                    milestone_ids.add(regard_id.lower())

        logger.info(
            f"Found {len(milestone_ids)} milestone team memberships "
            f"out of {len(all_teams)} total access teams"
        )

        return {
            "success": True,
            "milestone_ids": milestone_ids,
            "team_count": len(all_teams),
        }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out.", "milestone_ids": set()}
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "error": f"Connection error: {str(e)[:100]}",
            "milestone_ids": set(),
        }
    except Exception as e:
        logger.exception("Error getting milestone team memberships")
        return {"success": False, "error": str(e), "milestone_ids": set()}


def get_my_deal_team_ids() -> Dict[str, Any]:
    """
    Get the set of opportunity IDs the current user is on the deal team for.

    Queries the user's access team memberships (teamtype=1) and filters to
    opportunity teams by checking the team name suffix against the opportunity
    team template ID.

    Returns:
        Dict with:
        - success: bool
        - opportunity_ids: set of lowercase opportunity GUID strings
        - error: str if failed
    """
    try:
        user_id = get_current_user_id()
        if not user_id:
            return {
                "success": False,
                "error": "Could not get current user ID",
                "opportunity_ids": set(),
            }

        all_teams = []
        url = (
            f"{CRM_BASE_URL}/systemusers({user_id})/teammembership_association"
            f"?$select=_regardingobjectid_value,teamid,name,teamtype"
            f"&$filter=teamtype eq 1"
            f"&$top=5000"
        )
        response = _msx_request('GET', url)
        if response.status_code != 200:
            if response.status_code == 403 and is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True, "opportunity_ids": set()}
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
                "opportunity_ids": set(),
            }

        data = response.json()
        all_teams = data.get("value", [])

        next_link = data.get("@odata.nextLink")
        while next_link:
            resp = _msx_request('GET', next_link)
            if resp.status_code == 200:
                page_data = resp.json()
                all_teams.extend(page_data.get("value", []))
                next_link = page_data.get("@odata.nextLink")
            else:
                break

        opportunity_ids = set()
        template_suffix = f"+{OPPORTUNITY_TEAM_TEMPLATE_ID}"
        for team in all_teams:
            name = team.get("name", "")
            if template_suffix in name:
                regard_id = team.get("_regardingobjectid_value")
                if regard_id:
                    opportunity_ids.add(regard_id.lower())

        logger.info(
            f"Found {len(opportunity_ids)} deal team memberships "
            f"out of {len(all_teams)} total access teams"
        )

        return {
            "success": True,
            "opportunity_ids": opportunity_ids,
        }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out.", "opportunity_ids": set()}
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "error": f"Connection error: {str(e)[:100]}",
            "opportunity_ids": set(),
        }
    except Exception as e:
        logger.exception("Error getting deal team memberships")
        return {"success": False, "error": str(e), "opportunity_ids": set()}


def add_user_to_milestone_team(milestone_msx_id: str) -> Dict[str, Any]:
    """
    Add the current user to a milestone's access team in MSX.

    Args:
        milestone_msx_id: The MSX GUID of the milestone.

    Returns:
        Dict with success: bool and optional error message.
    """
    try:
        user_id = get_current_user_id()
        if not user_id:
            return {"success": False, "error": "Could not get current user ID"}

        url = (
            f"{CRM_BASE_URL}/systemusers({user_id})"
            f"/Microsoft.Dynamics.CRM.AddUserToRecordTeam"
        )
        payload = {
            "Record": {
                "@odata.type": "Microsoft.Dynamics.CRM.msp_engagementmilestone",
                "msp_engagementmilestoneid": milestone_msx_id,
            },
            "TeamTemplate": {
                "@odata.type": "Microsoft.Dynamics.CRM.teamtemplate",
                "teamtemplateid": MILESTONE_TEAM_TEMPLATE_ID,
            },
        }

        response = _msx_request('POST', url, json_data=payload)

        if response.status_code in (200, 204):
            logger.info(f"Added user to milestone team: {milestone_msx_id}")
            return {"success": True}
        else:
            error_text = response.text[:300]
            # Check for "already on team" type errors
            if "already" in error_text.lower() or response.status_code == 409:
                return {"success": True, "already_on_team": True}
            logger.warning(
                f"Failed to add user to milestone team {milestone_msx_id}: "
                f"HTTP {response.status_code} — {error_text}"
            )
            return {
                "success": False,
                "error": f"MSX returned HTTP {response.status_code}",
            }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error adding user to milestone team {milestone_msx_id}")
        return {"success": False, "error": str(e)}


def remove_user_from_milestone_team(milestone_msx_id: str) -> Dict[str, Any]:
    """
    Remove the current user from a milestone's access team in MSX.

    Args:
        milestone_msx_id: The MSX GUID of the milestone.

    Returns:
        Dict with success: bool and optional error message.
    """
    try:
        user_id = get_current_user_id()
        if not user_id:
            return {"success": False, "error": "Could not get current user ID"}

        url = (
            f"{CRM_BASE_URL}/systemusers({user_id})"
            f"/Microsoft.Dynamics.CRM.RemoveUserFromRecordTeam"
        )
        payload = {
            "Record": {
                "@odata.type": "Microsoft.Dynamics.CRM.msp_engagementmilestone",
                "msp_engagementmilestoneid": milestone_msx_id,
            },
            "TeamTemplate": {
                "@odata.type": "Microsoft.Dynamics.CRM.teamtemplate",
                "teamtemplateid": MILESTONE_TEAM_TEMPLATE_ID,
            },
        }

        response = _msx_request('POST', url, json_data=payload)

        if response.status_code in (200, 204):
            logger.info(f"Removed user from milestone team: {milestone_msx_id}")
            return {"success": True}
        else:
            error_text = response.text[:300]
            if "not a member" in error_text.lower() or response.status_code == 409:
                return {"success": True, "not_on_team": True}
            logger.warning(
                f"Failed to remove user from milestone team {milestone_msx_id}: "
                f"HTTP {response.status_code} -- {error_text}"
            )
            return {
                "success": False,
                "error": f"MSX returned HTTP {response.status_code}",
            }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error removing user from milestone team {milestone_msx_id}")
        return {"success": False, "error": str(e)}


def add_user_to_deal_team(opportunity_msx_id: str) -> Dict[str, Any]:
    """
    Add the current user to an opportunity's deal team in MSX.

    Args:
        opportunity_msx_id: The MSX GUID of the opportunity.

    Returns:
        Dict with success: bool and optional error message.
    """
    try:
        user_id = get_current_user_id()
        if not user_id:
            return {"success": False, "error": "Could not get current user ID"}

        url = (
            f"{CRM_BASE_URL}/systemusers({user_id})"
            f"/Microsoft.Dynamics.CRM.AddUserToRecordTeam"
        )
        payload = {
            "Record": {
                "@odata.type": "Microsoft.Dynamics.CRM.opportunity",
                "opportunityid": opportunity_msx_id,
            },
            "TeamTemplate": {
                "@odata.type": "Microsoft.Dynamics.CRM.teamtemplate",
                "teamtemplateid": OPPORTUNITY_TEAM_TEMPLATE_ID,
            },
        }

        response = _msx_request('POST', url, json_data=payload)

        if response.status_code in (200, 204):
            logger.info(f"Added user to deal team: {opportunity_msx_id}")
            return {"success": True}
        else:
            error_text = response.text[:300]
            if "already" in error_text.lower() or response.status_code == 409:
                return {"success": True, "already_on_team": True}
            logger.warning(
                f"Failed to add user to deal team {opportunity_msx_id}: "
                f"HTTP {response.status_code} — {error_text}"
            )
            return {
                "success": False,
                "error": f"MSX returned HTTP {response.status_code}",
            }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error adding user to deal team {opportunity_msx_id}")
        return {"success": False, "error": str(e)}


def get_current_user_id() -> Optional[str]:
    """
    Get the current user's system user ID from MSX.
    
    Returns:
        User GUID if successful, None otherwise.
    """
    result = test_connection()
    if result.get("success"):
        return result.get("user_id")
    return None


def create_task(
    milestone_id: str,
    subject: str,
    task_category: int,
    duration_minutes: int = 60,
    description: str = None,
    due_date: str = None,
) -> Dict[str, Any]:
    """
    Create a task in MSX linked to a milestone.
    
    Args:
        milestone_id: The milestone GUID to link the task to.
        subject: Task subject/title.
        task_category: Numeric task category code.
        duration_minutes: Task duration (default 60).
        description: Optional task description.
        
    Returns:
        Dict with:
        - success: bool
        - task_id: str (GUID) if successful
        - task_url: str (MSX URL) if successful
        - error: str if failed
    """
    # Get current user ID for task owner
    user_id = get_current_user_id()
    if not user_id:
        return {"success": False, "error": "Could not determine current user."}
    
    try:
        # Build task payload
        task_data = {
            "subject": subject,
            "msp_taskcategory": task_category,
            "scheduleddurationminutes": duration_minutes,
            "prioritycode": 1,  # Normal priority (0=Low, 1=Normal, 2=High)
            "regardingobjectid_msp_engagementmilestone@odata.bind": f"/msp_engagementmilestones({milestone_id})",
            "ownerid@odata.bind": f"/systemusers({user_id})",
        }
        
        if description:
            task_data["description"] = description
        
        if due_date:
            task_data["scheduledend"] = due_date
        
        response = _msx_request('POST', f"{CRM_BASE_URL}/tasks", json_data=task_data)
        
        if response.status_code in (200, 201, 204):
            # Extract task ID from OData-EntityId header
            entity_id_header = response.headers.get("OData-EntityId", "")
            task_id = None
            
            import re
            match = re.search(r'tasks\(([a-f0-9-]{36})\)', entity_id_header, re.IGNORECASE)
            if match:
                task_id = match.group(1)
            
            if task_id:
                return {
                    "success": True,
                    "task_id": task_id,
                    "task_url": build_task_url(task_id),
                }
            else:
                return {
                    "success": True,
                    "task_id": None,
                    "task_url": None,
                    "warning": "Task created but could not extract ID from response.",
                }
                
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        elif response.status_code == 403:
            if is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
            return {"success": False, "error": "Access denied. You may not have permission to create tasks."}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:300]}"
            }
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out. Check VPN connection."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error (VPN?): {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error creating task for milestone {milestone_id}")
        return {"success": False, "error": str(e)}


def get_tasks_for_milestones(
    milestone_msx_ids: List[str],
) -> Dict[str, Any]:
    """
    Fetch the current user's tasks from MSX for a list of milestones.

    Queries the tasks entity filtered by ownerid = current user AND
    regardingobjectid in the given milestone GUIDs. OData doesn't support
    ``in`` on lookup fields, so we batch milestones into OR groups of 15
    to stay within Dynamics 365 filter-length limits.

    Args:
        milestone_msx_ids: List of milestone GUIDs (msp_engagementmilestoneid).

    Returns:
        Dict with:
        - success: bool
        - tasks: List of task dicts (keyed by milestone GUID)
        - error: str if failed
    """
    if not milestone_msx_ids:
        return {"success": True, "tasks": []}

    user_id = get_current_user_id()
    if not user_id:
        return {"success": False, "tasks": [], "error": "Could not determine current user."}

    # Build a lookup from task_category value -> {name, is_hok}
    cat_lookup = {
        c["value"]: {"name": c["label"], "is_hok": c["is_hok"]}
        for c in TASK_CATEGORIES
    }

    all_tasks: List[Dict[str, Any]] = []

    # Batch milestones in groups of 15 to keep OData filter length sane
    batch_size = 15
    for i in range(0, len(milestone_msx_ids), batch_size):
        batch = milestone_msx_ids[i:i + batch_size]

        # Build OR clause for regarding milestone IDs
        regarding_clauses = " or ".join(
            f"_regardingobjectid_value eq '{mid}'" for mid in batch
        )
        filter_str = (
            f"_ownerid_value eq '{user_id}'"
            f" and ({regarding_clauses})"
        )

        url = (
            f"{CRM_BASE_URL}/tasks"
            f"?$filter={filter_str}"
            f"&$select=activityid,subject,description,"
            f"msp_taskcategory,scheduleddurationminutes,"
            f"scheduledend,_regardingobjectid_value"
            f"&$top=5000"
        )

        try:
            response = _msx_request('GET', url)

            if response.status_code == 200:
                data = response.json()
                for raw in data.get("value", []):
                    task_id = raw.get("activityid")
                    if not task_id:
                        continue

                    category_code = raw.get("msp_taskcategory")
                    cat_info = cat_lookup.get(category_code, {})

                    due_date_str = raw.get("scheduledend")

                    all_tasks.append({
                        "task_id": task_id,
                        "subject": raw.get("subject", ""),
                        "description": raw.get("description"),
                        "task_category": category_code,
                        "task_category_name": cat_info.get("name"),
                        "is_hok": cat_info.get("is_hok", False),
                        "duration_minutes": raw.get("scheduleddurationminutes") or 60,
                        "due_date": due_date_str,
                        "milestone_msx_id": (
                            raw.get("_regardingobjectid_value") or ""
                        ).lower(),
                        "task_url": build_task_url(task_id),
                    })

                # Follow pagination
                next_link = data.get("@odata.nextLink")
                while next_link:
                    resp = _msx_request('GET', next_link)
                    if resp.status_code == 200:
                        page_data = resp.json()
                        for raw in page_data.get("value", []):
                            task_id = raw.get("activityid")
                            if not task_id:
                                continue
                            category_code = raw.get("msp_taskcategory")
                            cat_info = cat_lookup.get(category_code, {})
                            all_tasks.append({
                                "task_id": task_id,
                                "subject": raw.get("subject", ""),
                                "description": raw.get("description"),
                                "task_category": category_code,
                                "task_category_name": cat_info.get("name"),
                                "is_hok": cat_info.get("is_hok", False),
                                "duration_minutes": raw.get("scheduleddurationminutes") or 60,
                                "due_date": raw.get("scheduledend"),
                                "milestone_msx_id": (
                                    raw.get("_regardingobjectid_value") or ""
                                ).lower(),
                                "task_url": build_task_url(task_id),
                            })
                        next_link = page_data.get("@odata.nextLink")
                    else:
                        break

            elif response.status_code == 401:
                return {"success": False, "tasks": [], "error": "Not authenticated. Run 'az login' first."}
            elif response.status_code == 403:
                if is_vpn_blocked():
                    return {"success": False, "tasks": [], "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
                return {"success": False, "tasks": [], "error": "Access denied querying tasks."}
            else:
                return {
                    "success": False, "tasks": [],
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }

        except requests.exceptions.Timeout:
            return {"success": False, "tasks": [], "error": "Request timed out. Check VPN connection."}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "tasks": [], "error": f"Connection error (VPN?): {str(e)[:100]}"}
        except Exception as e:
            logger.exception("Error fetching tasks for milestones")
            return {"success": False, "tasks": [], "error": str(e)}

    logger.info(f"Fetched {len(all_tasks)} user tasks across {len(milestone_msx_ids)} milestones")
    return {"success": True, "tasks": all_tasks}


# =============================================================================
# MSX Exploration / Schema Discovery Functions
# =============================================================================

def query_entity(
    entity_name: str,
    select: Optional[List[str]] = None,
    filter_query: Optional[str] = None,
    expand: Optional[str] = None,
    top: int = 10,
    order_by: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generic OData query for any MSX entity.
    
    Note: Dynamics 365 doesn't support $skip. For pagination, use query_entity_all()
    which follows @odata.nextLink.
    
    Args:
        entity_name: The entity set name (e.g., 'accounts', 'systemusers', 'territories')
        select: List of fields to return (None = all fields)
        filter_query: OData $filter expression
        expand: OData $expand for related entities
        top: Max records to return (default 10)
        order_by: OData $orderby expression
        
    Returns:
        Dict with success/error and records array
    """
    try:
        # Build query params
        params = [f"$top={top}"]
        
        if select:
            params.append(f"$select={','.join(select)}")
        if filter_query:
            params.append(f"$filter={filter_query}")
        if expand:
            params.append(f"$expand={expand}")
        if order_by:
            params.append(f"$orderby={order_by}")
        
        query_string = "&".join(params)
        url = f"{CRM_BASE_URL}/{entity_name}?{query_string}"
        
        logger.info(f"Querying MSX: {url}")
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            data = response.json()
            records = data.get("value", [])
            next_link = data.get("@odata.nextLink")
            return {
                "success": True,
                "entity": entity_name,
                "count": len(records),
                "records": records,
                "next_link": next_link,  # For pagination
                "query_url": url
            }
        elif response.status_code == 401:
            return {"success": False, "error": "Not authenticated. Run 'az login' first."}
        elif response.status_code == 403:
            if is_vpn_blocked():
                return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
            return {"success": False, "error": "Access denied."}
        elif response.status_code == 404:
            return {"success": False, "error": f"Entity '{entity_name}' not found."}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:500]}"
            }
            
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out."}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "error": f"Connection error: {str(e)[:100]}"}
    except Exception as e:
        logger.exception(f"Error querying {entity_name}")
        return {"success": False, "error": str(e)}


def query_next_page(next_link: str) -> Dict[str, Any]:
    """
    Follow an @odata.nextLink to get the next page of results.
    
    Dynamics 365 uses continuation tokens instead of $skip for pagination.
    """
    try:
        logger.info(f"Following nextLink: {next_link[:100]}...")
        response = _msx_request('GET', next_link)
        
        if response.status_code == 200:
            data = response.json()
            records = data.get("value", [])
            new_next_link = data.get("@odata.nextLink")
            return {
                "success": True,
                "count": len(records),
                "records": records,
                "next_link": new_next_link
            }
        elif response.status_code == 403 and is_vpn_blocked():
            return {"success": False, "error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:500]}"
            }
    except Exception as e:
        logger.exception("Error following nextLink")
        return {"success": False, "error": str(e)}


def batch_query_accounts(
    account_ids: List[str],
    batch_size: int = 15,
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Query multiple accounts in batches using OData 'or' filter.
    
    Much more efficient than individual queries - reduces 299 calls to ~20.
    
    Args:
        account_ids: List of account GUIDs to query
        batch_size: How many accounts per query (default 15 to avoid URL length limits)
        progress_callback: Optional callback(batch_num, total_batches, fetched_so_far)
            called after each batch completes.
        
    Returns:
        Dict with success/error and accounts dict keyed by account ID
    """
    all_accounts = {}  # accountid -> account record
    total_batches = (len(account_ids) + batch_size - 1) // batch_size
    
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            
            # Build OR filter: accountid eq 'X' or accountid eq 'Y' or ...
            filter_parts = [f"accountid eq {aid}" for aid in batch]
            filter_query = " or ".join(filter_parts)
            
            result = query_entity(
                "accounts",
                select=[
                    "accountid", "name", "msp_mstopparentid", "_territoryid_value",
                    "msp_verticalcode", "msp_verticalcategorycode",
                    "websiteurl", "msp_parentinglevelcode"
                ],
                filter_query=filter_query,
                top=batch_size + 5  # A little buffer just in case
            )
            
            if not result.get("success"):
                logger.warning(f"Batch query failed: {result.get('error')}")
                continue
            
            for record in result.get("records", []):
                acct_id = record.get("accountid")
                if acct_id:
                    all_accounts[acct_id] = record
            
            if progress_callback:
                progress_callback(batch_num, total_batches, len(all_accounts))
        
        return {
            "success": True,
            "accounts": all_accounts,
            "count": len(all_accounts)
        }
        
    except Exception as e:
        logger.exception("Error in batch account query")
        return {"success": False, "error": str(e)}


def batch_query_territories(
    territory_ids: List[str],
    batch_size: int = 15,
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Query multiple territories in batches.
    
    Args:
        territory_ids: List of territory GUIDs to query
        batch_size: How many territories per query
        progress_callback: Optional callback(batch_num, total_batches, fetched_so_far)
            called after each batch completes.
        
    Returns:
        Dict with territories dict keyed by territory ID
    """
    all_territories = {}  # territoryid -> territory record
    
    try:
        # Deduplicate territory IDs
        unique_ids = list(set(territory_ids))
        total_batches = (len(unique_ids) + batch_size - 1) // batch_size
        
        for batch_num, i in enumerate(range(0, len(unique_ids), batch_size), start=1):
            batch = unique_ids[i:i + batch_size]
            
            filter_parts = [f"territoryid eq {tid}" for tid in batch]
            filter_query = " or ".join(filter_parts)
            
            result = query_entity(
                "territories",
                select=["territoryid", "name", "msp_ownerid", "msp_salesunitname", "msp_accountteamunitname"],
                filter_query=filter_query,
                top=batch_size + 5
            )
            
            if not result.get("success"):
                logger.warning(f"Batch territory query failed: {result.get('error')}")
                continue
            
            for record in result.get("records", []):
                terr_id = record.get("territoryid")
                if terr_id:
                    all_territories[terr_id] = record
            
            if progress_callback:
                progress_callback(batch_num, total_batches, len(all_territories))
        
        return {
            "success": True,
            "territories": all_territories,
            "count": len(all_territories)
        }
        
    except Exception as e:
        logger.exception("Error in batch territory query")
        return {"success": False, "error": str(e)}


def batch_query_account_teams(
    account_ids: List[str],
    batch_size: int = 5
) -> Dict[str, Any]:
    """
    Query msp_accountteams for all accounts to get sellers and SEs.
    
    Uses server-side filtering for Corporate + Cloud & AI qualifiers,
    reducing 300+ team members per account to ~20-30 (no pagination needed).
    
    Filters for qualifier1="Corporate" AND Cloud & AI roles:
    - Sellers: "Cloud & AI" (Growth), "Cloud & AI-Acq" (Acquisition) with title "Specialists IC"
    - SEs: "Cloud & AI Data", "Cloud & AI Infrastructure", "Cloud & AI Apps"
    
    Args:
        account_ids: List of account GUIDs
        batch_size: How many accounts per query (can be higher now with server-side filtering)
        
    Returns:
        Dict with:
        - account_sellers: {account_id: {name, type, user_id}}
        - unique_sellers: {seller_name: {name, type, user_id}}
        - se_by_pod_account: {account_id: {data_se, infra_se, apps_se}}
    """
    # Relevant qualifier2 values
    seller_qualifiers = {"Cloud & AI": "Growth", "Cloud & AI-Acq": "Acquisition"}
    se_qualifiers = {
        "Cloud & AI Data": "data_se",
        "Cloud & AI Infrastructure": "infra_se",
        "Cloud & AI Apps": "apps_se"
    }
    
    account_sellers = {}  # account_id -> {name, type, user_id}
    unique_sellers = {}   # seller_name -> {name, type, user_id}
    account_ses = {}      # account_id -> {data_se, infra_se, apps_se}
    
    def process_record(record):
        """Process a single team member record."""
        acct_id = record.get("_msp_accountid_value")
        qualifier2 = record.get("msp_qualifier2", "")
        standardtitle = record.get("msp_standardtitle", "")
        name = record.get("msp_fullname", "")
        user_id = record.get("_msp_systemuserid_value")
        
        if not acct_id or not name:
            return
        
        # Seller assignment: qualifier2 is Cloud & AI or Cloud & AI-Acq
        # AND standardtitle contains "Specialists IC" (filters out managers, CSAs, CSU, etc.)
        if qualifier2 in seller_qualifiers and "Specialists IC" in standardtitle:
            seller_type = seller_qualifiers[qualifier2]
            account_sellers[acct_id] = {"name": name, "type": seller_type, "user_id": user_id}
            if name not in unique_sellers:
                unique_sellers[name] = {"name": name, "type": seller_type, "user_id": user_id}
        
        # SE assignment - collect ALL SEs per role (there can be multiple)
        elif qualifier2 in se_qualifiers:
            if acct_id not in account_ses:
                account_ses[acct_id] = {"data_se": [], "infra_se": [], "apps_se": []}
            
            se_key = se_qualifiers[qualifier2]  # "data_se", "infra_se", or "apps_se"
            # Add SE if not already in the list for this account
            existing_names = [se["name"] for se in account_ses[acct_id][se_key]]
            if name not in existing_names:
                account_ses[acct_id][se_key].append({"name": name, "user_id": user_id})
    
    try:
        # Query accounts in batches with server-side filtering
        # Server-side filter for Corporate + Cloud & AI* reduces 300+ to ~20-30 per account
        for i in range(0, len(account_ids), batch_size):
            batch = account_ids[i:i + batch_size]
            
            # Build filter: account IDs + Corporate + Cloud & AI qualifiers (server-side)
            account_filter = " or ".join([f"_msp_accountid_value eq {aid}" for aid in batch])
            # Filter server-side for Corporate + Cloud & AI* (reduces 300+ records to ~20-30)
            filter_query = f"({account_filter}) and msp_qualifier1 eq 'Corporate' and startswith(msp_qualifier2,'Cloud ')"
            
            result = query_entity(
                "msp_accountteams",
                select=["_msp_accountid_value", "msp_fullname", "msp_qualifier2", "msp_standardtitle", "_msp_systemuserid_value"],
                filter_query=filter_query,
                top=100  # With server-side filtering, 100 should be enough for 3 accounts
            )
            
            if not result.get("success"):
                logger.warning(f"Batch account teams query failed: {result.get('error')}")
                continue
            
            records = result.get("records", [])
            # Warn if we hit the 100 record limit (may have lost data)
            if len(records) >= 100:
                logger.warning(f"Hit 100 record limit for batch of {len(batch)} accounts - may be missing sellers/SEs")
            
            for record in records:
                process_record(record)
        
        return {
            "success": True,
            "account_sellers": account_sellers,
            "unique_sellers": unique_sellers,
            "account_ses": account_ses,
            "seller_count": len(unique_sellers),
            "accounts_with_sellers": len(account_sellers),
        }
        
    except Exception as e:
        logger.exception("Error in batch account teams query")
        return {"success": False, "error": str(e)}


def get_user_alias(systemuser_id: str) -> Optional[str]:
    """
    Look up a systemuser by ID and return their email alias.
    
    The alias is extracted from the email address (part before @microsoft.com).
    Used when creating new sellers/SEs to populate their alias field.
    
    Args:
        systemuser_id: The MSX systemuser GUID
        
    Returns:
        Email alias (e.g., 'alexbla') or None if lookup fails
    """
    if not systemuser_id:
        return None
    
    try:
        url = f"{CRM_BASE_URL}/systemusers({systemuser_id})?$select=domainname,internalemailaddress"
        response = _msx_request('GET', url)
        
        if response.status_code != 200:
            logger.warning(f"Failed to look up systemuser {systemuser_id}: {response.status_code}")
            return None
        
        data = response.json()
        # Get email from domainname or internalemailaddress
        email = data.get("domainname") or data.get("internalemailaddress") or ""
        
        # Extract alias (part before @)
        if "@" in email:
            return email.split("@")[0]
        
        return None
        
    except Exception as e:
        logger.warning(f"Error looking up user alias for {systemuser_id}: {e}")
        return None


def query_pod_ses_from_account(account_id: str) -> Dict[str, Any]:
    """
    Query ONE account's team to get all SEs for the POD.
    
    Uses server-side filtering for Corporate + Cloud & AI* qualifiers,
    reducing 300+ team members to ~20-30 records (no pagination needed).
    
    Args:
        account_id: Single account GUID
        
    Returns:
        Dict with:
        - success: bool
        - ses: {data_se: [...], infra_se: [...], apps_se: [...]}
    """
    se_qualifiers = {
        "Cloud & AI Data": "data_se",
        "Cloud & AI Infrastructure": "infra_se",
        "Cloud & AI Apps": "apps_se"
    }
    
    ses = {"data_se": [], "infra_se": [], "apps_se": []}
    
    try:
        # Filter server-side for Corporate + Cloud & AI qualifiers only
        # This reduces 300+ records to ~20-30, avoiding pagination issues
        filter_query = f"_msp_accountid_value eq {account_id} and msp_qualifier1 eq 'Corporate' and startswith(msp_qualifier2,'Cloud ')"
        result = query_entity(
            "msp_accountteams",
            select=["msp_fullname", "msp_qualifier1", "msp_qualifier2", "_msp_systemuserid_value"],
            filter_query=filter_query,
            top=100
        )
        
        if not result.get("success"):
            logger.warning(f"Pod SE query failed: {result.get('error')}")
            return {"success": False, "error": result.get("error")}
        
        for record in result.get("records", []):
            qualifier2 = record.get("msp_qualifier2", "")
            name = record.get("msp_fullname", "")
            user_id = record.get("_msp_systemuserid_value")
            
            if qualifier2 in se_qualifiers:
                se_key = se_qualifiers[qualifier2]
                existing_names = [se["name"] for se in ses[se_key]]
                if name not in existing_names:
                    ses[se_key].append({"name": name, "user_id": user_id})
        
        return {"success": True, "ses": ses}
        
    except Exception as e:
        logger.exception(f"Error querying POD SEs for account {account_id}")
        return {"success": False, "error": str(e)}


def find_account_seller(account_id: str) -> Dict[str, Any]:
    """
    Find the seller for ONE account.
    
    Uses server-side filtering for Corporate + Cloud & AI* qualifiers,
    reducing 300+ team members to ~20-30 records (no pagination needed).
    Then filters locally for "Specialists IC" in title.
    
    Args:
        account_id: Single account GUID
        
    Returns:
        Dict with:
        - success: bool
        - seller: {name, type, user_id} or None if not found
    """
    seller_qualifiers = {"Cloud & AI": "Growth", "Cloud & AI-Acq": "Acquisition"}
    
    try:
        # Filter server-side for Corporate + Cloud & AI qualifiers only
        # This reduces 300+ records to ~20-30, avoiding pagination issues
        filter_query = f"_msp_accountid_value eq {account_id} and msp_qualifier1 eq 'Corporate' and startswith(msp_qualifier2,'Cloud ')"
        result = query_entity(
            "msp_accountteams",
            select=["msp_fullname", "msp_qualifier1", "msp_qualifier2", "msp_standardtitle", "_msp_systemuserid_value"],
            filter_query=filter_query,
            top=100
        )
        
        if not result.get("success"):
            logger.warning(f"Seller query failed: {result.get('error')}")
            return {"success": False, "error": result.get("error")}
        
        # Find seller in filtered results
        for record in result.get("records", []):
            qualifier2 = record.get("msp_qualifier2", "")
            standardtitle = record.get("msp_standardtitle", "")
            name = record.get("msp_fullname", "")
            user_id = record.get("_msp_systemuserid_value")
            
            # Seller: Cloud & AI or Cloud & AI-Acq + "Specialists IC" in title
            if qualifier2 in seller_qualifiers and "Specialists IC" in standardtitle:
                seller_type = seller_qualifiers[qualifier2]
                return {"success": True, "seller": {"name": name, "type": seller_type, "user_id": user_id}}
        
        return {"success": True, "seller": None}
        
    except Exception as e:
        logger.exception(f"Error finding seller for account {account_id}")
        return {"success": False, "error": str(e)}


def get_current_user() -> Dict[str, Any]:
    """
    Get the current authenticated user's details from MSX.
    
    Returns user ID, name, email, and other useful info.
    """
    try:
        # First get user ID via WhoAmI
        whoami_response = _msx_request('GET', f"{CRM_BASE_URL}/WhoAmI")
        
        if whoami_response.status_code != 200:
            if whoami_response.status_code == 401:
                return {"success": False, "error": "Not authenticated. Run 'az login' first."}
            return {"success": False, "error": f"WhoAmI failed: {whoami_response.status_code}"}
        
        whoami_data = whoami_response.json()
        user_id = whoami_data.get("UserId")
        
        if not user_id:
            return {"success": False, "error": "Could not get user ID from WhoAmI"}
        
        # Now get full user record
        url = f"{CRM_BASE_URL}/systemusers({user_id})"
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            user_data = response.json()
            return {
                "success": True,
                "user_id": user_id,
                "user": user_data
            }
        else:
            return {
                "success": False,
                "error": f"Failed to get user details: {response.status_code}"
            }
            
    except Exception as e:
        logger.exception("Error getting current user")
        return {"success": False, "error": str(e)}


def get_entity_metadata(entity_name: str) -> Dict[str, Any]:
    """
    Get metadata/schema for an entity to discover available fields.
    
    Args:
        entity_name: Logical name of entity (e.g., 'account', 'systemuser')
        
    Returns:
        Dict with entity attributes and their types
    """
    try:
        # Query the metadata endpoint
        url = f"{CRM_BASE_URL}/EntityDefinitions(LogicalName='{entity_name}')/Attributes"
        response = _msx_request('GET', url)
        
        if response.status_code == 200:
            data = response.json()
            attributes = data.get("value", [])
            
            # Simplify the output - just key info
            simplified = []
            for attr in attributes:
                simplified.append({
                    "name": attr.get("LogicalName"),
                    "display_name": attr.get("DisplayName", {}).get("UserLocalizedLabel", {}).get("Label") if isinstance(attr.get("DisplayName"), dict) else None,
                    "type": attr.get("AttributeType"),
                    "description": attr.get("Description", {}).get("UserLocalizedLabel", {}).get("Label") if isinstance(attr.get("Description"), dict) else None,
                })
            
            # Sort by name
            simplified.sort(key=lambda x: x.get("name", ""))
            
            return {
                "success": True,
                "entity": entity_name,
                "attribute_count": len(simplified),
                "attributes": simplified
            }
        elif response.status_code == 404:
            return {"success": False, "error": f"Entity '{entity_name}' not found"}
        else:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:300]}"
            }
            
    except Exception as e:
        logger.exception(f"Error getting metadata for {entity_name}")
        return {"success": False, "error": str(e)}


def explore_user_territories() -> Dict[str, Any]:
    """
    Explore what territories/accounts the current user has access to.
    
    Tries multiple approaches to find the user's assigned territories/customers.
    """
    results = {
        "success": True,
        "explorations": []
    }
    
    # 1. Get current user
    user_result = get_current_user()
    if not user_result.get("success"):
        return user_result
    
    user = user_result.get("user", {})
    user_id = user_result.get("user_id")
    
    results["current_user"] = {
        "id": user_id,
        "name": user.get("fullname"),
        "email": user.get("internalemailaddress"),
        "title": user.get("title"),
        "business_unit": user.get("_businessunitid_value"),
        "territory": user.get("_territoryid_value"),  # Direct territory assignment
    }
    
    # 2. Check if user has a direct territory assignment
    if user.get("_territoryid_value"):
        territory_id = user.get("_territoryid_value")
        territory_result = query_entity(
            "territories",
            filter_query=f"territoryid eq {territory_id}",
            top=1
        )
        if territory_result.get("success") and territory_result.get("records"):
            results["direct_territory"] = territory_result["records"][0]
    
    # 3. Look for team memberships that might link to territories
    team_result = query_entity(
        "teammemberships",
        filter_query=f"systemuserid eq {user_id}",
        top=50
    )
    if team_result.get("success"):
        results["explorations"].append({
            "query": "User team memberships",
            "result": team_result
        })
    
    # 4. Look for accounts where user is owner
    owned_accounts = query_entity(
        "accounts",
        select=["accountid", "name", "msp_mstopparentid", "msp_accountsegment"],
        filter_query=f"_ownerid_value eq {user_id}",
        top=20
    )
    if owned_accounts.get("success"):
        results["owned_accounts"] = owned_accounts.get("records", [])
    
    # 5. Look for msp_accountteammember or similar
    # Try to find account team member records for this user
    try:
        team_member_result = query_entity(
            "msp_accountteammembers",
            filter_query=f"_msp_user_value eq {user_id}",
            top=50
        )
        if team_member_result.get("success"):
            results["explorations"].append({
                "query": "Account team memberships (msp_accountteammembers)",
                "result": team_member_result
            })
    except Exception:
        pass  # Entity might not exist
    
    return results


def get_my_accounts() -> Dict[str, Any]:
    """
    Get all accounts the current user has access to via team memberships.
    
    Pattern:
    1. Get current user ID via WhoAmI
    2. Query teammemberships for my user ID (get team IDs)
    3. Query teams for those IDs to get regardingobjectid (account IDs)
    4. Query accounts to get names, TPIDs, sellers, territories
    
    Returns:
        Dict with accounts array containing name, tpid, territory, seller info
    """
    try:
        # 1. Get current user ID
        user_result = get_current_user()
        if not user_result.get("success"):
            return user_result
        
        user_id = user_result.get("user_id")
        user_name = user_result.get("user", {}).get("fullname", "Unknown")
        
        # 2. Get my team memberships (cap at 200 to be reasonable)
        team_memberships = query_entity(
            "teammemberships",
            filter_query=f"systemuserid eq {user_id}",
            top=200
        )
        
        if not team_memberships.get("success"):
            return team_memberships
        
        team_ids = [tm.get("teamid") for tm in team_memberships.get("records", []) if tm.get("teamid")]
        
        if not team_ids:
            return {
                "success": True,
                "accounts": [],
                "message": "No team memberships found"
            }
        
        # 3. Get teams to find regardingobjectid (account IDs)
        # Query in batches to avoid URL length limits
        account_ids = set()
        batch_size = 15
        
        for i in range(0, len(team_ids), batch_size):
            batch = team_ids[i:i+batch_size]
            filter_parts = [f"teamid eq {tid}" for tid in batch]
            filter_query = " or ".join(filter_parts)
            
            teams_result = query_entity(
                "teams",
                select=["teamid", "name", "_regardingobjectid_value"],
                filter_query=filter_query,
                top=50
            )
            
            if teams_result.get("success"):
                for team in teams_result.get("records", []):
                    # Check if this team is associated with an account
                    regard_id = team.get("_regardingobjectid_value")
                    if regard_id:
                        account_ids.add(regard_id)
        
        if not account_ids:
            return {
                "success": True,
                "accounts": [],
                "message": "No accounts found via team memberships"
            }
        
        # 4. Get account details
        accounts = []
        account_list = list(account_ids)
        
        for i in range(0, len(account_list), batch_size):
            batch = account_list[i:i+batch_size]
            filter_parts = [f"accountid eq {aid}" for aid in batch]
            filter_query = " or ".join(filter_parts)
            
            accounts_result = query_entity(
                "accounts",
                select=[
                    "accountid", "name", "msp_mstopparentid",
                    "_ownerid_value", "_msp_atu_value"
                ],
                filter_query=filter_query,
                top=50
            )
            
            if accounts_result.get("success"):
                for acct in accounts_result.get("records", []):
                    accounts.append({
                        "account_id": acct.get("accountid"),
                        "name": acct.get("name"),
                        "tpid": acct.get("msp_mstopparentid"),
                        "owner_id": acct.get("_ownerid_value"),
                        "owner_name": acct.get("_ownerid_value@OData.Community.Display.V1.FormattedValue"),
                        "atu_id": acct.get("_msp_atu_value"),
                        "atu_name": acct.get("_msp_atu_value@OData.Community.Display.V1.FormattedValue"),
                    })
        
        # Sort by name
        accounts.sort(key=lambda x: (x.get("name") or "").lower())
        
        return {
            "success": True,
            "user": user_name,
            "user_id": user_id,
            "team_count": len(team_ids),
            "account_count": len(accounts),
            "accounts": accounts
        }
        
    except Exception as e:
        logger.exception("Error getting my accounts")
        return {"success": False, "error": str(e)}


def search_territories(query: str, top: int = 50) -> Dict[str, Any]:
    """
    Search for territories by partial name match.
    
    Args:
        query: Partial territory name to search for (e.g., "0602" or "MAA")
        top: Maximum number of results to return
        
    Returns:
        Dict with:
        - success: bool
        - territories: list of territory dicts with id and name
    """
    try:
        # Use contains() for partial matching
        result = query_entity(
            "territories",
            select=["territoryid", "name", "msp_ownerid"],
            filter_query=f"contains(name, '{query}')",
            top=top,
            order_by="name asc"
        )
        
        if not result.get("success"):
            return result
        
        territories = []
        for t in result.get("records", []):
            territories.append({
                "id": t.get("territoryid"),
                "name": t.get("name"),
                "seller_id": t.get("msp_ownerid"),
                "seller_name": t.get("msp_ownerid@OData.Community.Display.V1.FormattedValue"),
            })
        
        return {
            "success": True,
            "count": len(territories),
            "territories": territories
        }
        
    except Exception as e:
        logger.exception(f"Error searching territories for '{query}'")
        return {"success": False, "error": str(e)}


def get_accounts_for_territories(territory_names: List[str]) -> Dict[str, Any]:
    """
    Get all accounts for a list of territory names.
    
    Args:
        territory_names: List of territory names (e.g., ["East.SMECC.SDP.0603", "East.SMECC.MAA.0601"])
        
    Returns:
        Dict with:
        - success: bool
        - accounts: list of account dicts with name, tpid, seller, territory
        - territories: list of territory info that was found
    """
    try:
        # 1. Look up territory IDs from names
        territories = []
        for name in territory_names:
            result = query_entity(
                "territories",
                select=["territoryid", "name"],
                filter_query=f"name eq '{name}'",
                top=1
            )
            if result.get("success") and result.get("records"):
                territories.append(result["records"][0])
        
        if not territories:
            return {
                "success": False,
                "error": "No territories found matching the provided names"
            }
        
        # 2. Query accounts for each territory
        accounts = []
        for territory in territories:
            territory_id = territory.get("territoryid")
            territory_name = territory.get("name")
            
            # Query accounts with this territory - cap at 200 per territory
            accounts_result = query_entity(
                "accounts",
                select=["accountid", "name", "msp_mstopparentid", "_ownerid_value", "_territoryid_value"],
                filter_query=f"_territoryid_value eq {territory_id}",
                top=200
            )
            
            if accounts_result.get("success"):
                for acct in accounts_result.get("records", []):
                    accounts.append({
                        "account_id": acct.get("accountid"),
                        "name": acct.get("name"),
                        "tpid": acct.get("msp_mstopparentid"),
                        "seller_id": acct.get("_ownerid_value"),
                        "seller_name": acct.get("_ownerid_value@OData.Community.Display.V1.FormattedValue"),
                        "territory_id": territory_id,
                        "territory_name": territory_name,
                    })
        
        # Sort by name
        accounts.sort(key=lambda x: (x.get("name") or "").lower())
        
        return {
            "success": True,
            "territory_count": len(territories),
            "territories": [{"id": t.get("territoryid"), "name": t.get("name")} for t in territories],
            "account_count": len(accounts),
            "accounts": accounts
        }
        
    except Exception as e:
        logger.exception("Error getting accounts for territories")
        return {"success": False, "error": str(e)}


def find_my_territories(atu_filter: Optional[str] = None) -> Dict[str, Any]:
    """
    Find territories where the current user is assigned as a Data SE, Infra SE, or Apps SE.
    
    Uses msp_accountteams to find account assignments where the user's qualifier2 is:
    - "Cloud & AI Data" (Data SE)
    - "Cloud & AI Infrastructure" (Infra SE) 
    - "Cloud & AI Apps" (Apps SE)
    - "Cloud & AI" (Growth Seller)
    - "Cloud & AI-Acq" (Acquisition Seller)
    
    Args:
        atu_filter: Optional ATU code to filter by (e.g., "MAA", "HLA"). If provided,
                   only returns territories matching "East.SMECC.{atu_filter}.*"
    
    Returns:
        Dict with:
        - success: bool
        - user: current user info
        - role: detected role (Data SE, Infra SE, Apps SE, Growth Seller, Acq Seller)
        - territories: list of territories the user is assigned to
        - sample_accounts: one sample account per territory for verification
    """
    try:
        # 1. Get current user
        user_result = get_current_user()
        if not user_result.get("success"):
            return user_result
        
        user_id = user_result.get("user_id")
        user = user_result.get("user", {})
        user_name = user.get("fullname", "Unknown")
        user_qualifier2 = user.get("msp_qualifier2", "")
        
        # Determine role from qualifier2
        role_map = {
            "Cloud & AI Data": "Data SE",
            "Cloud & AI Infrastructure": "Infra SE",
            "Cloud & AI Apps": "Apps SE",
            "Cloud & AI": "Growth Seller",
            "Cloud & AI-Acq": "Acquisition Seller",
        }
        detected_role = role_map.get(user_qualifier2, f"Unknown ({user_qualifier2})")
        
        # 2. Query msp_accountteams where this user is a member
        # Filter by relevant qualifier2 values (SE or Seller roles)
        relevant_qualifiers = ["Cloud & AI Data", "Cloud & AI Infrastructure", "Cloud & AI Apps", 
                               "Cloud & AI", "Cloud & AI-Acq"]
        
        team_result = query_entity(
            "msp_accountteams",
            select=["msp_accountteamid", "_msp_accountid_value", "msp_qualifier2", "msp_fullname"],
            filter_query=f"_msp_systemuserid_value eq {user_id}",
            top=500  # Get up to 500 assignments
        )
        
        if not team_result.get("success"):
            return team_result
        
        # 3. Filter to only SE/Seller roles and collect unique account IDs
        account_ids = set()
        my_qualifier2 = None
        for entry in team_result.get("records", []):
            qualifier2 = entry.get("msp_qualifier2", "")
            if qualifier2 in relevant_qualifiers:
                account_id = entry.get("_msp_accountid_value")
                if account_id:
                    account_ids.add(account_id)
                if not my_qualifier2:
                    my_qualifier2 = qualifier2
        
        if not account_ids:
            return {
                "success": True,
                "user": {"id": user_id, "name": user_name, "qualifier2": user_qualifier2},
                "role": detected_role,
                "territories": [],
                "message": "No account team assignments found for SE/Seller roles"
            }
        
        # 4. Get territories for these accounts
        # We'll query a sample of accounts to get territory info
        territory_map = {}  # territory_id -> territory info
        sample_accounts = {}  # territory_id -> sample account
        
        # Query accounts in batches to get their territories
        account_list = list(account_ids)[:100]  # Limit to first 100 for performance
        
        for account_id in account_list:
            acct_result = query_entity(
                "accounts",
                select=["accountid", "name", "msp_mstopparentid", "_territoryid_value"],
                filter_query=f"accountid eq {account_id}",
                top=1
            )
            
            if acct_result.get("success") and acct_result.get("records"):
                acct = acct_result["records"][0]
                territory_id = acct.get("_territoryid_value")
                territory_name = acct.get("_territoryid_value@OData.Community.Display.V1.FormattedValue", "")
                
                if territory_id and territory_id not in territory_map:
                    territory_map[territory_id] = {
                        "id": territory_id,
                        "name": territory_name,
                    }
                    sample_accounts[territory_id] = {
                        "account_id": acct.get("accountid"),
                        "name": acct.get("name"),
                        "tpid": acct.get("msp_mstopparentid"),
                    }
        
        # 5. Get additional territory details (POD info, seller)
        # Build ATU prefix for filtering if specified
        atu_prefix = f"East.SMECC.{atu_filter}." if atu_filter else None
        
        territories = []
        for territory_id, territory_info in territory_map.items():
            # Early filter by ATU if specified
            territory_name = territory_info.get("name", "")
            if atu_prefix and not territory_name.startswith(atu_prefix):
                continue
            
            # Query territory for more details
            terr_result = query_entity(
                "territories",
                select=["territoryid", "name", "msp_ownerid", "msp_salesunitname", "msp_accountteamunitname"],
                filter_query=f"territoryid eq {territory_id}",
                top=1
            )
            
            if terr_result.get("success") and terr_result.get("records"):
                terr = terr_result["records"][0]
                
                # Try to derive POD from territory name
                # e.g., "East.SMECC.MAA.0601" -> "East POD 06"
                # Note: some territories have suffixes like ".A" or ".B"
                # (e.g., "East.SMECC.HLA.0610.A"), so always use index 3
                name_parts = terr.get("name", "").split(".")
                pod_name = None
                if len(name_parts) >= 4:
                    region = name_parts[0]  # "East"
                    territory_num = name_parts[3]  # "0601" or "0610"
                    if len(territory_num) >= 2:
                        pod_num = territory_num[:2]  # "06"
                        pod_name = f"{region} POD {pod_num}"
                
                territories.append({
                    "id": territory_id,
                    "name": terr.get("name"),
                    "seller_id": terr.get("msp_ownerid"),
                    "seller_name": terr.get("msp_ownerid@OData.Community.Display.V1.FormattedValue"),
                    "sales_unit": terr.get("msp_salesunitname"),
                    "atu": terr.get("msp_accountteamunitname"),
                    "pod": pod_name,
                    "sample_account": sample_accounts.get(territory_id),
                })
        
        # Sort by name
        territories.sort(key=lambda x: x.get("name", ""))
        
        return {
            "success": True,
            "user": {"id": user_id, "name": user_name, "qualifier2": user_qualifier2},
            "role": detected_role,
            "atu_filter": atu_filter,
            "account_assignments_found": len(account_ids),
            "accounts_sampled": len(account_list),
            "territory_count": len(territories),
            "territories": territories,
        }
        
    except Exception as e:
        logger.exception("Error finding user's territories")
        return {"success": False, "error": str(e)}


def scan_init() -> Dict[str, Any]:
    """
    Initialize territory scanning by getting user info and list of accounts to scan.
    
    This is the first step of the scanning process. Returns account IDs that
    can then be scanned individually with scan_account() for progress updates.
    
    Returns:
        Dict with:
        - success: bool
        - user: current user info (id, name, qualifier2)
        - role: detected role
        - account_ids: list of account IDs to scan
    """
    try:
        # 1. Get current user
        user_result = get_current_user()
        if not user_result.get("success"):
            return user_result
        
        user_id = user_result.get("user_id")
        user = user_result.get("user", {})
        user_name = user.get("fullname", "Unknown")
        user_qualifier2 = user.get("msp_qualifier2", "")
        
        # Determine role from qualifier2
        role_map = {
            "Cloud & AI Data": "Data SE",
            "Cloud & AI Infrastructure": "Infra SE",
            "Cloud & AI Apps": "Apps SE",
            "Cloud & AI": "Growth Seller",
            "Cloud & AI-Acq": "Acquisition Seller",
        }
        detected_role = role_map.get(user_qualifier2, f"Unknown ({user_qualifier2})")
        
        # 2. Query msp_accountteams where this user is a member
        relevant_qualifiers = ["Cloud & AI Data", "Cloud & AI Infrastructure", "Cloud & AI Apps", 
                               "Cloud & AI", "Cloud & AI-Acq"]
        
        team_result = query_entity(
            "msp_accountteams",
            select=["msp_accountteamid", "_msp_accountid_value", "msp_qualifier2"],
            filter_query=f"_msp_systemuserid_value eq {user_id}",
            top=500
        )
        
        if not team_result.get("success"):
            return team_result
        
        # 3. Filter to only SE/Seller roles and collect unique account IDs
        account_ids = set()
        for entry in team_result.get("records", []):
            qualifier2 = entry.get("msp_qualifier2", "")
            if qualifier2 in relevant_qualifiers:
                account_id = entry.get("_msp_accountid_value")
                if account_id:
                    account_ids.add(account_id)
        
        return {
            "success": True,
            "user": {"id": user_id, "name": user_name, "qualifier2": user_qualifier2},
            "role": detected_role,
            "total_accounts": len(account_ids),
            "account_ids": list(account_ids),
        }
        
    except Exception as e:
        logger.exception("Error initializing territory scan")
        return {"success": False, "error": str(e)}


def scan_account(account_id: str) -> Dict[str, Any]:
    """
    Scan a single account to get its territory information.
    
    Args:
        account_id: The account GUID to look up
        
    Returns:
        Dict with:
        - success: bool
        - account: account info (id, name, tpid)
        - territory: territory info (id, name, atu, pod, seller) or None
    """
    try:
        # 1. Get account with territory info
        acct_result = query_entity(
            "accounts",
            select=["accountid", "name", "msp_mstopparentid", "_territoryid_value"],
            filter_query=f"accountid eq {account_id}",
            top=1
        )
        
        if not acct_result.get("success"):
            return acct_result
        
        if not acct_result.get("records"):
            return {"success": True, "account": None, "territory": None}
        
        acct = acct_result["records"][0]
        account_info = {
            "id": acct.get("accountid"),
            "name": acct.get("name"),
            "tpid": acct.get("msp_mstopparentid"),
        }
        
        territory_id = acct.get("_territoryid_value")
        territory_name = acct.get("_territoryid_value@OData.Community.Display.V1.FormattedValue", "")
        
        if not territory_id:
            return {"success": True, "account": account_info, "territory": None}
        
        # 2. Get territory details
        terr_result = query_entity(
            "territories",
            select=["territoryid", "name", "msp_ownerid", "msp_salesunitname", "msp_accountteamunitname"],
            filter_query=f"territoryid eq {territory_id}",
            top=1
        )
        
        territory_info = {
            "id": territory_id,
            "name": territory_name,
        }
        
        if terr_result.get("success") and terr_result.get("records"):
            terr = terr_result["records"][0]
            territory_info["name"] = terr.get("name", territory_name)
            territory_info["seller_id"] = terr.get("msp_ownerid")
            territory_info["seller_name"] = terr.get("msp_ownerid@OData.Community.Display.V1.FormattedValue")
            territory_info["sales_unit"] = terr.get("msp_salesunitname")
            territory_info["atu"] = terr.get("msp_accountteamunitname")
            
            # Derive POD from territory name (e.g., "East.SMECC.MAA.0601" -> "East POD 06")
            # Note: some territories have suffixes like ".A" or ".B"
            # (e.g., "East.SMECC.HLA.0610.A"), so always use index 3
            name_parts = terr.get("name", "").split(".")
            if len(name_parts) >= 4:
                region = name_parts[0]
                territory_num = name_parts[3]
                if len(territory_num) >= 2:
                    pod_num = territory_num[:2]
                    territory_info["pod"] = f"{region} POD {pod_num}"
                    # Extract ATU code from name (e.g., "MAA" from "East.SMECC.MAA.0601")
                    territory_info["atu_code"] = name_parts[2] if len(name_parts) >= 3 else None
        
        return {
            "success": True,
            "account": account_info,
            "territory": territory_info,
        }
        
    except Exception as e:
        logger.exception(f"Error scanning account {account_id}")
        return {"success": False, "error": str(e)}


def get_account_details(account_id: str, territory_cache: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get full account details for import, including territory, seller, and verticals.
    
    Optimized approach with territory caching - many accounts share the same territory,
    so we cache territory lookups to avoid redundant API calls.
    
    Args:
        account_id: The account GUID
        territory_cache: Optional dict to cache territory lookups (territory_id -> result)
        
    Returns:
        Dict with account info, territory info, seller info, and verticals
    """
    try:
        # Get account with territory and verticals in one query
        acct_result = query_entity(
            "accounts",
            select=[
                "accountid", "name", "msp_mstopparentid", "_territoryid_value",
                "msp_verticalcode", "msp_verticalcategorycode"
            ],
            filter_query=f"accountid eq {account_id}",
            top=1
        )
        
        if not acct_result.get("success"):
            return acct_result
        
        if not acct_result.get("records"):
            return {"success": True, "account": None}
        
        acct = acct_result["records"][0]
        
        # Build account info
        account_info = {
            "id": acct.get("accountid"),
            "name": acct.get("name"),
            "tpid": acct.get("msp_mstopparentid"),
            "url": build_account_url(acct.get("accountid")),
            "vertical": acct.get("msp_verticalcode@OData.Community.Display.V1.FormattedValue"),
            "vertical_category": acct.get("msp_verticalcategorycode@OData.Community.Display.V1.FormattedValue"),
        }
        
        territory_id = acct.get("_territoryid_value")
        territory_name_hint = acct.get("_territoryid_value@OData.Community.Display.V1.FormattedValue", "")
        
        if not territory_id:
            return {
                "success": True,
                "account": account_info,
                "territory": None,
                "seller": None,
                "pod": None,
            }
        
        # Check territory cache first
        if territory_cache is not None and territory_id in territory_cache:
            cached = territory_cache[territory_id]
            return {
                "success": True,
                "account": account_info,
                "territory": cached.get("territory"),
                "seller": cached.get("seller"),
                "pod": cached.get("pod"),
            }
        
        # Get territory details (cache miss)
        terr_result = query_entity(
            "territories",
            select=["territoryid", "name", "msp_ownerid", "msp_salesunitname", "msp_accountteamunitname"],
            filter_query=f"territoryid eq {territory_id}",
            top=1
        )
        
        territory_info = None
        seller_info = None
        pod_name = None
        
        if terr_result.get("success") and terr_result.get("records"):
            terr = terr_result["records"][0]
            terr_name = terr.get("name", "")
            
            # Derive POD from territory name (e.g., "East.SMECC.MAA.0601" -> "East POD 06")
            # Note: some territories have suffixes like ".A" or ".B"
            # (e.g., "East.SMECC.HLA.0610.A"), so always use index 3
            name_parts = terr_name.split(".")
            if len(name_parts) >= 4:
                region = name_parts[0]
                territory_num = name_parts[3]
                if len(territory_num) >= 2:
                    pod_num = territory_num[:2]
                    pod_name = f"{region} POD {pod_num}"
            
            territory_info = {
                "id": territory_id,
                "name": terr_name,
                "atu": terr.get("msp_accountteamunitname"),
            }
            
            seller_id = terr.get("msp_ownerid")
            if seller_id:
                seller_info = {
                    "id": seller_id,
                    "name": terr.get("msp_ownerid@OData.Community.Display.V1.FormattedValue"),
                }
        
        # Cache the territory result for reuse
        if territory_cache is not None and territory_id:
            territory_cache[territory_id] = {
                "territory": territory_info,
                "seller": seller_info,
                "pod": pod_name,
            }
        
        return {
            "success": True,
            "account": account_info,
            "territory": territory_info,
            "seller": seller_info,
            "pod": pod_name,
        }
        
    except Exception as e:
        logger.exception(f"Error getting account details for {account_id}")
        return {"success": False, "error": str(e)}


def get_pod_team_members(sample_account_id: str) -> Dict[str, Any]:
    """
    Get all team members (SEs and sellers) for a POD by querying any account in that POD.
    
    This should only be called ONCE per POD since all accounts in a POD share SEs.
    
    Args:
        sample_account_id: Any account ID from the POD
        
    Returns:
        Dict with data_se, infra_se, apps_se, growth_seller, acq_seller
    """
    try:
        # Query all team members for this account
        team_result = query_entity(
            "msp_accountteams",
            select=["msp_accountteamid", "msp_fullname", "msp_qualifier2", "_msp_systemuserid_value"],
            filter_query=f"_msp_accountid_value eq {sample_account_id}",
            top=50
        )
        
        if not team_result.get("success"):
            return team_result
        
        # Parse team members by role
        team = {
            "data_se": None,
            "infra_se": None,
            "apps_se": None,
            "growth_seller": None,
            "acq_seller": None,
        }
        
        role_map = {
            "Cloud & AI Data": "data_se",
            "Cloud & AI Infrastructure": "infra_se",
            "Cloud & AI Apps": "apps_se",
            "Cloud & AI": "growth_seller",
            "Cloud & AI-Acq": "acq_seller",
        }
        
        for member in team_result.get("records", []):
            qualifier2 = member.get("msp_qualifier2", "")
            role_key = role_map.get(qualifier2)
            if role_key:
                # Extract alias from systemuser ID if available
                system_user_id = member.get("_msp_systemuserid_value")
                full_name = member.get("msp_fullname", "")
                
                # Try to get alias from the formatted value or user lookup
                alias = None
                if system_user_id:
                    # The alias is often the email prefix - we'll get it from user lookup later
                    alias_value = member.get("_msp_systemuserid_value@OData.Community.Display.V1.FormattedValue", "")
                    if "@" in alias_value:
                        alias = alias_value.split("@")[0].lower()
                
                team[role_key] = {
                    "name": full_name,
                    "user_id": system_user_id,
                    "alias": alias,
                }
        
        return {
            "success": True,
            "team": team,
        }
        
    except Exception as e:
        logger.exception(f"Error getting POD team for account {sample_account_id}")
        return {"success": False, "error": str(e)}


def get_seller_type_for_account(account_id: str, user_id: str) -> str:
    """
    Determine if an account's seller is Growth or Acquisition by checking the user's qualifier2.
    
    Args:
        account_id: The account GUID
        user_id: The seller's user GUID
        
    Returns:
        "Growth" or "Acquisition"
    """
    try:
        # Query msp_accountteams for this specific user on this account
        team_result = query_entity(
            "msp_accountteams",
            select=["msp_qualifier2"],
            filter_query=f"_msp_accountid_value eq {account_id} and _msp_systemuserid_value eq {user_id}",
            top=1
        )
        
        if team_result.get("success") and team_result.get("records"):
            qualifier2 = team_result["records"][0].get("msp_qualifier2", "")
            if qualifier2 == "Cloud & AI-Acq":
                return "Acquisition"
        
        return "Growth"  # Default to Growth
        
    except Exception:
        return "Growth"