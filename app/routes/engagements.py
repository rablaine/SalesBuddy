"""
Engagement routes for Sales Buddy.
Handles CRUD operations for customer engagement threads.
"""
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from app.models import (
    db, Engagement, Customer, Note, Opportunity, Milestone, Topic
)
from app.services.milestone_tracking import track_engagement_on_milestones

logger = logging.getLogger(__name__)

# Create blueprint
engagements_bp = Blueprint('engagements', __name__)


@engagements_bp.route('/engagements')
def engagements_hub():
    """Engagements hub - overview of all engagements across customers."""
    # Summary stats
    total = Engagement.query.count()
    active = Engagement.query.filter_by(status='Active').count()
    on_hold = Engagement.query.filter_by(status='On Hold').count()
    won = Engagement.query.filter_by(status='Won').count()
    lost = Engagement.query.filter_by(status='Lost').count()

    # Story completeness breakdown (active + on hold only)
    active_engagements = Engagement.query.filter(
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

    query = Engagement.query
    if status_filter in Engagement.STATUSES:
        query = query.filter(Engagement.status == status_filter)

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
        estimated_acr = request.form.get('estimated_acr', '').strip() or None
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
        estimated_acr = request.form.get('estimated_acr', '').strip() or None
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
                       'solution_resources', 'estimated_acr'):
            val = data.get(field, '').strip()
            if val:
                setattr(engagement, field, val)
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
