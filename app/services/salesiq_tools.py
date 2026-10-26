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
    'one_on_one': '/one-on-one/',
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


@tool(
    'get_sync_job_status',
    'Read every data sync operation, worker health, active/latest jobs and last successful refreshes.',
    {'type': 'object', 'properties': {}},
)
def get_sync_job_status() -> dict:
    """Read the same durable sync projection used by the Sync Status page."""
    from app.services.sync_jobs import operation_snapshots
    return operation_snapshots()


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

# -- 1:1 workspaces ----------------------------------------------------------

@tool(
    'report_ptof',
    'Read saved Power BI Premium to Fabric renewal assessments and subscription details. '
    'Includes local customer names, TPIDs, full expiration dates, source renewal status, '
    'SA expiration and P-SKU renewal amounts, and F-SKU annualized RI and PAYG ACR. '
    'Account assessments and subscription details are separate grains; do not add them together. '
    'Does not import or refresh data.',
    {'type': 'object', 'properties': {
        'customer_id': {'type': 'integer', 'minimum': 1,
                        'description': 'Optional Sales Buddy customer ID.'},
    }},
)
def report_ptof(customer_id: int | None = None) -> dict:
    """Reuse the renewal report and customer Fabric-tab read service."""
    from app.services.ptof import get_ptof_report

    return get_ptof_report(customer_id)


@tool(
    'report_fabric_consumption',
    'Read the saved Fabric workload mix and capacity utilization snapshot. '
    'Includes all customer coverage and optional customer capacity detail. '
    'Customer detail includes saved fiscal YTD and monthly reserved-instance versus PAYG revenue, '
    'not reservation inventory or per-capacity coverage. '
    'Set overview=true for one row per capacity with customer-wide workload shares. '
    'Does not import or refresh data.',
    {'type': 'object', 'properties': {
        'customer_id': {'type': 'integer', 'minimum': 1,
                        'description': 'Sales Buddy customer ID for detailed Fabric measurements.'},
        'overview': {'type': 'boolean',
                     'description': 'Return all customer/capacity comparison rows instead of detail.'},
    }},
)
def report_fabric_consumption(customer_id: int | None = None, overview: bool = False) -> dict:
    """Read the same saved snapshot as the Fabric report and customer tab."""
    from app.services.fabric_consumption import get_fabric_capacity_overview, get_fabric_report

    if overview:
        return get_fabric_capacity_overview()
    return get_fabric_report(customer_id=customer_id)


@tool(
    'report_fabric_acquisition',
    'Read the Fabric Acquisition tab of Whitespace Analysis for Data SEs and sellers. '
    'Returns customers with positive AI, Databases or Analytics excluding Fabric '
    'and no positive monthly net Fabric ACR in the current partial calendar month or previous '
    'three months. All displayed amounts are four-month totals divided by 4. '
    'Set include_no_other_spend=true to include all current customers regardless of other spend. '
    'Accounts flagged stale after dropping out of account sync are excluded. '
    'Missing customer, product and month revenue observations count as zero. '
    'Saved observations do not prove non-adoption.',
    {'type': 'object', 'properties': {
        'include_no_other_spend': {
            'type': 'boolean',
            'description': 'Include all customers with no Fabric spend regardless of other spend.',
        },
    }},
)
def report_fabric_acquisition(include_no_other_spend: bool = False) -> dict:
    """Reuse the report acquisition queue and its optional expansion without importing data."""
    from app.services.fabric_acquisition import get_fabric_acquisition_report

    data = get_fabric_acquisition_report(include_no_other_spend=include_no_other_spend)
    if not data['available']:
        return {'error': 'Fabric Acquisition is available for Data SEs and sellers.'}
    return data


@tool(
    'report_database_whitespace',
    'Read Database whitespace candidates for Data SEs and sellers from the existing '
    'Whitespace Analysis grid. Uses the source-defined Databases pillar and the same '
    'latest imported month minus 62 days window, averaging stored monthly pillar totals. '
    'Includes customers with positive AI, Analytics or Apps coverage and no Databases coverage. '
    'No added minimum spend or new stale-account rule. Returns count, ranked customer rows, '
    'AI, Analytics (including Fabric), Apps and their total monthly spend, and seller groups. '
    'A discovery queue, not confirmed opportunities or achieved consumption wins.',
    {'type': 'object', 'properties': {}},
)
def report_database_whitespace() -> dict:
    """Reuse the existing grid-backed widget calculation without refreshing source data."""
    from app.services.whitespace import get_database_whitespace_widget

    data = get_database_whitespace_widget()
    if not data['available']:
        return {'error': 'Database whitespace is available for Data SEs and sellers.'}
    return data


@tool(
    'report_database_expansion',
    'Read Database Expansion signals from the existing Revenue Analyzer for Data SEs '
    'and sellers. Databases bucket only, priority 20+, New or To be reviewed status, '
    'with EXPANSION OUTREACH or CHECK-IN recommendations. Monitor-only signals excluded. '
    'Returns the complete priority-ranked queue, separate growth/risk counts and monthly '
    'impact estimates. Uses existing saved scores, not new propensity or qualified outcomes.',
    {'type': 'object', 'properties': {}},
)
def report_database_expansion() -> dict:
    """Reuse the analyzer-backed motion widget without rerunning revenue analysis."""
    from app.services.revenue_analysis import get_database_expansion_widget
    from app.services.se_specialty import has_specialty_access

    if not has_specialty_access('data'):
        return {'error': 'Database Expansion is available for Data SEs and sellers.'}
    return get_database_expansion_widget()


@tool(
    'report_sql_renewals',
    'Read upcoming SQL expirations for current customers from the saved shared SQL Motions '
    'snapshot. Includes exact source dates, unknown amounts, on-prem SA license cores, '
    'estimated capped Azure IaaS cores and modeled monthly ACR opportunity. '
    'Modeled ACR is not qualified pipeline. Does not refresh data.',
    {'type': 'object', 'properties': {}},
)
def report_sql_renewals() -> dict:
    """Reuse the saved report queue and its current customer and seller scope."""
    from app.services.se_specialty import has_specialty_access
    from app.services.sql_motions import get_sql_motion_report

    if not has_specialty_access('data'):
        return {'error': 'SQL motions are available for Data SEs and sellers.'}
    return get_sql_motion_report('renewals')


@tool(
    'report_sql_eos',
    'Warning: SQL Server 2016 data is known to be incorrect. Do not rely on the counts '
    'or customer lists; always surface the returned data_warning. '
    'Read retained SQL Server 2016 Arc discovery in the saved shared SQL '
    'Motions snapshot. Uses only positive All Discovered counts, never overlapping category '
    'totals. Includes subscription evidence and source processing dates. '
    'Discovery units and ESU protection are unverified. No observation means unknown, '
    'not proof of no exposure. Does not refresh data.',
    {'type': 'object', 'properties': {}},
)
def report_sql_eos() -> dict:
    """Reuse the report without inferring entitlement from paid license fields."""
    from app.services.se_specialty import has_specialty_access
    from app.services.sql_motions import get_sql_motion_report

    if not has_specialty_access('data'):
        return {'error': 'SQL motions are available for Data SEs and sellers.'}
    return get_sql_motion_report('eos')


@tool(
    'report_rti',
    'Read Real-Time Intelligence customer discovery signals: native rolling 28-day RTI '
    'CU-hours and workload share alongside Event Hubs, Stream Analytics and IoT ACR. '
    'ACR covers the last three complete calendar months, divided by 3; current month excluded. '
    'Current accounts qualify with zero RTI usage and a positive Azure streaming product-family '
    'total, including accounts without Fabric capacity. Missing RTI measurements and revenue '
    'observations count as zero. Includes seller groups. Saved data only, not architecture '
    'or migration readiness.',
    {'type': 'object', 'properties': {}},
)
def report_rti() -> dict:
    """Reuse RTI report calculation without refreshing source data."""
    from app.services.rti import get_rti_report

    return get_rti_report()


@tool(
    'report_fabric_modernization',
    'Read Fabric Modernization: one row per customer with positive recent Synapse, '
    'Azure Analysis Services, Azure Data Explorer, Azure Databricks, or Azure Data Factory revenue. '
    'Displays average monthly ACR across the last three complete calendar months. '
    'Other Analytics combines Synapse, AAS, ADX and Databricks; ADF is separate. Eligibility uses '
    'combined Other Analytics plus ADF three-month totals of at least $50, or $100 '
    'with positive Fabric spend. '
    'Rows retain spend and source_revenue as three-month totals; monthly_spend and '
    'monthly_source_revenue divide by each customer\'s available months within that period, '
    'returning zero with no observed months. '
    'Includes seller groups and monthly coverage. Saved data only, not evidence of '
    'migration, readiness, or non-Azure Databricks inventory.',
    {'type': 'object', 'properties': {}},
)
def report_fabric_modernization() -> dict:
    """Reuse the report's recent-product revenue calculation."""
    from app.services.fabric_modernization import get_fabric_modernization_report

    return get_fabric_modernization_report()


@tool(
    'report_fabric_modernization_candidates',
    'Read the Fabric modernization widget: report-qualified customers whose last three '
    'complete months of Other Analytics (Synapse, AAS, ADX and Azure Databricks) plus ADF '
    'strictly exceed '
    'Fabric spend. Includes matching customers, count, seller groups and coverage. '
    'monthly_other_analytics_revenue and monthly_adf_revenue are separate monthly averages; '
    'monthly_source_revenue combines them. Summary averages sum each customer\'s monthly '
    'average using that customer\'s available months, or zero without observations. '
    'source_revenue retains the combined three-month total. '
    'has_synapse marks positive net Synapse consumption, not confirmed migration. '
    'Saved observations only, not proof of migration or readiness.',
    {'type': 'object', 'properties': {}},
)
def report_fabric_modernization_candidates() -> dict:
    """Reuse the widget's report-backed candidate calculation."""
    from app.services.fabric_modernization import get_fabric_modernization_widget

    return get_fabric_modernization_widget()


@tool(
    'report_fabric_expansion',
    'Read saved Power BI-led Fabric expansion signals for customers with paid F-SKUs. '
    'One row per customer with the largest paid F-SKU by CU size, P/trial capacity flags, '
    'equal-weight average capacity '
    'utilization and customer-wide '
    'rolling 28-day workload shares. Power BI-led means at least 80% Power BI. '
    'Includes the same customer rows grouped by seller. Missing or unreconciled '
    'workload mixes are excluded and counted separately. Includes known workloads above 1% for discovery '
    'conversations, not validated workload fit or readiness. Does not import or refresh data.',
    {'type': 'object', 'properties': {}},
)
def report_fabric_expansion() -> dict:
    """Reuse the Capacity overview-backed dashboard expansion calculations."""
    from app.services.fabric_consumption import get_fabric_expansion_widget

    return get_fabric_expansion_widget()


@tool(
    'report_fabric_trial_conversion',
    'Find Trial to Paid prospects: distinct customers with saved Fabric trial SKUs '
    '(FTL4/FTL64) and no paid F-SKU capacities. P-SKU customers remain eligible. '
    'Includes customer queues split at 0.1% average trial utilization over the saved '
    'source trailing 15-day window, averaging trial capacity means equally. '
    'Includes P-SKU context, average observed days across trials, and customer detail links. '
    'A trial present in the latest snapshot is active for this workflow. Engagement creation '
    'and trial-to-F-SKU conversion tracking are manual, not automatic outcome detection.',
    {'type': 'object', 'properties': {}},
)
def report_fabric_trial_conversion() -> dict:
    """Reuse the dashboard trial cohort counts without refreshing Fabric data."""
    from app.services.fabric_consumption import get_fabric_trial_conversion_widget

    return get_fabric_trial_conversion_widget()


@tool(
    'report_fabric_capacity_pressure',
    'Investigate the full saved Fabric capacity pressure report, including recorded throttling, '
    'latest utilization, historical peaks, incomplete data, timestamps, and exact capacity links. '
    'Defaults to capacities needing attention. Use signal=all for the complete population. '
    'Unlike the dashboard widget, results are not limited to five capacities.',
    {'type': 'object', 'properties': {
        'signal': {'type': 'string', 'description': 'attention, all, throttled, overloaded, '
                   'near_limit, historical_spike, unknown, or below_threshold.'},
        'search': {'type': 'string', 'description': 'Customer, seller, TPID, SKU or capacity ID.'},
    }},
)
def report_fabric_capacity_pressure(signal: str = 'attention', search: str = '') -> dict:
    """Read the same complete, filterable investigation data as the report."""
    from app.services.fabric_consumption import get_fabric_capacity_pressure_report

    return get_fabric_capacity_pressure_report(signal=signal, search=search)


@tool(
    'report_manager_one_on_one',
    'Read Initiative Tracker, including named initiatives, '
    'seller-grouped engagements and milestones, internal projects, live status, commitment, dates, '
    'combined ACR/month from engagements and directly added milestones per initiative and seller, '
    'current points, and discussion history with creation and discussion timestamps.',
    {'type': 'object', 'properties': {}},
)
def report_manager_one_on_one() -> dict:
    """Return Initiative Tracker using the page's shared query service."""
    from app.services.manager_one_on_one import get_manager_one_on_one_report

    return get_manager_one_on_one_report()


@tool(
    'get_manager_u2c_candidates',
    'Read this quarter\'s remaining U2C snapshot milestones for Initiative Tracker. '
    'Includes locally available milestones and explicitly marks records not yet synced. '
    'Does not add anything to the report.',
    {'type': 'object', 'properties': {
        'search': {'type': 'string', 'description': 'Filter by milestone, customer, or workload.'},
        'workload_prefix': {
            'type': 'string',
            'description': 'U2C workload group, such as Data or Infra. Empty means all workloads.',
        },
        'workloads': {
            'type': 'array', 'items': {'type': 'string'},
            'description': 'Exact detailed workload names, OR within the selection. Empty means all.',
        },
    }},
)
def get_manager_u2c_candidates(
    search: str = '', workload_prefix: str = '', workloads: list[str] | None = None,
) -> dict:
    """Use Initiative Tracker's shared U2C-assisted selection service."""
    from app.services.manager_one_on_one import get_manager_u2c_candidates as get_candidates

    return get_candidates(search, workload_prefix=workload_prefix, workloads=workloads)


@tool(
    'get_one_on_one_workspaces',
    'Get persistent 1:1 notes and agenda items for sellers, managers, and other people.',
    {
        'type': 'object',
        'properties': {
            'include_discussed': {
                'type': 'boolean',
                'description': 'Include completed discussion-history items.',
            },
        },
    },
)
def get_one_on_one_workspaces(include_discussed: bool = False) -> dict:
    """Return persistent one-on-one workspace summaries."""
    from app.services.one_on_one import get_one_on_one_workspace_summaries

    workspaces = get_one_on_one_workspace_summaries(include_discussed)
    for workspace in workspaces:
        workspace['url'] = _url('one_on_one', workspace['id'])
    return {'count': len(workspaces), 'workspaces': workspaces}

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
    'get_customer_timeline',
    'Read customer history: call notes, engagement creation, opportunity creation/closure, '
    'and created/committed/completed milestones where I am on the team.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {'type': 'integer'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
        },
        'required': ['customer_id'],
    },
)
def get_customer_timeline(customer_id: int, limit: int = 100, offset: int = 0) -> dict:
    """Read the same paged event feed used by the customer history rail."""
    from app.models import Customer, db
    from app.services.customer_timeline import get_customer_timeline as read_timeline

    if db.session.get(Customer, customer_id) is None:
        return {'error': f'Customer {customer_id} not found.'}
    return read_timeline(customer_id, limit=limit, offset=offset)


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
            Milestone.customer_id.isnot(None),
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
    '"Active" means not Completed/Cancelled/Lost - use active_only=true. '
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
                'description': 'Filter by exact MSX execution status (On Track, At Risk, Blocked, '
                               'Completed, Cancelled, Lost to Competitor, Hygiene/Duplicate).',
            },
            'active_only': {
                'type': 'boolean',
                'description': 'If true, only return active milestones (On Track, At Risk, Blocked). '
                               'Excludes Completed, Cancelled, Lost, Hygiene. '
                               'Use this when user says "active" milestones.',
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
    active_only: bool | None = None,
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

    q = Milestone.query.filter(Milestone.customer_id.isnot(None))
    if customer_id:
        q = q.filter(Milestone.customer_id == customer_id)
    if commitment:
        q = q.filter(Milestone.customer_commitment == commitment)
    if status:
        q = q.filter(Milestone.msx_status == status)
    if active_only:
        q = q.filter(Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']))
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
    'get_dashboards',
    'List saved dashboards and the widget catalog, or read one dashboard with '
    'its current report-backed widget data and setup requirements.',
    {
        'type': 'object',
        'properties': {
            'dashboard_id': {
                'type': 'integer',
                'minimum': 1,
                'description': 'Optional saved dashboard ID. Omit to list dashboards.',
            },
            'live_filters': {
                'type': 'object',
                'description': 'Optional customer_ids and seller_ids arrays. Requires a dashboard '
                'with those live controls enabled; only narrows saved scope.',
                'properties': {
                    'customer_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
                    'seller_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
                },
                'additionalProperties': False,
            },
        },
    },
)
def get_dashboards(dashboard_id: int | None = None, live_filters: dict | None = None) -> dict:
    """Read the same saved dashboard definitions and values used by the UI."""
    from app.models import Dashboard, db
    from app.services.dashboards import (
        get_dashboard_data, get_widget_catalog, list_dashboards,
    )

    if dashboard_id is None:
        if live_filters is not None:
            raise ValueError('Choose a dashboard before applying live filters.')
        from app.services.dashboard_templates import list_dashboard_templates

        return {'dashboards': list_dashboards(), 'catalog': get_widget_catalog(),
                'templates': list_dashboard_templates(),
                'url': f'{_BASE}/reports/dashboards'}
    if type(dashboard_id) is not int or dashboard_id < 1:
        raise ValueError('Choose a valid dashboard ID.')
    dashboard = db.session.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise ValueError('Dashboard not found.')
    result = (get_dashboard_data(dashboard, live_filters=live_filters)
              if live_filters is not None else get_dashboard_data(dashboard))
    result['url'] = f'{_BASE}{result["url"]}'
    return result


@tool(
    'report_dashboard_signals',
    'Read a dashboard widget calculation without creating a dashboard or refreshing data. '
    'Use widget_key from the get_dashboards catalog. Includes hygiene.gaps, '
    'activity_coverage.meetings, revenue.attention, ptof.upcoming_renewals, and '
    'fabric.capacity_pressure, fabric.trial_conversion, and fabric.expansion. '
    'Returns saved-data scope, '
    'missing data, source timestamps, '
    'and drill-through destinations alongside each signal. Shared prerequisites are preserved.',
    {'type': 'object', 'properties': {
        'widget_key': {'type': 'string', 'description': 'Registered widget key.'},
        'filters': {
            'type': 'object',
            'description': 'Optional Customer/Seller scope for a widget that supports it.',
            'properties': {
                'customer_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
                'seller_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
            },
            'additionalProperties': False,
        },
    }, 'required': ['widget_key']},
)
def report_dashboard_signals(widget_key: str, filters: dict | None = None) -> dict:
    """Reuse the catalog provider rather than duplicating a widget's analytical rules."""
    from app.services.dashboards import (
        WIDGET_DEFINITIONS, get_widget_requirements, validate_widget_filters,
    )
    from app.services.dashboard_filters import resolve_customer_scope, scope_report_destination

    definition = WIDGET_DEFINITIONS.get(widget_key)
    if definition is None:
        raise ValueError('Choose a registered dashboard widget key from get_dashboards.')
    selected = validate_widget_filters(widget_key, filters)
    customer_ids = resolve_customer_scope(selected)
    requirements = get_widget_requirements(definition)
    if requirements:
        return {'requirements': requirements}
    data = (definition.provider(None, customer_ids=customer_ids) if customer_ids is not None
            else definition.provider(None))
    return scope_report_destination(data, widget_key, customer_ids)


@tool(
    'report_connect_goals',
    'Get shared FY27 Data Connect Goals widget metrics, goals, status, and '
    'drill-down destinations.',
    {'type': 'object', 'properties': {}},
)
def report_connect_goals() -> dict:
    """Return the shared Connect Goals calculations."""
    from app.services.connect_goals import get_connect_goals
    from app.services.dashboard_templates import get_template_dashboard_url

    data = get_connect_goals()
    data['url'] = _BASE + get_template_dashboard_url('connect-goals')
    return data


@tool(
    'report_hvas',
    'List your owned MSX HVA tasks across all dates and milestone scopes, including completed '
    'tasks. Returns filtered totals by status, creation/due/completion dates and paged records.',
    {
        'type': 'object',
        'properties': {
            'search': {'type': 'string', 'description': 'Task, customer, category or related work.'},
            'status': {'type': 'string', 'enum': ['', 'open', 'completed', 'canceled', 'unknown']},
            'customer_id': {'type': 'integer', 'minimum': 1},
            'category': {'type': 'integer', 'description': 'MSX HVA category code.'},
            'date_field': {'type': 'string', 'enum': ['created', 'due', 'completed']},
            'date_from': {'type': 'string', 'description': 'Inclusive UTC date, YYYY-MM-DD.'},
            'date_to': {'type': 'string', 'description': 'Inclusive UTC date, YYYY-MM-DD.'},
            'page': {'type': 'integer', 'minimum': 1},
            'per_page': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        },
    },
)
def report_hvas(
    search: str = '', status: str = '', customer_id: int | None = None,
    category: int | None = None, date_field: str = 'created',
    date_from: str | None = None, date_to: str | None = None,
    page: int = 1, per_page: int = 50,
) -> dict:
    """Use the same owned-task inventory as Browse > Work > HVAs, without remote reads."""
    from urllib.parse import urlencode
    from app.services.hva_inventory import get_hva_inventory

    options = {
        'search': search, 'status': status, 'customer_id': customer_id, 'category': category,
        'date_field': date_field, 'date_from': date_from, 'date_to': date_to,
        'page': page, 'per_page': per_page,
    }
    data = get_hva_inventory(
        search=search, status=status, customer_id=customer_id, category=category,
        date_field=date_field, date_from=date_from, date_to=date_to,
        page=page, per_page=per_page,
    )
    params = urlencode({key: value for key, value in options.items() if value is not None})
    return {'url': f'{_BASE}/reports/hvas?{params}', **data}


@tool(
    'report_activity_coverage',
    'Get fiscal-year meeting and on-team milestone HVA activity coverage.',
    {
        'type': 'object',
        'properties': {
            'week': {
                'type': 'string',
                'description': 'Any date in the requested week, YYYY-MM-DD.',
            },
        },
    },
)
def report_activity_coverage(week: str | None = None) -> dict:
    """Return meeting coverage plus active milestones needing current-FY HVA."""
    from app.services.activity_coverage import (
        get_milestone_coverage_data,
        get_report_data,
        normalize_week_start,
    )

    try:
        week_start = normalize_week_start(week) if week else None
    except ValueError:
        return {'error': 'week must use YYYY-MM-DD format'}
    data = get_report_data(week_start)
    milestone_data = get_milestone_coverage_data()
    return {
        'url': f'{_BASE}/reports/activity-coverage?week={data["week_start"].isoformat()}',
        'milestone_url': f'{_BASE}/reports/activity-coverage?lens=milestones',
        'fiscal_year': {
            'start': data['fiscal_start'].isoformat(),
            'end': data['fiscal_end'].isoformat(),
        },
        'summary': data['summary'],
        'milestone_summary': milestone_data['milestone_summary'],
        'milestones_needing_hva': [
            {
                'id': row['id'],
                'customer': (
                    row['milestone'].customer.name
                    if row['milestone'].customer else None
                ),
                'milestone': row['milestone'].display_text,
                'status': row['milestone'].msx_status,
                'due_date': (
                    row['milestone'].due_date.date().isoformat()
                    if row['milestone'].due_date else None
                ),
                'prior_hva_fiscal_year': row['prior_fiscal_year'],
                'prepared_meetings': row['prepared_meeting_count'],
            }
            for row in milestone_data['milestone_rows']
        ],
        'meetings': [
            {
                'id': meeting['id'],
                'subject': _clean(meeting['subject']),
                'date': meeting['meeting_date'].isoformat(),
                'customer': meeting['customer'].name if meeting['customer'] else None,
                'status': meeting['status'],
                'milestone': (
                    meeting['activity'].milestone.display_text
                    if meeting['activity'] else None
                ),
            }
            for meeting in data['meetings']
        ],
    }


@tool(
    'report_caip_coverage',
    'Get Activities Logged and current-FY HVA coverage for CAIP team milestones.',
    {'type': 'object', 'properties': {}},
)
def report_caip_coverage() -> dict:
    """Return CAIP coverage metrics and milestone evidence states."""
    from app.services.activity_coverage import get_caip_coverage_data

    data = get_caip_coverage_data()
    return {
        'url': f'{_BASE}/reports/activity-coverage?lens=milestones&coverage=caip',
        'fiscal_year': data['fiscal_year_label'],
        'summary': data['caip_summary'],
        'milestones': [
            {
                'id': row['id'],
                'milestone': row['milestone'].display_text,
                'target_fy': row['target_fy'],
                'category': row['milestone'].milestone_category,
                'status': row['milestone'].msx_status,
                'customer': (
                    row['milestone'].customer.name
                    if row['milestone'].customer else None
                ),
                'opportunity': (
                    row['milestone'].opportunity.name
                    if row['milestone'].opportunity
                    else row['milestone'].opportunity_name
                ),
                'activity_logged': row['activity_logged'],
                'hva_covered': row['hva_covered'],
            }
            for group in data['caip_groups']
            for row in group['rows']
        ],
    }

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
    """Return the shared active hygiene repair queues."""
    from app.services.hygiene import get_hygiene_report_data

    data = get_hygiene_report_data(seller_id)
    return {
        'engagements_without_milestones': [
            {
                'id': e.id,
                'url': _url('engagement', e.id),
                'title': e.title,
                'customer': e.customer.name if e.customer else None,
            }
            for e in data['engagements_no_ms']
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
            for m in data['milestones_no_eng']
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
            Milestone.customer_id.isnot(None),
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
            Milestone.customer_id.isnot(None),
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
                'description': 'Filter by category (DECLINING, RECENT_DIP, '
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
    'SuperStrategicPillars: AI, Analytics, Apps, Databases, Developer, Infra, Security.',
    {
        'type': 'object',
        'properties': {
            'customer_id': {
                'type': 'integer',
                'description': 'Check whitespace for a specific customer.',
            },
            'pillar': {
                'type': 'string',
                'description': 'Check how many customers have historical spend in this pillar.',
                'enum': ['AI', 'Analytics', 'Apps', 'Databases', 'Developer', 'Infra', 'Security'],
            },
        },
    },
)
def report_whitespace(
    customer_id: int | None = None,
    pillar: str | None = None,
) -> dict:
    """Return source-defined pillar coverage, using the report's shared monthly rollup."""
    from app.services.whitespace import pillar_coverage
    return pillar_coverage(customer_id, pillar)


@tool(
    'get_strategic_pillar_revenue',
    'Read monthly ACR by SuperStrategicPillar for a customer, independent of service buckets.',
    {'type': 'object', 'properties': {
        'customer_id': {'type': 'integer', 'description': 'Sales Buddy customer ID.'},
        'pillar': {'type': 'string', 'enum': [
            'AI', 'Analytics', 'Apps', 'Databases', 'Developer', 'Infra', 'Security',
        ]},
    }, 'required': ['customer_id']},
)
def get_strategic_pillar_revenue(customer_id: int, pillar: str | None = None) -> dict:
    """Read the same monthly pillar totals used by whitespace analysis."""
    from app.services.whitespace import pillar_history
    return pillar_history(customer_id, pillar)


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
        Milestone.customer_id.isnot(None),
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


# -- Anonymous app adoption --------------------------------------------------

@tool(
    'get_usage_metrics',
    'Read anonymous Sales Buddy adoption from App Insights: page-based DAU, page loads, '
    'install Git versions, roles and SE specialties. Requires telemetry resource read access. '
    'Never opens sign-in prompts; auth and query failures are returned explicitly.',
    {
        'type': 'object',
        'properties': {
            'insight': {
                'type': 'string',
                'enum': [
                    'activity_summary', 'dau', 'page_loads', 'install_inventory',
                    'installs_by_role', 'se_specialties', 'app_versions',
                ],
                'description': 'The usage insight to read (default activity_summary).',
            },
            'days': {
                'type': 'integer',
                'enum': [1, 7, 30, 90],
                'description': 'UTC calendar days including today (default 7).',
            },
        },
    },
)
def get_usage_metrics(insight: str = 'activity_summary', days: int = 7) -> dict:
    """Use the same query and error contract as the metrics dashboard."""
    from app.services.usage_metrics import get_metric_card

    return get_metric_card(insight, days)


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
    'and milestone updates in a configurable rolling window (14 days by default; 30 for MBRs).',
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
    from app.models import Note, Engagement, Milestone, Customer
    from app.services.one_on_one_report import get_activity_cutoffs

    cutoff, call_date_cutoff = get_activity_cutoffs(days)

    # Recent notes
    note_q = Note.query.filter(Note.call_date >= call_date_cutoff)
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
        Milestone.customer_id.isnot(None),
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
            'workloads': {
                'type': 'array', 'items': {'type': 'string'},
                'description': 'Exact detailed workload OR selection. Empty means all.',
            },
            'search': {
                'type': 'string',
                'description': (
                    'Case-insensitive literal search across all saved milestone fields, '
                    'cached forecast comments, and related customer, seller, and opportunity names.'
                ),
            },
        },
    },
)
def get_msx_workspace_milestones(**kwargs: Any) -> Any:
    """Return milestones matching the given filters."""
    from app.models import Customer, Milestone, db
    from app.services.milestone_search import get_milestone_search_text
    from app.services.workload_filters import normalize_workloads

    query = Milestone.query.filter(Milestone.customer_id.isnot(None))
    workloads = normalize_workloads(kwargs.get('workloads'))
    if workloads is not None:
        query = query.filter(
            db.func.trim(db.func.coalesce(Milestone.workload, '')).in_(workloads),
        )

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

    search = kwargs.get('search', '').strip().lower()
    query = query.options(
        db.joinedload(Milestone.customer).joinedload(Customer.seller),
        db.joinedload(Milestone.customer).joinedload(Customer.territory),
        db.joinedload(Milestone.opportunity),
    ).order_by(Milestone.due_date)
    if search:
        milestones = []
        for milestone in query.yield_per(200):
            if search in get_milestone_search_text(milestone):
                milestones.append(milestone)
                if len(milestones) == 200:
                    break
    else:
        milestones = query.limit(200).all()
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
            'workloads': {
                'type': 'array', 'items': {'type': 'string'},
                'description': 'Exact detailed workload OR selection. Empty means all.',
            },
        },
    },
)
def get_u2c_attainment(
    fiscal_quarter: str | None = None,
    workload: str | None = None,
    workloads: list[str] | None = None,
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

    result = get_attainment(snapshot.id, workload or None, workloads=workloads)
    # Trim item details for tool response (keep top 10 remaining)
    if result.get('remaining_items'):
        result['remaining_items'] = result['remaining_items'][:10]
    if result.get('committed_items'):
        result['committed_items'] = result['committed_items'][:10]
    return result


@tool(
    'get_u2c_attainment_trend',
    'Get U2C attainment history within a fiscal quarter. Historical points are '
    'captured MSX Insights publications or explicitly labeled daily observations, showing committed ACR '
    'against the starting target. The current, non-final quarter also includes '
    'a separate Today/live point using current commitment status and the latest '
    'imported snapshot dollars. Every publish is retained, including one on the same date. '
    'Useful for "how has attainment progressed?" '
    'questions. Note the series can go down - MSXi restates conversions.',
    {
        'type': 'object',
        'properties': {
            'fiscal_quarter': {
                'type': 'string',
                'description': 'Fiscal quarter label, e.g. "FY27 Q1". Defaults to current quarter.',
            },
            'workloads': {
                'type': 'array', 'items': {'type': 'string'},
                'description': 'Exact detailed workload OR selection. Empty means all.',
            },
        },
    },
)
def get_u2c_attainment_trend(
    fiscal_quarter: str | None = None, workloads: list[str] | None = None,
) -> dict:
    """Return saved publications, observations, and a separate current-quarter Today point."""
    from app.models import U2CSnapshot
    from app.services.u2c_snapshot import (
        current_fiscal_quarter, get_attainment_trend,
    )

    fq = fiscal_quarter or current_fiscal_quarter()
    snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
    if not snapshot:
        return {'fiscal_quarter': fq, 'message': f'No U2C snapshot exists for {fq}.'}

    points = get_attainment_trend(snapshot.id, include_live=True, workloads=workloads)
    if not points:
        return {
            'fiscal_quarter': fq,
            'message': (
                f'No history stored yet for {fq}. It fills in as Sales Buddy '
                'refreshes MSXi data.'
            ),
        }

    return {
        'fiscal_quarter': fq,
        'is_final': snapshot.is_final,
        'msxi_version': snapshot.msxi_version,
        'points': points,
        'latest_u2c_pct': points[-1]['u2c_pct'],
    }


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


# -- Recently Viewed ---------------------------------------------------------

@tool(
    'get_recently_viewed',
    'Get the most recently viewed entities, optionally filtered by type. '
    'Useful for "what was I looking at?", "my recent milestones", '
    '"what did I view today?", or "show me what I was working on".',
    {
        'type': 'object',
        'properties': {
            'entity_type': {
                'type': 'string',
                'description': 'Filter to a specific entity type: customer, milestone, '
                               'engagement, opportunity, note, seller, partner, territory, '
                               'pod, project, solution_engineer, topic. '
                               'Omit for all types.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results per entity type (default 10).',
            },
            'days': {
                'type': 'integer',
                'description': 'Only include views from the last N days (default: no limit).',
            },
        },
    },
)
def get_recently_viewed(
    entity_type: str | None = None,
    limit: int = 10,
    days: int | None = None,
) -> list[dict]:
    """Query usage_events for recently viewed entities."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func
    from app.models import db, UsageEvent

    query = (
        db.session.query(
            UsageEvent.entity_type,
            UsageEvent.entity_id,
            func.max(UsageEvent.timestamp).label('last_viewed'),
            func.count().label('view_count'),
        )
        .filter(
            UsageEvent.entity_type.isnot(None),
            UsageEvent.entity_id.isnot(None),
            UsageEvent.method == 'GET',
            UsageEvent.status_code == 200,
        )
    )

    if entity_type:
        query = query.filter(UsageEvent.entity_type == entity_type.lower())

    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(UsageEvent.timestamp >= cutoff)

    rows = (
        query
        .group_by(UsageEvent.entity_type, UsageEvent.entity_id)
        .order_by(func.max(UsageEvent.timestamp).desc())
        .limit(limit)
        .all()
    )

    results = []
    for etype, eid, last_viewed, view_count in rows:
        entry = {
            'entity_type': etype,
            'entity_id': eid,
            'last_viewed': last_viewed.isoformat() if last_viewed else None,
            'view_count': view_count,
        }
        # Resolve display name if possible
        if etype in _ENTITY_PATHS:
            entry['url'] = _url(etype, eid)
        _enrich_recently_viewed(entry, etype, eid)
        results.append(entry)

    return results


def _enrich_recently_viewed(entry: dict, etype: str, eid: int) -> None:
    """Add display name and customer context to a recently-viewed entry."""
    from app.models import (db, Customer, Milestone, Engagement, Note,
                            Seller, Partner, Territory, Topic)
    _resolvers = {
        'customer': (Customer, 'name'),
        'milestone': (Milestone, 'display_text'),
        'engagement': (Engagement, 'title'),
        'note': (Note, 'content'),
        'seller': (Seller, 'name'),
        'partner': (Partner, 'name'),
        'territory': (Territory, 'name'),
        'topic': (Topic, 'name'),
    }
    try:
        from app.models import Opportunity
        _resolvers['opportunity'] = (Opportunity, 'name')
    except ImportError:
        pass

    resolver = _resolvers.get(etype)
    if not resolver:
        return
    model_cls, attr = resolver
    obj = db.session.get(model_cls, eid)
    if not obj:
        entry['name'] = f'[deleted {etype} #{eid}]'
        return
    name = getattr(obj, attr, None) or f'{etype} #{eid}'
    if attr == 'content' and len(name) > 80:
        name = name[:77] + '...'
    elif len(name) > 60:
        name = name[:57] + '...'
    entry['name'] = _clean(name)
    if hasattr(obj, 'customer') and obj.customer:
        entry['customer_name'] = obj.customer.name


# -- Recently Updated --------------------------------------------------------

@tool(
    'get_recently_updated',
    'Get the most recently updated entities by type. Uses the updated_at '
    'timestamp on each entity (not telemetry). Useful for "what changed recently?", '
    '"latest milestone updates", or "recently modified engagements".',
    {
        'type': 'object',
        'properties': {
            'entity_type': {
                'type': 'string',
                'description': 'Filter to a specific entity type: customer, milestone, '
                               'engagement, opportunity. Omit for all types.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results per entity type (default 10).',
            },
            'days': {
                'type': 'integer',
                'description': 'Only include updates from the last N days (default: no limit).',
            },
        },
    },
)
def get_recently_updated(
    entity_type: str | None = None,
    limit: int = 10,
    days: int | None = None,
) -> list[dict]:
    """Query entities by updated_at descending."""
    from datetime import datetime, timezone, timedelta
    from app.models import (db, Customer, Milestone, Engagement)

    _types = {
        'customer': (Customer, 'name'),
        'milestone': (Milestone, 'display_text'),
        'engagement': (Engagement, 'title'),
    }
    try:
        from app.models import Opportunity
        _types['opportunity'] = (Opportunity, 'name')
    except ImportError:
        pass

    if entity_type:
        entity_type = entity_type.lower()
        _types = {k: v for k, v in _types.items() if k == entity_type}

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    results = []
    for etype, (model_cls, attr) in _types.items():
        if not hasattr(model_cls, 'updated_at'):
            continue
        q = model_cls.query.order_by(model_cls.updated_at.desc())
        if cutoff:
            q = q.filter(model_cls.updated_at >= cutoff)
        rows = q.limit(limit).all()
        for obj in rows:
            name = getattr(obj, attr, None) or f'{etype} #{obj.id}'
            if len(name) > 60:
                name = name[:57] + '...'
            entry = {
                'entity_type': etype,
                'entity_id': obj.id,
                'name': _clean(name),
                'updated_at': obj.updated_at.isoformat() if obj.updated_at else None,
            }
            if etype in _ENTITY_PATHS:
                entry['url'] = _url(etype, obj.id)
            if hasattr(obj, 'customer') and obj.customer:
                entry['customer_name'] = obj.customer.name
            results.append(entry)

    # Sort all results by updated_at descending
    results.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
    return results[:limit]
