"""
Routes for milestone management and milestone tracker.
Milestones are URLs from the MSX sales platform that can be linked to call logs.
The Milestone Tracker provides visibility into all active (uncommitted) milestones
across customers, sorted by dollar value and due date urgency.
"""
import json
import logging
import calendar as cal
from datetime import date, datetime, timezone
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, g, jsonify, Response, stream_with_context, current_app,
)
from app.models import db, Milestone, MsxTask, Note, Customer, Seller, SolutionEngineer, Favorite
from app.services.seller_mode import get_seller_mode_seller_id

logger = logging.getLogger(__name__)

bp = Blueprint('milestones', __name__)


@bp.route('/milestones')
def milestones_list():
    """List all milestones."""
    milestones = Milestone.query.order_by(Milestone.created_at.desc()).all()
    return render_template('milestones_list.html', milestones=milestones)


@bp.route('/milestone/new', methods=['GET', 'POST'])
def milestone_create():
    """Create a new milestone."""
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        title = request.form.get('title', '').strip() or None
        
        if not url:
            flash('URL is required', 'danger')
            return render_template('milestone_form.html', milestone=None)
        
        # Check for duplicate URL
        existing = Milestone.query.filter_by(url=url).first()
        if existing:
            flash('A milestone with this URL already exists', 'danger')
            return render_template('milestone_form.html', milestone=None)
        
        milestone = Milestone(
            url=url,
            title=title,
        )
        db.session.add(milestone)
        db.session.commit()
        
        flash('Milestone created successfully', 'success')
        return redirect(url_for('milestones.milestone_view', id=milestone.id))
    
    return render_template('milestone_form.html', milestone=None)


@bp.route('/milestone/<int:id>')
def milestone_view(id):
    """View a milestone and its associated call logs and tasks."""
    milestone = Milestone.query.get_or_404(id)
    tasks = MsxTask.query.filter_by(milestone_id=milestone.id).order_by(
        MsxTask.created_at.desc()
    ).all()
    # Parse cached MSX comments for immediate display
    cached_comments = None
    if milestone.cached_comments_json:
        try:
            cached_comments = json.loads(milestone.cached_comments_json)
        except (json.JSONDecodeError, TypeError):
            cached_comments = None
    # Check if owner matches a Seller or SE in our database
    owner_link = None
    if milestone.owner_name:
        seller = Seller.query.filter(
            db.func.lower(Seller.name) == milestone.owner_name.lower()
        ).first()
        if seller:
            owner_link = {'url': url_for('sellers.seller_view', id=seller.id), 'type': 'seller'}
        else:
            se = SolutionEngineer.query.filter(
                db.func.lower(SolutionEngineer.name) == milestone.owner_name.lower()
            ).first()
            if se:
                owner_link = {'url': url_for('solution_engineers.solution_engineer_view', id=se.id), 'type': 'se'}

    return render_template(
        'milestone_view.html',
        milestone=milestone,
        tasks=tasks,
        cached_comments=cached_comments,
        owner_link=owner_link,
    )


@bp.route('/api/milestone/<int:id>/detail')
def milestone_detail_fragment(id):
    """Return rendered HTML fragment of milestone view for modal embedding."""
    milestone = Milestone.query.get_or_404(id)
    tasks = MsxTask.query.filter_by(milestone_id=milestone.id).order_by(
        MsxTask.created_at.desc()
    ).all()
    cached_comments = None
    if milestone.cached_comments_json:
        try:
            cached_comments = json.loads(milestone.cached_comments_json)
        except (json.JSONDecodeError, TypeError):
            cached_comments = None
    owner_link = None
    if milestone.owner_name:
        seller = Seller.query.filter(
            db.func.lower(Seller.name) == milestone.owner_name.lower()
        ).first()
        if seller:
            owner_link = {'url': url_for('sellers.seller_view', id=seller.id), 'type': 'seller'}
        else:
            se = SolutionEngineer.query.filter(
                db.func.lower(SolutionEngineer.name) == milestone.owner_name.lower()
            ).first()
            if se:
                owner_link = {'url': url_for('solution_engineers.solution_engineer_view', id=se.id), 'type': 'se'}

    return render_template(
        'partials/milestone_view_content.html',
        milestone=milestone,
        tasks=tasks,
        cached_comments=cached_comments,
        owner_link=owner_link,
        show_edit_delete=False,
    )


@bp.route('/milestone/<int:id>/tasks', methods=['POST'])
def milestone_create_task(id):
    """
    Create a task on a milestone from the milestone view page.
    
    Expects JSON body with task details. Creates the task in MSX first,
    then stores a local MsxTask record (without a call log association).
    
    Returns JSON response for the modal form.
    """
    milestone = Milestone.query.get_or_404(id)
    
    if not request.is_json:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    
    data = request.json
    subject = (data.get("subject") or "").strip()
    task_category = data.get("task_category")
    duration_minutes = data.get("duration_minutes", 60)
    description = (data.get("description") or "").strip()
    due_date_str = data.get("due_date")
    
    if not subject:
        return jsonify({"success": False, "error": "Task title is required"}), 400
    if not task_category:
        return jsonify({"success": False, "error": "Task category is required"}), 400
    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "Milestone has no MSX ID — cannot create task"}), 400
    
    # Import here to avoid circular imports
    from app.services.msx_api import create_task, TASK_CATEGORIES, HOK_TASK_CATEGORIES
    
    # Create the task in MSX
    result = create_task(
        milestone_id=milestone.msx_milestone_id,
        subject=subject,
        task_category=int(task_category),
        duration_minutes=int(duration_minutes),
        description=description or None,
        due_date=due_date_str,
    )
    
    if not result.get("success"):
        return jsonify(result), 400
    
    # Look up category display name
    cat_info = next(
        (c for c in TASK_CATEGORIES if c["value"] == int(task_category)),
        {"label": "Unknown", "is_hok": False}
    )
    
    # Parse due date for local storage
    task_due_date = None
    if due_date_str:
        try:
            task_due_date = datetime.strptime(due_date_str[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            pass
    
    # Store local record (no note_id)
    msx_task = MsxTask(
        msx_task_id=result["task_id"],
        msx_task_url=result.get("task_url", ""),
        subject=subject,
        description=description or None,
        task_category=int(task_category),
        task_category_name=cat_info["label"],
        duration_minutes=int(duration_minutes),
        is_hok=int(task_category) in HOK_TASK_CATEGORIES,
        due_date=task_due_date,
        note_id=None,
        milestone_id=milestone.id,
    )
    db.session.add(msx_task)
    db.session.commit()
    
    logger.info(f"Created task '{subject}' on milestone {milestone.id} (MSX: {result['task_id']})")
    
    return jsonify({
        "success": True,
        "task_id": result["task_id"],
        "task_url": result.get("task_url", ""),
        "message": f"Task '{subject}' created successfully",
    })


@bp.route('/milestone/<int:id>/edit', methods=['GET', 'POST'])
def milestone_edit(id):
    """Edit a milestone."""
    milestone = Milestone.query.get_or_404(id)
    
    if request.method == 'POST':
        url = request.form.get('url', '').strip()
        title = request.form.get('title', '').strip() or None
        
        if not url:
            flash('URL is required', 'danger')
            return render_template('milestone_form.html', milestone=milestone)
        
        # Check for duplicate URL (excluding current milestone)
        existing = Milestone.query.filter(
            Milestone.url == url,
            Milestone.id != milestone.id
        ).first()
        if existing:
            flash('A milestone with this URL already exists', 'danger')
            return render_template('milestone_form.html', milestone=milestone)
        
        milestone.url = url
        milestone.title = title
        db.session.commit()
        
        flash('Milestone updated successfully', 'success')
        return redirect(url_for('milestones.milestone_view', id=milestone.id))
    
    return render_template('milestone_form.html', milestone=milestone)


@bp.route('/milestone/<int:id>/delete', methods=['POST'])
def milestone_delete(id):
    """Delete a milestone (only if not linked to any call logs)."""
    milestone = Milestone.query.get_or_404(id)
    
    # Protect milestones that are linked to call logs
    if milestone.notes:
        flash('Cannot delete this milestone — it is linked to notes. '
              'Remove the milestone from those call logs first.', 'danger')
        return redirect(url_for('milestones.milestone_view', id=milestone.id))
    
    db.session.delete(milestone)
    db.session.commit()
    
    flash('Milestone deleted successfully', 'success')
    return redirect(url_for('milestones.milestones_list'))


@bp.route('/api/milestone/<int:id>/msx-details')
def api_milestone_msx_details(id: int):
    """Fetch fresh milestone details and comments from MSX.

    Called via JS after the page loads so the initial render is instant.
    Caches comments and details_fetched_at back to the local DB.
    """
    from app.services.msx_api import get_milestone_details

    milestone = Milestone.query.get_or_404(id)
    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "No MSX ID on this milestone"})

    result = get_milestone_details(milestone.msx_milestone_id)
    if result.get("success"):
        msx_data = result["milestone"]
        try:
            # Cache details back to local DB
            if msx_data.get("title"):
                milestone.title = msx_data["title"]
            if msx_data.get("msx_status"):
                milestone.msx_status = msx_data["msx_status"]
            if msx_data.get("msx_status_code") is not None:
                milestone.msx_status_code = msx_data["msx_status_code"]
            if msx_data.get("customer_commitment"):
                milestone.customer_commitment = msx_data["customer_commitment"]
            if msx_data.get("milestone_number"):
                milestone.milestone_number = msx_data["milestone_number"]
            if msx_data.get("dollar_value") is not None:
                milestone.dollar_value = msx_data["dollar_value"]
            if msx_data.get("monthly_usage") is not None:
                milestone.monthly_usage = msx_data["monthly_usage"]
            if msx_data.get("workload"):
                milestone.workload = msx_data["workload"]
            if msx_data.get("owner_name"):
                milestone.owner_name = msx_data["owner_name"]
            if msx_data.get("due_date"):
                try:
                    milestone.due_date = datetime.fromisoformat(
                        msx_data["due_date"].replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass
            comments = msx_data.get("comments")
            if comments is not None:
                milestone.cached_comments_json = json.dumps(comments)
            milestone.details_fetched_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            logger.exception(f"Failed to cache MSX details for milestone {id}")
            db.session.rollback()

        return jsonify({"success": True, "milestone": msx_data})
    else:
        return jsonify({
            "success": False,
            "error": result.get("error", "Could not fetch from MSX"),
            "vpn_blocked": result.get("vpn_blocked", False),
        })


@bp.route('/milestone/<int:id>/comment', methods=['POST'])
def milestone_add_comment(id: int):
    """Post a comment to a milestone's MSX forecast comments (form submit)."""
    from app.services.msx_api import add_milestone_comment

    milestone = Milestone.query.get_or_404(id)
    comment_text = request.form.get('comment', '').strip()
    if not comment_text:
        flash('Comment cannot be empty.', 'warning')
        return redirect(url_for('milestones.milestone_view', id=id))

    if not milestone.msx_milestone_id:
        flash('This milestone has no MSX ID - cannot post comments.', 'danger')
        return redirect(url_for('milestones.milestone_view', id=id))

    result = add_milestone_comment(milestone.msx_milestone_id, comment_text)
    if result.get("success"):
        flash('Comment posted to MSX.', 'success')
    else:
        flash(
            f'Failed to post comment: {result.get("error", "Unknown error")}',
            'danger',
        )
    return redirect(url_for('milestones.milestone_view', id=id))


@bp.route('/api/milestone/<int:id>/comment', methods=['POST'])
def api_milestone_add_comment(id: int):
    """API endpoint to post a comment to a milestone (AJAX)."""
    from app.services.msx_api import add_milestone_comment

    milestone = Milestone.query.get_or_404(id)
    data = request.get_json()
    if not data or not data.get('comment', '').strip():
        return jsonify({"success": False, "error": "Comment cannot be empty"}), 400

    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "No MSX ID on this milestone"}), 400

    result = add_milestone_comment(
        milestone.msx_milestone_id,
        data['comment'].strip(),
    )
    return jsonify(result)


@bp.route('/api/milestone/<int:id>/comment', methods=['PUT'])
def api_milestone_edit_comment(id: int):
    """API endpoint to edit a milestone comment (AJAX)."""
    from app.services.msx_api import edit_milestone_comment

    milestone = Milestone.query.get_or_404(id)
    data = request.get_json()
    if not data or not data.get('comment', '').strip():
        return jsonify({"success": False, "error": "Comment cannot be empty"}), 400
    if not data.get('modifiedOn') or not data.get('userId'):
        return jsonify({"success": False, "error": "Missing comment identifier"}), 400
    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "No MSX ID on this milestone"}), 400

    result = edit_milestone_comment(
        milestone.msx_milestone_id,
        data['modifiedOn'],
        data['userId'],
        data['comment'].strip(),
    )
    return jsonify(result)


@bp.route('/api/milestone/<int:id>/comment', methods=['DELETE'])
def api_milestone_delete_comment(id: int):
    """API endpoint to delete a milestone comment (AJAX)."""
    from app.services.msx_api import delete_milestone_comment

    milestone = Milestone.query.get_or_404(id)
    data = request.get_json()
    if not data or not data.get('modifiedOn') or not data.get('userId'):
        return jsonify({"success": False, "error": "Missing comment identifier"}), 400
    if not milestone.msx_milestone_id:
        return jsonify({"success": False, "error": "No MSX ID on this milestone"}), 400

    result = delete_milestone_comment(
        milestone.msx_milestone_id,
        data['modifiedOn'],
        data['userId'],
    )
    return jsonify(result)


@bp.route('/api/milestones/find-or-create', methods=['POST'])
def api_find_or_create_milestone():
    """Find an existing milestone by URL or create a new one.
    
    Used by the call log form when associating a milestone URL.
    """
    data = request.get_json()
    if not data or not data.get('url'):
        return jsonify({'error': 'URL is required'}), 400
    
    url = data['url'].strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    # Try to find existing milestone
    milestone = Milestone.query.filter_by(url=url).first()
    
    if not milestone:
        # Create new milestone
        milestone = Milestone(
            url=url,
            title=None,
        )
        db.session.add(milestone)
        db.session.commit()
    
    return jsonify({
        'id': milestone.id,
        'url': milestone.url,
        'title': milestone.title,
        'display_text': milestone.display_text,
        'created': milestone is not None
    })


# =============================================================================
# Milestone Tracker
# =============================================================================

# Redirect from old URL
@bp.route('/milestone-tracker')
def _redirect_milestone_tracker():
    return redirect(url_for('milestones.milestone_tracker'), code=301)


@bp.route('/reports/milestone-tracker')
def milestone_tracker():
    """
    Milestone Tracker page.
    
    Shows all active (uncommitted) milestones across customers, sorted by
    dollar value and grouped by due date urgency. Provides a sync button
    to pull fresh data from MSX.
    """
    from app.services.milestone_sync import (
        get_milestone_tracker_data, get_milestone_tracker_data_for_seller
    )
    from app.models import SyncStatus
    
    seller_mode_sid = get_seller_mode_seller_id()
    locked_seller = None
    if seller_mode_sid:
        tracker_data = get_milestone_tracker_data_for_seller(seller_mode_sid)
        locked_seller = Seller.query.get(seller_mode_sid)
    else:
        tracker_data = get_milestone_tracker_data()
    
    sync_status = SyncStatus.get_status('milestones')
    favorited_ms_ids = {f.object_id for f in Favorite.query.filter_by(object_type='milestone').all()}
    return render_template(
        'milestone_tracker.html',
        milestones=tracker_data["milestones"],
        summary=tracker_data["summary"],
        last_sync=tracker_data.get("last_sync"),
        sellers=tracker_data.get("sellers", []),
        areas=tracker_data["areas"],
        quarters=tracker_data["quarters"],
        sync_status=sync_status,
        locked_seller=locked_seller,
        favorited_ms_ids=favorited_ms_ids,
    )


@bp.route('/api/milestone-tracker/sync', methods=['POST'])
def api_sync_milestones():
    """
    Trigger a milestone sync from MSX with Server-Sent Events progress.

    Streams real-time progress events as each customer is synced.
    Falls back to JSON response if Accept header doesn't include event-stream.
    """
    from app.models import Customer
    if Customer.query.first() is None:
        return jsonify({'success': False, 'error': 'Import accounts first'}), 400

    from app.services.milestone_sync import (
        sync_all_customer_milestones,
        sync_all_customer_milestones_stream,
    )

    # SSE streaming path
    if 'text/event-stream' in request.headers.get('Accept', ''):
        def generate():
            yield from sync_all_customer_milestones_stream()

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    # JSON fallback for non-SSE clients (e.g. scheduled task script).
    # Fire-and-forget: start sync in a background thread and return 202
    # immediately so the caller doesn't time out on long syncs.
    import threading

    def _run_sync(app):
        with app.app_context():
            try:
                sync_all_customer_milestones()
            except Exception:
                logger.exception("Background milestone sync failed")

    t = threading.Thread(
        target=_run_sync,
        args=(current_app._get_current_object(),),
        daemon=True,
    )
    t.start()
    return jsonify({
        "success": True,
        "message": "Milestone sync started in background.",
        "async": True,
    }), 202


@bp.route('/api/milestone-tracker/sync-customer/<int:customer_id>', methods=['POST'])
def api_sync_customer_milestones(customer_id):
    """
    Sync milestones from MSX for a single customer.
    
    Args:
        customer_id: The customer ID to sync.
        
    Returns:
        JSON with sync results for the single customer.
    """
    from app.models import Customer
    from app.services.milestone_sync import sync_customer_milestones
    
    customer = Customer.query.get_or_404(customer_id)
    
    if not customer.tpid_url:
        return jsonify({
            "success": False,
            "error": "Customer has no MSX account link (tpid_url).",
        }), 400
    
    try:
        result = sync_customer_milestones(customer)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Milestone sync failed for customer {customer_id}")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


# =============================================================================
# Milestone Calendar API
# =============================================================================

ACTIVE_STATUSES = ['On Track', 'At Risk', 'Blocked']


@bp.route('/api/milestones/calendar')
def milestones_calendar_api():
    """API endpoint returning milestone due dates for a calendar view.

    Query params:
        year:      int (default: current year)
        month:     int, 1-12 (default: current month)
        team_only: '1' to show only milestones where user is on the team
        status:    milestone status string (e.g. 'On Track', 'At Risk', 'Blocked')
        seller_id: int seller ID to filter by
        area:      workload area prefix (e.g. 'Infra', 'Data & AI')
        quarters:  comma-separated fiscal quarter strings (e.g. 'FY26 Q3,FY26 Q4')
        urgency:   urgency level ('past_due', 'this_week', 'this_month')

    Returns JSON with:
        - year, month, month_name
        - days: dict mapping day number -> list of milestone dicts
        - days_in_month, today_day
    """
    today = date.today()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)

    # Filter params
    status_filter = request.args.get('status', '')
    seller_id_param = request.args.get('seller_id', '', type=str)
    area_filter = request.args.get('area', '')
    quarters_filter = request.args.get('quarters', '')
    urgency_filter = request.args.get('urgency', '')

    if month < 1 or month > 12:
        month = today.month

    first_day = date(year, month, 1)
    last_day = date(year, month, cal.monthrange(year, month)[1])

    milestones_q = (
        Milestone.query
        .filter(
            Milestone.due_date >= first_day,
            Milestone.due_date <= last_day,
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
    )

    # Status filter (server-side) - supports comma-separated for multi-select
    if status_filter:
        status_values = [s.strip() for s in status_filter.split(',') if s.strip()]
        if len(status_values) == 1:
            milestones_q = milestones_q.filter(Milestone.msx_status == status_values[0])
        elif len(status_values) > 1:
            milestones_q = milestones_q.filter(Milestone.msx_status.in_(status_values))

    # Seller filter: seller mode takes priority, then explicit param
    seller_mode_sid = get_seller_mode_seller_id()
    if seller_mode_sid:
        milestones_q = milestones_q.join(
            Customer, Milestone.customer_id == Customer.id
        ).filter(Customer.seller_id == seller_mode_sid)
    elif seller_id_param:
        milestones_q = milestones_q.join(
            Customer, Milestone.customer_id == Customer.id
        ).filter(Customer.seller_id == int(seller_id_param))

    # Team filter
    if request.args.get('team_only') == '1':
        milestones_q = milestones_q.filter(Milestone.on_my_team.is_(True))

    milestones = milestones_q.order_by(Milestone.due_date).all()

    # Post-query filters for area, quarters, urgency (computed fields)
    quarters_set = (
        set(q.strip() for q in quarters_filter.split(',') if q.strip())
        if quarters_filter else None
    )
    filtered = []
    for ms in milestones:
        # Area filter - supports comma-separated for multi-select
        if area_filter:
            area_values = [a.strip() for a in area_filter.split(',') if a.strip()]
            wl_area = ''
            if ms.workload and ':' in ms.workload:
                wl_area = ms.workload.split(':', 1)[0].strip()
            elif ms.workload:
                wl_area = ms.workload.strip()
            if wl_area not in area_values:
                continue

        # Quarters filter
        if quarters_set and ms.due_date:
            m = ms.due_date.month
            y = ms.due_date.year
            if m >= 7:
                fy = y + 1
                q = 1 if m <= 9 else 2
            else:
                fy = y
                q = 3 if m <= 3 else 4
            if f"FY{fy % 100:02d} Q{q}" not in quarters_set:
                continue

        # Urgency filter
        if urgency_filter and ms.due_date_urgency != urgency_filter:
            continue

        filtered.append(ms)

    days: dict = {}
    for ms in filtered:
        day = ms.due_date.day
        if day not in days:
            days[day] = []
        days[day].append({
            'id': ms.id,
            'title': ms.display_text,
            'milestone_number': ms.milestone_number,
            'status': ms.msx_status,
            'monthly_usage': ms.monthly_usage,
            'workload': ms.workload,
            'on_my_team': ms.on_my_team,
            'customer_name': ms.customer.get_display_name() if ms.customer else 'Unknown',
            'customer_id': ms.customer.id if ms.customer else None,
            'seller_name': ms.customer.seller.name if ms.customer and ms.customer.seller else None,
            'url': ms.url,
        })

    return jsonify({
        'year': year,
        'month': month,
        'month_name': cal.month_name[month],
        'days': days,
        'days_in_month': cal.monthrange(year, month)[1],
        'today_day': today.day if today.year == year and today.month == month else None,
    })


# =============================================================================
# Favorites
# =============================================================================

@bp.route('/api/milestone/<int:id>/favorite', methods=['POST'])
def api_toggle_milestone_favorite(id: int):
    """Toggle the favorite state of a milestone.

    Returns the new is_favorited value. Creates or deletes a Favorite row -
    no changes made to the Milestone model itself.
    """
    Milestone.query.get_or_404(id)
    is_favorited = Favorite.toggle('milestone', id)
    return jsonify(success=True, is_favorited=is_favorited)

