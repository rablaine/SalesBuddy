"""Business logic for persistent one-on-one workspaces."""

from datetime import datetime, timezone

from sqlalchemy.orm import selectinload

from app.models import (
    Engagement,
    Milestone,
    OneOnOneAgendaItem,
    OneOnOneWorkspace,
    Seller,
    db,
)


def get_or_create_seller_workspace(seller: Seller) -> OneOnOneWorkspace:
    """Return the persistent workspace linked to a seller, creating it if needed."""
    workspace = OneOnOneWorkspace.query.filter_by(seller_id=seller.id).first()
    if workspace:
        if workspace.person_name != seller.name:
            workspace.person_name = seller.name
        return workspace

    workspace = OneOnOneWorkspace(
        seller_id=seller.id,
        person_name=seller.name,
        person_type='Seller',
    )
    db.session.add(workspace)
    db.session.flush()
    return workspace


def add_or_restore_agenda_entity(
    workspace: OneOnOneWorkspace,
    item_type: str,
    entity: Milestone | Engagement,
    talking_points: str | None = None,
) -> tuple[OneOnOneAgendaItem, str]:
    """Add or restore one linked entity without committing the transaction."""
    id_field = 'milestone_id' if item_type == 'milestone' else 'engagement_id'
    existing = OneOnOneAgendaItem.query.filter_by(
        workspace_id=workspace.id,
        item_type=item_type,
        **{id_field: entity.id},
    ).first()
    if existing and existing.status == 'active':
        return existing, 'already_active'

    now = datetime.now(timezone.utc)
    if existing:
        existing.status = 'active'
        existing.discussed_at = None
        existing.updated_at = now
        item = existing
        outcome = 'restored'
    else:
        customer = entity.customer
        title = entity.display_text if item_type == 'milestone' else entity.title
        item = OneOnOneAgendaItem(
            workspace=workspace,
            item_type=item_type,
            title_snapshot=title,
            customer_snapshot=customer.get_display_name(),
            milestone_id=entity.id if item_type == 'milestone' else None,
            engagement_id=entity.id if item_type == 'engagement' else None,
            sort_order=len(workspace.agenda_items),
        )
        db.session.add(item)
        outcome = 'added'

    if talking_points is not None:
        item.talking_points = talking_points
    workspace.updated_at = now
    return item, outcome


def add_milestone_context_to_seller_agenda(
    seller: Seller,
    milestone: Milestone,
    talking_points: str,
) -> tuple[OneOnOneWorkspace, list[tuple[OneOnOneAgendaItem, str]]]:
    """Queue all linked engagements, or the milestone itself, for a seller 1:1."""
    if not milestone.customer or milestone.customer.seller_id != seller.id:
        raise ValueError('Milestone is outside this seller workspace scope')

    engagements = sorted(milestone.engagements, key=lambda engagement: engagement.id)
    if any(
        not engagement.customer or engagement.customer.seller_id != seller.id
        for engagement in engagements
    ):
        raise ValueError('A linked engagement is outside this seller workspace scope')

    workspace = get_or_create_seller_workspace(seller)
    targets = (
        [('engagement', engagement) for engagement in engagements]
        if engagements
        else [('milestone', milestone)]
    )
    results = [
        add_or_restore_agenda_entity(
            workspace,
            item_type,
            entity,
            talking_points=talking_points,
        )
        for item_type, entity in targets
    ]
    return workspace, results


def add_agenda_state_to_milestone_rows(rows: list[dict]) -> None:
    """Mark report rows whose milestone context is already on the seller agenda."""
    seller_ids = {row.get('seller_id') for row in rows if row.get('seller_id')}
    workspaces = (
        OneOnOneWorkspace.query
        .options(selectinload(OneOnOneWorkspace.agenda_items))
        .filter(OneOnOneWorkspace.seller_id.in_(seller_ids))
        .all()
        if seller_ids
        else []
    )
    active_by_seller = {
        workspace.seller_id: {
            (
                item.item_type,
                item.milestone_id if item.item_type == 'milestone'
                else item.engagement_id,
            )
            for item in workspace.agenda_items
            if item.status == 'active'
        }
        for workspace in workspaces
    }

    for row in rows:
        engagement_ids = [
            engagement['id'] for engagement in row.get('engagements', [])
        ]
        targets = (
            [('engagement', engagement_id) for engagement_id in engagement_ids]
            if engagement_ids
            else [('milestone', row.get('milestone_id'))]
        )
        active_targets = active_by_seller.get(row.get('seller_id'), set())
        row['one_on_one_active'] = (
            bool(row.get('seller_id'))
            and all(target_id and (item_type, target_id) in active_targets
                    for item_type, target_id in targets)
        )


def get_one_on_one_workspace_summaries(include_discussed: bool = False) -> list[dict]:
    """Return workspace notes and agenda summaries ordered by recent activity."""
    workspaces = OneOnOneWorkspace.query.options(
        db.joinedload(OneOnOneWorkspace.seller),
        db.joinedload(OneOnOneWorkspace.agenda_items),
    ).order_by(OneOnOneWorkspace.updated_at.desc()).all()

    results = []
    for workspace in workspaces:
        agenda = [
            {
                'id': item.id,
                'type': item.item_type,
                'title': item.title,
                'customer': item.customer_name,
                'talking_points': item.talking_points,
                'status': item.status,
                'discussed_at': (
                    item.discussed_at.isoformat() if item.discussed_at else None
                ),
            }
            for item in workspace.agenda_items
            if include_discussed or item.status == 'active'
        ]
        results.append({
            'id': workspace.id,
            'person_name': workspace.person_name,
            'person_type': workspace.person_type,
            'seller_id': workspace.seller_id,
            'notes': workspace.notes,
            'agenda': agenda,
            'updated_at': workspace.updated_at.isoformat(),
        })
    return results
