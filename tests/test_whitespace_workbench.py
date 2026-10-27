"""Native whitespace surfaces and unchanged analytical query contracts."""

from datetime import date
from unittest.mock import patch

from bs4 import BeautifulSoup
import pytest

from app.models import (
    Customer, Seller, StrategicPillarRevenueData, RevenueImport, db,
)


@pytest.fixture
def whitespace_data(app, monkeypatch):
    """Seed distinct recent averages, historical coverage and zero-only customers."""
    with app.app_context():
        from app.services.monthly_acr import complete_acr_months

        monkeypatch.setattr('app.services.whitespace.complete_acr_months',
                            lambda reference=None: complete_acr_months(
                                reference or date(2026, 10, 20)))
        customers = [
            Customer(name="Partial Customer", nickname="Partial", tpid=10001),
            Customer(name="Covered Customer", tpid=10002),
            Customer(name="Zero Customer", tpid=10003),
        ]
        db.session.add_all(customers)
        imported = RevenueImport(filename="whitespace.csv", record_count=7)
        db.session.add(imported)
        db.session.flush()
        records = [
            (0, "Databases", date(2026, 1, 1), 5000),
            (0, "Databases", date(2026, 8, 1), 100.12),
            (0, "Databases", date(2026, 9, 1), 300.12),
            (0, "Analytics", date(2026, 1, 1), 50),
            (1, "Databases", date(2026, 9, 1), 1200.01),
            (1, "Analytics", date(2026, 9, 1), 400),
            (2, "Analytics", date(2026, 10, 1), 0),
        ]
        for index, bucket, month, revenue in records:
            customer = customers[index]
            db.session.add(StrategicPillarRevenueData(
                customer_name=customer.name, tpid=customer.tpid,
                customer_id=customer.id, pillar=bucket, product='', month_date=month,
                fiscal_month=month.strftime("%Y-%m"), revenue=revenue,
                last_import_id=imported.id,
            ))
        for index, product in [(0, "SQL"), (1, "SQL"), (1, "PostgreSQL")]:
            customer = customers[index]
            db.session.add(StrategicPillarRevenueData(
                customer_name=customer.name, customer_id=customer.id, pillar="Databases",
                tpid=customer.tpid,
                product=product, month_date=date(2026, 1, 1), fiscal_month="2026-01",
                revenue=100, last_import_id=imported.id,
            ))
        db.session.commit()
        return [customer.id for customer in customers]


@pytest.mark.parametrize("embedded", [False, True])
def test_native_structure(client, embedded):
    """Render the existing modes plus Data-specialty Fabric acquisition."""
    with patch("app.services.whitespace.has_pillar_data", return_value=True):
        response = client.get("/reports/whitespace" + ("?embed=1" if embedded else ""))
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    root = soup.select_one("[data-sb-whitespace]")
    assert root["data-embedded"] == str(embedded).lower()
    assert ("sb-directory-frame" in root["class"]) is not embedded
    assert ("sb-directory-frame-wide" in root["class"]) is not embedded
    assert len(root.select('[role="tab"]')) == 4
    assert len(root.select('[role="tabpanel"]')) == 4
    assert root.select_one("dialog.sb-dialog [data-bucket-frame]")
    assert root.select_one("#bucketCheckboxes")
    assert root.select_one('#gridGroupBySeller[type="checkbox"]')
    assert len(root.select('#gridGroupBySeller')) == 1
    assert root.select_one('label[for="whitespaceFocus"]')
    assert root.select_one('#whitespaceFocus option[value=""]').text == 'Any selected pillar'
    assert root.select_one('#acquisitionGroupBySeller[type="checkbox"]')
    assert not root.select(".sb-whitespace-tools button")
    assert root.select_one('[data-error="preferences"][hidden] [role="alert"]')
    assert root.select_one('[data-retry="preferences"]')
    assert root.select_one('#reverseCustomerSearch[role="combobox"]')
    assert not root.select(".card, .nav-tabs, .modal, [data-bs-toggle]")
    assert bool(soup.select_one("body.sb-page-frame")) is not embedded
    assert bool(soup.select_one("footer.sb-page-status")) is not embedded
    assert "gridTable[data-export-visible=true]" in response.text
    assert "ui-whitespace.js" in response.text


def test_empty_native_surface(client):
    """Keep import guidance usable when there is no revenue data."""
    with patch("app.services.whitespace.has_pillar_data", return_value=False):
        response = client.get("/reports/whitespace")
    soup = BeautifulSoup(response.data, "html.parser")
    assert response.status_code == 200
    root = soup.select_one("[data-sb-whitespace]")
    assert "No strategic pillar data yet" in root.get_text()
    assert root.select_one('a[href="/revenue/import"]')
    assert not root.select('[role="tab"], dialog')
    assert "ui-whitespace.js" not in response.text


def test_whitespace_help_explains_distinct_scope(client):
    """Document historical versus recent scope and grid-only exports."""
    source = client.get("/static/js/page-help.js").text
    start = source.index("title: 'Whitespace Analysis'")
    help_text = source[start:source.index("title: 'Revenue Reports'", start)]
    for phrase in ["three complete calendar months", "zero-only",
                   "does not change the saved Revenue filter",
                   "export only the loaded grid", "nickname or TPID",
                   "checkboxes automatically updates the grid",
                   "AI, Analytics or Apps", "Developer, Infra and Security alone do not qualify",
                   "saved on this device and restored on future visits"]:
        assert phrase in help_text


def test_recent_average_and_grid_scope(client, whitespace_data):
    """Divide complete-month totals by three, counting missing months as zero."""
    response = client.get("/api/reports/whitespace?buckets=Databases,Analytics&show_all=1")
    data = response.get_json()
    assert response.status_code == 200
    assert data["buckets"] == ["Databases", "Analytics"]
    rows = {row["id"]: row for row in data["customers"]}
    assert set(rows) == set(whitespace_data[:2])
    assert rows[whitespace_data[0]]["buckets"] == pytest.approx({"Databases": 400.24 / 3})
    assert rows[whitespace_data[0]]["nickname"] == "Partial"
    assert rows[whitespace_data[1]]["buckets"] == pytest.approx({
        "Databases": 1200.01 / 3, "Analytics": 400 / 3,
    })
    partial = client.get("/api/reports/whitespace?buckets=Databases,Analytics").get_json()
    assert [row["id"] for row in partial["customers"]] == [whitespace_data[0]]
    assert client.get("/api/reports/whitespace").get_json() == {
        "customers": [], "buckets": [],
    }


def test_grid_includes_current_seller_and_unassigned_customers(client, whitespace_data):
    """Grouping metadata follows local ownership without changing spend or membership."""
    seller = Seller(name='Whitespace owner')
    db.session.add(seller)
    customer = db.session.get(Customer, whitespace_data[0])
    customer.seller = seller
    db.session.commit()
    rows = client.get(
        '/api/reports/whitespace?buckets=Databases,Analytics&show_all=1',
    ).get_json()['customers']
    by_id = {row['id']: row for row in rows}
    assert set(by_id) == set(whitespace_data[:2])
    assert by_id[customer.id]['seller'] == {'id': seller.id, 'name': seller.name}
    assert by_id[whitespace_data[1]]['seller'] is None
    assert by_id[customer.id]['buckets'] == pytest.approx({'Databases': 400.24 / 3})


def test_reverse_keeps_historical_bucket_and_product_meaning(client, whitespace_data):
    """Old positive bucket/product spend counts even outside the grid window."""
    response = client.get(f"/api/reports/whitespace/reverse/{whitespace_data[0]}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["customer"] == {"id": whitespace_data[0], "name": "Partial Customer"}
    assert data["missing_buckets"] == ["AI", "Apps", "Developer", "Infra", "Security"]
    assert data["missing_products"] == {"Databases": ["PostgreSQL"]}
    assert client.get("/api/reports/whitespace/reverse/999999").status_code == 404


def test_penetration_includes_zero_only_denominator(client, whitespace_data):
    """Historical spend numerator differs intentionally from recent grid coverage."""
    response = client.get("/api/reports/whitespace/penetration")
    assert response.status_code == 200
    from app.services.whitespace import STRATEGIC_PILLARS
    assert response.get_json() == [
        {"bucket": bucket, "customers_with_spend": 2 if bucket in ("Databases", "Analytics") else 0,
         "total_customers": 3,
         "penetration_pct": 66.7 if bucket in ("Databases", "Analytics") else 0}
        for bucket in STRATEGIC_PILLARS
    ]
    details = client.get(
        "/api/reports/whitespace/penetration/customers?bucket=Analytics"
    ).get_json()
    assert {customer["id"] for customer in details["with_spend"]} == set(whitespace_data[:2])
    assert [customer["id"] for customer in details["without_spend"]] == [whitespace_data[2]]
