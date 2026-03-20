"""
Engagement routes for Sales Buddy.
Handles CRUD operations for customer engagement threads.
"""
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from app.models import (
    db, Engagement, EngagementTask, Customer, Note, Opportunity, Milestone, Topic
)
from app.services.milestone_tracking import track_engagement_on_milestones
from app.services.seller_mode import get_seller_mode_seller_id

logger = logging.getLogger(__name__)

# Create blueprint
engagements_bp = Blueprint('engagements', __name__)


@engagements_bp.route('/engagements')
def engagements_hub():
    """Engagements hub - overview of all engagements across customers."""
    seller_mode_sid = get_seller_mode_seller_id()

    def _base_q():
        q = Engagement.query
        if seller_mode_sid:
            q = q.join(Customer, Engagement.customer_id == Customer.id).filter(
                Customer.seller_id == seller_mode_sid
            )
        return q

    # Summary stats
    total = _base_q().count()
    active = _base_q().filter(Engagement.status == 'Active').count()
    on_hold = _base_q().filter(Engagement.status == 'On Hold').count()
    won = _base_q().filter(Engagement.status == 'Won').count()
    lost = _base_q().filter(Engagement.status == 'Lost').count()

    # Story completeness breakdown (active + on hold only)
    active_engagements = _base_q().filter(
        Engagement.status.in_(['Active', 'On Hold'])
    ).all()
    story_empty = sum(1 for e in active_engagements if e.story_completeness == 0)
    story_partial = sum(1 for e in active_engagements if 0 < e.story_completeness < 100)
    story_complete = sum(1 for e in active_engagements if e.story_completeness == 100)

    return render_template(
        'engagements_hub.html',
        stats={
            'total': total,
            'active': active,
            'on_hold': on_hold,
            'won': won,
            'lost': lost,
            'story_empty': story_empty,
            'story_partial': story_partial,
            'story_complete': story_complete,
        },
    )


@engagements_bp.route('/api/engagements/all')
def api_all_engagements():
    """Return all engagements, optionally filtered by status."""
    from sqlalchemy.orm import joinedload, subqueryload

    status_filter = request.args.get('status', '').strip()
    seller_mode_sid = get_seller_mode_seller_id()

    query = Engagement.query
    if status_filter in Engagement.STATUSES:
        query = query.filter(Engagement.status == status_filter)

    if seller_mode_sid:
        query = query.join(Customer, Engagement.customer_id == Customer.id).filter(
            Customer.seller_id == seller_mode_sid
        )

    query = query.options(
        joinedload(Engagement.customer).joinedload(Customer.seller),
        subqueryload(Engagement.notes),
        subqueryload(Engagement.opportunities),
        subqueryload(Engagement.milestones),
        subqueryload(Engagement.tasks),
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
            'open_task_count': eng.open_task_count,
            'updated_at': eng.updated_at.isoformat() if eng.updated_at else None,
        })

    return jsonify({'success': True, 'engagements': results, 'count': len(results)})


@engagements_bp.route('/engagement/<int:id>')
def engagement_view(id: int):
    """View engagement details with linked notes, opportunities, and milestones."""
    engagement = Engagement.query.get_or_404(id)
    customer = engagement.customer

    # Get notes linked to this engagement, sorted by date desc
    linked_notes = sorted(engagement.notes, key=lambda n: n.call_date, reverse=True)

    # Get all customer notes NOT linked to any engagement (for "assign" UI)
    all_engagement_note_ids = set()
    for eng in customer.engagements:
        for note in eng.notes:
            all_engagement_note_ids.add(note.id)
    unassigned_notes = [
        n for n in customer.notes
        if n.id not in all_engagement_note_ids
    ]
    unassigned_notes.sort(key=lambda n: n.call_date, reverse=True)

    return render_template(
        'engagement_view.html',
        engagement=engagement,
        customer=customer,
        linked_notes=linked_notes,
        unassigned_notes=unassigned_notes,
    )


@engagements_bp.route('/customer/<int:customer_id>/engagement/new', methods=['GET', 'POST'])
def engagement_create(customer_id: int):
    """Create a new engagement for a customer."""
    customer = Customer.query.get_or_404(customer_id)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        status = request.form.get('status', 'Active').strip()
        key_individuals = request.form.get('key_individuals', '').strip() or None
        technical_problem = request.form.get('technical_problem', '').strip() or None
        business_impact = request.form.get('business_impact', '').strip() or None
        solution_resources = request.form.get('solution_resources', '').strip() or None
        estimated_acr_str = request.form.get('estimated_acr', '').strip()
        estimated_acr = int(estimated_acr_str) if estimated_acr_str else None
        target_date_str = request.form.get('target_date', '').strip()

        if not title:
            flash('Engagement title is required.', 'danger')
            return redirect(url_for('engagements.engagement_create',
                                    customer_id=customer_id))

        target_date = None
        if target_date_str:
            try:
                target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid target date format.', 'danger')
                return redirect(url_for('engagements.engagement_create',
                                        customer_id=customer_id))

        engagement = Engagement(
            customer_id=customer_id,
            title=title,
            status=status,
            key_individuals=key_individuals,
            technical_problem=technical_problem,
            business_impact=business_impact,
            solution_resources=solution_resources,
            estimated_acr=estimated_acr,
            target_date=target_date,
        )

        # Link selected notes
        note_ids = request.form.getlist('note_ids')
        if note_ids:
            notes = Note.query.filter(
                Note.id.in_([int(nid) for nid in note_ids]),
                Note.customer_id == customer_id
            ).all()
            engagement.notes.extend(notes)

        # Link selected opportunities
        opp_ids = request.form.getlist('opportunity_ids')
        if opp_ids:
            opps = Opportunity.query.filter(
                Opportunity.id.in_([int(oid) for oid in opp_ids]),
                Opportunity.customer_id == customer_id
            ).all()
            engagement.opportunities.extend(opps)

        # Link selected milestones
        milestone_ids = request.form.getlist('milestone_ids')
        if milestone_ids:
            milestones_list = Milestone.query.filter(
                Milestone.id.in_([int(mid) for mid in milestone_ids]),
                Milestone.customer_id == customer_id
            ).all()
            engagement.milestones.extend(milestones_list)

        db.session.add(engagement)
        db.session.commit()

        flash(f'Engagement "{title}" created.', 'success')
        return redirect(url_for('customers.customer_view', id=customer_id))

    # GET: load form data
    customer_notes = sorted(customer.notes, key=lambda n: n.call_date, reverse=True)
    opportunities = customer.opportunities.order_by(Opportunity.name).all()
    milestones = customer.milestones.order_by(Milestone.title).all()

    return render_template(
        'engagement_form.html',
        customer=customer,
        engagement=None,
        customer_notes=customer_notes,
        opportunities=opportunities,
        milestones=milestones,
    )


@engagements_bp.route('/engagement/<int:id>/edit', methods=['GET', 'POST'])
def engagement_edit(id: int):
    """Edit an existing engagement."""
    engagement = Engagement.query.get_or_404(id)
    customer = engagement.customer

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        status = request.form.get('status', 'Active').strip()
        key_individuals = request.form.get('key_individuals', '').strip() or None
        technical_problem = request.form.get('technical_problem', '').strip() or None
        business_impact = request.form.get('business_impact', '').strip() or None
        solution_resources = request.form.get('solution_resources', '').strip() or None
        estimated_acr_str = request.form.get('estimated_acr', '').strip()
        estimated_acr = int(estimated_acr_str) if estimated_acr_str else None
        target_date_str = request.form.get('target_date', '').strip()

        if not title:
            flash('Engagement title is required.', 'danger')
            return redirect(url_for('engagements.engagement_edit', id=id))

        target_date = None
        if target_date_str:
            try:
                target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid target date format.', 'danger')
                return redirect(url_for('engagements.engagement_edit', id=id))

        engagement.title = title
        engagement.status = status
        engagement.key_individuals = key_individuals
        engagement.technical_problem = technical_problem
        engagement.business_impact = business_impact
        engagement.solution_resources = solution_resources
        engagement.estimated_acr = estimated_acr
        engagement.target_date = target_date

        # Update linked notes
        note_ids = request.form.getlist('note_ids')
        if note_ids:
            notes = Note.query.filter(
                Note.id.in_([int(nid) for nid in note_ids]),
                Note.customer_id == customer.id
            ).all()
            engagement.notes = notes
        else:
            engagement.notes = []

        # Update linked opportunities
        opp_ids = request.form.getlist('opportunity_ids')
        if opp_ids:
            opps = Opportunity.query.filter(
                Opportunity.id.in_([int(oid) for oid in opp_ids]),
                Opportunity.customer_id == customer.id
            ).all()
            engagement.opportunities = opps
        else:
            engagement.opportunities = []

        # Update linked milestones
        milestone_ids = request.form.getlist('milestone_ids')
        if milestone_ids:
            milestones_list = Milestone.query.filter(
                Milestone.id.in_([int(mid) for mid in milestone_ids]),
                Milestone.customer_id == customer.id
            ).all()
            engagement.milestones = milestones_list
        else:
            engagement.milestones = []

        # Auto-track this engagement on any linked milestones
        track_engagement_on_milestones(engagement)

        db.session.commit()

        flash(f'Engagement "{title}" updated.', 'success')
        return redirect(url_for('engagements.engagement_view', id=id))

    # GET: load form data
    customer_notes = sorted(customer.notes, key=lambda n: n.call_date, reverse=True)
    opportunities = customer.opportunities.order_by(Opportunity.name).all()
    milestones = customer.milestones.order_by(Milestone.title).all()

    return render_template(
        'engagement_form.html',
        customer=customer,
        engagement=engagement,
        customer_notes=customer_notes,
        opportunities=opportunities,
        milestones=milestones,
    )


@engagements_bp.route('/engagement/<int:id>/delete', methods=['POST'])
def engagement_delete(id: int):
    """Delete an engagement (does NOT delete linked notes/milestones/opps)."""
    engagement = Engagement.query.get_or_404(id)
    customer_id = engagement.customer_id
    title = engagement.title

    # Clear associations (but don't delete linked objects)
    engagement.notes = []
    engagement.opportunities = []
    engagement.milestones = []
    db.session.delete(engagement)
    db.session.commit()

    flash(f'Engagement "{title}" deleted.', 'success')
    return redirect(url_for('customers.customer_view', id=customer_id))


@engagements_bp.route('/engagement/<int:id>/assign-notes', methods=['POST'])
def engagement_assign_notes(id: int):
    """Assign unassigned notes to an engagement (AJAX or form post)."""
    engagement = Engagement.query.get_or_404(id)
    note_ids = request.form.getlist('note_ids')

    if note_ids:
        notes = Note.query.filter(
            Note.id.in_([int(nid) for nid in note_ids]),
            Note.customer_id == engagement.customer_id
        ).all()
        for note in notes:
            if note not in engagement.notes:
                engagement.notes.append(note)
        db.session.commit()
        flash(f'Assigned {len(notes)} note(s) to "{engagement.title}".', 'success')

    return redirect(url_for('engagements.engagement_view', id=id))


@engagements_bp.route('/api/customer/<int:customer_id>/engagements')
def api_customer_engagements(customer_id: int):
    """List engagements for a customer (for dynamic loading on note form)."""
    customer = Customer.query.get_or_404(customer_id)
    return jsonify([
        {'id': e.id, 'title': e.title, 'status': e.status}
        for e in customer.engagements
    ])


@engagements_bp.route('/api/engagements/milestones')
def api_engagements_milestones():
    """Return milestones linked to the given engagement IDs.

    Query params:
        ids: comma-separated engagement IDs (e.g. ?ids=1,2,3)

    Returns JSON shaped for the note form's selectMilestone() function.
    """
    raw_ids = request.args.get('ids', '')
    try:
        eng_ids = [int(x) for x in raw_ids.split(',') if x.strip()]
    except ValueError:
        return jsonify([])
    if not eng_ids:
        return jsonify([])

    engagements = Engagement.query.filter(Engagement.id.in_(eng_ids)).all()
    seen: set[str] = set()
    results: list[dict] = []
    for eng in engagements:
        for m in eng.milestones:
            key = m.msx_milestone_id or str(m.id)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                'id': m.msx_milestone_id or '',
                'name': m.title or m.milestone_number or 'Milestone',
                'number': m.milestone_number or '',
                'status': m.msx_status or 'Unknown',
                'status_code': m.msx_status_code,
                'opportunity_name': m.opportunity_name or '',
                'workload': m.workload or '',
                'monthly_usage': m.monthly_usage,
                'due_date': m.due_date.isoformat() if m.due_date else None,
                'url': m.url or '',
                'on_my_team': m.on_my_team,
                'local_milestone_id': m.id,
            })
    return jsonify(results)


@engagements_bp.route('/customer/<int:customer_id>/engagement/create-inline',
                       methods=['POST'])
def engagement_create_inline(customer_id: int):
    """Create an engagement with just a title (inline from note form)."""
    customer = Customer.query.get_or_404(customer_id)
    data = request.get_json() if request.is_json else None
    title = (data.get('title', '') if data else request.form.get('title', '')).strip()

    if not title:
        return jsonify(success=False, error='Title is required'), 400

    status = (data.get('status', '') if data else request.form.get('status', '')).strip()
    engagement = Engagement(
        customer_id=customer_id,
        title=title,
        status=status if status in ('Active', 'On Hold', 'Won', 'Lost') else 'Active',
    )
    # Optional story fields
    if data:
        for field in ('key_individuals', 'technical_problem', 'business_impact',
                       'solution_resources'):
            val = data.get(field, '').strip()
            if val:
                setattr(engagement, field, val)
        acr_val = data.get('estimated_acr', '').strip() if data.get('estimated_acr') else ''
        if acr_val:
            try:
                engagement.estimated_acr = int(acr_val)
            except (ValueError, TypeError):
                pass
        target = data.get('target_date', '').strip() if data.get('target_date') else ''
        if target:
            from datetime import date as date_cls
            try:
                engagement.target_date = date_cls.fromisoformat(target)
            except ValueError:
                pass
    db.session.add(engagement)
    db.session.commit()

    return jsonify(success=True, id=engagement.id, title=engagement.title)


# =============================================================================
# Engagement Task Routes
# =============================================================================

@engagements_bp.route('/engagement/<int:id>/tasks', methods=['POST'])
def task_create(id: int):
    """Create a new task on an engagement (JSON API)."""
    engagement = Engagement.query.get_or_404(id)
    data = request.get_json(silent=True) or {}

    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(success=False, error='Title is required.'), 400

    due_date = None
    due_str = (data.get('due_date') or '').strip()
    if due_str:
        try:
            due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify(success=False, error='Invalid date format.'), 400

    # Determine sort_order: place new tasks at the end
    max_order = db.session.query(db.func.max(EngagementTask.sort_order)).filter_by(
        engagement_id=id
    ).scalar() or 0

    task = EngagementTask(
        engagement_id=id,
        title=title,
        description=(data.get('description') or '').strip() or None,
        due_date=due_date,
        contact=(data.get('contact') or '').strip() or None,
        priority=data.get('priority', 'normal') if data.get('priority') in EngagementTask.PRIORITIES else 'normal',
        note_id=data.get('note_id') or None,
        sort_order=max_order + 1,
    )
    db.session.add(task)
    db.session.commit()

    return jsonify(success=True, task=_task_to_dict(task)), 201


@engagements_bp.route('/task/<int:id>', methods=['GET'])
def task_get(id: int):
    """Get a single task as JSON."""
    task = EngagementTask.query.get_or_404(id)
    return jsonify(success=True, task=_task_to_dict(task))


@engagements_bp.route('/task/<int:id>', methods=['PUT'])
def task_update(id: int):
    """Update an existing task (JSON API)."""
    task = EngagementTask.query.get_or_404(id)
    data = request.get_json(silent=True) or {}

    if 'title' in data:
        title = (data['title'] or '').strip()
        if not title:
            return jsonify(success=False, error='Title is required.'), 400
        task.title = title

    if 'description' in data:
        task.description = (data['description'] or '').strip() or None

    if 'due_date' in data:
        due_str = (data['due_date'] or '').strip()
        if due_str:
            try:
                task.due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
            except ValueError:
                return jsonify(success=False, error='Invalid date format.'), 400
        else:
            task.due_date = None

    if 'contact' in data:
        task.contact = (data['contact'] or '').strip() or None

    if 'priority' in data and data['priority'] in EngagementTask.PRIORITIES:
        task.priority = data['priority']

    db.session.commit()
    return jsonify(success=True, task=_task_to_dict(task))


@engagements_bp.route('/task/<int:id>/toggle', methods=['POST'])
def task_toggle(id: int):
    """Toggle task between open and completed."""
    task = EngagementTask.query.get_or_404(id)

    if task.status == 'open':
        task.status = 'completed'
        task.completed_at = datetime.now()
    else:
        task.status = 'open'
        task.completed_at = None

    db.session.commit()
    return jsonify(success=True, task=_task_to_dict(task))


@engagements_bp.route('/engagement/<int:id>/tasks/reorder', methods=['POST'])
def task_reorder(id: int):
    """Persist new sort order for tasks on an engagement."""
    Engagement.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    task_ids = data.get('task_ids', [])
    if not isinstance(task_ids, list):
        return jsonify(success=False, error='task_ids must be a list.'), 400

    for idx, tid in enumerate(task_ids):
        task = EngagementTask.query.filter_by(id=tid, engagement_id=id).first()
        if task:
            task.sort_order = idx
    db.session.commit()
    return jsonify(success=True)


@engagements_bp.route('/task/<int:id>', methods=['DELETE'])
def task_delete(id: int):
    """Delete a task."""
    task = EngagementTask.query.get_or_404(id)
    engagement_id = task.engagement_id
    db.session.delete(task)
    db.session.commit()
    return jsonify(success=True, engagement_id=engagement_id)


def _task_to_dict(task: EngagementTask) -> dict:
    """Serialize a task to a dictionary."""
    return {
        'id': task.id,
        'engagement_id': task.engagement_id,
        'note_id': task.note_id,
        'title': task.title,
        'description': task.description,
        'due_date': task.due_date.isoformat() if task.due_date else None,
        'contact': task.contact,
        'status': task.status,
        'priority': task.priority,
        'is_overdue': task.is_overdue,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
        'created_at': task.created_at.isoformat() if task.created_at else None,
        'sort_order': task.sort_order,
    }
