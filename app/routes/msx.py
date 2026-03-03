"""
MSX Integration Routes.

Provides API endpoints for MSX (Dynamics 365) integration:
- Authentication status and device code flow
- Token refresh
- TPID account lookup
- Connection testing
- Streaming import from MSX
"""

import json
import time
from flask import Blueprint, jsonify, request, Response, g, current_app
import logging

from app.services.msx_auth import (
    get_msx_auth_status,
    refresh_token,
    clear_token_cache,
    start_token_refresh_job,
    start_device_code_flow,
    get_device_code_status,
    cancel_device_code_flow,
    get_az_cli_status,
    start_az_login,
    get_az_login_process_status,
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
    get_my_milestone_team_ids,
    extract_account_id_from_url,
    create_task,
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
    build_account_url,
    get_user_alias,
)
from app.models import Customer, Milestone, Territory, Seller, POD, SolutionEngineer, Vertical, db

logger = logging.getLogger(__name__)

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

    The frontend should poll ``/api/msx/az-status`` afterwards to
    detect when the user completes sign-in.
    """
    result = start_az_login()

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

    Sets the subscription and refreshes the CRM token so subsequent
    API calls work immediately.
    """
    status = get_az_cli_status()
    if not status.get("logged_in"):
        return jsonify({"success": False, "error": "Not logged in yet"}), 400

    set_subscription()
    token_ok = refresh_token()
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
            if existing and existing.call_logs:
                milestone_data["used_in_call_logs"] = len(existing.call_logs)
                milestone_data["local_milestone_id"] = existing.id
            else:
                milestone_data["used_in_call_logs"] = 0
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
            if existing and existing.call_logs:
                milestone_data["used_in_call_logs"] = len(existing.call_logs)
                milestone_data["local_milestone_id"] = existing.id
            else:
                milestone_data["used_in_call_logs"] = 0
                milestone_data["local_milestone_id"] = existing.id if existing else None
            # Use live MSX team membership if available, fall back to local record
            if my_team_ids:
                milestone_data["on_my_team"] = msx_milestone_id.lower() in my_team_ids
            else:
                milestone_data["on_my_team"] = existing.on_my_team if existing else False
    
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
    from app.services.msx_api import add_user_to_milestone_team

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
    Import accounts from MSX into NoteHelper database.
    
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
        
        # 2. Create customers from accounts
        for acct in accounts:
            tpid = acct.get("tpid")
            customer_name = acct.get("name")
            territory_name = acct.get("territory_name")
            seller_name = acct.get("seller_name")
            
            if not tpid or not customer_name:
                continue
            
            # Check if customer already exists by TPID
            existing = Customer.query.filter_by(tpid=tpid).first()
            if existing:
                customers_skipped += 1
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
                customer.sellers.append(seller_map[seller_name])
            
            db.session.add(customer)
            customers_created += 1
        
        db.session.commit()
        
        logger.info(f"API Import complete: {territories_created} territories, {sellers_created} sellers, {customers_created} customers created, {customers_skipped} skipped")
        
        return jsonify({
            "success": True,
            "territories_created": territories_created,
            "sellers_created": sellers_created,
            "customers_created": customers_created,
            "customers_skipped": customers_skipped
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception("Error importing accounts from MSX")
        return jsonify({"success": False, "error": str(e)}), 500


# -----------------------------------------------------------------------------
# Streaming MSX Import (SSE)
# -----------------------------------------------------------------------------

@msx_bp.route('/import-stream')
def import_stream():
    """
    Stream import all accounts/data from MSX into NoteHelper database.
    
    Uses Server-Sent Events (SSE) to stream progress updates.
    
    Optimized approach:
    - One query for all account assignments (scan_init)
    - One query per account for details + territory
    - One query per POD for team members (cached across accounts)
    
    Creates: PODs, Territories, Sellers, SEs, Verticals, Customers
    """
    # Capture app context before generator (SSE generators lose request context)
    app = current_app._get_current_object()
    
    def generate():
        # Push app context for database operations in SSE generator
        # Using push() instead of with-block so we don't need to re-indent all the code
        ctx = app.app_context()
        ctx.push()
        
        try:
            # Get the user_id for single-user mode (user_id=1 hack for SSE context)
            # In SSE streams we don't have proper request context, so use user_id=1
            # This matches how the app handles single-user mode
            user_id = 1
            
            import_start_time = time.time()
            yield "data: " + json.dumps({"message": "Starting MSX import..."}) + "\n\n"
            
            # 1. Initialize - get all account IDs
            yield "data: " + json.dumps({"message": "Fetching your account assignments from MSX..."}) + "\n\n"
            
            init_result = scan_init()
            if not init_result.get("success"):
                error_msg = init_result.get("error", "Failed to initialize scan")
                if init_result.get("vpn_blocked") or is_vpn_blocked():
                    yield "data: " + json.dumps({"error": error_msg, "vpn_blocked": True}) + "\n\n"
                else:
                    yield "data: " + json.dumps({"error": error_msg}) + "\n\n"
                return
            
            account_ids = init_result.get("account_ids", [])
            total_accounts = len(account_ids)
            user_info = init_result.get("user", {})
            role = init_result.get("role", "Unknown")
            
            yield "data: " + json.dumps({
                "message": f"Found {total_accounts} accounts to import...",
                "user": user_info.get("name"),
                "role": role
            }) + "\n\n"
            
            # 2. Batch query all accounts — inline the loop so we can
            #    yield SSE progress after each batch in real time.
            BATCH_SIZE = 15
            accounts_raw = {}
            total_batches = (len(account_ids) + BATCH_SIZE - 1) // BATCH_SIZE

            yield "data: " + json.dumps({
                "message": f"Querying {len(account_ids)} accounts ({total_batches} batches)...",
                "progress": 2
            }) + "\n\n"

            for batch_num, i in enumerate(range(0, len(account_ids), BATCH_SIZE), start=1):
                # Bail early on VPN block
                if is_vpn_blocked():
                    yield "data: " + json.dumps({"error": "IP address is blocked — connect to VPN and retry.", "vpn_blocked": True}) + "\n\n"
                    return

                batch = account_ids[i:i + BATCH_SIZE]
                filter_parts = [f"accountid eq {aid}" for aid in batch]
                filter_query = " or ".join(filter_parts)
                result = query_entity(
                    "accounts",
                    select=["accountid", "name", "msp_mstopparentid",
                            "_territoryid_value", "msp_verticalcode",
                            "msp_verticalcategorycode"],
                    filter_query=filter_query,
                    top=BATCH_SIZE + 5,
                )
                if result.get("success"):
                    for rec in result.get("records", []):
                        acct_id = rec.get("accountid")
                        if acct_id:
                            accounts_raw[acct_id] = rec
                pct = 2 + int((batch_num / total_batches) * 10)  # 2-12%
                yield "data: " + json.dumps({
                    "message": f"Querying accounts... batch {batch_num}/{total_batches} ({len(accounts_raw)} fetched)",
                    "progress": pct
                }) + "\n\n"

            if not accounts_raw:
                yield "data: " + json.dumps({"error": "Failed to query any accounts"}) + "\n\n"
                return

            yield "data: " + json.dumps({
                "message": f"Retrieved {len(accounts_raw)} accounts. Getting territory details...",
                "progress": 13
            }) + "\n\n"
            
            # 3. Collect unique territory IDs and batch query them inline
            territory_ids = set()
            for acct in accounts_raw.values():
                terr_id = acct.get("_territoryid_value")
                if terr_id:
                    territory_ids.add(terr_id)
            
            territories_raw = {}
            if territory_ids:
                unique_terr = list(territory_ids)
                terr_batches = (len(unique_terr) + BATCH_SIZE - 1) // BATCH_SIZE

                yield "data: " + json.dumps({
                    "message": f"Querying {len(unique_terr)} territories ({terr_batches} batches)...",
                    "progress": 14
                }) + "\n\n"

                for batch_num, i in enumerate(range(0, len(unique_terr), BATCH_SIZE), start=1):
                    batch = unique_terr[i:i + BATCH_SIZE]
                    filter_parts = [f"territoryid eq {tid}" for tid in batch]
                    filter_query = " or ".join(filter_parts)
                    result = query_entity(
                        "territories",
                        select=["territoryid", "name", "msp_ownerid",
                                "msp_salesunitname", "msp_accountteamunitname"],
                        filter_query=filter_query,
                        top=BATCH_SIZE + 5,
                    )
                    if result.get("success"):
                        for rec in result.get("records", []):
                            terr_id = rec.get("territoryid")
                            if terr_id:
                                territories_raw[terr_id] = rec
                    pct = 14 + int((batch_num / terr_batches) * 6)  # 14-20%
                    yield "data: " + json.dumps({
                        "message": f"Querying territories... batch {batch_num}/{terr_batches} ({len(territories_raw)} fetched)",
                        "progress": pct
                    }) + "\n\n"
            
            yield "data: " + json.dumps({
                "message": f"Processing account data...",
                "progress": 21
            }) + "\n\n"
            
            # 4. Process all accounts and build data structures
            accounts_data = []  # List of account details
            pod_accounts = {}  # pod_name -> [account_ids] for team lookup
            territories_seen = {}  # territory_name -> territory_info
            verticals_seen = set()  # vertical names
            
            for account_id, acct in accounts_raw.items():
                # Get territory info from batch result
                territory_id = acct.get("_territoryid_value")
                territory_info = None
                pod_name = None
                
                if territory_id and territory_id in territories_raw:
                    terr = territories_raw[territory_id]
                    terr_name = terr.get("name", "")
                    
                    # Derive POD from territory name (e.g., "East.SMECC.MAA.0601" -> "East POD 06")
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
                
                # Build account data (seller info will be added after batch query)
                vertical = acct.get("msp_verticalcode@OData.Community.Display.V1.FormattedValue")
                vertical_category = acct.get("msp_verticalcategorycode@OData.Community.Display.V1.FormattedValue")
                
                account_data = {
                    "id": account_id,
                    "name": acct.get("name"),
                    "tpid": acct.get("msp_mstopparentid"),
                    "url": build_account_url(account_id),
                    "vertical": vertical,
                    "vertical_category": vertical_category,
                    "territory_name": territory_info.get("name") if territory_info else None,
                    "seller_name": None,  # Will be set from msp_accountteams query
                    "seller_type": None,  # Will be set from msp_accountteams query
                    "pod_name": pod_name,
                }
                accounts_data.append(account_data)
                
                # Track unique territories
                if territory_info and territory_info.get("name"):
                    territories_seen[territory_info["name"]] = {
                        "name": territory_info["name"],
                        "atu": territory_info.get("atu"),
                        "pod_name": pod_name,
                    }
                
                # Track POD for SE deduplication later
                if pod_name:
                    if pod_name not in pod_accounts:
                        pod_accounts[pod_name] = []
                    pod_accounts[pod_name].append(account_id)
                
                # Track verticals
                if vertical and vertical.upper() != "N/A":
                    verticals_seen.add(vertical)
                if vertical_category and vertical_category.upper() != "N/A":
                    verticals_seen.add(vertical_category)
            
            yield "data: " + json.dumps({
                "message": f"Found {len(accounts_data)} accounts, {len(territories_seen)} territories, {len(pod_accounts)} PODs",
                "progress": 22
            }) + "\n\n"
            
            # 5. Batch query all account teams for sellers and SEs
            # Uses server-side filtering: Corporate + Cloud & AI*, batched by 3 accounts
            # (3 accounts × ~22 Cloud & AI records = ~66, safely under 100 limit)
            # Process in chunks for progress updates
            account_ids_list = [a["id"] for a in accounts_data]
            batch_size = 3
            total_batches = (len(account_ids_list) + batch_size - 1) // batch_size
            
            yield "data: " + json.dumps({
                "message": f"Fetching sellers and SEs ({total_batches} batches)...",
                "progress": 25
            }) + "\n\n"
            
            # Run batch_query_account_teams but show incremental progress
            # Progress spans 25-85% during this phase
            account_sellers = {}
            sellers_seen = {}
            account_ses = {}
            
            for batch_idx in range(0, len(account_ids_list), batch_size):
                batch = account_ids_list[batch_idx:batch_idx + batch_size]
                batch_num = batch_idx // batch_size + 1
                
                # Update progress
                progress = 25 + int((batch_num / total_batches) * 60)  # 25-85%
                if batch_num % 5 == 0 or batch_num == 1:  # Update every 5 batches
                    yield "data: " + json.dumps({
                        "message": f"Querying teams batch {batch_num}/{total_batches}...",
                        "progress": progress
                    }) + "\n\n"
                
                # Query this batch
                teams_result = batch_query_account_teams(batch, batch_size=len(batch))
                
                if teams_result.get("success"):
                    # Merge results
                    account_sellers.update(teams_result.get("account_sellers", {}))
                    sellers_seen.update(teams_result.get("unique_sellers", {}))
                    account_ses.update(teams_result.get("account_ses", {}))
            
            # Populate seller info on account_data
            accounts_with_sellers = 0
            for account_data in accounts_data:
                acct_id = account_data["id"]
                if acct_id in account_sellers:
                    seller = account_sellers[acct_id]
                    account_data["seller_name"] = seller["name"]
                    account_data["seller_type"] = seller["type"]
                    accounts_with_sellers += 1
            
            # Build pod_teams by aggregating SEs from all accounts in each POD
            pod_teams = {}  # pod_name -> {data_se: [...], infra_se: [...], apps_se: [...]}
            for pod_name, acct_ids in pod_accounts.items():
                pod_teams[pod_name] = {"data_se": [], "infra_se": [], "apps_se": []}
                seen_ses = {"data_se": set(), "infra_se": set(), "apps_se": set()}
                
                for acct_id in acct_ids:
                    if acct_id in account_ses:
                        for role in ["data_se", "infra_se", "apps_se"]:
                            for se in account_ses[acct_id].get(role, []):
                                if se["name"] not in seen_ses[role]:
                                    seen_ses[role].add(se["name"])
                                    pod_teams[pod_name][role].append(se)
            
            yield "data: " + json.dumps({
                "message": f"Found {len(sellers_seen)} sellers for {accounts_with_sellers}/{len(accounts_data)} accounts, SEs for {len(pod_teams)} PODs",
                "progress": 86
            }) + "\n\n"
            
            # 6. Create database entities
            yield "data: " + json.dumps({"message": "Creating PODs..."}) + "\n\n"
            
            pods_map = {}  # pod_name -> POD object
            pods_created = 0
            
            for pod_name in pod_accounts.keys():
                existing = POD.query.filter_by(name=pod_name, user_id=user_id).first()
                if existing:
                    pods_map[pod_name] = existing
                else:
                    pod = POD(name=pod_name, user_id=user_id)
                    db.session.add(pod)
                    pods_map[pod_name] = pod
                    pods_created += 1
            
            db.session.flush()
            yield "data: " + json.dumps({
                "message": f"Created {pods_created} new PODs",
                "progress": 88
            }) + "\n\n"
            
            # Create Territories
            yield "data: " + json.dumps({"message": "Creating territories..."}) + "\n\n"
            
            territories_map = {}  # territory_name -> Territory object
            territories_created = 0
            
            for terr_name, terr_info in territories_seen.items():
                existing = Territory.query.filter_by(name=terr_name, user_id=user_id).first()
                if existing:
                    territories_map[terr_name] = existing
                    # Update POD if not set
                    if not existing.pod and terr_info.get("pod_name"):
                        existing.pod = pods_map.get(terr_info["pod_name"])
                else:
                    territory = Territory(name=terr_name, user_id=user_id)
                    if terr_info.get("pod_name"):
                        territory.pod = pods_map.get(terr_info["pod_name"])
                    db.session.add(territory)
                    territories_map[terr_name] = territory
                    territories_created += 1
            
            db.session.flush()
            yield "data: " + json.dumps({
                "message": f"Created {territories_created} new territories",
                "progress": 90
            }) + "\n\n"
            
            # Create Sellers
            yield "data: " + json.dumps({"message": "Creating sellers..."}) + "\n\n"
            
            sellers_map = {}  # seller_name -> Seller object
            sellers_created = 0
            
            for seller_name, seller_info in sellers_seen.items():
                existing = Seller.query.filter_by(name=seller_name, user_id=user_id).first()
                if existing:
                    sellers_map[seller_name] = existing
                else:
                    seller_type = seller_info.get("type", "Growth")
                    # Look up alias for new sellers using their systemuser ID
                    systemuser_id = seller_info.get("user_id")
                    alias = get_user_alias(systemuser_id) if systemuser_id else None
                    
                    seller = Seller(name=seller_name, seller_type=seller_type, alias=alias, user_id=user_id)
                    db.session.add(seller)
                    sellers_map[seller_name] = seller
                    sellers_created += 1
            
            db.session.flush()
            
            # Associate sellers with territories
            for account_data in accounts_data:
                seller_name = account_data.get("seller_name")
                territory_name = account_data.get("territory_name")
                if seller_name and territory_name:
                    seller = sellers_map.get(seller_name)
                    territory = territories_map.get(territory_name)
                    if seller and territory and territory not in seller.territories:
                        seller.territories.append(territory)
            
            db.session.flush()
            yield "data: " + json.dumps({
                "message": f"Created {sellers_created} new sellers",
                "progress": 92
            }) + "\n\n"
            
            # Create Solution Engineers
            yield "data: " + json.dumps({"message": "Creating solution engineers..."}) + "\n\n"
            
            solution_engineers_map = {}  # (name, specialty) -> SE object
            ses_created = 0
            
            # Map SE role keys to specialty names
            specialty_map = {
                "data_se": "Azure Data",
                "infra_se": "Azure Core and Infra",
                "apps_se": "Azure Apps and AI"
            }
            
            # Collect SE info from all POD teams
            for pod_name, team in pod_teams.items():
                pod = pods_map.get(pod_name)
                if not pod:
                    continue
                
                # Process all SE roles (each is now a list of SEs)
                for se_role, specialty in specialty_map.items():
                    se_list = team.get(se_role, [])
                    for se_info in se_list:
                        if not se_info.get("name"):
                            continue
                        
                        se_key = (se_info["name"], specialty)
                        if se_key not in solution_engineers_map:
                            existing = SolutionEngineer.query.filter_by(
                                name=se_info["name"],
                                specialty=specialty,
                                user_id=user_id
                            ).first()
                            if existing:
                                solution_engineers_map[se_key] = existing
                            else:
                                # Look up alias for new SEs using their systemuser ID
                                systemuser_id = se_info.get("user_id")
                                alias = get_user_alias(systemuser_id) if systemuser_id else None
                                
                                se = SolutionEngineer(
                                    name=se_info["name"],
                                    alias=alias,
                                    specialty=specialty,
                                    user_id=user_id
                                )
                                db.session.add(se)
                                solution_engineers_map[se_key] = se
                                ses_created += 1
                        
                        # Add POD association
                        se = solution_engineers_map.get(se_key)
                        if se and pod not in se.pods:
                            se.pods.append(pod)
            
            db.session.flush()
            yield "data: " + json.dumps({
                "message": f"Created {ses_created} new solution engineers",
                "progress": 93
            }) + "\n\n"
            
            # Create Verticals
            yield "data: " + json.dumps({"message": "Creating verticals..."}) + "\n\n"
            
            verticals_map = {}  # name -> Vertical object
            verticals_created = 0
            
            for vertical_name in verticals_seen:
                existing = Vertical.query.filter_by(name=vertical_name, user_id=user_id).first()
                if existing:
                    verticals_map[vertical_name] = existing
                else:
                    vertical = Vertical(name=vertical_name, user_id=user_id)
                    db.session.add(vertical)
                    verticals_map[vertical_name] = vertical
                    verticals_created += 1
            
            db.session.flush()
            yield "data: " + json.dumps({
                "message": f"Created {verticals_created} new verticals",
                "progress": 94
            }) + "\n\n"
            
            # Create Customers
            yield "data: " + json.dumps({
                "message": "Creating customers...",
                "progress": 95
            }) + "\n\n"
            
            customers_created = 0
            customers_skipped = 0
            
            for idx, account_data in enumerate(accounts_data, 1):
                if idx % 10 == 0:
                    yield "data: " + json.dumps({
                        "message": f"Processing customer {idx}/{len(accounts_data)}...",
                        "progress": 95 + int((idx / len(accounts_data)) * 4)  # 95-99%
                    }) + "\n\n"
                
                tpid = account_data.get("tpid")
                customer_name = account_data.get("name")
                
                if not tpid or not customer_name:
                    customers_skipped += 1
                    continue
                
                # Check if customer already exists by TPID
                existing = Customer.query.filter_by(tpid=tpid, user_id=user_id).first()
                if existing:
                    customers_skipped += 1
                    # Update tpid_url if not set
                    if not existing.tpid_url and account_data.get("url"):
                        existing.tpid_url = account_data["url"]
                    continue
                
                # Create new customer
                customer = Customer(
                    name=customer_name,
                    tpid=tpid,
                    tpid_url=account_data.get("url"),
                    user_id=user_id
                )
                
                # Associate territory
                territory_name = account_data.get("territory_name")
                if territory_name and territory_name in territories_map:
                    customer.territory = territories_map[territory_name]
                
                # Associate seller
                seller_name = account_data.get("seller_name")
                if seller_name and seller_name in sellers_map:
                    customer.seller = sellers_map[seller_name]
                
                # Associate verticals
                if account_data.get("vertical") and account_data["vertical"] in verticals_map:
                    customer.verticals.append(verticals_map[account_data["vertical"]])
                if account_data.get("vertical_category") and account_data["vertical_category"] in verticals_map:
                    vert = verticals_map[account_data["vertical_category"]]
                    if vert not in customer.verticals:
                        customer.verticals.append(vert)
                
                db.session.add(customer)
                customers_created += 1
            
            db.session.commit()
            
            # Final summary
            yield "data: " + json.dumps({
                "message": "Import complete!",
                "progress": 100,
                "complete": True,
                "summary": {
                    "pods_created": pods_created,
                    "territories_created": territories_created,
                    "sellers_created": sellers_created,
                    "solution_engineers_created": ses_created,
                    "verticals_created": verticals_created,
                    "customers_created": customers_created,
                    "customers_skipped": customers_skipped,
                    "duration": round(time.time() - import_start_time, 1),
                }
            }) + "\n\n"
            
            logger.info(
                f"MSX Import complete: {pods_created} PODs, {territories_created} territories, "
                f"{sellers_created} sellers, {ses_created} SEs, {verticals_created} verticals, "
                f"{customers_created} customers created, {customers_skipped} skipped"
            )
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Error during MSX import stream")
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
        finally:
            ctx.pop()
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )
