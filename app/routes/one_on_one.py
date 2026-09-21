"""Persistent one-on-one notes and agenda workspace routes."""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.models import (
    Customer,
    Engagement,
    Milestone,
    Note,
    OneOnOneAgendaItem,
    OneOnOneWorkspace,
    Seller,
    db,
)
from app.services.backup import schedule_customer_backup
from app.services.milestone_tracking import track_note_on_milestones
from app.services.one_on_one import (
    add_milestone_context_to_seller_agenda,
    add_or_restore_agenda_entity,
    get_or_create_seller_workspace,
)

one_on_one_bp = Blueprint('one_on_one', __name__)
logger = logging.getLogger(__name__)

_PERSON_TYPES = {'Manager', 'Seller', 'Other'}
_ITEM_TYPES = {'milestone', 'engagement'}
_ITEM_STATUSES = {'active', 'discussed'}


def _entity_for_item(item_type: str, entity_id: int):
    """Load a supported agenda entity by type and ID."""
    model = Milestone if item_type == 'milestone' else Engagement
    return db.session.get(model, entity_id)


def _entity_customer(entity):
    """Return an agenda entity's customer, if available."""
    return entity.customer if entity else None


def _entity_allowed(workspace: OneOnOneWorkspace, entity) -> bool:
    """Return whether an entity is within a seller workspace's customer scope."""
    customer = _entity_customer(entity)
    if not customer:
        return False
    return not workspace.seller_id or customer.seller_id == workspace.seller_id


def _agenda_item_payload(item: OneOnOneAgendaItem) -> dict:
    """Serialize one agenda item for inline UI updates."""
    return {
        'id': item.id,
        'item_type': item.item_type,
        'title': item.title,
        'customer_name': item.customer_name,
        'talking_points': item.talking_points,
        'status': item.status,
        'discussed_at': item.discussed_at.isoformat() if item.discussed_at else None,
    }


@one_on_one_bp.route('/one-on-one/')
def workspace_hub():
    """Render the unsurfaced hub for all persistent one-on-one workspaces."""
    workspaces = (
        OneOnOneWorkspace.query
        .options(
            joinedload(OneOnOneWorkspace.seller),
            joinedload(OneOnOneWorkspace.agenda_items),
        )
        .order_by(OneOnOneWorkspace.updated_at.desc())
        .all()
    )
    linked_seller_ids = {
        workspace.seller_id for workspace in workspaces if workspace.seller_id
    }
    available_sellers = Seller.query.filter(
        ~Seller.id.in_(linked_seller_ids)
    ).order_by(Seller.name).all()
    active_counts = {
        workspace.id: sum(
            item.status == 'active' for item in workspace.agenda_items
        )
        for workspace in workspaces
    }
    discussed_counts = {
        workspace.id: sum(
            item.status == 'discussed' for item in workspace.agenda_items
        )
        for workspace in workspaces
    }
    return render_template(
        'one_on_one_hub.html',
        workspaces=workspaces,
        available_sellers=available_sellers,
        active_counts=active_counts,
        discussed_counts=discussed_counts,
    )


@one_on_one_bp.route('/seller/<int:seller_id>/one-on-one')
def seller_workspace(seller_id: int):
    """Open the persistent one-on-one workspace for a seller."""
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return 'Seller not found', 404
    workspace = get_or_create_seller_workspace(seller)
    db.session.commit()
    return redirect(url_for('one_on_one.workspace_view', workspace_id=workspace.id))


@one_on_one_bp.route('/one-on-one/new', methods=['POST'])
def workspace_create():
    """Create a standalone manager or other-person workspace."""
    person_name = (request.form.get('person_name') or '').strip()
    person_type = (request.form.get('person_type') or 'Manager').strip()
    if not person_name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    if person_type not in _PERSON_TYPES - {'Seller'}:
        return jsonify({'success': False, 'error': 'Invalid person type'}), 400

    workspace = OneOnOneWorkspace(
        person_name=person_name,
        person_type=person_type,
    )
    db.session.add(workspace)
    db.session.commit()
    return redirect(url_for('one_on_one.workspace_view', workspace_id=workspace.id))


@one_on_one_bp.route('/one-on-one/<int:workspace_id>')
def workspace_view(workspace_id: int):
    """Render one person's persistent notes and agenda."""
    workspace = (
        OneOnOneWorkspace.query
        .options(
            joinedload(OneOnOneWorkspace.seller),
            joinedload(OneOnOneWorkspace.agenda_items)
            .joinedload(OneOnOneAgendaItem.milestone)
            .joinedload(Milestone.customer),
            joinedload(OneOnOneWorkspace.agenda_items)
            .joinedload(OneOnOneAgendaItem.engagement)
            .joinedload(Engagement.customer),
        )
        .filter_by(id=workspace_id)
        .first_or_404()
    )
    active_items = [item for item in workspace.agenda_items if item.status == 'active']
    discussed_items = sorted(
        (item for item in workspace.agenda_items if item.status == 'discussed'),
        key=lambda item: item.discussed_at or item.updated_at,
        reverse=True,
    )
    return render_template(
        'one_on_one_workspace.html',
        workspace=workspace,
        active_items=active_items,
        discussed_items=discussed_items,
    )


@one_on_one_bp.route('/api/one-on-one/<int:workspace_id>', methods=['PATCH'])
def workspace_update(workspace_id: int):
    """Autosave persistent workspace notes."""
    workspace = db.session.get(OneOnOneWorkspace, workspace_id)
    if not workspace:
        return jsonify({'success': False, 'error': 'Workspace not found'}), 404
    data = request.get_json(silent=True) or {}
    workspace.notes = str(data.get('notes') or '')
    workspace.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'success': True, 'updated_at': workspace.updated_at.isoformat()})


@one_on_one_bp.route('/api/one-on-one/<int:workspace_id>/candidates')
def workspace_candidates(workspace_id: int):
    """Search milestones or engagements available to add to an agenda."""
    workspace = db.session.get(OneOnOneWorkspace, workspace_id)
    if not workspace:
        return jsonify({'success': False, 'error': 'Workspace not found'}), 404

    item_type = (request.args.get('type') or 'milestone').strip().lower()
    search = (request.args.get('q') or '').strip()
    if item_type not in _ITEM_TYPES:
        return jsonify({'success': False, 'error': 'Invalid item type'}), 400

    active_items = OneOnOneAgendaItem.query.filter_by(
        workspace_id=workspace.id,
        item_type=item_type,
        status='active',
    ).all()
    active_ids = {
        item.milestone_id if item_type == 'milestone' else item.engagement_id
        for item in active_items
    }

    if item_type == 'milestone':
        query = (
            Milestone.query
            .join(Customer, Milestone.customer_id == Customer.id)
            .filter(Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']))
            .options(joinedload(Milestone.customer))
        )
        if workspace.seller_id:
            query = query.filter(Customer.seller_id == workspace.seller_id)
        if search:
            pattern = f'%{search}%'
            query = query.filter(or_(
                Milestone.title.ilike(pattern),
                Milestone.workload.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.nickname.ilike(pattern),
            ))
        entities = query.order_by(
            Milestone.on_my_team.desc(),
            Milestone.monthly_usage.desc(),
            Customer.name,
            Milestone.title,
        ).limit(75).all()
        results = [
            {
                'id': entity.id,
                'title': entity.display_text,
                'customer_name': entity.customer.get_display_name(),
                'status': entity.msx_status or '',
                'acr': entity.monthly_usage,
                'due_date': entity.due_date.date().isoformat() if entity.due_date else None,
                'detail': entity.workload or entity.milestone_number or '',
                'on_my_team': entity.on_my_team,
            }
            for entity in entities
            if entity.id not in active_ids
        ]
    else:
        query = (
            Engagement.query
            .join(Customer, Engagement.customer_id == Customer.id)
            .filter(Engagement.status.in_(['Active', 'On Hold']))
            .options(joinedload(Engagement.customer))
        )
        if workspace.seller_id:
            query = query.filter(Customer.seller_id == workspace.seller_id)
        if search:
            pattern = f'%{search}%'
            query = query.filter(or_(
                Engagement.title.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.nickname.ilike(pattern),
            ))
        entities = query.order_by(
            Engagement.estimated_acr.desc(),
            Customer.name,
            Engagement.title,
        ).limit(75).all()
        results = [
            {
                'id': entity.id,
                'title': entity.title,
                'customer_name': entity.customer.get_display_name(),
                'status': entity.status,
                'acr': entity.estimated_acr,
                'due_date': entity.target_date.isoformat() if entity.target_date else None,
                'detail': '',
                'on_my_team': None,
            }
            for entity in entities
            if entity.id not in active_ids
        ]

    return jsonify({'success': True, 'results': results})


@one_on_one_bp.route('/api/one-on-one/<int:workspace_id>/agenda', methods=['POST'])
def agenda_add(workspace_id: int):
    """Add or restore a milestone or engagement on a workspace agenda."""
    workspace = db.session.get(OneOnOneWorkspace, workspace_id)
    if not workspace:
        return jsonify({'success': False, 'error': 'Workspace not found'}), 404
    data = request.get_json(silent=True) or {}
    item_type = str(data.get('item_type') or '').strip().lower()
    try:
        entity_id = int(data.get('entity_id') or 0)
    except (TypeError, ValueError):
        entity_id = 0
    if item_type not in _ITEM_TYPES or not entity_id:
        return jsonify({'success': False, 'error': 'Valid item type and ID are required'}), 400

    entity = _entity_for_item(item_type, entity_id)
    if not entity:
        return jsonify({'success': False, 'error': 'Agenda item not found'}), 404
    if not _entity_allowed(workspace, entity):
        return jsonify({'success': False, 'error': 'Item is outside this workspace scope'}), 400

    item, outcome = add_or_restore_agenda_entity(workspace, item_type, entity)
    if outcome == 'already_active':
        return jsonify({'success': False, 'error': 'Item is already on the agenda'}), 409
    db.session.commit()
    return jsonify({'success': True, 'item': _agenda_item_payload(item)})


@one_on_one_bp.route(
    '/api/seller/<int:seller_id>/one-on-one/u2c',
    methods=['POST'],
)
def agenda_add_u2c_context(seller_id: int):
    """Add a U2C milestone's engagement context to a seller 1:1 agenda."""
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return jsonify({'success': False, 'error': 'Seller not found'}), 404

    data = request.get_json(silent=True) or {}
    try:
        milestone_id = int(data.get('milestone_id') or 0)
    except (TypeError, ValueError):
        milestone_id = 0
    talking_points = str(data.get('talking_points') or '').strip()
    if not milestone_id:
        return jsonify({'success': False, 'error': 'Milestone ID is required'}), 400
    if not talking_points:
        return jsonify({'success': False, 'error': 'A discussion note is required'}), 400

    milestone = db.session.get(Milestone, milestone_id)
    if not milestone:
        return jsonify({'success': False, 'error': 'Milestone not found'}), 404

    try:
        workspace, results = add_milestone_context_to_seller_agenda(
            seller,
            milestone,
            talking_points,
        )
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception(
            "Failed to add U2C milestone %s to seller %s's 1:1",
            milestone_id,
            seller_id,
        )
        return jsonify({
            'success': False,
            'error': 'The 1:1 agenda could not be updated. Please try again.',
        }), 500
    outcomes = [outcome for _, outcome in results]
    return jsonify({
        'success': True,
        'workspace_url': url_for(
            'one_on_one.workspace_view',
            workspace_id=workspace.id,
        ),
        'target_count': len(results),
        'added_count': outcomes.count('added'),
        'restored_count': outcomes.count('restored'),
        'already_active_count': outcomes.count('already_active'),
        'items': [_agenda_item_payload(item) for item, _ in results],
    })


@one_on_one_bp.route('/api/one-on-one/agenda/<int:item_id>', methods=['PATCH'])
def agenda_update(item_id: int):
    """Update talking points or discussion status for an agenda item."""
    item = db.session.get(OneOnOneAgendaItem, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Agenda item not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'talking_points' in data:
        item.talking_points = str(data.get('talking_points') or '')
    if 'status' in data:
        status = str(data.get('status') or '')
        if status not in _ITEM_STATUSES:
            return jsonify({'success': False, 'error': 'Invalid status'}), 400
        item.status = status
        item.discussed_at = (
            datetime.now(timezone.utc) if status == 'discussed' else None
        )
    item.updated_at = datetime.now(timezone.utc)
    item.workspace.updated_at = item.updated_at
    db.session.commit()
    return jsonify({'success': True, 'item': _agenda_item_payload(item)})


@one_on_one_bp.route(
    '/api/one-on-one/agenda/<int:item_id>/engagement',
    methods=['POST'],
)
def agenda_create_engagement(item_id: int):
    """Turn a milestone agenda item into a linked active engagement."""
    item = db.session.get(OneOnOneAgendaItem, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Agenda item not found'}), 404
    if item.status != 'active' or item.item_type != 'milestone' or not item.milestone:
        return jsonify({
            'success': False,
            'error': 'Only active milestone agenda items can create engagements',
        }), 400

    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()
    if not title:
        return jsonify({'success': False, 'error': 'Engagement title is required'}), 400
    if not _entity_allowed(item.workspace, item.milestone):
        return jsonify({'success': False, 'error': 'Item is outside this workspace scope'}), 400

    engagement = Engagement(
        customer_id=item.milestone.customer_id,
        title=title,
        status='Active',
    )
    engagement.milestones.append(item.milestone)
    db.session.add(engagement)
    db.session.flush()

    item.item_type = 'engagement'
    item.engagement_id = engagement.id
    item.milestone_id = None
    item.title_snapshot = engagement.title
    item.updated_at = datetime.now(timezone.utc)
    item.workspace.updated_at = item.updated_at
    db.session.commit()
    return jsonify({
        'success': True,
        'item': _agenda_item_payload(item),
        'engagement_id': engagement.id,
    })


@one_on_one_bp.route(
    '/api/one-on-one/agenda/<int:item_id>/notes',
    methods=['POST'],
)
def agenda_create_note(item_id: int):
    """Save a 1:1 discussion note on an engagement agenda item."""
    item = db.session.get(OneOnOneAgendaItem, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Agenda item not found'}), 404
    if item.item_type != 'engagement' or not item.engagement:
        return jsonify({
            'success': False,
            'error': 'Discussion notes require an engagement agenda item',
        }), 400
    if not _entity_allowed(item.workspace, item.engagement):
        return jsonify({'success': False, 'error': 'Item is outside this workspace scope'}), 400

    data = request.get_json(silent=True) or {}
    content = str(data.get('content') or '').strip()
    if not content:
        return jsonify({'success': False, 'error': 'Note content is required'}), 400

    note = Note(
        customer_id=item.engagement.customer_id,
        call_date=datetime.now(),
        content=content,
    )
    db.session.add(note)
    note.engagements.append(item.engagement)
    note.milestones.extend(item.engagement.milestones)
    db.session.flush()
    track_note_on_milestones(note)
    db.session.commit()
    schedule_customer_backup(note.customer_id)
    return jsonify({
        'success': True,
        'note': {
            'id': note.id,
            'content': note.content,
            'call_date': note.call_date.isoformat(),
        },
    })


@one_on_one_bp.route('/api/one-on-one/agenda/<int:item_id>', methods=['DELETE'])
def agenda_delete(item_id: int):
    """Permanently remove an agenda item from a workspace."""
    item = db.session.get(OneOnOneAgendaItem, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Agenda item not found'}), 404
    workspace = item.workspace
    db.session.delete(item)
    workspace.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'success': True})
