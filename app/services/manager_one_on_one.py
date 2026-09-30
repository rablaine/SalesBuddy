"""Persistent initiative sections and live report data for manager conversations."""

from datetime import datetime, timezone

from sqlalchemy.orm import selectinload

from app.models import (
    Customer, Engagement, ManagerInitiativeItem, ManagerInitiativeSection, Milestone,
)
from app.services.agenda_candidates import candidate_payload


def get_manager_one_on_one_report() -> dict:
    """Return all sections and current linked details, including unavailable records."""
    sections = ManagerInitiativeSection.query.options(
        selectinload(ManagerInitiativeSection.items)
        .joinedload(ManagerInitiativeItem.engagement).joinedload(Engagement.customer)
        .joinedload(Customer.seller),
        selectinload(ManagerInitiativeSection.items)
        .joinedload(ManagerInitiativeItem.milestone).joinedload(Milestone.customer)
        .joinedload(Customer.seller),
        selectinload(ManagerInitiativeSection.items)
        .selectinload(ManagerInitiativeItem.discussed_points),
    ).order_by(ManagerInitiativeSection.id).all()
    return {
        'sections': [
            get_manager_section_payload(section)
            for section in sections
        ],
    }


def get_manager_section_payload(section: ManagerInitiativeSection) -> dict:
    """Keep the flat item inventory and group the same items by their current seller."""
    items = [get_manager_item_payload(item) for item in section.items]
    groups = {}
    for item in items:
        group = groups.setdefault(item['seller_id'], {
            'seller_id': item['seller_id'],
            'seller_name': item['seller_name'],
            'items': [],
        })
        group['items'].append(item)
    seller_groups = sorted(groups.values(), key=lambda group: (
        group['seller_id'] is None,
        group['seller_name'].casefold(),
        group['seller_id'] or 0,
    ))
    return {
        'id': section.id,
        'name': section.name,
        'description': section.description,
        'items': items,
        'seller_groups': seller_groups,
    }


def get_manager_item_payload(item: ManagerInitiativeItem) -> dict:
    """Use live entity values and retain snapshots when a linked record is deleted."""
    entity = item.milestone if item.item_type == 'milestone' else item.engagement
    live = bool(entity and entity.customer)
    seller = entity.customer.seller if live else None
    payload = candidate_payload(entity, item.item_type) if live else {
        'id': None,
        'title': item.title_snapshot,
        'customer_name': item.customer_snapshot,
        'status': 'Record unavailable',
        'acr': None,
        'due_date': None,
        'commitment': '',
        'detail': '',
        'on_my_team': None,
    }
    return {
        **payload,
        'entity_id': payload['id'],
        'id': item.id,
        'item_type': item.item_type,
        'talking_points': item.talking_points,
        'points_created_at': _utc_timestamp(item.points_created_at),
        'discussed_points': [{
            'id': point.id,
            'text': point.text,
            'created_at': _utc_timestamp(point.created_at),
            'discussed_at': _utc_timestamp(point.discussed_at),
        } for point in item.discussed_points],
        'available': live,
        'seller_id': seller.id if seller else None,
        'seller_name': seller.name if seller else 'No seller',
    }


def _utc_timestamp(value: datetime | None) -> str | None:
    """Restore SQLite's UTC timestamps before sending them to the client."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
