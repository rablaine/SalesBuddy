"""Same-period modernization revenue qualification, identity and source coverage."""
from datetime import date

import pytest

from app.models import Customer, ProductRevenueData, RevenueAnalysis, RevenueImport, Seller, db
from app.services.fabric_modernization import (
    _product_family,
    get_fabric_modernization_report, get_fabric_modernization_widget,
)
from app.services.salesiq_tools import execute_tool
from app.services.monthly_acr import monthly_product_breakdown


@pytest.fixture
def revenue_rows(app):
    """Write source rows only into the isolated test database."""
    with app.app_context():
        snapshot = RevenueImport(filename='test')
        db.session.add(snapshot)
        db.session.flush()

        def add(name, product, amount, month=9, customer=None, bucket='Analytics', year=2026):
            """Append a product source observation at its actual customer/bucket/month grain."""
            db.session.add(ProductRevenueData(
                customer_name=name, customer_id=customer.id if customer else None,
                product=product, revenue=amount, bucket=bucket,
                month_date=date(year, month, 1), fiscal_month=f'FY27-{month}',
                last_import_id=snapshot.id,
            ))
            db.session.flush()
        yield add


@pytest.mark.parametrize(('product', 'family'), [
    ('Azure Synapse Analytics Dedicated SQL Pool', 'synapse'),
    ('Azure Analysis Services Standard v2', 'aas'),
    ('Azure Data Explorer', 'adx'), ('Azure Databricks Regional', 'databricks'),
    ('Azure Data Factory', 'adf'), ('Azure Data Factory v2', 'adf'),
    ('Azure Data Factory v2 - Managed Airflow', 'adf'),
    ('Azure Data Factory v2 Data Flow - General Purpose', 'adf'),
    ('Azure Data Factory v2 Data Flow - Memory Optimized', 'adf'),
    ('Azure Data FactoryX', None), ('Data Factory', None),
    ('Fabric Capacity', 'fabric'), ('SQL Server Standard - Analysis Services', None),
    ('Azure Synapse Link for SQL', None), ('Azure DatabricksX', None),
])
def test_product_family_boundaries(product, family):
    """Match intended Azure product families, not unrelated SQL or near-matching names."""
    assert _product_family(product) == family


def test_three_complete_months_and_independent_product_spend(revenue_rows):
    """Exclude old, current and future spend; include credits without clipping them."""
    add = revenue_rows
    for month in (7, 8, 9):
        add('Prospect', 'Azure Synapse Analytics Pipelines', 10, month)
        add('Prospect', 'Azure Synapse Analytics Dedicated SQL Pool', 20, month)
        add('Prospect', 'Fabric Capacity', 5, month, bucket='Fabric')
    add('Prospect', 'Azure Analysis Services Basic', 11)
    add('Prospect', 'Azure Data Explorer', -2)
    add('Prospect', 'Azure Databricks Regional', 4)
    add('Prospect', 'Azure Synapse Analytics SQL Storage', 999, 6)
    add('Prospect', 'Fabric Capacity', 999, 10)
    add('Prospect', 'Fabric Capacity', 999, 11)
    add('Old only', 'Azure Data Explorer', 100, 6)
    add('Current only', 'Azure Data Explorer', 100, 10)
    add('Fabric only', 'Fabric Capacity', 100)
    add('Credit only', 'Azure Synapse Analytics', -100)
    add('Zero only', 'Azure Synapse Analytics', 0)
    result = get_fabric_modernization_report(date(2026, 10, 20))
    assert result['months'] == ['2026-07-01', '2026-08-01', '2026-09-01']
    assert [row['customer_name'] for row in result['rows']] == ['Prospect']
    row = result['rows'][0]
    assert row['spend'] == {
        'synapse': 90, 'aas': 11, 'adx': -2, 'databricks': 4, 'adf': 0, 'fabric': 15,
    }
    assert row['source_revenue'] == 103
    assert row['monthly_source_revenue'] == pytest.approx(103 / 3)
    assert row['monthly_spend'] == pytest.approx({
        'synapse': 30, 'aas': 11 / 3, 'adx': -2 / 3, 'databricks': 4 / 3, 'adf': 0, 'fabric': 5,
    })
    assert row['coverage_complete']
    assert row['months_observed'] == 3
    assert result['totals'] == row['spend']


def test_adf_and_other_analytics_stay_separate_with_combined_eligibility(revenue_rows):
    """Include pipeline-only and combined plays without double-counting Synapse pipelines."""
    for month in (7, 8, 9):
        revenue_rows('ADF only', 'Azure Data Factory v2', 100, month)
        revenue_rows('Both plays', 'Azure Data Factory v2 - Managed Airflow', 40, month)
        revenue_rows('Both plays', 'Azure Synapse Analytics Pipelines', 30, month)
        revenue_rows('Both plays', 'Fabric Capacity', 60, month)
        revenue_rows('Synapse credits', 'Azure Data Factory v2', 30, month)
        revenue_rows('Synapse credits', 'Azure Synapse Analytics', -1, month)
    for month in (6, 10, 11):
        revenue_rows('ADF only', 'Azure Data Factory v2', 90000, month)
    revenue_rows('Current ADF only', 'Azure Data Factory v2', 90000, 10)
    revenue_rows('Credit only', 'Azure Data Factory v2 Data Flow - General Purpose', -100)
    report = get_fabric_modernization_report(date(2026, 10, 20))
    rows = {row['customer_name']: row for row in report['rows']}
    assert set(rows) == {'ADF only', 'Both plays', 'Synapse credits'}
    assert rows['ADF only']['monthly_spend']['adf'] == 100
    assert rows['ADF only']['monthly_other_analytics_revenue'] == 0
    assert rows['ADF only']['has_synapse'] is False
    assert rows['Both plays']['monthly_spend']['adf'] == 40
    assert rows['Both plays']['monthly_other_analytics_revenue'] == 30
    assert rows['Both plays']['monthly_source_revenue'] == 70
    assert rows['Both plays']['has_synapse'] is True
    assert rows['Synapse credits']['has_synapse'] is False
    widget = get_fabric_modernization_widget(date(2026, 10, 20))
    assert {row['customer_name'] for row in widget['details']['rows']} == set(rows)
    assert widget['monthly_other_analytics_revenue'] == 29
    assert widget['monthly_adf_revenue'] == 170
    assert widget['monthly_source_revenue'] == 199
    assert widget['source_revenue'] == (
        widget['other_analytics_revenue'] + widget['adf_revenue']
    )


@pytest.mark.parametrize(('amount', 'fabric', 'included'), [
    (49.999, 0, False), (50, 0, True), (99.999, 1, False), (100, 1, True),
])
def test_adf_uses_existing_report_cutoffs(revenue_rows, amount, fabric, included):
    """ADF-only candidates obey the same unrounded source-volume thresholds."""
    revenue_rows('Pipeline candidate', 'Azure Data Factory v2', amount)
    revenue_rows('Pipeline candidate', 'Fabric Capacity', fabric)
    assert bool(get_fabric_modernization_report(date(2026, 10, 20))['rows']) is included
    assert bool(get_fabric_modernization_widget(date(2026, 10, 20))['count']) is included


def test_linked_customer_identity_across_names_buckets_and_same_name_sellers(revenue_rows):
    """Never join by display name or duplicate seller name; use current local ownership."""
    first_seller, second_seller = Seller(name='Alex'), Seller(name='Alex')
    first = Customer(name='Renamed customer', seller=first_seller, tpid=123)
    second = Customer(name='Second customer', seller=second_seller, tpid=456)
    db.session.add_all([first, second])
    db.session.flush()
    revenue_rows('Old name', 'Azure Analysis Services Standard', 100, customer=first)
    revenue_rows('New name', 'Azure Data Explorer', 200, customer=first, bucket='Other')
    revenue_rows('New name', 'Fabric Capacity', 8, customer=first, bucket='Fabric')
    revenue_rows('Second customer', 'Azure Databricks', 150, customer=second)
    revenue_rows('Unlinked', 'Azure Data Explorer', 50)
    result = get_fabric_modernization_report(date(2026, 10, 20))
    assert len(result['rows']) == 3
    row = result['rows'][0]
    assert row['customer_id'] == first.id
    assert row['customer_name'] == 'Renamed customer'
    assert row['spend']['aas'] == 100 and row['spend']['adx'] == 200
    assert row['spend']['fabric'] == 8
    assert len(result['seller_groups']) == 3
    assert [group['seller']['id'] for group in result['seller_groups'][:2]] == [
        first_seller.id, second_seller.id,
    ]
    assert result['seller_groups'][-1]['name'] is None
    assert result['partial_customer_count'] == 3


@pytest.mark.parametrize(('revenue', 'expected'), [
    (900, 300), (600, 200), (300, 100), (-99, -33), (0, 0),
])
def test_product_average_uses_fixed_divisor(revenue, expected):
    """Product evidence uses the same fixed divisor, including credits and empty totals."""
    products = monthly_product_breakdown({'Example': [revenue]})
    assert sum(product['monthly_revenue'] for product in products) == expected


def test_period_is_calendar_based_even_with_missing_months(revenue_rows):
    """Count available customer months without stretching the three-month lookback."""
    revenue_rows('Sparse', 'Azure Synapse Analytics', 100, 9)
    revenue_rows('Sparse', 'Other product', 10, 8)
    revenue_rows('Sparse', 'Fabric Capacity', 99, 5)
    result = get_fabric_modernization_report(date(2026, 10, 20))
    row = result['rows'][0]
    assert row['months_observed'] == 2
    assert not row['coverage_complete']
    assert row['spend']['fabric'] == 0
    assert row['monthly_source_revenue'] == pytest.approx(100 / 3)
    assert row['monthly_other_analytics_revenue'] == pytest.approx(100 / 3)
    assert row['monthly_spend'] == pytest.approx({
        'synapse': 100 / 3, 'aas': 0, 'adx': 0, 'databricks': 0, 'adf': 0, 'fabric': 0,
    })
    assert result['partial_customer_count'] == 1


def test_widget_sums_customer_averages_with_different_month_counts(revenue_rows):
    """Use the same fixed divisor for full, partial and single-month customer totals."""
    for month in (7, 8, 9):
        revenue_rows('Full', 'Azure Synapse Analytics', 90, month)
        revenue_rows('Full', 'Azure Data Factory v2', 30, month)
        revenue_rows('Full', 'Fabric Capacity', 10, month)
    for month in (8, 9):
        revenue_rows('Partial', 'Azure Data Factory v2', 500, month)
        revenue_rows('Partial', 'Azure Data Factory v2 Data Flow', 100, month, bucket='Other')
    revenue_rows('Single', 'Azure Synapse Analytics', 150)
    report = get_fabric_modernization_report(date(2026, 10, 20))
    rows = {row['customer_name']: row for row in report['rows']}
    assert rows['Full']['months_observed'] == 3
    assert rows['Partial']['months_observed'] == 2
    assert rows['Single']['months_observed'] == 1
    assert rows['Partial']['monthly_spend']['adf'] == 400
    assert rows['Single']['monthly_other_analytics_revenue'] == 50
    widget = get_fabric_modernization_widget(date(2026, 10, 20))
    assert widget['count'] == 3
    assert widget['partial_customer_count'] == 2
    assert widget['monthly_other_analytics_revenue'] == 140
    assert widget['monthly_adf_revenue'] == 430
    assert widget['monthly_source_revenue'] == 570


def test_distinct_source_identities_and_ambiguous_ownership(revenue_rows):
    """Same-name unlinked source data stays separate, with no arbitrary seller choice."""
    customer = Customer(name='Local customer', tpid=789, seller=Seller(name='Current seller'))
    db.session.add(customer)
    db.session.flush()
    revenue_rows('Shared source name', 'Azure Data Explorer', 100, customer=customer)
    revenue_rows('Shared source name', 'Azure Data Explorer', 120, bucket='Unlinked')
    for bucket, seller in (('First', 'Seller A'), ('Second', 'Seller B')):
        db.session.add(RevenueAnalysis(
            customer_name='Shared source name', seller_name=seller, bucket=bucket,
            months_analyzed=3, avg_revenue=0, latest_revenue=0, category='STABLE',
            recommended_action='MONITOR', confidence='HIGH', priority_score=0,
        ))
    db.session.flush()
    result = get_fabric_modernization_report(date(2026, 10, 20))
    assert len(result['rows']) == 2
    assert result['rows'][0]['customer_id'] is None
    assert result['rows'][0]['spend']['adx'] == 120
    assert result['rows'][0]['seller_name'] is None
    assert result['rows'][1]['customer_id'] == customer.id
    assert result['rows'][1]['seller_name'] == 'Current seller'
    assert result['seller_groups'][-1]['name'] is None


def test_year_boundary_and_no_data(revenue_rows):
    """Previous calendar year months work without a stale-data fallback."""
    empty = get_fabric_modernization_report(date(2027, 1, 1))
    assert empty['months'] == ['2026-10-01', '2026-11-01', '2026-12-01']
    assert empty['rows'] == []
    assert not empty['has_revenue_data']
    revenue_rows('Past', 'Azure Synapse Analytics', 10)
    result = get_fabric_modernization_report(date(2027, 1, 1))
    assert result['rows'] == []
    assert result['has_revenue_data']


@pytest.mark.parametrize(('source_total', 'fabric', 'included'), [
    (49.999, 0, False), (49.999, 1000, False),
    (50, 0, True), (50, -1, True), (50, 0.001, False),
    (99.999, 0, True), (99.999, 1, False),
    (100, 0, True), (100, 0.001, True), (100.001, 10, True),
    (-10, 0, False), (0, 1000, False),
])
def test_source_revenue_cutoffs(revenue_rows, source_total, fabric, included):
    """Apply strict cutoffs to unrounded Other Analytics totals, with positive Fabric spend."""
    revenue_rows('Threshold customer', 'Azure Synapse Analytics', source_total + 25, 7)
    revenue_rows('Threshold customer', 'Azure Data Explorer', -25, 8)
    revenue_rows('Threshold customer', 'Fabric Capacity', fabric, bucket='Fabric')
    result = get_fabric_modernization_report(date(2026, 10, 20))
    assert bool(result['rows']) is included
    if included:
        assert result['rows'][0]['source_revenue'] == pytest.approx(source_total)
        assert result['totals']['fabric'] == fabric
        assert sum(len(group['rows']) for group in result['seller_groups']) == 1
    else:
        assert result['seller_groups'] == []
        assert all(total == 0 for total in result['totals'].values())
        assert result['partial_customer_count'] == 0


def test_api_tool_and_report_share_provider(client, monkeypatch):
    """Expose a thin read tool and API, without creating a dashboard widget."""
    monkeypatch.setattr(
        'app.services.fabric_modernization.get_fabric_modernization_report',
        lambda: {'rows': [], 'months': ['2026-07-01'], 'url': '/reports/fabric-modernization'},
    )
    response = client.get('/api/reports/fabric-modernization')
    assert response.status_code == 200
    assert execute_tool('report_fabric_modernization', {}) == response.json


@pytest.mark.parametrize('url', [
    '/reports/synapse-customers?mode=new', '/reports/synapse-customers?mode=current',
    '/reports/new-synapse-users', '/revenue/reports/new-synapse-users',
])
def test_old_bookmarks_redirect_to_modernization(client, url):
    """Old report links stay useful without retaining the old report implementation."""
    response = client.get(url)
    assert response.status_code == 301
    assert response.headers['Location'].endswith('/reports/fabric-modernization')


def test_old_bookmark_preserves_embedded_context(client):
    """A bookmarked embedded report remains embedded after the redirect."""
    response = client.get('/reports/synapse-customers?mode=current&embed=1')
    assert response.status_code == 301
    assert response.headers['Location'].endswith('/reports/fabric-modernization?embed=1')


def test_widget_preserves_report_cutoffs_before_strict_spend_comparison(revenue_rows):
    """Do not reintroduce excluded small footprints or round equal-looking amounts."""
    for name, source, fabric in (
        ('Below fifty', 49.999, 0), ('Below hundred with Fabric', 99.999, 1),
        ('No Fabric', 50, 0), ('Equal', 100, 100),
        ('Just above', 100.0001, 100), ('Just below', 100, 100.0001),
    ):
        revenue_rows(name, 'Azure Data Explorer', source)
        revenue_rows(name, 'Fabric Capacity', fabric, bucket='Fabric')
    result = get_fabric_modernization_widget(date(2026, 10, 20))
    assert [row['customer_name'] for row in result['details']['rows']] == [
        'Just above', 'No Fabric',
    ]
    assert result['count'] == 2
    assert result['other_analytics_revenue'] == pytest.approx(150.0001)
    assert result['monthly_other_analytics_revenue'] == pytest.approx(150.0001 / 3)
    assert result['has_revenue_data']
    assert result['period_label'] == 'Jul 2026 through Sep 2026'
