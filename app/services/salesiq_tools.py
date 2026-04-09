"""Shared tool registry for SalesIQ chat panel and MCP server.

Tools are registered via the @tool decorator. Each tool is a thin wrapper
over existing service/query code - never duplicate business logic here.

Consumers:
    - Chat endpoint (Phase 2): get_openai_tools() + execute_tool()
    - MCP server (Phase 5): get_mcp_tools() + execute_tool()
"""
from typing import Any

_BASE = 'http://localhost:5151'

_ENTITY_PATHS = {
    'milestone': '/milestone/',
    'customer': '/customer/',
    'engagement': '/engagement/',
    'opportunity': '/opportunity/',
    'note': '/note/',
    'seller': '/seller/',
    'partner': '/partners/',
    'territory': '/territory/',
    'pod': '/pod/',
}


def _clean(text: str | None) -> str | None:
    """Sanitize text for safe markdown rendering (escapes pipe chars)."""
    if text is None:
        return None
    return text.replace('|', '-')


def _url(entity: str, id: int) -> str:
    """Build a Sales Buddy app URL for an entity."""
    return f'{_BASE}{_ENTITY_PATHS[entity]}{id}'


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOLS: list[dict] = []


def tool(name: str, description: str, parameters: dict):
    """Register a function as a SalesIQ/MCP tool.

    Args:
        name: Unique tool name (snake_case).
        description: One-line description for the LLM.
        parameters: JSON Schema object describing accepted parameters.
    """
    def decorator(func):
        TOOLS.append({
            'name': name,
            'description': description,
            'parameters': parameters,
            'handler': func,
        })
        return func
    return decorator


def get_openai_tools() -> list[dict]:
    """Convert registry to OpenAI function-calling format."""
    return [
        {
            'type': 'function',
            'function': {
                'name': t['name'],
                'description': t['description'],
                'parameters': t['parameters'],
            },
        }
        for t in TOOLS
    ]


def get_mcp_tools() -> list[dict]:
    """Convert registry to MCP tool format."""
    return [
        {
            'name': t['name'],
            'description': t['description'],
            'inputSchema': t['parameters'],
        }
        for t in TOOLS
    ]


def execute_tool(name: str, params: dict) -> Any:
    """Dispatch a tool call by name.

    Args:
        name: The tool name to invoke.
        params: Dict of parameters matching the tool's schema.

    Returns:
        The tool handler's return value (should be JSON-serializable).

    Raises:
        ValueError: If the tool name is not registered.
    """
    for t in TOOLS:
        if t['name'] == name:
            return t['handler'](**params)
    raise ValueError(f'Unknown tool: {name}')


# ============================================================================
# Entity tools
# ============================================================================

# -- Customers ---------------------------------------------------------------

@tool(
    'search_customers',
    'Search customers by name, territory, seller, or vertical. '
    'Returns total_count (before limit) and paginated results.',
    {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Name or partial name to search for.',
            },
            'seller_id': {
                'type': 'integer',
                'description': 'Filter to a specific seller.',
            },
            'territory_id': {
                'type': 'integer',
                'description': 'Filter to a specific territory.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def search_customers(
    query: str = '',
    seller_id: int | None = None,
    territory_id: int | None = None,
    limit: int = 20,
) -> dict:
    """Search customers by name with optional seller/territory filters."""
    from app.models import Customer

    q = Customer.query
    if query:
        q = q.filter(Customer.name.ilike(f'%{query}%'))
    if seller_id:
        q = q.filter(Customer.seller_id == seller_id)
    if territory_id:
        q = q.filter(Customer.territory_id == territory_id)
    total = q.count()
    customers = q.order_by(Customer.name).limit(limit).all()
    return {
        'total_count': total,
        'results': [
            {
                'id': c.id,
                'url': _url('customer', c.id),
                'name': c.name,
                'nickname': c.nickname,
                'tpid': c.tpid,
                'seller': c.seller.name if c.seller else None,
                'territory': c.territory.name if c.territory else None,
            }
            for c in customers
        ],
    }


@tool(
    'get_customer_summary',
    'Get an activity summary for a customer including engagements, '
    'milestones, recent notes, and contacts.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Customer ID.',
            },
        },
        'required': ['customer_id'],
    },
)
def get_customer_summary(customer_id: int) -> dict:
    """Return a summary of a customer's activity."""
    from app.models import db, Customer, Engagement, Milestone, Note

    customer = db.session.get(Customer, customer_id)
    if not customer:
        return {'error': f'Customer {customer_id} not found.'}

    engagements = (
        Engagement.query
        .filter_by(customer_id=customer_id)
        .order_by(Engagement.updated_at.desc())
        .all()
    )
    milestones = (
        Milestone.query
        .filter_by(customer_id=customer_id)
        .order_by(Milestone.due_date.desc().nullslast())
        .limit(10)
        .all()
    )
    recent_notes = (
        Note.query
        .filter_by(customer_id=customer_id)
        .order_by(Note.call_date.desc())
        .limit(5)
        .all()
    )

    return {
        'id': customer.id,
        'url': _url('customer', customer.id),
        'name': customer.name,
        'nickname': customer.nickname,
        'seller': customer.seller.name if customer.seller else None,
        'territory': customer.territory.name if customer.territory else None,
        'engagements': [
            {
                'id': e.id,
                'url': _url('engagement', e.id),
                'title': e.title,
                'status': e.status,
                'updated_at': e.updated_at.isoformat() if e.updated_at else None,
            }
            for e in engagements
        ],
        'milestones': [
            {
                'id': m.id,
                'url': _url('milestone', m.id),
                'title': _clean(m.display_text),
                'commitment': m.customer_commitment,
                'status': m.msx_status,
                'due_date': m.due_date.strftime('%Y-%m-%d') if m.due_date else None,
                'on_my_team': m.on_my_team,
            }
            for m in milestones
        ],
        'recent_notes': [
            {
                'id': n.id,
                'url': _url('note', n.id),
                'call_date': n.call_date.strftime('%Y-%m-%d') if n.call_date else None,
                'snippet': (n.content or '')[:200],
            }
            for n in recent_notes
        ],
    }


# -- Portfolio overview ------------------------------------------------------

@tool(
    'get_portfolio_overview',
    'Get a high-level summary of the entire portfolio: customer count, '
    'active engagements, open milestones, seller count, and recent note volume.',
    {
        'type': 'object',
        'properties': {},
    },
)
def get_portfolio_overview() -> dict:
    """Return aggregate counts across the whole database."""
    from datetime import datetime, timedelta, timezone
    from app.models import Customer, Engagement, Milestone, Seller, Note, Partner, ActionItem

    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    return {
        'customers': Customer.query.count(),
        'sellers': Seller.query.count(),
        'partners': Partner.query.count(),
        'active_engagements': Engagement.query.filter_by(status='Active').count(),
        'open_milestones': Milestone.query.filter(
            Milestone.on_my_team.is_(True),
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
        ).count(),
        'notes_last_30d': Note.query.filter(Note.call_date >= cutoff_30d).count(),
        'open_action_items': ActionItem.query.filter_by(status='open').count(),
    }


# -- Sellers -----------------------------------------------------------------

@tool(
    'list_sellers',
    'List all sellers (DSSs) with their type, territories, and customer counts. '
    'Optionally filter by territory.',
    {
        'type': 'object',
        'properties': {
            'territory_id': {
                'type': 'integer',
                'description': 'Filter to sellers covering a specific territory.',
            },
        },
    },
)
def list_sellers(territory_id: int | None = None) -> list[dict]:
    """Return all sellers with summary stats."""
    from app.models import Seller, Customer, Engagement

    q = Seller.query
    if territory_id:
        q = q.filter(Seller.territories.any(id=territory_id))
    sellers = q.order_by(Seller.name).all()

    results = []
    for s in sellers:
        cust_count = Customer.query.filter_by(seller_id=s.id).count()
        active_eng = (
            Engagement.query
            .join(Customer, Engagement.customer_id == Customer.id)
            .filter(Customer.seller_id == s.id, Engagement.status == 'Active')
            .count()
        )
        results.append({
            'id': s.id,
            'url': _url('seller', s.id),
            'name': s.name,
            'alias': s.alias,
            'seller_type': s.seller_type,
            'territories': [t.name for t in s.territories],
            'customer_count': cust_count,
            'active_engagements': active_eng,
        })
    return results


# -- Notes -------------------------------------------------------------------

@tool(
    'search_notes',
    'Search notes by keyword, customer, topic, or date range. '
    'All filters are optional - call with just limit to get most recent notes, '
    'or with days to get notes from a recent time window.',
    {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Full-text keyword search.',
            },
            'customer_id': {
                'type': 'integer',
                'description': 'Filter to a specific customer.',
            },
            'topic_id': {
                'type': 'integer',
                'description': 'Filter to notes tagged with this topic.',
            },
            'days': {
                'type': 'integer',
                'description': 'Limit to notes from the last N days.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def search_notes(
    query: str = '',
    customer_id: int | None = None,
    topic_id: int | None = None,
    days: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search call notes with optional filters."""
    from datetime import datetime, timedelta, timezone
    from app.models import Note

    q = Note.query
    if query:
        q = q.filter(Note.content.ilike(f'%{query}%'))
    if customer_id:
        q = q.filter(Note.customer_id == customer_id)
    if topic_id:
        q = q.filter(Note.topics.any(id=topic_id))
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = q.filter(Note.call_date >= cutoff)
    notes = q.order_by(Note.call_date.desc()).limit(limit).all()
    return [
        {
            'id': n.id,
            'url': _url('note', n.id),
            'customer': n.customer.name if n.customer else None,
            'call_date': n.call_date.strftime('%Y-%m-%d') if n.call_date else None,
            'snippet': (n.content or '')[:200],
            'topics': [t.name for t in n.topics],
        }
        for n in notes
    ]


# -- Engagements ------------------------------------------------------------

@tool(
    'get_engagement_details',
    'Get engagement info including status, story fields, action items, and linked notes.',
    {
        'type': 'object',
        'properties': {
            'engagement_id': {
                'type': 'integer',
                'description': 'Engagement ID.',
            },
        },
        'required': ['engagement_id'],
    },
)
def get_engagement_details(engagement_id: int) -> dict:
    """Return details for a single engagement."""
    from app.models import db, Engagement

    eng = db.session.get(Engagement, engagement_id)
    if not eng:
        return {'error': f'Engagement {engagement_id} not found.'}

    return {
        'id': eng.id,
        'url': _url('engagement', eng.id),
        'title': eng.title,
        'status': eng.status,
        'customer': eng.customer.name if eng.customer else None,
        'customer_id': eng.customer_id,
        'scenario': eng.scenario,
        'problem_statement': eng.problem_statement,
        'desired_outcome': eng.desired_outcome,
        'proposed_solution': eng.proposed_solution,
        'action_items': [
            {
                'id': ai.id,
                'description': ai.description,
                'status': ai.status,
                'due_date': ai.due_date.strftime('%Y-%m-%d') if ai.due_date else None,
            }
            for ai in eng.action_items
        ],
        'note_count': len(eng.notes),
    }


@tool(
    'search_engagements',
    'Search engagements with optional filters for customer, seller, status, '
    'or keyword. Returns engagement summary with customer and action item counts.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Filter to engagements for a specific customer.',
            },
            'seller_id': {
                'type': 'integer',
                'description': 'Filter to engagements for customers of a specific seller.',
            },
            'status': {
                'type': 'string',
                'description': 'Filter by status: Active, On Hold, Won, or Lost.',
            },
            'query': {
                'type': 'string',
                'description': 'Keyword search in engagement title.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def search_engagements(
    customer_id: int | None = None,
    seller_id: int | None = None,
    status: str | None = None,
    query: str = '',
    limit: int = 20,
) -> list[dict]:
    """Search engagements with optional filters."""
    from app.models import Engagement, Customer

    q = Engagement.query
    if customer_id:
        q = q.filter(Engagement.customer_id == customer_id)
    if seller_id:
        q = q.join(Customer, Engagement.customer_id == Customer.id).filter(
            Customer.seller_id == seller_id,
        )
    if status:
        q = q.filter(Engagement.status == status)
    if query:
        q = q.filter(Engagement.title.ilike(f'%{query}%'))

    engagements = q.order_by(Engagement.updated_at.desc()).limit(limit).all()
    return [
        {
            'id': e.id,
            'url': _url('engagement', e.id),
            'title': e.title,
            'status': e.status,
            'customer': e.customer.name if e.customer else None,
            'customer_id': e.customer_id,
            'estimated_acr': e.estimated_acr,
            'target_date': e.target_date.strftime('%Y-%m-%d') if e.target_date else None,
            'open_action_items': sum(
                1 for ai in e.action_items if ai.status == 'open'
            ),
            'note_count': len(e.notes),
            'updated_at': e.updated_at.isoformat() if e.updated_at else None,
        }
        for e in engagements
    ]


# -- Milestones --------------------------------------------------------------

@tool(
    'get_milestone_status',
    'Get milestone details or list milestones filtered by commitment, status, customer, or team. '
    'Milestones have TWO status fields: commitment (Committed/Uncommitted - the primary filter) '
    'and status (On Track/At Risk/Blocked - the MSX execution status). '
    '"My milestones" means on_my_team=true. "Uncommitted milestones" means commitment="Uncommitted". '
    'Use sort_by="value" to find largest milestones (sorts by estimated monthly ACR descending).',
    {
        'type': 'object',
        'properties': {
            'milestone_id': {
                'type': 'integer',
                'description': 'Get a specific milestone by ID.',
            },
            'customer_id': {
                'type': 'integer',
                'description': 'Filter milestones to a customer.',
            },
            'commitment': {
                'type': 'string',
                'description': 'Filter by customer commitment (Committed, Uncommitted). '
                               'This is the primary filter most users care about.',
            },
            'status': {
                'type': 'string',
                'description': 'Filter by MSX execution status (On Track, At Risk, Blocked, etc.).',
            },
            'on_my_team': {
                'type': 'boolean',
                'description': 'Filter to milestones where user is on the team. '
                               'Use true for "my milestones".',
            },
            'sort_by': {
                'type': 'string',
                'description': 'Sort order: "due_date" (default) or "value" '
                               '(estimated monthly ACR descending).',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def get_milestone_status(
    milestone_id: int | None = None,
    customer_id: int | None = None,
    commitment: str | None = None,
    status: str | None = None,
    on_my_team: bool | None = None,
    sort_by: str = 'due_date',
    limit: int = 20,
) -> dict | list[dict]:
    """Get milestone details or search milestones."""
    from app.models import db, Milestone

    if milestone_id:
        ms = db.session.get(Milestone, milestone_id)
        if not ms:
            return {'error': f'Milestone {milestone_id} not found.'}
        return {
            'id': ms.id,
            'url': _url('milestone', ms.id),
            'title': _clean(ms.display_text),
            'commitment': ms.customer_commitment,
            'status': ms.msx_status,
            'customer': ms.customer.name if ms.customer else None,
            'due_date': ms.due_date.strftime('%Y-%m-%d') if ms.due_date else None,
            'estimated_monthly_acr': ms.monthly_usage,
            'on_my_team': ms.on_my_team,
            'workload': ms.workload,
            'msx_url': ms.url,
        }

    q = Milestone.query
    if customer_id:
        q = q.filter(Milestone.customer_id == customer_id)
    if commitment:
        q = q.filter(Milestone.customer_commitment == commitment)
    if status:
        q = q.filter(Milestone.msx_status == status)
    if on_my_team is not None:
        q = q.filter(Milestone.on_my_team == on_my_team)

    if sort_by == 'value':
        q = q.order_by(Milestone.monthly_usage.desc().nullslast())
    else:
        q = q.order_by(Milestone.due_date.asc().nullslast())

    milestones = q.limit(limit).all()
    return [
        {
            'id': m.id,
            'url': _url('milestone', m.id),
            'title': _clean(m.display_text),
            'commitment': m.customer_commitment,
            'status': m.msx_status,
            'customer': m.customer.name if m.customer else None,
            'due_date': m.due_date.strftime('%Y-%m-%d') if m.due_date else None,
            'estimated_monthly_acr': m.monthly_usage,
            'on_my_team': m.on_my_team,
        }
        for m in milestones
    ]


# -- Sellers -----------------------------------------------------------------

@tool(
    'get_seller_workload',
    "Get a seller's customers, open engagement count, and milestone summary.",
    {
        'type': 'object',
        'properties': {
            'seller_id': {
                'type': 'integer',
                'description': 'Seller ID.',
            },
        },
        'required': ['seller_id'],
    },
)
def get_seller_workload(seller_id: int) -> dict:
    """Return workload summary for a seller."""
    from app.models import db, Seller, Engagement, Milestone

    seller = db.session.get(Seller, seller_id)
    if not seller:
        return {'error': f'Seller {seller_id} not found.'}

    customers = seller.customers
    customer_ids = [c.id for c in customers]

    open_engagements = (
        Engagement.query
        .filter(
            Engagement.customer_id.in_(customer_ids),
            Engagement.status == 'Active',
        )
        .count()
    ) if customer_ids else 0

    active_milestones = (
        Milestone.query
        .filter(
            Milestone.customer_id.in_(customer_ids),
            Milestone.on_my_team == True,
        )
        .count()
    ) if customer_ids else 0

    return {
        'id': seller.id,
        'url': _url('seller', seller.id),
        'name': seller.name,
        'seller_type': seller.seller_type,
        'customer_count': len(customers),
        'open_engagements': open_engagements,
        'active_milestones': active_milestones,
        'territories': [t.name for t in seller.territories],
    }


# -- Opportunities -----------------------------------------------------------

@tool(
    'get_opportunity_details',
    'Get opportunity details including linked milestones and engagements.',
    {
        'type': 'object',
        'properties': {
            'opportunity_id': {
                'type': 'integer',
                'description': 'Opportunity ID.',
            },
        },
        'required': ['opportunity_id'],
    },
)
def get_opportunity_details(opportunity_id: int) -> dict:
    """Return details for a single opportunity."""
    from app.models import db, Opportunity

    opp = db.session.get(Opportunity, opportunity_id)
    if not opp:
        return {'error': f'Opportunity {opportunity_id} not found.'}

    return {
        'id': opp.id,
        'url': _url('opportunity', opp.id),
        'name': opp.name,
        'customer': opp.customer.name if opp.customer else None,
        'msx_url': opp.msx_url,
        'milestones': [
            {'id': m.id, 'url': _url('milestone', m.id), 'title': _clean(m.display_text), 'commitment': m.customer_commitment, 'status': m.msx_status}
            for m in opp.milestones
        ],
    }


# -- Partners ----------------------------------------------------------------

@tool(
    'search_partners',
    'Search partner organizations by name or specialty.',
    {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Partner name search.',
            },
            'specialty': {
                'type': 'string',
                'description': 'Filter to partners with this specialty.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def search_partners(
    query: str = '',
    specialty: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search partners by name or specialty."""
    from app.models import Partner, Specialty

    q = Partner.query
    if query:
        q = q.filter(Partner.name.ilike(f'%{query}%'))
    if specialty:
        q = q.filter(Partner.specialties.any(Specialty.name.ilike(f'%{specialty}%')))
    partners = q.order_by(Partner.name).limit(limit).all()
    return [
        {
            'id': p.id,
            'url': _url('partner', p.id),
            'name': p.name,
            'specialties': [s.name for s in p.specialties],
        }
        for p in partners
    ]


# -- Action Items ------------------------------------------------------------

@tool(
    'list_action_items',
    'List open action items, optionally filtered by engagement or project.',
    {
        'type': 'object',
        'properties': {
            'engagement_id': {
                'type': 'integer',
                'description': 'Filter to a specific engagement.',
            },
            'status': {
                'type': 'string',
                'description': 'Filter by status (open, completed, cancelled).',
            },
            'overdue_only': {
                'type': 'boolean',
                'description': 'Only return overdue items.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 30).',
            },
        },
    },
)
def list_action_items(
    engagement_id: int | None = None,
    status: str | None = None,
    overdue_only: bool = False,
    limit: int = 30,
) -> list[dict]:
    """List action items with optional filters."""
    from datetime import date
    from app.models import ActionItem

    q = ActionItem.query
    if engagement_id:
        q = q.filter(ActionItem.engagement_id == engagement_id)
    if status:
        q = q.filter(ActionItem.status == status)
    if overdue_only:
        q = q.filter(
            ActionItem.status == 'open',
            ActionItem.due_date < date.today(),
        )
    items = q.order_by(ActionItem.due_date.asc().nullslast()).limit(limit).all()
    return [
        {
            'id': ai.id,
            'description': ai.description,
            'status': ai.status,
            'priority': ai.priority,
            'due_date': ai.due_date.strftime('%Y-%m-%d') if ai.due_date else None,
            'engagement': ai.engagement.title if ai.engagement else None,
            'engagement_url': _url('engagement', ai.engagement_id) if ai.engagement_id else None,
            'customer': (
                ai.engagement.customer.name
                if ai.engagement and ai.engagement.customer
                else None
            ),
        }
        for ai in items
    ]


# ============================================================================
# Report tools
# ============================================================================

@tool(
    'report_hygiene',
    'Get data hygiene gaps: engagements missing milestones and milestones missing engagements.',
    {
        'type': 'object',
        'properties': {
            'seller_id': {
                'type': 'integer',
                'description': 'Scope to a specific seller.',
            },
        },
    },
)
def report_hygiene(seller_id: int | None = None) -> dict:
    """Return hygiene report data."""
    from app.models import Engagement, Milestone

    eng_q = (
        Engagement.query
        .filter(Engagement.status == 'Active')
        .filter(~Engagement.milestones.any())
    )
    ms_q = (
        Milestone.query
        .filter(Milestone.on_my_team == True)
        .filter(~Milestone.engagements.any())
    )
    if seller_id:
        from app.models import Customer
        cust_ids = [
            c.id for c in Customer.query.filter_by(seller_id=seller_id).all()
        ]
        eng_q = eng_q.filter(Engagement.customer_id.in_(cust_ids))
        ms_q = ms_q.filter(Milestone.customer_id.in_(cust_ids))

    return {
        'engagements_without_milestones': [
            {
                'id': e.id,
                'url': _url('engagement', e.id),
                'title': e.title,
                'customer': e.customer.name if e.customer else None,
            }
            for e in eng_q.all()
        ],
        'milestones_without_engagements': [
            {
                'id': m.id,
                'url': _url('milestone', m.id),
                'title': _clean(m.display_text),
                'customer': m.customer.name if m.customer else None,
                'commitment': m.customer_commitment,
                'status': m.msx_status,
            }
            for m in ms_q.all()
        ],
    }


@tool(
    'report_workload',
    'Get workload coverage - customers grouped by topic/workload with counts.',
    {
        'type': 'object',
        'properties': {
            'seller_id': {
                'type': 'integer',
                'description': 'Scope to a seller.',
            },
        },
    },
)
def report_workload(seller_id: int | None = None) -> dict:
    """Return workload summary counts."""
    from app.models import Customer, Engagement, Milestone

    cust_q = Customer.query
    if seller_id:
        cust_q = cust_q.filter_by(seller_id=seller_id)
    customers = cust_q.all()

    return {
        'customer_count': len(customers),
        'customers': [
            {
                'id': c.id,
                'url': _url('customer', c.id),
                'name': c.name,
                'engagement_count': Engagement.query.filter_by(
                    customer_id=c.id, status='Active'
                ).count(),
                'milestone_count': Milestone.query.filter(
                    Milestone.customer_id == c.id,
                    Milestone.on_my_team == True,
                ).count(),
            }
            for c in customers[:50]  # Cap to avoid huge responses
        ],
    }


@tool(
    'report_whats_new',
    'Get milestones created or updated in the last N days.',
    {
        'type': 'object',
        'properties': {
            'days': {
                'type': 'integer',
                'description': 'Lookback window in days (default 14, max 90).',
            },
        },
    },
)
def report_whats_new(days: int = 14) -> dict:
    """Return recently created/updated milestones."""
    from datetime import datetime, timedelta, timezone
    from app.models import db, Milestone

    days = max(1, min(days, 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    created = (
        Milestone.query
        .filter(
            db.or_(
                Milestone.msx_created_on >= cutoff,
                db.and_(
                    Milestone.msx_created_on.is_(None),
                    Milestone.created_at >= cutoff,
                ),
            )
        )
        .order_by(Milestone.created_at.desc())
        .limit(50)
        .all()
    )
    updated = (
        Milestone.query
        .filter(
            Milestone.msx_modified_on >= cutoff,
            db.or_(
                Milestone.msx_created_on < cutoff,
                Milestone.msx_created_on.is_(None),
            ),
        )
        .order_by(Milestone.msx_modified_on.desc())
        .limit(50)
        .all()
    )

    def _ms_dict(m):
        return {
            'id': m.id,
            'url': _url('milestone', m.id),
            'title': _clean(m.display_text),
            'customer': m.customer.name if m.customer else None,
            'commitment': m.customer_commitment,
            'status': m.msx_status,
            'on_my_team': m.on_my_team,
        }

    return {
        'days': days,
        'created': [_ms_dict(m) for m in created],
        'updated': [_ms_dict(m) for m in updated],
    }


@tool(
    'report_revenue_alerts',
    'Get revenue alerts - customers with declining revenue, dips, or expansion opportunities.',
    {
        'type': 'object',
        'properties': {
            'category': {
                'type': 'string',
                'description': 'Filter by category (CHURN_RISK, RECENT_DIP, '
                'EXPANSION_OPPORTUNITY, VOLATILE).',
            },
            'seller_name': {
                'type': 'string',
                'description': 'Filter to a specific seller by name.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def report_revenue_alerts(
    category: str | None = None,
    seller_name: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return revenue analysis alerts."""
    from app.models import RevenueAnalysis

    q = RevenueAnalysis.query.filter(
        RevenueAnalysis.review_status.in_(['new', 'to_be_reviewed'])
    )
    if category:
        q = q.filter(RevenueAnalysis.category == category)
    if seller_name:
        q = q.filter(RevenueAnalysis.seller_name == seller_name)
    alerts = (
        q.order_by(RevenueAnalysis.priority_score.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            'id': a.id,
            'customer_name': a.customer_name,
            'bucket': a.bucket,
            'category': a.category,
            'priority_score': a.priority_score,
            'dollars_at_risk': a.dollars_at_risk,
            'dollars_opportunity': a.dollars_opportunity,
            'engagement_rationale': a.engagement_rationale,
        }
        for a in alerts
    ]


@tool(
    'report_whitespace',
    'Get whitespace analysis - which customers are missing coverage in which '
    'technology buckets.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Check whitespace for a specific customer.',
            },
            'bucket': {
                'type': 'string',
                'description': 'Check which customers lack coverage in this bucket.',
            },
        },
    },
)
def report_whitespace(
    customer_id: int | None = None,
    bucket: str | None = None,
) -> dict:
    """Return whitespace gaps."""
    from app.models import CustomerRevenueData

    if customer_id:
        rows = (
            CustomerRevenueData.query
            .filter_by(customer_id=customer_id)
            .all()
        )
        buckets_with_spend = {r.bucket for r in rows if r.latest_month and r.latest_month > 0}
        return {
            'customer_id': customer_id,
            'buckets_with_spend': sorted(buckets_with_spend),
        }

    if bucket:
        customers_with = (
            CustomerRevenueData.query
            .filter_by(bucket=bucket)
            .filter(CustomerRevenueData.latest_month > 0)
            .all()
        )
        return {
            'bucket': bucket,
            'customers_with_spend': len(customers_with),
        }

    return {'error': 'Provide customer_id or bucket to query whitespace.'}


# ============================================================================
# Phase 4 tools — expanded entity & report coverage
# ============================================================================

# -- Milestone due_within_days enhancement -----------------------------------

@tool(
    'get_milestones_due_soon',
    'Get milestones due within a number of days. Useful for upcoming deadlines.',
    {
        'type': 'object',
        'properties': {
            'due_within_days': {
                'type': 'integer',
                'description': 'Return milestones due within this many days (default 30).',
            },
            'on_my_team': {
                'type': 'boolean',
                'description': 'Filter to milestones where user is on the team.',
            },
            'seller_id': {
                'type': 'integer',
                'description': 'Filter to a specific seller.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 30).',
            },
        },
    },
)
def get_milestones_due_soon(
    due_within_days: int = 30,
    on_my_team: bool | None = None,
    seller_id: int | None = None,
    limit: int = 30,
) -> list[dict]:
    """Get milestones with due dates within N days from today."""
    from datetime import date, timedelta
    from app.models import Milestone, Customer

    today = date.today()
    cutoff = today + timedelta(days=due_within_days)

    q = Milestone.query.filter(
        Milestone.due_date >= today,
        Milestone.due_date <= cutoff,
    )
    if on_my_team is not None:
        q = q.filter(Milestone.on_my_team == on_my_team)
    if seller_id:
        cust_ids = [c.id for c in Customer.query.filter_by(seller_id=seller_id).all()]
        q = q.filter(Milestone.customer_id.in_(cust_ids))

    milestones = q.order_by(Milestone.due_date.asc()).limit(limit).all()
    return [
        {
            'id': m.id,
            'url': _url('milestone', m.id),
            'title': _clean(m.display_text),
            'commitment': m.customer_commitment,
            'status': m.msx_status,
            'customer': m.customer.name if m.customer else None,
            'due_date': m.due_date.strftime('%Y-%m-%d') if m.due_date else None,
            'on_my_team': m.on_my_team,
            'workload': m.workload,
        }
        for m in milestones
    ]


# -- Territory summary -------------------------------------------------------

@tool(
    'get_territory_summary',
    'List all territories or get detailed info for one territory including '
    'customers, sellers, solution engineers, and recent activity. '
    'Call without territory_id to list all territories.',
    {
        'type': 'object',
        'properties': {
            'territory_id': {
                'type': 'integer',
                'description': 'Territory ID. Omit to list all territories.',
            },
        },
    },
)
def get_territory_summary(territory_id: int | None = None) -> dict | list:
    """Return summary data for a territory or list all territories."""
    from app.models import db, Territory, Note

    if territory_id is None:
        territories = Territory.query.order_by(Territory.name).all()
        return [
            {
                'id': t.id,
                'url': _url('territory', t.id),
                'name': t.name,
                'pod': t.pod.name if t.pod else None,
                'customer_count': len(t.customers),
                'seller_count': len(t.sellers),
            }
            for t in territories
        ]

    territory = db.session.get(Territory, territory_id)
    if not territory:
        return {'error': f'Territory {territory_id} not found.'}

    customers = territory.customers
    customer_ids = [c.id for c in customers]

    recent_note_count = 0
    if customer_ids:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent_note_count = (
            Note.query
            .filter(Note.customer_id.in_(customer_ids), Note.call_date >= cutoff)
            .count()
        )

    return {
        'id': territory.id,
        'url': _url('territory', territory.id),
        'name': territory.name,
        'pod': territory.pod.name if territory.pod else None,
        'customers': [
            {'id': c.id, 'url': _url('customer', c.id), 'name': c.name}
            for c in sorted(customers, key=lambda c: c.name)
        ],
        'sellers': [
            {'id': s.id, 'url': _url('seller', s.id), 'name': s.name, 'type': s.seller_type}
            for s in territory.sellers
        ],
        'solution_engineers': [
            {'id': se.id, 'name': se.name, 'specialty': se.specialty}
            for se in territory.solution_engineers
        ],
        'customer_count': len(customers),
        'notes_last_30_days': recent_note_count,
    }


# -- POD overview ------------------------------------------------------------

@tool(
    'get_pod_overview',
    'List all PODs or get detailed info for one POD including territories, '
    'sellers, and solution engineers. Call without pod_id to list all PODs.',
    {
        'type': 'object',
        'properties': {
            'pod_id': {
                'type': 'integer',
                'description': 'POD ID. Omit to list all PODs.',
            },
        },
    },
)
def get_pod_overview(pod_id: int | None = None) -> dict | list:
    """Return overview of one POD or list all PODs."""
    from app.models import db, POD

    if pod_id is None:
        pods = POD.query.order_by(POD.name).all()
        return [
            {
                'id': p.id,
                'url': _url('pod', p.id),
                'name': p.name,
                'territory_count': len(p.territories),
                'se_count': len(p.solution_engineers),
            }
            for p in pods
        ]

    pod = db.session.get(POD, pod_id)
    if not pod:
        return {'error': f'POD {pod_id} not found.'}

    territories = pod.territories
    all_sellers = set()
    total_customers = 0
    for t in territories:
        for s in t.sellers:
            all_sellers.add((s.id, s.name, s.seller_type))
        total_customers += len(t.customers)

    return {
        'id': pod.id,
        'url': _url('pod', pod.id),
        'name': pod.name,
        'territories': [
            {
                'id': t.id,
                'url': _url('territory', t.id),
                'name': t.name,
                'customer_count': len(t.customers),
            }
            for t in territories
        ],
        'sellers': [
            {'id': sid, 'url': _url('seller', sid), 'name': sname, 'type': stype}
            for sid, sname, stype in sorted(all_sellers, key=lambda x: x[1])
        ],
        'solution_engineers': [
            {'id': se.id, 'name': se.name, 'specialty': se.specialty}
            for se in pod.solution_engineers
        ],
        'total_customers': total_customers,
    }


# -- Analytics summary -------------------------------------------------------

@tool(
    'get_analytics_summary',
    'Get call volume metrics, top topics, customers needing attention, '
    'and seller activity.',
    {
        'type': 'object',
        'properties': {
            'days': {
                'type': 'integer',
                'description': 'Lookback window in days (default 30, max 90).',
            },
        },
    },
)
def get_analytics_summary(days: int = 30) -> dict:
    """Return analytics dashboard data."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, distinct
    from app.models import db, Note, Customer, Topic, notes_topics

    days = max(1, min(days, 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_notes = Note.query.count()
    recent_notes = Note.query.filter(Note.call_date >= cutoff).count()
    total_customers = Customer.query.count()
    active_customers = (
        db.session.query(func.count(distinct(Note.customer_id)))
        .filter(Note.call_date >= cutoff)
        .scalar()
    ) or 0

    # Top topics in the period
    top_topics = (
        db.session.query(Topic.name, func.count(Note.id).label('cnt'))
        .join(notes_topics, Topic.id == notes_topics.c.topic_id)
        .join(Note, Note.id == notes_topics.c.note_id)
        .filter(Note.call_date >= cutoff)
        .group_by(Topic.name)
        .order_by(func.count(Note.id).desc())
        .limit(10)
        .all()
    )

    # Customers not called in the period
    called_ids = (
        db.session.query(distinct(Note.customer_id))
        .filter(Note.call_date >= cutoff, Note.customer_id.isnot(None))
        .subquery()
    )
    neglected = (
        Customer.query
        .filter(~Customer.id.in_(db.session.query(called_ids)))
        .count()
    )

    return {
        'period_days': days,
        'total_notes': total_notes,
        'notes_in_period': recent_notes,
        'total_customers': total_customers,
        'active_customers': active_customers,
        'neglected_customers': neglected,
        'top_topics': [
            {'name': name, 'call_count': cnt} for name, cnt in top_topics
        ],
    }


# -- 1:1 report data --------------------------------------------------------

@tool(
    'report_one_on_one',
    'Get 1:1 manager prep data: recent customer activity, open engagements, '
    'and milestone updates in the last 2 weeks.',
    {
        'type': 'object',
        'properties': {
            'days': {
                'type': 'integer',
                'description': 'Lookback window in days (default 14).',
            },
            'seller_id': {
                'type': 'integer',
                'description': 'Scope to a specific seller.',
            },
        },
    },
)
def report_one_on_one(days: int = 14, seller_id: int | None = None) -> dict:
    """Return 1:1 prep data."""
    from datetime import datetime, timedelta, timezone
    from app.models import Note, Engagement, Milestone, Customer

    days = max(1, min(days, 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Recent notes
    note_q = Note.query.filter(Note.call_date >= cutoff)
    if seller_id:
        cust_ids = [c.id for c in Customer.query.filter_by(seller_id=seller_id).all()]
        note_q = note_q.filter(Note.customer_id.in_(cust_ids))
    recent_notes = note_q.order_by(Note.call_date.desc()).limit(50).all()

    # Group by customer
    customer_notes: dict[str, list] = {}
    for n in recent_notes:
        cname = n.customer.name if n.customer else 'No Customer'
        customer_notes.setdefault(cname, []).append({
            'id': n.id,
            'url': _url('note', n.id),
            'call_date': n.call_date.strftime('%Y-%m-%d') if n.call_date else None,
            'snippet': (n.content or '')[:150],
            'topics': [t.name for t in n.topics],
        })

    # Open engagements
    eng_q = Engagement.query.filter(Engagement.status == 'Active')
    if seller_id:
        eng_q = eng_q.filter(Engagement.customer_id.in_(cust_ids))
    open_engagements = eng_q.count()

    # Recently committed milestones
    ms_q = Milestone.query.filter(
        Milestone.on_my_team == True,
        Milestone.committed_at >= cutoff,
    )
    if seller_id:
        ms_q = ms_q.filter(Milestone.customer_id.in_(cust_ids))
    committed_milestones = ms_q.all()

    return {
        'period_days': days,
        'customer_activity': customer_notes,
        'customers_with_calls': len(customer_notes),
        'total_notes': len(recent_notes),
        'open_engagements': open_engagements,
        'recently_committed_milestones': [
            {
                'id': m.id,
                'url': _url('milestone', m.id),
                'title': _clean(m.display_text),
                'customer': m.customer.name if m.customer else None,
                'committed_at': m.committed_at.strftime('%Y-%m-%d') if m.committed_at else None,
            }
            for m in committed_milestones
        ],
    }


# -- Search contacts ---------------------------------------------------------

@tool(
    'search_contacts',
    'Search customer and partner contacts by name, email, or title.',
    {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Name, email, or title to search for.',
            },
            'customer_id': {
                'type': 'integer',
                'description': 'Filter to contacts at a specific customer.',
            },
            'partner_id': {
                'type': 'integer',
                'description': 'Filter to contacts at a specific partner.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 20).',
            },
        },
    },
)
def search_contacts(
    query: str = '',
    customer_id: int | None = None,
    partner_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search contacts across customers and partners."""
    from app.models import db, CustomerContact, PartnerContact

    results = []

    if not partner_id:
        # Search customer contacts
        cc_q = CustomerContact.query
        if query:
            pattern = f'%{query}%'
            cc_q = cc_q.filter(
                db.or_(
                    CustomerContact.name.ilike(pattern),
                    CustomerContact.email.ilike(pattern),
                    CustomerContact.title.ilike(pattern),
                )
            )
        if customer_id:
            cc_q = cc_q.filter(CustomerContact.customer_id == customer_id)
        for c in cc_q.limit(limit).all():
            results.append({
                'type': 'customer_contact',
                'id': c.id,
                'name': c.name,
                'email': c.email,
                'title': c.title,
                'customer': c.customer.name if c.customer else None,
                'customer_id': c.customer_id,
                'customer_url': _url('customer', c.customer_id) if c.customer_id else None,
            })

    if not customer_id:
        # Search partner contacts
        remaining = limit - len(results)
        if remaining > 0:
            pc_q = PartnerContact.query
            if query:
                pattern = f'%{query}%'
                pc_q = pc_q.filter(
                    db.or_(
                        PartnerContact.name.ilike(pattern),
                        PartnerContact.email.ilike(pattern),
                        PartnerContact.title.ilike(pattern),
                    )
                )
            if partner_id:
                pc_q = pc_q.filter(PartnerContact.partner_id == partner_id)
            for c in pc_q.limit(remaining).all():
                results.append({
                    'type': 'partner_contact',
                    'id': c.id,
                    'name': c.name,
                    'email': c.email,
                    'title': c.title,
                    'partner': c.partner.name if c.partner else None,
                    'partner_id': c.partner_id,
                    'partner_url': _url('partner', c.partner_id) if c.partner_id else None,
                })

    return results


# -- Revenue customer detail -------------------------------------------------

@tool(
    'get_revenue_customer_detail',
    'Get per-customer revenue history and bucket breakdown.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Customer ID to look up revenue for.',
            },
            'customer_name': {
                'type': 'string',
                'description': 'Customer name (used if customer_id not provided).',
            },
            'months': {
                'type': 'integer',
                'description': 'Number of recent months to include (default 6).',
            },
        },
    },
)
def get_revenue_customer_detail(
    customer_id: int | None = None,
    customer_name: str | None = None,
    months: int = 6,
) -> dict:
    """Return revenue breakdown for a customer."""
    from app.models import CustomerRevenueData

    if not customer_id and not customer_name:
        return {'error': 'Provide customer_id or customer_name.'}

    q = CustomerRevenueData.query
    if customer_id:
        q = q.filter(CustomerRevenueData.customer_id == customer_id)
    elif customer_name:
        q = q.filter(CustomerRevenueData.customer_name.ilike(f'%{customer_name}%'))

    rows = (
        q.order_by(
            CustomerRevenueData.month_date.desc(),
            CustomerRevenueData.bucket,
        )
        .all()
    )

    if not rows:
        return {'error': 'No revenue data found for this customer.'}

    # Get unique months sorted desc, limit to requested count
    all_months = sorted({r.month_date for r in rows}, reverse=True)[:months]

    # Build bucket summary
    bucket_totals: dict[str, float] = {}
    monthly: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.month_date not in all_months:
            continue
        bucket_totals[r.bucket] = bucket_totals.get(r.bucket, 0) + r.revenue
        month_key = r.fiscal_month
        monthly.setdefault(month_key, {})[r.bucket] = r.revenue

    return {
        'customer_name': rows[0].customer_name,
        'tpid': rows[0].tpid,
        'months_included': len(all_months),
        'buckets': {
            k: round(v, 2) for k, v in
            sorted(bucket_totals.items(), key=lambda x: -x[1])
        },
        'monthly_breakdown': {
            mk: {b: round(v, 2) for b, v in bv.items()}
            for mk, bv in monthly.items()
        },
    }


# ---------------------------------------------------------------------------
# MSX Workspace Tools
# ---------------------------------------------------------------------------

@tool(
    'get_msx_workspace_opportunities',
    'Query MSX opportunities with optional filters for customer, status, and deal team.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Filter by customer ID.',
            },
            'status': {
                'type': 'string',
                'description': 'Filter by state: open, won, lost, or empty for all.',
            },
            'team': {
                'type': 'string',
                'description': 'Filter by deal team: on, off, or empty for all.',
            },
            'search': {
                'type': 'string',
                'description': 'Search by name, number, or owner.',
            },
        },
    },
)
def get_msx_workspace_opportunities(**kwargs: Any) -> Any:
    """Return opportunities matching the given filters."""
    from app.models import Opportunity, Customer
    from sqlalchemy import or_

    query = Opportunity.query.filter(Opportunity.customer_id.isnot(None))

    customer_id = kwargs.get('customer_id')
    if customer_id:
        query = query.filter(Opportunity.customer_id == customer_id)

    status = kwargs.get('status', '')
    if status == 'open':
        query = query.filter(Opportunity.statecode == 0)
    elif status == 'won':
        query = query.filter(Opportunity.statecode == 1)
    elif status == 'lost':
        query = query.filter(Opportunity.statecode == 2)

    team = kwargs.get('team', '')
    if team == 'on':
        query = query.filter(Opportunity.on_deal_team.is_(True))
    elif team == 'off':
        query = query.filter(Opportunity.on_deal_team.is_(False))

    search = kwargs.get('search', '')
    if search:
        pat = f'%{search}%'
        query = query.filter(or_(
            Opportunity.name.ilike(pat),
            Opportunity.opportunity_number.ilike(pat),
            Opportunity.owner_name.ilike(pat),
        ))

    opps = query.order_by(Opportunity.name).limit(100).all()
    return {
        'count': len(opps),
        'opportunities': [
            {
                'id': o.id,
                'url': _url('opportunity', o.id),
                'name': _clean(o.name),
                'number': o.opportunity_number,
                'state': o.state or 'Open',
                'owner': o.owner_name,
                'customer': o.customer.name if o.customer else None,
                'on_deal_team': o.on_deal_team,
                'estimated_value': o.estimated_value,
                'close_date': o.estimated_close_date,
            }
            for o in opps
        ],
    }


@tool(
    'get_msx_workspace_milestones',
    'Query MSX milestones with optional filters for customer, status, and team membership.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Filter by customer ID.',
            },
            'status': {
                'type': 'string',
                'description': 'Comma-separated statuses: On Track, At Risk, Blocked, etc.',
            },
            'team': {
                'type': 'string',
                'description': 'Filter by team: on, off, or empty for all.',
            },
            'search': {
                'type': 'string',
                'description': 'Search by title, number, workload, or owner.',
            },
        },
    },
)
def get_msx_workspace_milestones(**kwargs: Any) -> Any:
    """Return milestones matching the given filters."""
    from app.models import Milestone
    from sqlalchemy import or_

    query = Milestone.query.filter(Milestone.customer_id.isnot(None))

    customer_id = kwargs.get('customer_id')
    if customer_id:
        query = query.filter(Milestone.customer_id == customer_id)

    status = kwargs.get('status', '')
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            query = query.filter(Milestone.msx_status.in_(statuses))

    team = kwargs.get('team', '')
    if team == 'on':
        query = query.filter(Milestone.on_my_team.is_(True))
    elif team == 'off':
        query = query.filter(Milestone.on_my_team.is_(False))

    search = kwargs.get('search', '')
    if search:
        pat = f'%{search}%'
        query = query.filter(or_(
            Milestone.title.ilike(pat),
            Milestone.milestone_number.ilike(pat),
            Milestone.workload.ilike(pat),
            Milestone.owner_name.ilike(pat),
        ))

    milestones = query.order_by(Milestone.due_date).limit(200).all()
    return {
        'count': len(milestones),
        'milestones': [
            {
                'id': m.id,
                'url': _url('milestone', m.id),
                'title': _clean(m.title),
                'number': m.milestone_number,
                'commitment': m.customer_commitment,
                'status': m.msx_status,
                'customer': m.customer.name if m.customer else None,
                'opportunity': _clean(m.opportunity.name if m.opportunity else m.opportunity_name),
                'workload': m.workload,
                'due_date': m.due_date.strftime('%Y-%m-%d') if m.due_date else None,
                'monthly_usage': m.monthly_usage,
                'on_my_team': m.on_my_team,
                'owner': m.owner_name,
            }
            for m in milestones
        ],
    }


@tool(
    'get_marketing_insights',
    'Get marketing engagement data for a customer or across all customers. '
    'Shows total interactions, content downloads, trials, engaged contacts, '
    'and per-sales-play breakdowns.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Get marketing insights for a specific customer.',
            },
        },
    },
)
def get_marketing_insights(customer_id: int | None = None) -> dict:
    """Return marketing engagement data."""
    from app.models import MarketingSummary, MarketingInteraction, MarketingContact

    if customer_id:
        summary = MarketingSummary.query.filter_by(customer_id=customer_id).first()
        if not summary:
            return {'customer_id': customer_id, 'message': 'No marketing data synced for this customer.'}

        interactions = (
            MarketingInteraction.query
            .filter_by(customer_id=customer_id)
            .order_by(MarketingInteraction.all_interactions.desc())
            .all()
        )
        contacts = (
            MarketingContact.query
            .filter_by(customer_id=customer_id)
            .order_by(
                (MarketingContact.mail_interactions + MarketingContact.meeting_interactions).desc()
            )
            .limit(10)
            .all()
        )
        return {
            'customer_id': customer_id,
            'summary': {
                'total_interactions': summary.total_interactions,
                'content_downloads': summary.content_downloads,
                'trials': summary.trials,
                'engaged_contacts': summary.engaged_contacts,
                'unique_decision_makers': summary.unique_decision_makers,
            },
            'sales_plays': [
                {
                    'solution_area': i.solution_area,
                    'sales_play': i.sales_play,
                    'interactions': i.all_interactions,
                }
                for i in interactions
            ],
            'top_contacts': [
                {
                    'name': c.contact_name,
                    'title': c.job_title,
                    'mail_interactions': c.mail_interactions,
                    'meeting_interactions': c.meeting_interactions,
                    'engagement_level': c.engagement_level,
                }
                for c in contacts
            ],
        }

    # All customers summary
    summaries = (
        MarketingSummary.query
        .order_by(MarketingSummary.total_interactions.desc())
        .limit(20)
        .all()
    )
    return {
        'customers_with_data': len(summaries),
        'top_customers': [
            {
                'customer_id': s.customer_id,
                'total_interactions': s.total_interactions,
                'engaged_contacts': s.engaged_contacts,
            }
            for s in summaries
        ],
    }


@tool(
    'get_u2c_attainment',
    'Get U2C (uncommitted-to-committed) milestone attainment for the current '
    'or specified fiscal quarter. Shows target ACR, committed ACR, attainment '
    'percentage, and lists remaining milestones to commit.',
    {
        'type': 'object',
        'properties': {
            'fiscal_quarter': {
                'type': 'string',
                'description': 'Fiscal quarter label, e.g. "FY26 Q4". Defaults to current quarter.',
            },
            'workload': {
                'type': 'string',
                'description': 'Workload prefix filter, e.g. "Data" or "Infra".',
            },
        },
    },
)
def get_u2c_attainment(
    fiscal_quarter: str | None = None,
    workload: str | None = None,
) -> dict:
    """Return U2C attainment data for a fiscal quarter."""
    from app.models import U2CSnapshot
    from app.services.u2c_snapshot import (
        current_fiscal_quarter, get_attainment,
    )

    fq = fiscal_quarter or current_fiscal_quarter()
    snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
    if not snapshot:
        return {'fiscal_quarter': fq, 'message': f'No U2C snapshot exists for {fq}.'}

    result = get_attainment(snapshot.id, workload or None)
    # Trim item details for tool response (keep top 10 remaining)
    if result.get('remaining_items'):
        result['remaining_items'] = result['remaining_items'][:10]
    if result.get('committed_items'):
        result['committed_items'] = result['committed_items'][:10]
    return result


@tool(
    'report_connect_impact',
    'Get Connect Impact report: customers ranked by total estimated ACR/mo '
    'from committed milestones you are on the team for. Defaults to last 5 months.',
    {
        'type': 'object',
        'properties': {
            'since': {
                'type': 'string',
                'description': 'Start date (YYYY-MM-DD). Defaults to 5 months ago.',
            },
        },
    },
)
def report_connect_impact(since: str | None = None) -> dict:
    """Return Connect Impact data."""
    from app.models import Milestone, Customer
    from datetime import datetime, date, timedelta

    if since:
        try:
            since_date = datetime.strptime(since, '%Y-%m-%d').date()
        except ValueError:
            since_date = None
    else:
        since_date = None

    if not since_date:
        m = date.today().month - 5
        y = date.today().year
        while m < 1:
            m += 12
            y -= 1
        since_date = date(y, m, min(date.today().day, 28))

    milestones = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,
            Milestone.customer_commitment == 'Committed',
            Milestone.committed_at >= datetime(
                since_date.year, since_date.month, since_date.day,
            ),
        )
        .join(Customer, Milestone.customer_id == Customer.id)
        .all()
    )

    customer_map: dict[int, dict] = {}
    for ms in milestones:
        if not ms.customer_id:
            continue
        cid = ms.customer_id
        if cid not in customer_map:
            customer_map[cid] = {
                'customer_id': cid,
                'customer': ms.customer.nickname or ms.customer.name,
                'total_acr_mo': 0.0,
                'milestones': [],
            }
        customer_map[cid]['total_acr_mo'] += ms.monthly_usage or 0.0
        customer_map[cid]['milestones'].append({
            'id': ms.id,
            'url': _url('milestone', ms.id),
            'title': _clean(ms.title),
            'workload': ms.workload,
            'monthly_usage': ms.monthly_usage or 0,
            'commitment': ms.customer_commitment,
            'status': ms.msx_status,
        })

    ranked = sorted(customer_map.values(), key=lambda x: x['total_acr_mo'], reverse=True)
    return {
        'since': since_date.isoformat(),
        'grand_total_acr_mo': sum(r['total_acr_mo'] for r in ranked),
        'customers': ranked,
    }


# ---------------------------------------------------------------------------
# Stale Customers / M&A
# ---------------------------------------------------------------------------

@tool(
    'get_stale_customers',
    'Get customers flagged as stale (TPID disappeared from MSX sync). '
    'These may have been merged, acquired, or reassigned in MSX.',
    {
        'type': 'object',
        'properties': {},
    },
)
def get_stale_customers() -> dict:
    """Return all customers with stale_since set."""
    from app.models import db, Customer, Note, Engagement
    stale = Customer.query.filter(
        Customer.stale_since.isnot(None)
    ).order_by(Customer.stale_since).all()

    return {
        'count': len(stale),
        'customers': [
            {
                'id': c.id,
                'url': _url('customer', c.id),
                'name': c.get_display_name(),
                'tpid': c.tpid,
                'stale_since': c.stale_since.isoformat() if c.stale_since else None,
                'notes_count': Note.query.filter_by(customer_id=c.id).count(),
                'engagements_count': Engagement.query.filter_by(customer_id=c.id).count(),
            }
            for c in stale
        ],
    }
