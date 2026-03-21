"""
Main routes for Sales Buddy.
Handles index, search, preferences, and API endpoints.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, session, g, send_from_directory, current_app)
from datetime import datetime, timezone, date, timedelta
from sqlalchemy import func, extract
import calendar as cal
import json
import os
import re

from app.models import (db, Note, Customer, Seller, Territory, Topic,
                        UserPreference, NoteTemplate, User, SyncStatus,
                        Engagement, ActionItem, Milestone, RevenueAnalysis)
from app.services.backup import backup_template, delete_template_backup
from app.services.seller_mode import get_seller_mode_seller_id

# Create blueprint
main_bp = Blueprint('main', __name__)


# =============================================================================
# PWA Support
# =============================================================================

@main_bp.route('/sw.js')
def service_worker():
    """Serve service worker from root scope so it can control all routes."""
    return send_from_directory(
        os.path.join(current_app.root_path, '..', 'static'),
        'sw.js',
        mimetype='application/javascript'
    )


@main_bp.route('/manifest.json')
def manifest():
    """Serve PWA manifest from root path."""
    return send_from_directory(
        os.path.join(current_app.root_path, '..', 'static'),
        'manifest.json',
        mimetype='application/json'
    )


# =============================================================================
# Health Check Endpoint
# =============================================================================

@main_bp.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for Azure App Service monitoring.
    Returns 200 OK if app is healthy and can connect to database.
    """
    try:
        # Test database connectivity with a simple query
        db.session.execute(db.text('SELECT 1'))
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'timestamp': datetime.now(timezone.utc).isoformat()
        }), 200
    except Exception as e:
        # Return 503 Service Unavailable if database is down
        return jsonify({
            'status': 'unhealthy',
            'database': 'disconnected',
            'error': str(e),
            'timestamp': datetime.now(timezone.utc).isoformat()
        }), 503


# =============================================================================
# Helper Functions
# =============================================================================

def get_seller_color(seller_id: int) -> str:
    """
    Generate a consistent, visually distinct color for a seller based on their ID.
    Returns a CSS color class name.
    """
    # Define a palette of distinct, accessible colors
    color_classes = [
        'seller-color-1',   # Purple
        'seller-color-2',   # Teal
        'seller-color-3',   # Red
        'seller-color-4',   # Pink
        'seller-color-5',   # Blue
        'seller-color-6',   # Emerald
        'seller-color-7',   # Yellow
        'seller-color-8',   # Orange
        'seller-color-9',   # Slate
        'seller-color-10',  # Brown
    ]
    
    # Use modulo to cycle through colors if we have more sellers than colors
    color_index = (seller_id - 1) % len(color_classes)
    return color_classes[color_index]


# =============================================================================
# Main Routes
# =============================================================================

@main_bp.route('/')
def index():
    """Home page dashboard showing actionable items and activity."""
    today = date.today()
    seller_mode_sid = get_seller_mode_seller_id()

    # Open action items from engagements, ordered by due date
    open_tasks_q = ActionItem.query.filter(
        ActionItem.status == 'open'
    ).options(
        db.joinedload(ActionItem.engagement).joinedload(Engagement.customer)
    )
    if seller_mode_sid:
        open_tasks_q = open_tasks_q.join(
            Engagement, ActionItem.engagement_id == Engagement.id
        ).join(Customer, Engagement.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )
    open_tasks = open_tasks_q.order_by(
        ActionItem.due_date.asc().nullslast(),
        ActionItem.priority.desc(),
        ActionItem.created_at.desc()
    ).all()

    # Milestones due in the next 30 days (active, on my team only)
    milestones_q = Milestone.query.filter(
        Milestone.on_my_team == True,
        Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
        Milestone.due_date.isnot(None),
        Milestone.due_date <= today + timedelta(days=30),
    ).options(
        db.joinedload(Milestone.customer)
    )
    if seller_mode_sid:
        milestones_q = milestones_q.join(Customer, Milestone.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )
    upcoming_milestones = milestones_q.order_by(Milestone.due_date.asc()).all()

    # Unactioned revenue alerts (new or to_be_reviewed, top priority)
    alerts_q = RevenueAnalysis.query.filter(
        RevenueAnalysis.review_status.in_(['new', 'to_be_reviewed'])
    )
    if seller_mode_sid:
        seller = Seller.query.get(seller_mode_sid)
        if seller:
            alerts_q = alerts_q.filter(RevenueAnalysis.seller_name == seller.name)
    revenue_alerts = alerts_q.order_by(
        RevenueAnalysis.priority_score.desc()
    ).limit(10).all()

    has_milestones = Milestone.query.first() is not None
    engagement_q = Engagement.query.filter(
        Engagement.status.in_(['Active', 'On Hold'])
    )
    if seller_mode_sid:
        engagement_q = engagement_q.join(Customer, Engagement.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )
    engagement_count = engagement_q.count()

    return render_template(
        'index.html',
        open_tasks=open_tasks,
        upcoming_milestones=upcoming_milestones,
        revenue_alerts=revenue_alerts,
        has_milestones=has_milestones,
        has_engagements=engagement_count > 0,
        engagement_count=engagement_count,
    )


@main_bp.route('/api/notes/calendar')
def notes_calendar_api():
    """API endpoint returning call logs for calendar view.
    
    Query params:
        year: int (default: current year)
        month: int, 1-12 (default: current month)
    
    Returns JSON with:
        - year, month: the requested period
        - days: dict mapping day number -> list of {id, customer_name, customer_id}
        - month_name: human-readable month name
        - prev/next month info for navigation
    """
    today = date.today()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)
    
    # Validate month range
    if month < 1 or month > 12:
        month = today.month
    
    # Get first and last day of the month
    first_day = date(year, month, 1)
    last_day = date(year, month, cal.monthrange(year, month)[1])
    
    # Query call logs for this month with customer data and relationships
    seller_mode_sid = get_seller_mode_seller_id()
    cal_query = Note.query.options(
        db.joinedload(Note.customer),
        db.joinedload(Note.milestones),
        db.joinedload(Note.topics),
    ).filter(
        Note.call_date >= first_day,
        Note.call_date <= last_day
    )
    if seller_mode_sid:
        cal_query = cal_query.join(Customer, Note.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )
    notes = cal_query.order_by(Note.call_date).all()
    
    # Group by day (notes already sorted by call_date from query)
    days = {}
    for log in notes:
        day = log.call_date.day
        if day not in days:
            days[day] = []

        # For general notes, build a short label from content or topics
        if log.customer:
            display_name = log.customer.get_display_name()
            is_general = False
        else:
            # Use first topic name, or a snippet of the content
            if log.topics:
                display_name = log.topics[0].name
            else:
                plain = re.sub(r'<[^>]+>', '', log.content or '').strip()
                display_name = (plain[:30] + '...') if len(plain) > 30 else plain
                display_name = display_name or 'General Note'
            is_general = True

        days[day].append({
            'id': log.id,
            'customer_name': display_name,
            'customer_id': log.customer.id if log.customer else None,
            'is_general': is_general,
            'has_milestone': len(log.milestones) > 0,
            'has_task': log.msx_tasks.count() > 0,
            'has_hok': any(t.is_hok for t in log.msx_tasks.all()),
            'has_engagement': len(log.engagements) > 0,
            'time': log.call_date.strftime('%I:%M %p').lstrip('0') if log.call_date.hour != 0 or log.call_date.minute != 0 else None
        })
    
    # Calculate prev/next month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    
    # Calendar info for rendering
    first_weekday = first_day.weekday()  # Monday = 0, Sunday = 6
    # Shift to Sunday-start week: Sunday = 0
    first_weekday = (first_weekday + 1) % 7
    days_in_month = cal.monthrange(year, month)[1]
    
    return jsonify({
        'year': year,
        'month': month,
        'month_name': cal.month_name[month],
        'days': days,
        'first_weekday': first_weekday,
        'days_in_month': days_in_month,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
        'today_day': today.day if today.year == year and today.month == month else None
    })


@main_bp.route('/api/engagements/active')
def api_active_engagements():
    """Return active/on-hold engagements for the homepage tab."""
    status_filter = request.args.get('status', '').strip()

    query = Engagement.query.filter(
        Engagement.status.in_(['Active', 'On Hold'])
    )
    if status_filter in ('Active', 'On Hold'):
        query = query.filter(Engagement.status == status_filter)

    seller_mode_sid = get_seller_mode_seller_id()
    if seller_mode_sid:
        query = query.join(Customer, Engagement.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )

    # Eager-load relationships to avoid N+1
    from sqlalchemy.orm import joinedload, subqueryload
    query = query.options(
        joinedload(Engagement.customer).joinedload(Customer.seller),
        subqueryload(Engagement.notes),
        subqueryload(Engagement.opportunities),
        subqueryload(Engagement.milestones),
    )

    engagements = query.order_by(Engagement.updated_at.desc()).all()

    results = []
    for eng in engagements:
        results.append({
            'id': eng.id,
            'title': eng.title,
            'status': eng.status,
            'customer_name': eng.customer.name if eng.customer else 'Unknown',
            'customer_id': eng.customer_id,
            'seller_name': (eng.customer.seller.name
                           if eng.customer and eng.customer.seller else None),
            'customer_favicon': (eng.customer.favicon_b64
                                if eng.customer and eng.customer.favicon_b64
                                else None),
            'estimated_acr': eng.estimated_acr,
            'target_date': eng.target_date.isoformat() if eng.target_date else None,
            'story_completeness': eng.story_completeness,
            'linked_note_count': eng.linked_note_count,
            'opportunity_count': len(eng.opportunities),
            'milestone_count': len(eng.milestones),
            'updated_at': eng.updated_at.isoformat() if eng.updated_at else None,
        })

    return jsonify({'success': True, 'engagements': results, 'count': len(results)})


@main_bp.route('/search')
def search():
    """Search and filter call logs (FR011)."""
    # Get filter parameters
    search_text = request.args.get('q', '').strip()
    customer_id = request.args.get('customer_id', type=int)
    seller_id = request.args.get('seller_id', type=int)
    territory_id = request.args.get('territory_id', type=int)
    topic_ids = request.args.getlist('topic_ids', type=int)
    seller_mode_sid = get_seller_mode_seller_id()
    
    # In seller mode, force seller filter
    if seller_mode_sid:
        seller_id = seller_mode_sid
    
    # Check if any search criteria provided
    has_search = bool(search_text or customer_id or seller_id or territory_id or topic_ids)
    
    notes = []
    grouped_data = {}
    
    # Only perform search if criteria provided
    if has_search:
        # Start with base query filtered by user
        query = Note.query
        
        # Apply filters
        if search_text:
            query = query.filter(Note.content.ilike(f'%{search_text}%'))
        
        if customer_id:
            query = query.filter(Note.customer_id == customer_id)
        
        if seller_id:
            query = query.join(Customer).filter(Customer.seller_id == seller_id)
        
        if territory_id:
            if not seller_id:  # Avoid duplicate join
                query = query.join(Customer)
            query = query.filter(Customer.territory_id == territory_id)
        
        if topic_ids:
            # Filter by topics (call logs that have ANY of the selected topics)
            query = query.join(Note.topics).filter(Topic.id.in_(topic_ids))
        
        # Get filtered call logs
        notes = query.order_by(Note.call_date.desc()).all()
        
        # Group call logs by Seller → Customer structure (FR011)
        # Structure: { seller_id: { 'seller': Seller, 'customers': { customer_id: { 'customer': Customer, 'calls': [Note] } } } }
        for call in notes:
            seller_id_key = call.seller.id if call.seller else 0  # 0 = no seller
            customer_id_key = call.customer_id if call.customer_id else 0  # 0 = no customer
            
            # Initialize seller group
            if seller_id_key not in grouped_data:
                grouped_data[seller_id_key] = {
                    'seller': call.seller,
                    'customers': {}
                }
            
            # Initialize customer group
            if customer_id_key not in grouped_data[seller_id_key]['customers']:
                grouped_data[seller_id_key]['customers'][customer_id_key] = {
                    'customer': call.customer,
                    'calls': [],
                    'most_recent_date': call.call_date
                }
            
            # Add call to customer group
            grouped_data[seller_id_key]['customers'][customer_id_key]['calls'].append(call)
            
            # Update most recent date
            if call.call_date > grouped_data[seller_id_key]['customers'][customer_id_key]['most_recent_date']:
                grouped_data[seller_id_key]['customers'][customer_id_key]['most_recent_date'] = call.call_date
        
        # Sort customers by most recent call within each seller
        for seller_id_key in grouped_data:
            customers_list = list(grouped_data[seller_id_key]['customers'].values())
            customers_list.sort(key=lambda x: x['most_recent_date'], reverse=True)
            grouped_data[seller_id_key]['customers_sorted'] = customers_list
    
    # Get all filter options for dropdowns
    customers = Customer.query.order_by(Customer.name).all()
    sellers = Seller.query.order_by(Seller.name).all()
    territories = Territory.query.order_by(Territory.name).all()
    topics = Topic.query.order_by(Topic.name).all()
    
    return render_template('search.html',
                         grouped_data=grouped_data,
                         notes=notes,
                         search_text=search_text,
                         selected_customer_id=customer_id,
                         selected_seller_id=seller_id,
                         selected_territory_id=territory_id,
                         selected_topic_ids=topic_ids,
                         customers=customers,
                         sellers=sellers,
                         territories=territories,
                         topics=topics)


@main_bp.route('/preferences')
def preferences():
    """User settings page with Appearance, WorkIQ, and Note Templates."""
    pref = UserPreference.query.first()
    templates = NoteTemplate.query.order_by(NoteTemplate.name).all()

    from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
    workiq_prompt = pref.workiq_summary_prompt if pref and pref.workiq_summary_prompt else DEFAULT_SUMMARY_PROMPT

    return render_template('settings.html',
                         pref=pref,
                         templates=templates,
                         workiq_prompt=workiq_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT)


# =============================================================================
# Note Template CRUD
# =============================================================================

@main_bp.route('/templates/new')
def template_create():
    """Show blank template editor."""
    return render_template('note_template_form.html', template=None)


@main_bp.route('/templates/<int:id>/edit')
def template_edit(id):
    """Show template editor with existing content."""
    template = NoteTemplate.query.get_or_404(id)
    return render_template('note_template_form.html', template=template)


@main_bp.route('/templates', methods=['POST'])
def template_save():
    """Create a new template."""
    name = request.form.get('name', '').strip()
    content = request.form.get('content', '').strip()

    if not name:
        flash('Template name is required.', 'danger')
        return redirect(url_for('main.template_create'))

    if not content:
        flash('Template content is required.', 'danger')
        return redirect(url_for('main.template_create'))

    template = NoteTemplate(name=name, content=content)
    db.session.add(template)
    db.session.commit()

    backup_template(template.id)

    flash(f'Template "{name}" created.', 'success')
    return redirect(url_for('main.preferences'))


@main_bp.route('/templates/<int:id>', methods=['POST'])
def template_update(id):
    """Update an existing template."""
    template = NoteTemplate.query.get_or_404(id)
    name = request.form.get('name', '').strip()
    content = request.form.get('content', '').strip()

    if not name:
        flash('Template name is required.', 'danger')
        return redirect(url_for('main.template_edit', id=id))

    if not content:
        flash('Template content is required.', 'danger')
        return redirect(url_for('main.template_edit', id=id))

    template.name = name
    template.content = content
    db.session.commit()

    backup_template(template.id)

    flash(f'Template "{name}" updated.', 'success')
    return redirect(url_for('main.preferences'))


# =============================================================================
# Note Template API Endpoints
# =============================================================================

@main_bp.route('/api/templates')
def api_templates_list():
    """Return all templates as JSON (for dropdowns)."""
    templates = NoteTemplate.query.order_by(NoteTemplate.name).all()
    return jsonify([{
        'id': t.id,
        'name': t.name,
    } for t in templates])


@main_bp.route('/api/templates/<int:id>')
def api_template_get(id):
    """Return template content as JSON (for AJAX on note form)."""
    template = NoteTemplate.query.get_or_404(id)
    return jsonify({
        'id': template.id,
        'name': template.name,
        'content': template.content,
    })


@main_bp.route('/api/templates/<int:id>/delete', methods=['POST'])
def api_template_delete(id):
    """Delete a template. Clears any default references."""
    template = NoteTemplate.query.get_or_404(id)

    # Clear default references in UserPreference
    pref = UserPreference.query.first()
    if pref:
        if pref.default_template_customer_id == id:
            pref.default_template_customer_id = None
        if pref.default_template_noncustomer_id == id:
            pref.default_template_noncustomer_id = None

    db.session.delete(template)
    db.session.commit()

    delete_template_backup(id)

    flash(f'Template "{template.name}" deleted.', 'success')
    return redirect(url_for('main.preferences'))


@main_bp.route('/api/preferences/default-templates', methods=['POST'])
def api_save_default_templates():
    """Save default template preferences for customer and non-customer notes."""
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)

    customer_id = request.form.get('default_template_customer_id')
    noncustomer_id = request.form.get('default_template_noncustomer_id')

    pref.default_template_customer_id = int(customer_id) if customer_id else None
    pref.default_template_noncustomer_id = int(noncustomer_id) if noncustomer_id else None
    db.session.commit()

    flash('Default templates saved.', 'success')
    return redirect(url_for('main.preferences'))


@main_bp.route('/analytics')
def analytics():
    """Analytics and insights dashboard."""
    from datetime import date, timedelta
    from sqlalchemy import func, distinct
    
    # Date ranges
    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    three_months_ago = today - timedelta(days=90)
    
    # Call volume metrics
    total_calls = Note.query.count()
    calls_this_week = Note.query.filter(
        Note.call_date >= week_ago
    ).count()
    calls_this_month = Note.query.filter(
        Note.call_date >= month_ago
    ).count()
    
    # Customer engagement
    total_customers = Customer.query.count()
    customers_called_this_week = db.session.query(func.count(distinct(Note.customer_id))).filter(
        Note.call_date >= week_ago
    ).scalar()
    customers_called_this_month = db.session.query(func.count(distinct(Note.customer_id))).filter(
        Note.call_date >= month_ago
    ).scalar()
    
    # Topic insights - most discussed topics
    top_topics = db.session.query(
        Topic.id,
        Topic.name,
        func.count(Note.id).label('call_count')
    ).join(
        Note.topics
    ).filter(
        Note.call_date >= three_months_ago
    ).group_by(
        Topic.id,
        Topic.name
    ).order_by(
        func.count(Note.id).desc()
    ).limit(10).all()
    
    # Customers not called recently (90+ days or never)
    customers_with_recent_calls = db.session.query(Note.customer_id).filter(
        Note.call_date >= three_months_ago
    ).distinct().scalar_subquery()
    
    customers_needing_attention = Customer.query.filter(
        ~Customer.id.in_(customers_with_recent_calls)
    ).order_by(Customer.name).limit(10).all()
    
    # Seller activity (calls per seller this month)
    seller_activity = db.session.query(
        Seller.id,
        Seller.name,
        func.count(Note.id).label('call_count')
    ).join(
        Customer, Customer.seller_id == Seller.id
    ).join(
        Note, Note.customer_id == Customer.id
    ).filter(
        Note.call_date >= month_ago
    ).group_by(
        Seller.id,
        Seller.name
    ).order_by(
        func.count(Note.id).desc()
    ).limit(10).all()
    
    # Call frequency trend (last 30 days, grouped by week)
    weekly_calls = []
    for i in range(4):
        week_start = today - timedelta(days=7*(i+1))
        week_end = today - timedelta(days=7*i)
        count = Note.query.filter(
            Note.call_date >= week_start,
            Note.call_date < week_end
        ).count()
        weekly_calls.append({
            'week_label': f"{week_start.strftime('%b %d')} - {week_end.strftime('%b %d')}",
            'count': count
        })
    weekly_calls.reverse()  # Show oldest to newest
    
    return render_template('analytics.html',
                         total_calls=total_calls,
                         calls_this_week=calls_this_week,
                         calls_this_month=calls_this_month,
                         total_customers=total_customers,
                         customers_called_this_week=customers_called_this_week,
                         customers_called_this_month=customers_called_this_month,
                         top_topics=top_topics,
                         customers_needing_attention=customers_needing_attention,
                         seller_activity=seller_activity,
                         weekly_calls=weekly_calls)



# =============================================================================
# Preferences API Routes
# =============================================================================

@main_bp.route('/api/preferences/dark-mode', methods=['GET', 'POST'])
def dark_mode_preference():
    """Get or set dark mode preference."""
    if request.method == 'POST':
        data = request.get_json()
        dark_mode = data.get('dark_mode', False)
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(dark_mode=dark_mode)
            db.session.add(pref)
        else:
            pref.dark_mode = dark_mode
        
        db.session.commit()
        return jsonify({'dark_mode': pref.dark_mode}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(dark_mode=True)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'dark_mode': pref.dark_mode}), 200


@main_bp.route('/api/preferences/customer-view', methods=['GET', 'POST'])
def customer_view_preference():
    """Get or set customer view preference (alphabetical vs grouped)."""
    if request.method == 'POST':
        data = request.get_json()
        customer_view_grouped = data.get('customer_view_grouped', False)
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(customer_view_grouped=customer_view_grouped)
            db.session.add(pref)
        else:
            pref.customer_view_grouped = customer_view_grouped
        
        db.session.commit()
        return jsonify({'customer_view_grouped': pref.customer_view_grouped}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(customer_view_grouped=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'customer_view_grouped': pref.customer_view_grouped}), 200


@main_bp.route('/api/preferences/topic-sort', methods=['GET', 'POST'])
def topic_sort_preference():
    """Get or set topic sort preference (alphabetical vs by calls)."""
    if request.method == 'POST':
        data = request.get_json()
        topic_sort_by_calls = data.get('topic_sort_by_calls', False)
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(topic_sort_by_calls=topic_sort_by_calls)
            db.session.add(pref)
        else:
            pref.topic_sort_by_calls = topic_sort_by_calls
        
        db.session.commit()
        return jsonify({'topic_sort_by_calls': pref.topic_sort_by_calls}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(topic_sort_by_calls=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'topic_sort_by_calls': pref.topic_sort_by_calls}), 200


@main_bp.route('/api/preferences/territory-view', methods=['GET', 'POST'])
def territory_view_preference():
    """Get or set territory view preference (recent calls vs accounts)."""
    if request.method == 'POST':
        data = request.get_json()
        territory_view_accounts = data.get('territory_view_accounts', False)
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(territory_view_accounts=territory_view_accounts)
            db.session.add(pref)
        else:
            pref.territory_view_accounts = territory_view_accounts
        
        db.session.commit()
        return jsonify({'territory_view_accounts': pref.territory_view_accounts}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(territory_view_accounts=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'territory_view_accounts': pref.territory_view_accounts}), 200


@main_bp.route('/api/preferences/customer-sort-by', methods=['GET', 'POST'])
def customer_sort_by_preference():
    """Get or set customer sorting preference (alphabetical, grouped, or by_calls)."""
    if request.method == 'POST':
        data = request.get_json()
        customer_sort_by = data.get('customer_sort_by', 'alphabetical')
        
        # Validate the sort option
        valid_options = ['alphabetical', 'grouped', 'by_calls']
        if customer_sort_by not in valid_options:
            return jsonify({'error': 'Invalid sort option'}), 400
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(customer_sort_by=customer_sort_by)
            db.session.add(pref)
        else:
            pref.customer_sort_by = customer_sort_by
        
        db.session.commit()
        return jsonify({'customer_sort_by': pref.customer_sort_by}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(customer_sort_by='alphabetical')
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'customer_sort_by': pref.customer_sort_by}), 200


@main_bp.route('/api/preferences/show-customers-without-calls', methods=['GET', 'POST'])
def show_customers_without_calls_preference():
    """Get or set preference for showing customers without call logs."""
    if request.method == 'POST':
        data = request.get_json()
        show_customers_without_calls = data.get('show_customers_without_calls', False)
        
        # Get or create user preference
        pref = UserPreference.query.first()
        if not pref:
            pref = UserPreference(show_customers_without_calls=show_customers_without_calls)
            db.session.add(pref)
        else:
            pref.show_customers_without_calls = show_customers_without_calls
        
        db.session.commit()
        return jsonify({'show_customers_without_calls': pref.show_customers_without_calls}), 200
    
    # GET request
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(show_customers_without_calls=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'show_customers_without_calls': pref.show_customers_without_calls}), 200


@main_bp.route('/api/preferences/dismiss-welcome-modal', methods=['POST'])
def dismiss_welcome_modal():
    """Dismiss the first-run welcome modal."""
    # Get or create user preference
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(first_run_modal_dismissed=True)
        db.session.add(pref)
    else:
        pref.first_run_modal_dismissed = True
    
    db.session.commit()
    return jsonify({'first_run_modal_dismissed': True}), 200




@main_bp.route('/api/preferences/guided-tour-complete', methods=['POST'])
def guided_tour_complete():
    """Mark the guided product tour as completed."""
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference(guided_tour_completed=True)
        db.session.add(pref)
    else:
        pref.guided_tour_completed = True
    
    db.session.commit()
    return jsonify({'guided_tour_completed': True}), 200


@main_bp.route('/api/preferences/reset-onboarding', methods=['POST'])
def reset_onboarding():
    """Reset the onboarding wizard so it shows again on next page load."""
    pref = UserPreference.query.first()
    if pref:
        pref.first_run_modal_dismissed = False
        pref.user_role = None
        pref.my_seller_id = None
        pref.my_seller_alias = None
        session.pop('seller_mode_seller_id', None)
        db.session.commit()
    
    return jsonify({'first_run_modal_dismissed': False}), 200


@main_bp.route('/api/preferences/workiq-prompt', methods=['POST'])
def update_workiq_prompt():
    """Update the custom WorkIQ meeting summary prompt."""
    data = request.get_json()
    prompt_text = data.get('workiq_summary_prompt', '').strip()
    
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)
    
    # Empty string means "use default" — store as null
    pref.workiq_summary_prompt = prompt_text if prompt_text else None
    db.session.commit()
    
    return jsonify({'success': True, 'workiq_summary_prompt': pref.workiq_summary_prompt}), 200


@main_bp.route('/api/preferences/workiq-connect-impact', methods=['POST'])
def update_workiq_connect_impact():
    """Update the Connect impact extraction preference."""
    data = request.get_json()
    enabled = bool(data.get('workiq_connect_impact', True))
    
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)
    
    pref.workiq_connect_impact = enabled
    db.session.commit()
    
    return jsonify({'success': True, 'workiq_connect_impact': pref.workiq_connect_impact}), 200


# =============================================================================
# User Role & Seller Mode
# =============================================================================

@main_bp.route('/api/preferences/user-role', methods=['POST'])
def set_user_role():
    """Set the user role (DSS or SE). Can only be set once during onboarding."""
    data = request.get_json()
    role = data.get('role', '').strip().lower()
    
    if role not in ('se', 'dss'):
        return jsonify({'error': 'Invalid role. Must be "se" or "dss".'}), 400
    
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)
    
    # Role is permanent once set, unless onboarding is being re-run
    if pref.user_role is not None and pref.first_run_modal_dismissed:
        return jsonify({'error': 'Role already set. Reset onboarding to change.'}), 409
    
    pref.user_role = role
    db.session.commit()
    
    return jsonify({'user_role': pref.user_role}), 200


@main_bp.route('/api/preferences/save-alias', methods=['POST'])
def save_alias():
    """Save the DSS user's email alias during onboarding Step 2."""
    data = request.get_json()
    email = (data.get('email') or '').strip()
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email.'}), 400

    alias = email.split('@')[0].lower()

    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)

    pref.my_seller_alias = alias
    db.session.commit()

    return jsonify({'alias': alias}), 200


@main_bp.route('/api/preferences/my-seller', methods=['POST'])
def set_my_seller():
    """Manually set the DSS user's Seller record (fallback picker)."""
    data = request.get_json()
    seller_id = data.get('seller_id')
    if not seller_id:
        return jsonify({'error': 'seller_id is required.'}), 400

    seller = db.session.get(Seller, seller_id)
    if not seller:
        return jsonify({'error': 'Seller not found.'}), 404

    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)

    pref.my_seller_id = seller.id
    if seller.alias:
        pref.my_seller_alias = seller.alias
    db.session.commit()

    return jsonify({
        'my_seller_id': seller.id,
        'seller_name': seller.name,
    }), 200


@main_bp.route('/api/preferences/match-seller', methods=['POST'])
def match_seller_alias():
    """Auto-match the saved alias to an imported Seller record and set user_role."""
    pref = UserPreference.query.first()
    if not pref or not pref.my_seller_alias:
        # No alias saved - default to SE
        if pref and not pref.user_role:
            pref.user_role = 'se'
            db.session.commit()
        return jsonify({'matched': False, 'error': 'No alias saved.', 'role_set': 'se'}), 200

    alias = pref.my_seller_alias.lower()
    all_sellers = Seller.query.order_by(Seller.name).all()

    seller = next((s for s in all_sellers if s.alias and s.alias.lower() == alias), None)
    if seller:
        pref.my_seller_id = seller.id
        pref.user_role = 'dss'
        db.session.commit()
        return jsonify({
            'matched': True,
            'seller_id': seller.id,
            'seller_name': seller.name,
            'role_set': 'dss',
        }), 200

    # No match - set SE role
    pref.user_role = 'se'
    db.session.commit()
    return jsonify({
        'matched': False,
        'alias': alias,
        'role_set': 'se',
        'sellers': [{'id': s.id, 'name': s.name, 'alias': s.alias} for s in all_sellers],
    }), 200


@main_bp.route('/api/seller-mode/activate/<int:seller_id>', methods=['POST'])
def activate_seller_mode(seller_id: int):
    """Activate seller mode for a specific seller (SE only)."""
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return jsonify({'error': 'Seller not found.'}), 404
    
    session['seller_mode_seller_id'] = seller_id
    return jsonify({'seller_mode': True, 'seller_id': seller_id, 'seller_name': seller.name}), 200


@main_bp.route('/api/seller-mode/deactivate', methods=['POST'])
def deactivate_seller_mode():
    """Deactivate seller mode (return to normal SE view)."""
    session.pop('seller_mode_seller_id', None)
    return jsonify({'seller_mode': False}), 200


@main_bp.route('/api/admin/dev-toggle-role', methods=['POST'])
def dev_toggle_role():
    """Dev-only: Toggle between SE and DSS mode for testing."""
    if os.environ.get('FLASK_ENV') != 'development':
        return jsonify({'error': 'Dev-only endpoint.'}), 403
    
    pref = UserPreference.query.first()
    if not pref:
        pref = UserPreference()
        db.session.add(pref)
    
    if pref.user_role == 'dss':
        # Switch to SE
        pref.user_role = 'se'
        pref.my_seller_id = None
        pref.my_seller_alias = None
        session.pop('seller_mode_seller_id', None)
    else:
        # Switch to DSS with seller ID 7
        pref.user_role = 'dss'
        pref.my_seller_id = 7
    
    db.session.commit()
    
    return jsonify({'user_role': pref.user_role, 'my_seller_id': pref.my_seller_id}), 200


# =============================================================================
# Context Processor
# =============================================================================

@main_bp.app_context_processor
def inject_preferences():
    """Inject user preferences and pending link requests into all templates."""
    pref = UserPreference.query.first() if g.user.is_authenticated else None
    dark_mode = pref.dark_mode if pref else True
    customer_view_grouped = pref.customer_view_grouped if pref else False
    topic_sort_by_calls = pref.topic_sort_by_calls if pref else False
    first_run_modal_dismissed = pref.first_run_modal_dismissed if pref else False
    guided_tour_completed = pref.guided_tour_completed if pref else False
    accounts_synced = SyncStatus.is_complete('accounts')
    has_milestones = Milestone.query.first() is not None
    has_revenue = SyncStatus.is_complete('revenue_import')
    accounts_sync_state = SyncStatus.get_status('accounts')['state']
    milestones_sync_state = SyncStatus.get_status('milestones')['state']
    revenue_sync_state = SyncStatus.get_status('revenue_import')['state']
    
    # Check for available updates (lightweight -- reads cached state, no git calls)
    update_available = False
    try:
        from app.services.update_checker import get_update_state
        update_state = get_update_state()
        # If the background checker hasn't run yet, the cache is empty
        if update_state.get('last_checked') is None:
            from app.services.update_checker import check_for_updates
            update_state = check_for_updates()
        dismissed = pref.dismissed_update_commit if pref else None
        update_available = (
            update_state.get('available', False)
            and dismissed != update_state.get('remote_commit')
        )
    except Exception:
        pass
    
    # Get sellers for navbar dropdown
    nav_sellers = []
    if g.user.is_authenticated:
        nav_sellers = Seller.query.order_by(Seller.name).all()

    # FY transition banner state
    fy_transition_active = pref.fy_transition_active if pref else False
    fy_transition_label = pref.fy_transition_label if pref else None
    fy_sync_complete = pref.fy_sync_complete if pref else False

    # FY changeover reminder: show banner Jul 1 – Aug 31 if not yet completed
    fy_changeover_reminder = False
    now = datetime.today()
    if now.month in (7, 8) and not fy_transition_active:
        from app.services.fy_cutover import get_fiscal_year_labels
        next_fy = get_fiscal_year_labels()["next_fy"]
        fy_last_completed = pref.fy_last_completed if pref else None
        if fy_last_completed != next_fy:
            fy_changeover_reminder = next_fy

    # Seller mode resolution
    user_role_raw = pref.user_role if pref else None
    user_role = user_role_raw or 'se'
    user_role_set = user_role_raw is not None
    seller_mode = False
    seller_mode_seller = None
    if user_role == 'dss' and pref and pref.my_seller_id:
        seller_mode = True
        seller_mode_seller = pref.my_seller
    elif user_role == 'se' and session.get('seller_mode_seller_id'):
        seller = db.session.get(Seller, session['seller_mode_seller_id'])
        if seller:
            seller_mode = True
            seller_mode_seller = seller
        else:
            session.pop('seller_mode_seller_id', None)

    return dict(
        dark_mode=dark_mode, 
        customer_view_grouped=customer_view_grouped, 
        topic_sort_by_calls=topic_sort_by_calls,
        first_run_modal_dismissed=first_run_modal_dismissed,
        guided_tour_completed=guided_tour_completed,
        accounts_synced=accounts_synced,
        has_milestones=has_milestones,
        has_revenue=has_revenue,
        accounts_sync_state=accounts_sync_state,
        milestones_sync_state=milestones_sync_state,
        revenue_sync_state=revenue_sync_state,
        get_seller_color=get_seller_color,
        update_available=update_available,
        today=datetime.today(),
        nav_sellers=nav_sellers,
        fy_transition_active=fy_transition_active,
        fy_transition_label=fy_transition_label,
        fy_sync_complete=fy_sync_complete,
        fy_changeover_reminder=fy_changeover_reminder,
        user_role=user_role,
        user_role_set=user_role_set,
        seller_mode=seller_mode,
        seller_mode_seller=seller_mode_seller,
        is_debug=current_app.debug,
    )

