"""Shared live milestone and engagement search for 1:1 item pickers."""

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.models import Customer, Engagement, Milestone


def get_agenda_candidates(
    item_type: str,
    search: str = '',
    seller_id: int | None = None,
    excluded_ids: set[int] | None = None,
) -> list[dict]:
    """Find up to 75 active candidates, preserving the seller agenda ordering."""
    if item_type not in {'milestone', 'engagement'}:
        raise ValueError('Invalid item type')
    model = Milestone if item_type == 'milestone' else Engagement
    query = model.query.join(Customer, model.customer_id == Customer.id).options(
        joinedload(model.customer)
    )
    if item_type == 'milestone':
        query = query.filter(Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']))
        ordering = (
            Milestone.on_my_team.desc(), Milestone.monthly_usage.desc(),
            Customer.name, Milestone.title,
        )
    else:
        query = query.filter(Engagement.status.in_(['Active', 'On Hold']))
        ordering = (Engagement.estimated_acr.desc(), Customer.name, Engagement.title)
    if seller_id:
        query = query.filter(Customer.seller_id == seller_id)
    if excluded_ids:
        query = query.filter(~model.id.in_([
            entity_id for entity_id in excluded_ids if entity_id is not None
        ]))
    if search:
        fields = [model.title, Customer.name, Customer.nickname]
        if item_type == 'milestone':
            fields.append(Milestone.workload)
        query = query.filter(or_(*(field.ilike(f'%{search}%') for field in fields)))

    return [candidate_payload(entity, item_type) for entity in query.order_by(
        *ordering
    ).limit(75).all()]


def candidate_payload(entity: Milestone | Engagement, item_type: str) -> dict:
    """Serialize current details consistently for both 1:1 report pickers."""
    is_milestone = item_type == 'milestone'
    due = entity.due_date if is_milestone else entity.target_date
    return {
        'id': entity.id,
        'title': entity.display_text if is_milestone else entity.title,
        'customer_name': entity.customer.get_display_name(),
        'customer_id': entity.customer_id,
        'status': (entity.msx_status or '') if is_milestone else entity.status,
        'acr': entity.monthly_usage if is_milestone else entity.estimated_acr,
        'due_date': (due.date().isoformat() if is_milestone else due.isoformat()) if due else None,
        'detail': (entity.workload or entity.milestone_number or '') if is_milestone else '',
        'on_my_team': entity.on_my_team if is_milestone else None,
        'commitment': (entity.customer_commitment or '') if is_milestone else '',
    }
