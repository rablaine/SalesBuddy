"""
MSX Integration Routes.

Provides API endpoints for MSX (Dynamics 365) integration:
- Authentication status and device code flow
- Token refresh
- TPID account lookup
- Connection testing
- Streaming import from MSX (sequential and parallel)
"""

import json
import math
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from flask import Blueprint, jsonify, request, Response, g, current_app, stream_with_context
import logging

from app.services.msx_auth import (
    get_msx_auth_status,
    get_msx_token,
    refresh_token,
    clear_token_cache,
    start_token_refresh_job,
    start_device_code_flow,
    get_device_code_status,
    cancel_device_code_flow,
    get_az_cli_status,
    start_az_login,
    get_az_login_process_status,
    kill_az_login_process,
    set_subscription,
    az_logout,
    is_vpn_blocked,
    get_vpn_state,
    check_vpn_recovery,
)
from app.services.msx_api import (
    test_connection,
    lookup_account_by_tpid,
    get_milestones_by_account,
    get_opportunities_by_account,
    get_my_milestone_team_ids,
    extract_account_id_from_url,
    create_task,
    add_user_to_milestone_team,
    remove_user_from_milestone_team,
    TASK_CATEGORIES,
    query_entity,
    get_current_user,
    get_entity_metadata,
    explore_user_territories,
    get_my_accounts,
    get_accounts_for_territories,
    search_territories,
    find_my_territories,
    scan_init,
    scan_account,
    get_account_details,
    get_pod_team_members,
    get_seller_type_for_account,
    batch_query_accounts,
    batch_query_territories,
    batch_query_account_teams,
    batch_query_account_csams,
    batch_query_account_dss,
    build_account_url,
    get_user_alias,
    get_user_info,
)
from app.models import Customer, CustomerCSAM, Milestone, Opportunity, Territory, Seller, POD, SolutionEngineer, SyncStatus, Vertical, db


logger = logging.getLogger(__name__)


def _extract_domain(url_or_domain: str) -> str:
    """Extract a clean domain from a URL or bare domain string.

    Handles MSX websiteurl values like:
      'http://www.example.com'  -> 'example.com'
      'https://example.com/foo' -> 'example.com'
      'example.com'             -> 'example.com'
      'www.example.com'         -> 'example.com'

    Returns:
        Clean domain string, or empty string if unparseable.
    """
    if not url_or_domain:
        return ""
    raw = url_or_domain.strip()
    # Add scheme if missing so urlparse works
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
    except Exception:
        return ""
    # Strip www. prefix
    if host.startswith("www."):
        host = host[4:]
    # Basic sanity: must contain at least one dot
    if "." not in host:
        return ""
    return host.lower()


msx_bp = Blueprint('msx', __name__, url_prefix='/api/msx')


@msx_bp.after_request
def set_vpn_blocked_status(response):
    """Return 403 status on JSON responses that indicate VPN/IP block.

    This ensures the global fetch interceptor in base.html can detect
    VPN blocks from any MSX endpoint, even when the route handler
    returns a 200 with an error in the JSON body.
    """
    if response.content_type and 'application/json' in response.content_type:
        if response.status_code == 200:
            try:
                data = json.loads(response.get_data(as_text=True))
                if data and data.get('vpn_blocked'):
                    response.status_code = 403
            except Exception:
                pass
    return response


@msx_bp.route('/status')
def auth_status():
    """Get current MSX authentication status."""
    status = get_msx_auth_status()
    
    # Convert datetime to ISO string for JSON
    if status.get("expires_on"):
        status["expires_on"] = status["expires_on"].isoformat()
    if status.get("last_refresh"):
        status["last_refresh"] = status["last_refresh"].isoformat()
    
    return jsonify(status)


@msx_bp.route('/refresh', methods=['POST'])
def refresh():
    """Manually refresh the MSX token."""
    success = refresh_token()
    status = get_msx_auth_status()
    
    # Convert datetime to ISO string for JSON
    if status.get("expires_on"):
        status["expires_on"] = status["expires_on"].isoformat()
    if status.get("last_refresh"):
        status["last_refresh"] = status["last_refresh"].isoformat()
    
    return jsonify({
        "success": success,
        "status": status
    })


@msx_bp.route('/clear', methods=['POST'])
def clear():
    """Clear cached MSX tokens."""
    clear_token_cache()
    return jsonify({"success": True, "message": "Token cache cleared"})


@msx_bp.route('/vpn-status')
def vpn_status():
    """Get current VPN blocked state."""
    state = get_vpn_state()
    if state.get("blocked_at"):
        state["blocked_at"] = state["blocked_at"].isoformat()
    if state.get("last_check"):
        state["last_check"] = state["last_check"].isoformat()
    return jsonify(state)


@msx_bp.route('/vpn-check', methods=['POST'])
def vpn_check():
    """User says they're back on VPN — test MSX and clear block if OK."""
    result = check_vpn_recovery()
    return jsonify(result)


@msx_bp.route('/test')
def test():
    """Test MSX connection by calling WhoAmI."""
    result = test_connection()
    return jsonify(result)


@msx_bp.route('/lookup-tpid/<tpid>')
def lookup_tpid(tpid: str):
    """
    Look up an MSX account by TPID.
    
    Args (query params):
        customer_name: Optional customer name for better matching.
    
    Returns:
        JSON with accounts found and direct MSX URL if exactly one match.
    """
    customer_name = request.args.get('customer_name')
    result = lookup_account_by_tpid(tpid, customer_name=customer_name)
    return jsonify(result)


@msx_bp.route('/start-refresh-job', methods=['POST'])
def start_refresh():
    """Start the background token refresh job."""
    interval = request.json.get('interval', 300) if request.is_json else 300
    start_token_refresh_job(interval_seconds=interval)
    return jsonify({"success": True, "message": f"Refresh job started (interval: {interval}s)"})


@msx_bp.route('/device-code/start', methods=['POST'])
def device_code_start():
    """
    Start the device code authentication flow.
    
    Returns the device code and URL for the user to complete login.
    """
    result = start_device_code_flow()
    return jsonify(result)


@msx_bp.route('/device-code/status')
def device_code_status():
    """
    Check the status of an active device code flow.
    
    Poll this endpoint to know when the user has completed login.
    """
    status = get_device_code_status()
    
    # If completed successfully, also return the updated auth status
    if status.get("completed") and status.get("success"):
        auth_status = get_msx_auth_status()
        if auth_status.get("expires_on"):
            auth_status["expires_on"] = auth_status["expires_on"].isoformat()
        if auth_status.get("last_refresh"):
            auth_status["last_refresh"] = auth_status["last_refresh"].isoformat()
        status["auth_status"] = auth_status
    
    return jsonify(status)


@msx_bp.route('/device-code/cancel', methods=['POST'])
def device_code_cancel():
    """Cancel any active device code flow."""
    cancel_device_code_flow()
    return jsonify({"success": True, "message": "Device code flow cancelled"})


# -----------------------------------------------------------------------------
# Browser-based az login flow
# -----------------------------------------------------------------------------

@msx_bp.route('/az-status')
def az_cli_status():
    """Check Azure CLI install & login status (no CRM token needed).

    Returns az_installed, logged_in, user_email, message.
    """
    return jsonify(get_az_cli_status())


@msx_bp.route('/az-login/start', methods=['POST'])
def az_login_start():
    """Launch ``az login --tenant ...`` in a visible console window.

    Accepts an optional JSON body ``{"scope": "api://…/.default"}``
    to include an OAuth scope in the login command, which triggers
    user consent for that resource (e.g. the AI gateway).
    """
    scope = None
    if request.is_json and request.json:
        scope = request.json.get("scope")
    result = start_az_login(scope=scope)

    # If already logged in, also set subscription and grab a CRM token
    if result.get("success"):
        status = get_az_cli_status()
        if status.get("logged_in"):
            set_subscription()
            refresh_token()

    return jsonify(result)


@msx_bp.route('/az-logout', methods=['POST'])
def az_logout_endpoint():
    """Sign out of Azure CLI (used when wrong tenant detected)."""
    return jsonify(az_logout())


@msx_bp.route('/clear-cli-cache', methods=['POST'])
def clear_cli_cache():
    """Clear the cached Azure CLI installed check.

    Used when retrying after a transient failure (timeout, etc.).
    """
    from app.services.msx_auth import _az_cli_installed_cache
    _az_cli_installed_cache["installed"] = None
    _az_cli_installed_cache["last_error"] = None
    return jsonify({"success": True})


@msx_bp.route('/az-login/status')
def az_login_status():
    """Poll the az login process status (instant, no subprocess calls).

    Returns running, exit_code, elapsed_seconds so the frontend can
    detect success/failure immediately when the process exits.
    """
    return jsonify(get_az_login_process_status())


@msx_bp.route('/az-login/complete', methods=['POST'])
def az_login_complete():
    """Called by the frontend once polling detects a successful login.

    Sets the subscription, refreshes the CRM token, and clears the
    AI gateway token cache so the new consent is picked up immediately.
    """
    status = get_az_cli_status()
    if not status.get("logged_in"):
        return jsonify({"success": False, "error": "Not logged in yet"}), 400

    set_subscription()
    token_ok = refresh_token()

    # Kill the az login process — it's still running in a console window
    # waiting for subscription selection, and we don't need it anymore.
    kill_az_login_process()

    # Clear gateway token cache so fresh consent is used
    from app.gateway_client import clear_token_cache as clear_gw_cache
    clear_gw_cache()

    auth = get_msx_auth_status()

    # Serialise datetimes
    if auth.get("expires_on"):
        auth["expires_on"] = auth["expires_on"].isoformat()
    if auth.get("last_refresh"):
        auth["last_refresh"] = auth["last_refresh"].isoformat()

    return jsonify({
        "success": token_ok,
        "user_email": status.get("user_email"),
        "auth_status": auth,
    })


# -----------------------------------------------------------------------------
# Milestone Routes
# -----------------------------------------------------------------------------

@msx_bp.route('/milestones/<account_id>')
def get_milestones(account_id: str):
    """
    Get all milestones for an account.
    
    Returns milestones sorted by status (Active first, then Blocked, Completed, etc.)
    with indication of whether each milestone is used in any call logs.
    """
    result = get_milestones_by_account(account_id)
    
    if not result.get("success"):
        return jsonify(result)
    
    # Check which milestones are already used in call logs
    milestones = result.get("milestones", [])
    for milestone_data in milestones:
        msx_milestone_id = milestone_data.get("id")
        if msx_milestone_id:
            # Check if any milestone in our DB has this MSX ID
            existing = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            if existing and existing.notes:
                milestone_data["used_in_notes"] = len(existing.notes)
                milestone_data["local_milestone_id"] = existing.id
            else:
                milestone_data["used_in_notes"] = 0
                milestone_data["local_milestone_id"] = existing.id if existing else None
    
    return jsonify(result)


@msx_bp.route('/milestones-for-customer/<int:customer_id>')
def get_milestones_for_customer(customer_id: int):
    """
    Get all milestones for a customer using their TPID URL.
    
    Extracts the account ID from the customer's tpid_url and fetches milestones.
    """
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({"success": False, "error": "Customer not found"})
    
    if not customer.tpid_url:
        return jsonify({
            "success": False, 
            "error": "Customer has no MSX account linked",
            "needs_tpid": True
        })
    
    # Extract account ID from the tpid_url
    account_id = extract_account_id_from_url(customer.tpid_url)
    if not account_id:
        return jsonify({
            "success": False,
            "error": "Could not extract account ID from customer's MSX URL"
        })
    
    # Get milestones
    result = get_milestones_by_account(account_id)
    
    if not result.get("success"):
        return jsonify(result)
    
    # Get live team membership from MSX (best-effort, fall back to local DB)
    my_team_ids: set = set()
    try:
        team_result = get_my_milestone_team_ids()
        if team_result.get("success"):
            my_team_ids = team_result.get("milestone_ids", set())
    except Exception:
        logger.debug("Could not fetch live team membership, falling back to local DB")
    
    # Enrich milestones with local metadata and team membership
    milestones = result.get("milestones", [])
    for milestone_data in milestones:
        msx_milestone_id = milestone_data.get("id")
        if msx_milestone_id:
            existing = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            if existing and existing.notes:
                milestone_data["used_in_notes"] = len(existing.notes)
                milestone_data["local_milestone_id"] = existing.id
            else:
                milestone_data["used_in_notes"] = 0
                milestone_data["local_milestone_id"] = existing.id if existing else None
            # Use live MSX team membership if available, fall back to local record
            if my_team_ids:
                milestone_data["on_my_team"] = msx_milestone_id.lower() in my_team_ids
            else:
                milestone_data["on_my_team"] = existing.on_my_team if existing else False
    
    return jsonify(result)


@msx_bp.route('/opportunities-for-customer/<int:customer_id>')
def get_opportunities_for_customer(customer_id: int):
    """
    Get open opportunities for a customer using their TPID URL.

    Extracts the account ID from the customer's tpid_url and fetches
    open opportunities from MSX.
    """
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({"success": False, "error": "Customer not found"})

    if not customer.tpid_url:
        return jsonify({
            "success": False,
            "error": "Customer has no MSX account linked",
            "needs_tpid": True
        })

    account_id = extract_account_id_from_url(customer.tpid_url)
    if not account_id:
        return jsonify({
            "success": False,
            "error": "Could not extract account ID from customer's MSX URL"
        })

    result = get_opportunities_by_account(account_id)

    if not result.get("success"):
        return jsonify(result)

    # Enrich with local metadata (which opportunities are already linked to notes)
    opportunities = result.get("opportunities", [])
    for opp_data in opportunities:
        msx_opp_id = opp_data.get("id")
        if msx_opp_id:
            existing = Opportunity.query.filter_by(
                msx_opportunity_id=msx_opp_id
            ).first()
            if existing and existing.notes:
                opp_data["used_in_notes"] = len(existing.notes)
                opp_data["local_opportunity_id"] = existing.id
            else:
                opp_data["used_in_notes"] = 0
                opp_data["local_opportunity_id"] = existing.id if existing else None

    return jsonify(result)


# -----------------------------------------------------------------------------
# Task Routes
# -----------------------------------------------------------------------------

@msx_bp.route('/task-categories')
def get_task_categories():
    """
    Get all available task categories.
    
    Returns categories with HOK flags for UI highlighting.
    """
    return jsonify({
        "success": True,
        "categories": TASK_CATEGORIES
    })


@msx_bp.route('/tasks', methods=['POST'])
def create_msx_task():
    """
    Create a task on a milestone in MSX.
    
    Expected JSON body:
        milestone_id: MSX milestone GUID
        subject: Task title
        task_category: Category code (e.g., 861980004)
        duration_minutes: Duration in minutes (default: 60)
        description: Optional task description
    
    Returns:
        task_id: MSX task GUID
        task_url: Direct URL to the task in MSX
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    
    data = request.json
    milestone_id = data.get("milestone_id")
    subject = data.get("subject")
    task_category = data.get("task_category")
    duration_minutes = data.get("duration_minutes", 60)
    description = data.get("description")
    due_date = data.get("due_date")
    
    if not milestone_id:
        return jsonify({"success": False, "error": "milestone_id required"}), 400
    if not subject:
        return jsonify({"success": False, "error": "subject required"}), 400
    if not task_category:
        return jsonify({"success": False, "error": "task_category required"}), 400
    
    result = create_task(
        milestone_id=milestone_id,
        subject=subject,
        task_category=task_category,
        duration_minutes=duration_minutes,
        description=description,
        due_date=due_date
    )
    
    # Auto-join the milestone team if task creation succeeded
    if result.get('success'):
        try:
            join_result = add_user_to_milestone_team(milestone_id)
            if join_result.get('success'):
                # Update local milestone record if it exists
                local_ms = Milestone.query.filter_by(msx_milestone_id=milestone_id).first()
                if local_ms:
                    local_ms.on_my_team = True
                    db.session.commit()
                result['joined_team'] = True
                logger.info(f'Auto-joined milestone team for {milestone_id}')
            elif 'already' in join_result.get('error', '').lower():
                result['joined_team'] = True  # Already on team
            else:
                result['joined_team'] = False
                logger.warning(f'Could not auto-join milestone team: {join_result.get("error")}')
        except Exception as e:
            result['joined_team'] = False
            logger.warning(f'Auto-join milestone team failed (non-blocking): {e}')
    
    return jsonify(result)


# -----------------------------------------------------------------------------
# Team Membership Routes
# -----------------------------------------------------------------------------

@msx_bp.route('/join-milestone-team', methods=['POST'])
def join_milestone_team():
    """
    Add the current user to a milestone's access team in MSX.

    Expected JSON body:
        milestone_id: local milestone ID (integer)

    Updates the local on_my_team flag on success.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    from app.models import db, Milestone

    milestone_id = request.json.get("milestone_id")
    if not milestone_id:
        return jsonify({"success": False, "error": "milestone_id required"}), 400

    milestone = Milestone.query.get(milestone_id)
    if not milestone:
        return jsonify({"success": False, "error": "Milestone not found"}), 404

    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "Milestone has no MSX ID"}), 400

    result = add_user_to_milestone_team(milestone.msx_milestone_id)

    if result.get("success"):
        milestone.on_my_team = True
        db.session.commit()

    return jsonify(result)


@msx_bp.route('/leave-milestone-team', methods=['POST'])
def leave_milestone_team():
    """
    Remove the current user from a milestone's access team in MSX.

    Expected JSON body:
        milestone_id: local milestone ID (integer)

    Updates the local on_my_team flag on success.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    from app.models import db, Milestone

    milestone_id = request.json.get("milestone_id")
    if not milestone_id:
        return jsonify({"success": False, "error": "milestone_id required"}), 400

    milestone = Milestone.query.get(milestone_id)
    if not milestone:
        return jsonify({"success": False, "error": "Milestone not found"}), 404

    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "Milestone has no MSX ID"}), 400

    result = remove_user_from_milestone_team(milestone.msx_milestone_id)

    if result.get("success"):
        milestone.on_my_team = False
        db.session.commit()

    return jsonify(result)


@msx_bp.route('/join-deal-team', methods=['POST'])
def join_deal_team():
    """
    Add the current user to an opportunity's deal team in MSX.

    Expected JSON body:
        opportunity_id: local opportunity ID (integer)

    Updates the local on_deal_team flag on success.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    from app.models import db, Opportunity
    from app.services.msx_api import add_user_to_deal_team

    opportunity_id = request.json.get("opportunity_id")
    if not opportunity_id:
        return jsonify({"success": False, "error": "opportunity_id required"}), 400

    opportunity = Opportunity.query.get(opportunity_id)
    if not opportunity:
        return jsonify({"success": False, "error": "Opportunity not found"}), 404

    if not opportunity.msx_opportunity_id:
        return jsonify({"success": False, "error": "Opportunity has no MSX ID"}), 400

    result = add_user_to_deal_team(opportunity.msx_opportunity_id)

    if result.get("success"):
        opportunity.on_deal_team = True
        db.session.commit()

    return jsonify(result)


# -----------------------------------------------------------------------------
# Exploration / Schema Discovery Routes
# -----------------------------------------------------------------------------

@msx_bp.route('/explore/me')
def explore_me():
    """
    Get the current authenticated user's details.
    
    Returns user ID, name, email, title, and other profile info.
    """
    result = get_current_user()
    return jsonify(result)


@msx_bp.route('/explore/my-accounts')
def explore_my_accounts():
    """
    Get all accounts the current user has access to via team memberships.
    
    Uses the pattern: user → teammemberships → teams → accounts
    
    Returns list of accounts with name, TPID, owner, and ATU info.
    """
    result = get_my_accounts()
    return jsonify(result)


@msx_bp.route('/explore/accounts-by-territory', methods=['POST'])
def explore_accounts_by_territory():
    """
    Get all accounts for specified territories.
    
    POST JSON body:
        territories: List of territory names (e.g., ["East.SMECC.SDP.0603"])
    
    Returns accounts with name, TPID, seller, and territory info.
    
    This is the recommended approach for seeding the database - provide
    your known territory names and get all accounts for those territories.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    
    territories = request.json.get("territories", [])
    if not territories:
        return jsonify({"success": False, "error": "territories list required"}), 400
    
    result = get_accounts_for_territories(territories)
    return jsonify(result)


@msx_bp.route('/explore/search-territories')
def search_territories_route():
    """
    Search for territories by partial name match.
    
    Query params:
        q: Search query (partial territory name, e.g., "0602" or "MAA")
        top: Max results (default 50)
    
    Returns list of matching territories with id, name, and seller info.
    """
    query = request.args.get('q', '')
    if not query or len(query) < 2:
        return jsonify({"success": False, "error": "Query must be at least 2 characters"}), 400
    
    try:
        top = min(int(request.args.get('top', 50)), 100)
    except ValueError:
        top = 50
    
    result = search_territories(query, top=top)
    return jsonify(result)


@msx_bp.route('/explore/territories')
def explore_territories():
    """
    Explore what territories and accounts the current user has access to.
    
    Tries multiple discovery approaches to find assignments.
    """
    result = explore_user_territories()
    return jsonify(result)


@msx_bp.route('/explore/my-territories')
def my_territories():
    """
    Find territories where the current user is assigned as SE or Seller.
    
    Query params:
        atu: Optional ATU code to filter by (e.g., "MAA", "HLA", "SDP")
             If provided, only scans territories matching "East.SMECC.{atu}.*"
    
    Uses msp_accountteams to find account assignments and derives:
    - User's role (Data SE, Infra SE, Apps SE, Growth Seller, Acq Seller)
    - Territories the user is assigned to
    - POD grouping (derived from territory suffix, e.g., 0601 -> POD 06)
    - Sample account per territory for verification
    """
    atu_filter = request.args.get('atu', None)
    result = find_my_territories(atu_filter=atu_filter)
    return jsonify(result)


@msx_bp.route('/explore/scan-init')
def scan_init_route():
    """
    Initialize territory scanning - returns user info and list of account IDs to scan.
    
    This is the first step of progressive scanning. Call this once, then call
    /explore/scan-account/<id> for each account to get per-account progress.
    
    Returns:
        - user: current user info
        - role: detected role
        - account_ids: list of account GUIDs to scan
        - total_assignments: total found (may be more than returned due to 100 limit)
    """
    result = scan_init()
    return jsonify(result)


@msx_bp.route('/explore/scan-account/<account_id>')
def scan_account_route(account_id: str):
    """
    Scan a single account to get its territory information.
    
    Args:
        account_id: The account GUID to look up
        
    Returns:
        - account: {id, name, tpid}
        - territory: {id, name, atu, atu_code, pod, seller_id, seller_name} or null
    """
    result = scan_account(account_id)
    return jsonify(result)


@msx_bp.route('/explore/entity/<entity_name>')
def explore_entity(entity_name: str):
    """
    Query any MSX entity for data discovery.
    
    Query params:
        select: Comma-separated field names (optional)
        filter: OData $filter expression (optional)
        expand: OData $expand expression (optional)
        top: Max records (default 10, max 100)
        orderby: OData $orderby expression (optional)
    
    Examples:
        /api/msx/explore/entity/accounts?top=5&select=name,msp_mstopparentid
        /api/msx/explore/entity/systemusers?filter=contains(fullname,'Alex')
        /api/msx/explore/entity/territories?top=20
    """
    select = request.args.get('select')
    filter_query = request.args.get('filter')
    expand = request.args.get('expand')
    order_by = request.args.get('orderby')
    
    try:
        top = min(int(request.args.get('top', 10)), 100)
    except ValueError:
        top = 10
    
    select_list = select.split(',') if select else None
    
    result = query_entity(
        entity_name=entity_name,
        select=select_list,
        filter_query=filter_query,
        expand=expand,
        top=top,
        order_by=order_by
    )
    return jsonify(result)


@msx_bp.route('/explore/metadata/<entity_name>')
def explore_metadata(entity_name: str):
    """
    Get the schema/metadata for an entity to discover available fields.
    
    Examples:
        /api/msx/explore/metadata/account
        /api/msx/explore/metadata/systemuser
        /api/msx/explore/metadata/territory
        /api/msx/explore/metadata/msp_milestone
    
    Note: Use singular logical name (account, not accounts).
    """
    result = get_entity_metadata(entity_name)
    return jsonify(result)


# -----------------------------------------------------------------------------
# API Import Routes
# -----------------------------------------------------------------------------

@msx_bp.route('/import-accounts', methods=['POST'])
def import_accounts():
    """
    Import accounts from MSX into Sales Buddy database.
    
    Creates territories, sellers, and customers based on the provided data.
    
    POST JSON body:
        accounts: List of account objects from get_accounts_for_territories
        territories: List of selected territory objects with seller info
    
    Returns:
        success: bool
        territories_created: count
        sellers_created: count
        customers_created: count
        customers_skipped: count (already existed)
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    
    data = request.json
    accounts = data.get("accounts", [])
    territories_data = data.get("territories", [])
    
    if not accounts:
        return jsonify({"success": False, "error": "No accounts provided"}), 400
    
    try:
        SyncStatus.mark_started('accounts')

        territories_created = 0
        sellers_created = 0
        customers_created = 0
        customers_skipped = 0
        
        # Track created entities to avoid duplicates within this import
        territory_map = {}  # name -> Territory object
        seller_map = {}  # name -> Seller object
        
        # 1. Create territories
        for t_data in territories_data:
            territory_name = t_data.get("name")
            if not territory_name:
                continue
            
            # Check if territory already exists
            territory = Territory.query.filter_by(name=territory_name).first()
            if not territory:
                territory = Territory(name=territory_name)
                db.session.add(territory)
                territories_created += 1
                logger.info(f"Created territory: {territory_name}")
            
            territory_map[territory_name] = territory
            
            # Create seller from territory owner if we have the info
            seller_name = t_data.get("seller_name")
            if seller_name and seller_name not in seller_map:
                seller = Seller.query.filter_by(name=seller_name).first()
                if not seller:
                    seller = Seller(name=seller_name)
                    db.session.add(seller)
                    sellers_created += 1
                    logger.info(f"Created seller: {seller_name}")
                seller_map[seller_name] = seller
        
        db.session.flush()  # Flush to get IDs for the territories and sellers
        
        # 2. Upsert customers from accounts
        customers_updated = 0
        customers_unchanged = 0
        seen_tpids = set()  # In-memory dedup for same-TPID accounts in batch

        # Pre-load existing customers keyed by TPID for upsert comparison.
        # Normalize to int -- old imports may have stored TPIDs as strings.
        existing_customers_by_tpid: dict = {}
        for cust in Customer.query.all():
            try:
                val = int(cust.tpid) if cust.tpid is not None else None
            except (ValueError, TypeError):
                val = cust.tpid
            if val is not None:
                existing_customers_by_tpid[val] = cust

        with db.session.no_autoflush:
            for acct in accounts:
                raw_tpid = acct.get("tpid")
                # Cast to int (API may return strings, column is BigInteger)
                try:
                    tpid = int(raw_tpid) if raw_tpid else None
                except (ValueError, TypeError):
                    tpid = None
                customer_name = acct.get("name")
                territory_name = acct.get("territory_name")
                seller_name = acct.get("seller_name")

                if not tpid or not customer_name:
                    continue

                # Skip duplicates within the same import batch
                if tpid in seen_tpids:
                    customers_skipped += 1
                    continue
                seen_tpids.add(tpid)

                # Check if customer already exists by TPID
                if tpid in existing_customers_by_tpid:
                    # --- Upsert existing customer ---
                    cust = existing_customers_by_tpid[tpid]
                    changed = False

                    if customer_name and cust.name != customer_name:
                        cust.name = customer_name
                        changed = True

                    new_territory = territory_map.get(territory_name) if territory_name else None
                    if new_territory and cust.territory != new_territory:
                        cust.territory = new_territory
                        changed = True

                    new_seller = seller_map.get(seller_name) if seller_name else None
                    if new_seller and cust.seller != new_seller:
                        cust.seller = new_seller
                        changed = True

                    if changed:
                        customers_updated += 1
                    else:
                        customers_unchanged += 1

                    continue

                # Create new customer
                customer = Customer(
                    name=customer_name,
                    tpid=tpid
                )

                # Associate with territory if we have it
                if territory_name and territory_name in territory_map:
                    customer.territory = territory_map[territory_name]

                # Associate with seller if we have it
                if seller_name and seller_name in seller_map:
                    customer.seller = seller_map[seller_name]

                db.session.add(customer)
                existing_customers_by_tpid[tpid] = customer
                customers_created += 1

        db.session.commit()

        SyncStatus.mark_completed(
            'accounts', success=True,
            items_synced=customers_created,
            details=f'{customers_created} created, {customers_updated} updated, {customers_unchanged} unchanged, {customers_skipped} skipped',
        )
        
        logger.info(f"API Import complete: {territories_created} territories, {sellers_created} sellers, {customers_created} customers created, {customers_updated} updated, {customers_unchanged} unchanged, {customers_skipped} skipped")
        
        return jsonify({
            "success": True,
            "territories_created": territories_created,
            "sellers_created": sellers_created,
            "customers_created": customers_created,
            "customers_skipped": customers_skipped,
            "customers_updated": customers_updated,
            "customers_unchanged": customers_unchanged,
        })
        
    except Exception as e:
        db.session.rollback()
        SyncStatus.mark_completed('accounts', success=False, details=str(e))
        logger.exception("Error importing accounts from MSX")
        return jsonify({"success": False, "error": str(e)}), 500


# -----------------------------------------------------------------------------
# Streaming MSX Import (SSE) -- 3 concurrent workers
# -----------------------------------------------------------------------------

# Parallel worker helpers
_PARALLEL_WORKERS = 3
_ACCT_BATCH = 15
_TEAM_BATCH = 3


def _split_chunks(items: list, n: int) -> list:
    """Split *items* into *n* roughly-equal chunks."""
    k, m = divmod(len(items), n)
    return [
        items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)]
        for i in range(n)
    ]


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"


def _drain(q: queue.Queue) -> list:
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            break
    return events


def _par_query_accounts(account_ids, batch_size, progress_q, worker_id):
    """Worker: query account details for a chunk of IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying accounts - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    accounts = {}
    batches = math.ceil(len(account_ids) / batch_size)
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            filter_parts = [f"accountid eq {aid}" for aid in batch]
            result = query_entity(
                "accounts",
                select=["accountid", "name", "msp_mstopparentid",
                        "_territoryid_value", "msp_verticalcode",
                        "msp_verticalcategorycode", "websiteurl",
                        "msp_parentinglevelcode", "_ownerid_value"],
                filter_query=" or ".join(filter_parts),
                top=batch_size + 5,
            )
            if result.get("success"):
                for rec in result.get("records", []):
                    aid = rec.get("accountid")
                    if aid:
                        accounts[aid] = rec
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "fetched": len(accounts)})
    finally:
        msx_retry_state.callback = None
    return accounts


def _par_query_territories(territory_ids, batch_size, progress_q, worker_id):
    """Worker: query territory details for a chunk of IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying territories - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    territories = {}
    batches = math.ceil(len(territory_ids) / batch_size) if territory_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(territory_ids), batch_size), start=1):
            batch = territory_ids[i:i + batch_size]
            filter_parts = [f"territoryid eq {tid}" for tid in batch]
            result = query_entity(
                "territories",
                select=["territoryid", "name", "msp_ownerid",
                        "msp_salesunitname", "msp_accountteamunitname"],
                filter_query=" or ".join(filter_parts),
                top=batch_size + 5,
            )
            if result.get("success"):
                for rec in result.get("records", []):
                    tid = rec.get("territoryid")
                    if tid:
                        territories[tid] = rec
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "fetched": len(territories)})
    finally:
        msx_retry_state.callback = None
    return territories


def _par_query_teams(account_ids, batch_size, progress_q, worker_id):
    """Worker: query account teams for a chunk of account IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying teams - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    account_sellers, unique_sellers, account_ses = {}, {}, {}
    batches = math.ceil(len(account_ids) / batch_size) if account_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            teams_result = batch_query_account_teams(batch, batch_size=len(batch))
            if teams_result.get("success"):
                account_sellers.update(teams_result.get("account_sellers", {}))
                unique_sellers.update(teams_result.get("unique_sellers", {}))
                account_ses.update(teams_result.get("account_ses", {}))
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "sellers_found": len(unique_sellers)})
    finally:
        msx_retry_state.callback = None
    return {"account_sellers": account_sellers,
            "unique_sellers": unique_sellers,
            "account_ses": account_ses}


_CSAM_BATCH = 10  # CSAMs are rare (~0-3 per account), safe to batch more


def _par_query_csams(account_ids, batch_size, progress_q, worker_id):
    """Worker: query CSAM team members for a chunk of account IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying CSAMs - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    account_csams: dict = {}
    unique_csams: dict = {}
    batches = math.ceil(len(account_ids) / batch_size) if account_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            csam_result = batch_query_account_csams(batch, batch_size=len(batch))
            if csam_result.get("success"):
                account_csams.update(csam_result.get("account_csams", {}))
                unique_csams.update(csam_result.get("unique_csams", {}))
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "csams_found": len(unique_csams)})
    finally:
        msx_retry_state.callback = None
    return {"account_csams": account_csams, "unique_csams": unique_csams}


_DSS_BATCH = 10  # DSSs are sparse (~0-3 per account), safe to batch more


def _par_query_dss(account_ids, batch_size, progress_q, worker_id):
    """Worker: query DSS team members for a chunk of account IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying DSSs - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    account_dss: dict = {}
    unique_dss: dict = {}
    batches = math.ceil(len(account_ids) / batch_size) if account_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            dss_result = batch_query_account_dss(batch, batch_size=len(batch))
            if dss_result.get("success"):
                account_dss.update(dss_result.get("account_dss", {}))
                unique_dss.update(dss_result.get("unique_dss", {}))
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "dss_found": len(unique_dss)})
    finally:
        msx_retry_state.callback = None
    return {"account_dss": account_dss, "unique_dss": unique_dss}


@msx_bp.route('/import-stream')
def import_stream():
    """
    Stream import all accounts/data from MSX into Sales Buddy database.

    Uses 3 concurrent workers for the API query phases (accounts,
    territories, teams) then writes to the database sequentially.
    Sends Server-Sent Events (SSE) to stream progress updates.
    """
    token = get_msx_token()
    if not token:
        logger.warning("import-stream: No MSX token available")
        return jsonify({
            "error": "Not authenticated with MSX. Complete Step 2 (Sign in with Azure) first.",
            "auth_required": True,
        }), 401

    try:
        user_check = db.session.execute(db.text("SELECT id FROM users LIMIT 1")).fetchone()
        if not user_check:
            return jsonify({"error": "Database not initialized. No user record found."}), 500
    except Exception as e:
        logger.exception("import-stream: Database check failed")
        return jsonify({"error": f"Database error: {e}"}), 500

    def generate():
        phase = "initializing"
        try:
            import_start_time = time.time()
            progress_q: queue.Queue = queue.Queue()

            yield _sse({"message": "Starting parallel MSX import...", "progress": 0})

            # ----------------------------------------------------------
            # Phase 1: scan_init (single-threaded)
            # ----------------------------------------------------------
            phase = "fetching account assignments"
            yield _sse({"message": "Fetching your account assignments from MSX...", "progress": 1})

            # Wire up retry callback so SSE stream can report timeouts/retries
            from app.services.msx_api import msx_retry_state
            retry_messages = []  # Collect retry events from the callback
            def _on_retry(attempt, max_retries, wait_secs, error_type):
                retry_messages.append(
                    f"Fetching assignments - timeout, retrying ({attempt}/{max_retries})..."
                )
            msx_retry_state.callback = _on_retry
            try:
                init_result = scan_init()
            finally:
                msx_retry_state.callback = None

            # Flush any retry messages as SSE events
            for msg in retry_messages:
                yield _sse({"message": msg, "progress": 1})
            if not init_result.get("success"):
                error_msg = init_result.get("error", "Failed to initialize scan")
                if init_result.get("vpn_blocked") or is_vpn_blocked():
                    yield _sse({"error": error_msg, "vpn_blocked": True})
                elif init_result.get("msx_outage"):
                    yield _sse({"error": error_msg, "msx_outage": True})
                else:
                    yield _sse({"error": error_msg})
                return

            account_ids = init_result.get("account_ids", [])
            user_info = init_result.get("user", {})
            role = init_result.get("role", "Unknown")
            yield _sse({
                "message": f"Found {len(account_ids)} accounts to import...",
                "user": user_info.get("name"), "role": role, "progress": 1,
            })

            if not account_ids:
                yield _sse({"error": "No accounts found for this user."})
                return

            # ----------------------------------------------------------
            # Phase 2: Parallel account queries (3 workers)
            # ----------------------------------------------------------
            phase = "querying accounts"
            chunks = _split_chunks(account_ids, _PARALLEL_WORKERS)
            total_batches = sum(math.ceil(len(c) / _ACCT_BATCH) for c in chunks if c)
            completed = 0

            yield _sse({
                "message": f"Querying {len(account_ids)} accounts in parallel ({_PARALLEL_WORKERS} workers)...",
                "progress": 1,
            })

            accounts_raw: dict = {}
            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_par_query_accounts, chunk, _ACCT_BATCH, progress_q, idx + 1)
                    for idx, chunk in enumerate(chunks) if chunk
                ]
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        completed += 1
                        pct = 1 + int((completed / max(total_batches, 1)) * 7)
                        yield _sse({
                            "message": f"Querying accounts... ({evt['fetched']} fetched)",
                            "progress": min(pct, 8),
                        })
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    completed += 1
                    pct = 1 + int((completed / max(total_batches, 1)) * 7)
                    yield _sse({
                        "message": f"Querying accounts... ({evt['fetched']} fetched)",
                        "progress": min(pct, 8),
                    })
                for f in futures:
                    accounts_raw.update(f.result())

            if not accounts_raw:
                yield _sse({"error": "Failed to query any accounts"})
                return

            yield _sse({
                "message": f"Retrieved {len(accounts_raw)} accounts. Getting territory details...",
                "progress": 8,
            })

            # ----------------------------------------------------------
            # Phase 3: Parallel territory queries (3 workers)
            # ----------------------------------------------------------
            phase = "querying territories"
            territory_ids = list({
                acct.get("_territoryid_value")
                for acct in accounts_raw.values()
                if acct.get("_territoryid_value")
            })

            territories_raw: dict = {}
            if territory_ids:
                t_chunks = _split_chunks(territory_ids, _PARALLEL_WORKERS)
                t_total = sum(math.ceil(len(c) / _ACCT_BATCH) for c in t_chunks if c)
                t_done = 0

                yield _sse({
                    "message": f"Querying {len(territory_ids)} territories in parallel...",
                    "progress": 8,
                })

                with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                    futures = [
                        pool.submit(_par_query_territories, chunk, _ACCT_BATCH, progress_q, idx + 1)
                        for idx, chunk in enumerate(t_chunks) if chunk
                    ]
                    while not all(f.done() for f in futures):
                        time.sleep(0.3)
                        for evt in _drain(progress_q):
                            if evt.get('retry'):
                                yield _sse({"message": evt['message']})
                                continue
                            t_done += 1
                            pct = 8 + int((t_done / max(t_total, 1)) * 1)
                            yield _sse({
                                "message": f"Querying territories... ({evt['fetched']} fetched)",
                                "progress": min(pct, 9),
                            })
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        t_done += 1
                        pct = 8 + int((t_done / max(t_total, 1)) * 1)
                        yield _sse({
                            "message": f"Querying territories... ({evt['fetched']} fetched)",
                            "progress": min(pct, 9),
                        })
                    for f in futures:
                        territories_raw.update(f.result())

            yield _sse({"message": "Processing account data...", "progress": 9})

            # ----------------------------------------------------------
            # Process accounts into data structures (same as sequential)
            # ----------------------------------------------------------
            phase = "processing account data"

            # Build a TPID -> website map, preferring top-level parent's website.
            # msp_parentinglevelcode 861980000 = Top, 861980001 = Child
            tpid_website_map: dict = {}  # tpid (str) -> website domain
            for acct in accounts_raw.values():
                raw_tpid = acct.get("msp_mstopparentid")
                website = acct.get("websiteurl") or ""
                if not raw_tpid or not website:
                    continue
                level_code = acct.get("msp_parentinglevelcode")
                is_top = (level_code == 861980000)
                domain = _extract_domain(website)
                if not domain:
                    continue
                # Top-level parent always wins; otherwise first-come
                if raw_tpid not in tpid_website_map or is_top:
                    tpid_website_map[raw_tpid] = domain

            accounts_data = []
            pod_accounts = {}
            territories_seen = {}
            verticals_seen = set()

            for account_id, acct in accounts_raw.items():
                territory_id = acct.get("_territoryid_value")
                territory_info = None
                pod_name = None

                if territory_id and territory_id in territories_raw:
                    terr = territories_raw[territory_id]
                    terr_name = terr.get("name", "")
                    name_parts = terr_name.split(".")
                    if len(name_parts) >= 4:
                        region = name_parts[0]
                        suffix = name_parts[-1]
                        if len(suffix) >= 2:
                            pod_num = suffix[:2]
                            pod_name = f"{region} POD {pod_num}"
                    territory_info = {
                        "id": territory_id,
                        "name": terr_name,
                        "atu": terr.get("msp_accountteamunitname"),
                    }

                vertical = acct.get(
                    "msp_verticalcode@OData.Community.Display.V1.FormattedValue")
                vertical_category = acct.get(
                    "msp_verticalcategorycode@OData.Community.Display.V1.FormattedValue")

                # Cast TPID to int (API returns strings, column is BigInteger)
                raw_tpid = acct.get("msp_mstopparentid")
                try:
                    tpid_int = int(raw_tpid) if raw_tpid else None
                except (ValueError, TypeError):
                    tpid_int = None

                account_data = {
                    "id": account_id,
                    "name": acct.get("name"),
                    "tpid": tpid_int,
                    "url": build_account_url(account_id),
                    "website": tpid_website_map.get(acct.get("msp_mstopparentid")),
                    "vertical": vertical,
                    "vertical_category": vertical_category,
                    "territory_name": territory_info.get("name") if territory_info else None,
                    "seller_name": None,
                    "seller_type": None,
                    "pod_name": pod_name,
                    "owner_id": acct.get("_ownerid_value"),
                }
                accounts_data.append(account_data)

                if territory_info and territory_info.get("name"):
                    territories_seen[territory_info["name"]] = {
                        "name": territory_info["name"],
                        "atu": territory_info.get("atu"),
                        "pod_name": pod_name,
                    }

                if pod_name:
                    pod_accounts.setdefault(pod_name, []).append(account_id)

                if vertical and vertical.upper() != "N/A":
                    verticals_seen.add(vertical)
                if vertical_category and vertical_category.upper() != "N/A":
                    verticals_seen.add(vertical_category)

            yield _sse({
                "message": (
                    f"Found {len(accounts_data)} accounts, "
                    f"{len(territories_seen)} territories, "
                    f"{len(pod_accounts)} PODs"
                ),
                "progress": 9,
            })

            # ----------------------------------------------------------
            # Phase 4: Parallel team queries (3 workers)
            # ----------------------------------------------------------
            phase = "querying sellers and SEs"
            all_ids = [a["id"] for a in accounts_data]
            a_chunks = _split_chunks(all_ids, _PARALLEL_WORKERS)
            team_total = sum(math.ceil(len(c) / _TEAM_BATCH) for c in a_chunks if c)
            team_done = 0

            yield _sse({
                "message": (
                    f"Fetching sellers and SEs in parallel "
                    f"({_PARALLEL_WORKERS} workers, ~{team_total} batches)..."
                ),
                "progress": 9,
            })

            account_sellers: dict = {}
            sellers_seen: dict = {}
            account_ses: dict = {}

            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_par_query_teams, chunk, _TEAM_BATCH, progress_q, idx + 1)
                    for idx, chunk in enumerate(a_chunks) if chunk
                ]
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        team_done += 1
                        pct = 9 + int((team_done / max(team_total, 1)) * 51)
                        if team_done % 3 == 0 or team_done == 1:
                            yield _sse({
                                "message": f"Querying teams batch {team_done}/{team_total}...",
                                "progress": min(pct, 60),
                            })
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    team_done += 1
                    pct = 9 + int((team_done / max(team_total, 1)) * 51)
                    yield _sse({
                        "message": f"Querying teams batch {team_done}/{team_total}...",
                        "progress": min(pct, 60),
                    })
                for f in futures:
                    r = f.result()
                    account_sellers.update(r["account_sellers"])
                    sellers_seen.update(r["unique_sellers"])
                    account_ses.update(r["account_ses"])

            # ----------------------------------------------------------
            # Phase 4b: Parallel CSAM queries (3 workers)
            # ----------------------------------------------------------
            phase = "querying CSAMs"
            yield _sse({"message": "Querying CSAMs...", "progress": 60})

            csam_chunks = _split_chunks(all_ids, _PARALLEL_WORKERS)
            csam_total = sum(
                math.ceil(len(c) / _CSAM_BATCH) for c in csam_chunks if c
            )
            csam_done = 0

            account_csams: dict = {}    # account_id -> [{name, user_id}]
            csams_seen: dict = {}       # name -> {name, user_id}

            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_par_query_csams, chunk, _CSAM_BATCH,
                                progress_q, idx + 1)
                    for idx, chunk in enumerate(csam_chunks) if chunk
                ]
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        csam_done += 1
                    if csam_done > 0 and csam_done % 3 == 0:
                        yield _sse({
                            "message": f"Querying CSAMs batch {csam_done}/{csam_total}...",
                            "progress": 60 + round((csam_done / max(csam_total, 1)) * 10),
                        })
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    csam_done += 1
                for f in futures:
                    r = f.result()
                    account_csams.update(r["account_csams"])
                    csams_seen.update(r["unique_csams"])

            yield _sse({
                "message": f"Found {len(csams_seen)} CSAMs",
                "progress": 70,
            })

            # ----------------------------------------------------------
            # Phase 4c: Parallel DSS queries (3 workers)
            # ----------------------------------------------------------
            phase = "querying DSSs"
            yield _sse({"message": "Querying DSSs...", "progress": 70})

            dss_chunks = _split_chunks(all_ids, _PARALLEL_WORKERS)
            dss_batch_total = sum(
                math.ceil(len(c) / _DSS_BATCH) for c in dss_chunks if c
            )
            dss_done = 0

            account_dss: dict = {}    # account_id -> [{name, specialty, user_id}]
            dss_seen: dict = {}       # name -> {name, specialty, user_id}

            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_par_query_dss, chunk, _DSS_BATCH,
                                progress_q, idx + 1)
                    for idx, chunk in enumerate(dss_chunks) if chunk
                ]
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        dss_done += 1
                    if dss_done > 0 and dss_done % 3 == 0:
                        yield _sse({
                            "message": f"Querying DSSs batch {dss_done}...",
                            "progress": 70 + round((dss_done / max(dss_batch_total, 1)) * 18),
                        })
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    dss_done += 1
                for f in futures:
                    r = f.result()
                    account_dss.update(r["account_dss"])
                    dss_seen.update(r["unique_dss"])

            # Filter out broad/non-specific DSS specialties that aren't useful
            _DSS_EXCLUDED_SPECIALTIES = {"Unified", "Cloud & AI-Acq", "Cloud & AI"}
            dss_seen = {
                name: info for name, info in dss_seen.items()
                if info.get("specialty", "") not in _DSS_EXCLUDED_SPECIALTIES
            }
            _allowed_dss_names = set(dss_seen.keys())
            for acct_id in list(account_dss):
                account_dss[acct_id] = [
                    d for d in account_dss[acct_id]
                    if d["name"] in _allowed_dss_names
                ]
                if not account_dss[acct_id]:
                    del account_dss[acct_id]

            # Clean up previously-synced DSS records with excluded specialties
            excluded_dss = SolutionEngineer.query.filter(
                SolutionEngineer.specialty.in_(_DSS_EXCLUDED_SPECIALTIES)
            ).all()
            for se in excluded_dss:
                se.territories.clear()
                se.pods.clear()
                db.session.delete(se)
            if excluded_dss:
                db.session.flush()

            # Populate seller info on accounts
            accounts_with_sellers = 0
            for ad in accounts_data:
                if ad["id"] in account_sellers:
                    seller = account_sellers[ad["id"]]
                    ad["seller_name"] = seller["name"]
                    ad["seller_type"] = seller["type"]
                    accounts_with_sellers += 1

            # Build pod_teams
            pod_teams = {}
            for pn, acct_ids in pod_accounts.items():
                pod_teams[pn] = {"data_se": [], "infra_se": [], "apps_se": []}
                seen_ses = {"data_se": set(), "infra_se": set(), "apps_se": set()}
                for aid in acct_ids:
                    if aid in account_ses:
                        for role in ("data_se", "infra_se", "apps_se"):
                            for se in account_ses[aid].get(role, []):
                                if se["name"] not in seen_ses[role]:
                                    seen_ses[role].add(se["name"])
                                    pod_teams[pn][role].append(se)

            yield _sse({
                "message": (
                    f"Found {len(sellers_seen)} sellers, "
                    f"{len(csams_seen)} CSAMs, "
                    f"{len(dss_seen)} DSSs for "
                    f"{accounts_with_sellers}/{len(accounts_data)} accounts"
                ),
                "progress": 88,
            })

            # ----------------------------------------------------------
            # Phase 4d: Parallel alias resolution for all people
            # ----------------------------------------------------------
            phase = "resolving aliases"

            # Collect all unique user_ids from every source
            all_user_ids: set = set()
            for info in sellers_seen.values():
                if info.get("user_id"):
                    all_user_ids.add(info["user_id"])
            for team in pod_teams.values():
                for se_list in team.values():
                    for se_info in se_list:
                        if se_info.get("user_id"):
                            all_user_ids.add(se_info["user_id"])
            for info in dss_seen.values():
                if info.get("user_id"):
                    all_user_ids.add(info["user_id"])
            for info in csams_seen.values():
                if info.get("user_id"):
                    all_user_ids.add(info["user_id"])
            for ad in accounts_data:
                if ad.get("owner_id"):
                    all_user_ids.add(ad["owner_id"])

            alias_total = len(all_user_ids)
            alias_cache: dict = {}  # user_id -> {alias, fullname} or None
            alias_done = 0

            if alias_total > 0:
                yield _sse({
                    "message": f"Resolving aliases (0/{alias_total})...",
                    "progress": 88,
                })

                alias_q: queue.Queue = queue.Queue()
                alias_list = list(all_user_ids)
                alias_workers = min(_PARALLEL_WORKERS, alias_total)
                alias_chunk_size = math.ceil(alias_total / alias_workers)
                alias_chunks = [
                    alias_list[i:i + alias_chunk_size]
                    for i in range(0, alias_total, alias_chunk_size)
                ]

                def _resolve_aliases(ids, q):
                    for uid in ids:
                        info = get_user_info(uid)
                        q.put((uid, info))

                with ThreadPoolExecutor(max_workers=alias_workers) as pool:
                    for chunk in alias_chunks:
                        pool.submit(_resolve_aliases, chunk, alias_q)

                    while alias_done < alias_total:
                        try:
                            uid, info = alias_q.get(timeout=60)
                            alias_cache[uid] = info
                            alias_done += 1
                            if alias_done % 5 == 0 or alias_done == alias_total:
                                yield _sse({
                                    "message": (
                                        f"Resolving aliases "
                                        f"({alias_done}/{alias_total})..."
                                    ),
                                    "progress": 88 + round(
                                        (alias_done / alias_total) * 2
                                    ),
                                })
                        except queue.Empty:
                            break

            def _cached_alias(user_id):
                """Look up alias from pre-resolved cache."""
                if not user_id:
                    return None
                info = alias_cache.get(user_id)
                return info["alias"] if info else None

            # ----------------------------------------------------------
            # Phase 5: Database writes (sequential)
            # ----------------------------------------------------------
            phase = "creating database records"
            yield _sse({"message": "Creating PODs...", "progress": 90})

            pods_map = {}
            pods_created = 0
            for pn in pod_accounts:
                existing = POD.query.filter_by(name=pn).first()
                if existing:
                    pods_map[pn] = existing
                else:
                    pod = POD(name=pn)
                    db.session.add(pod)
                    pods_map[pn] = pod
                    pods_created += 1
            db.session.flush()

            # Clear all POD associations so they get rebuilt fresh from MSX
            # This prevents stale memberships from accumulating over time.
            for pod in pods_map.values():
                pod.territories = []
                pod.solution_engineers = []
            db.session.flush()

            yield _sse({
                "message": f"Created {pods_created} new PODs (rebuilding associations)",
                "progress": 90,
            })

            # Territories
            yield _sse({"message": "Creating territories...", "progress": 90})
            territories_map = {}
            territories_created = 0
            for terr_name, terr_info in territories_seen.items():
                existing = Territory.query.filter_by(
                    name=terr_name).first()
                if existing:
                    territories_map[terr_name] = existing
                    # Always update pod assignment from MSX (authoritative)
                    if terr_info.get("pod_name"):
                        existing.pod = pods_map.get(terr_info["pod_name"])
                else:
                    territory = Territory(name=terr_name)
                    if terr_info.get("pod_name"):
                        territory.pod = pods_map.get(terr_info["pod_name"])
                    db.session.add(territory)
                    territories_map[terr_name] = territory
                    territories_created += 1
            db.session.flush()

            yield _sse({
                "message": f"Created {territories_created} new territories",
                "progress": 90,
            })

            # Sellers
            yield _sse({"message": "Creating sellers...", "progress": 91})
            sellers_map = {}
            sellers_created = 0
            sellers_updated = 0
            sellers_total = len(sellers_seen)
            for seller_idx, (seller_name, seller_info) in enumerate(sellers_seen.items(), 1):
                existing = Seller.query.filter_by(
                    name=seller_name).first()
                if existing:
                    sellers_map[seller_name] = existing
                    # Update seller_type and alias from MSX (authoritative)
                    seller_changed = False
                    new_type = seller_info.get("type", "Growth")
                    if new_type and existing.seller_type != new_type:
                        existing.seller_type = new_type
                        seller_changed = True
                    systemuser_id = seller_info.get("user_id")
                    if systemuser_id and not existing.alias:
                        new_alias = _cached_alias(systemuser_id)
                        if new_alias:
                            existing.alias = new_alias
                            seller_changed = True
                    if seller_changed:
                        sellers_updated += 1
                else:
                    seller_type = seller_info.get("type", "Growth")
                    systemuser_id = seller_info.get("user_id")
                    alias = _cached_alias(systemuser_id)
                    seller = Seller(
                        name=seller_name, seller_type=seller_type,
                        alias=alias,
                    )
                    db.session.add(seller)
                    sellers_map[seller_name] = seller
                    sellers_created += 1
                if seller_idx % 5 == 0 or seller_idx == sellers_total:
                    yield _sse({
                        "message": f"Creating sellers ({seller_idx}/{sellers_total})...",
                        "progress": 91 + round((seller_idx / max(sellers_total, 1)) * 1),
                    })
            db.session.flush()

            # Associate sellers with territories
            for ad in accounts_data:
                sn = ad.get("seller_name")
                tn = ad.get("territory_name")
                if sn and tn:
                    s = sellers_map.get(sn)
                    t = territories_map.get(tn)
                    if s and t and t not in s.territories:
                        s.territories.append(t)
            db.session.flush()

            yield _sse({
                "message": f"Created {sellers_created} new sellers, updated {sellers_updated}",
                "progress": 92,
            })

            # Solution Engineers
            yield _sse({"message": "Creating solution engineers...", "progress": 92})
            se_items_total = sum(
                len(se_list)
                for team in pod_teams.values()
                for se_list in team.values()
            )
            se_items_done = 0
            se_map = {}
            ses_created = 0
            specialty_map = {
                "data_se": "Azure Data",
                "infra_se": "Azure Core and Infra",
                "apps_se": "Azure Apps and AI",
            }
            for pn, team in pod_teams.items():
                pod = pods_map.get(pn)
                if not pod:
                    continue
                for se_role, specialty in specialty_map.items():
                    for se_info in team.get(se_role, []):
                        se_items_done += 1
                        if not se_info.get("name"):
                            continue
                        se_key = (se_info["name"], specialty)
                        if se_key not in se_map:
                            existing = SolutionEngineer.query.filter_by(
                                name=se_info["name"], specialty=specialty,
                            ).first()
                            if existing:
                                se_map[se_key] = existing
                            else:
                                systemuser_id = se_info.get("user_id")
                                alias = _cached_alias(systemuser_id)
                                se = SolutionEngineer(
                                    name=se_info["name"], alias=alias,
                                    specialty=specialty,
                                )
                                db.session.add(se)
                                se_map[se_key] = se
                                ses_created += 1
                        se = se_map.get(se_key)
                        if se and pod not in se.pods:
                            se.pods.append(pod)
                        if se_items_done % 5 == 0 or se_items_done == se_items_total:
                            yield _sse({
                                "message": f"Creating SEs ({se_items_done}/{se_items_total})...",
                                "progress": 92 + round((se_items_done / max(se_items_total, 1)) * 1),
                            })
            db.session.flush()

            yield _sse({
                "message": f"Created {ses_created} new solution engineers",
                "progress": 93,
            })

            # Digital Solution Specialists (DSSs)
            # DSSs are SolutionEngineer records linked to territories (not pods).
            dss_map: dict = {}  # (name, specialty) -> SolutionEngineer
            dss_created = 0
            dss_total = len(dss_seen)

            yield _sse({
                "message": f"Syncing digital solution specialists (0/{dss_total})...",
                "progress": 93,
            })

            # Build account_id → territory_name for DSS territory linking
            acct_territory: dict = {
                ad["id"]: ad.get("territory_name")
                for ad in accounts_data
                if ad.get("territory_name")
            }

            for dss_idx, (dss_name, dss_info) in enumerate(dss_seen.items(), 1):
                specialty = dss_info.get("specialty", "")
                dss_key = (dss_name, specialty)
                existing = SolutionEngineer.query.filter_by(
                    name=dss_name, specialty=specialty,
                ).first()
                if existing:
                    dss_map[dss_key] = existing
                    # Backfill alias if missing
                    if not existing.alias and dss_info.get("user_id"):
                        alias = _cached_alias(dss_info["user_id"])
                        if alias:
                            existing.alias = alias
                else:
                    systemuser_id = dss_info.get("user_id")
                    alias = _cached_alias(systemuser_id)
                    se = SolutionEngineer(
                        name=dss_name, alias=alias, specialty=specialty,
                    )
                    db.session.add(se)
                    dss_map[dss_key] = se
                    dss_created += 1
                if dss_idx % 5 == 0 or dss_idx == dss_total:
                    yield _sse({
                        "message": f"Syncing DSSs ({dss_idx}/{dss_total})...",
                        "progress": 93 + round((dss_idx / max(dss_total, 1)) * 1),
                    })
            db.session.flush()

            # Link DSSs to territories based on which accounts they cover
            for acct_id, dss_list in account_dss.items():
                terr_name = acct_territory.get(acct_id)
                territory = territories_map.get(terr_name) if terr_name else None
                if not territory:
                    continue
                for d in dss_list:
                    dss_key = (d["name"], d.get("specialty", ""))
                    se = dss_map.get(dss_key)
                    if se and territory not in se.territories:
                        se.territories.append(territory)
            db.session.flush()

            yield _sse({
                "message": f"Created {dss_created} new DSSs, linked to territories",
                "progress": 94,
            })

            # CSAMs
            csam_map: dict = {}  # name -> CustomerCSAM
            csams_created = 0
            csam_total = len(csams_seen)

            yield _sse({
                "message": f"Syncing CSAMs (0/{csam_total})...",
                "progress": 94,
            })

            for csam_idx, (csam_name, csam_info) in enumerate(csams_seen.items(), 1):
                existing = CustomerCSAM.query.filter_by(name=csam_name).first()
                if existing:
                    csam_map[csam_name] = existing
                    # Backfill alias if missing
                    if not existing.alias and csam_info.get("user_id"):
                        alias = _cached_alias(csam_info["user_id"])
                        if alias:
                            existing.alias = alias
                else:
                    systemuser_id = csam_info.get("user_id")
                    alias = _cached_alias(systemuser_id)
                    csam = CustomerCSAM(name=csam_name, alias=alias)
                    db.session.add(csam)
                    csam_map[csam_name] = csam
                    csams_created += 1
                if csam_idx % 5 == 0 or csam_idx == csam_total:
                    yield _sse({
                        "message": f"Syncing CSAMs ({csam_idx}/{csam_total})...",
                        "progress": 94 + round((csam_idx / max(csam_total, 1)) * 1),
                    })
            db.session.flush()

            yield _sse({
                "message": f"Created {csams_created} new CSAMs",
                "progress": 95,
            })

            # DAE aliases already resolved in alias_cache (Phase 4d)
            owner_alias_cache = alias_cache

            # Verticals
            yield _sse({"message": "Creating verticals...", "progress": 96})
            verticals_map = {}
            verticals_created = 0
            for vn in verticals_seen:
                existing = Vertical.query.filter_by(
                    name=vn).first()
                if existing:
                    verticals_map[vn] = existing
                else:
                    vertical = Vertical(name=vn)
                    db.session.add(vertical)
                    verticals_map[vn] = vertical
                    verticals_created += 1
            db.session.flush()

            yield _sse({
                "message": f"Created {verticals_created} new verticals",
                "progress": 97,
            })

            # Customers
            SyncStatus.mark_started('accounts')
            yield _sse({"message": "Syncing customers...", "progress": 97})
            customers_created = 0
            customers_skipped = 0
            customers_updated = 0
            customers_unchanged = 0
            seen_tpids = set()  # In-memory dedup for same-TPID accounts in batch

            # Pre-load existing TPIDs to avoid per-row DB queries + autoflush.
            # Query ALL tpids (unique constraint is on tpid alone, not per-user).
            # Normalize to int -- old imports may have stored TPIDs as strings.
            def _safe_int(val):
                try:
                    return int(val) if val is not None else None
                except (ValueError, TypeError):
                    return val

            # Pre-load existing customers keyed by TPID for upsert comparison
            existing_customers_by_tpid: dict = {}
            for cust in Customer.query.all():
                normalized = _safe_int(cust.tpid)
                if normalized is not None:
                    existing_customers_by_tpid[normalized] = cust

            with db.session.no_autoflush:
                logger.info(
                    "Customer import: %d existing TPIDs in DB, %d accounts to process",
                    len(existing_customers_by_tpid), len(accounts_data),
                )
                for idx, ad in enumerate(accounts_data, 1):
                    if idx % 50 == 0:
                        yield _sse({
                            "message": f"Processing customer {idx}/{len(accounts_data)}...",
                            "progress": 99,
                        })

                    tpid = ad.get("tpid")
                    customer_name = ad.get("name")
                    if not tpid or not customer_name:
                        customers_skipped += 1
                        continue

                    # Skip duplicates within the same import batch
                    if tpid in seen_tpids:
                        customers_skipped += 1
                        continue
                    seen_tpids.add(tpid)

                    if tpid in existing_customers_by_tpid:
                        # --- Upsert existing customer ---
                        cust = existing_customers_by_tpid[tpid]
                        changed = False

                        # Update name (could be rebrand — log it)
                        if customer_name and cust.name != customer_name:
                            logger.info(
                                "Customer name changed for TPID %s: '%s' -> '%s'",
                                tpid, cust.name, customer_name,
                            )
                            cust.name = customer_name
                            changed = True

                        # Backfill tpid_url
                        if ad.get("url") and not cust.tpid_url:
                            cust.tpid_url = ad["url"]
                            changed = True

                        # Backfill website
                        if ad.get("website") and not cust.website:
                            cust.website = ad["website"]
                            changed = True

                        # Update territory (MSX is authoritative)
                        territory_name = ad.get("territory_name")
                        new_territory = territories_map.get(territory_name) if territory_name else None
                        if new_territory and cust.territory != new_territory:
                            logger.info(
                                "Territory changed for '%s' (TPID %s): '%s' -> '%s'",
                                cust.name, tpid,
                                cust.territory.name if cust.territory else None,
                                new_territory.name,
                            )
                            cust.territory = new_territory
                            changed = True

                        # Update seller (MSX is authoritative)
                        seller_name = ad.get("seller_name")
                        new_seller = sellers_map.get(seller_name) if seller_name else None
                        if new_seller and cust.seller != new_seller:
                            logger.info(
                                "Seller changed for '%s' (TPID %s): '%s' -> '%s'",
                                cust.name, tpid,
                                cust.seller.name if cust.seller else None,
                                new_seller.name,
                            )
                            cust.seller = new_seller
                            changed = True

                        # Update verticals (replace with current MSX verticals)
                        new_verticals = []
                        if ad.get("vertical") and ad["vertical"] in verticals_map:
                            new_verticals.append(verticals_map[ad["vertical"]])
                        if ad.get("vertical_category") and ad["vertical_category"] in verticals_map:
                            vert = verticals_map[ad["vertical_category"]]
                            if vert not in new_verticals:
                                new_verticals.append(vert)
                        if set(new_verticals) != set(cust.verticals):
                            cust.verticals = new_verticals
                            changed = True

                        # Update DAE (account owner) from MSX
                        owner_id = ad.get("owner_id")
                        if owner_id:
                            info = owner_alias_cache.get(owner_id)
                            alias = info["alias"] if info else None
                            fullname = info["fullname"] if info else None
                            if alias and cust.dae_alias != alias:
                                cust.dae_alias = alias
                                changed = True
                            if fullname and cust.dae_name != fullname:
                                cust.dae_name = fullname
                                changed = True
                            elif alias and not cust.dae_name:
                                cust.dae_name = alias
                                changed = True

                        # Update available CSAMs (M2M) from MSX
                        acct_csam_list = account_csams.get(ad["id"], [])
                        new_csam_objs = [
                            csam_map[c["name"]]
                            for c in acct_csam_list if c["name"] in csam_map
                        ]
                        if set(new_csam_objs) != set(cust.available_csams):
                            cust.available_csams = new_csam_objs
                            changed = True

                        if changed:
                            customers_updated += 1
                        else:
                            customers_unchanged += 1

                        continue

                    # --- Create new customer ---
                    customer = Customer(
                        name=customer_name, tpid=tpid,
                        tpid_url=ad.get("url"),
                        website=ad.get("website"),
                    )
                    territory_name = ad.get("territory_name")
                    if territory_name and territory_name in territories_map:
                        customer.territory = territories_map[territory_name]
                    seller_name = ad.get("seller_name")
                    if seller_name and seller_name in sellers_map:
                        customer.seller = sellers_map[seller_name]
                    if ad.get("vertical") and ad["vertical"] in verticals_map:
                        customer.verticals.append(verticals_map[ad["vertical"]])
                    if ad.get("vertical_category") and ad["vertical_category"] in verticals_map:
                        vert = verticals_map[ad["vertical_category"]]
                        if vert not in customer.verticals:
                            customer.verticals.append(vert)
                    # DAE
                    owner_id = ad.get("owner_id")
                    if owner_id:
                        info = owner_alias_cache.get(owner_id)
                        if info:
                            customer.dae_alias = info["alias"]
                            customer.dae_name = info.get("fullname") or info["alias"]
                    # Available CSAMs (M2M)
                    acct_csam_list = account_csams.get(ad["id"], [])
                    for c in acct_csam_list:
                        csam_obj = csam_map.get(c["name"])
                        if csam_obj:
                            customer.available_csams.append(csam_obj)
                    db.session.add(customer)
                    existing_customers_by_tpid[tpid] = customer
                    customers_created += 1

            try:
                db.session.commit()
            except Exception as commit_err:
                # IntegrityError safety net: rollback and retry one-by-one
                logger.warning("Batch commit failed (%s), retrying individually", commit_err)
                db.session.rollback()

                # Re-check what's already in DB after rollback
                existing_tpids_after = set()
                for row in db.session.query(Customer.tpid).all():
                    n = _safe_int(row[0])
                    if n is not None:
                        existing_tpids_after.add(n)

                retry_created = 0
                for idx, ad in enumerate(accounts_data, 1):
                    tpid = ad.get("tpid")
                    customer_name = ad.get("name")
                    if not tpid or not customer_name:
                        continue
                    if tpid in existing_tpids_after:
                        continue

                    try:
                        cust = Customer(
                            name=customer_name, tpid=tpid,
                            tpid_url=ad.get("url"),
                        )
                        territory_name = ad.get("territory_name")
                        if territory_name and territory_name in territories_map:
                            cust.territory = territories_map[territory_name]
                        seller_name = ad.get("seller_name")
                        if seller_name and seller_name in sellers_map:
                            cust.seller = sellers_map[seller_name]
                        db.session.add(cust)
                        db.session.commit()
                        existing_tpids_after.add(tpid)
                        retry_created += 1
                    except Exception:
                        db.session.rollback()

                customers_created = retry_created

            SyncStatus.mark_completed(
                'accounts', success=True,
                items_synced=customers_created,
                details=f'{customers_created} created, {customers_updated} updated, {customers_unchanged} unchanged, {customers_skipped} skipped',
            )

            # Persist the list of TPIDs seen during this sync for FY finalization
            try:
                import json as _json
                tpid_file = Path(current_app.instance_path).parent / 'data' / 'last_sync_tpids.json'
                tpid_file.write_text(_json.dumps(sorted(seen_tpids)))
                logger.info("Saved %d synced TPIDs to %s", len(seen_tpids), tpid_file)
            except Exception as e:
                logger.warning("Failed to save synced TPIDs: %s", e)

            duration = round(time.time() - import_start_time, 1)
            yield _sse({
                "message": "Import complete!",
                "progress": 100,
                "complete": True,
                "summary": {
                    "pods_created": pods_created,
                    "territories_created": territories_created,
                    "sellers_created": sellers_created,
                    "sellers_updated": sellers_updated,
                    "solution_engineers_created": ses_created,
                    "dss_created": dss_created,
                    "csams_created": csams_created,
                    "verticals_created": verticals_created,
                    "customers_created": customers_created,
                    "customers_skipped": customers_skipped,
                    "customers_updated": customers_updated,
                    "customers_unchanged": customers_unchanged,
                    "duration": duration,
                },
            })

            logger.info(
                f"Parallel MSX Import complete in {duration}s: "
                f"{pods_created} PODs, {territories_created} territories, "
                f"{sellers_created} sellers ({sellers_updated} updated), {ses_created} SEs, "
                f"{csams_created} CSAMs, {verticals_created} verticals, "
                f"{customers_created} customers created, {customers_updated} updated, "
                f"{customers_unchanged} unchanged, {customers_skipped} skipped"
            )

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            # Mark accounts sync as failed if it was started
            try:
                SyncStatus.mark_completed('accounts', success=False, details=str(e))
            except Exception:
                pass
            error_detail = f"[{type(e).__name__}] {e}"
            logger.exception(
                f"Error during parallel MSX import (phase: {phase}): {error_detail}"
            )
            yield _sse({"error": f"Import failed during '{phase}': {error_detail}"})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
