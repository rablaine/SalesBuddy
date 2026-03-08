"""
Engagement routes for NoteHelper.
Handles CRUD operations for customer engagement threads.
"""
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from app.models import (
    db, Engagement, Customer, Note, Opportunity, Milestone, Topic
)

logger = logging.getLogger(__name__)

# Create blueprint
engagements_bp = Blueprint('engagements', __name__)


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

    from app.routes.ai import is_ai_enabled
    ai_enabled = is_ai_enabled()

    return render_template(
        'engagement_view.html',
        engagement=engagement,
        customer=customer,
        linked_notes=linked_notes,
        unassigned_notes=unassigned_notes,
        ai_enabled=ai_enabled,
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


@engagements_bp.route('/customer/<int:customer_id>/engagement/create-inline',
                       methods=['POST'])
def engagement_create_inline(customer_id: int):
    """Create an engagement with just a title (inline from note form)."""
    customer = Customer.query.get_or_404(customer_id)
    title = request.form.get('title', '').strip()

    if not title:
        return jsonify(success=False, error='Title is required'), 400

    engagement = Engagement(
        customer_id=customer_id,
        title=title,
        status='Active',
    )
    db.session.add(engagement)
    db.session.commit()

    return jsonify(success=True, id=engagement.id, title=engagement.title)
