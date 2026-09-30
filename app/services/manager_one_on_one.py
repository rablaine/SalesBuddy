"""Persistent initiatives and live work data for Initiative Tracker."""

from datetime import datetime, timezone

from sqlalchemy.orm import joinedload, selectinload

from app.models import (
    Customer, Engagement, ManagerInitiativeItem, ManagerInitiativeSection, Milestone,
    U2CSnapshot,
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
        selectinload(ManagerInitiativeSection.items).joinedload(ManagerInitiativeItem.project),
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
    entity = getattr(item, item.item_type)
    is_project = item.item_type == 'project'
    live = bool(entity and (is_project or entity.customer))
    seller = entity.customer.seller if live and not is_project else None
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


def get_manager_u2c_candidates(
    search: str = '', existing_ids: set[int] | None = None, workload_prefix: str = '',
) -> dict:
    """Offer this quarter's remaining snapshot milestones without changing the report."""
    from app.services.u2c_snapshot import current_fiscal_quarter, get_attainment

    quarter = current_fiscal_quarter()
    snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=quarter).first()
    if snapshot is None:
        return {
            'snapshot': None, 'fiscal_quarter': quarter, 'results': [],
            'workload_prefix': workload_prefix,
            'message': f'No {quarter} U2C snapshot is available. Open U2C to refresh it.',
        }
    attainment = get_attainment(snapshot.id)
    if not attainment['success']:
        raise ValueError(attainment['error'])
    rows = [
        row for row in attainment['remaining_items']
        if (
            not workload_prefix
            or (row['workload'] or '').startswith((workload_prefix + ':', workload_prefix + ' '))
        )
    ]
    ids = {row['milestone_id'] for row in rows if row['milestone_id'] is not None}
    milestones = {
        milestone.id: milestone for milestone in Milestone.query.options(
            joinedload(Milestone.customer),
        ).filter(Milestone.id.in_(ids)).all()
    }
    existing_ids = existing_ids or set()
    results = []
    seen = set()
    for row in rows:
        milestone = milestones.get(row['milestone_id'])
        available = bool(milestone and milestone.customer)
        if milestone is not None and available and milestone.id in seen:
            continue
        if milestone is not None and milestone.customer is not None:
            payload = candidate_payload(milestone, 'milestone')
        else:
            payload = {
                'id': None, 'title': row['milestone_title'],
                'customer_name': row['customer_name'], 'status': row['current_status'],
                'commitment': row['current_commitment'], 'acr': row['monthly_acr'],
                'due_date': row['due_date'][:10] if row['due_date'] else None,
                'detail': row['workload'] or row['milestone_number'] or '',
                'on_my_team': None,
            }
        haystack = ' '.join(str(value or '') for value in (
            payload['title'], payload['customer_name'], payload['detail'],
            row['customer_name'], row['milestone_title'], row['milestone_number'],
        )).casefold()
        if search.strip().casefold() not in haystack:
            continue
        if available:
            seen.add(payload['id'])
        added = available and payload['id'] in existing_ids
        results.append({
            **payload, 'available': available, 'already_added': added,
            'selectable': available and not added,
            'reason': 'Already added' if added else 'Not synced locally' if not available else '',
        })
    return {
        'snapshot': {
            'id': snapshot.id, 'fiscal_quarter': quarter,
            'snapshot_date': _utc_timestamp(snapshot.snapshot_date),
            'version_date': (
                snapshot.msxi_version_date.isoformat() if snapshot.msxi_version_date else None
            ),
        },
        'fiscal_quarter': quarter, 'results': results, 'message': '',
        'workload_prefix': workload_prefix,
    }
