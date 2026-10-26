"""Customer/Seller scope boundaries, persistence, and isolated widget calculations."""
from dataclasses import replace
from unittest.mock import Mock
import json

from bs4 import BeautifulSoup
import pytest
from werkzeug.datastructures import MultiDict

from app.models import Customer, Dashboard, DashboardWidget, Engagement, Seller, db
from app.services import dashboard_filters as filters, dashboards
from tests.test_dashboard_drafts import draft_payload
from tests.test_dashboards import BASE, add_widget, create_dashboard


pytestmark = pytest.mark.usefixtures('dashboard_sources_ready')


GROUPING_WIDGETS = (
    'ptof.upcoming_renewals', 'fabric.trial_conversion', 'fabric.expansion',
    'fabric.modernization',
)


@pytest.mark.parametrize('key', GROUPING_WIDGETS)
def test_live_grouping_never_changes_saved_widgets_or_customer_scope(
    client, app, population, monkeypatch, key,
):
    """One enabled viewer choice groups each supported widget without changing saved filters."""
    customers, _ = population
    provider = Mock(return_value={'destination': '/reports/fabric-consumption'})
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, key, replace(
        dashboards.WIDGET_DEFINITIONS[key], provider=provider, requirements=(),
    ))
    dashboard_id = create_dashboard(client)
    assert client.post(f'{BASE}/{dashboard_id}/widgets', json={
        'key': key, 'filters': {'customer_ids': customers[:1]},
    }).status_code == 200
    payload = draft_payload(client, dashboard_id)
    payload['filter_config'] = {'live_filters': ['group_by_seller']}
    assert client.post(f'{BASE}/{dashboard_id}/save', json=payload).status_code == 200
    before = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    provider.reset_mock()
    with app.app_context():
        preview = dashboards.get_widget_preview(
            key, 'detailed', 6, filter_context={
                'dashboard': payload['filter_config'],
                'widget': {'customer_ids': customers[:1]},
                'live': {'group_by_seller': True},
            },
        )
        assert preview['group_by_seller'] is True
        assert preview['filters'] == filters.normalize_filters({'customer_ids': customers[:1]})
        assert preview['filter_info']['scoped']
        provider.assert_called_once_with(None, customer_ids=(customers[0],))
    provider.reset_mock()
    data = client.get(f'{BASE}/{dashboard_id}?group_by_seller=true').json['dashboard']
    grouped = data['widgets'][0]
    assert grouped['group_by_seller'] is True
    assert grouped['filters'] == before['widgets'][0]['filters']
    assert data['layout_revision'] == before['layout_revision']
    provider.assert_called_once_with(None, customer_ids=(customers[0],))
    assert client.get(f'{BASE}/{dashboard_id}?group_by_seller=false').json['dashboard'] == before
    assert client.get(f'{BASE}/{dashboard_id}').json['dashboard'] == before


@pytest.mark.parametrize('grouping', [None, 0, 1, 'true', [], {}])
def test_live_grouping_rejects_non_booleans(grouping):
    """Truthiness must not turn malformed live selections into grouped output."""
    with pytest.raises(ValueError, match='boolean'):
        filters.normalize_live_filters({'group_by_seller': grouping})


def test_grouping_is_only_available_as_a_live_control():
    """Permanent scope and per-widget restrictions cannot silently save viewer presentation."""
    with pytest.raises(ValueError):
        filters.normalize_filter_config({'scope': {'group_by_seller': True}})
    assert filters.normalize_filter_config(
        {'live_filters': ['group_by_seller']},
    )['live_filters'] == ['group_by_seller']
    with pytest.raises(ValueError):
        dashboards.validate_widget_filters('fabric.expansion', {'group_by_seller': True})


def test_live_grouping_shares_calculations_and_skips_unsupported_widgets(client, app, monkeypatch):
    """Group supported instances only, without splitting shared provider calculations."""
    key = 'fabric.expansion'
    provider = Mock(return_value={'destination': '/reports/fabric-consumption'})
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, key, replace(
        dashboards.WIDGET_DEFINITIONS[key], provider=provider, requirements=(),
    ))
    dashboard_id = create_dashboard(client)
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, 'hygiene.gaps', replace(
        dashboards.WIDGET_DEFINITIONS['hygiene.gaps'], provider=provider, requirements=(),
    ))
    for widget_key in (key, key, 'hygiene.gaps'):
        assert client.post(f'{BASE}/{dashboard_id}/widgets', json={
            'key': widget_key,
        }).status_code == 200
    payload = draft_payload(client, dashboard_id)
    payload['filter_config'] = {'live_filters': ['group_by_seller']}
    assert client.post(f'{BASE}/{dashboard_id}/save', json=payload).status_code == 200
    provider.reset_mock()
    widgets = client.get(f'{BASE}/{dashboard_id}?group_by_seller=true').json['dashboard']['widgets']
    assert [item['group_by_seller'] for item in widgets] == [True, True, False]
    assert not any(item['filter_info']['scoped'] for item in widgets)
    assert provider.call_count == 2


@pytest.mark.parametrize('query', [
    'group_by_seller=yes', 'group_by_seller=1', 'group_by_seller=',
    'group_by_seller=true&group_by_seller=false',
])
def test_invalid_grouping_urls_fail_explicitly(client, query):
    """Both dashboard read surfaces reject malformed grouping rather than showing flat success."""
    dashboard_id = create_dashboard(client)
    for base in (BASE, '/reports/dashboards'):
        response = client.get(f'{base}/{dashboard_id}?{query}')
        assert response.status_code == 400


def test_disabled_grouping_is_rejected_in_view_and_preview(client, app):
    """A viewer cannot activate a control that the dashboard has not enabled."""
    dashboard_id = create_dashboard(client)
    for base in (BASE, '/reports/dashboards'):
        assert client.get(f'{base}/{dashboard_id}?group_by_seller=true').status_code == 400
    with app.app_context(), pytest.raises(ValueError, match='not enabled'):
        dashboards.get_widget_preview(
            'fabric.expansion', 'detailed', 6,
            filter_context={'live': {'group_by_seller': True}},
        )


@pytest.fixture
def population(app):
    """Create independent account owners without relying on live data."""
    with app.app_context():
        first = Seller(name='First owner')
        second = Seller(name='Second owner')
        customers = [
            Customer(name='First account', seller=first, tpid=990001),
            Customer(name='Other first account', seller=first, tpid=990002),
            Customer(name='Second account', seller=second, tpid=990003),
            Customer(name='Unassigned account', tpid=990004),
        ]
        db.session.add_all([first, second, *customers])
        db.session.commit()
        return [row.id for row in customers], [first.id, second.id]


def test_layers_intersect_dimensions_and_preserve_explicit_no_matches(app, population):
    """Multi-select OR never turns an impossible AND or a deleted ID into all customers."""
    customers, sellers = population
    layer = filters.normalize_filters
    with app.app_context():
        assert filters.resolve_customer_scope(layer()) is None
        assert filters.resolve_customer_scope(layer({'seller_ids': [sellers[0]]})) == tuple(
            customers[:2]
        )
        assert filters.resolve_customer_scope(layer({'customer_ids': customers[:3]}), layer({
            'seller_ids': [sellers[0], sellers[1]],
        }), layer({'customer_ids': [customers[1]]})) == (customers[1],)
        assert filters.resolve_customer_scope(layer({'customer_ids': [customers[0]]}), layer({
            'seller_ids': [sellers[1]],
        })) == ()
        assert filters.resolve_customer_scope(layer({'customer_ids': [99999999]})) == ()
        assert filters.resolve_customer_scope(layer({'seller_ids': [99999999]})) == ()


@pytest.mark.parametrize('value', [
    [], '', 1, {'unknown': []}, {'customer_ids': '1'}, {'seller_ids': None},
    {'customer_ids': [True]}, {'customer_ids': [0]}, {'customer_ids': ['1']},
    {'seller_ids': [-1]}, {'customer_ids': [1.1]}, {'customer_ids': list(range(1, 1002))},
    {'customer_ids': [2 ** 63]},
])
def test_invalid_selection_shapes_raise_explicit_errors(value):
    """Malformed IDs must not silently produce an unfiltered query."""
    with pytest.raises(ValueError):
        filters.normalize_filters(value)


@pytest.mark.parametrize('value', [
    [], {'unknown': []}, {'live_filters': 'customer_ids'},
    {'live_filters': ['unknown']}, {'live_filters': [{}]}, {'scope': []},
    {'scope': None},
])
def test_invalid_config_shapes_raise_explicit_errors(value):
    """Live controls and permanent boundaries have separate validated fields."""
    with pytest.raises(ValueError):
        filters.normalize_filter_config(value)


def install_scoped_widget(app, dashboard_id, customer_ids, monkeypatch):
    """Use one deterministic provider to inspect actual effective scope and caching."""
    provider = Mock(side_effect=lambda reference, **scope: {'customers': scope.get('customer_ids')})
    definition = dashboards.WIDGET_DEFINITIONS['hygiene.gaps']
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, 'hygiene.gaps',
                        replace(definition, provider=provider))
    with app.app_context():
        dashboard = db.session.get(Dashboard, dashboard_id)
        dashboard.filter_config = {'scope': {}, 'live_filters': ['customer_ids', 'seller_ids']}
        dashboard.widgets.extend([
            DashboardWidget(widget_key='hygiene.gaps', variant_key='metric',
                            filters={'customer_ids': [id]}, sort_order=index,
                            layout_row=index * 4, layout_column=0, column_span=6, row_span=4)
            for index, id in enumerate(customer_ids)
        ])
        db.session.commit()
    return provider


def test_same_widget_type_uses_effective_scope_cache_not_key(client, app, population, monkeypatch):
    """Different instances stay isolated; equal resolved scopes share the calculation."""
    customers, _ = population
    dashboard_id = create_dashboard(client)
    provider = install_scoped_widget(
        app, dashboard_id, [customers[0], customers[1], customers[0]], monkeypatch,
    )
    response = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    assert [row['data']['customers'] for row in response['widgets']] == [
        [customers[0]], [customers[1]], [customers[0]],
    ]
    assert provider.call_count == 2
    provider.reset_mock()
    response = client.get(f'{BASE}/{dashboard_id}?customer_ids={customers[2]}').json['dashboard']
    assert [row['data']['customers'] for row in response['widgets']] == [[], [], []]
    assert provider.call_count == 1
    with app.app_context():
        saved = db.session.get(Dashboard, dashboard_id)
        assert saved.filter_config['scope'] == {}
        assert saved.layout_revision == response['layout_revision']
        assert saved.widgets[0].filters == {'customer_ids': [customers[0]]}


def test_save_filters_atomic_and_report_read_parity(client, app, population, monkeypatch):
    """Dashboard scope and per-instance selections publish alongside layout in one revision."""
    customers, sellers = population
    dashboard_id = create_dashboard(client)
    install_scoped_widget(app, dashboard_id, [customers[0]], monkeypatch)
    payload = draft_payload(client, dashboard_id)
    payload['filter_config'] = {
        'scope': {'seller_ids': [sellers[0]]}, 'live_filters': ['customer_ids'],
    }
    payload['widgets'][0]['filters'] = {'customer_ids': customers[:3]}
    response = client.post(f'{BASE}/{dashboard_id}/save', json=payload)
    assert response.status_code == 200
    data = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    assert data['filter_config'] == filters.normalize_filter_config(payload['filter_config'])
    assert data['widgets'][0]['filters'] == filters.normalize_filters(
        payload['widgets'][0]['filters']
    )
    assert data['widgets'][0]['data']['customers'] == customers[:2]
    assert data['layout_revision'] == payload['revision'] + 1
    payload = draft_payload(client, dashboard_id)
    payload.update(name='Must not publish', filter_config={'scope': {'customer_ids': [False]}})
    assert client.post(f'{BASE}/{dashboard_id}/save', json=payload).status_code == 400
    assert client.get(f'{BASE}/{dashboard_id}').json['dashboard'] == data


def test_invalid_live_filter_never_returns_unfiltered_success(client, app, population, monkeypatch):
    """Disabled and malformed live inputs are explicit 400s on both read surfaces."""
    customers, _ = population
    dashboard_id = create_dashboard(client)
    install_scoped_widget(app, dashboard_id, customers[:1], monkeypatch)
    for suffix in ('customer_ids=x', 'seller_ids=-1', 'customer_ids=0', 'customer_ids=1.5'):
        for url in (f'{BASE}/{dashboard_id}', f'/reports/dashboards/{dashboard_id}'):
            response = client.get(url + '?' + suffix)
            assert response.status_code == 400
            assert response.json['success'] is False
    with app.app_context():
        db.session.get(Dashboard, dashboard_id).filter_config = {}
        db.session.commit()
    assert client.get(f'{BASE}/{dashboard_id}?customer_ids={customers[0]}').status_code == 400


def test_preview_uses_unsaved_scope_and_rejects_unsupported_widget_filters(
    client, app, population, monkeypatch,
):
    """Picker and editor previews share the saved widget calculation and effective boundaries."""
    customers, sellers = population
    dashboard_id = create_dashboard(client)
    provider = install_scoped_widget(app, dashboard_id, customers[:1], monkeypatch)
    context = {
        'dashboard': {'scope': {'customer_ids': customers[:2]}, 'live_filters': ['seller_ids']},
        'widget': {'customer_ids': [customers[1]]}, 'live': {'seller_ids': [sellers[1]]},
    }
    provider.reset_mock()
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, 'connect_goals.u2c', replace(
        dashboards.WIDGET_DEFINITIONS['connect_goals.u2c'], supported_filters=(),
    ))
    with app.app_context():
        preview = dashboards.get_widget_preview(
            'hygiene.gaps', 'metric', 6, filter_context=context,
        )
        assert preview['data']['customers'] == ()
        provider.assert_called_once_with(None, customer_ids=())
        assert preview['filter_info']['scoped'] is True
        with pytest.raises(ValueError, match='does not support'):
            dashboards.get_widget_preview(
                'connect_goals.u2c', 'metric', 6,
                filter_context={'widget': {'customer_ids': [customers[0]]}},
            )
    response = client.get(BASE + '/widget-preview', query_string={
        'key': 'hygiene.gaps', 'variant': 'metric', 'width': 6, 'filter_context': '{',
    })
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(Dashboard, dashboard_id).filter_config['scope'] == {}


def test_unsupported_widgets_are_honest_and_not_filtered(client, app, population, monkeypatch):
    """Portfolio aggregates don't masquerade as customer metrics."""
    customers, _ = population
    provider = Mock(return_value={'message': 'No data', 'destination': '/reports/u2c'})
    key = 'connect_goals.u2c'
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, key, replace(
        dashboards.WIDGET_DEFINITIONS[key], provider=provider, requirements=(), supported_filters=(),
    ))
    dashboard_id = create_dashboard(client)
    with app.app_context():
        saved = db.session.get(Dashboard, dashboard_id)
        saved.filter_config = {'scope': {'customer_ids': customers[:1]}}
        saved.widgets.append(DashboardWidget(
            widget_key=key, variant_key='metric', layout_row=0, layout_column=0,
            column_span=6, row_span=4,
        ))
        db.session.commit()
    data = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    assert data['widgets'][0]['filter_info']['ignored'] == ['Customer']
    provider.assert_called_once_with(None)
    html = client.get(f'/reports/dashboards/{dashboard_id}').data
    assert b'Portfolio-wide' in html and b'Does not follow Customer filters' in html
    page = BeautifulSoup(html, 'html.parser')
    config = json.loads(page.select_one('#dashboardFilterConfig').string)
    assert config['config']['scope']['customer_ids'] == customers[:1]


def test_report_scope_roundtrip_cannot_broaden_zero_matches(app, population):
    """Following a filtered report retains an empty result instead of loading the portfolio."""
    customers, _ = population
    with app.app_context():
        assert filters.report_customer_scope(MultiDict({'customer_scope': '1'})) == ()
        assert filters.report_customer_scope(MultiDict()) is None
        assert filters.report_customer_scope(MultiDict([
            ('customer_scope', '1'), ('customer_ids', str(customers[0])),
        ])) == (customers[0],)
        with pytest.raises(ValueError):
            filters.report_customer_scope(MultiDict({'customer_scope': 'invalid'}))
    assert filters.report_scope_query(()) == 'customer_scope=1'


def test_saved_filter_settings_are_in_editor_side_panel_only(client):
    """Permanent rules and widget restrictions are not duplicate inline viewer controls."""
    dashboard_id = create_dashboard(client)
    edit = BeautifulSoup(client.get(
        f'/reports/dashboards/{dashboard_id}/edit',
    ).data, 'html.parser')
    panel = edit.select_one('aside#dashboardFiltersPanel')
    assert panel.has_attr('hidden')
    assert panel['aria-labelledby'] == 'dashboardFiltersTitle'
    assert edit.select_one('#openDashboardFilters')['aria-controls'] == panel['id']
    rail = edit.select_one('.db-dashboard-workspace > button#openDashboardFilters')
    assert rail and 'db-filter-rail' in rail['class']
    assert not rail.has_attr('hidden')
    assert not edit.select_one('#layoutEditor #openDashboardFilters')
    assert panel.select_one('#closeDashboardFilters')['aria-label'] == 'Collapse filters'
    assert panel.select_one('#dashboardScopeControls')
    assert panel.select_one('#widgetFilterControls')
    assert panel.select_one('#dashboardFilterTab')['role'] == 'tab'
    assert panel.select_one('#widgetFilterTab').has_attr('disabled')
    assert panel.select_one('#dashboardFilterSettings')['role'] == 'tabpanel'
    assert panel.select_one('#widgetFilterSettings').has_attr('hidden')
    assert 'Always include' in panel.get_text()
    assert 'Available viewer filters' in panel.get_text()
    assert not edit.select_one('.db-dashboard #dashboardScopeControls')
    assert panel.select_one('#dashboardLiveFilters h3').get_text() == 'Filter dashboard'
    assert not edit.select_one('.db-dashboard #dashboardLiveControls')
    assert panel.select_one('#resetDashboardFilters').get_text() == 'Reset filters'
    view = BeautifulSoup(client.get(
        f'/reports/dashboards/{dashboard_id}',
    ).data, 'html.parser')
    assert view.select_one('#dashboardFiltersPanel').has_attr('hidden')
    assert view.select_one('#openDashboardFilters').has_attr('hidden')
    assert view.select_one('#dashboardFiltersPanel #dashboardLiveControls')
    assert not view.select_one('#dashboardFilterSettings, #widgetFilterSettings, [data-live-filter]')


def test_published_filter_rail_is_available_only_with_enabled_supported_controls(client, app):
    """Viewer affordances respect the saved author choice and never expose configuration."""
    dashboard_id = create_dashboard(client)
    add_widget(client, dashboard_id)
    with app.app_context():
        saved = db.session.get(Dashboard, dashboard_id)
        saved.filter_config = {'scope': {}, 'live_filters': ['customer_ids']}
        db.session.commit()
    page = BeautifulSoup(client.get(
        f'/reports/dashboards/{dashboard_id}',
    ).data, 'html.parser')
    rail = page.select_one('#openDashboardFilters')
    assert not rail.has_attr('hidden')
    assert rail['aria-controls'] == 'dashboardFiltersPanel'
    assert rail.select_one('#dashboardFilterCount').has_attr('hidden')
    assert page.select_one('#dashboardViewerScope')
    assert 'remembered for this dashboard in this browser' in page.select_one(
        '#dashboardTemporaryFilterHint',
    ).get_text()
    assert not page.select_one('#dashboardFilterTabs, #dashboardScopeControls')
    assert not page.select_one('.db-dashboard #dashboardLiveControls')


@pytest.mark.parametrize('mutation', ['null-config', 'null-widget', 'unsupported-widget'])
def test_invalid_filter_configuration_does_not_partially_publish(
    client, app, population, monkeypatch, mutation,
):
    """No invalid scope can clear a saved boundary or partially publish metadata."""
    customers, _ = population
    dashboard_id = create_dashboard(client)
    install_scoped_widget(app, dashboard_id, customers[:1], monkeypatch)
    before = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    payload = draft_payload(client, dashboard_id)
    payload['name'] = 'Must not publish'
    if mutation == 'null-config':
        payload['filter_config'] = None
    elif mutation == 'null-widget':
        payload['widgets'][0]['filters'] = None
    else:
        payload['widgets'][0]['filters'] = {'territory_ids': [1]}
    response = client.post(f'{BASE}/{dashboard_id}/save', json=payload)
    assert response.status_code == 400
    assert client.get(f'{BASE}/{dashboard_id}').json['dashboard'] == before


def test_scoped_hygiene_report_refresh_retains_boundary(client, app, population):
    """Report HTML, repair refresh URL, and an empty scope all agree with the widget."""
    customers, _ = population
    with app.app_context():
        db.session.add_all([
            Engagement(customer_id=customers[0], title='Visible scope gap', status='Active'),
            Engagement(customer_id=customers[1], title='Other scope gap', status='Active'),
        ])
        db.session.commit()
    response = client.get('/reports/hygiene', query_string={
        'customer_scope': '1', 'customer_ids': customers[0],
    })
    page = BeautifulSoup(response.data, 'html.parser')
    assert len(page.select('.engagement-gap-row')) == 1
    assert 'Visible scope gap' in page.select_one('.engagement-gap-row').get_text()
    assert page.select_one('[data-hygiene]')['data-customer-scope'] == (
        f'customer_scope=1&customer_ids={customers[0]}'
    )
    assert not BeautifulSoup(client.get(
        '/reports/hygiene?customer_scope=1&embed=1',
    ).data, 'html.parser').select('.engagement-gap-row')
    assert client.get('/reports/hygiene?customer_ids=invalid').status_code == 400


def test_fabric_report_filters_keep_dashboard_scope_when_resetting(client, app, population):
    """Search/signal submissions and reset links retain permanent report boundaries."""
    customers, _ = population
    response = client.get('/reports/fabric-capacity-pressure', query_string={
        'customer_scope': '1', 'customer_ids': customers[0], 'q': 'No such capacity',
    })
    assert response.status_code == 200
    page = BeautifulSoup(response.data, 'html.parser')
    form = page.select_one('form[method=get]')
    assert form.select_one('[name=customer_scope]')['value'] == '1'
    assert form.select_one('[name=customer_ids]')['value'] == str(customers[0])
    assert form.select_one('a[href]')['href'] == (
        f'/reports/fabric-capacity-pressure?customer_scope=1&customer_ids={customers[0]}'
    )
    assert client.get('/api/reports/fabric-capacity-pressure?customer_ids=x').status_code == 400
    empty = client.get('/api/reports/fabric-capacity-pressure?customer_scope=1')
    assert empty.status_code == 200 and empty.json['counts']['capacities'] == 0


def test_read_tools_use_saved_scope_and_optional_live_filters(client, app, population, monkeypatch):
    """SalesIQ tools reuse the same effective calculations as dashboard views."""
    from app.services.salesiq_tools import execute_tool, get_dashboards, report_dashboard_signals

    customers, _ = population
    dashboard_id = create_dashboard(client)
    install_scoped_widget(app, dashboard_id, customers[:1], monkeypatch)
    with app.app_context():
        tool = get_dashboards(dashboard_id, live_filters={'customer_ids': [customers[1]]})
        assert tool['widgets'][0]['data']['customers'] == ()
        assert execute_tool('get_dashboards', {
            'dashboard_id': dashboard_id, 'live_filters': {'customer_ids': [customers[1]]},
        })['widgets'][0]['data']['customers'] == ()
        assert report_dashboard_signals(
            'hygiene.gaps', filters={'customer_ids': [customers[0]]},
        )['customers'] == (customers[0],)
        monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, 'connect_goals.u2c', replace(
            dashboards.WIDGET_DEFINITIONS['connect_goals.u2c'], supported_filters=(),
        ))
        with pytest.raises(ValueError, match='does not support'):
            report_dashboard_signals(
                'connect_goals.u2c', filters={'customer_ids': [customers[0]]},
            )
