"""Acquisition calculations, specialty access and shared report/widget surfaces."""

from datetime import date, datetime, timezone
from dataclasses import replace
import json
from unittest.mock import patch

from bs4 import BeautifulSoup
import pytest

from app.models import (
    Customer, RevenueImport, Seller, StrategicPillarRevenueData, UserPreference, db,
)
from app.services.fabric_acquisition import (
    get_fabric_acquisition_report, get_fabric_acquisition_widget,
)
from app.services.dashboards import WIDGET_DEFINITIONS
from app.services.salesiq_tools import execute_tool
from tests.test_fabric_renewal_widgets import render_widget

REFERENCE = date(2026, 10, 20)


@pytest.fixture
def acquisition_rows(app):
    """Seed only the isolated database and preserve the user's saved role/specialty."""
    with app.app_context():
        pref = UserPreference.query.first()
        original = pref.user_role, pref.se_specialty
        pref.user_role, pref.se_specialty = 'se', 'data'
        imported = RevenueImport(filename='acquisition-test')
        db.session.add(imported)
        db.session.flush()
        seller = Seller(name='Acquisition owner')
        db.session.add(seller)
        db.session.flush()

        def add(
            name, pillar='AI', product='Azure OpenAI', amount=30,
            months=(7, 8, 9, 10), year=2026,
        ):
            """Append source-defined product/month rows with current customer ownership."""
            customer = Customer.query.filter_by(name=name).first()
            if customer is None:
                customer = Customer(name=name, tpid=10000 + Customer.query.count(), seller=seller)
                db.session.add(customer)
                db.session.flush()
            for month in months:
                db.session.add(StrategicPillarRevenueData(
                    customer_id=customer.id, customer_name=name, tpid=customer.tpid,
                    pillar=pillar, product=product, month_date=date(year, month, 1),
                    fiscal_month=f'FY27-{month}', revenue=amount, last_import_id=imported.id,
                ))
            db.session.flush()
            return customer

        yield add, pref
        pref.user_role, pref.se_specialty = original
        db.session.commit()


def test_acquisition_splits_fabric_and_preserves_credits_and_period(acquisition_rows):
    """Use one source, a three-month divisor and no old/current/future contamination."""
    add, _ = acquisition_rows
    customer = add('Covered')
    add('Covered', 'Databases', 'Azure SQL Database', 60)
    add('Covered', 'Analytics', 'Azure Synapse Analytics', 90)
    add('Covered', 'Analytics', 'Fabric Capacity', 0)
    add('Covered', 'Analytics', 'Fabric Capacity Regional', -3)
    add('Covered', 'Analytics', 'Fabric CapacityX', 6)
    add('Covered', amount=9000, months=(6, 11))
    paid = add('Paid')
    add('Paid', 'Analytics', 'Fabric Capacity', 10)
    add('Fabric only', 'Analytics', 'Fabric Capacity', 50)
    data = get_fabric_acquisition_report(REFERENCE)
    assert data['months'] == ['2026-07-01', '2026-08-01', '2026-09-01']
    assert data['period_label'] == 'Jul 2026 through Sep 2026'
    assert data['count'] == 1
    assert data['rows'][0]['customer_id'] == customer.id
    assert data['rows'][0]['monthly_spend'] == {
        'AI': 30, 'Databases': 60, 'Analytics': 96, 'Fabric': -3,
    }
    assert get_fabric_acquisition_report(REFERENCE, customer_ids=())['count'] == 0
    assert get_fabric_acquisition_report(REFERENCE, customer_ids=(paid.id,))['count'] == 0
    assert get_fabric_acquisition_report(
        REFERENCE, customer_ids=(customer.id,),
    )['count'] == 1


def test_missing_months_are_zero_in_report_and_widget(acquisition_rows):
    """Missing complete months contribute zero; current partial months are excluded."""
    add, _ = acquisition_rows
    add('Missing middle month', amount=3000, months=(7, 9, 10))
    add('Missing current month', amount=3000, months=(7, 8, 9))
    add('Current month only', amount=3000, months=(10,))
    add('Covered', amount=3000)
    add('Infrastructure only', 'Infra', 'Virtual Machines', 100)
    data = get_fabric_acquisition_report(REFERENCE)
    assert data['count'] == 3
    assert {row['customer_name']: row['revenue'] for row in data['rows']} == {
        'Covered': 3000, 'Missing middle month': 2000,
        'Missing current month': 3000,
    }
    widget = get_fabric_acquisition_widget(REFERENCE)
    assert widget['count'] == 3
    assert {row['customer_name'] for row in widget['rows']} == {
        'Covered', 'Missing middle month', 'Missing current month',
    }


def test_report_only_option_includes_all_customers_regardless_of_other_spend(acquisition_rows):
    """Include customers without observations, retaining Fabric checks and widget minimum."""
    add, _ = acquisition_rows
    add('Positive prospect', amount=3000)
    add('Infrastructure only', 'Infra', 'Virtual Machines', 100)
    add('Zero spend', amount=0)
    add('Credits only', amount=-10)
    add('Fabric credits only', 'Analytics', 'Fabric Capacity', -10)
    add('Missing current', 'Infra', 'Virtual Machines', 100, months=(7, 8, 9))
    add('New Fabric', 'Infra', 'Virtual Machines', 100)
    add('New Fabric', 'Analytics', 'Fabric Capacity', 1, months=(10,))
    add('Offset Fabric', 'Analytics', 'Fabric Capacity', 20, months=(7,))
    add('Offset Fabric', 'Analytics', 'Fabric Capacity', -20, months=(8, 9, 10))
    add('Old Fabric only', 'Analytics', 'Fabric Capacity', 50, months=(6, 11))
    missing = Customer(name='No observations', tpid=999999)
    db.session.add(missing)
    db.session.flush()

    default = get_fabric_acquisition_report(REFERENCE)
    assert {row['customer_name'] for row in default['rows']} == {'Positive prospect'}
    assert default['include_no_other_spend'] is False
    expanded = get_fabric_acquisition_report(REFERENCE, include_no_other_spend=True)
    assert {row['customer_name'] for row in expanded['rows']} == {
        'Positive prospect', 'Infrastructure only', 'Zero spend', 'Credits only',
        'Fabric credits only', 'Missing current', 'No observations', 'Old Fabric only', 'New Fabric',
    }
    assert expanded['include_no_other_spend'] is True
    zero = next(row for row in expanded['rows'] if row['customer_name'] == 'Infrastructure only')
    assert zero['revenue'] == 0
    assert all(amount == 0 for amount in zero['monthly_spend'].values())
    scoped = get_fabric_acquisition_report(
        REFERENCE, include_no_other_spend=True, customer_ids=(missing.id,),
    )
    assert scoped['count'] == 1
    assert scoped['rows'][0]['customer_id'] == missing.id
    assert all(amount == 0 for amount in scoped['rows'][0]['monthly_spend'].values())
    assert {row['customer_name'] for row in get_fabric_acquisition_widget(REFERENCE)['rows']} == {
        'Positive prospect',
    }
    assert get_fabric_acquisition_report(
        REFERENCE, include_no_other_spend=True, customer_ids=(),
    )['count'] == 0


@pytest.mark.parametrize('include_no_other_spend', [False, True])
def test_stale_accounts_excluded_across_surfaces(
    client, acquisition_rows, include_no_other_spend,
):
    """Keep dropped accounts saved but out of every acquisition scope until they return."""
    add, _ = acquisition_rows
    current = add('Current account', amount=3000)
    stale = add('Dropped account', amount=3000)
    stale.stale_since = datetime.now(timezone.utc)
    missing = Customer(name='Current without revenue', tpid=999998)
    stale_missing = Customer(
        name='Dropped without revenue', tpid=999999, stale_since=datetime.now(timezone.utc),
    )
    db.session.add_all([missing, stale_missing])
    db.session.flush()
    expected = {current.id, missing.id} if include_no_other_spend else {current.id}
    result = get_fabric_acquisition_report(
        REFERENCE, include_no_other_spend=include_no_other_spend,
    )
    assert {row['customer_id'] for row in result['rows']} == expected
    assert sum(len(group['rows']) for group in result['seller_groups']) == len(expected)
    assert get_fabric_acquisition_report(
        REFERENCE, include_no_other_spend=include_no_other_spend, customer_ids=(stale.id,),
    )['count'] == 0
    assert get_fabric_acquisition_widget(REFERENCE, customer_ids=(stale.id,))['count'] == 0
    assert [row['customer_id'] for row in get_fabric_acquisition_widget(REFERENCE)['rows']] == [
        current.id,
    ]
    with patch(
        'app.services.fabric_acquisition.get_fabric_acquisition_report',
        side_effect=lambda reference=None, **kwargs: get_fabric_acquisition_report(
            REFERENCE, **kwargs,
        ),
    ):
        response = client.get('/api/reports/whitespace/fabric-acquisition', query_string={
            'include_no_other_spend': str(include_no_other_spend).lower(),
        })
        assert response.status_code == 200
        assert {row['customer_id'] for row in response.json['rows']} == expected
        tool = execute_tool('report_fabric_acquisition', {
            'include_no_other_spend': include_no_other_spend,
        })
        assert {row['customer_id'] for row in tool['rows']} == expected
    assert Customer.query.count() == 4
    assert stale.stale_since is not None
    assert stale_missing.stale_since is not None
    stale.stale_since = None
    db.session.flush()
    assert get_fabric_acquisition_widget(REFERENCE)['count'] == 2


@pytest.mark.parametrize(('query', 'expected'), [
    ('', False), ('?include_no_other_spend=false', False),
    ('?include_no_other_spend=true', True),
])
def test_report_expansion_api_and_tool(client, acquisition_rows, query, expected):
    """Pass the report option through the read API and SalesIQ without changing defaults."""
    add, _ = acquisition_rows
    add('<Infrastructure only>', 'Infra', 'Virtual Machines', 100)
    with patch(
        'app.services.fabric_acquisition.get_fabric_acquisition_report',
        side_effect=lambda **kwargs: get_fabric_acquisition_report(REFERENCE, **kwargs),
    ):
        response = client.get('/api/reports/whitespace/fabric-acquisition' + query)
        assert response.status_code == 200
        assert response.json['include_no_other_spend'] is expected
        assert response.json['count'] == int(expected)
        table = BeautifulSoup(response.json['table_html'], 'html.parser')
        assert bool(table.select('tr[data-row-key]')) is expected
        assert not table.select('infrastructure')
        params = {'include_no_other_spend': True} if expected else {}
        tool = execute_tool('report_fabric_acquisition', params)
        assert tool['count'] == int(expected)
        assert tool['include_no_other_spend'] is expected
        page = BeautifulSoup(client.get(
            '/reports/whitespace?view=fabric-acquisition',
        ).data, 'html.parser')
        toggle = page.select_one('#acquisitionIncludeNoOtherSpend')
        assert toggle is not None
        assert toggle.parent.get_text(strip=True) == (
            'All customers with no Fabric spend'
        )
        assert not toggle.has_attr('checked')
        assert page.select_one('[data-retry="acquisition"]')


@pytest.mark.parametrize('value', ['1', '0', 'yes', 'no', '', 'invalid'])
def test_report_expansion_rejects_invalid_boolean(client, acquisition_rows, value):
    """Reject ambiguous query values instead of silently expanding the queue."""
    response = client.get('/api/reports/whitespace/fabric-acquisition', query_string={
        'include_no_other_spend': value,
    })
    assert response.status_code == 400
    assert response.json['error'] == 'include_no_other_spend must be true or false.'


def test_footprint_uses_unrounded_monthly_values(acquisition_rows):
    """Include Analytics using exact values without subtracting Fabric twice."""
    add, _ = acquisition_rows
    add('Fractional', amount=0.49)
    add('Fractional', 'Databases', 'Azure SQL Database', 0.49)
    add('Fractional', 'Analytics', 'Azure Synapse Analytics', 100)
    data = get_fabric_acquisition_report(REFERENCE)
    assert [column['key'] for column in data['columns']] == [
        'AI', 'Databases', 'Analytics',
    ]
    assert data['rows'][0]['revenue'] == pytest.approx(100.98)


@pytest.mark.parametrize(('ai', 'databases', 'analytics', 'fabric', 'included'), [
    (1999.9999, 0, 0, 0, False),
    (2000, 0, 0, 0, True),
    (2000.0001, 0, 0, 0, True),
    (1000, 600, 400, 0, True),
    (1000, 600, 399.9999, 0, False),
    (1999.99, 0, 0, -100, False),
    (2500, 0, -600, 0, False),
])
def test_widget_only_monthly_minimum(
    acquisition_rows, ai, databases, analytics, fabric, included,
):
    """Apply the unrounded monthly threshold only to widget counts, rows and groups."""
    add, _ = acquisition_rows
    customer = add('Threshold prospect', amount=ai)
    add(customer.name, 'Databases', 'Azure SQL Database', databases)
    add(customer.name, 'Analytics', 'Azure Synapse Analytics', analytics)
    add(customer.name, 'Analytics', 'Fabric Capacity', fabric)
    assert get_fabric_acquisition_report(REFERENCE)['count'] == 1
    widget = get_fabric_acquisition_widget(REFERENCE)
    assert widget['count'] == int(included)
    assert len(widget['rows']) == int(included)
    assert sum(len(group['rows']) for group in widget['seller_groups']) == int(included)
    assert get_fabric_acquisition_widget(REFERENCE, customer_ids=())['count'] == 0
    assert get_fabric_acquisition_widget(
        REFERENCE, customer_ids=(customer.id,),
    )['count'] == int(included)


def test_positive_fabric_month_cannot_be_hidden_by_later_credit(acquisition_rows):
    """A net-zero reporting period still has paid Fabric when one month is positive."""
    add, _ = acquisition_rows
    add('Credited Fabric')
    add('Credited Fabric', 'Analytics', 'Fabric Capacity', 30, months=(7,))
    add('Credited Fabric', 'Analytics', 'Fabric Capacity', -30, months=(8,))
    assert get_fabric_acquisition_report(REFERENCE)['count'] == 0


@pytest.mark.parametrize('current_amount', [0, 500, 900000])
def test_partial_current_month_does_not_change_widget_average(acquisition_rows, current_amount):
    """Partial current-month amounts cannot change the complete-month average or threshold."""
    add, _ = acquisition_rows
    add('Partial month threshold', amount=2500, months=(7, 8, 9))
    add('Partial month threshold', amount=current_amount, months=(10,))
    report = get_fabric_acquisition_report(REFERENCE)
    assert report['count'] == 1
    assert report['rows'][0]['revenue'] == 2500
    assert get_fabric_acquisition_widget(REFERENCE)['count'] == 1


@pytest.mark.parametrize(('current_fabric', 'credit', 'included'), [
    (1, 0, True),
    (30, -30, True),
    (30, -31, True),
])
def test_current_month_fabric_does_not_change_complete_month_cohort(
    acquisition_rows, current_fabric, credit, included,
):
    """Use the same complete-month window for Fabric and non-Fabric eligibility."""
    add, _ = acquisition_rows
    add('New Fabric customer', amount=3000)
    add('New Fabric customer', 'Analytics', 'Fabric Capacity', -100, months=(9,))
    add('New Fabric customer', 'Analytics', 'Fabric Capacity', current_fabric, months=(10,))
    add('New Fabric customer', 'Analytics', 'Fabric Capacity Regional', credit, months=(10,))
    for provider in (get_fabric_acquisition_report, get_fabric_acquisition_widget):
        data = provider(REFERENCE)
        assert data['count'] == int(included)


def test_covered_zero_months_use_fixed_divisor(acquisition_rows):
    """Other pillar observations cover months with no positive target-product rows."""
    add, _ = acquisition_rows
    add('Sparse AI', 'Infra', 'Virtual Machines', 100, months=(7, 8, 9))
    add('Sparse AI', amount=30, months=(9,))
    data = get_fabric_acquisition_report(REFERENCE)
    assert data['count'] == 1
    assert data['rows'][0]['monthly_spend']['AI'] == 10
    assert data['rows'][0]['revenue'] == 10


@pytest.mark.parametrize('day', [1, 15, 31])
def test_three_complete_months_cross_calendar_year(acquisition_rows, day):
    """January consistently uses October through December, regardless of current day."""
    add, _ = acquisition_rows
    add('New year prospect', months=(10, 11, 12))
    add('New year prospect', months=(1,), year=2027)
    add('New year prospect', amount=9000, months=(9,))
    add('New year prospect', amount=9000, months=(2,), year=2027)
    data = get_fabric_acquisition_report(date(2027, 1, day))
    assert data['months'] == ['2026-10-01', '2026-11-01', '2026-12-01']
    assert data['count'] == 1
    assert data['rows'][0]['monthly_spend']['AI'] == 30


@pytest.mark.parametrize(('role', 'specialty', 'allowed'), [
    ('se', 'data', True), ('se', 'infra', False), ('se', 'apps_ai', False),
    ('dss', 'infra', True), ('dss', 'other', True),
])
def test_role_gates_tab_api_catalog_and_tool(client, acquisition_rows, role, specialty, allowed):
    """Every surface includes sellers independently of their stored SE specialty."""
    add, pref = acquisition_rows
    add('Prospect')
    pref.user_role, pref.se_specialty = role, specialty
    db.session.commit()
    result = get_fabric_acquisition_report(REFERENCE)
    with patch('app.services.fabric_acquisition.get_fabric_acquisition_report', return_value=result):
        page = client.get('/reports/whitespace')
        assert page.status_code == 200
        soup = BeautifulSoup(page.data, 'html.parser')
        assert bool(soup.select_one('#acquisition-tab')) is allowed
        deep = client.get('/reports/whitespace?view=fabric-acquisition')
        assert deep.status_code == (200 if allowed else 403)
        if allowed:
            assert BeautifulSoup(deep.data, 'html.parser').select_one(
                '[data-initial-mode="acquisition"]',
            )
        api = client.get('/api/reports/whitespace/fabric-acquisition')
        assert api.status_code == (200 if allowed else 403)
        tool = execute_tool('report_fabric_acquisition', {})
        assert ('error' not in tool) is allowed
    catalog = client.get('/api/reports/dashboards').json['catalog']
    assert any(item['key'] == 'fabric.acquisition' for item in catalog) is allowed
    assert pref.se_specialty == specialty


def test_report_widget_and_tool_share_source_and_grouping(client, acquisition_rows, monkeypatch):
    """Keep the report unfiltered while applying the widget-only minimum at every width."""
    add, _ = acquisition_rows
    add('<Prospect>', amount=2000)
    add('Below widget minimum', amount=1999.9999)
    add('Incomplete prospect', months=(7, 9))
    data = get_fabric_acquisition_report(REFERENCE)
    assert data['count'] == 3
    monkeypatch.setitem(WIDGET_DEFINITIONS, 'fabric.acquisition', replace(
        WIDGET_DEFINITIONS['fabric.acquisition'],
        provider=lambda reference=None, customer_ids=None: get_fabric_acquisition_widget(
            REFERENCE, customer_ids=customer_ids,
        ),
    ))
    with patch('app.services.fabric_acquisition.get_fabric_acquisition_report', return_value=data):
        page = BeautifulSoup(client.get(
            '/reports/whitespace?view=fabric-acquisition',
        ).data, 'html.parser')
        assert page.select_one('#fabricAcquisitionTable')
        assert 'Jul 2026 through Sep 2026' in page.text
        assert page.select_one('#acquisitionPanel > p.sb-whitespace-notice') is None
        assert len(page.select('#fabricAcquisitionTable thead th')) == 4
        assert 'excluded because monthly coverage' not in page.text
        assert len(page.select('#fabricAcquisitionTable tr[data-row-key]')) == data['count']
        assert page.select_one('#fabricAcquisitionTable th:nth-child(4)').text == (
            'Other Analytics'
        )
        assert execute_tool('report_fabric_acquisition', {})['rows'] == data['rows']
    context = {
        'dashboard': {'live_filters': ['group_by_seller']},
        'live': {'group_by_seller': True},
    }
    for variant, widths in [('metric', (3, 4, 6, 12)), ('detailed', (6, 12))]:
        for width in widths:
            response = client.get('/api/reports/dashboards/widget-preview', query_string={
                'key': 'fabric.acquisition', 'variant': variant, 'width': width,
                'filter_context': json.dumps(context),
            })
            assert response.status_code == 200
            widget = BeautifulSoup(response.json['html'], 'html.parser')
            assert 'Jul 2026 through Sep 2026' in widget.text
            assert 'Three-month total divided by 3' in widget.text
            assert 'excluded because monthly coverage' not in widget.text
            assert widget.select_one('a[href="/reports/whitespace?view=fabric-acquisition"]')
            if variant == 'detailed':
                assert len(widget.select('thead th')) == 4
                assert widget.select_one('thead th:last-child').text == 'Other Analytics'
                assert widget.select_one('.db-report-seller-heading th')['colspan'] == '4'
                assert widget.select_one('.db-report-seller')
                assert len(widget.select('tr[data-row-key]')) == 1
                assert '<Prospect>' in widget.text
                assert not widget.select('prospect')
                assert 'Below widget minimum' not in widget.text
            else:
                assert widget.select_one('.db-acquisition-count').text == '1'


def test_empty_import_and_disabled_widget_are_distinct(client, acquisition_rows):
    """Missing observations become zero without bypassing specialty access."""
    _, pref = acquisition_rows
    db.session.add(Customer(name='No imported revenue', tpid=999999))
    db.session.flush()
    data = get_fabric_acquisition_report(REFERENCE)
    assert not data['has_revenue_data']
    assert data['count'] == 0
    expanded = get_fabric_acquisition_report(REFERENCE, include_no_other_spend=True)
    assert expanded['count'] == 1
    assert expanded['rows'][0]['revenue'] == 0
    assert all(amount == 0 for amount in expanded['rows'][0]['monthly_spend'].values())
    widget = get_fabric_acquisition_widget(REFERENCE)
    assert widget['count'] == 0
    html = render_widget('fabric_acquisition', widget, 'metric')
    assert BeautifulSoup(html, 'html.parser').select_one('.db-acquisition-count').text == '0'
    pref.user_role, pref.se_specialty = 'se', 'infra'
    db.session.commit()
    response = client.get('/api/reports/dashboards/widget-preview', query_string={
        'key': 'fabric.acquisition', 'variant': 'metric', 'width': 3,
    })
    assert 'Available for Data SEs and sellers' in response.json['html']
