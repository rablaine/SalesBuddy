"""Read services for persistent one-on-one workspaces."""

from app.models import OneOnOneWorkspace, db


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
