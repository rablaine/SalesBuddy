"""Database discovery reuses grid coverage for AI, Analytics and Apps-qualified gaps."""
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup
import pytest

from app.models import (
    Customer, RevenueImport, Seller, StrategicPillarRevenueData, UserPreference, db,
)
from app.services.dashboard_sync import WIDGET_SOURCES
from app.services.whitespace import (
    DATABASE_WHITESPACE_ADJACENT_PILLARS, get_database_whitespace_widget, get_whitespace_grid,
)
from app.services.salesiq_tools import execute_tool
from tests.test_dashboard_drafts import draft_payload
from tests.test_fabric_renewal_widgets import render_widget


@pytest.fixture
def database_rows(app, monkeypatch):
    """Create native pillar rows only in the isolated test database."""
    with app.app_context():
        from app.services.monthly_acr import complete_acr_months

        monkeypatch.setattr('app.services.whitespace.complete_acr_months',
                            lambda reference=None: complete_acr_months(
                                reference or date(2026, 10, 20)))
        pref = UserPreference.query.first()
        original = pref.user_role, pref.se_specialty
        pref.user_role, pref.se_specialty = 'se', 'data'
        imported = RevenueImport(filename='database-whitespace-test')
        db.session.add(imported)
        db.session.flush()

        def add(name, pillar='AI', amount=100, month=None, product='Source product'):
            """Append another product observation without guessing its pillar."""
            customer = Customer.query.filter_by(name=name).first()
            if customer is None:
                customer = Customer(name=name, tpid=100000 + Customer.query.count())
                db.session.add(customer)
                db.session.flush()
            for observed in ((month,) if month is not None else (7, 8, 9)):
                db.session.add(StrategicPillarRevenueData(
                    customer_id=customer.id, customer_name=name, tpid=customer.tpid,
                    pillar=pillar, product=product, month_date=date(2026, observed, 1),
                    fiscal_month=f'FY27-{observed}', revenue=amount, last_import_id=imported.id,
                ))
            db.session.flush()
            return customer

        yield add, pref
        pref.user_role, pref.se_specialty = original
        db.session.commit()


def test_candidates_exactly_match_existing_grid_database_gaps(client, database_rows):
    """Use pillar membership, credits, existing account scope and no new spend minimum."""
    add, _ = database_rows
    add('AI prospect')
    add('Small footprint', 'AI', 0.01)
    add('Rounded zero footprint', 'Apps', 0.001)
    add('Analytics prospect', 'Analytics', 100)
    add('Infra only', 'Infra', 1000)
    add('Developer only', 'Developer', 1000)
    add('Security only', 'Security', 1000)
    add('Zero qualifying spend', 'Infra', 1000)
    add('Zero qualifying spend', 'Apps', 0)
    add('Credit qualifying spend', 'Infra', 1000)
    add('Credit qualifying spend', 'Analytics', -10)
    add('Database user')
    add('Database user', 'Databases', 100, product='Unfamiliar database product')
    add('Tiny database user')
    add('Tiny database user', 'Databases', 0.001)
    add('Old database user')
    add('Old database user', 'Databases', 100, month=6)
    add('Net zero database')
    add('Net zero database', 'Databases', 10, month=8)
    add('Net zero database', 'Databases', -10, month=9)
    add('Zero only', amount=0)
    add('Credits only', amount=-100)
    stale = add('Existing report scope')
    stale.stale_since = datetime.now(timezone.utc)
    db.session.flush()
    response = client.get('/api/reports/whitespace', query_string={
        'buckets': ','.join([*DATABASE_WHITESPACE_ADJACENT_PILLARS, 'Databases']), 'show_all': '1',
    })
    assert response.status_code == 200
    expected = {
        row['id'] for row in response.json['customers'] if 'Databases' not in row['buckets']
    }
    result = get_database_whitespace_widget()
    assert {row['customer_id'] for row in result['rows']} == expected
    assert {row['customer_name'] for row in result['rows']} == {
        'AI prospect', 'Small footprint', 'Old database user',
        'Net zero database', 'Existing report scope', 'Analytics prospect', 'Rounded zero footprint',
    }
    assert result['count'] == len(expected)
    assert result['destination'] == '/reports/whitespace?view=database-whitespace'
    assert execute_tool('report_database_whitespace', {}) == result
    assert execute_tool('report_dashboard_signals', {
        'widget_key': 'database.whitespace',
    })['count'] == result['count']


def test_monthly_values_reuse_grid_rollup_window_and_rounding(database_rows):
    """Divide all product and pillar totals by three without early display rounding."""
    add, _ = database_rows
    prospect = add('Prospect', amount=100, month=8, product='AI first')
    add('Prospect', amount=200, month=8, product='AI second')
    add('Prospect', amount=900, month=9, product='AI first')
    add('Prospect', 'Analytics', 70.126)
    add('Prospect', 'Apps', 50)
    add('Prospect', 'Infra', 5000)
    add('Prospect', 'Developer', 6000)
    add('Prospect', 'Security', 7000)
    add('Prospect', 'Databases', 9999, month=6)
    result = get_database_whitespace_widget(date(2026, 10, 20))
    assert result['rows'][0]['monthly_spend'] == pytest.approx({
        'AI': 400, 'Analytics': 70.126, 'Apps': 50, 'Total': 520.126,
    })
    assert get_database_whitespace_widget(date(2040, 1, 1))['count'] == 0
    assert get_database_whitespace_widget(customer_ids=(prospect.id,))['count'] == 1
    assert get_database_whitespace_widget(customer_ids=())['count'] == 0
    assert get_database_whitespace_widget(customer_ids=(999999,))['count'] == 0
    assert get_whitespace_grid([]) == {'customers': [], 'buckets': []}


def test_ranking_ignores_unrelated_pillar_spend(database_rows):
    """Unrelated spend cannot qualify a customer or inflate the ranked adjacent total."""
    add, _ = database_rows
    add('Lower adjacent spend', 'AI', 10)
    add('Lower adjacent spend', 'Infra', 100000)
    add('Higher adjacent spend', 'Apps', 20)
    add('Unrelated only', 'Developer', 200000)
    result = get_database_whitespace_widget()
    assert [row['customer_name'] for row in result['rows']] == [
        'Higher adjacent spend', 'Lower adjacent spend',
    ]
    assert [row['monthly_spend']['Total'] for row in result['rows']] == [20, 10]


@pytest.mark.parametrize('grouped', [False, True])
def test_details_sort_grouping_escaping_and_summary(database_rows, grouped):
    """Keep all candidates and exact numeric sort keys in both seller presentations."""
    add, _ = database_rows
    first = add('<Customer>', amount=300)
    first.nickname = 'Nickname'
    first.seller = Seller(name='Alex')
    second = add('Second', 'Analytics', 100)
    second.seller = Seller(name='Alex')
    add('Unassigned', 'Apps', 50)
    db.session.flush()
    data = get_database_whitespace_widget()
    soup = BeautifulSoup(render_widget(
        'database_whitespace', data, group_by_seller=grouped,
    ), 'html.parser')
    assert not soup.select('customer')
    assert soup.select_one('td a').text == '<Customer> (Nickname)'
    assert soup.select_one('td a')['href'] == f'/customer/{first.id}/revenue'
    table = soup.select_one('table')
    assert [th.text for th in table.select('thead th')] == [
        'Customer', 'AI', 'Analytics', 'Apps', 'Total / mo',
    ]
    assert table['data-sb-sort-default-column'] == '4'
    assert table['data-sb-sort-default-direction'] == 'descending'
    assert soup.select_one('header a')['href'] == (
        '/reports/whitespace?view=database-whitespace'
    )
    assert len(table.select('tr[data-row-key]')) == 3
    assert len(table.select('tbody')) == (3 if grouped else 1)
    if grouped:
        assert len(soup.select('.badge.bg-primary .bi-person')) == 2
        assert all(th['colspan'] == '5' for th in table.select('tbody th'))
        assert 'No seller assigned' in table.text
    values = table.select_one('tr[data-row-key] td:last-child')
    assert float(values['data-sb-sort-value']) == 300
    summary = BeautifulSoup(render_widget('database_whitespace', data, 'metric'), 'html.parser')
    assert summary.select_one('dd').text == '3'
    assert not summary.select('table')
    duplicate = BeautifulSoup(render_widget(
        'database_whitespace', data, prefix='second-',
    ), 'html.parser')
    assert duplicate.select_one('table')['data-sb-sort-key'] != table['data-sb-sort-key']


def test_empty_and_missing_data_states(database_rows):
    """Do not conflate an empty qualifying queue with an import that has not run."""
    add, _ = database_rows
    empty = get_database_whitespace_widget()
    assert not empty['has_revenue_data']
    for variant in ('metric', 'detailed'):
        assert 'No saved pillar revenue' in render_widget('database_whitespace', empty, variant)
    add('Database user', 'Databases')
    empty = get_database_whitespace_widget()
    assert empty['has_revenue_data']
    assert empty['count'] == 0
    assert 'No customers match' in render_widget('database_whitespace', empty)
    assert BeautifulSoup(
        render_widget('database_whitespace', empty, 'metric'), 'html.parser',
    ).select_one('dd').text == '0'


@pytest.mark.parametrize(('role', 'specialty', 'allowed'), [
    ('se', 'data', True), ('se', 'infra', False), ('dss', 'infra', True),
])
def test_access_catalog_and_tool(client, database_rows, role, specialty, allowed):
    """Apply the same specialty scope as the Fabric acquisition widget."""
    add, pref = database_rows
    add('Prospect')
    pref.user_role, pref.se_specialty = role, specialty
    db.session.flush()
    assert get_database_whitespace_widget()['available'] is allowed
    catalog = client.get('/api/reports/dashboards').json['catalog']
    assert any(item['key'] == 'database.whitespace' for item in catalog) is allowed
    assert ('error' not in execute_tool('report_database_whitespace', {})) is allowed


def test_catalog_previews_saved_dashboard_and_live_scope(
    client, database_rows, dashboard_sources_ready,
):
    """Wire every size, the source dependency, saved placement and intersected owner filters."""
    add, _ = database_rows
    prospect = add('Prospect')
    seller = Seller(name='Owner')
    prospect.seller = seller
    other = add('Other', 'Analytics', 200)
    db.session.flush()
    assert WIDGET_SOURCES['database.whitespace'] == ('pillars',)
    catalog = client.get('/api/reports/dashboards').json['catalog']
    definition = next(item for item in catalog if item['key'] == 'database.whitespace')
    assert definition['supported_filters'] == ['customer_ids', 'seller_ids', 'group_by_seller']
    for variant in definition['variants']:
        for width in variant['widths']:
            response = client.get('/api/reports/dashboards/widget-preview', query_string={
                'key': 'database.whitespace', 'variant': variant['key'], 'width': width,
            })
            assert response.status_code == 200
            assert 'Database whitespace' in response.json['html']
    dashboard_id = client.post('/api/reports/dashboards', json={'name': 'Database'}).json['id']
    assert client.post(f'/api/reports/dashboards/{dashboard_id}/widgets', json={
        'key': 'database.whitespace',
    }).status_code == 200
    payload = draft_payload(client, dashboard_id)
    payload['filter_config'] = {'live_filters': ['group_by_seller', 'seller_ids', 'customer_ids']}
    assert client.post(f'/api/reports/dashboards/{dashboard_id}/save', json=payload).status_code == 200
    response = client.get(f'/reports/dashboards/{dashboard_id}', query_string={
        'group_by_seller': 'true', 'seller_ids': seller.id, 'customer_ids': prospect.id,
    })
    soup = BeautifulSoup(response.data, 'html.parser')
    assert len(soup.select('.db-database-whitespace-table tr[data-row-key]')) == 1
    assert soup.select_one('.db-report-seller-heading .bi-person')
    response = client.get(f'/reports/dashboards/{dashboard_id}', query_string={
        'seller_ids': seller.id, 'customer_ids': other.id,
    })
    assert 'No customers match' in response.text


def test_missing_pillar_source_uses_dashboard_sync(client, database_rows):
    """Use the existing account/revenue sync workflow instead of a permanent empty widget."""
    response = client.get('/api/reports/dashboards/widget-preview', query_string={
        'key': 'database.whitespace', 'variant': 'metric', 'width': 6,
    })
    assert response.status_code == 200
    assert 'data-widget-sync=' in response.json['html']
