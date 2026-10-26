"""Code-owned dashboard starting points, copied into ordinary user-owned dashboards."""
import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.models import Dashboard, DashboardTemplateInstallation, DashboardWidget, db
from app.services.dashboard_filters import normalize_filter_config, normalize_filters
from app.services.dashboards import get_variant, validate_footprint

logger = logging.getLogger(__name__)

DASHBOARD_TEMPLATES = {
    'connect-goals': {
        'key': 'connect-goals',
        'version': 1,
        'name': 'Connect Goals',
        'subtitle': 'See where the work stands, then open the report that helps move it.',
        'description': (
            'Track quarterly U2C progression, milestone team and HVA coverage, '
            'and Fabric and database whitespace wins.'
        ),
        'content_width': 'wide',
        'display_style': 'dividers',
        'widgets': (
            ('connect_goals.u2c', 'detailed', 0, 0, 12, 5),
            ('connect_goals.team_coverage', 'detailed', 0, 5, 6, 6),
            ('connect_goals.hva_coverage', 'detailed', 6, 5, 6, 6),
            ('connect_goals.whitespace', 'detailed', 0, 11, 12, 9),
        ),
    },
    'fy27-data-motions': {
        'key': 'fy27-data-motions',
        'version': 1,
        'name': 'FY27 Data Motions',
        'subtitle': 'Track and action your FY27 goals and proactively engage your customers',
        'description': (
            'Find Fabric and database acquisition, expansion and modernization prospects, '
            'and upcoming renewals. The retained SQL 2016 discovery data is incorrect.'
        ),
        'content_width': 'wide',
        'display_style': 'cards',
        'filter_config': {
            'scope': {'customer_ids': [], 'seller_ids': []},
            'live_filters': ['seller_ids', 'group_by_seller'],
        },
        'widgets': (
            ('fabric.modernization', 'detailed', 0, 0, 6, 5),
            ('fabric.acquisition', 'detailed', 6, 0, 6, 5),
            ('fabric.expansion', 'detailed', 0, 5, 6, 5),
            ('fabric.trial_conversion', 'detailed', 6, 5, 6, 5),
            ('ptof.upcoming_renewals', 'detailed', 0, 10, 6, 5),
            ('fabric.rti', 'detailed', 6, 10, 6, 5),
            ('database.whitespace', 'detailed', 0, 15, 6, 5),
            ('database.expansion', 'detailed', 6, 15, 6, 5),
            ('sql.eos', 'detailed', 0, 20, 6, 8),
            ('sql.renewals', 'detailed', 6, 20, 6, 8),
        ),
    },
}


def list_dashboard_templates() -> list[dict[str, Any]]:
    """Describe available starting points without running widget calculations."""
    return [
        {key: value for key, value in template.items() if key != 'widgets'}
        | {'widget_count': len(template['widgets'])}
        for template in DASHBOARD_TEMPLATES.values()
    ]


def create_dashboard_from_template(key: str) -> Dashboard:
    """Stage an independent editable copy; the caller owns the transaction."""
    template = DASHBOARD_TEMPLATES.get(key)
    if template is None:
        raise ValueError('Choose an available dashboard template.')
    dashboard = Dashboard(filter_config=normalize_filter_config(template.get('filter_config')), **{
        field: template[field] for field in (
            'name', 'subtitle', 'content_width', 'display_style',
        )
    })
    for order, (widget_key, variation, column, row, width, height) in enumerate(template['widgets']):
        validate_footprint(get_variant(widget_key, variation), width, height)
        dashboard.widgets.append(DashboardWidget(
            widget_key=widget_key, variant_key=variation, sort_order=order,
            layout_column=column, layout_row=row, column_span=width, row_span=height,
        ))
    db.session.add(dashboard)
    return dashboard


def _matches_template(dashboard: Dashboard, template: dict[str, Any]) -> bool:
    """Adopt an exact pre-release copy without renaming or rearranging user work."""
    if any(getattr(dashboard, field) != template[field] for field in (
        'name', 'subtitle', 'content_width', 'display_style',
    )):
        return False
    if (normalize_filter_config(dashboard.filter_config)
            != normalize_filter_config(template.get('filter_config'))
            or any(normalize_filters(widget.filters) != normalize_filters()
                   for widget in dashboard.widgets)):
        return False
    placements = sorted(
        (widget.widget_key, widget.variant_key, widget.layout_column,
         widget.layout_row, widget.column_span, widget.row_span)
        for widget in dashboard.widgets
    )
    return placements == sorted(template['widgets'])


def initialize_dashboard_templates() -> None:
    """Install each starter once, under the application's database startup lock."""
    try:
        for key, template in DASHBOARD_TEMPLATES.items():
            if db.session.get(DashboardTemplateInstallation, key) is not None:
                continue
            dashboard = next((
                candidate for candidate in Dashboard.query.filter_by(
                    name=template['name'],
                ).order_by(Dashboard.id).all()
                if _matches_template(candidate, template)
            ), None)
            if dashboard is None:
                dashboard = create_dashboard_from_template(key)
            db.session.add(DashboardTemplateInstallation(
                key=key, version=template['version'], dashboard=dashboard,
            ))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Could not initialize starter dashboards')
        raise


def get_template_dashboard_url(key: str) -> str:
    """Resolve an installed starter without recreating a deleted dashboard on reads."""
    installation = db.session.get(DashboardTemplateInstallation, key)
    if installation is not None and installation.dashboard is not None:
        return f'/reports/dashboards/{installation.dashboard_id}'
    return f'/reports/dashboards?template={key}'
