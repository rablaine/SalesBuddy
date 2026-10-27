"""Streaming customers without RTI usage, including Fabric entry-point prospects."""
from datetime import date
from math import fsum

from app.models import Customer, ProductRevenueData, db
from app.services.dashboard_filters import group_rows_by_seller
from app.services.fabric_consumption import get_fabric_capacity_overview
from app.services.monthly_acr import ACR_WINDOW_MONTHS, complete_acr_months


PRODUCT_FAMILIES = (
    ('event_hubs', 'Event Hubs', ('Event Hubs',)),
    ('stream_analytics', 'Stream Analytics', ('Stream Analytics',)),
    ('iot', 'IoT', ('IoT', 'Device Update for IoT Hub')),
)
RTI_WORKLOAD = 'Real Time Intelligence'


def _product_family(product: str) -> str | None:
    """Match source product families without treating Event Grid as Event Hubs."""
    for key, _, prefixes in PRODUCT_FAMILIES:
        if any(product == prefix or product.startswith(prefix + ' ') for prefix in prefixes):
            return key
    return None


def get_rti_report(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Include current accounts without RTI usage and with a positive streaming family.

    ACR uses the last three complete calendar months and a fixed three-month divisor.
    Fabric usage retains its independent rolling 28-day window; missing RTI counts as zero.
    Capacity rows repeat customer-wide usage, so only the first row is used.
    """
    reference = reference or date.today()
    months = complete_acr_months(reference)
    end = reference.replace(day=1)
    customers = Customer.query.filter(Customer.stale_since.is_(None))
    if customer_ids is not None:
        customers = customers.filter(Customer.id.in_(customer_ids))
    customer_ids_in_scope = [customer.id for customer in customers.all()]
    records = db.session.query(
        ProductRevenueData.customer_id, ProductRevenueData.product,
        db.func.sum(ProductRevenueData.revenue).label('revenue'),
    ).filter(
        ProductRevenueData.customer_id.in_(customer_ids_in_scope),
        ProductRevenueData.month_date >= months[0], ProductRevenueData.month_date < end,
        db.or_(*(
            db.or_(
                ProductRevenueData.product == prefix,
                ProductRevenueData.product.startswith(prefix + ' ', autoescape=True),
            )
            for _, _, prefixes in PRODUCT_FAMILIES for prefix in prefixes
        )),
    ).group_by(ProductRevenueData.customer_id, ProductRevenueData.product).all()
    amounts: dict[int, dict[str, list[float]]] = {}
    for record in records:
        family = _product_family(record.product)
        if family:
            amounts.setdefault(
                record.customer_id, {key: [] for key, _, _ in PRODUCT_FAMILIES},
            )[family].append(record.revenue)
    overview = get_fabric_capacity_overview()
    has_revenue_data = ProductRevenueData.query.first() is not None
    rows = []
    seen = set()
    scope = set(customer_ids_in_scope)
    for customer in overview['rows']:
        customer_id = customer['id']
        if customer_id not in scope or customer_id in seen:
            continue
        seen.add(customer_id)
        spend = {
            key: fsum(amounts.get(customer_id, {}).get(key, []))
            for key, _, _ in PRODUCT_FAMILIES
        }
        rti_hours = customer['workload_cu_hours'].get(RTI_WORKLOAD) or 0
        has_rti = rti_hours > 0
        if has_rti or not any(amount > 0 for amount in spend.values()):
            continue
        rows.append({
            'id': customer_id, 'row_key': str(customer_id), 'name': customer['name'],
            'seller': customer['seller'], 'rti_cu_hours': rti_hours,
            'rti_share_pct': customer['workload_shares'].get(RTI_WORKLOAD) or 0,
            'has_rti': has_rti, 'spend': spend,
            'monthly_spend': {
                key: amount / ACR_WINDOW_MONTHS if has_revenue_data else None
                for key, amount in spend.items()
            },
            'monthly_acr': fsum(spend.values()) / ACR_WINDOW_MONTHS if has_revenue_data else None,
            'destination': f'/reports/fabric-consumption?customer_id={customer_id}',
            'revenue_destination': f'/customer/{customer_id}/revenue',
        })
    rows.sort(key=lambda row: (-(row['monthly_acr'] or 0), row['name'].casefold(), row['id']))
    return {
        'rows': rows, 'seller_groups': group_rows_by_seller(rows),
        'count': len(rows), 'rti_customer_count': sum(row['has_rti'] for row in rows),
        'monthly_acr': fsum(row['monthly_acr'] for row in rows) if has_revenue_data else None,
        'products': [{'key': key, 'label': label} for key, label, _ in PRODUCT_FAMILIES],
        'months': [month.isoformat() for month in months],
        'period_label': f'{months[0]:%b %Y} through {months[-1]:%b %Y}',
        'snapshot': overview['snapshot'],
        'has_revenue_data': has_revenue_data,
        'destination': '/reports/rti',
    }


def get_rti_widget(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Reuse report qualification, counts, scope and seller groups without another cutoff."""
    report = get_rti_report(reference, customer_ids=customer_ids)
    return {**report, 'details': {
        'rows': report['rows'], 'seller_groups': report['seller_groups'],
    }}
