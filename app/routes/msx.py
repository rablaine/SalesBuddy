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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from flask import Blueprint, jsonify, request, Response, g, current_app, stream_with_context
import logging
from sqlalchemy.exc import SQLAlchemyError

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
    extract_account_id_from_url,
    create_task,
    close_task as close_msx_task,
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
    build_account_url,
    get_user_alias,
    get_user_info,
)
from app.models import Customer, InternalContact, Milestone, MsxTask, Opportunity, Territory, Seller, POD, SolutionEngineer, SyncStatus, Vertical, db, utc_now


logger = logging.getLogger(__name__)
ACCOUNT_SYNC_VERSION = 2


from app.services.account_sync import _extract_domain


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

    Accepts an optional JSON body ``{"scope": ".../.default"}``
    to include an OAuth scope in the login command.
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
    AI gateway token cache so the new credentials are picked up immediately.
    """
    status = get_az_cli_status()
    if not status.get("logged_in"):
        return jsonify({"success": False, "error": "Not logged in yet"}), 400

    set_subscription()
    token_ok = refresh_token()

    # Kill the az login process — it's still running in a console window
    # waiting for subscription selection, and we don't need it anymore.
    kill_az_login_process()

    # Clear gateway token cache so fresh credentials are used
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
    with indication of whether each milestone is used in any notes.
    """
    result = get_milestones_by_account(account_id)
    
    if not result.get("success"):
        return jsonify(result)
    
    # Check which milestones are already used in notes
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


@msx_bp.route('/milestones-for-customer/<int:customer_id>', methods=['GET', 'POST'])
def get_milestones_for_customer(customer_id: int):
    """
    Read customer milestone choices, or queue a fresh picker read in the shared worker.
    
    GET retains compatibility with existing pickers. The note editor uses POST
    for both its initial load and manual refresh, with persisted job observation.
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
    
    if request.method == 'POST':
        from app.routes.jobs import start_response
        return start_response('customer-milestones', {
            'customer_id': customer_id, 'picker_only': True,
        })

    from app.services.milestone_picker import get_customer_milestone_choices
    return jsonify(get_customer_milestone_choices(customer))


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
    
    Returns categories with HVA flags for UI highlighting.
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
    complete_on_create = data.get("complete_on_create") is True
    
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
        # Save the task locally so it shows in the workspace immediately
        local_task = None
        try:
            from app.services.msx_api import TASK_CATEGORIES, HVA_TASK_CATEGORIES
            local_ms = Milestone.query.filter_by(msx_milestone_id=milestone_id).first()
            if local_ms and result.get('task_id'):
                cat_name = next(
                    (c['label'] for c in TASK_CATEGORIES if c['value'] == task_category),
                    'Unknown',
                )
                local_task = MsxTask(
                    msx_task_id=result['task_id'],
                    msx_task_url=result.get('task_url'),
                    subject=subject,
                    description=description,
                    task_category=task_category,
                    task_category_name=cat_name,
                    is_hva=task_category in HVA_TASK_CATEGORIES,
                    milestone_id=local_ms.id,
                )
                if due_date:
                    from datetime import datetime as _dt
                    try:
                        local_task.due_date = _dt.fromisoformat(
                            due_date.replace('Z', '+00:00').replace('+00:00', '')
                        )
                    except (ValueError, AttributeError):
                        pass
                db.session.add(local_task)
                db.session.commit()
        except Exception as e:
            logger.warning(f'Could not save task locally: {e}')

        if complete_on_create and result.get('task_id'):
            close_result = close_msx_task(result['task_id'])
            result['completed'] = close_result.get('success', False)
            if result['completed'] and local_task:
                local_task.statecode = 1
                local_task.statuscode = 5
                db.session.commit()
            if not result['completed']:
                result['completion_warning'] = (
                    close_result.get('error') or 'MSX task completion failed'
                )
                logger.warning(
                    'Created MSX task %s but could not complete it: %s',
                    result['task_id'],
                    result['completion_warning'],
                )

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
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("MSX join succeeded but deal-team cache failed for %s", opportunity_id)
            return jsonify(success=False, remote_changed=True, error=(
                'MSX team membership changed, but the local cache could not be saved. '
                'Check MSX before retrying.'
            )), 500

    return jsonify(result)


@msx_bp.route('/leave-deal-team', methods=['POST'])
def leave_deal_team():
    """
    Remove the current user from an opportunity's deal team in MSX.

    Expected JSON body:
        opportunity_id: local opportunity ID (integer)

    Updates the local on_deal_team flag on success.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    from app.services.msx_api import remove_user_from_deal_team

    opportunity_id = request.json.get("opportunity_id")
    if not opportunity_id:
        return jsonify({"success": False, "error": "opportunity_id required"}), 400

    opportunity = Opportunity.query.get(opportunity_id)
    if not opportunity:
        return jsonify({"success": False, "error": "Opportunity not found"}), 404

    if not opportunity.msx_opportunity_id:
        return jsonify({"success": False, "error": "Opportunity has no MSX ID"}), 400

    result = remove_user_from_deal_team(opportunity.msx_opportunity_id)

    if result.get("success"):
        opportunity.on_deal_team = False
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("MSX leave succeeded but deal-team cache failed for %s", opportunity_id)
            return jsonify(success=False, remote_changed=True, error=(
                'MSX team membership changed, but the local cache could not be saved. '
                'Check MSX before retrying.'
            )), 500

    return jsonify(result)


# -----------------------------------------------------------------------------
# Task CRUD Routes (for MSX Workspace)
# -----------------------------------------------------------------------------

@msx_bp.route('/task/<string:task_id>/update', methods=['PATCH'])
def update_task_route(task_id: str):
    """Update task fields in MSX, including its task category."""
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    from app.services.msx_api import update_task
    result = update_task(task_id, request.json)
    return jsonify(result)


@msx_bp.route('/task/<string:task_id>/close', methods=['POST'])
def close_task_route(task_id: str):
    """Close a task in MSX and remove from local DB."""
    from app.services.msx_api import close_task
    result = close_task(task_id)
    if result.get('success'):
        local_task = MsxTask.query.filter_by(msx_task_id=task_id).first()
        if local_task:
            db.session.delete(local_task)
            db.session.commit()
    return jsonify(result)


@msx_bp.route('/task/<string:task_id>/delete', methods=['DELETE'])
def delete_task_route(task_id: str):
    """Delete a task from MSX and remove from local DB."""
    from app.services.msx_api import delete_task
    result = delete_task(task_id)
    if result.get('success'):
        local_task = MsxTask.query.filter_by(msx_task_id=task_id).first()
        if local_task:
            db.session.delete(local_task)
            db.session.commit()
    return jsonify(result)


@msx_bp.route('/milestone/<string:milestone_id>/update', methods=['PATCH'])
def update_milestone_route(milestone_id: str):
    """Update a milestone in MSX. Accepts msp_milestonedate, msp_monthlyuse."""
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    from app.services.msx_api import update_milestone
    result = update_milestone(milestone_id, request.json)
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
    """Admit the same selected-account import for all callers."""
    from app.routes.jobs import start_response
    return start_response('accounts-selected', request.get_json(silent=True))


# -----------------------------------------------------------------------------
# Streaming MSX Import (SSE) -- 3 concurrent workers
# -----------------------------------------------------------------------------

# Parallel worker helpers
_PARALLEL_WORKERS = 3
_ACCT_BATCH = 15
_TEAM_BATCH = 3


from app.services.account_sync import _split_chunks


from app.services.account_sync import _sse


from app.services.account_sync import _derive_pod_name


from app.services.account_sync import _drain


from app.services.account_sync import _account_import_sort_key


from app.services.account_sync import _update_customer_tpid_url


from app.services.account_sync import _par_query_accounts


from app.services.account_sync import _par_query_territories


from app.services.account_sync import _par_query_teams


@msx_bp.route('/accounts/sync', methods=['POST'])
def sync_accounts():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('accounts', {'source': request.args.get('source', 'default'), 'skip_aura': request.args.get('skip_aura') == '1'})


def run_account_sync_headless():
    """Admit the same account sync for a headless caller."""
    from app.services.sync_jobs import admit_sync
    return admit_sync('accounts', {'skip_aura': True}, initiator='headless')


@msx_bp.route('/sync-marketing', methods=['POST'])
def api_sync_marketing():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('marketing', None)
