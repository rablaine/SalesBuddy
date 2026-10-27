"""Cross-surface monthly ACR parity and safe product evidence."""

import json
from datetime import date
from math import fsum
from unittest.mock import patch

from bs4 import BeautifulSoup
import pytest
from sqlalchemy import event

from app.models import Customer, ProductRevenueData, RevenueImport, StrategicPillarRevenueData, db
from app.services.fabric_acquisition import (
    get_fabric_acquisition_report, get_fabric_acquisition_widget,
)
from app.services.fabric_modernization import get_fabric_modernization_report
from app.services.monthly_acr import complete_acr_months
from app.services.request_cache import shared_read_scope
from app.services.whitespace import get_database_whitespace_widget, get_whitespace_grid
from tests.test_fabric_renewal_widgets import render_widget

REFERENCE = date(2026, 10, 27)


@pytest.mark.parametrize(('reference', 'expected'), [
    (date(2026, 10, 1), ('2026-07-01', '2026-08-01', '2026-09-01')),
    (date(2026, 10, 31), ('2026-07-01', '2026-08-01', '2026-09-01')),
    (date(2027, 1, 1), ('2026-10-01', '2026-11-01', '2026-12-01')),
    (date(2028, 3, 1), ('2027-12-01', '2028-01-01', '2028-02-01')),
])
def test_complete_month_boundaries(reference, expected):
    """Keep the same calendar definition across day, year and leap-year boundaries."""
    assert tuple(month.isoformat() for month in complete_acr_months(reference)) == expected


def test_product_evidence_matches_category_and_shared_widget_values(app, client):
    """Credits, missing months and escaped product names reconcile across surfaces."""
    with app.app_context():
        imported = RevenueImport(filename='breakdown-test')
        customer = Customer(name='Breakdown customer', tpid=987654)
        db.session.add_all([imported, customer])
        db.session.flush()
        for pillar, product, amount, month in (
            ('AI', 'Azure OpenAI', 9000, 7),
            ('Databases', '<SQL & database>', 300, 8),
            ('Databases', 'Database credit', -30, 9),
            ('Analytics', 'Azure Synapse Analytics', 900, 7),
            ('Analytics', 'Azure Data Explorer', -60, 9),
            ('Analytics', 'Azure Data Factory v2', 150, 8),
            ('Analytics', 'Fabric Capacity', -9, 9),
            ('Analytics', 'Azure Synapse Analytics', 999999, 10),
            ('Databases', 'Old SQL', 999999, 6),
        ):
            db.session.add(StrategicPillarRevenueData(
                customer_id=customer.id, customer_name=customer.name, tpid=customer.tpid,
                pillar=pillar, product=product, revenue=amount, month_date=date(2026, month, 1),
                fiscal_month=f'FY27-{month}', last_import_id=imported.id,
            ))
            db.session.add(ProductRevenueData(
                customer_id=customer.id, customer_name=customer.name, bucket=pillar,
                product=product, revenue=amount, month_date=date(2026, month, 1),
                fiscal_month=f'FY27-{month}', last_import_id=imported.id,
            ))
        db.session.flush()
        report = get_fabric_acquisition_report(REFERENCE)
        row = report['rows'][0]
        assert row['monthly_spend'] == {'AI': 3000, 'Databases': 90, 'Analytics': 330, 'Fabric': -3}
        for category, products in row['product_breakdown'].items():
            assert fsum(product['monthly_revenue'] for product in products) == pytest.approx(
                row['monthly_spend'][category],
            )
        assert row['product_breakdown']['Databases'] == [
            {'product': '<SQL & database>', 'monthly_revenue': 100},
            {'product': 'Database credit', 'monthly_revenue': -10},
        ]
        grid = get_whitespace_grid(
            ['AI', 'Databases', 'Analytics'], show_all=True, reference=REFERENCE,
        )
        grid_row = grid['customers'][0]
        assert grid_row['buckets']['Databases'] == row['monthly_spend']['Databases']
        assert grid_row['buckets']['AI'] == row['monthly_spend']['AI']
        # The grid includes Fabric while acquisition excludes it from Analytics.
        assert grid_row['buckets']['Analytics'] == 327
        modernization_report = get_fabric_modernization_report(REFERENCE)
        modernization = modernization_report['rows'][0]
        assert modernization['monthly_other_analytics_revenue'] == 280
        assert fsum(product['monthly_revenue'] for product in
                    modernization['product_breakdown']['other_analytics']) == 280
        assert modernization['monthly_spend']['adf'] == 50
        assert modernization['monthly_spend']['fabric'] == -3
        for grouped in (False, True):
            widget = get_fabric_acquisition_widget(REFERENCE, customer_ids=(customer.id,))
            soup = BeautifulSoup(render_widget(
                'fabric_acquisition', widget, group_by_seller=grouped,
            ), 'html.parser')
            assert not soup.select('sql, script')
            button = soup.select_one('[data-revenue-label="Databases"]')
            assert json.loads(button['data-sb-revenue-breakdown']) == (
                row['product_breakdown']['Databases']
            )
            assert button.text == '$90'
            assert button.parent['data-export-label'] == '$90'
            assert 'Jul 2026 through Sep 2026' == button['data-revenue-period']
            assert button['type'] == 'button'
            assert 'Product breakdown' in button['aria-label']
            assert button['aria-label'].startswith('Databases: $90 average monthly ACR.')
            assert len(soup.select('td')) == 4
        with patch('app.services.fabric_modernization.get_fabric_modernization_report',
                   return_value=modernization_report):
            page = BeautifulSoup(client.get('/reports/fabric-modernization').data, 'html.parser')
        cell = page.select_one('.sb-modernization-table tbody td:last-child')
        assert cell.select_one('[data-sb-revenue-breakdown]')
        assert cell['data-export-label'] == cell.get_text(strip=True) == '$280'
        assert get_fabric_acquisition_report(REFERENCE, customer_ids=())['rows'] == []


def test_pillar_widgets_share_one_scoped_product_read(app):
    """Read the same saved pillar source only once per dashboard scope and render."""
    with app.test_request_context('/'):
        queries = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            """Record only the shared product aggregation, not existence queries."""
            if 'sum(strategic_pillar_revenue_data.revenue)' in statement.lower():
                queries.append(statement)

        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            with shared_read_scope():
                get_fabric_acquisition_widget(REFERENCE, customer_ids=())
                get_database_whitespace_widget(REFERENCE, customer_ids=())
            assert len(queries) == 1
            with shared_read_scope():
                get_database_whitespace_widget(REFERENCE, customer_ids=())
            assert len(queries) == 2
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
