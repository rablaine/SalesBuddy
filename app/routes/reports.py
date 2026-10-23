"""Reports blueprint - cross-domain reports hub and individual report views."""
import json
import logging
from datetime import datetime, timedelta, timezone, date
from flask import (
    Blueprint, Response, abort, current_app, render_template, url_for, jsonify, request, redirect,
)
from app.models import (
    db, Customer, Engagement, Note, Milestone, MilestoneAudit, Opportunity, Seller,
    SolutionEngineer, SyncStatus, MsxTask, PrefetchedMeeting,
    Topic, RevenueAnalysis, HygieneNote, CustomerRevenueData, ProductRevenueData,
    notes_engagements, notes_milestones, notes_topics,
    MarketingSummary, MarketingInteraction, MarketingContact,
    U2CSnapshot, U2CSnapshotItem,
    OneOnOneWorkspace, Territory, Project, Dashboard, DashboardWidget,
)
from sqlalchemy import func, desc, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

logger = logging.getLogger(__name__)

bp = Blueprint('reports', __name__)


def _read_sql_motion(motion: str) -> dict:
    """Validate report scope and apply the same specialty gate as its widgets."""
    from app.services.dashboard_filters import report_customer_scope
    from app.services.se_specialty import has_specialty_access
    from app.services.sql_motions import get_sql_motion_report

    if not has_specialty_access('data'):
        abort(403, description='SQL motions are available for Data SEs and sellers.')
    try:
        customer_ids = report_customer_scope(request.args)
    except ValueError as exc:
        abort(400, description=str(exc))
    return get_sql_motion_report(motion, customer_ids=customer_ids)


@bp.route('/reports/sql-renewals', defaults={'motion': 'renewals'}, endpoint='report_sql_renewals')
@bp.route('/reports/sql-eos', defaults={'motion': 'eos'}, endpoint='report_sql_eos')
def report_sql_motion(motion: str) -> str:
    """Show source-backed SQL renewal or SQL 2016 discovery candidates."""
    grouping = request.args.get('group_by_seller', 'false')
    if grouping not in ('true', 'false'):
        abort(400, description='Group by seller must be true or false.')
    report = _read_sql_motion(motion)
    return render_template(
        'report_sql_motion.html', report=report, title=report['title'],
        group_by_seller=grouping == 'true',
    )


@bp.route('/api/reports/sql-renewals', defaults={'motion': 'renewals'}, endpoint='api_sql_renewals')
@bp.route('/api/reports/sql-eos', defaults={'motion': 'eos'}, endpoint='api_sql_eos')
def api_sql_motion(motion: str) -> Response:
    """Expose the same saved SQL queues without starting a remote pull."""
    return jsonify(_read_sql_motion(motion))


@bp.route('/reports/fabric-modernization')
def report_fabric_modernization():
    """Compare recent modernization-source revenue with Fabric on the same period."""
    from app.services.fabric_modernization import get_fabric_modernization_report

    return render_template(
        'report_fabric_modernization.html', report=get_fabric_modernization_report(),
    )


@bp.route('/api/reports/fabric-modernization')
def api_fabric_modernization():
    """Expose the report's saved-data calculation without a separate query implementation."""
    from app.services.fabric_modernization import get_fabric_modernization_report

    return jsonify(get_fabric_modernization_report())


@bp.route('/reports/rti')
def report_rti() -> str:
    """Show streaming and IoT customers without RTI usage as Fabric prospects."""
    from app.services.rti import get_rti_report

    grouping = request.args.get('group_by_seller', 'true')
    if grouping not in ('true', 'false'):
        abort(400, description='Group by seller must be true or false.')
    return render_template(
        'report_rti.html', report=get_rti_report(), title='Real-Time Intelligence',
        group_by_seller=grouping == 'true',
    )


@bp.route('/api/reports/rti')
def api_rti() -> Response:
    """Read the same saved data as the RTI report and dashboard widgets."""
    from app.services.rti import get_rti_report

    return jsonify(get_rti_report())


@bp.route('/reports/dashboards')
def dashboards_hub():
    """Manage saved dashboards separately from their report views."""
    from app.services.dashboards import list_dashboards
    from app.services.dashboard_templates import list_dashboard_templates

    templates = list_dashboard_templates()
    requested = next((
        template for template in templates if template['key'] == request.args.get('template')
    ), None)
    return render_template(
        'dashboards.html', dashboards=list_dashboards(), dashboard_templates=templates,
        requested_template=requested,
    )


@bp.route('/api/reports/dashboards/from-template/<key>', methods=['POST'])
def dashboard_from_template(key: str):
    """Create a user-owned copy without changing an existing dashboard or local draft."""
    from app.models import DashboardTemplateInstallation
    from app.services.dashboard_templates import (
        DASHBOARD_TEMPLATES, create_dashboard_from_template,
    )

    try:
        dashboard = create_dashboard_from_template(key)
        installation = db.session.get(DashboardTemplateInstallation, key)
        if installation is None:
            installation = DashboardTemplateInstallation(
                key=key, version=DASHBOARD_TEMPLATES[key]['version'],
            )
            db.session.add(installation)
        if installation.dashboard is None:
            installation.dashboard = dashboard
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except SQLAlchemyError:
        return _dashboard_write_error()
    return _save_dashboard_changes(dashboard, created=True)


@bp.route('/reports/dashboards/<int:dashboard_id>')
@bp.route('/reports/dashboards/<int:dashboard_id>/edit', endpoint='dashboard_edit')
def dashboard_view(dashboard_id: int):
    """Render a saved report, or its explicit dashboard management surface."""
    from app.services.dashboards import (
        CANVAS_WIDTHS, GRID_GAP, GRID_PADDING, GRID_ROW_HEIGHT, MAX_LAYOUT_ROWS,
        get_dashboard_data, get_widget_catalog,
    )

    dashboard = db.get_or_404(Dashboard, dashboard_id)
    manage_dashboard = request.endpoint == 'reports.dashboard_edit'
    from app.services.dashboard_filters import get_filter_choices, live_filters_from_query

    try:
        data = get_dashboard_data(
            dashboard, live_filters=live_filters_from_query(request.args),
        )
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    return render_template(
        'dashboard_view.html', dashboard=data, filter_choices=get_filter_choices(),
        manage_dashboard=manage_dashboard,
        catalog=get_widget_catalog() if manage_dashboard else [],
        canvas_widths=CANVAS_WIDTHS,
        grid_config={
            'rowHeight': GRID_ROW_HEIGHT, 'gap': GRID_GAP, 'padding': GRID_PADDING,
            'maxRows': MAX_LAYOUT_ROWS,
        },
    )


@bp.route('/api/reports/dashboards', methods=['GET', 'POST'])
@bp.route('/api/reports/dashboards/<int:dashboard_id>', methods=['GET', 'POST'])
@bp.route('/api/reports/dashboards/<int:dashboard_id>/delete', methods=['POST'])
def dashboard_data(dashboard_id: int | None = None):
    """Read, create, describe, or delete dashboards without changing source records."""
    from app.services.dashboards import (
        get_dashboard_data, get_widget_catalog, list_dashboards, validate_dashboard_name,
        validate_dashboard_subtitle,
    )

    dashboard = db.get_or_404(Dashboard, dashboard_id) if dashboard_id is not None else None
    if request.method == 'GET':
        if dashboard is not None:
            from app.services.dashboard_filters import live_filters_from_query

            try:
                data = get_dashboard_data(dashboard, live_filters=live_filters_from_query(request.args))
            except ValueError as exc:
                return jsonify(success=False, error=str(exc)), 400
            return jsonify(success=True, dashboard=data)
        return jsonify(success=True, dashboards=list_dashboards(), catalog=get_widget_catalog())
    if request.path.endswith('/delete'):
        db.session.delete(dashboard)
        return _save_dashboard_changes()
    data = request.get_json(silent=True)
    created = dashboard is None
    try:
        if not isinstance(data, dict):
            raise ValueError('Enter a dashboard name of 1 to 200 characters.')
        changes = {}
        if created or 'name' in data:
            changes['name'] = validate_dashboard_name(data.get('name'))
        if created or 'subtitle' in data:
            changes['subtitle'] = validate_dashboard_subtitle(data.get('subtitle', ''))
        if not changes:
            raise ValueError('Provide a dashboard name or subtitle to save.')
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    if created:
        dashboard = Dashboard(**changes)
        db.session.add(dashboard)
    else:
        for field, value in changes.items():
            setattr(dashboard, field, value)
    return _save_dashboard_changes(dashboard, created)


def _save_dashboard_changes(
    dashboard: Dashboard | None = None, created: bool = False,
) -> tuple:
    """Commit dashboard edits with rollback and a visible failure response."""
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Failed to save dashboard changes')
        return jsonify(success=False, error='Dashboard changes could not be saved. Try again.'), 500
    return jsonify(
        success=True, id=dashboard.id if dashboard is not None else None,
        name=dashboard.name if dashboard is not None else None,
        subtitle=dashboard.subtitle if dashboard is not None else None,
    ), (
        201 if created else 200
    )


@bp.route('/api/reports/dashboards/<int:dashboard_id>/widgets', methods=['POST'])
@bp.route(
    '/api/reports/dashboards/<int:dashboard_id>/widgets/<int:widget_id>/<action>',
    methods=['POST'],
)
def dashboard_widget_edit(
    dashboard_id: int, widget_id: int | None = None, action: str | None = None,
):
    """Add or remove one placement without rearranging its neighbors."""
    from app.services.dashboards import (
        LayoutConflict, add_dashboard_widget, advance_layout_revision,
    )

    dashboard = db.get_or_404(Dashboard, dashboard_id)
    instance = None
    if widget_id is not None:
        instance = db.first_or_404(db.select(DashboardWidget).filter_by(
            id=widget_id, dashboard_id=dashboard_id,
        ))
    data = request.get_json(silent=True)
    try:
        if widget_id is None:
            add_dashboard_widget(
                dashboard, data.get('key') if isinstance(data, dict) else None,
                data.get('variant_key') if isinstance(data, dict) else None,
                data.get('column_span') if isinstance(data, dict) else None,
                data.get('row_span') if isinstance(data, dict) else None,
                data.get('filters') if isinstance(data, dict) else None,
            )
        elif action == 'remove':
            advance_layout_revision(dashboard, dashboard.layout_revision)
            dashboard.widgets.remove(instance)
        else:
            return jsonify(success=False, error='Unknown widget action.'), 400
    except LayoutConflict as exc:
        db.session.rollback()
        return jsonify(success=False, error=str(exc)), 409
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except SQLAlchemyError:
        return _dashboard_write_error()
    return _save_dashboard_changes()


def _dashboard_write_error() -> tuple[Response, int]:
    """Roll back failed layout writes, including errors before commit."""
    db.session.rollback()
    logger.exception('Failed to update dashboard layout')
    return jsonify(success=False, error='Dashboard changes could not be saved. Try again.'), 500


@bp.route('/api/reports/dashboards/<int:dashboard_id>/layout', methods=['POST'])
def dashboard_layout_save(dashboard_id: int) -> tuple[Response, int]:
    """Save explicit grid positions and sizes atomically, never repacking widgets."""
    from app.services.dashboards import LayoutConflict, save_dashboard_layout

    dashboard = db.get_or_404(Dashboard, dashboard_id)
    try:
        save_dashboard_layout(dashboard, request.get_json(silent=True))
    except LayoutConflict as exc:
        db.session.rollback()
        return jsonify(success=False, error=str(exc)), 409
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except SQLAlchemyError:
        return _dashboard_write_error()
    return _save_dashboard_changes()


@bp.route('/api/reports/dashboards/widget-preview')
def dashboard_widget_preview():
    """Render an actual-size library preview using the placed-widget presentation."""
    from app.services.dashboards import get_widget_preview

    try:
        context = json.loads(request.args['filter_context']) if 'filter_context' in request.args else None
        instance = get_widget_preview(
            request.args.get('key', ''), request.args.get('variant', ''),
            request.args.get('width', type=int),
            filter_context=context,
        )
    except (ValueError, TypeError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    instance_id = request.args.get('instance_id', type=int)
    if 'instance_id' in request.args and instance_id is None:
        return jsonify(success=False, error='Provide a valid widget preview identity.'), 400
    if instance_id is not None:
        instance['id'] = instance_id
    return jsonify(success=True, html=render_template(
        'partials/dashboard_widget_content.html', instance=instance,
    ))


@bp.route('/api/reports/dashboards/widgets/<key>/sync', methods=['POST'])
def dashboard_widget_sync(key: str):
    """Admit only a visible widget's next missing source after checking its setup."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.services.dashboards import WIDGET_DEFINITIONS, get_widget_readiness
    from app.services.dashboard_sync import start_widget_sync
    from app.services.se_specialty import has_specialty_access

    definition = WIDGET_DEFINITIONS.get(key)
    if definition is None:
        return jsonify(success=False, error='Unknown widget.'), 404
    if definition.specialty and not has_specialty_access(definition.specialty):
        return jsonify(success=False, error='This widget is not available for your specialty.'), 403
    options = request.get_json(silent=True)
    if not isinstance(options, dict) or set(options) - {'retry'} or (
        'retry' in options and not isinstance(options['retry'], bool)
    ):
        return jsonify(success=False, error='Provide a valid sync request.'), 400
    try:
        requirements, sync = get_widget_readiness(definition)
        if sync:
            sync = start_widget_sync(sync, retry=options.get('retry', False))
        return jsonify(success=True, ready=not requirements and not sync,
                       requirements=bool(requirements), sync=sync)
    except ValueError as exc:
        current_app.logger.info('Dashboard widget sync rejected for %s: %s', key, exc)
        return jsonify(success=False, error=str(exc)), getattr(exc, 'status', 400)
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Could not admit dashboard widget sync for %s', key)
        return jsonify(success=False, error='Could not save the sync job. Please retry.'), 500


@bp.route('/api/reports/dashboards/requirements/<key>')
def dashboard_requirement(key: str):
    """Return current shared setup without changing dashboard or draft state."""
    from app.services.dashboards import WIDGET_REQUIREMENTS

    if key not in WIDGET_REQUIREMENTS:
        return jsonify(success=False, error='Unknown widget requirement.'), 404
    setup = WIDGET_REQUIREMENTS[key](None)
    return jsonify(success=True, setup=setup, html=render_template(
        'partials/dashboard_requirement_form.html', setup=setup,
    ))


@bp.route('/api/reports/dashboards/<int:dashboard_id>/save', methods=['POST'])
def dashboard_draft_save(dashboard_id: int) -> tuple:
    """Publish a complete local draft in one database transaction."""
    from app.services.dashboards import LayoutConflict, save_dashboard_draft

    dashboard = db.get_or_404(Dashboard, dashboard_id)
    try:
        save_dashboard_draft(dashboard, request.get_json(silent=True))
    except LayoutConflict as exc:
        db.session.rollback()
        return jsonify(success=False, error=str(exc)), 409
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except SQLAlchemyError:
        return _dashboard_write_error()
    return _save_dashboard_changes(dashboard)


@bp.route('/reports/p-to-f')
def report_ptof():
    """Show saved Premium-to-Fabric renewal assessments and subscription details."""
    from app.services.se_specialty import is_data_se

    return render_template('report_ptof.html', ptof_auto_sync=is_data_se())


@bp.route('/api/reports/p-to-f')
def ptof_data():
    """Read saved P to F data, optionally scoped to one existing customer."""
    from app.services.ptof import get_ptof_report

    customer_id = request.args.get('customer_id', type=int)
    if 'customer_id' in request.args:
        if customer_id is None or customer_id <= 0:
            return jsonify(success=False, error='Choose a valid customer.'), 400
        if db.session.get(Customer, customer_id) is None:
            return jsonify(success=False, error='Customer not found.'), 404
    return jsonify(success=True, **get_ptof_report(customer_id))


@bp.route('/reports/fabric-consumption')
def report_fabric_consumption():
    """Show saved Fabric workload mix and capacity utilization."""
    from app.services.se_specialty import is_data_se

    if request.args.get('view') == 'overview':
        return render_template('report_fabric_capacity_overview.html', fabric_auto_sync=is_data_se())
    return render_template('report_fabric_consumption.html', fabric_auto_sync=is_data_se())


@bp.route('/api/reports/fabric-consumption/overview')
def fabric_capacity_overview_data():
    """Read all saved capacity comparisons without querying MSXi."""
    from app.services.fabric_consumption import get_fabric_capacity_overview

    return jsonify(success=True, **get_fabric_capacity_overview())


@bp.route('/api/reports/fabric-capacity-pressure')
@bp.route('/reports/fabric-capacity-pressure')
def report_fabric_capacity_pressure():
    """Investigate the full saved capacity population behind the compact widget."""
    from app.services.fabric_consumption import (
        PRESSURE_SIGNALS, get_fabric_capacity_pressure_report,
    )
    from app.services.dashboard_filters import report_customer_scope, report_scope_query

    try:
        customer_ids = report_customer_scope(request.args)
        scope = {'customer_ids': customer_ids} if customer_ids is not None else {}
        data = get_fabric_capacity_pressure_report(
            signal=request.args.get('signal', 'attention'),
            search=request.args.get('q', ''),
            **scope,
        )
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    if request.path.startswith('/api/'):
        return jsonify(success=True, **data)
    return render_template(
        'report_fabric_capacity_pressure.html', data=data, signal_labels=PRESSURE_SIGNALS,
        dashboard_customer_scope=customer_ids,
        dashboard_scope_query=report_scope_query(customer_ids) if customer_ids is not None else '',
    )


@bp.route('/api/reports/fabric-consumption')
def fabric_consumption_data():
    """Read a saved Fabric snapshot without contacting the remote report."""
    raw_customer_id = request.args.get('customer_id')
    customer_id = None
    if raw_customer_id is not None:
        try:
            customer_id = int(raw_customer_id)
        except ValueError:
            return jsonify(success=False, error='Choose a valid customer.'), 400
        if customer_id <= 0:
            return jsonify(success=False, error='Choose a valid customer.'), 400
        if db.session.get(Customer, customer_id) is None:
            return jsonify(success=False, error='Customer not found.'), 404
    from app.services.fabric_consumption import get_fabric_report

    return jsonify(success=True, **get_fabric_report(customer_id=customer_id))


@bp.route('/api/reports/fabric-consumption/status')
def fabric_consumption_status():
    """Read progress of the current manual or scheduled Fabric import."""
    from app.services.fabric_consumption import SYNC_TYPE

    return jsonify(success=True, status=SyncStatus.get_status(SYNC_TYPE))


@bp.route('/api/reports/fabric-consumption/import', methods=['POST'])
def fabric_consumption_import():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('fabric', None)


def _save_manager_report_changes() -> tuple:
    """Commit report edits with explicit rollback and user-visible failure messages."""
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.exception('Conflicting Initiative Tracker edit')
        return jsonify(success=False, error='An item is already in this initiative.'), 409
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Failed to save Initiative Tracker')
        return jsonify(
            success=False, error='Your changes could not be saved. Please try again.',
        ), 500
    return jsonify(success=True), 200


@bp.route('/reports/manager-one-on-one')
@bp.route('/reports/initiative-tracker')
def report_manager_one_on_one():
    """Render Initiative Tracker, retaining the original page URL for saved links."""
    from app.services.manager_one_on_one import get_manager_one_on_one_report
    from app.services.agenda_candidates import get_selectable_projects_query

    return render_template(
        'report_manager_one_on_one.html', **get_manager_one_on_one_report(),
        has_projects=get_selectable_projects_query().with_entities(Project.id).first() is not None,
    )


@bp.route('/api/reports/manager-one-on-one')
def manager_one_on_one_data():
    """Read the same live report data used by the page and SalesIQ."""
    from app.services.manager_one_on_one import get_manager_one_on_one_report

    return jsonify(success=True, **get_manager_one_on_one_report())


@bp.route('/api/reports/manager-one-on-one/sections', methods=['POST'])
@bp.route(
    '/api/reports/manager-one-on-one/sections/<int:section_id>',
    methods=['PATCH', 'DELETE'],
)
def manager_one_on_one_section(section_id: int | None = None):
    """Create, rename, describe, or remove a section without changing linked work."""
    from app.models import ManagerInitiativeSection

    section = (
        db.session.get(ManagerInitiativeSection, section_id)
        if section_id is not None else None
    )
    if section_id is not None and section is None:
        return jsonify(success=False, error='Initiative not found.'), 404
    if request.method == 'DELETE':
        db.session.delete(section)
        return _save_manager_report_changes()

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error='An initiative name and description are required.'), 400
    name = data.get('name')
    description = data.get('description', '')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        return jsonify(success=False, error='Enter an initiative name of 1 to 200 characters.'), 400
    if not isinstance(description, str) or len(description) > 10000:
        return jsonify(success=False, error='Description must be at most 10,000 characters.'), 400
    if section is None:
        section = ManagerInitiativeSection()
        db.session.add(section)
    section.name = name.strip()
    section.description = description.strip()
    return _save_manager_report_changes()


@bp.route('/api/reports/manager-one-on-one/sections/<int:section_id>/candidates')
def manager_one_on_one_candidates(section_id: int):
    """Search current work, excluding only items already in the selected section."""
    from app.models import ManagerInitiativeSection
    from app.services.agenda_candidates import WORK_MODELS, get_agenda_candidates

    section = db.session.get(ManagerInitiativeSection, section_id)
    if section is None:
        return jsonify(success=False, error='Initiative not found.'), 404
    item_type = request.args.get('type', 'engagement')
    if item_type not in WORK_MODELS:
        return jsonify(success=False, error='Choose engagements, milestones, or projects.'), 400
    source = request.args.get('source', 'search')
    if source not in {'search', 'u2c'} or (source == 'u2c' and item_type != 'milestone'):
        return jsonify(success=False, error='U2C is available for milestones only.'), 400
    excluded = {
        getattr(item, f'{item_type}_id')
        for item in section.items if item.item_type == item_type
    }
    if source == 'u2c':
        from app.services.manager_one_on_one import get_manager_u2c_candidates

        return jsonify(success=True, **get_manager_u2c_candidates(
            request.args.get('q', '').strip(), existing_ids=excluded,
            workload_prefix=request.args.get('workload_prefix', '').strip(),
            workloads=request.args.getlist('workload'),
        ))
    return jsonify(success=True, results=get_agenda_candidates(
        item_type, request.args.get('q', '').strip(), excluded_ids=excluded,
    ))


@bp.route(
    '/api/reports/manager-one-on-one/sections/<int:section_id>/items', methods=['POST'],
)
def manager_one_on_one_add_items(section_id: int):
    """Add a validated selection atomically, permitting reuse in other initiatives."""
    from app.models import ManagerInitiativeItem, ManagerInitiativeSection
    from app.services.agenda_candidates import (
        WORK_MODELS, candidate_payload, get_selectable_projects_query,
    )

    section = db.session.get(ManagerInitiativeSection, section_id)
    if section is None:
        return jsonify(success=False, error='Initiative not found.'), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error='Choose items to add.'), 400
    item_type = data.get('item_type')
    ids = data.get('entity_ids')
    if (
        not isinstance(item_type, str) or item_type not in WORK_MODELS
        or not isinstance(ids, list) or not 1 <= len(ids) <= 75
        or any(type(entity_id) is not int or entity_id <= 0 for entity_id in ids)
        or len(set(ids)) != len(ids)
    ):
        return jsonify(success=False, error='Choose 1 to 75 distinct, valid items.'), 400
    model = WORK_MODELS[item_type]
    query = (
        get_selectable_projects_query() if item_type == 'project' else model.query
    ).filter(model.id.in_(ids))
    if item_type != 'project':
        query = query.options(db.joinedload(model.customer))
    entities = query.all()
    if len(entities) != len(ids) or (
        item_type != 'project' and any(not entity.customer for entity in entities)
    ):
        return jsonify(success=False, error='A selected item is no longer available.'), 404
    existing = {
        getattr(item, f'{item_type}_id')
        for item in section.items if item.item_type == item_type
    }
    if existing.intersection(ids):
        return jsonify(success=False, error='An item is already in this initiative.'), 409
    by_id = {entity.id: entity for entity in entities}
    for entity_id in ids:
        payload = candidate_payload(by_id[entity_id], item_type)
        section.items.append(ManagerInitiativeItem(
            item_type=item_type,
            milestone_id=entity_id if item_type == 'milestone' else None,
            engagement_id=entity_id if item_type == 'engagement' else None,
            project_id=entity_id if item_type == 'project' else None,
            title_snapshot=payload['title'],
            customer_snapshot=payload['customer_name'],
        ))
    return _save_manager_report_changes()


@bp.route(
    '/api/reports/manager-one-on-one/items/<int:item_id>', methods=['GET', 'PATCH', 'DELETE'],
)
def manager_one_on_one_item(item_id: int):
    """Save talking points or remove a report link, never the source entity."""
    from app.models import ManagerInitiativeItem
    from app.services.manager_one_on_one import get_manager_item_payload

    item = db.session.get(ManagerInitiativeItem, item_id)
    if item is None:
        return jsonify(success=False, error='Report item not found.'), 404
    if request.method == 'GET':
        return jsonify(success=True, item=get_manager_item_payload(item))
    if request.method == 'DELETE':
        db.session.delete(item)
    else:
        data = request.get_json(silent=True)
        points = data.get('talking_points') if isinstance(data, dict) else None
        if not isinstance(points, str) or len(points) > 10000:
            return jsonify(
                success=False, error='Talking points must be at most 10,000 characters.',
            ), 400
        if points.strip() and not item.talking_points.strip():
            item.points_created_at = datetime.now(timezone.utc)
        elif not points.strip():
            item.points_created_at = None
        item.talking_points = points
    response, status = _save_manager_report_changes()
    if status == 200 and request.method == 'PATCH':
        return jsonify(success=True, item=get_manager_item_payload(item)), 200
    return response, status


@bp.route(
    '/api/reports/manager-one-on-one/items/<int:item_id>/discuss', methods=['POST'],
)
def manager_one_on_one_discuss(item_id: int) -> Response | tuple[Response, int]:
    """Archive a saved agenda block once, retaining timestamps and starting fresh."""
    from app.models import ManagerDiscussedPoint, ManagerInitiativeItem
    from app.services.manager_one_on_one import get_manager_item_payload

    item = db.session.get(ManagerInitiativeItem, item_id)
    if item is None:
        return jsonify(success=False, error='Report item not found.'), 404
    data = request.get_json(silent=True)
    points = data.get('talking_points') if isinstance(data, dict) else None
    if not isinstance(points, str) or not points.strip() or len(points) > 10000:
        return jsonify(success=False, error='Add points before marking them discussed.'), 400
    created_at = item.points_created_at
    try:
        updated = ManagerInitiativeItem.query.filter_by(
            id=item_id, talking_points=points, points_created_at=created_at,
        ).update({'talking_points': '', 'points_created_at': None})
        if updated != 1:
            db.session.rollback()
            return jsonify(
                success=False,
                error='These points changed or were already discussed. Reopen Points to refresh.',
            ), 409
        db.session.add(ManagerDiscussedPoint(
            item=item, text=points, created_at=created_at,
            discussed_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Failed to archive Initiative Tracker points for item %s', item_id)
        return jsonify(
            success=False, error='Points could not be marked discussed. Please try again.',
        ), 500
    return jsonify(success=True, item=get_manager_item_payload(item))


@bp.route('/reports')
def reports_hub():
    """List available reports in the same groups and order as the Reports menu."""
    report_groups = [
        {
            'title': 'Planning & Reviews',
            'icon': 'bi-people',
            'reports': [
                {
                    'id': 'initiative-tracker',
                    'name': 'Initiative Tracker',
                    'description': (
                        'Track priorities with named initiatives, hand-picked engagements '
                        'and milestones, and discussion points for your conversations.'
                    ),
                    'icon': 'bi-layout-text-window',
                    'url': url_for('reports.report_manager_one_on_one'),
                },
                {
                    'id': 'one-on-one',
                    'name': '1:1 Report',
                    'description': (
                        'Active engagements and recent notes for 1:1 meeting prep. '
                        'Shows what you have been working on and where milestones stand.'
                    ),
                    'icon': 'bi-chat-left-text',
                    'url': url_for('reports.report_one_on_one'),
                },
                {
                    'id': 'connect-impact',
                    'name': 'Connect Impact',
                    'description': (
                        'Customers ranked by total estimated ACR/mo from '
                        'committed milestones you are on the team for this '
                        'quarter. Know your highest-impact accounts for Connect.'
                    ),
                    'icon': 'bi-trophy',
                    'url': url_for('reports.report_connect_impact'),
                },
            ],
        },
        {
            'title': 'Execution & Coverage',
            'icon': 'bi-bullseye',
            'reports': [
                {
                    'id': 'u2c-attainment',
                    'name': 'U2C Attainment',
                    'description': (
                        'Track uncommitted-to-committed milestone attainment '
                        'against a quarterly snapshot baseline. See what you '
                        'started with and how far you have come.'
                    ),
                    'icon': 'bi-bullseye',
                    'url': url_for('reports.report_u2c'),
                },
                {
                    'id': 'milestone-tracker',
                    'name': 'Milestone Tracker',
                    'description': (
                        'Surface milestones across all accounts. Find milestones '
                        'you are not aligned to yet and get on the team.'
                    ),
                    'icon': 'bi-flag-fill',
                    'url': url_for('milestones.milestone_tracker'),
                },
                {
                    'id': 'activity-coverage',
                    'name': 'Activity Coverage',
                    'description': (
                        'Review fiscal-year customer meetings, match them to '
                        'milestones, and create or reconcile MSX activities.'
                    ),
                    'icon': 'bi-calendar2-check',
                    'url': url_for('reports.report_activity_coverage'),
                },
                {
                    'id': 'hvas',
                    'name': 'HVAs',
                    'description': 'Browse your owned high-value activities, including completed '
                                   'tasks, with status, creation dates, due dates and related work.',
                    'icon': 'bi-lightning-charge',
                    'url': url_for('reports.report_hvas'),
                },
                {
                    'id': 'whitespace',
                    'name': 'Whitespace Analysis',
                    'description': (
                        'Find gaps in customer technology adoption. See which '
                        'customers are missing spend across strategic pillars and identify '
                        'outreach opportunities.'
                    ),
                    'icon': 'bi-grid-3x3-gap',
                    'url': url_for('reports.report_whitespace'),
                },
            ],
        },
        {
            'title': 'Pipeline Health',
            'icon': 'bi-clipboard-check',
            'reports': [
                {
                    'id': 'hygiene-report',
                    'name': 'Engagement / Milestone Hygiene',
                    'description': (
                        'Engagements without milestones and milestones without '
                        'engagements. Add notes explaining why so you can report '
                        'on gaps quickly.'
                    ),
                    'icon': 'bi-link-45deg',
                    'url': url_for('reports.report_hygiene'),
                },
                {
                    'id': 'msx-workspace',
                    'name': 'MSX Workspace',
                    'description': (
                        'Browse opportunities, milestones, and tasks from MSX. '
                        'Manage team membership, create HVA tasks, and edit '
                        'milestones - all without leaving Sales Buddy.'
                    ),
                    'icon': 'bi-grid-3x3',
                    'url': url_for('reports.report_msx_workspace'),
                },
                {
                    'id': 'whats-new',
                    'name': "What's New",
                    'description': (
                        'Milestones created or updated in the last 2 weeks. '
                        'Filter by seller to see what changed on your accounts.'
                    ),
                    'icon': 'bi-megaphone',
                    'url': url_for('reports.report_whats_new'),
                },
            ],
        },
        {
            'title': 'Account Insights',
            'icon': 'bi-graph-up',
            'reports': [
                {
                    'id': 'revenue-analyzer',
                    'name': 'Revenue Analyzer',
                    'description': (
                        'Revenue trends, alerts, and drilldowns across all customers. '
                        'Track declining accounts and expansion opportunities.'
                    ),
                    'icon': 'bi-graph-up',
                    'url': url_for('revenue.revenue_dashboard'),
                },
                {
                    'id': 'workload-report',
                    'name': 'Workload Report',
                    'description': (
                        'Customers grouped by workload topic. Quickly find who is '
                        'working on a specific technology for workshop targeting.'
                    ),
                    'icon': 'bi-tag',
                    'url': url_for('reports.report_workload'),
                },
                {
                    'id': 'marketing-insights',
                    'name': 'Marketing Insights',
                    'description': (
                        'Marketing engagement data from MSX. See which accounts '
                        'have active marketing activity, sales play breakdowns, '
                        'and top engaged contacts.'
                    ),
                    'icon': 'bi-megaphone',
                    'url': url_for('reports.report_marketing_insights'),
                },
            ],
        },
        {
            'title': 'FY27 Data Motions',
            'icon': 'bi-database-gear',
            'reports': [
                {
                    'id': 'p-to-f',
                    'name': 'P to F',
                    'description': (
                        'Power BI Premium renewal dates, product families and source '
                        'renewal status, matched to customer names and Fabric migration.'
                    ),
                    'icon': 'bi-arrow-left-right',
                    'url': url_for('reports.report_ptof'),
                },
                {
                    'id': 'fabric-consumption',
                    'name': 'Fabric Consumption',
                    'description': (
                        'Manually import Fabric workload usage and capacity utilization '
                        'for your customers. Inspect SKU consumption, throttling, '
                        'and monthly RI/PAYG revenue.'
                    ),
                    'icon': 'bi-pie-chart',
                    'url': url_for('reports.report_fabric_consumption'),
                },
                {
                    'id': 'fabric-capacity-pressure',
                    'name': 'Fabric Capacity Pressure',
                    'description': (
                        'Investigate saved capacity throttling, high utilization, '
                        'historical peaks, and incomplete measurements.'
                    ),
                    'icon': 'bi-speedometer2',
                    'url': url_for('reports.report_fabric_capacity_pressure'),
                },
                {
                    'id': 'fabric-modernization',
                    'name': 'Fabric Modernization',
                    'description': (
                        'Recent Synapse, AAS, ADX and Azure Databricks revenue '
                        'alongside Fabric spend, grouped by seller.'
                    ),
                    'icon': 'bi-database-gear',
                    'url': url_for('reports.report_fabric_modernization'),
                },
                {
                    'id': 'rti',
                    'name': 'Real-Time Intelligence',
                    'description': (
                        'RTI workload usage alongside Event Hubs, Stream Analytics '
                        'and IoT consumption, grouped by seller.'
                    ),
                    'icon': 'bi-broadcast',
                    'url': url_for('reports.report_rti'),
                },
                {
                    'id': 'sql-renewals',
                    'name': 'SQL Renewals',
                    'description': (
                        'Upcoming SQL expirations, licensed footprint and modeled '
                        'migration opportunity from Azure Migrations.'
                    ),
                    'icon': 'bi-calendar-event',
                    'url': url_for('reports.report_sql_renewals'),
                },
                {
                    'id': 'sql-eos',
                    'name': 'SQL 2016 EOS / ESU',
                    'description': (
                        'Explicit SQL Server 2016 discovery from Arc, with subscription '
                        'evidence. ESU protection remains unverified.'
                    ),
                    'icon': 'bi-database-exclamation',
                    'url': url_for('reports.report_sql_eos'),
                },
            ],
        },
    ]

    return render_template('reports_hub.html', report_groups=report_groups)


@bp.route('/reports/activity-coverage')
def report_activity_coverage():
    """Review meeting and milestone HVA activity coverage."""
    from app.services.activity_coverage import (
        get_caip_coverage_data,
        get_milestone_coverage_data,
        get_population_status,
        get_reconciliation_status,
        get_report_data,
    )

    week_value = request.args.get('week')
    try:
        week_start = (
            datetime.strptime(week_value, '%Y-%m-%d').date()
            if week_value else None
        )
    except ValueError:
        week_start = None
    lens = request.args.get('lens', 'meetings')
    scope = request.args.get('scope', '')
    if lens == 'milestones':
        coverage_view = request.args.get('coverage', 'fy')
        if coverage_view == 'caip':
            data = get_caip_coverage_data()
        else:
            coverage_view = 'fy'
            coverage_scope = {}
            if scope == 'fy27-data':
                from app.services.activity_coverage import fiscal_year_bounds
                fiscal_start, fiscal_end = fiscal_year_bounds()
                coverage_scope = {
                    'workload_area': 'Data',
                    'due_start': fiscal_start,
                    'due_end': date(fiscal_end.year + 1, 6, 30),
                }
            data = get_milestone_coverage_data(
                include_covered=request.args.get('covered') == '1',
                include_inactive=request.args.get('inactive') == '1',
                **coverage_scope,
            )
        data['coverage_view'] = coverage_view
    else:
        lens = 'meetings'
        view_all = request.args.get('view') == 'all'
        try:
            milestone_id = int(request.args.get('milestone') or 0) or None
        except ValueError:
            milestone_id = None
        data = get_report_data(
            week_start,
            view_all=view_all,
            milestone_id=milestone_id,
        )
    data['lens'] = lens
    data['scope'] = scope
    data['population'] = get_population_status()
    data['reconciliation'] = get_reconciliation_status()
    from app.services.activity_enrichment import get_enrichment_status
    data['enrichment'] = get_enrichment_status()
    return render_template('report_activity_coverage.html', **data)


@bp.route(
    '/api/reports/activity-coverage/milestones/<int:milestone_id>/draft',
    methods=['PATCH'],
)
def api_activity_coverage_update_milestone_draft(milestone_id):
    """Save one standalone milestone HVA activity draft."""
    from app.services.activity_coverage import update_milestone_coverage_draft

    try:
        draft = update_milestone_coverage_draft(
            milestone_id,
            request.get_json(silent=True) or {},
        )
    except (TypeError, ValueError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'draft_id': draft.id})


@bp.route(
    '/api/reports/activity-coverage/milestones/<int:milestone_id>/create',
    methods=['POST'],
)
def api_activity_coverage_create_milestone_hva(milestone_id):
    """Create one standalone current-FY HVA activity."""
    from app.services.activity_coverage import (
        create_milestone_hva_activity,
        get_milestone_coverage_data,
    )

    try:
        task = create_milestone_hva_activity(milestone_id)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502
    return jsonify({
        'success': True,
        'task_id': task.id,
        'task_url': task.msx_task_url,
        'subject': task.subject,
        'category': task.task_category_name or '',
        'due_date': task.due_date.date().isoformat() if task.due_date else None,
        'summary': get_milestone_coverage_data()['milestone_summary'],
    })


@bp.route('/api/reports/activity-coverage/populate', methods=['POST'])
def api_activity_coverage_populate():
    """Admit resumable calendar population without request-owned execution."""
    from app.routes.jobs import start_response
    return start_response('calendar-population')


@bp.route('/api/reports/activity-coverage/import-status')
def api_activity_coverage_import_status():
    """Return fiscal population checkpoint and live progress."""
    from app.services.activity_coverage import get_population_status

    return jsonify({'success': True, **get_population_status()})


@bp.route('/api/reports/activity-coverage/reconcile', methods=['POST'])
def api_activity_coverage_reconcile():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('activity-reconciliation', None)


@bp.route('/api/reports/activity-coverage/reconcile-status')
def api_activity_coverage_reconcile_status():
    """Return live MSX activity reconciliation state."""
    from app.services.activity_coverage import get_reconciliation_status

    return jsonify({'success': True, **get_reconciliation_status()})


@bp.route('/api/reports/activity-coverage/match-milestones', methods=['POST'])
def api_activity_coverage_match_milestones():
    """Admit meeting preparation using the shared queue contract."""
    from app.routes.jobs import start_response
    return start_response('meeting-preparation', request.get_json(silent=True))


@bp.route('/api/reports/activity-coverage/match-status')
def api_activity_coverage_match_status():
    """Return durable milestone matching progress."""
    from app.services.activity_enrichment import get_enrichment_status

    return jsonify({'success': True, **get_enrichment_status()})


@bp.route('/api/reports/activity-coverage/customers/<int:customer_id>/milestones')
def api_activity_coverage_milestones(customer_id):
    """Return locally cached milestones for one customer."""
    from app.services.activity_coverage import milestone_picker_order

    milestones = (
        Milestone.query.filter_by(customer_id=customer_id)
        .filter(Milestone.msx_milestone_id.isnot(None))
        .order_by(*milestone_picker_order())
        .all()
    )
    return jsonify({
        'success': True,
        'milestones': [
            {
                'id': item.id,
                'label': item.display_text,
                'number': item.milestone_number,
                'status': item.msx_status,
                'on_my_team': item.on_my_team,
                'opportunity': (
                    item.opportunity.name
                    if item.opportunity
                    else item.opportunity_name
                ),
                'workload': item.workload,
            }
            for item in milestones
        ],
    })


@bp.route('/api/reports/activity-coverage/meetings/<int:meeting_id>', methods=['PATCH'])
def api_activity_coverage_update_meeting(meeting_id):
    """Save customer, milestone, and editable activity draft fields."""
    from app.services.activity_coverage import update_meeting_draft

    try:
        meeting = update_meeting_draft(meeting_id, request.get_json(silent=True) or {})
    except (TypeError, ValueError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'meeting_id': meeting.id})


def _activity_coverage_task_payload(task: MsxTask) -> dict:
    """Return activity fields needed for an inline logged-state update."""
    return {
        'task_id': task.id,
        'task_url': task.msx_task_url,
        'subject': task.subject,
        'category': task.task_category_name or '',
        'milestone': task.milestone.display_text if task.milestone else '',
    }


@bp.route(
    '/api/reports/activity-coverage/meetings/<int:meeting_id>/create',
    methods=['POST'],
)
def api_activity_coverage_create(meeting_id):
    """Create an MSX activity from a saved meeting draft."""
    from app.services.activity_coverage import create_meeting_activity

    try:
        task = create_meeting_activity(meeting_id)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 502
    return jsonify({'success': True, **_activity_coverage_task_payload(task)})


@bp.route(
    '/api/reports/activity-coverage/meetings/<int:meeting_id>/link',
    methods=['POST'],
)
def api_activity_coverage_link(meeting_id):
    """Confirm an existing imported MSX task for a meeting."""
    from app.services.activity_coverage import link_existing_activity

    data = request.get_json(silent=True) or {}
    try:
        task = link_existing_activity(meeting_id, int(data.get('task_id') or 0))
    except (TypeError, ValueError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, **_activity_coverage_task_payload(task)})


@bp.route(
    '/api/reports/activity-coverage/meetings/<int:meeting_id>/dismiss',
    methods=['POST'],
)
def api_activity_coverage_dismiss(meeting_id):
    """Dismiss one occurrence or all known occurrences in a recurring series."""
    from app.services.activity_coverage import get_report_data
    from app.services.ghost_meetings import dismiss_ghost

    dismiss_series = bool((request.get_json(silent=True) or {}).get('series'))
    meeting = db.session.get(
        PrefetchedMeeting, meeting_id, populate_existing=True,
    )
    dismissed_ids = [meeting_id]
    if dismiss_series and meeting and meeting.is_recurring and meeting.recurring_key:
        dismissed_ids = [
            row.id for row in PrefetchedMeeting.query.filter_by(
                recurring_key=meeting.recurring_key,
            ).all()
        ]
    success, error = dismiss_ghost(meeting_id, dismiss_series=dismiss_series)
    if not success:
        return jsonify({'success': False, 'error': error}), 404
    return jsonify({
        'success': True,
        'dismissed_ids': dismissed_ids,
        'summary': get_report_data(view_all=True)['summary'],
    })


@bp.route('/reports/one-on-one')
def report_one_on_one():
    """1:1 Manager / SE meeting prep report.

    Two views:
    - Recent activity: engagements and notes in a rolling 14- or 30-day window
    - All time: all open engagements grouped by customer
    """
    from app.services.one_on_one_report import get_activity_cutoffs, sort_customer_activity

    days = request.args.get('days', '14')
    activity_sort = request.args.get('activity_sort', 'recent')
    if days not in {'14', '30'} or activity_sort not in {'recent', 'acr', 'seller', 'territory'}:
        abort(400, description='Choose a valid reporting period and customer activity order.')
    days = int(days)
    timestamp_cutoff, call_date_cutoff = get_activity_cutoffs(days)
    period_label = 'Last 2 Weeks' if days == 14 else 'Last 30 Days'

    # Note call dates, not edit timestamps, define activity in the chosen window.
    recent_notes = (
        Note.query
        .filter(Note.call_date >= call_date_cutoff)
        .order_by(desc(Note.call_date))
        .all()
    )

    # Engagements linked to those recent notes (not filtered by updated_at)
    recent_engagement_ids = set()
    recent_engagements = []
    for note in recent_notes:
        for eng in note.engagements:
            if eng.id not in recent_engagement_ids:
                recent_engagement_ids.add(eng.id)
                recent_engagements.append(eng)

    # Build recent activity grouped by customer
    recent_customers = {}
    for eng in recent_engagements:
        cust = eng.customer
        if cust and cust.id not in recent_customers:
            recent_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
                'notes': [],
            }
        if cust:
            recent_customers[cust.id]['engagements'].append(eng)

    for note in recent_notes:
        cust = note.customer
        if cust and cust.id not in recent_customers:
            recent_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
                'notes': [],
            }
        if cust:
            recent_customers[cust.id]['notes'].append(note)

    recent_sorted = sort_customer_activity(list(recent_customers.values()), activity_sort)

    # --- All open engagements ---
    open_engagements = (
        Engagement.query
        .filter(Engagement.status == 'Active')
        .all()
    )

    # Group by customer
    all_customers = {}
    for eng in open_engagements:
        cust = eng.customer
        if cust and cust.id not in all_customers:
            all_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
            }
        if cust:
            all_customers[cust.id]['engagements'].append(eng)

    all_sorted = sort_customer_activity(list(all_customers.values()), activity_sort)

    # --- Milestone highlights (on_my_team only) ---
    # Recently committed milestones in the selected window.
    # Uses committed_at date set by sync change detection
    milestone_commitments = (
        Milestone.query
        .filter(
            Milestone.customer_id.isnot(None),
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.committed_at >= timestamp_cutoff,
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(Milestone.committed_at))
        .all()
    )

    # Upcoming or overdue active milestones to follow up on
    today = date.today()

    # Current fiscal quarter milestones (on my team, active, with due date)
    from datetime import time as _time
    fy_month = (today.month - 7) % 12
    fq_start = (fy_month // 3) * 3
    q_start_month = ((fq_start + 7 - 1) % 12) + 1
    quarter_start = datetime.combine(date(today.year, q_start_month, 1), _time.min)
    end_month = q_start_month + 3
    end_year = today.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    quarter_end = datetime.combine(
        date(end_year, end_month, 1) - timedelta(days=1), _time(23, 59, 59)
    )

    today_dt = datetime.combine(today, _time.min)
    quarter_milestones = (
        Milestone.query
        .filter(
            Milestone.customer_id.isnot(None),
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Milestone.due_date.isnot(None),
            db.or_(
                # Due this quarter
                db.and_(Milestone.due_date >= quarter_start,
                        Milestone.due_date <= quarter_end),
                # Overdue from before the quarter (still active)
                Milestone.due_date < today_dt,
            ),
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(func.coalesce(Milestone.monthly_usage, 0)))
        .all()
    )

    # --- Topic trends (selected window + FY sparkline) ---
    topic_counts = {}
    for note in recent_notes:
        for topic in note.topics:
            if topic.name not in topic_counts:
                topic_counts[topic.name] = {'id': topic.id, 'count': 0, 'customers': set()}
            topic_counts[topic.name]['count'] += 1
            if note.customer:
                topic_counts[topic.name]['customers'].add(note.customer.name)

    # FY sparkline: monthly note counts per topic since FY start
    fy_year = today.year if today.month >= 7 else today.year - 1
    fy_start = datetime(fy_year, 7, 1)
    # Build list of month keys from FY start to current month
    fy_months = []
    m_year, m_month = fy_year, 7
    while (m_year, m_month) <= (today.year, today.month):
        fy_months.append((m_year, m_month))
        m_month += 1
        if m_month > 12:
            m_month = 1
            m_year += 1
    fy_month_labels = [
        datetime(y, m, 1).strftime('%b') for y, m in fy_months
    ]

    # Query all notes from FY start with their topics
    fy_notes = (
        Note.query
        .filter(Note.call_date >= fy_start)
        .options(db.joinedload(Note.topics))
        .all()
    )
    # Build per-topic monthly counts
    topic_monthly = {}  # topic_name -> {(year, month): count}
    for note in fy_notes:
        key = (note.call_date.year, note.call_date.month)
        for topic in note.topics:
            if topic.name not in topic_monthly:
                topic_monthly[topic.name] = {}
            topic_monthly[topic.name][key] = topic_monthly[topic.name].get(key, 0) + 1

    # Sort by selected-period frequency, attach sparkline data.
    top_topics = sorted(
        [
            {
                'id': data['id'],
                'name': name,
                'note_count': data['count'],
                'customer_count': len(data['customers']),
                'sparkline': [
                    topic_monthly.get(name, {}).get(mk, 0) for mk in fy_months
                ],
            }
            for name, data in topic_counts.items()
        ],
        key=lambda t: t['note_count'],
        reverse=True,
    )[:10]

    # --- Revenue alerts reviewed in the selected window ---
    reviewed_alerts = (
        RevenueAnalysis.query
        .filter(
            RevenueAnalysis.review_status.in_(['reviewed', 'actioned']),
            RevenueAnalysis.reviewed_at >= timestamp_cutoff,
        )
        .order_by(desc(RevenueAnalysis.reviewed_at))
        .all()
    )

    # Stats
    commitments_acr = sum(m.monthly_usage or 0 for m in milestone_commitments)
    stats = {
        'recent_engagement_count': len(recent_engagements),
        'recent_note_count': len(recent_notes),
        'recent_customer_count': len(recent_customers),
        'open_engagement_count': len(open_engagements),
        'open_customer_count': len(all_customers),
        'commitments_acr': commitments_acr,
    }

    one_on_one_workspaces = OneOnOneWorkspace.query.options(
        db.joinedload(OneOnOneWorkspace.seller),
    ).order_by(OneOnOneWorkspace.updated_at.desc()).all()
    linked_seller_ids = {
        workspace.seller_id
        for workspace in one_on_one_workspaces
        if workspace.seller_id
    }
    available_workspace_sellers = Seller.query.filter(
        ~Seller.id.in_(linked_seller_ids)
    ).order_by(Seller.name).all()

    return render_template(
        'report_one_on_one.html',
        recent_customers=recent_sorted,
        all_customers=all_sorted,
        stats=stats,
        cutoff_date=call_date_cutoff,
        period_days=days,
        period_label=period_label,
        period_short='2w' if days == 14 else '30d',
        activity_sort=activity_sort,
        milestone_commitments=milestone_commitments,
        quarter_milestones=quarter_milestones,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        today=today,
        reviewed_alerts=reviewed_alerts,
        top_topics=top_topics,
        fy_month_labels=fy_month_labels,
        one_on_one_workspaces=one_on_one_workspaces,
        available_workspace_sellers=available_workspace_sellers,
    )


@bp.route('/reports/workload')
def report_workload():
    """Workload report - customers grouped by topic/workload.

    All data is loaded server-side; date filtering is done client-side
    using the call_date on each customer-topic entry.
    """
    from app.services.seller_mode import get_seller_mode_seller_id
    seller_mode_sid = get_seller_mode_seller_id()

    # Get all topics with at least one note
    topics_with_notes = (
        Topic.query
        .join(notes_topics, Topic.id == notes_topics.c.topic_id)
        .join(Note, Note.id == notes_topics.c.note_id)
        .group_by(Topic.id)
        .having(func.count(Note.id) > 0)
        .order_by(Topic.name)
        .all()
    )

    workload_groups = []
    for topic in topics_with_notes:
        customer_q = (
            Customer.query
            .join(Note, Note.customer_id == Customer.id)
            .join(notes_topics, Note.id == notes_topics.c.note_id)
            .filter(notes_topics.c.topic_id == topic.id)
        )
        if seller_mode_sid:
            customer_q = customer_q.filter(Customer.seller_id == seller_mode_sid)
        customers = customer_q.distinct().order_by(Customer.name).all()

        if not customers:
            continue

        customer_data = []
        total_acr = 0
        milestone_acr_entries = {}  # accumulate id->monthly_usage across customers
        for cust in customers:
            active_engs = [e for e in cust.engagements if e.status == 'Active']

            # Get milestones linked to notes tagged with this topic for this customer
            topic_milestones = (
                Milestone.query
                .join(notes_milestones, Milestone.id == notes_milestones.c.milestone_id)
                .join(Note, Note.id == notes_milestones.c.note_id)
                .join(notes_topics, Note.id == notes_topics.c.note_id)
                .filter(
                    notes_topics.c.topic_id == topic.id,
                    Note.customer_id == cust.id,
                    Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
                )
                .distinct()
                .all()
            )
            cust_acr = sum(m.monthly_usage or 0 for m in topic_milestones)
            total_acr += cust_acr
            milestone_ids = [m.id for m in topic_milestones]
            for m in topic_milestones:
                milestone_acr_entries[m.id] = m.monthly_usage or 0

            latest_note = (
                Note.query
                .join(notes_topics, Note.id == notes_topics.c.note_id)
                .filter(
                    notes_topics.c.topic_id == topic.id,
                    Note.customer_id == cust.id,
                )
                .order_by(desc(Note.call_date))
                .first()
            )

            customer_data.append({
                'customer': cust,
                'engagement_count': len(active_engs),
                'milestone_count': len(topic_milestones),
                'acr': cust_acr,
                'latest_note_date': latest_note.call_date if latest_note else None,
                'latest_note_iso': latest_note.call_date.strftime('%Y-%m-%d') if latest_note and latest_note.call_date else '',
                'committed': any(
                    m.customer_commitment == 'Committed' for m in topic_milestones
                ),
                'milestone_ids': milestone_ids,
            })

        customer_data.sort(key=lambda c: c['acr'], reverse=True)

        workload_groups.append({
            'topic': topic,
            'customer_count': len(customer_data),
            'total_acr': total_acr,
            'customers': customer_data,
            'milestone_acr_entries': milestone_acr_entries,
        })

    workload_groups.sort(key=lambda g: g['customer_count'], reverse=True)

    # Build a deduplicated map of milestone_id -> monthly_usage for the grand total
    # Each milestone may appear in multiple groups (when tagged with multiple topics)
    # so we merge all per-group maps - last write wins, but values are identical since
    # monthly_usage is a property of the milestone, not the topic relationship.
    milestone_acr_map: dict = {}
    for group in workload_groups:
        milestone_acr_map.update(group['milestone_acr_entries'])

    # Compute fiscal quarter boundaries for preset buttons
    _today = date.today()
    fy_month = (_today.month - 7) % 12
    fq_start_idx = (fy_month // 3) * 3
    q_start_month = ((fq_start_idx + 7 - 1) % 12) + 1
    fq_start = date(_today.year, q_start_month, 1)
    end_month = q_start_month + 3
    end_year = _today.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    fq_end = date(end_year, end_month, 1) - timedelta(days=1)
    # Previous quarter
    prev_month = q_start_month - 3
    prev_year = _today.year
    if prev_month <= 0:
        prev_month += 12
        prev_year -= 1
    prev_fq_start = date(prev_year, prev_month, 1)
    prev_fq_end = fq_start - timedelta(days=1)

    # FQ labels (e.g. "FQ3: Jan 1 - Mar 31")
    fq_num = (fq_start_idx // 3) + 1
    prev_fq_num = fq_num - 1 if fq_num > 1 else 4

    return render_template(
        'report_workload.html',
        workload_groups=workload_groups,
        milestone_acr_map=milestone_acr_map,
        fq_start=fq_start.isoformat(),
        fq_end=fq_end.isoformat(),
        fq_label=f'FQ{fq_num}',
        prev_fq_start=prev_fq_start.isoformat(),
        prev_fq_end=prev_fq_end.isoformat(),
        prev_fq_label=f'FQ{prev_fq_num}',
    )


@bp.route('/api/reports/sync-milestone-dates', methods=['POST'])
def sync_milestone_dates():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('milestone-dates', None)


# =========================================================================
# Hygiene Report
# =========================================================================

@bp.route('/reports/hygiene')
def report_hygiene():
    """Engagement/Milestone hygiene report.

    Shows engagements without milestones and milestones without
    engagements so gaps can be identified and annotated quickly.
    """
    from app.services.hygiene import get_hygiene_report_data
    from app.services.dashboard_filters import report_customer_scope, report_scope_query

    try:
        customer_ids = report_customer_scope(request.args)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    data = (get_hygiene_report_data(customer_ids=customer_ids)
            if customer_ids is not None else get_hygiene_report_data())
    return render_template(
        'report_hygiene.html', **data, dashboard_customer_scope=customer_ids,
        dashboard_scope_query=report_scope_query(customer_ids) if customer_ids is not None else '',
    )


@bp.route('/api/hygiene-note', methods=['POST'])
def save_hygiene_note():
    """Save or update a reason note for an engagement without milestones."""
    data = request.get_json(silent=True) or {}
    entity_type = (data.get('entity_type') or '').strip()
    entity_id = data.get('entity_id')
    note_text = (data.get('note') or '').strip()

    if entity_type not in HygieneNote.ENTITY_TYPES:
        return jsonify(success=False, error='Invalid entity_type'), 400
    if not entity_id:
        return jsonify(success=False, error='entity_id is required'), 400

    try:
        entity_id = int(entity_id)
    except (ValueError, TypeError):
        return jsonify(success=False, error='Invalid entity_id'), 400

    hn = HygieneNote.query.filter_by(
        entity_type=entity_type, entity_id=entity_id
    ).first()

    if note_text:
        if hn:
            hn.note = note_text
            hn.updated_at = datetime.now(timezone.utc)
        else:
            hn = HygieneNote(
                entity_type=entity_type,
                entity_id=entity_id,
                note=note_text,
            )
            db.session.add(hn)
    else:
        # Empty note - delete the record if it exists
        if hn:
            db.session.delete(hn)

    db.session.commit()
    return jsonify(success=True)


# =============================================================================
# What's New Report
# =============================================================================

@bp.route('/reports/whats-new')
def report_whats_new():
    """What's New report - milestones created or updated in a configurable window."""
    from app.services.seller_mode import get_seller_mode_seller_id

    now = datetime.now(timezone.utc)
    days = request.args.get('days', 14, type=int)
    days = max(1, min(days, 90))  # Clamp to 1-90
    cutoff = now - timedelta(days=days)
    seller_mode_sid = get_seller_mode_seller_id()

    # --- Milestones created in MSX in the window ---
    # Use msx_created_on (from MSX Dataverse) when available, fall back to local created_at
    created_q = (
        Milestone.query
        .filter(
            Milestone.customer_id.isnot(None),
            db.or_(
                Milestone.msx_created_on >= cutoff,
                db.and_(Milestone.msx_created_on.is_(None), Milestone.created_at >= cutoff),
            )
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(db.func.coalesce(Milestone.msx_created_on, Milestone.created_at)))
    )
    created_milestones = created_q.all()

    # --- Milestones modified in MSX in the window (exclude newly created) ---
    # Use msx_modified_on when available, fall back to local updated_at
    updated_q = (
        Milestone.query
        .filter(
            Milestone.customer_id.isnot(None),
            db.or_(
                db.and_(
                    Milestone.msx_modified_on >= cutoff,
                    db.or_(Milestone.msx_created_on < cutoff, Milestone.msx_created_on.is_(None)),
                ),
                db.and_(
                    Milestone.msx_modified_on.is_(None),
                    Milestone.updated_at >= cutoff,
                    Milestone.created_at < cutoff,
                ),
            )
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(db.func.coalesce(Milestone.msx_modified_on, Milestone.updated_at)))
    )
    updated_milestones = updated_q.all()

    # Build seller list for filter (union of sellers from both lists)
    seller_ids_seen = set()
    sellers = []
    areas_seen = set()
    for ms in created_milestones + updated_milestones:
        if ms.customer and ms.customer.seller:
            s = ms.customer.seller
            if s.id not in seller_ids_seen:
                seller_ids_seen.add(s.id)
                sellers.append(s)
        if ms.workload:
            area = ms.workload.split(':', 1)[0].strip() if ':' in ms.workload else ms.workload.strip()
            if area:
                areas_seen.add(area)
    sellers.sort(key=lambda s: s.name)
    areas = sorted(areas_seen)

    # Build audit change-type data for updated milestones
    audit_changes = {}  # milestone_id -> set of field labels
    audit_dates = {}    # milestone_id -> {label: latest_changed_on as ISO string}
    change_types_seen = set()
    if updated_milestones:
        updated_ids = [ms.id for ms in updated_milestones]
        audits = (
            MilestoneAudit.query
            .filter(MilestoneAudit.milestone_id.in_(updated_ids))
            .order_by(desc(MilestoneAudit.changed_on))
            .all()
        )
        for a in audits:
            label = a.field_label
            audit_changes.setdefault(a.milestone_id, set()).add(label)
            change_types_seen.add(label)
            # Track the most recent date per milestone per label
            ms_dates = audit_dates.setdefault(a.milestone_id, {})
            if label not in ms_dates and a.changed_on:
                ms_dates[label] = a.changed_on.strftime('%Y-%m-%dT%H:%M:%S')

    # Convert sets to sorted lists for template
    audit_changes = {k: sorted(v) for k, v in audit_changes.items()}
    change_types = sorted(change_types_seen)

    return render_template(
        'report_whats_new.html',
        created_milestones=created_milestones,
        updated_milestones=updated_milestones,
        sellers=sellers,
        areas=areas,
        seller_mode_sid=seller_mode_sid,
        audit_changes=audit_changes,
        audit_dates=audit_dates,
        change_types=change_types,
        days=days,
    )


# =============================================================================
# Whitespace Report
# =============================================================================

@bp.route('/reports/whitespace')
def report_whitespace():
    """Whitespace report: find gaps in customer technology adoption."""
    from app.services.whitespace import has_pillar_data
    from app.services.fabric_acquisition import get_fabric_acquisition_report
    from app.services.se_specialty import has_specialty_access

    acquisition_requested = request.args.get('view') == 'fabric-acquisition'
    allowed = has_specialty_access('data')
    if acquisition_requested and not allowed:
        abort(403, description='Fabric Acquisition is available for Data SEs and sellers.')
    has_revenue_data_flag = has_pillar_data()
    return render_template(
        'report_whitespace.html', has_revenue_data=has_revenue_data_flag,
        acquisition=get_fabric_acquisition_report() if allowed else None,
        initial_mode='acquisition' if acquisition_requested else '',
    )


@bp.route('/api/reports/whitespace/fabric-acquisition')
def api_fabric_acquisition() -> Response | tuple[Response, int]:
    """Read covered acquisition customers with an optional report-only spend expansion."""
    from app.services.fabric_acquisition import get_fabric_acquisition_report

    include_no_other_spend = request.args.get('include_no_other_spend', 'false').lower()
    if include_no_other_spend not in ('true', 'false'):
        return jsonify(error='include_no_other_spend must be true or false.'), 400
    data = get_fabric_acquisition_report(
        include_no_other_spend=include_no_other_spend == 'true',
    )
    if not data['available']:
        return jsonify(error='Fabric Acquisition is available for Data SEs and sellers.'), 403
    data['table_html'] = render_template(
        'partials/_fabric_acquisition_table.html',
        acquisition=data, acquisition_table_id='fabricAcquisitionTable',
    )
    return jsonify(data)


@bp.route('/api/reports/whitespace/pillars')
def api_whitespace_pillars():
    """Return management's source-defined strategic pillar categories."""
    from app.services.whitespace import STRATEGIC_PILLARS
    return jsonify(list(STRATEGIC_PILLARS))


@bp.route('/reports/whitespace/customer/<int:customer_id>/pillar/<pillar>')
def whitespace_pillar_products(customer_id: int, pillar: str):
    """Inspect pillar-specific products with the shared native product/history view."""
    from flask import abort
    from app.services.whitespace import STRATEGIC_PILLARS, product_details

    if pillar not in STRATEGIC_PILLARS:
        abort(404)
    customer = Customer.query.get_or_404(customer_id)
    return render_template(
        'revenue_bucket_products.html', customer=customer, customer_name=customer.name,
        bucket=pillar, pillar_view=True, analysis=None, **product_details(customer_id, pillar),
    )


@bp.route('/api/reports/whitespace')
def api_whitespace_grid():
    """Return source-defined pillar whitespace, retaining the existing grid API shape.

    Query params:
        buckets: comma-separated strategic pillar names (existing parameter name)
        min_revenue: minimum recent average monthly revenue to count as spend (default 0)

    Returns JSON with:
        customers: list of {id, name, nickname, seller, buckets: {pillar: average_revenue}}
        buckets: list of selected strategic pillar names
    """
    bucket_param = request.args.get('buckets', '')
    if not bucket_param:
        return jsonify(customers=[], buckets=[])
    selected_buckets = [b.strip() for b in bucket_param.split(',') if b.strip()]
    min_revenue = float(request.args.get('min_revenue', 0))
    from app.services.whitespace import get_whitespace_grid
    return jsonify(get_whitespace_grid(
        selected_buckets, min_revenue=min_revenue,
        show_all=request.args.get('show_all', '') == '1',
    ))


@bp.route('/api/reports/whitespace/reverse/<int:customer_id>')
def api_whitespace_reverse(customer_id: int):
    """Return strategic pillars and products without historical positive spend.

    Returns JSON with:
        customer: {id, name}
        missing_buckets: strategic pillars without historical positive monthly spend
        missing_products: {pillar: [product_names]} for pillars they DO use
    """
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify(error='Customer not found'), 404

    from app.models import StrategicPillarRevenueData
    from app.services.whitespace import STRATEGIC_PILLARS, monthly_revenue
    monthly = monthly_revenue().c
    all_buckets = set(STRATEGIC_PILLARS)

    # All distinct products per bucket
    all_products_rows = (
        db.session.query(StrategicPillarRevenueData.pillar, StrategicPillarRevenueData.product)
        .distinct()
        .all()
    )
    all_products_by_bucket = {}
    for bucket, product in all_products_rows:
        if bucket and product:
            all_products_by_bucket.setdefault(bucket, set()).add(product)

    # This customer's buckets (any non-zero spend ever)
    customer_buckets = {
        r[0] for r in
        db.session.query(monthly.bucket)
        .filter(
            monthly.customer_id == customer_id,
            monthly.revenue > 0,
        )
        .distinct()
        .all()
        if r[0]
    }

    # This customer's products per bucket
    customer_products_rows = (
        db.session.query(StrategicPillarRevenueData.pillar, StrategicPillarRevenueData.product)
        .filter(
            StrategicPillarRevenueData.customer_id == customer_id,
            StrategicPillarRevenueData.revenue > 0,
        )
        .distinct()
        .all()
    )
    customer_products_by_bucket = {}
    for bucket, product in customer_products_rows:
        if bucket and product:
            customer_products_by_bucket.setdefault(bucket, set()).add(product)

    # Missing buckets
    missing_buckets = sorted(all_buckets - customer_buckets)

    # Missing products within buckets they DO use
    missing_products = {}
    for bucket in customer_buckets:
        all_prods = all_products_by_bucket.get(bucket, set())
        cust_prods = customer_products_by_bucket.get(bucket, set())
        missing = sorted(all_prods - cust_prods)
        if missing:
            missing_products[bucket] = missing

    return jsonify(
        customer={'id': customer.id, 'name': customer.name},
        missing_buckets=missing_buckets,
        missing_products=missing_products,
    )


@bp.route('/api/reports/whitespace/penetration')
def api_whitespace_penetration():
    """Return pillar penetration with the existing historical, zero-inclusive scope.

    Query params:
        buckets: comma-separated bucket names (optional, defaults to all)

    Returns JSON list of:
        {bucket, customers_with_spend, total_customers, penetration_pct}
    """
    bucket_param = request.args.get('buckets', '')
    selected_buckets = (
        [b.strip() for b in bucket_param.split(',') if b.strip()]
        if bucket_param else None
    )
    from app.models import StrategicPillarRevenueData
    from app.services.whitespace import STRATEGIC_PILLARS, monthly_revenue
    monthly = monthly_revenue().c

    # Total customers with any revenue data
    total_customers = (
        db.session.query(func.count(func.distinct(StrategicPillarRevenueData.customer_id)))
        .filter(StrategicPillarRevenueData.customer_id.isnot(None))
        .scalar()
    ) or 0

    if total_customers == 0:
        return jsonify([])

    # Customers with spend per bucket
    query = (
        db.session.query(
            monthly.bucket,
            func.count(func.distinct(monthly.customer_id))
        )
        .filter(
            monthly.customer_id.isnot(None),
            monthly.revenue > 0,
        )
    )
    if selected_buckets:
        query = query.filter(monthly.bucket.in_(selected_buckets))
    bucket_counts = dict(query.group_by(monthly.bucket).all())

    result = []
    for bucket in sorted(selected_buckets or STRATEGIC_PILLARS):
        count = bucket_counts.get(bucket, 0)
        pct = round(count / total_customers * 100, 1) if total_customers else 0
        result.append({
            'bucket': bucket,
            'customers_with_spend': count,
            'total_customers': total_customers,
            'penetration_pct': pct,
        })

    return jsonify(result)


@bp.route('/api/reports/whitespace/penetration/customers')
def api_whitespace_penetration_customers():
    """Return customers with and without historical positive spend in a pillar.

    Query params:
        bucket: bucket name (required)

    Returns JSON:
        {bucket, with_spend: [{id, name, nickname}], without_spend: [{id, name, nickname}]}
    """
    bucket = request.args.get('bucket', '').strip()
    if not bucket:
        return jsonify(error='Strategic pillar name is required'), 400
    from app.models import StrategicPillarRevenueData
    from app.services.whitespace import monthly_revenue
    monthly = monthly_revenue().c

    # All customer IDs with any revenue data
    all_customer_ids = {
        r[0] for r in
        db.session.query(func.distinct(StrategicPillarRevenueData.customer_id))
        .filter(StrategicPillarRevenueData.customer_id.isnot(None))
        .all()
    }

    # Customer IDs with spend > 0 in this bucket
    with_spend_ids = {
        r[0] for r in
        db.session.query(func.distinct(monthly.customer_id))
        .filter(
            monthly.customer_id.isnot(None),
            monthly.bucket == bucket,
            monthly.revenue > 0,
        )
        .all()
    }

    without_spend_ids = all_customer_ids - with_spend_ids

    # Fetch customer details
    with_spend = (
        Customer.query.filter(Customer.id.in_(with_spend_ids))
        .order_by(Customer.name).all()
    ) if with_spend_ids else []
    without_spend = (
        Customer.query.filter(Customer.id.in_(without_spend_ids))
        .order_by(Customer.name).all()
    ) if without_spend_ids else []

    return jsonify(
        bucket=bucket,
        with_spend=[
            {'id': c.id, 'name': c.name, 'nickname': c.nickname}
            for c in with_spend
        ],
        without_spend=[
            {'id': c.id, 'name': c.name, 'nickname': c.nickname}
            for c in without_spend
        ],
    )


# =============================================================================
# MSX Workspace
# =============================================================================

def _read_hva_inventory() -> dict:
    """Validate browser/API filters through the shared inventory service."""
    from app.services.hva_inventory import get_hva_inventory

    return get_hva_inventory(
        search=request.args.get('search', ''), status=request.args.get('status', ''),
        customer_id=int(request.args['customer_id']) if request.args.get('customer_id') else None,
        category=int(request.args['category']) if request.args.get('category') else None,
        date_field=request.args.get('date_field', 'created'),
        date_from=request.args.get('date_from'), date_to=request.args.get('date_to'),
        sort=request.args.get('sort', 'created'), direction=request.args.get('direction', 'desc'),
        page=int(request.args.get('page', '1')), per_page=int(request.args.get('per_page', '50')),
    )


@bp.route('/reports/hvas')
def report_hvas():
    """Browse every owned HVA task without starting from a meeting or milestone."""
    from app.services.msx_api import TASK_CATEGORIES

    try:
        data = _read_hva_inventory()
    except ValueError as exc:
        logger.info('Invalid HVA inventory filters: %s', exc)
        abort(400, description=str(exc))

    def inventory_url(**changes) -> str:
        """Preserve current filters across global sorting and pagination."""
        return url_for('reports.report_hvas', **{**request.args.to_dict(), **changes})

    return render_template(
        'report_hvas.html', **data, inventory_url=inventory_url,
        customers=Customer.query.order_by(Customer.name).all(),
        categories=[category for category in TASK_CATEGORIES if category['is_hva']],
        task_categories=TASK_CATEGORIES,
    )


@bp.route('/api/reports/hvas')
def api_hvas():
    """Read the same bounded HVA inventory used by the browse view."""
    try:
        return jsonify(success=True, **_read_hva_inventory())
    except ValueError as exc:
        logger.info('Invalid HVA inventory filters: %s', exc)
        return jsonify(success=False, error=str(exc)), 400


@bp.post('/api/reports/hvas/<int:task_id>/category')
def update_hva_category(task_id: int) -> Response | tuple[Response, int]:
    """Update an owned task's MSX category and keep the local HVA classification in sync."""
    from app.services.hva_inventory import change_hva_category
    from app.services.msx_api import TASK_CATEGORIES

    data = request.get_json(silent=True)
    code = data.get('category') if isinstance(data, dict) else None
    category = next((item for item in TASK_CATEGORIES if item['value'] == code), None)
    if isinstance(code, bool) or not isinstance(code, int) or category is None:
        logger.info('Invalid category change for task %s', task_id)
        return jsonify(success=False, error='Choose a valid task category.'), 400
    task = MsxTask.query.filter_by(id=task_id, is_current_owner=True).one_or_none()
    if task is None:
        logger.info('Category change rejected for missing or unowned task %s', task_id)
        return jsonify(success=False, error='Owned task not found. Refresh MSX tasks.'), 404
    if task.task_category != code:
        result = change_hva_category(task, code)
        if not result.get('success'):
            error = result.get('error') or 'MSX could not update this task category.'
            logger.warning('MSX category update failed for task %s: %s', task_id, error)
            return jsonify(success=False, error=error,
                           remote_changed=result.get('remote_changed', False)), 502
    try:
        task.task_category = code
        task.task_category_name = category['label']
        task.is_hva = category['is_hva']
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Category cache save failed for task %s after MSX update', task_id)
        return jsonify(success=False, remote_changed=True, error=(
            'Category saved in MSX, but Sales Buddy could not save the updated category. '
            'Refresh MSX tasks before retrying.'
        )), 500
    return jsonify(success=True, category=code, category_name=category['label'],
                   is_hva=category['is_hva'])


@bp.post('/api/reports/hvas/<int:task_id>/complete', defaults={'completed': True})
@bp.post('/api/reports/hvas/<int:task_id>/reopen', endpoint='reopen_hva',
         defaults={'completed': False})
def complete_hva(task_id: int, completed: bool) -> Response | tuple[Response, int]:
    """Set an owned HVA's completion state while retaining its historical links."""
    from app.services.msx_api import HVA_TASK_CATEGORIES, close_task, reopen_task

    action = 'complete' if completed else 'reopen'

    task = MsxTask.query.filter(
        MsxTask.id == task_id, MsxTask.is_current_owner.is_(True),
        MsxTask.is_hva.is_(True), MsxTask.task_category.in_(HVA_TASK_CATEGORIES),
    ).one_or_none()
    if task is None:
        logger.info('HVA %s rejected for missing or ineligible task %s', action, task_id)
        return jsonify(success=False, error='Owned HVA not found. Refresh MSX tasks.'), 404
    if task.statecode == 2:
        logger.info('HVA %s rejected for canceled task %s', action, task_id)
        return jsonify(success=False, error='Canceled HVAs are left unchanged.'), 409
    target_state = 1 if completed else 0
    if task.statecode == target_state:
        return jsonify(success=True, already_completed=completed, statecode=target_state)
    result = close_task(task.msx_task_id) if completed else reopen_task(task.msx_task_id)
    if not result.get('success'):
        error = result.get('error') or f'MSX could not {action} this HVA.'
        logger.warning('MSX %s failed for HVA %s: %s', action, task_id, error)
        return jsonify(success=False, error=error), 502
    try:
        task.statecode = target_state
        task.statuscode = 5 if completed else 2
        task.actual_end = datetime.now(timezone.utc) if completed else None
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('MSX %s succeeded for HVA %s but cache save failed', action, task_id)
        return jsonify(success=False, remote_changed=True, error=(
            'Updated in MSX, but Sales Buddy could not save the updated status. '
            'Refresh MSX tasks before retrying.'
        )), 500
    return jsonify(success=True, statecode=target_state)


@bp.route('/reports/msx-workspace')
def report_msx_workspace():
    """MSX Workspace - browse opportunities, milestones, and tasks from MSX."""
    from app.services.msx_api import TASK_CATEGORIES

    customers = (
        Customer.query
        .filter(Customer.tpid.isnot(None))
        .order_by(Customer.name)
        .all()
    )

    # Build seller list and customer-seller mapping for SE mode filtering
    sellers = Seller.query.order_by(Seller.name).all()
    customer_seller_map = {
        c.id: c.seller_id for c in customers if c.seller_id
    }

    return render_template(
        'report_msx_workspace.html',
        customers=customers,
        sellers=sellers,
        customer_seller_map=customer_seller_map,
        task_categories=TASK_CATEGORIES,
    )


@bp.route('/api/reports/msx-workspace/opportunities')
def api_msx_workspace_opportunities():
    """JSON endpoint: opportunities with filters for the MSX workspace."""
    customer_id = request.args.get('customer_id', type=int)
    seller_id = request.args.get('seller_id', type=int)
    status_filter = request.args.get('status', '')  # 'open', 'won', 'lost', or ''
    search = request.args.get('search', '').strip()
    team_filter = request.args.get('team', '')  # 'on', 'off', or ''

    query = Opportunity.query

    if customer_id:
        query = query.filter(Opportunity.customer_id == customer_id)
    elif seller_id:
        seller_customer_ids = [c.id for c in Customer.query.filter_by(seller_id=seller_id).all()]
        query = query.filter(Opportunity.customer_id.in_(seller_customer_ids))
    else:
        # Only show opportunities for customers we track
        query = query.filter(Opportunity.customer_id.isnot(None))

    if status_filter == 'open':
        query = query.filter(Opportunity.statecode == 0)
    elif status_filter == 'won':
        query = query.filter(Opportunity.statecode == 1)
    elif status_filter == 'lost':
        query = query.filter(Opportunity.statecode == 2)

    if team_filter == 'on':
        query = query.filter(Opportunity.on_deal_team.is_(True))
    elif team_filter == 'off':
        query = query.filter(Opportunity.on_deal_team.is_(False))

    if search:
        pattern = f'%{search}%'
        query = query.filter(or_(
            Opportunity.name.ilike(pattern),
            Opportunity.opportunity_number.ilike(pattern),
            Opportunity.owner_name.ilike(pattern),
        ))

    opportunities = query.order_by(Opportunity.name).all()

    # Pre-compute milestone ACR sums for opportunities missing estimated_value
    opp_ids_no_value = [o.id for o in opportunities if not o.estimated_value]
    ms_acr_sums = {}
    if opp_ids_no_value:
        rows = (
            db.session.query(
                Milestone.opportunity_id,
                func.sum(Milestone.monthly_usage),
            )
            .filter(
                Milestone.opportunity_id.in_(opp_ids_no_value),
                Milestone.msx_status != 'Cancelled',
                Milestone.monthly_usage.isnot(None),
            )
            .group_by(Milestone.opportunity_id)
            .all()
        )
        ms_acr_sums = {row[0]: row[1] for row in rows}

    results = []
    for opp in opportunities:
        cust = opp.customer
        acr = opp.estimated_value
        acr_from_milestones = False
        if not acr:
            ms_sum = ms_acr_sums.get(opp.id)
            if ms_sum:
                acr = ms_sum
                acr_from_milestones = True
        results.append({
            'id': opp.id,
            'msx_id': opp.msx_opportunity_id,
            'name': opp.name,
            'number': opp.opportunity_number,
            'state': opp.state or 'Open',
            'status_reason': opp.status_reason,
            'estimated_value': acr,
            'acr_from_milestones': acr_from_milestones,
            'estimated_close_date': opp.estimated_close_date,
            'owner_name': opp.owner_name,
            'on_deal_team': opp.on_deal_team,
            'customer_name': cust.name if cust else None,
            'customer_id': opp.customer_id,
            'msx_url': opp.msx_url,
            'description': opp.description,
            'customer_need': opp.customer_need,
            'compete_threat': opp.compete_threat,
        })

    return jsonify(opportunities=results, count=len(results))


@bp.route('/api/reports/msx-workspace/milestones')
def api_msx_workspace_milestones():
    """JSON endpoint: milestones with filters for the MSX workspace."""
    customer_id = request.args.get('customer_id', type=int)
    seller_id = request.args.get('seller_id', type=int)
    opportunity_id = request.args.get('opportunity_id', type=int)
    status_filter = request.args.get('status', '')  # comma-separated
    team_filter = request.args.get('team', '')  # 'on', 'off', or ''
    search = request.args.get('search', '').strip()

    query = Milestone.query

    if customer_id:
        query = query.filter(Milestone.customer_id == customer_id)
    elif seller_id:
        seller_customer_ids = [c.id for c in Customer.query.filter_by(seller_id=seller_id).all()]
        query = query.filter(Milestone.customer_id.in_(seller_customer_ids))
    else:
        query = query.filter(Milestone.customer_id.isnot(None))

    if opportunity_id:
        query = query.filter(Milestone.opportunity_id == opportunity_id)

    if status_filter:
        statuses = [s.strip() for s in status_filter.split(',') if s.strip()]
        if statuses:
            query = query.filter(Milestone.msx_status.in_(statuses))

    if team_filter == 'on':
        query = query.filter(Milestone.on_my_team.is_(True))
    elif team_filter == 'off':
        query = query.filter(Milestone.on_my_team.is_(False))

    if search:
        pattern = f'%{search}%'
        query = query.filter(or_(
            Milestone.title.ilike(pattern),
            Milestone.milestone_number.ilike(pattern),
            Milestone.workload.ilike(pattern),
            Milestone.owner_name.ilike(pattern),
        ))

    milestones = query.order_by(Milestone.due_date).all()

    results = []
    for ms in milestones:
        cust = ms.customer
        opp = ms.opportunity
        comment_count = 0
        if ms.cached_comments_json:
            try:
                import json
                comments = json.loads(ms.cached_comments_json)
                comment_count = len(comments) if isinstance(comments, list) else 0
            except (json.JSONDecodeError, TypeError):
                pass
        results.append({
            'id': ms.id,
            'msx_id': ms.msx_milestone_id,
            'title': ms.title,
            'number': ms.milestone_number,
            'status': ms.msx_status,
            'due_date': ms.due_date.strftime('%Y-%m-%d') if ms.due_date else None,
            'due_date_urgency': ms.due_date_urgency,
            'dollar_value': ms.dollar_value,
            'monthly_usage': ms.monthly_usage,
            'workload': ms.workload,
            'workload_area': ms.workload.split(':', 1)[0].strip() if ms.workload and ':' in ms.workload else (ms.workload.strip() if ms.workload else ''),
            'owner_name': ms.owner_name,
            'on_my_team': ms.on_my_team,
            'customer_name': cust.name if cust else None,
            'customer_id': ms.customer_id,
            'opportunity_name': opp.name if opp else ms.opportunity_name,
            'opportunity_id': ms.opportunity_id,
            'commitment': ms.customer_commitment,
            'comment_count': comment_count,
            'url': ms.url,
        })

    return jsonify(milestones=results, count=len(results))


@bp.route('/api/reports/msx-workspace/tasks')
def api_msx_workspace_tasks():
    """JSON endpoint: tasks from local DB (synced from MSX)."""
    customer_id = request.args.get('customer_id', type=int)
    seller_id = request.args.get('seller_id', type=int)
    milestone_id = request.args.get('milestone_id', type=int)
    search = request.args.get('search', '').strip()

    query = (
        MsxTask.query
        .join(Milestone, MsxTask.milestone_id == Milestone.id)
        .filter(or_(MsxTask.statecode.is_(None), MsxTask.statecode == 0))
    )

    if customer_id:
        query = query.filter(Milestone.customer_id == customer_id)
    elif seller_id:
        seller_customer_ids = [c.id for c in Customer.query.filter_by(seller_id=seller_id).all()]
        query = query.filter(Milestone.customer_id.in_(seller_customer_ids))
    else:
        query = query.filter(Milestone.customer_id.isnot(None))

    if milestone_id:
        query = query.filter(MsxTask.milestone_id == milestone_id)

    if search:
        pattern = f'%{search}%'
        query = query.filter(or_(
            MsxTask.subject.ilike(pattern),
            MsxTask.task_category_name.ilike(pattern),
        ))

    tasks = query.order_by(MsxTask.due_date.asc().nullslast()).all()

    results = []
    for t in tasks:
        ms = t.milestone
        results.append({
            'task_id': t.msx_task_id,
            'subject': t.subject,
            'description': t.description,
            'task_category': t.task_category,
            'task_category_name': t.task_category_name,
            'is_hva': t.is_hva,
            'due_date': t.due_date.isoformat() if t.due_date else None,
            'milestone_msx_id': ms.msx_milestone_id if ms else None,
            'milestone_title': ms.title if ms else None,
            'milestone_id': t.milestone_id,
            'task_url': t.msx_task_url,
            'customer_id': ms.customer_id if ms else None,
        })

    return jsonify(tasks=results, count=len(results))


@bp.route('/reports/marketing-insights')
def report_marketing_insights():
    """Marketing Insights report - shows synced marketing engagement data.

    Displays account-level summary stats, per-sales-play breakdown,
    and engaged contacts for all customers with marketing data.
    """
    # Get sync status
    sync_status = SyncStatus.get_status('marketing')

    # Customers with marketing data, ordered by total interactions desc
    summaries = (
        MarketingSummary.query
        .join(Customer, MarketingSummary.customer_id == Customer.id)
        .order_by(MarketingSummary.total_interactions.desc())
        .all()
    )

    # Build per-customer data with interactions and contacts
    customer_data = []
    for s in summaries:
        interactions = (
            MarketingInteraction.query
            .filter_by(customer_id=s.customer_id)
            .order_by(MarketingInteraction.all_interactions.desc())
            .all()
        )
        contacts = (
            MarketingContact.query
            .filter_by(customer_id=s.customer_id)
            .order_by(
                (MarketingContact.mail_interactions + MarketingContact.meeting_interactions).desc()
            )
            .limit(10)
            .all()
        )
        customer_data.append({
            'customer': s.customer,
            'summary': s,
            'interactions': interactions,
            'contacts': contacts,
        })

    # Aggregate stats across all customers
    total_customers = len(summaries)
    total_interactions = sum(s.total_interactions for s in summaries)
    total_contacts = sum(s.engaged_contacts for s in summaries)

    return render_template(
        'report_marketing_insights.html',
        customer_data=customer_data,
        total_customers=total_customers,
        total_interactions=total_interactions,
        total_contacts=total_contacts,
        sync_status=sync_status,
    )


# =============================================================================
# Shared Connect Goals metrics and former page destination
# =============================================================================

@bp.route('/reports/connect-goals')
def connect_goals_dashboard():
    """Send old bookmarks to the editable starter, or offer explicit restoration."""
    from app.services.dashboard_templates import get_template_dashboard_url

    return redirect(get_template_dashboard_url('connect-goals'))


@bp.route('/api/reports/connect-goals')
def api_connect_goals():
    """Return the shared FY27 Data Connect Goals calculations."""
    from app.services.connect_goals import get_connect_goals

    return jsonify(get_connect_goals())


# =============================================================================
# U2C Attainment Report
# =============================================================================

@bp.route('/reports/u2c')
def report_u2c():
    """U2C Attainment report - quarterly milestone commitment tracking."""
    from app.services.u2c_snapshot import (
        U2C_TARGET,
        current_fiscal_quarter, fiscal_quarter_date_range, get_attainment,
        get_attainment_trend_by_workload,
    )

    # Get all snapshots for the dropdown
    snapshots = (
        U2CSnapshot.query
        .order_by(U2CSnapshot.fiscal_quarter.desc())
        .all()
    )

    # Determine which snapshot to show
    selected_fq = request.args.get('fq')
    current_fq = current_fiscal_quarter()

    snapshot = None
    attainment = None
    trend = {}
    trend_bounds = None

    if selected_fq:
        snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=selected_fq).first()
    elif snapshots:
        # Default to current FQ snapshot, or most recent
        snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=current_fq).first()
        if not snapshot:
            snapshot = snapshots[0]

    if snapshot:
        from app.services.one_on_one import add_agenda_state_to_milestone_rows

        attainment = get_attainment(snapshot.id)
        add_agenda_state_to_milestone_rows(attainment['remaining_items'])
        trend = get_attainment_trend_by_workload(snapshot.id, include_live=True, detailed=True)
        quarter_start, quarter_end = fiscal_quarter_date_range(
            snapshot.fiscal_quarter)
        trend_bounds = {
            'start': quarter_start.isoformat(),
            'end': quarter_end.isoformat(),
        }

    # The official MSXi import is scoped by the territories configured here.
    territory_count = Territory.query.count()

    # When the baseline last refreshed, and what MSXi load it came from. The
    # two are different questions: we check daily, MSXi publishes weekly.
    u2c_sync_status = SyncStatus.get_status('u2c_import')
    u2c_sync_error = None
    if u2c_sync_status.get('state') == 'failed':
        try:
            sync_details = json.loads(u2c_sync_status.get('details') or '{}')
        except (TypeError, json.JSONDecodeError):
            logger.warning("U2C sync status contains invalid JSON details")
            sync_details = {}
        u2c_sync_error = sync_details.get('error') or (
            'Check your VPN connection, Azure sign-in, and configured territories.'
        )

    return render_template(
        'report_u2c.html',
        snapshots=snapshots,
        snapshot=snapshot,
        attainment=attainment,
        current_fq=current_fq,
        territory_count=territory_count,
        last_checked=u2c_sync_status.get('completed_at'),
        u2c_sync_error=u2c_sync_error,
        trend=trend,
        trend_bounds=trend_bounds,
        u2c_goal=U2C_TARGET,
    )


@bp.route('/api/reports/u2c/import-official', methods=['POST'])
def api_u2c_import_official():
    """Admit a durable sync; the worker owns its execution."""
    from app.routes.jobs import start_response
    return start_response('u2c', {'force': True})


def _get_connect_impact_date_presets(reference: date) -> list[dict[str, str]]:
    """Build current Microsoft fiscal-year start-date shortcuts, without end dates."""
    from app.services.u2c_snapshot import current_fiscal_quarter, fiscal_quarter_date_range

    fiscal_year = current_fiscal_quarter(reference).split()[0]
    presets = []
    for quarter in range(1, 5):
        start, _ = fiscal_quarter_date_range(f'{fiscal_year} Q{quarter}')
        label = f'{fiscal_year} start / Q1' if quarter == 1 else f'{fiscal_year} Q{quarter} start'
        presets.append({'since': start.isoformat(), 'label': f'{label} ({start.isoformat()})'})
    return presets


@bp.route('/reports/connect-impact')
def report_connect_impact():
    """Connect Impact report.

    Ranks customers by total estimated ACR/mo from committed milestones
    where the user is on the milestone team, committed on or after a
    configurable start date. Helps identify highest-impact customers
    for Connect prep.
    """
    since_str = request.args.get('since', '')
    try:
        since_date = datetime.strptime(since_str, '%Y-%m-%d').date() if since_str else None
    except ValueError:
        since_date = None

    # Default: 5 months ago (client sends this from localStorage)
    if not since_date:
        since_date = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
        # Go back 5 months from today
        m = date.today().month - 5
        y = date.today().year
        while m < 1:
            m += 12
            y -= 1
        since_date = date(y, m, date.today().day if date.today().day <= 28 else 28)

    # Milestones committed since the start date where I'm on the team
    milestones = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,
            Milestone.customer_commitment == 'Committed',
            Milestone.committed_at >= datetime(
                since_date.year, since_date.month, since_date.day,
            ),
        )
        .options(db.joinedload(Milestone.customer))
        .all()
    )

    # Group by customer and sum monthly_usage
    customer_map: dict[int, dict] = {}
    for ms in milestones:
        if not ms.customer_id:
            continue
        cid = ms.customer_id
        if cid not in customer_map:
            customer_map[cid] = {
                'customer': ms.customer,
                'total_acr': 0.0,
                'milestones': [],
            }
        customer_map[cid]['total_acr'] += ms.monthly_usage or 0.0
        customer_map[cid]['milestones'].append(ms)

    # Sort by total ACR descending
    ranked = sorted(customer_map.values(), key=lambda x: x['total_acr'], reverse=True)

    # Grand total
    grand_total = sum(r['total_acr'] for r in ranked)

    return render_template(
        'report_connect_impact.html',
        since_date=since_date.isoformat(),
        date_presets=_get_connect_impact_date_presets(date.today()),
        ranked=ranked,
        grand_total=grand_total,
    )
