"""Shared live work search for agenda and Initiative Tracker item pickers."""

from sqlalchemy import or_
from sqlalchemy.orm import Query, joinedload

from app.models import Customer, Engagement, Milestone, Project

WORK_MODELS = {'milestone': Milestone, 'engagement': Engagement, 'project': Project}


def get_selectable_projects_query() -> Query[Project]:
    """Include user-facing project types, excluding the system's saved Copilot tasks."""
    return Project.query.filter(Project.project_type != 'copilot_saved')


def get_agenda_candidates(
    item_type: str,
    search: str = '',
    seller_id: int | None = None,
    excluded_ids: set[int] | None = None,
) -> list[dict]:
    """Find up to 75 candidates; internal projects are available in every status."""
    if item_type not in WORK_MODELS:
        raise ValueError('Invalid item type')
    if item_type == 'project':
        if seller_id is not None:
            raise ValueError('Internal projects are not scoped to a seller')
        query = get_selectable_projects_query()
        if excluded_ids:
            query = query.filter(~Project.id.in_([
                entity_id for entity_id in excluded_ids if entity_id is not None
            ]))
        if search:
            query = query.filter(or_(*(
                field.ilike(f'%{search}%')
                for field in (Project.title, Project.description, Project.project_type)
            )))
        return [
            candidate_payload(project, item_type)
            for project in query.order_by(Project.title, Project.id).limit(75).all()
        ]
    model = WORK_MODELS[item_type]
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


def candidate_payload(entity: Milestone | Engagement | Project, item_type: str) -> dict:
    """Serialize current details consistently for all supported work types."""
    if isinstance(entity, Project):
        return {
            'id': entity.id, 'title': entity.title,
            'customer_name': 'Internal project', 'customer_id': None,
            'status': entity.status, 'acr': None,
            'due_date': entity.due_date.isoformat() if entity.due_date else None,
            'detail': entity.project_type.replace('_', ' ').title(),
            'on_my_team': None, 'commitment': '',
        }
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
