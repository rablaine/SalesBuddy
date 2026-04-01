"""
Opportunity routes for Sales Buddy.
Handles viewing opportunity details (fetched fresh from MSX) and posting comments.
"""
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g

from app.models import db, Opportunity, Milestone, Favorite
from app.services.msx_api import (
    get_opportunity,
    add_opportunity_comment,
    edit_opportunity_comment,
    delete_opportunity_comment,
    build_opportunity_url,
)

logger = logging.getLogger(__name__)

# Create blueprint
opportunities_bp = Blueprint('opportunities', __name__)


@opportunities_bp.route('/opportunity/<int:id>')
def opportunity_view(id: int):
    """
    View opportunity details.

    Loads the local Opportunity record and milestones immediately.
    MSX details (status, value, comments) are lazy-loaded via JS fetch
    to /api/opportunity/<id>/msx-details so the page renders instantly.
    """
    opportunity = Opportunity.query.get_or_404(id)

    # Get local milestones linked to this opportunity
    milestones = Milestone.query.filter_by(
        opportunity_id=opportunity.id
    ).order_by(Milestone.msx_status, Milestone.title).all()

    # Build the MSX URL from the GUID so the "Open in MSX" link works immediately
    msx_url = build_opportunity_url(opportunity.msx_opportunity_id)

    # Parse cached comments if available
    cached_comments = None
    if opportunity.cached_comments_json:
        try:
            cached_comments = json.loads(opportunity.cached_comments_json)
        except (json.JSONDecodeError, TypeError):
            cached_comments = None

    return render_template(
        'opportunity_view.html',
        opportunity=opportunity,
        milestones=milestones,
        msx_url=msx_url,
        cached_comments=cached_comments,
        is_favorited=Favorite.is_favorited('opportunity', opportunity.id),
    )


@opportunities_bp.route('/api/opportunity/<int:id>/msx-details')
def api_opportunity_msx_details(id: int):
    """
    API endpoint to fetch fresh MSX opportunity details (details + comments).

    Called via JS after the page loads so the initial render is instant.
    On success, caches the fetched details back to the local DB so subsequent
    page loads show the last-known data immediately.
    """
    opportunity = Opportunity.query.get_or_404(id)

    msx_result = get_opportunity(opportunity.msx_opportunity_id)
    if msx_result.get("success"):
        opp_data = msx_result["opportunity"]

        # Cache details back to local DB
        try:
            opportunity.name = opp_data.get("name") or opportunity.name
            opportunity.opportunity_number = opp_data.get("number") or opportunity.opportunity_number
            opportunity.statecode = opp_data.get("statecode")
            opportunity.state = opp_data.get("state")
            opportunity.status_reason = opp_data.get("status")
            opportunity.estimated_value = opp_data.get("estimated_value")
            opportunity.estimated_close_date = opp_data.get("estimated_close_date")
            opportunity.owner_name = opp_data.get("owner")
            opportunity.compete_threat = opp_data.get("compete_threat")
            opportunity.customer_need = opp_data.get("customer_need")
            opportunity.description = opp_data.get("description")
            opportunity.msx_url = opp_data.get("url")
            # Cache comments as JSON string
            comments = opp_data.get("comments")
            if comments is not None:
                opportunity.cached_comments_json = json.dumps(comments)
            opportunity.details_fetched_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            logger.exception(f"Failed to cache MSX details for opportunity {id}")
            db.session.rollback()

        return jsonify({
            "success": True,
            "opportunity": opp_data,
        })
    else:
        return jsonify({
            "success": False,
            "error": msx_result.get("error", "Could not fetch from MSX"),
            "vpn_blocked": msx_result.get("vpn_blocked", False),
        })


@opportunities_bp.route('/opportunity/<int:id>/comment', methods=['POST'])
def opportunity_add_comment(id: int):
    """Post a new comment to an opportunity's MSX forecast comments."""
    opportunity = Opportunity.query.get_or_404(id)
    
    comment_text = request.form.get('comment', '').strip()
    if not comment_text:
        flash('Comment cannot be empty.', 'warning')
        return redirect(url_for('opportunities.opportunity_view', id=id))
    
    result = add_opportunity_comment(opportunity.msx_opportunity_id, comment_text)
    
    if result.get("success"):
        flash('Comment posted to MSX.', 'success')
    else:
        flash(f'Failed to post comment: {result.get("error", "Unknown error")}', 'danger')
    
    return redirect(url_for('opportunities.opportunity_view', id=id))


@opportunities_bp.route('/api/opportunity/<int:id>/comment', methods=['POST'])
def api_opportunity_add_comment(id: int):
    """API endpoint to post a comment (for AJAX usage)."""
    opportunity = Opportunity.query.get_or_404(id)
    
    data = request.get_json()
    if not data or not data.get('comment', '').strip():
        return jsonify({"success": False, "error": "Comment cannot be empty"}), 400
    
    result = add_opportunity_comment(
        opportunity.msx_opportunity_id,
        data['comment'].strip()
    )
    
    return jsonify(result)


@opportunities_bp.route('/api/opportunity/<int:id>/comment', methods=['PUT'])
def api_opportunity_edit_comment(id: int):
    """API endpoint to edit an opportunity comment (AJAX)."""
    opportunity = Opportunity.query.get_or_404(id)
    data = request.get_json()
    if not data or not data.get('comment', '').strip():
        return jsonify({"success": False, "error": "Comment cannot be empty"}), 400
    if not data.get('modifiedOn') or not data.get('userId'):
        return jsonify({"success": False, "error": "Missing comment identifier"}), 400
    if not opportunity.msx_opportunity_id:
        return jsonify({"success": False, "error": "No MSX ID on this opportunity"}), 400

    result = edit_opportunity_comment(
        opportunity.msx_opportunity_id,
        data['modifiedOn'],
        data['userId'],
        data['comment'].strip(),
    )
    return jsonify(result)


@opportunities_bp.route('/api/opportunity/<int:id>/comment', methods=['DELETE'])
def api_opportunity_delete_comment(id: int):
    """API endpoint to delete an opportunity comment (AJAX)."""
    opportunity = Opportunity.query.get_or_404(id)
    data = request.get_json()
    if not data or not data.get('modifiedOn') or not data.get('userId'):
        return jsonify({"success": False, "error": "Missing comment identifier"}), 400
    if not opportunity.msx_opportunity_id:
        return jsonify({"success": False, "error": "No MSX ID on this opportunity"}), 400

    result = delete_opportunity_comment(
        opportunity.msx_opportunity_id,
        data['modifiedOn'],
        data['userId'],
    )
    return jsonify(result)


# =============================================================================
# Favorites
# =============================================================================

@opportunities_bp.route('/api/opportunity/<int:id>/favorite', methods=['POST'])
def api_toggle_opportunity_favorite(id: int):
    """Toggle the favorite state of an opportunity.

    Returns the new is_favorited value. Creates or deletes a Favorite row -
    no changes made to the Opportunity model itself.
    """
    Opportunity.query.get_or_404(id)
    is_favorited = Favorite.toggle('opportunity', id)
    return jsonify(success=True, is_favorited=is_favorited)
