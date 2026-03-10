"""
Call log routes for NoteHelper.
Handles call log listing, creation, viewing, editing, and Fill My Day bulk import.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify
from datetime import datetime
import logging

from app.models import db, Note, Customer, Seller, Territory, Topic, Partner, Milestone, MsxTask, UserPreference, NoteTemplate
from app.services.msx_api import TASK_CATEGORIES
from app.services.backup import backup_customer as _backup_customer

logger = logging.getLogger(__name__)

# Create blueprint
notes_bp = Blueprint('notes', __name__)


def _handle_milestone_and_task(note):
    """
    Handle MSX milestone selection and optional task creation.
    
    Reads form data for milestone info and creates/links the milestone.
    If task fields are provided, creates the task in MSX and stores it locally.
    
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    # Get MSX milestone data from form
    milestone_msx_id = request.form.get('milestone_msx_id', '').strip()
    milestone_url = request.form.get('milestone_url', '').strip()
    
    if not milestone_msx_id:
        # No milestone selected - clear any existing
        note.milestones = []
        return True, None
    
    # Get additional milestone metadata
    milestone_name = request.form.get('milestone_name', '').strip()
    milestone_number = request.form.get('milestone_number', '').strip()
    milestone_status = request.form.get('milestone_status', '').strip()
    milestone_status_code = request.form.get('milestone_status_code', '').strip()
    milestone_opp_name = request.form.get('milestone_opportunity_name', '').strip()
    milestone_workload = request.form.get('milestone_workload', '').strip()
    milestone_monthly_usage_str = request.form.get('milestone_monthly_usage', '').strip()
    milestone_monthly_usage = float(milestone_monthly_usage_str) if milestone_monthly_usage_str else None
    
    # Get customer for milestone association
    customer_id = request.form.get('customer_id')
    
    # Find or create milestone by MSX ID
    milestone = Milestone.query.filter_by(msx_milestone_id=milestone_msx_id).first()
    if not milestone:
        # Create new milestone
        milestone = Milestone(
            msx_milestone_id=milestone_msx_id,
            url=milestone_url,
            milestone_number=milestone_number,
            title=milestone_name,
            msx_status=milestone_status,
            msx_status_code=int(milestone_status_code) if milestone_status_code else None,
            opportunity_name=milestone_opp_name,
            workload=milestone_workload or None,
            monthly_usage=milestone_monthly_usage,
            customer_id=int(customer_id) if customer_id else None
        )
        db.session.add(milestone)
    else:
        # Update existing milestone with latest data
        if milestone_name:
            milestone.title = milestone_name
        if milestone_url:
            milestone.url = milestone_url
        if milestone_status:
            milestone.msx_status = milestone_status
        if milestone_status_code:
            milestone.msx_status_code = int(milestone_status_code)
        if milestone_opp_name:
            milestone.opportunity_name = milestone_opp_name
        if milestone_workload:
            milestone.workload = milestone_workload
        if milestone_monthly_usage is not None:
            milestone.monthly_usage = milestone_monthly_usage
    
    # Associate milestone with call log
    note.milestones = [milestone]
    
    # Check if a task was already created (via the "Create Task in MSX" button)
    created_task_id = request.form.get('created_task_id', '').strip()
    
    if created_task_id:
        # Task was pre-created - just store the local record
        task_subject = request.form.get('task_subject', '').strip()
        task_category = request.form.get('task_category', '').strip()
        task_duration = request.form.get('task_duration', '60')
        task_description = request.form.get('task_description', '').strip()
        task_due_date_str = request.form.get('task_due_date', '').strip()
        created_task_url = request.form.get('created_task_url', '').strip()
        created_task_category_name = request.form.get('created_task_category_name', '').strip()
        created_task_is_hok = request.form.get('created_task_is_hok', '').strip() == '1'
        
        try:
            duration_minutes = int(task_duration)
        except (ValueError, TypeError):
            duration_minutes = 60
        
        try:
            task_category_int = int(task_category) if task_category else 0
        except (ValueError, TypeError):
            task_category_int = 0
        
        logger.info(f"Linking pre-created MSX task {created_task_id} to call log")
        
        # Parse due date
        task_due_date = None
        if task_due_date_str:
            try:
                task_due_date = datetime.strptime(task_due_date_str, '%Y-%m-%d')
            except ValueError:
                pass
        
        msx_task = MsxTask(
            msx_task_id=created_task_id,
            msx_task_url=created_task_url,
            subject=task_subject,
            description=task_description if task_description else None,
            task_category=task_category_int,
            task_category_name=created_task_category_name or 'Unknown',
            duration_minutes=duration_minutes,
            is_hok=created_task_is_hok,
            due_date=task_due_date,
            note=note,
            milestone=milestone
        )
        db.session.add(msx_task)
        logger.info(f"Pre-created MSX task linked successfully: {created_task_id}")
    
    return True, None


@notes_bp.route('/notes')
def notes_list():
    """List all call logs (FR010)."""
    filter_type = request.args.get('filter', '')
    
    query = Note.query.options(
        db.joinedload(Note.customer).joinedload(Customer.seller),
        db.joinedload(Note.customer).joinedload(Customer.territory),
        db.joinedload(Note.topics),
        db.joinedload(Note.partners)
    )
    
    if filter_type == 'customer':
        query = query.filter(Note.customer_id.isnot(None))
    elif filter_type == 'general':
        query = query.filter(Note.customer_id.is_(None))
    
    notes = query.order_by(Note.call_date.desc()).all()
    return render_template('notes_list.html', notes=notes, filter_type=filter_type)


@notes_bp.route('/note/new', methods=['GET', 'POST'])
def note_create():
    """Create a new call log (FR005)."""
    if request.method == 'POST':
        customer_id = request.form.get('customer_id')
        seller_id = request.form.get('seller_id')
        call_date_str = request.form.get('call_date')
        content = request.form.get('content', '').strip()
        topic_ids = request.form.getlist('topic_ids')
        partner_ids = request.form.getlist('partner_ids')
        engagement_ids = request.form.getlist('engagement_ids')
        referrer = request.form.get('referrer', '')
        
        # Validation -- customer is optional for general notes
        if not call_date_str:
            flash('Call date is required.', 'danger')
            return redirect(url_for('notes.note_create'))
        
        if not content:
            flash('Note content is required.', 'danger')
            return redirect(url_for('notes.note_create'))
        
        # Parse call date and time
        call_time_str = request.form.get('call_time', '')
        try:
            if call_time_str:
                call_date = datetime.strptime(f'{call_date_str} {call_time_str}', '%Y-%m-%d %H:%M')
            else:
                call_date = datetime.strptime(call_date_str, '%Y-%m-%d')
        except ValueError:
            flash('Invalid date/time format.', 'danger')
            return redirect(url_for('notes.note_create'))
        
        # Get customer and auto-fill territory (customer is optional)
        customer = None
        territory_id = None
        if customer_id:
            customer = Customer.query.filter_by(id=int(customer_id)).first()
            territory_id = customer.territory_id if customer else None
            
            # If customer doesn't have a seller but one is selected, associate it
            if customer and not customer.seller_id and seller_id:
                customer.seller_id = int(seller_id)
                # Also update customer's territory if seller has one
                seller = Seller.query.filter_by(id=int(seller_id)).first()
                if seller and seller.territory_id:
                    customer.territory_id = seller.territory_id
                    territory_id = seller.territory_id
        
        # Create call log
        note = Note(
            customer_id=int(customer_id) if customer_id else None,
            call_date=call_date,
            content=content)
        
        # Add topics
        if topic_ids:
            topics = Topic.query.filter(Topic.id.in_([int(tid) for tid in topic_ids])).all()
            note.topics.extend(topics)
        
        # Add partners
        if partner_ids:
            partners = Partner.query.filter(Partner.id.in_([int(pid) for pid in partner_ids])).all()
            note.partners.extend(partners)
        
        # Add engagements
        if engagement_ids:
            from app.models import Engagement
            engagements = Engagement.query.filter(
                Engagement.id.in_([int(eid) for eid in engagement_ids])
            ).all()
            note.engagements.extend(engagements)
        
        db.session.add(note)
        
        # Handle milestone and optional task creation (only for customer-linked notes)
        if customer_id:
            try:
                _handle_milestone_and_task(note)
            except Exception as e:
                logger.exception("Error handling milestone/task during call log create")
                flash(f'Note will be saved, but milestone/task failed: {e}', 'warning')
        
        db.session.commit()

        # Back up this customer's call logs
        if note.customer_id:
            try:
                _backup_customer(note.customer_id)
            except Exception:
                logger.debug("Backup skipped", exc_info=True)
        
        flash('Note created successfully!', 'success')
        
        # Redirect back to referrer if provided
        if referrer:
            return redirect(referrer)
        
        return redirect(url_for('notes.note_view', id=note.id))
    
    # GET request - load form
    # customer_id is optional: if not provided, it's a general (non-customer) note
    preselect_customer_id = request.args.get('customer_id', type=int)
    
    preselect_customer = None
    previous_calls = []
    
    if preselect_customer_id:
        # Load customer and their previous call logs
        preselect_customer = Customer.query.filter_by(id=preselect_customer_id).first_or_404()
        previous_calls = Note.query.filter_by(customer_id=preselect_customer_id).options(
            db.joinedload(Note.topics)
        ).order_by(Note.call_date.desc()).all()
    
    customers = Customer.query.order_by(Customer.name).all()
    sellers = Seller.query.order_by(Seller.name).all()
    topics = Topic.query.order_by(Topic.name).all()
    partners = Partner.query.order_by(Partner.name).all()
    
    # Pre-select topic from query params
    preselect_topic_id = request.args.get('topic_id', type=int)
    
    # Pre-select engagement from query params
    preselect_engagement_id = request.args.get('engagement_id', type=int)
    
    # Capture referrer for redirect after creation
    referrer = request.referrer or ''
    
    # Pass date and time (from query param or now)
    from datetime import date
    date_param = request.args.get('date', '')
    if date_param:
        # Validate date format
        try:
            datetime.strptime(date_param, '%Y-%m-%d')
            today = date_param
        except ValueError:
            today = date.today().strftime('%Y-%m-%d')
    else:
        today = date.today().strftime('%Y-%m-%d')
    
    # Current time for new call logs (default to now)
    now_time = datetime.now().strftime('%H:%M')
    
    # Get user's custom WorkIQ prompt (for meeting import modal)
    from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
    pref = UserPreference.query.first()
    user_prompt = pref.workiq_summary_prompt if pref and pref.workiq_summary_prompt else DEFAULT_SUMMARY_PROMPT
    connect_impact_enabled = pref.workiq_connect_impact if pref else True
    
    # Get note templates for the template selector dropdown
    templates = NoteTemplate.query.order_by(NoteTemplate.name).all()
    
    # Determine the default template content for new notes
    default_template_content = None
    if pref:
        if preselect_customer_id and pref.default_template_customer_id:
            dt = db.session.get(NoteTemplate, pref.default_template_customer_id)
            if dt:
                default_template_content = dt.content
        elif not preselect_customer_id and pref.default_template_noncustomer_id:
            dt = db.session.get(NoteTemplate, pref.default_template_noncustomer_id)
            if dt:
                default_template_content = dt.content
    
    return render_template('note_form.html', 
                         note=None, 
                         customers=customers,
                         sellers=sellers,
                         topics=topics,
                         partners=partners,
                         templates=templates,
                         default_template_content=default_template_content,
                         preselect_customer_id=preselect_customer_id,
                         preselect_customer=preselect_customer,
                         preselect_topic_id=preselect_topic_id,
                         preselect_engagement_id=preselect_engagement_id,
                         previous_calls=previous_calls,
                         referrer=referrer,
                         today=today,
                         now_time=now_time,
                         workiq_prompt=user_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT,
                         workiq_connect_impact=connect_impact_enabled,
                         next_url='')


@notes_bp.route('/note/<int:id>')
def note_view(id):
    """View call log details (FR010)."""
    note = Note.query.filter_by(id=id).first_or_404()

    # Capture where the user came from so Edit → Save/Cancel can return there.
    # Ignore self-referrals (the note view or edit page itself).
    back_url = request.args.get('next') or ''
    if not back_url and request.referrer:
        from urllib.parse import urlparse
        ref_path = urlparse(request.referrer).path
        if not ref_path.startswith(f'/note/{id}'):
            back_url = ref_path

    return render_template('note_view.html', note=note, back_url=back_url)


@notes_bp.route('/note/<int:id>/edit', methods=['GET', 'POST'])
def note_edit(id):
    """Edit call log (FR010)."""
    note = Note.query.filter_by(id=id).first_or_404()
    
    if request.method == 'POST':
        customer_id = request.form.get('customer_id')
        seller_id = request.form.get('seller_id')
        call_date_str = request.form.get('call_date')
        content = request.form.get('content', '').strip()
        topic_ids = request.form.getlist('topic_ids')
        partner_ids = request.form.getlist('partner_ids')
        engagement_ids = request.form.getlist('engagement_ids')
        
        # Validation -- customer is optional for general notes
        if not call_date_str:
            flash('Call date is required.', 'danger')
            return redirect(url_for('notes.note_edit', id=id))
        
        if not content:
            flash('Note content is required.', 'danger')
            return redirect(url_for('notes.note_edit', id=id))
        
        # Parse call date and time
        call_time_str = request.form.get('call_time', '')
        try:
            if call_time_str:
                call_date = datetime.strptime(f'{call_date_str} {call_time_str}', '%Y-%m-%d %H:%M')
            else:
                call_date = datetime.strptime(call_date_str, '%Y-%m-%d')
        except ValueError:
            flash('Invalid date/time format.', 'danger')
            return redirect(url_for('notes.note_edit', id=id))
        
        # Update call log
        note.customer_id = int(customer_id) if customer_id else None
        # Seller and territory are now derived from customer
        note.call_date = call_date
        note.content = content
        
        # Update topics - remove all existing associations first
        note.topics = []
        if topic_ids:
            topics = Topic.query.filter(Topic.id.in_([int(tid) for tid in topic_ids])).all()
            note.topics = topics
        
        # Update partners - remove all existing associations first
        note.partners = []
        if partner_ids:
            partners = Partner.query.filter(Partner.id.in_([int(pid) for pid in partner_ids])).all()
            note.partners = partners
        
        # Update engagements
        note.engagements = []
        if engagement_ids:
            from app.models import Engagement
            engagements = Engagement.query.filter(
                Engagement.id.in_([int(eid) for eid in engagement_ids])
            ).all()
            note.engagements = engagements
        
        # Handle milestone and optional task creation (only for customer-linked notes)
        if customer_id:
            try:
                _handle_milestone_and_task(note)
            except Exception as e:
                logger.exception("Error handling milestone/task during call log edit")
                flash(f'Note will be saved, but milestone/task failed: {e}', 'warning')
        
        db.session.commit()

        # Back up this customer's call logs
        if note.customer_id:
            try:
                _backup_customer(note.customer_id)
            except Exception:
                logger.debug("Backup skipped", exc_info=True)
        
        flash('Note updated successfully!', 'success')

        # Redirect back to where the user originally came from (e.g. customer page)
        next_url = request.form.get('next', '')
        if next_url:
            return redirect(next_url)
        return redirect(url_for('notes.note_view', id=note.id))
    
    # GET request - load form
    next_url = request.args.get('next', '')
    customers = Customer.query.order_by(Customer.name).all()
    sellers = Seller.query.order_by(Seller.name).all()
    topics = Topic.query.order_by(Topic.name).all()
    partners = Partner.query.order_by(Partner.name).all()
    
    # Get note templates for the template selector dropdown
    templates = NoteTemplate.query.order_by(NoteTemplate.name).all()
    
    # Get user's custom WorkIQ prompt (for meeting import modal)
    from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
    pref = UserPreference.query.first()
    user_prompt = pref.workiq_summary_prompt if pref and pref.workiq_summary_prompt else DEFAULT_SUMMARY_PROMPT
    connect_impact_enabled = pref.workiq_connect_impact if pref else True
    
    return render_template('note_form.html',
                         note=note,
                         customers=customers,
                         sellers=sellers,
                         topics=topics,
                         partners=partners,
                         templates=templates,
                         preselect_customer_id=None,
                         preselect_topic_id=None,
                         workiq_prompt=user_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT,
                         workiq_connect_impact=connect_impact_enabled,
                         next_url=next_url)


@notes_bp.route('/note/<int:id>/delete', methods=['POST'])
def note_delete(id):
    """Delete a call log."""
    note = db.session.get(Note, id)
    
    if not note:
        flash('Note not found.', 'danger')
        return redirect(url_for('notes.notes_list'))
    
    # Store customer for redirect
    customer_id = note.customer_id
    
    # Delete the call log
    db.session.delete(note)
    db.session.commit()

    # Back up this customer's call logs (reflects the deletion)
    try:
        _backup_customer(customer_id)
    except Exception:
        logger.debug("Backup skipped", exc_info=True)
    
    flash('Note deleted successfully.', 'success')
    
    # Redirect to customer view if we have a customer, otherwise call logs list
    if customer_id:
        return redirect(url_for('customers.customer_view', id=customer_id))
    else:
        return redirect(url_for('notes.notes_list'))


# =============================================================================
# Meeting Import API (WorkIQ Integration)
# =============================================================================

@notes_bp.route('/api/meetings')
def api_get_meetings():
    """
    Get meetings for a specific date with optional fuzzy matching.
    
    Query params:
        date: Date in YYYY-MM-DD format (required)
        customer_name: Customer name to fuzzy match against (optional)
        
    Returns JSON:
        - meetings: List of meeting objects
        - auto_selected_index: Index of fuzzy-matched meeting (or null)
        - auto_selected_reason: Explanation of why meeting was selected
    """
    from flask import jsonify
    from app.services.workiq_service import get_meetings_for_date, find_best_customer_match
    
    date_str = request.args.get('date')
    customer_name = request.args.get('customer_name', '')
    
    if not date_str:
        return jsonify({'error': 'date parameter is required'}), 400
    
    # Validate date format
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    # Get meetings from WorkIQ
    try:
        meetings, raw_response = get_meetings_for_date(date_str)
    except Exception as e:
        logger.error(f"WorkIQ error: {e}")
        return jsonify({'error': f'Failed to fetch meetings: {str(e)}'}), 500
    
    # Format for API response
    formatted_meetings = []
    for m in meetings:
        formatted_meetings.append({
            'id': m.get('id', ''),
            'title': m.get('title', ''),
            'start_time': m['start_time'].isoformat() if m.get('start_time') else None,
            'start_time_display': m['start_time'].strftime('%I:%M %p') if m.get('start_time') else m.get('start_time_str', ''),
            'customer': m.get('customer', ''),
            'attendees': m.get('attendees', [])
        })
    
    # Find best match if customer name provided
    auto_selected = None
    auto_selected_reason = None
    
    if customer_name and meetings:
        match_idx = find_best_customer_match(meetings, customer_name)
        if match_idx is not None:
            auto_selected = match_idx
            matched = meetings[match_idx]
            auto_selected_reason = f"Auto-selected: '{matched.get('customer') or matched.get('title')}' matches '{customer_name}'"
    
    return jsonify({
        'meetings': formatted_meetings,
        'auto_selected_index': auto_selected,
        'auto_selected_reason': auto_selected_reason,
        'date': date_str,
        'customer_name': customer_name,
        'debug_raw_response': raw_response if not formatted_meetings else None,
    })


@notes_bp.route('/api/meetings/summary')
def api_get_meeting_summary():
    """
    Get a 250-word summary for a specific meeting.
    
    Query params:
        title: Meeting title (required)
        date: Date in YYYY-MM-DD format (optional, helps narrow down)
        prompt: Custom prompt template (optional, uses {title} and {date} placeholders)
        
    Returns JSON:
        - summary: The 250-word meeting summary
        - topics: List of technologies/topics discussed
        - action_items: List of follow-up items
    """
    from flask import jsonify
    from app.services.workiq_service import get_meeting_summary
    
    title = request.args.get('title')
    date_str = request.args.get('date')
    custom_prompt = request.args.get('prompt')
    extract_impact = request.args.get('extract_impact', '').lower() in ('true', '1', 'yes')
    
    if not title:
        return jsonify({'error': 'title parameter is required'}), 400
    
    try:
        result = get_meeting_summary(title, date_str, custom_prompt=custom_prompt,
                                     extract_impact=extract_impact)
        return jsonify({
            'summary': result.get('summary', ''),
            'topics': result.get('topics', []),
            'action_items': result.get('action_items', []),
            'task_subject': result.get('task_subject', ''),
            'task_description': result.get('task_description', ''),
            'connect_impact': result.get('connect_impact', []),
            'engagement_signals': result.get('engagement_signals', {}),
            'retry_suggested': result.get('retry_suggested', False),
            'success': True
        })
    except Exception as e:
        logger.error(f"Failed to get meeting summary: {e}")
        return jsonify({
            'error': f'Failed to fetch summary: {str(e)}',
            'success': False
        }), 500


# =============================================================================
# Fill My Day (Bulk Meeting Import)
# =============================================================================

@notes_bp.route('/fill-my-day')
def fill_my_day():
    """Fill My Day page - bulk import meetings for a date into call logs."""
    from datetime import date as date_type
    date_param = request.args.get('date', '')
    
    # Validate date if provided
    if date_param:
        try:
            datetime.strptime(date_param, '%Y-%m-%d')
        except ValueError:
            date_param = ''
    
    return render_template('fill_my_day.html', prefill_date=date_param)


@notes_bp.route('/api/fill-my-day/process', methods=['POST'])
def api_fill_my_day_process():
    """
    Process a single meeting for Fill My Day.
    
    Fetches the summary from WorkIQ and runs AI analysis.
    Called per-meeting to show progress in the UI.
    
    Request JSON:
        - meeting: Meeting object {title, start_time, customer, ...}
        - date: Date string YYYY-MM-DD
        - customer_id: Matched customer ID
        
    Returns JSON:
        - summary: Meeting summary text
        - content_html: Formatted HTML for call notes
        - topics: List of {id, name} suggested topics
        - task_subject: Suggested task subject
        - task_description: Suggested task description
        - success: bool
    """
    from app.services.workiq_service import get_meeting_summary
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    meeting = data.get('meeting', {})
    date_str = data.get('date', '')
    title = meeting.get('title', '')
    
    if not title:
        return jsonify({'success': False, 'error': 'Meeting title is required'}), 400
    
    customer_id = data.get('customer_id')

    # Check user's Connect impact extraction preference
    from app.models import UserPreference
    pref = UserPreference.query.first()
    extract_impact = pref.workiq_connect_impact if pref else True

    result = {'success': True, 'summary': '', 'content_html': '', 'topics': [],
              'task_subject': '', 'task_description': '', 'connect_impact': [],
              'engagement_signals': {},
              'summary_ok': False, 'milestone': None}
    
    # Step 1: Get meeting summary (WorkIQ provides summary + task suggestion)
    try:
        summary_data = get_meeting_summary(title, date_str,
                                           extract_impact=extract_impact)
        summary = summary_data.get('summary', '')
        action_items = summary_data.get('action_items', [])
        
        # Build HTML content
        content_html = f'<h2>{title}</h2>'
        content_html += '<p><strong>Summary:</strong></p>'
        content_html += f'<p>{summary}</p>'
        if action_items:
            content_html += '<p><strong>Action Items:</strong></p><ul>'
            for item in action_items:
                content_html += f'<li>{item}</li>'
            content_html += '</ul>'
        
        # Add Connect impact signals if present
        impact_items = summary_data.get('connect_impact', [])
        if impact_items:
            content_html += '<hr><p><strong>Impact Signals:</strong></p><ul>'
            for item in impact_items:
                content_html += f'<li>{item}</li>'
            content_html += '</ul>'
        
        # Add engagement metadata signals if present (Ben's table fields)
        eng_signals = summary_data.get('engagement_signals', {})
        if eng_signals:
            content_html += '<hr><p><strong>Engagement Metadata:</strong></p><ul>'
            for field, value in eng_signals.items():
                content_html += f'<li><strong>{field}:</strong> {value}</li>'
            content_html += '</ul>'
        
        result['summary'] = summary
        result['content_html'] = content_html
        result['connect_impact'] = impact_items
        result['engagement_signals'] = eng_signals
        result['summary_ok'] = bool(summary and not summary.startswith('Error'))
        
        # Use WorkIQ task suggestion as default (OpenAI milestone match may override)
        result['task_subject'] = summary_data.get('task_subject', '')
        result['task_description'] = summary_data.get('task_description', '')
    except Exception as e:
        logger.error(f"Fill My Day - summary error for '{title}': {e}")
        result['summary'] = f'[Could not fetch summary: {str(e)}]'
        result['content_html'] = f'<h2>{title}</h2><p><em>Summary unavailable</em></p>'
    
    # Step 2: AI analysis (topics) - only if we got a real summary and AI is enabled
    if result['summary_ok']:
        try:
            from app.gateway_client import gateway_call, GatewayError

            # Analyze call for topics via gateway
            ai_result = gateway_call("/v1/analyze-call", {
                "call_notes": result['summary'][:3000],
            })
            topic_names = ai_result.get("topics", [])

            # Match topic names to IDs
            all_topics = Topic.query.order_by(Topic.name).all()
            matched_topics = []
            for topic_name in topic_names:
                for t in all_topics:
                    if t.name.lower() == topic_name.lower():
                        matched_topics.append({'id': t.id, 'name': t.name})
                        break

            result['topics'] = matched_topics
        except Exception as e:
            logger.warning(f"Fill My Day - AI analysis error for '{title}': {e}")
            # Non-fatal - continue without AI enrichment
    # Step 3: Milestone matching - fetch from MSX and AI-match
    if result['summary_ok'] and customer_id:
        try:
            from app.services.msx_api import extract_account_id_from_url, get_milestones_by_account
            from app.gateway_client import gateway_call, GatewayError

            customer = db.session.get(Customer, int(customer_id))
            if customer and customer.tpid_url:
                account_id = extract_account_id_from_url(customer.tpid_url)
                if account_id:
                    msx_result = get_milestones_by_account(account_id)
                    if msx_result.get('success') and msx_result.get('milestones'):
                        milestones = msx_result['milestones']

                        # AI match milestones via gateway
                        if len(milestones) > 0:
                            ms_result = gateway_call("/v1/match-milestone", {
                                "call_notes": result['summary'][:2000],
                                "milestones": [
                                    {
                                        "id": m['id'],
                                        "name": m['name'],
                                        "status": m['status'],
                                        "opportunity": m.get('opportunity_name', ''),
                                        "workload": m.get('workload', ''),
                                    }
                                    for m in milestones
                                ],
                            })
                            matched_id = ms_result.get('milestone_id')

                            if matched_id:
                                matched = next(
                                    (m for m in milestones if m['id'] == matched_id),
                                    None
                                )
                                if matched:
                                    result['milestone'] = {
                                        'msx_milestone_id': matched['id'],
                                        'name': matched['name'],
                                        'number': matched.get('number', ''),
                                        'status': matched['status'],
                                        'status_code': matched.get('status_code'),
                                        'opportunity_name': matched.get('opportunity_name', ''),
                                        'url': matched.get('url', ''),
                                        'workload': matched.get('workload', ''),
                                        'reason': ms_result.get('reason', '')
                                    }
        except Exception as e:
            logger.warning(f"Fill My Day - milestone matching error for '{title}': {e}")
            # Non-fatal - continue without milestone
    
    return jsonify(result)


@notes_bp.route('/api/fill-my-day/save', methods=['POST'])
def api_fill_my_day_save():
    """
    Save a single call log from Fill My Day.
    
    Request JSON:
        - customer_id: int
        - call_date: YYYY-MM-DD
        - call_time: HH:MM (optional)
        - content: HTML content
        - topic_ids: list of int
        - milestone: dict with MSX milestone data (optional)
        - task_subject: str (optional)
        - task_description: str (optional)
        
    Returns JSON:
        - success: bool
        - note_id: int
        - view_url: str
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    customer_id = data.get('customer_id')
    call_date_str = data.get('call_date', '')
    call_time_str = data.get('call_time', '')
    content = data.get('content', '').strip()
    topic_ids = data.get('topic_ids', [])
    milestone_data = data.get('milestone')
    task_subject = data.get('task_subject', '').strip()
    task_description = data.get('task_description', '').strip()
    created_task_id = data.get('created_task_id', '').strip()
    created_task_url = data.get('created_task_url', '').strip()
    created_task_category_name = data.get('created_task_category_name', '').strip()
    created_task_is_hok = data.get('created_task_is_hok', '').strip() == '1'
    task_due_date_str = data.get('task_due_date', '').strip()
    
    # Validation
    if not customer_id:
        return jsonify({'success': False, 'error': 'Customer is required'}), 400
    if not call_date_str:
        return jsonify({'success': False, 'error': 'Date is required'}), 400
    if not content:
        return jsonify({'success': False, 'error': 'Content is required'}), 400
    
    # Parse date/time
    try:
        if call_time_str:
            call_date = datetime.strptime(f'{call_date_str} {call_time_str}', '%Y-%m-%d %H:%M')
        else:
            call_date = datetime.strptime(call_date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date/time format'}), 400
    
    # Verify customer exists
    customer = db.session.get(Customer, int(customer_id))
    if not customer:
        return jsonify({'success': False, 'error': 'Customer not found'}), 404
    
    try:
        # Create call log
        note = Note(
            customer_id=int(customer_id),
            call_date=call_date,
            content=content
        )
        db.session.add(note)
        
        # Add topics
        if topic_ids:
            topics = Topic.query.filter(
                Topic.id.in_([int(tid) for tid in topic_ids])
            ).all()
            note.topics.extend(topics)
        
        # Link milestone if provided
        if milestone_data and milestone_data.get('msx_milestone_id'):
            msx_id = milestone_data['msx_milestone_id']
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_id).first()
            if not milestone:
                milestone = Milestone(
                    msx_milestone_id=msx_id,
                    url=milestone_data.get('url', ''),
                    milestone_number=milestone_data.get('number', ''),
                    title=milestone_data.get('name', ''),
                    msx_status=milestone_data.get('status', ''),
                    msx_status_code=milestone_data.get('status_code'),
                    opportunity_name=milestone_data.get('opportunity_name', ''),
                    customer_id=int(customer_id)
                )
                db.session.add(milestone)
            else:
                # Update with latest data
                if milestone_data.get('name'):
                    milestone.title = milestone_data['name']
                if milestone_data.get('status'):
                    milestone.msx_status = milestone_data['status']
                if milestone_data.get('opportunity_name'):
                    milestone.opportunity_name = milestone_data['opportunity_name']
            
            note.milestones = [milestone]
        
        # Link pre-created MSX task if provided
        if created_task_id and milestone_data and milestone_data.get('msx_milestone_id'):
            msx_id = milestone_data['msx_milestone_id']
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_id).first()
            if milestone:
                task_due_date = None
                if task_due_date_str:
                    try:
                        task_due_date = datetime.strptime(task_due_date_str, '%Y-%m-%d')
                    except ValueError:
                        pass
                
                msx_task = MsxTask(
                    msx_task_id=created_task_id,
                    msx_task_url=created_task_url,
                    subject=task_subject,
                    description=task_description if task_description else None,
                    task_category=0,  # Category code not passed from fill-my-day
                    task_category_name=created_task_category_name or 'Unknown',
                    duration_minutes=60,
                    is_hok=created_task_is_hok,
                    due_date=task_due_date,
                    note=note,
                    milestone=milestone
                )
                db.session.add(msx_task)
                logger.info(f"Fill My Day - linked task {created_task_id} to call log")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'note_id': note.id,
            'view_url': url_for('notes.note_view', id=note.id)
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Fill My Day - save error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
