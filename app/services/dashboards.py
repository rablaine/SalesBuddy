"""Saved dashboards and the reusable, code-owned widget catalog.

Definitions own calculations and presentation. DashboardWidget rows own only
placement, so the same definition can be reused without copying report logic.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import logging
from typing import Any

from app.models import Dashboard, DashboardWidget, db, utc_now
from app.services import connect_goals
from app.services.request_cache import shared_read_scope
from app.services.activity_coverage import get_meeting_activity_coverage_widget
from app.services.fabric_consumption import (
    get_fabric_capacity_pressure_widget, get_fabric_expansion_widget,
    get_fabric_trial_conversion_widget,
)
from app.services.hygiene import get_hygiene_gaps_widget
from app.services.fabric_modernization import get_fabric_modernization_widget
from app.services.fabric_acquisition import get_fabric_acquisition_widget
from app.services.whitespace import get_database_whitespace_widget
from app.services.rti import get_rti_widget
from app.services.sql_motions import get_sql_eos_widget, get_sql_renewals_widget
from app.services.se_specialty import has_specialty_access
from app.services.manager_one_on_one import get_initiative_tracker_widget
from app.services.ptof import get_ptof_renewals_widget
from app.services.revenue_analysis import (
    get_database_expansion_widget, get_revenue_attention_widget,
)
from app.services.dashboard_filters import (
    FILTER_LABELS, LIVE_FILTER_LABELS, normalize_filters, normalize_live_filters,
    normalize_filter_config,
    resolve_customer_scope,
    scope_report_destination,
    describe_widget_filters,
)

logger = logging.getLogger(__name__)
MAX_DASHBOARD_NAME = 200
MAX_DASHBOARD_SUBTITLE = 500
CANVAS_WIDTHS = {'standard': 1152, 'wide': 1344}
DISPLAY_STYLES = ('cards', 'dividers')
GRID_COLUMNS = 12
GRID_ROW_HEIGHT = 64
GRID_GAP = 12
GRID_PADDING = 16
MAX_LAYOUT_ROWS = 1000
MIN_WIDGET_HEIGHT = 2
MAX_WIDGET_HEIGHT = 20


class LayoutConflict(ValueError):
    """A stale dashboard draft must not overwrite a newer saved layout."""


@dataclass(frozen=True)
class WidgetVariant:
    """A deliberately designed presentation with supported grid footprints."""

    key: str
    title: str
    template: str
    widths: tuple[int, ...]
    min_height: int
    default_height: int


def _connect_variants(template: str) -> tuple[WidgetVariant, ...]:
    """Share metric and detailed options without duplicating report calculations."""
    return (
        WidgetVariant(
            'metric', 'Metric', 'partials/widgets/connect_metric.html',
            (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
        ),
        WidgetVariant('detailed', 'Detailed', template, (6, 12), MIN_WIDGET_HEIGHT, 8),
    )


def _report_variants(template: str) -> tuple[WidgetVariant, ...]:
    """Offer compact summaries and full investigative queues using the same report data."""
    return (
        WidgetVariant('metric', 'Summary', template, (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4),
        WidgetVariant('detailed', 'Details', template, (6, 12), MIN_WIDGET_HEIGHT, 8),
    )


@dataclass(frozen=True)
class WidgetDefinition:
    """A stable widget identity with a shared report-backed data provider."""

    key: str
    title: str
    description: str
    report: str
    variants: tuple[WidgetVariant, ...]
    provider: Callable[..., dict[str, Any]]
    requirements: tuple[str, ...] = ()
    scope: str = 'FY27 Data'
    supported_filters: tuple[str, ...] = ()
    specialty: str | None = None


WIDGET_REQUIREMENTS = {
    'compensation_buckets': lambda reference: connect_goals.get_setup_state(reference),
}


def get_widget_requirements(
    definition: WidgetDefinition, reference: date | None = None,
    states: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check declared prerequisites before invoking a widget's data provider."""
    states = states if states is not None else {}
    missing = []
    for key in definition.requirements:
        if key not in states:
            states[key] = WIDGET_REQUIREMENTS[key](reference)
        if not states[key]['ready']:
            missing.append({'key': key, 'setup': states[key]})
    return missing


def get_widget_readiness(
    definition: WidgetDefinition, reference: date | None = None,
    states: dict | None = None,
) -> tuple[list[dict[str, Any]], dict | None]:
    """Load missing choices first, then collect input before syncing widget data."""
    from app.services.dashboard_sync import WIDGET_SOURCES, get_source_sync

    if definition.specialty and not has_specialty_access(definition.specialty):
        return [], None
    states = states if states is not None else {}
    requirements = get_widget_requirements(definition, reference, states)
    sources = WIDGET_SOURCES.get(definition.key, ())
    if requirements:
        if any(item['setup']['available_buckets'] for item in requirements):
            return requirements, None
        sources = ('revenue',)
    cache_key = ('sync', sources)
    if cache_key not in states:
        states[cache_key] = get_source_sync(sources, reference)
    sync = states[cache_key]
    return ([] if sync else requirements), sync


WIDGET_DEFINITIONS = {
    definition.key: definition for definition in (
        WidgetDefinition(
            'connect_goals.u2c', 'Quarterly U2C progression',
            'Quarterly commitment progress, pace, and trends.',
            'U2C Attainment', _connect_variants('partials/widgets/connect_u2c.html'),
            connect_goals.get_u2c_widget,
            ('compensation_buckets',),
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'connect_goals.team_coverage', 'Milestone team coverage',
            'ACR-weighted team coverage and off-team milestones.',
            'Milestone Tracker', _connect_variants('partials/widgets/connect_team_coverage.html'),
            connect_goals.get_team_coverage_widget,
            ('compensation_buckets',),
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'connect_goals.hva_coverage', 'Milestone HVA coverage',
            'Milestone activity coverage and technical validations.',
            'Activity Coverage', _connect_variants('partials/widgets/connect_hva_coverage.html'),
            connect_goals.get_hva_coverage_widget,
            ('compensation_buckets',),
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'connect_goals.whitespace', 'Fabric and database whitespace wins',
            'Sustained wins, ramping customers, and consumption trends.',
            'Whitespace Analysis', _connect_variants('partials/widgets/connect_whitespace.html'),
            connect_goals.get_whitespace_widget,
            ('compensation_buckets',),
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'initiative_tracker.overview', 'Initiative Tracker',
            'Initiative ACR totals and seller breakdowns.',
            'Initiative Tracker', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/initiative_tracker.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
                WidgetVariant(
                    'detailed', 'By seller', 'partials/widgets/initiative_tracker.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 8,
                ),
            ),
            get_initiative_tracker_widget, scope='All initiatives',
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'hygiene.gaps', 'Hygiene gaps',
            'Active engagements and milestones with missing links.',
            'Hygiene', _report_variants('partials/widgets/hygiene_gaps.html'),
            get_hygiene_gaps_widget, scope='Active work', supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'activity_coverage.meetings', 'Meeting activity coverage',
            'Meeting coverage and activities ready to log.',
            'Activity Coverage', _report_variants('partials/widgets/meeting_activity_coverage.html'),
            get_meeting_activity_coverage_widget, scope='Fiscal year to date',
            supported_filters=tuple(FILTER_LABELS),
        ),
        WidgetDefinition(
            'revenue.attention', 'Revenue risk / opportunity',
            'Unreviewed revenue risks and expansion opportunities.',
            'Revenue Analyzer', _report_variants('partials/widgets/revenue_attention.html'),
            get_revenue_attention_widget, scope='Saved unreviewed analysis',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'database.expansion', 'Database Expansion',
            'Prioritized, unreviewed database growth opportunities and consumption risks '
            'from Revenue Analyzer. Monitor-only signals are excluded.',
            'Revenue Analyzer / Databases', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/revenue_attention.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/revenue_attention.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_database_expansion_widget, scope='Saved database outreach / priority 20+',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'), specialty='data',
        ),
        WidgetDefinition(
            'ptof.upcoming_renewals', 'Upcoming P-to-F renewals',
            '90-day renewal summary or all future renewals.',
            'P to F', _report_variants('partials/widgets/ptof_renewals.html'),
            get_ptof_renewals_widget, scope='90-day summary / all future details',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'fabric.acquisition', 'Fabric acquisition',
            'At least $2,000/mo across AI, Databases and non-Fabric Analytics, '
            'with no positive Fabric ACR.',
            'Whitespace Analysis / Fabric Acquisition', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/fabric_acquisition.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/fabric_acquisition.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_fabric_acquisition_widget, scope='Avg monthly ACR / current + previous 3 months',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'), specialty='data',
        ),
        WidgetDefinition(
            'database.whitespace', 'Database whitespace',
            'Customers without Databases coverage and with AI, Analytics or Apps spend '
            'in the existing Whitespace Analysis grid, ranked by that monthly ACR.',
            'Whitespace Analysis', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/database_whitespace.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/database_whitespace.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_database_whitespace_widget, scope='Saved whitespace grid / recent pillar ACR',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'), specialty='data',
        ),
        WidgetDefinition(
            'fabric.rti', 'Real-Time Intelligence',
            'Customers without RTI usage who consume Event Hubs, Stream Analytics or IoT, '
            'including Fabric entry-point prospects. Missing RTI counts as zero.',
            'Real-Time Intelligence', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/rti.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/rti.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_rti_widget, scope='RTI / rolling 28 days; ACR / last 3 complete months',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'sql.renewals', 'SQL Renewals',
            'Upcoming SQL expirations, nearest first, including unknown renewal amounts. '
            'One shared SQL Motions sync also loads SQL 2016 discovery.',
            'SQL Renewals', _report_variants('partials/widgets/sql_motion.html'),
            get_sql_renewals_widget, scope='Upcoming dates / saved Azure Migrations snapshot',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'), specialty='data',
        ),
        WidgetDefinition(
            'sql.eos', 'SQL 2016 EOS / ESU (data incorrect)',
            'Known incorrect data. Do not rely on the SQL 2016 counts or customer lists. '
            'Retained for reference; the shared SQL Motions sync also loads renewals.',
            'SQL 2016 EOS / ESU', _report_variants('partials/widgets/sql_motion.html'),
            get_sql_eos_widget, scope='SQL Server 2016 / Arc All Discovered',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'), specialty='data',
        ),
        WidgetDefinition(
            'fabric.modernization', 'Fabric modernization',
            'Separate Other Analytics and ADF spend, with Synapse indicators, for customers '
            'whose combined source spend exceeds Fabric.',
            'Fabric Modernization', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/fabric_modernization.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/fabric_modernization.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_fabric_modernization_widget, scope='Avg monthly ACR / last 3 complete months',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'fabric.expansion', 'Fabric expansion',
            'Power BI-led Fabric customers to explore beyond BI.',
            'Fabric Consumption / Capacity overview', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/fabric_expansion.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/fabric_expansion.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_fabric_expansion_widget, scope='Paid F-SKU customers / 28-day workload mix',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'fabric.trial_conversion', 'Trial to Paid',
            'Customers with trial capacities and no paid F SKU, with trial engagement queues.',
            'Fabric Consumption / Trial to Paid audit', (
                WidgetVariant(
                    'metric', 'Summary', 'partials/widgets/fabric_trial_conversion.html',
                    (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
                ),
                WidgetVariant(
                    'detailed', 'Details', 'partials/widgets/fabric_trial_conversion.html',
                    (6, 12), MIN_WIDGET_HEIGHT, 5,
                ),
            ),
            get_fabric_trial_conversion_widget, scope='Saved Fabric 360 capacities',
            supported_filters=(*FILTER_LABELS, 'group_by_seller'),
        ),
        WidgetDefinition(
            'fabric.capacity_pressure', 'Fabric capacity pressure',
            'Capacity pressure counts and the top five needing attention.',
            'Fabric Capacity Pressure',
            _report_variants('partials/widgets/fabric_capacity_pressure.html'),
            get_fabric_capacity_pressure_widget, scope='Saved capacity measurements',
            supported_filters=tuple(FILTER_LABELS),
        ),
    )
}


def get_widget_catalog() -> list[dict[str, Any]]:
    """Describe selectable widgets without calculating their values."""
    return [
        {
            'key': definition.key,
            'title': definition.title,
            'description': definition.description,
            'scope': definition.scope,
            'report': definition.report,
            'requirements': definition.requirements,
            'supported_filters': definition.supported_filters,
            'variants': [
                {
                    'key': variant.key, 'title': variant.title,
                    'widths': variant.widths, 'min_height': variant.min_height,
                    'default_height': variant.default_height,
                }
                for variant in definition.variants
            ],
        }
        for definition in WIDGET_DEFINITIONS.values()
        if not definition.specialty or has_specialty_access(definition.specialty)
    ]


def validate_dashboard_name(value: object) -> str:
    """Require a readable name without silently truncating user input."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_DASHBOARD_NAME:
        raise ValueError('Enter a dashboard name of 1 to 200 characters.')
    return value.strip()


def validate_dashboard_subtitle(value: object) -> str:
    """Allow an empty subtitle without silently truncating descriptive text."""
    if not isinstance(value, str) or len(value.strip()) > MAX_DASHBOARD_SUBTITLE:
        raise ValueError('Enter a dashboard subtitle of up to 500 characters, or leave it blank.')
    return value.strip()


def get_dashboard_links() -> list[dict[str, Any]]:
    """List report destinations without loading widget instances or calculating metrics."""
    return [
        {'id': dashboard.id, 'name': dashboard.name,
         'url': f'/reports/dashboards/{dashboard.id}'}
        for dashboard in db.session.execute(
            db.select(Dashboard.id, Dashboard.name).order_by(Dashboard.name, Dashboard.id),
        )
    ]


def list_dashboards() -> list[dict[str, Any]]:
    """Return saved dashboard summaries without running their widget queries."""
    return [
        {
            'id': dashboard.id,
            'name': dashboard.name,
            'subtitle': dashboard.subtitle,
            'widget_count': len(dashboard.widgets),
            'content_width': dashboard.content_width,
            'display_style': dashboard.display_style,
            'created_at': dashboard.created_at,
            'updated_at': dashboard.updated_at,
            'url': f'/reports/dashboards/{dashboard.id}',
        }
        for dashboard in Dashboard.query.order_by(Dashboard.name, Dashboard.id).all()
    ]


@shared_read_scope()
def get_dashboard_data(
    dashboard: Dashboard, reference: date | None = None,
    *, live_filters: object = None,
) -> dict[str, Any]:
    """Check each definition's prerequisites before calculating its saved instances."""
    states: dict[str, dict[str, Any]] = {}
    values: dict[tuple, dict[str, Any]] = {}
    filter_config = normalize_filter_config(dashboard.filter_config)
    live = normalize_live_filters(live_filters)
    if any(
        live.get(key) and key not in filter_config['live_filters'] for key in LIVE_FILTER_LABELS
    ):
        raise ValueError('This dashboard has not enabled that live filter.')
    widgets = []
    for instance in sorted(dashboard.widgets, key=lambda item: (
        item.layout_row, item.layout_column, item.id,
    )):
        definition = WIDGET_DEFINITIONS.get(instance.widget_key)
        variant = next((
            item for item in definition.variants if item.key == instance.variant_key
        ), None) if definition else None
        requirements, sync = (
            get_widget_readiness(definition, reference, states) if definition else ([], None)
        )
        if definition is None:
            logger.warning('Unknown dashboard widget %s: %s', instance.id, instance.widget_key)
        filters = normalize_filters(instance.filters)
        layers = (filter_config['scope'], filters, live)
        supported = definition.supported_filters if definition else ()
        effective_layers = [
            {key: layer[key] if key in supported else [] for key in FILTER_LABELS}
            for layer in layers
        ]
        customer_ids = resolve_customer_scope(*effective_layers)
        cache_key = (instance.widget_key, customer_ids)
        if definition and variant and not requirements and not sync and cache_key not in values:
            values[cache_key] = scope_report_destination(
                definition.provider(reference, customer_ids=customer_ids)
                if customer_ids is not None else definition.provider(reference),
                definition.key, customer_ids,
            )
        widgets.append({
            'id': instance.id,
            'key': instance.widget_key,
            'sort_order': instance.sort_order,
            'title': definition.title if definition else 'Unavailable widget',
            'variant_key': instance.variant_key,
            'layout_column': instance.layout_column,
            'layout_row': instance.layout_row,
            'column_span': instance.column_span,
            'row_span': instance.row_span,
            'template': variant.template if variant else None,
            'data': values.get(cache_key) if variant else None,
            'filters': filters,
            'group_by_seller': (
                live.get('group_by_seller', False) and 'group_by_seller' in supported
            ),
            'filter_info': {
                'supported': supported,
                'widget_scope': describe_widget_filters(filters),
                'ignored': [
                    FILTER_LABELS[key] for key in FILTER_LABELS
                    if key not in supported and any(layer[key] for layer in layers)
                ],
                'scoped': customer_ids is not None,
            },
            'requirements': requirements if variant else [],
            'sync': sync if variant else None,
            'message': (
                'This widget is no longer in the catalog. Remove it in Manage Dashboards.'
                if definition is None else
                'This presentation is no longer available. Update it in Manage Dashboards.'
                if variant is None else
                'Confirm your compensation buckets to see this widget.'
                if requirements else None
            ),
        })
    return {
        'id': dashboard.id,
        'name': dashboard.name,
        'subtitle': dashboard.subtitle,
        'created_at': dashboard.created_at,
        'updated_at': dashboard.updated_at,
        'content_width': dashboard.content_width,
        'display_style': dashboard.display_style,
        'canvas_width': CANVAS_WIDTHS[dashboard.content_width],
        'layout_revision': dashboard.layout_revision,
        'filter_config': filter_config,
        'live_filters': live,
        'supported_filters': [
            key for key in LIVE_FILTER_LABELS if any(
                key in WIDGET_DEFINITIONS[item.widget_key].supported_filters
                for item in dashboard.widgets if item.widget_key in WIDGET_DEFINITIONS
            )
        ],
        'canvas_rows': max(
            (item.layout_row + item.row_span for item in dashboard.widgets), default=4,
        ),
        'widgets': widgets,
        'setup': states.get('compensation_buckets'),
        'url': f'/reports/dashboards/{dashboard.id}',
    }


def get_variant(key: object, variant_key: object) -> WidgetVariant:
    """Resolve only code-owned definitions and supported presentation keys."""
    if not isinstance(key, str) or key not in WIDGET_DEFINITIONS:
        raise ValueError('Choose a widget from the catalog.')
    variant = next((
        item for item in WIDGET_DEFINITIONS[key].variants if item.key == variant_key
    ), None)
    if variant is None:
        raise ValueError('Choose a supported widget variation.')
    return variant


def validate_footprint(variant: WidgetVariant, width: object, height: object) -> None:
    """Reject invalid sizes rather than silently squeezing or expanding widgets."""
    if type(width) is not int or width not in variant.widths:
        raise ValueError('Choose a supported width for this widget variation.')
    if type(height) is not int or not variant.min_height <= height <= MAX_WIDGET_HEIGHT:
        raise ValueError(
            f'Widget height must be {variant.min_height} to {MAX_WIDGET_HEIGHT} grid rows.',
        )


def advance_layout_revision(
    dashboard: Dashboard, expected: int, expected_details: tuple[str, str] | None = None,
) -> None:
    """Acquire the layout revision inside the same transaction as its changes."""
    statement = db.update(Dashboard).where(
        Dashboard.id == dashboard.id, Dashboard.layout_revision == expected,
    )
    if expected_details is not None:
        statement = statement.where(
            Dashboard.name == expected_details[0], Dashboard.subtitle == expected_details[1],
        )
    result = db.session.execute(statement.values(
        layout_revision=expected + 1, updated_at=utc_now(),
    ))
    if result.rowcount != 1:
        raise LayoutConflict('This dashboard changed in another window. Reload before editing.')


def add_dashboard_widget(
    dashboard: Dashboard, key: object, variant_key: object = None,
    column_span: object = None, row_span: object = None,
    filters: object = None,
) -> DashboardWidget:
    """Append below existing widgets without packing holes or moving neighbors."""
    if variant_key is None and isinstance(key, str) and key in WIDGET_DEFINITIONS:
        variants = WIDGET_DEFINITIONS[key].variants
        variant_key = next(
            (variant.key for variant in variants if variant.key == 'detailed'), variants[0].key,
        )
    variant = get_variant(key, variant_key)
    width = 12 if column_span is None else column_span
    height = variant.default_height if row_span is None else row_span
    validate_footprint(variant, width, height)
    filters = validate_widget_filters(key, filters)
    db.session.flush()
    row = max((item.layout_row + item.row_span for item in dashboard.widgets), default=0)
    if row + height > MAX_LAYOUT_ROWS:
        raise ValueError('No space below the current widgets. Remove or reposition a widget.')
    advance_layout_revision(dashboard, dashboard.layout_revision)
    instance = DashboardWidget(
        widget_key=key, variant_key=variant.key, column_span=width, row_span=height,
        filters=filters,
        layout_row=row, layout_column=0,
        sort_order=max((widget.sort_order for widget in dashboard.widgets), default=-1) + 1,
    )
    dashboard.widgets.append(instance)
    dashboard.updated_at = utc_now()
    return instance


def rectangles_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Detect occupied-cell intersections, allowing adjacent edges."""
    return (
        first['layout_column'] < second['layout_column'] + second['column_span']
        and second['layout_column'] < first['layout_column'] + first['column_span']
        and first['layout_row'] < second['layout_row'] + second['row_span']
        and second['layout_row'] < first['layout_row'] + first['row_span']
    )


def _validate_dashboard_layout(
    dashboard: Dashboard, payload: object, replace_widgets: bool = False,
) -> tuple[int, str, str, list[dict[str, Any]], dict[int, DashboardWidget]]:
    """Validate a complete snapshot before changing metadata or widget membership."""
    if not isinstance(payload, dict) or not isinstance(payload.get('widgets'), list):
        raise ValueError('Provide a complete dashboard layout.')
    revision = payload.get('revision')
    if type(revision) is not int or revision != dashboard.layout_revision:
        raise LayoutConflict('This dashboard changed in another window. Reload before editing.')
    width = payload.get('content_width')
    if not isinstance(width, str) or width not in CANVAS_WIDTHS:
        raise ValueError('Choose Standard or Wide dashboard width.')
    display_style = payload.get('display_style', dashboard.display_style)
    if not isinstance(display_style, str) or display_style not in DISPLAY_STYLES:
        raise ValueError('Choose Cards or Dividers dashboard display.')
    saved = {item.id: item for item in dashboard.widgets}
    entries = payload['widgets']
    ids = [item.get('id') for item in entries if isinstance(item, dict)]
    if (len(ids) != len(entries) or any(type(item) is not int or item == 0 for item in ids)
            or len(ids) != len(set(ids))
            or (not replace_widgets and set(ids) != set(saved))):
        raise ValueError('The layout must contain each widget on this dashboard exactly once.')
    for index, entry in enumerate(entries):
        if 'filters' in entry and not isinstance(entry['filters'], dict):
            raise ValueError('Provide valid widget filter selections.')
        instance = saved.get(entry['id'])
        if instance is None and entry['id'] > 0:
            raise ValueError('A widget does not belong to this dashboard.')
        key = instance.widget_key if instance is not None else entry.get('key')
        validate_widget_filters(
            key, entry.get('filters', instance.filters if instance is not None else None),
        )
        if replace_widgets and instance is not None and entry.get('key') != key:
            raise ValueError('An existing widget cannot change its report.')
        if instance is None or key in WIDGET_DEFINITIONS:
            variant = get_variant(key, entry.get('variant_key'))
        else:
            if entry.get('variant_key') != instance.variant_key:
                raise ValueError('An unavailable widget cannot change its variation.')
            variant = WidgetVariant(instance.variant_key, 'Unavailable', '', (3, 4, 6, 12), 3, 8)
        validate_footprint(variant, entry.get('column_span'), entry.get('row_span'))
        column, row = entry.get('layout_column'), entry.get('layout_row')
        if (type(column) is not int or type(row) is not int or column < 0 or row < 0
                or column + entry['column_span'] > GRID_COLUMNS
                or row + entry['row_span'] > MAX_LAYOUT_ROWS):
            raise ValueError('Place every widget inside the dashboard grid.')
        if any(rectangles_overlap(entry, other) for other in entries[:index]):
            raise ValueError('Widgets cannot overlap. Move or resize the highlighted widget.')
    return revision, width, display_style, entries, saved


def _assign_widget_layout(instance: DashboardWidget, entry: dict[str, Any]) -> None:
    """Apply already validated placement fields to one widget."""
    for field in ('layout_column', 'layout_row', 'column_span', 'row_span', 'variant_key'):
        setattr(instance, field, entry[field])
    if 'filters' in entry:
        instance.filters = validate_widget_filters(instance.widget_key, entry['filters'])


def save_dashboard_layout(dashboard: Dashboard, payload: object) -> None:
    """Save geometry for existing API callers without changing widget membership."""
    revision, width, display_style, entries, saved = _validate_dashboard_layout(dashboard, payload)
    if 'filter_config' in payload and not isinstance(payload['filter_config'], dict):
        raise ValueError('Provide a valid dashboard filter configuration.')
    filters = normalize_filter_config(payload.get('filter_config', dashboard.filter_config))
    advance_layout_revision(dashboard, revision)
    dashboard.content_width = width
    dashboard.display_style = display_style
    dashboard.filter_config = filters
    for entry in entries:
        _assign_widget_layout(saved[entry['id']], entry)


def save_dashboard_draft(dashboard: Dashboard, payload: object) -> None:
    """Publish metadata, membership, and geometry together with stale-draft protection."""
    if not isinstance(payload, dict):
        raise ValueError('Provide a complete dashboard draft.')
    if 'filter_config' in payload and not isinstance(payload['filter_config'], dict):
        raise ValueError('Provide a valid dashboard filter configuration.')
    name = validate_dashboard_name(payload.get('name'))
    subtitle = validate_dashboard_subtitle(payload.get('subtitle'))
    filters = normalize_filter_config(payload.get('filter_config', dashboard.filter_config))
    if (payload.get('base_name') != dashboard.name
            or payload.get('base_subtitle') != dashboard.subtitle):
        raise LayoutConflict('The saved dashboard details changed. Your local draft was kept.')
    revision, width, style, entries, saved = _validate_dashboard_layout(
        dashboard, payload, replace_widgets=True,
    )
    advance_layout_revision(
        dashboard, revision, (payload['base_name'], payload['base_subtitle']),
    )
    dashboard.name, dashboard.subtitle = name, subtitle
    dashboard.content_width, dashboard.display_style = width, style
    dashboard.filter_config = filters
    retained = {entry['id'] for entry in entries}
    for widget_id, instance in saved.items():
        if widget_id not in retained:
            dashboard.widgets.remove(instance)
    for index, entry in enumerate(entries):
        instance = saved.get(entry['id'])
        if instance is None:
            instance = DashboardWidget(widget_key=entry['key'])
            dashboard.widgets.append(instance)
        instance.sort_order = index
        _assign_widget_layout(instance, entry)


def validate_widget_filters(key: str, filters: object) -> dict[str, list[int]]:
    """Reject unsupported instance filters instead of silently ignoring saved configuration."""
    filters = normalize_filters(filters)
    definition = WIDGET_DEFINITIONS.get(key) if isinstance(key, str) else None
    supported = definition.supported_filters if definition else ()
    if any(filters[name] and name not in supported for name in FILTER_LABELS):
        raise ValueError('This widget does not support the selected filter.')
    return filters


def get_widget_preview(
    key: str, variant_key: str, width: int, *, filter_context: object = None,
) -> dict[str, Any]:
    """Use live saved data and the same template as a placed widget, never sample numbers."""
    variant = get_variant(key, variant_key)
    validate_footprint(variant, width, variant.default_height)
    definition = WIDGET_DEFINITIONS[key]
    requirements, sync = get_widget_readiness(definition)
    if filter_context is None:
        filter_context = {}
    if not isinstance(filter_context, dict) or set(filter_context) - {'dashboard', 'widget', 'live'}:
        raise ValueError('Provide a valid widget filter context.')
    config = normalize_filter_config(filter_context.get('dashboard'))
    filters = validate_widget_filters(key, filter_context.get('widget'))
    live = normalize_live_filters(filter_context.get('live'))
    if any(live.get(name) and name not in config['live_filters'] for name in LIVE_FILTER_LABELS):
        raise ValueError('This dashboard has not enabled that live filter.')
    layers = (config['scope'], filters, live)
    ids = resolve_customer_scope(*[
        {name: layer[name] if name in definition.supported_filters else [] for name in FILTER_LABELS}
        for layer in layers
    ])
    return {
        'id': 'preview', 'key': key, 'title': definition.title,
        'template': variant.template, 'variant_key': variant.key,
        'column_span': width, 'row_span': variant.default_height,
        'data': scope_report_destination((
            definition.provider(None, customer_ids=ids) if ids is not None
            else definition.provider(None)
        ), key, ids) if not requirements and not sync else None,
        'filters': filters,
        'group_by_seller': (
            live.get('group_by_seller', False)
            and 'group_by_seller' in definition.supported_filters
        ),
        'filter_info': {
            'supported': definition.supported_filters, 'scoped': ids is not None,
            'widget_scope': describe_widget_filters(filters),
            'ignored': [
                FILTER_LABELS[name] for name in FILTER_LABELS
                if name not in definition.supported_filters and any(layer[name] for layer in layers)
            ],
        },
        'requirements': requirements,
        'sync': sync,
        'message': (
            'Confirm your compensation buckets to preview live values.'
            if requirements else None
        ),
    }
