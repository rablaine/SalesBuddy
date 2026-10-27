"""Native modernization report grouping, revenue, links and export contracts."""
from copy import deepcopy

from bs4 import BeautifulSoup
import pytest


@pytest.fixture
def modernization_report(monkeypatch):
    """Supply exact report values while exercising real route and template rendering."""
    report = {
        'rows': [], 'seller_groups': [], 'period_label': 'Jul 2026 through Sep 2026',
        'products': [{'key': key, 'label': label, 'source': label} for key, label in (
            ('synapse', 'Synapse'), ('aas', 'AAS'), ('adx', 'ADX'),
            ('databricks', 'Databricks'), ('adf', 'ADF'), ('fabric', 'Fabric'),
        )],
        'has_revenue_data': True, 'partial_customer_count': 1,
    }
    for index, complete in enumerate((True, False)):
        row = {
            'customer_id': 17 if complete else None,
            'customer_name': 'Literal <customer & team>' if complete else 'Unmatched',
            'tpid_url': 'https://example.test/account?TPID=123' if complete else None,
            'spend': {'synapse': 100.49, 'aas': 0, 'adx': -0.25, 'databricks': 3.75,
                      'adf': 12.34, 'fabric': 0 if complete else 1.49},
            'source_revenue': 116.33,
            'other_analytics_revenue': 103.99,
            'coverage_complete': complete, 'row_key': str(index),
            'months_observed': 3 if complete else 2,
        }
        row['monthly_spend'] = {
            family: amount / 3 for family, amount in row['spend'].items()
        }
        row['monthly_source_revenue'] = row['source_revenue'] / 3
        row['monthly_other_analytics_revenue'] = 103.99 / 3
        report['rows'].append(row)
        report['seller_groups'].append({
            'key': str(index), 'name': 'R&D <seller>' if complete else None,
            'seller': {'id': 2, 'name': 'R&D <seller>'} if complete else None, 'rows': [row],
        })
    monkeypatch.setattr(
        'app.services.fabric_modernization.get_fabric_modernization_report',
        lambda: deepcopy(report),
    )
    return report


@pytest.mark.parametrize('embedded', [False, True])
def test_modernization_native_contract_and_exact_values(client, modernization_report, embedded):
    """All columns sort on raw values and export across seller tables without HTML injection."""
    response = client.get('/reports/fabric-modernization', query_string={
        **({'embed': '1'} if embedded else {}),
    })
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    root = soup.select_one('.sb-modernization-report')
    assert ('sb-directory-frame-wide' in root['class']) is not embedded
    assert root['data-sb-report-key'] == 'fabric-modernization-v2'
    assert root['data-sb-report-embedded'] == str(embedded).lower()
    assert bool(soup.select('footer.sb-page-status')) is not embedded
    assert soup.select_one('script[src*="/js/ui-revenue-reports.js"]')
    assert root.select_one('h1').text == 'Fabric Modernization'
    assert len(root.select('.sb-modernization-table')) == 2
    assert len(root.select('tbody tr')) == 2
    assert not root.select('img, tbody script, .nav-tabs')
    assert root.select_one('a[href="/customer/17/revenue"]').text == 'Literal <customer & team>'
    assert root.select_one('.badge.bg-primary')['href'] == '/seller/2'
    assert root.select_one('[data-export-url]')['data-export-url'].endswith('TPID=123')
    assert not root.select_one('.sb-revenue-report-notice:not([hidden])')
    for table in root.select('table'):
        assert table.has_attr('data-sb-sortable')
        assert len(table.select('thead [data-sb-sort-type]')) == 8
        assert len(table.select('tbody tr:first-child td')) == 8
        assert table['data-sb-sort-key'].startswith('modernization-v3-')
        assert 'Months observed' not in table.get_text()
        assert all(th.has_attr('data-export-label') for th in table.select('thead th'))
        assert table['data-sb-sort-default-column'] == '7'
        assert table.select('thead th')[7]['data-export-label'] == 'Other Analytics avg monthly ACR'
        assert table.select('thead th')[5]['data-export-label'] == 'ADF avg monthly ACR'
    cells = root.select('tbody')[0].select('td')
    assert [float(cell['data-sb-sort-value']) for cell in cells[1:]] == pytest.approx([
        100.49 / 3, 0, -0.25 / 3, 3.75 / 3, 12.34 / 3, 0, 103.99 / 3,
    ])
    assert 'Average monthly ACR across Jul 2026 through Sep 2026' in root.text
    assert b'Fabric Modernization - Avg monthly ACR' in response.data
    assert cells[6].get_text(strip=True) == '$0'
    partial = root.select('tbody')[1].select('td')
    assert [float(cell['data-sb-sort-value']) for cell in partial[1:]] == pytest.approx([
        100.49 / 3, 0, -0.25 / 3, 3.75 / 3, 12.34 / 3, 1.49 / 3, 103.99 / 3,
    ])
    assert partial[2].get_text(strip=True) == '$0'
    assert 'Unavailable' not in root.text
    assert 'available months' not in str(soup)
    assert b'const TABLE_SELECTOR = ".sb-modernization-table"' in response.data


def test_modernization_no_data_import_recovery(client, modernization_report):
    """No imported product revenue offers the real import destination."""
    modernization_report.update(has_revenue_data=False, rows=[], seller_groups=[])
    page = BeautifulSoup(client.get('/reports/fabric-modernization').data, 'html.parser')
    assert page.select_one('.sb-revenue-empty a')['href'] == '/revenue/import'
    assert not page.select('.sb-modernization-table')


def test_modernization_empty_period_explanation(client, modernization_report):
    """A saved dataset with no recent prospects is distinct from no import."""
    modernization_report.update(rows=[], seller_groups=[])
    page = BeautifulSoup(client.get('/reports/fabric-modernization').data, 'html.parser')
    assert page.select_one('.sb-revenue-empty h2').text == 'No recent modernization prospects'
    assert 'Jul 2026 through Sep 2026' in page.select_one('.sb-revenue-empty').text
    assert not page.select('.sb-revenue-empty a')


def test_revenue_reports_hub_retains_real_destinations(client):
    """The revenue report directory links to the replacement report."""
    page = BeautifulSoup(client.get('/reports/revenue-reports').data, 'html.parser')
    assert len(page.select('.sb-revenue-report-entry')) == 1
    assert page.select_one('.sb-revenue-report-entry')['href'] == '/reports/fabric-modernization'
    assert page.select_one('.sb-revenue-command')['href'] == '/reports/revenue'


def test_report_contextual_help_describes_actual_scope(client):
    """F1 help covers the shared three-month period rather than obsolete adoption modes."""
    response = client.get('/static/js/page-help.js')
    assert b"title: 'Fabric Modernization'" in response.data
    assert b'all seller tables' in response.data
