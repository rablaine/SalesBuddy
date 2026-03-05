"""
Main routes for NoteHelper.
Handles index, search, preferences, and API endpoints.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, g
from datetime import datetime, timezone, date
from sqlalchemy import func, extract
import calendar as cal
import json

from app.models import (db, CallLog, Customer, Seller, Territory, Topic,
                        UserPreference, User, SyncStatus)

# Create blueprint
main_bp = Blueprint('main', __name__)


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

def get_seller_color(seller_id: int, use_colors: bool = True) -> str:
    """
    Generate a consistent, visually distinct color for a seller based on their ID.
    Returns a CSS color class name. If use_colors is False, returns 'bg-secondary'.
    """
    if not use_colors:
        return 'bg-secondary'
    
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
    """Home page showing recent activity and stats."""
    # Count queries are fast on these small tables
    stats = {
        'call_logs': CallLog.query.count(),
        'customers': Customer.query.count(),
        'sellers': Seller.query.count(),
        'topics': Topic.query.count()
    }
    has_milestones = SyncStatus.is_complete('milestones')
    return render_template('index.html', stats=stats, has_milestones=has_milestones)


@main_bp.route('/api/call-logs/calendar')
def call_logs_calendar_api():
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
    call_logs = CallLog.query.options(
        db.joinedload(CallLog.customer),
        db.joinedload(CallLog.milestones)
    ).filter(
        CallLog.call_date >= first_day,
        CallLog.call_date <= last_day
    ).order_by(CallLog.call_date).all()
    
    # Group by day (call_logs already sorted by call_date from query)
    days = {}
    for log in call_logs:
        day = log.call_date.day
        if day not in days:
            days[day] = []
        days[day].append({
            'id': log.id,
            'customer_name': log.customer.name if log.customer else 'Unknown',
            'customer_id': log.customer.id if log.customer else None,
            'has_milestone': len(log.milestones) > 0,
            'has_task': log.msx_tasks.count() > 0,
            'has_hok': any(t.is_hok for t in log.msx_tasks.all()),
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


@main_bp.route('/search')
def search():
    """Search and filter call logs (FR011)."""
    # Get filter parameters
    search_text = request.args.get('q', '').strip()
    customer_id = request.args.get('customer_id', type=int)
    seller_id = request.args.get('seller_id', type=int)
    territory_id = request.args.get('territory_id', type=int)
    topic_ids = request.args.getlist('topic_ids', type=int)
    
    # Check if any search criteria provided
    has_search = bool(search_text or customer_id or seller_id or territory_id or topic_ids)
    
    call_logs = []
    grouped_data = {}
    
    # Only perform search if criteria provided
    if has_search:
        # Start with base query filtered by user
        query = CallLog.query
        
        # Apply filters
        if search_text:
            query = query.filter(CallLog.content.ilike(f'%{search_text}%'))
        
        if customer_id:
            query = query.filter(CallLog.customer_id == customer_id)
        
        if seller_id:
            query = query.join(Customer).filter(Customer.seller_id == seller_id)
        
        if territory_id:
            if not seller_id:  # Avoid duplicate join
                query = query.join(Customer)
            query = query.filter(Customer.territory_id == territory_id)
        
        if topic_ids:
            # Filter by topics (call logs that have ANY of the selected topics)
            query = query.join(CallLog.topics).filter(Topic.id.in_(topic_ids))
        
        # Get filtered call logs
        call_logs = query.order_by(CallLog.call_date.desc()).all()
        
        # Group call logs by Seller → Customer structure (FR011)
        # Structure: { seller_id: { 'seller': Seller, 'customers': { customer_id: { 'customer': Customer, 'calls': [CallLog] } } } }
        for call in call_logs:
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
                         call_logs=call_logs,
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
    """User preferences page."""
    from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
    
    user_id = g.user.id if g.user.is_authenticated else 1
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id)
        db.session.add(pref)
        db.session.commit()
    
    # Get user statistics
    stats = {
        'call_logs': CallLog.query.count(),
        'customers': Customer.query.count(),
        'topics': Topic.query.count()
    }
    
    return render_template('preferences.html', 
                         dark_mode=pref.dark_mode,
                         customer_view_grouped=pref.customer_view_grouped,
                         customer_sort_by=pref.customer_sort_by,
                         topic_sort_by_calls=pref.topic_sort_by_calls,
                         territory_view_accounts=pref.territory_view_accounts,
                         colored_sellers=pref.colored_sellers,
                         show_customers_without_calls=pref.show_customers_without_calls,
                         workiq_summary_prompt=pref.workiq_summary_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT,
                         workiq_connect_impact=pref.workiq_connect_impact,
                         stats=stats)


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
    total_calls = CallLog.query.count()
    calls_this_week = CallLog.query.filter(
        CallLog.call_date >= week_ago
    ).count()
    calls_this_month = CallLog.query.filter(
        CallLog.call_date >= month_ago
    ).count()
    
    # Customer engagement
    total_customers = Customer.query.count()
    customers_called_this_week = db.session.query(func.count(distinct(CallLog.customer_id))).filter(
        CallLog.call_date >= week_ago
    ).scalar()
    customers_called_this_month = db.session.query(func.count(distinct(CallLog.customer_id))).filter(
        CallLog.call_date >= month_ago
    ).scalar()
    
    # Topic insights - most discussed topics
    top_topics = db.session.query(
        Topic.id,
        Topic.name,
        func.count(CallLog.id).label('call_count')
    ).join(
        CallLog.topics
    ).filter(
        CallLog.call_date >= three_months_ago
    ).group_by(
        Topic.id,
        Topic.name
    ).order_by(
        func.count(CallLog.id).desc()
    ).limit(10).all()
    
    # Customers not called recently (90+ days or never)
    customers_with_recent_calls = db.session.query(CallLog.customer_id).filter(
        CallLog.call_date >= three_months_ago
    ).distinct().scalar_subquery()
    
    customers_needing_attention = Customer.query.filter(
        ~Customer.id.in_(customers_with_recent_calls)
    ).order_by(Customer.name).limit(10).all()
    
    # Seller activity (calls per seller this month)
    seller_activity = db.session.query(
        Seller.id,
        Seller.name,
        func.count(CallLog.id).label('call_count')
    ).join(
        Customer, Customer.seller_id == Seller.id
    ).join(
        CallLog, CallLog.customer_id == Customer.id
    ).filter(
        CallLog.call_date >= month_ago
    ).group_by(
        Seller.id,
        Seller.name
    ).order_by(
        func.count(CallLog.id).desc()
    ).limit(10).all()
    
    # Call frequency trend (last 30 days, grouped by week)
    weekly_calls = []
    for i in range(4):
        week_start = today - timedelta(days=7*(i+1))
        week_end = today - timedelta(days=7*i)
        count = CallLog.query.filter(
            CallLog.call_date >= week_start,
            CallLog.call_date < week_end
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
    # Get user ID (handle testing mode where login is disabled)
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        dark_mode = data.get('dark_mode', False)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, dark_mode=dark_mode)
            db.session.add(pref)
        else:
            pref.dark_mode = dark_mode
        
        db.session.commit()
        return jsonify({'dark_mode': pref.dark_mode}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, dark_mode=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'dark_mode': pref.dark_mode}), 200


@main_bp.route('/api/preferences/customer-view', methods=['GET', 'POST'])
def customer_view_preference():
    """Get or set customer view preference (alphabetical vs grouped)."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        customer_view_grouped = data.get('customer_view_grouped', False)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, customer_view_grouped=customer_view_grouped)
            db.session.add(pref)
        else:
            pref.customer_view_grouped = customer_view_grouped
        
        db.session.commit()
        return jsonify({'customer_view_grouped': pref.customer_view_grouped}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, customer_view_grouped=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'customer_view_grouped': pref.customer_view_grouped}), 200


@main_bp.route('/api/preferences/topic-sort', methods=['GET', 'POST'])
def topic_sort_preference():
    """Get or set topic sort preference (alphabetical vs by calls)."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        topic_sort_by_calls = data.get('topic_sort_by_calls', False)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, topic_sort_by_calls=topic_sort_by_calls)
            db.session.add(pref)
        else:
            pref.topic_sort_by_calls = topic_sort_by_calls
        
        db.session.commit()
        return jsonify({'topic_sort_by_calls': pref.topic_sort_by_calls}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, topic_sort_by_calls=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'topic_sort_by_calls': pref.topic_sort_by_calls}), 200


@main_bp.route('/api/preferences/territory-view', methods=['GET', 'POST'])
def territory_view_preference():
    """Get or set territory view preference (recent calls vs accounts)."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        territory_view_accounts = data.get('territory_view_accounts', False)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, territory_view_accounts=territory_view_accounts)
            db.session.add(pref)
        else:
            pref.territory_view_accounts = territory_view_accounts
        
        db.session.commit()
        return jsonify({'territory_view_accounts': pref.territory_view_accounts}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, territory_view_accounts=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'territory_view_accounts': pref.territory_view_accounts}), 200


@main_bp.route('/api/preferences/colored-sellers', methods=['GET', 'POST'])
def colored_sellers_preference():
    """Get or set colored sellers preference (grey vs colored badges)."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        colored_sellers = data.get('colored_sellers', True)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, colored_sellers=colored_sellers)
            db.session.add(pref)
        else:
            pref.colored_sellers = colored_sellers
        
        db.session.commit()
        return jsonify({'colored_sellers': pref.colored_sellers}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, colored_sellers=True)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'colored_sellers': pref.colored_sellers}), 200


@main_bp.route('/api/preferences/customer-sort-by', methods=['GET', 'POST'])
def customer_sort_by_preference():
    """Get or set customer sorting preference (alphabetical, grouped, or by_calls)."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        customer_sort_by = data.get('customer_sort_by', 'alphabetical')
        
        # Validate the sort option
        valid_options = ['alphabetical', 'grouped', 'by_calls']
        if customer_sort_by not in valid_options:
            return jsonify({'error': 'Invalid sort option'}), 400
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, customer_sort_by=customer_sort_by)
            db.session.add(pref)
        else:
            pref.customer_sort_by = customer_sort_by
        
        db.session.commit()
        return jsonify({'customer_sort_by': pref.customer_sort_by}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, customer_sort_by='alphabetical')
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'customer_sort_by': pref.customer_sort_by}), 200


@main_bp.route('/api/preferences/show-customers-without-calls', methods=['GET', 'POST'])
def show_customers_without_calls_preference():
    """Get or set preference for showing customers without call logs."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    if request.method == 'POST':
        data = request.get_json()
        show_customers_without_calls = data.get('show_customers_without_calls', False)
        
        # Get or create user preference
        pref = UserPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = UserPreference(user_id=user_id, show_customers_without_calls=show_customers_without_calls)
            db.session.add(pref)
        else:
            pref.show_customers_without_calls = show_customers_without_calls
        
        db.session.commit()
        return jsonify({'show_customers_without_calls': pref.show_customers_without_calls}), 200
    
    # GET request
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, show_customers_without_calls=False)
        db.session.add(pref)
        db.session.commit()
    
    return jsonify({'show_customers_without_calls': pref.show_customers_without_calls}), 200


@main_bp.route('/api/preferences/dismiss-welcome-modal', methods=['POST'])
def dismiss_welcome_modal():
    """Dismiss the first-run welcome modal."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    # Get or create user preference
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, first_run_modal_dismissed=True)
        db.session.add(pref)
    else:
        pref.first_run_modal_dismissed = True
    
    db.session.commit()
    return jsonify({'first_run_modal_dismissed': True}), 200




@main_bp.route('/api/preferences/guided-tour-complete', methods=['POST'])
def guided_tour_complete():
    """Mark the guided product tour as completed."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id, guided_tour_completed=True)
        db.session.add(pref)
    else:
        pref.guided_tour_completed = True
    
    db.session.commit()
    return jsonify({'guided_tour_completed': True}), 200


@main_bp.route('/api/preferences/reset-onboarding', methods=['POST'])
def reset_onboarding():
    """Reset the onboarding wizard so it shows again on next page load."""
    user_id = g.user.id if g.user.is_authenticated else 1
    
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if pref:
        pref.first_run_modal_dismissed = False
        db.session.commit()
    
    return jsonify({'first_run_modal_dismissed': False}), 200


@main_bp.route('/api/preferences/workiq-prompt', methods=['POST'])
def update_workiq_prompt():
    """Update the custom WorkIQ meeting summary prompt."""
    user_id = g.user.id if g.user.is_authenticated else 1
    data = request.get_json()
    prompt_text = data.get('workiq_summary_prompt', '').strip()
    
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id)
        db.session.add(pref)
    
    # Empty string means "use default" — store as null
    pref.workiq_summary_prompt = prompt_text if prompt_text else None
    db.session.commit()
    
    return jsonify({'success': True, 'workiq_summary_prompt': pref.workiq_summary_prompt}), 200


@main_bp.route('/api/preferences/workiq-connect-impact', methods=['POST'])
def update_workiq_connect_impact():
    """Update the Connect impact extraction preference."""
    user_id = g.user.id if g.user.is_authenticated else 1
    data = request.get_json()
    enabled = bool(data.get('workiq_connect_impact', True))
    
    pref = UserPreference.query.filter_by(user_id=user_id).first()
    if not pref:
        pref = UserPreference(user_id=user_id)
        db.session.add(pref)
    
    pref.workiq_connect_impact = enabled
    db.session.commit()
    
    return jsonify({'success': True, 'workiq_connect_impact': pref.workiq_connect_impact}), 200


# =============================================================================
# Context Processor
# =============================================================================

@main_bp.app_context_processor
def inject_preferences():
    """Inject user preferences and pending link requests into all templates."""
    pref = UserPreference.query.first() if g.user.is_authenticated else None
    dark_mode = pref.dark_mode if pref else False
    customer_view_grouped = pref.customer_view_grouped if pref else False
    topic_sort_by_calls = pref.topic_sort_by_calls if pref else False
    colored_sellers = pref.colored_sellers if pref else True
    first_run_modal_dismissed = pref.first_run_modal_dismissed if pref else False
    guided_tour_completed = pref.guided_tour_completed if pref else False
    has_customers = Customer.query.first() is not None
    has_milestones = SyncStatus.is_complete('milestones')
    has_revenue = SyncStatus.is_complete('revenue_import')
    milestones_sync_state = SyncStatus.get_status('milestones')['state']
    revenue_sync_state = SyncStatus.get_status('revenue_import')['state']
    
    # Create a wrapper function that always returns color classes (CSS will handle grey state)
    def get_seller_color_with_pref(seller_id: int) -> str:
        return get_seller_color(seller_id, use_colors=True)
    
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
    
    return dict(
        dark_mode=dark_mode, 
        customer_view_grouped=customer_view_grouped, 
        topic_sort_by_calls=topic_sort_by_calls,
        colored_sellers=colored_sellers,
        first_run_modal_dismissed=first_run_modal_dismissed,
        guided_tour_completed=guided_tour_completed,
        has_customers=has_customers,
        has_milestones=has_milestones,
        has_revenue=has_revenue,
        milestones_sync_state=milestones_sync_state,
        revenue_sync_state=revenue_sync_state,
        get_seller_color=get_seller_color_with_pref,
        update_available=update_available,
    )

