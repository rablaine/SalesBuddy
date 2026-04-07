"""
Note routes for Sales Buddy.
Handles note listing, creation, viewing, editing, and Fill My Day bulk import.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify, session
from datetime import datetime
import logging

from app.models import db, Note, Customer, Seller, Territory, Topic, Partner, Milestone, Opportunity, MsxTask, UserPreference, NoteTemplate, NoteAttendee, SolutionEngineer, CustomerContact, PartnerContact, InternalContact
from app.services.msx_api import TASK_CATEGORIES, add_user_to_milestone_team
from app.services.seller_mode import get_seller_mode_seller_id as _get_seller_mode_seller_id
from app.services.backup import backup_customer as _backup_customer
from app.services.milestone_tracking import track_note_on_milestones

logger = logging.getLogger(__name__)

# Create blueprint
notes_bp = Blueprint('notes', __name__)


def _cross_link_milestones_to_engagements(note):
    """Attach the note's milestones to its linked engagements if not already linked."""
    if not note.milestones or not note.engagements:
        return
    for eng in note.engagements:
        for ms in note.milestones:
            if ms not in eng.milestones:
                eng.milestones.append(ms)
                logger.info(
                    "Auto-linked milestone %s to engagement %s via note %s",
                    ms.id, eng.id, note.id,
                )


def _handle_milestone_and_task(note):
    """
    Handle MSX milestone selection and optional task creation.
    
    Reads form data for milestone info and creates/links milestones.
    If task fields are provided, creates the task in MSX and stores it locally.
    
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    # Get MSX milestone data from form (supports multiple)
    msx_ids = request.form.getlist('milestone_msx_id')
    # Filter out empty strings
    msx_ids = [mid.strip() for mid in msx_ids if mid.strip()]
    
    if not msx_ids:
        # No milestones selected - clear any existing
        note.milestones = []
        return True, None
    
    # Parallel metadata arrays from hidden inputs
    names = request.form.getlist('milestone_name')
    numbers = request.form.getlist('milestone_number')
    statuses = request.form.getlist('milestone_status')
    status_codes = request.form.getlist('milestone_status_code')
    opp_names = request.form.getlist('milestone_opportunity_name')
    workloads = request.form.getlist('milestone_workload')
    monthly_usages = request.form.getlist('milestone_monthly_usage')
    urls = request.form.getlist('milestone_url')
    
    customer_id = request.form.get('customer_id')
    
    milestones = []
    first_milestone = None
    
    for i, msx_id in enumerate(msx_ids):
        # Safe index into parallel arrays (edit-mode cards may not have all metadata)
        name = names[i] if i < len(names) else ''
        number = numbers[i] if i < len(numbers) else ''
        status = statuses[i] if i < len(statuses) else ''
        status_code = status_codes[i] if i < len(status_codes) else ''
        opp_name = opp_names[i] if i < len(opp_names) else ''
        workload = workloads[i] if i < len(workloads) else ''
        monthly_usage_str = monthly_usages[i] if i < len(monthly_usages) else ''
        url = urls[i] if i < len(urls) else ''
        monthly_usage = float(monthly_usage_str) if monthly_usage_str else None
        
        milestone = Milestone.query.filter_by(msx_milestone_id=msx_id).first()
        if not milestone:
            milestone = Milestone(
                msx_milestone_id=msx_id,
                url=url,
                milestone_number=number,
                title=name,
                msx_status=status,
                msx_status_code=int(status_code) if status_code else None,
                opportunity_name=opp_name,
                workload=workload or None,
                monthly_usage=monthly_usage,
                customer_id=int(customer_id) if customer_id else None
            )
            db.session.add(milestone)
        else:
            if name:
                milestone.title = name
            if url:
                milestone.url = url
            if status:
                milestone.msx_status = status
            if status_code:
                milestone.msx_status_code = int(status_code)
            if opp_name:
                milestone.opportunity_name = opp_name
            if workload:
                milestone.workload = workload
            if monthly_usage is not None:
                milestone.monthly_usage = monthly_usage
        
        milestones.append(milestone)
        if first_milestone is None:
            first_milestone = milestone
        
        # Auto-join the milestone access team (best-effort, non-blocking)
        if msx_id and not milestone.on_my_team:
            try:
                join_result = add_user_to_milestone_team(msx_id)
                if join_result.get('success') or 'already' in join_result.get('error', '').lower():
                    milestone.on_my_team = True
                    logger.info(f'Auto-joined milestone team for {msx_id}')
                else:
                    logger.warning(f'Could not auto-join milestone team: {join_result.get("error")}')
            except Exception as e:
                logger.warning(f'Auto-join milestone team failed (non-blocking): {e}')
    
    # Associate all milestones with note
    note.milestones = milestones
    
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
        
        logger.info(f"Linking pre-created MSX task {created_task_id} to note")
        
        # The API endpoint already saves the task locally on creation.
        # Just link the existing record to this note instead of creating a duplicate.
        existing_task = MsxTask.query.filter_by(msx_task_id=created_task_id).first()
        if existing_task:
            existing_task.note = note
            logger.info(f"Linked existing MSX task {created_task_id} to note")
        else:
            # Fallback: task wasn't saved locally yet (shouldn't happen normally)
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
                milestone=first_milestone
            )
            db.session.add(msx_task)
            logger.info(f"Created and linked MSX task {created_task_id} to note")
    
    return True, None


def _handle_opportunity(note):
    """
    Handle opportunity selection for DSS users.

    Reads form data for opportunity info and creates/links Opportunity records.
    No auto-writeback (no comments, no team join) per spec.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    msx_ids = request.form.getlist('opportunity_msx_id')
    msx_ids = [oid.strip() for oid in msx_ids if oid.strip()]

    if not msx_ids:
        note.opportunities = []
        return True, None

    names = request.form.getlist('opportunity_name')
    numbers = request.form.getlist('opportunity_number')
    states = request.form.getlist('opportunity_state')
    est_values = request.form.getlist('opportunity_estimated_value')
    urls = request.form.getlist('opportunity_url')

    customer_id = request.form.get('customer_id')

    opportunities = []
    for i, msx_id in enumerate(msx_ids):
        name = names[i] if i < len(names) else ''
        number = numbers[i] if i < len(numbers) else ''
        state = states[i] if i < len(states) else ''
        est_val_str = est_values[i] if i < len(est_values) else ''
        url = urls[i] if i < len(urls) else ''
        est_val = float(est_val_str) if est_val_str else None

        opp = Opportunity.query.filter_by(msx_opportunity_id=msx_id).first()
        if not opp:
            opp = Opportunity(
                msx_opportunity_id=msx_id,
                opportunity_number=number or None,
                name=name,
                state=state or None,
                estimated_value=est_val,
                msx_url=url or None,
                customer_id=int(customer_id) if customer_id else None,
            )
            db.session.add(opp)
        else:
            if name:
                opp.name = name
            if state:
                opp.state = state
            if est_val is not None:
                opp.estimated_value = est_val
            if url:
                opp.msx_url = url

        opportunities.append(opp)

    note.opportunities = opportunities
    return True, None


@notes_bp.route('/notes')
def notes_list():
    """List all notes (FR010)."""
    filter_type = request.args.get('filter', '')
    
    query = Note.query.options(
        db.joinedload(Note.customer).joinedload(Customer.seller),
        db.joinedload(Note.customer).joinedload(Customer.territory),
        db.joinedload(Note.topics),
        db.joinedload(Note.partners),
        db.joinedload(Note.engagements)
    )
    
    # Seller mode scoping
    seller_mode_seller_id = _get_seller_mode_seller_id()
    if seller_mode_seller_id:
        query = query.join(Note.customer).filter(Customer.seller_id == seller_mode_seller_id)
    
    if filter_type == 'customer':
        query = query.filter(Note.customer_id.isnot(None))
    elif filter_type == 'general':
        query = query.filter(Note.customer_id.is_(None))
    
    notes = query.order_by(Note.call_date.desc()).all()
    return render_template('notes_list.html', notes=notes, filter_type=filter_type)


@notes_bp.route('/note/new', methods=['GET', 'POST'])
def note_create():
    """Create a new note (FR005)."""
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
        
        # Create note
        note = Note(
            customer_id=int(customer_id) if customer_id else None,
            call_date=call_date,
            content=content)
        
        # Add topics
        if topic_ids:
            topics = Topic.query.filter(Topic.id.in_([int(tid) for tid in topic_ids])).all()
            note.topics.extend(topics)
        
        # Create and add any new (pending) topics
        new_topic_names = request.form.getlist('new_topic_names')
        for name in new_topic_names:
            name = name.strip()
            if not name:
                continue
            existing = Topic.query.filter(
                db.func.lower(Topic.name) == name.lower()
            ).first()
            if existing:
                if existing not in note.topics:
                    note.topics.append(existing)
            else:
                new_topic = Topic(name=name)
                db.session.add(new_topic)
                db.session.flush()
                note.topics.append(new_topic)
        
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

        # Add project (for general notes)
        project_id = request.form.get('project_id')
        if project_id:
            from app.models import Project
            proj = db.session.get(Project, int(project_id))
            if proj:
                note.projects.append(proj)
        
        db.session.add(note)
        
        # Add attendees
        attendee_types = request.form.getlist('attendee_types')
        attendee_ref_ids = request.form.getlist('attendee_ref_ids')
        ext_names = request.form.getlist('attendee_ext_names')
        ext_emails = request.form.getlist('attendee_ext_emails')
        ext_idx = 0
        for atype, ref_id in zip(attendee_types, attendee_ref_ids):
            if atype == 'external':
                att = NoteAttendee(
                    external_name=ext_names[ext_idx] if ext_idx < len(ext_names) else None,
                    external_email=ext_emails[ext_idx] if ext_idx < len(ext_emails) else None,
                )
                note.attendees.append(att)
                ext_idx += 1
            else:
                attendee = _make_attendee(atype, ref_id)
                if attendee:
                    note.attendees.append(attendee)

        # Handle milestone and optional task creation (only for customer-linked notes)
        if customer_id:
            try:
                pref = UserPreference.query.first()
                user_role = pref.user_role if pref else 'se'
                if user_role == 'dss':
                    _handle_opportunity(note)
                else:
                    _handle_milestone_and_task(note)
            except Exception as e:
                logger.exception("Error handling milestone/opportunity during note create")
                flash(f'Note will be saved, but milestone/opportunity failed: {e}', 'warning')
        
        # Cross-link: attach note milestones to linked engagements
        _cross_link_milestones_to_engagements(note)
        
        # Auto-track this note on any linked milestones
        track_note_on_milestones(note)

        db.session.commit()

        # Back up this customer's notes
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
    if preselect_customer_id:
        preselect_customer = Customer.query.filter_by(id=preselect_customer_id).first_or_404()
    
    customers = Customer.query.order_by(Customer.name).all()
    sellers = Seller.query.order_by(Seller.name).all()
    topics = Topic.query.order_by(Topic.name).all()
    partners = Partner.query.order_by(Partner.name).all()
    
    # Pre-select topic from query params
    preselect_topic_id = request.args.get('topic_id', type=int)
    
    # Pre-select engagement from query params
    preselect_engagement_id = request.args.get('engagement_id', type=int)

    # Pre-select milestone from query params
    preselect_milestone_id = request.args.get('milestone_id', type=int)

    # Pre-select project from query params (for general notes)
    preselect_project_id = request.args.get('project_id', type=int)

    # Active projects for the project selector (general notes)
    # Exclude copilot_saved - that's an internal backend construct.
    from app.models import Project
    active_projects = Project.query.filter(
        Project.status.in_(['Active', 'On Hold']),
        Project.project_type != 'copilot_saved',
    ).order_by(Project.title).all()

    # Unattached general notes for project flyout
    unattached_notes = (
        Note.query
        .filter(Note.customer_id.is_(None))
        .filter(~Note.projects.any())
        .order_by(Note.call_date.desc())
        .all()
    )
    
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
    
    # Current time for new notes (default to now)
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
                         preselect_milestone_id=preselect_milestone_id,
                         preselect_project_id=preselect_project_id,
                         active_projects=active_projects,
                         project_types=Project.BUILT_IN_TYPES,
                         unattached_notes=unattached_notes,
                         referrer=referrer,
                         today=today,
                         now_time=now_time,
                         workiq_prompt=user_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT,
                         workiq_connect_impact=connect_impact_enabled,
                         next_url='')


@notes_bp.route('/note/<int:id>')
def note_view(id):
    """View note details (FR010)."""
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


@notes_bp.route('/api/note/<int:id>/detail')
def note_detail_fragment(id):
    """Return rendered HTML fragment of note view for modal embedding."""
    note = Note.query.filter_by(id=id).first_or_404()
    return render_template(
        'partials/note_view_content.html',
        note=note,
        back_url='',
        show_edit_delete=False,
    )


@notes_bp.route('/api/notes/related')
def api_notes_related():
    """Return related notes as JSON for the note form right column.

    Query params:
        customer_id - required customer ID
        engagement_ids - comma-separated engagement IDs (optional)
        exclude_note_id - note ID to exclude (optional, for edit mode)

    If engagement_ids provided: returns notes linked to those engagements.
    Otherwise: returns customer notes that have no engagements.
    """
    from app.models import Engagement, notes_engagements
    customer_id = request.args.get('customer_id', type=int)
    if not customer_id:
        return jsonify([])

    exclude_note_id = request.args.get('exclude_note_id', type=int)
    eng_ids_str = request.args.get('engagement_ids', '')
    engagement_ids = [int(x) for x in eng_ids_str.split(',') if x.strip()]

    if engagement_ids:
        # Notes linked to any of the selected engagements
        query = (
            Note.query
            .filter(Note.customer_id == customer_id)
            .filter(Note.engagements.any(Engagement.id.in_(engagement_ids)))
            .options(db.joinedload(Note.topics))
            .order_by(Note.call_date.desc())
        )
    else:
        # Customer notes with no engagement links
        query = (
            Note.query
            .filter(Note.customer_id == customer_id)
            .filter(~Note.engagements.any())
            .options(db.joinedload(Note.topics))
            .order_by(Note.call_date.desc())
        )

    if exclude_note_id:
        query = query.filter(Note.id != exclude_note_id)

    notes = query.limit(50).all()
    result = []
    for n in notes:
        result.append({
            'id': n.id,
            'call_date': n.call_date.strftime('%b %d, %Y') if n.call_date else '',
            'call_time': (
                n.call_date.strftime('%I:%M %p')
                if n.call_date and (n.call_date.hour or n.call_date.minute) else ''
            ),
            'topics': [{'name': t.name} for t in n.topics],
            'content': n.content or '',
        })
    return jsonify(result)


@notes_bp.route('/note/<int:id>/edit', methods=['GET', 'POST'])
def note_edit(id):
    """Edit note (FR010)."""
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
        
        # Update note
        note.customer_id = int(customer_id) if customer_id else None
        # Seller and territory are now derived from customer
        note.call_date = call_date
        note.content = content
        
        # Update topics - remove all existing associations first
        note.topics = []
        if topic_ids:
            topics = Topic.query.filter(Topic.id.in_([int(tid) for tid in topic_ids])).all()
            note.topics = topics
        
        # Create and add any new (pending) topics
        new_topic_names = request.form.getlist('new_topic_names')
        for name in new_topic_names:
            name = name.strip()
            if not name:
                continue
            existing = Topic.query.filter(
                db.func.lower(Topic.name) == name.lower()
            ).first()
            if existing:
                if existing not in note.topics:
                    note.topics.append(existing)
            else:
                new_topic = Topic(name=name)
                db.session.add(new_topic)
                db.session.flush()
                note.topics.append(new_topic)
        
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

        # Update projects
        note.projects = []
        project_id = request.form.get('project_id')
        if project_id:
            from app.models import Project
            proj = db.session.get(Project, int(project_id))
            if proj:
                note.projects.append(proj)

        # Update attendees
        note.attendees = []
        attendee_types = request.form.getlist('attendee_types')
        attendee_ref_ids = request.form.getlist('attendee_ref_ids')
        ext_names = request.form.getlist('attendee_ext_names')
        ext_emails = request.form.getlist('attendee_ext_emails')
        ext_idx = 0
        for atype, ref_id in zip(attendee_types, attendee_ref_ids):
            if atype == 'external':
                att = NoteAttendee(
                    external_name=ext_names[ext_idx] if ext_idx < len(ext_names) else None,
                    external_email=ext_emails[ext_idx] if ext_idx < len(ext_emails) else None,
                )
                note.attendees.append(att)
                ext_idx += 1
            else:
                attendee = _make_attendee(atype, ref_id)
                if attendee:
                    note.attendees.append(attendee)
        
        # Handle milestone and optional task creation (only for customer-linked notes)
        if customer_id:
            try:
                pref = UserPreference.query.first()
                user_role = pref.user_role if pref else 'se'
                if user_role == 'dss':
                    _handle_opportunity(note)
                else:
                    _handle_milestone_and_task(note)
            except Exception as e:
                logger.exception("Error handling milestone/opportunity during note edit")
                flash(f'Note will be saved, but milestone/opportunity failed: {e}', 'warning')
        
        # Cross-link: attach note milestones to linked engagements
        _cross_link_milestones_to_engagements(note)
        
        # Auto-track this note on any linked milestones
        track_note_on_milestones(note)

        db.session.commit()

        # Back up this customer's notes
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
    
    from app.models import Project
    active_projects = Project.query.filter(
        Project.status.in_(['Active', 'On Hold']),
        Project.project_type != 'copilot_saved',
    ).order_by(Project.title).all()

    # Unattached general notes for project flyout
    unattached_notes = (
        Note.query
        .filter(Note.customer_id.is_(None))
        .filter(~Note.projects.any())
        .order_by(Note.call_date.desc())
        .all()
    )

    return render_template('note_form.html',
                         note=note,
                         customers=customers,
                         sellers=sellers,
                         topics=topics,
                         partners=partners,
                         templates=templates,
                         preselect_customer_id=None,
                         preselect_topic_id=None,
                         preselect_project_id=None,
                         active_projects=active_projects,
                         project_types=Project.BUILT_IN_TYPES,
                         unattached_notes=unattached_notes,
                         workiq_prompt=user_prompt,
                         default_workiq_prompt=DEFAULT_SUMMARY_PROMPT,
                         workiq_connect_impact=connect_impact_enabled,
                         next_url=next_url)


@notes_bp.route('/note/<int:id>/delete', methods=['POST'])
def note_delete(id):
    """Delete a note."""
    note = db.session.get(Note, id)
    
    if not note:
        flash('Note not found.', 'danger')
        return redirect(url_for('notes.notes_list'))
    
    # Store customer for redirect
    customer_id = note.customer_id
    
    # Delete the note
    db.session.delete(note)
    db.session.commit()

    # Back up this customer's notes (reflects the deletion)
    try:
        _backup_customer(customer_id)
    except Exception:
        logger.debug("Backup skipped", exc_info=True)
    
    flash('Note deleted successfully.', 'success')
    
    # Redirect to customer view if we have a customer, otherwise notes list
    if customer_id:
        return redirect(url_for('customers.customer_view', id=customer_id))
    else:
        return redirect(url_for('notes.notes_list'))


@notes_bp.route('/notes/<int:id>/retry-msx', methods=['POST'])
def note_retry_msx(id):
    """Re-trigger milestone comment sync for a note after a previous failure."""
    note = db.session.get(Note, id)
    if not note:
        return jsonify({"error": "Note not found"}), 404

    if not note.milestones:
        return jsonify({"error": "No milestones linked"}), 400

    track_note_on_milestones(note)
    return jsonify({"ok": True}), 202


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
        
        # Second pass: use OpenAI gateway for topic generation from the summary
        topics = []
        summary_text = result.get('summary', '')
        if summary_text and not summary_text.startswith('Error'):
            try:
                from app.gateway_client import gateway_call
                existing_topics = [t.name for t in Topic.query.order_by(Topic.name).all()]
                ai_result = gateway_call('/v1/suggest-topics', {
                    'call_notes': summary_text[:3000],
                    'existing_topics': existing_topics,
                })
                topics = ai_result.get('topics', [])
            except Exception as e:
                logger.warning(f'Topic suggestion failed for "{title}": {e}')
        
        return jsonify({
            'summary': summary_text,
            'topics': topics,
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
    """Fill My Day page - bulk import meetings for a date into notes."""
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
    from markupsafe import escape
    
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
        
        # Build HTML content — split summary on double-newlines into <p> tags
        content_html = f'<h2>{escape(title)}</h2>'
        content_html += '<p><strong>Summary:</strong></p>'
        for para in summary.split('\n\n'):
            stripped = para.strip()
            if stripped:
                content_html += '<p>' + str(escape(stripped)).replace('\n', '<br>') + '</p>'
        if action_items:
            content_html += '<p><strong>Action Items:</strong></p><ul>'
            for item in action_items:
                content_html += f'<li>{escape(item)}</li>'
            content_html += '</ul>'
        
        # Add Connect impact signals if present
        impact_items = summary_data.get('connect_impact', [])
        if impact_items:
            content_html += '<hr><p><strong>Impact Signals:</strong></p><ul>'
            for item in impact_items:
                content_html += f'<li>{escape(item)}</li>'
            content_html += '</ul>'
        
        # Add engagement metadata signals if present (Ben's table fields)
        eng_signals = summary_data.get('engagement_signals', {})
        if eng_signals:
            content_html += '<hr><p><strong>Engagement Metadata:</strong></p><ul>'
            for field, value in eng_signals.items():
                content_html += f'<li><strong>{escape(field)}:</strong> {escape(value)}</li>'
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
        result['content_html'] = f'<h2>{escape(title)}</h2><p><em>Summary unavailable</em></p>'
    
    # Step 2: AI topic generation via gateway suggest-topics (dedup + normalization)
    if result['summary_ok']:
        try:
            from app.gateway_client import gateway_call, GatewayError

            existing_topics = [t.name for t in Topic.query.order_by(Topic.name).all()]
            ai_result = gateway_call("/v1/suggest-topics", {
                "call_notes": result['summary'][:3000],
                "existing_topics": [t for t in existing_topics],
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
    Save a single note from Fill My Day.
    
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
        # Create note
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
            
            # Auto-join the milestone access team (best-effort, non-blocking)
            if not milestone.on_my_team:
                try:
                    join_result = add_user_to_milestone_team(msx_id)
                    if join_result.get('success') or 'already' in join_result.get('error', '').lower():
                        milestone.on_my_team = True
                        logger.info(f'Fill My Day: auto-joined milestone team for {msx_id}')
                    else:
                        logger.warning(f'Fill My Day: could not auto-join milestone team: {join_result.get("error")}')
                except Exception as e:
                    logger.warning(f'Fill My Day: auto-join milestone team failed (non-blocking): {e}')
        
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
                logger.info(f"Fill My Day - linked task {created_task_id} to note")
        
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


# =============================================================================
# Note Sharing API
# =============================================================================

@notes_bp.route('/api/share/note/<int:note_id>')
def api_share_serialize_note(note_id):
    """Serialize a single note for sharing."""
    from app.services.note_sharing import serialize_note
    note = Note.query.get_or_404(note_id)
    return jsonify({'success': True, 'note': serialize_note(note)})


@notes_bp.route('/api/share/receive-note', methods=['POST'])
def api_share_receive_note():
    """Receive and import a shared note from a sharing peer."""
    from app.services.note_sharing import import_shared_note
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    note_data = data.get('note')
    sender_name = data.get('sender_name', 'Unknown')

    if not note_data:
        return jsonify({'success': False, 'error': 'No note in payload'}), 400

    try:
        result = import_shared_note(note_data, sender_name)
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        logger.error(f"Note share import error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================================================
# Attendee helpers and API
# =============================================================================

def _make_attendee(atype: str, ref_id: str) -> 'NoteAttendee | None':
    """Create a NoteAttendee from a type string and reference ID."""
    try:
        rid = int(ref_id)
    except (ValueError, TypeError):
        return None
    if atype == 'customer_contact':
        return NoteAttendee(customer_contact_id=rid)
    elif atype == 'partner_contact':
        return NoteAttendee(partner_contact_id=rid)
    elif atype == 'se':
        return NoteAttendee(solution_engineer_id=rid)
    elif atype == 'seller':
        return NoteAttendee(seller_id=rid)
    elif atype == 'internal_contact':
        return NoteAttendee(internal_contact_id=rid)
    return None


@notes_bp.route('/api/note/<int:note_id>/attendees')
def api_note_attendees(note_id):
    """List attendees for a note."""
    note = Note.query.get_or_404(note_id)
    return jsonify([a.to_dict() for a in note.attendees])


@notes_bp.route('/api/note/<int:note_id>/attendees', methods=['POST'])
def api_add_attendee(note_id):
    """Add an attendee to a note."""
    note = Note.query.get_or_404(note_id)
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    atype = data.get('type', '')
    ref_id = data.get('id')

    if atype == 'external':
        attendee = NoteAttendee(
            note_id=note.id,
            external_name=data.get('name', '').strip() or None,
            external_email=data.get('email', '').strip() or None,
        )
    else:
        attendee = _make_attendee(atype, ref_id)
        if not attendee:
            return jsonify({'success': False, 'error': 'Invalid attendee'}), 400
        attendee.note_id = note.id

    db.session.add(attendee)
    db.session.commit()
    return jsonify({'success': True, 'attendee': attendee.to_dict()})


@notes_bp.route('/api/note/<int:note_id>/attendees/<int:attendee_id>', methods=['DELETE'])
def api_remove_attendee(note_id, attendee_id):
    """Remove an attendee from a note."""
    attendee = NoteAttendee.query.filter_by(id=attendee_id, note_id=note_id).first_or_404()
    db.session.delete(attendee)
    db.session.commit()
    return jsonify({'success': True})


@notes_bp.route('/api/attendee-search')
def api_attendee_search():
    """Search across all person types for the attendee picker.

    Query params:
        q: search string (required)
        customer_id: filter customer contacts to this customer
        partner_ids: comma-separated partner IDs for partner contact filtering
    """
    q = request.args.get('q', '').strip().lower()
    if len(q) < 1:
        return jsonify({'results': []})

    customer_id = request.args.get('customer_id', type=int)
    partner_ids_str = request.args.get('partner_ids', '')
    partner_ids = [int(x) for x in partner_ids_str.split(',') if x.strip().isdigit()]

    results = []

    # Customer contacts (filtered to note's customer)
    if customer_id:
        contacts = CustomerContact.query.filter(
            CustomerContact.customer_id == customer_id,
            db.or_(
                db.func.lower(CustomerContact.name).contains(q),
                db.func.lower(CustomerContact.email).contains(q),
            )
        ).limit(10).all()
        for c in contacts:
            results.append({
                'type': 'customer_contact', 'id': c.id,
                'name': c.name, 'email': c.email, 'detail': c.title,
                'icon': 'bi-person-circle', 'color': 'success',
            })

    # Partner contacts (filtered to tagged partners)
    if partner_ids:
        pcontacts = PartnerContact.query.filter(
            PartnerContact.partner_id.in_(partner_ids),
            db.or_(
                db.func.lower(PartnerContact.name).contains(q),
                db.func.lower(PartnerContact.email).contains(q),
            )
        ).limit(10).all()
        for c in pcontacts:
            results.append({
                'type': 'partner_contact', 'id': c.id,
                'name': c.name, 'email': c.email,
                'detail': c.partner.name if c.partner else None,
                'icon': 'bi-building', 'color': 'purple',
            })

    # Solution engineers
    ses = SolutionEngineer.query.filter(
        db.or_(
            db.func.lower(SolutionEngineer.name).contains(q),
            db.func.lower(SolutionEngineer.alias).contains(q),
        )
    ).limit(10).all()
    for se in ses:
        results.append({
            'type': 'se', 'id': se.id,
            'name': se.name, 'email': se.get_email(),
            'detail': se.specialty,
            'icon': 'bi-tools', 'color': 'info',
        })

    # Sellers
    sellers = Seller.query.filter(
        db.or_(
            db.func.lower(Seller.name).contains(q),
            db.func.lower(Seller.alias).contains(q),
        )
    ).limit(10).all()
    for s in sellers:
        results.append({
            'type': 'seller', 'id': s.id,
            'name': s.name, 'email': s.get_email(),
            'detail': s.seller_type,
            'icon': 'bi-person', 'color': 'primary',
        })

    # Internal contacts (DAEs, DSS, other Microsoft employees)
    internals = InternalContact.query.filter(
        db.or_(
            db.func.lower(InternalContact.name).contains(q),
            db.func.lower(InternalContact.alias).contains(q),
        )
    ).limit(10).all()
    for ic in internals:
        results.append({
            'type': 'internal_contact', 'id': ic.id,
            'name': ic.name, 'email': ic.get_email(),
            'detail': ic.role,
            'icon': 'bi-person-badge', 'color': 'warning',
        })

    return jsonify({'results': results})


# =============================================================================
# Internal Contacts (Microsoft employees not tracked as Sellers or SEs)
# =============================================================================

@notes_bp.route('/api/internal-contacts', methods=['POST'])
def api_create_internal_contact():
    """Create or find an internal contact.

    Accepts either a name or email (alias). If an alias is provided and
    already exists, returns the existing record instead of creating a
    duplicate. This lets users enter minimal info and update later.

    Body: {name, alias?, role?}
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    name = (data.get('name') or '').strip()
    alias = (data.get('alias') or '').strip().lower()
    role = (data.get('role') or '').strip() or None

    # Strip @microsoft.com if user entered full email
    if alias.endswith('@microsoft.com'):
        alias = alias[:-len('@microsoft.com')]

    if not name and not alias:
        return jsonify({'success': False, 'error': 'Name or alias required'}), 400

    # Check for existing by alias (dedup)
    if alias:
        existing = InternalContact.query.filter(
            db.func.lower(InternalContact.alias) == alias
        ).first()
        if existing:
            # Update name/role if provided and currently empty
            if name and not existing.name:
                existing.name = name
            if role and not existing.role:
                existing.role = role
            db.session.commit()
            return jsonify({
                'success': True,
                'contact': {
                    'id': existing.id, 'name': existing.name,
                    'alias': existing.alias, 'role': existing.role,
                },
            })

    ic = InternalContact(
        name=name or alias,
        alias=alias or None,
        role=role,
    )
    db.session.add(ic)
    db.session.commit()
    return jsonify({
        'success': True,
        'contact': {
            'id': ic.id, 'name': ic.name,
            'alias': ic.alias, 'role': ic.role,
        },
    }), 201


@notes_bp.route('/api/internal-contacts/<int:contact_id>', methods=['PATCH'])
def api_update_internal_contact(contact_id):
    """Update an internal contact's details.

    Body: {name?, alias?, role?}
    """
    ic = InternalContact.query.get_or_404(contact_id)
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    if 'name' in data and data['name'].strip():
        ic.name = data['name'].strip()
    if 'alias' in data:
        alias = data['alias'].strip().lower()
        if alias.endswith('@microsoft.com'):
            alias = alias[:-len('@microsoft.com')]
        ic.alias = alias or None
    if 'role' in data:
        ic.role = data['role'].strip() or None

    db.session.commit()
    return jsonify({
        'success': True,
        'contact': {
            'id': ic.id, 'name': ic.name,
            'alias': ic.alias, 'role': ic.role,
        },
    })


# =============================================================================
# Meeting Attendee Scraping
# =============================================================================

@notes_bp.route('/api/meeting-attendees/scrape', methods=['POST'])
def api_scrape_meeting_attendees():
    """Scrape and categorize attendees from a specific meeting.

    Body: {meeting_title, meeting_date, customer_id?, partner_ids?}
    """
    data = request.get_json()
    if not data or not data.get('meeting_title') or not data.get('meeting_date'):
        return jsonify({'success': False, 'error': 'meeting_title and meeting_date required'}), 400

    try:
        from app.services.meeting_attendee_scrape import scrape_meeting_attendees
        result = scrape_meeting_attendees(
            meeting_title=data['meeting_title'],
            meeting_date=data['meeting_date'],
            customer_id=data.get('customer_id'),
            partner_ids=data.get('partner_ids', []),
        )
        return jsonify({'success': True, **result})
    except TimeoutError:
        return jsonify({'success': False, 'error': 'WorkIQ query timed out. Try again.'}), 504
    except Exception as e:
        logger.exception("Meeting attendee scrape failed")
        return jsonify({'success': False, 'error': str(e)}), 500


@notes_bp.route('/api/meeting-attendees/apply', methods=['POST'])
def api_apply_meeting_attendees():
    """Apply selected meeting attendees - create contacts, partners, return attendee list.

    Body: {customer_id, attendees: [{category, name, email, ref_type, ref_id,
           partner_id, new_partner_domain, new_partner_name, checked}]}
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400

    customer_id = data.get('customer_id')
    attendees = data.get('attendees', [])
    created_contacts = 0
    new_partners = []
    linked_partner_ids = set()  # Track partner IDs to auto-add to the note
    attendee_results = []  # {type, id} for adding to NoteAttendee

    # Group new_partner attendees by domain
    new_partner_groups = {}
    for att in attendees:
        if not att.get('checked', True):
            continue
        cat = att.get('category')

        if cat == 'microsoft':
            ref_type = att.get('ref_type')
            ref_id = att.get('ref_id')
            if ref_type and ref_id:
                attendee_results.append({'type': ref_type, 'id': ref_id, 'name': att['name']})
            elif ref_type == 'external':
                # Unknown MS employee - add as external attendee
                attendee_results.append({
                    'type': 'external',
                    'name': att.get('name', ''),
                    'email': att.get('email', ''),
                })

        elif cat == 'customer_contact':
            if att.get('is_new_contact') and customer_id:
                contact = CustomerContact(
                    customer_id=customer_id,
                    name=(att.get('name') or '').strip(),
                    email=(att.get('email') or '').strip() or None,
                    title=(att.get('title') or '').strip() or None,
                )
                db.session.add(contact)
                db.session.flush()
                created_contacts += 1
                attendee_results.append({'type': 'customer_contact', 'id': contact.id, 'name': contact.name})
            elif att.get('ref_id'):
                # Update title if available
                if att.get('has_updates') and att.get('title'):
                    existing = db.session.get(CustomerContact, att['ref_id'])
                    if existing:
                        existing.title = att['title']
                attendee_results.append({'type': 'customer_contact', 'id': att['ref_id'], 'name': att['name']})

        elif cat == 'partner_contact':
            partner_id = att.get('partner_id')
            if att.get('is_new_contact') and partner_id:
                contact = PartnerContact(
                    partner_id=partner_id,
                    name=(att.get('name') or '').strip(),
                    email=(att.get('email') or '').strip() or None,
                    title=(att.get('title') or '').strip() or None,
                )
                db.session.add(contact)
                db.session.flush()
                created_contacts += 1
                attendee_results.append({'type': 'partner_contact', 'id': contact.id, 'name': contact.name})
            elif att.get('ref_id'):
                # Update title if available
                if att.get('has_updates') and att.get('title'):
                    existing = db.session.get(PartnerContact, att['ref_id'])
                    if existing:
                        existing.title = att['title']
                attendee_results.append({'type': 'partner_contact', 'id': att['ref_id'], 'name': att['name']})
            # Track the partner so it can be auto-added to the note
            if partner_id and partner_id not in linked_partner_ids:
                linked_partner_ids.add(partner_id)
                partner = db.session.get(Partner, partner_id)
                if partner:
                    new_partners.append({'id': partner.id, 'name': partner.name})

        elif cat == 'new_partner':
            domain = att.get('new_partner_domain', '')
            if domain not in new_partner_groups:
                new_partner_groups[domain] = {
                    'name': att.get('new_partner_name', domain),
                    'contacts': [],
                }
            new_partner_groups[domain]['contacts'].append(att)

    # Create new partners and their contacts
    from app.routes.admin import fetch_favicon_for_domain
    for domain, group in new_partner_groups.items():
        partner = Partner(
            name=group['name'],
            website=domain,
            favicon_b64=fetch_favicon_for_domain(domain),
        )
        db.session.add(partner)
        db.session.flush()
        if partner.id not in linked_partner_ids:
            new_partners.append({'id': partner.id, 'name': partner.name})
            linked_partner_ids.add(partner.id)

        for att in group['contacts']:
            contact = PartnerContact(
                partner_id=partner.id,
                name=(att.get('name') or '').strip(),
                email=(att.get('email') or '').strip() or None,
                title=(att.get('title') or '').strip() or None,
            )
            db.session.add(contact)
            db.session.flush()
            created_contacts += 1
            attendee_results.append({'type': 'partner_contact', 'id': contact.id, 'name': contact.name})

    db.session.commit()

    return jsonify({
        'success': True,
        'attendee_results': attendee_results,
        'new_partners': new_partners,
        'contacts_created': created_contacts,
    })
