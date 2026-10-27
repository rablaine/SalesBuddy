"""Fabric acquisition whitespace shared by the report, widgets and SalesIQ."""

from collections import defaultdict
from datetime import date
from math import fsum

from sqlalchemy.orm import joinedload

from app.models import Customer, StrategicPillarRevenueData, db
from app.services.se_specialty import has_specialty_access
from app.services.monthly_acr import complete_acr_months, monthly_product_breakdown
from app.services.whitespace import get_pillar_acr_records

DESTINATION = '/reports/whitespace?view=fabric-acquisition'
WIDGET_MIN_MONTHLY_ACR = 2000
COLUMNS = (
    ('AI', 'AI'),
    ('Databases', 'Databases'),
    ('Analytics', 'Other Analytics'),
)


def get_fabric_acquisition_report(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
    minimum_monthly_acr: float | None = None,
    include_no_other_spend: bool = False,
) -> dict:
    """Find customers with no positive monthly net Fabric ACR, treating missing data as zero.

    Use the last three complete calendar months, divided by three.
    Missing customer, product and month observations contribute zero.
    Fabric is split out of Analytics before any aggregation, never subtracted
    from rounded amounts or combined with the separate service-bucket feed.
    The optional footprint minimum is used only by the dashboard widget.
    Exclude customers flagged stale after dropping out of account sync.
    The report-only option includes all current customers regardless of other spend,
    including customers without any imported revenue observations.
    """
    months = complete_acr_months(reference)
    month_count = len(months)
    result = {
        'available': has_specialty_access('data'),
        'rows': [], 'seller_groups': [], 'count': 0,
        'columns': [{'key': key, 'label': label} for key, label in COLUMNS],
        'period_label': f'{months[0]:%b %Y} through {months[-1]:%b %Y}',
        'months': [month.isoformat() for month in months],
        'has_revenue_data': False, 'destination': DESTINATION,
        'include_no_other_spend': include_no_other_spend,
    }
    if not result['available']:
        return result
    result['has_revenue_data'] = (
        db.session.query(StrategicPillarRevenueData.id).first() is not None
    )
    records = get_pillar_acr_records(reference, customer_ids=customer_ids)
    spend = defaultdict(lambda: defaultdict(list))
    products = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    fabric_months = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record.pillar not in ('AI', 'Databases', 'Analytics'):
            continue
        fabric = record.pillar == 'Analytics' and (
            record.product == 'Fabric Capacity' or record.product.startswith('Fabric Capacity ')
        )
        family = 'Fabric' if fabric else record.pillar
        spend[record.customer_id][family].append(record.revenue)
        products[record.customer_id][family][record.product].append(record.revenue)
        if fabric:
            fabric_months[record.customer_id][record.month_date].append(record.revenue)
    customers = Customer.query.filter(Customer.stale_since.is_(None)).options(
        joinedload(Customer.seller),
    )
    if customer_ids is not None:
        customers = customers.filter(Customer.id.in_(customer_ids))
    for customer in customers.all():
        families = spend[customer.id]
        totals = {
            key: fsum(families[key]) for key in ('AI', 'Databases', 'Analytics', 'Fabric')
        }
        if not include_no_other_spend and not any(
            totals[key] > 0 for key in ('AI', 'Databases', 'Analytics')
        ):
            continue
        if any(fsum(values) > 0 for values in fabric_months[customer.id].values()):
            continue
        monthly_footprint = (
            fsum(totals[key] for key in ('AI', 'Databases', 'Analytics')) / month_count
        )
        if minimum_monthly_acr is not None and monthly_footprint < minimum_monthly_acr:
            continue
        result['rows'].append({
            'customer_id': customer.id, 'customer_name': customer.name,
            'destination': f'/customer/{customer.id}/revenue',
            'row_key': f'customer-{customer.id}',
            'seller': {'id': customer.seller.id, 'name': customer.seller.name}
            if customer.seller else None,
            'monthly_spend': {key: value / month_count for key, value in totals.items()},
            'product_breakdown': {
                key: monthly_product_breakdown(products[customer.id][key])
                for key in totals
            },
            'revenue': monthly_footprint,
        })
    result['rows'].sort(key=lambda row: (-row['revenue'], row['customer_name'].casefold()))
    groups = {}
    for row in result['rows']:
        seller = row['seller']
        key = seller['id'] if seller else None
        groups.setdefault(key, {'seller': seller, 'rows': []})['rows'].append(row)
    result['seller_groups'] = sorted(groups.values(), key=lambda group: (
        group['seller'] is None,
        group['seller']['name'].casefold() if group['seller'] else '',
        group['seller']['id'] if group['seller'] else 0,
    ))
    result['count'] = len(result['rows'])
    return result


def get_fabric_acquisition_widget(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Restrict widget counts and details to at least $2,000 monthly non-Fabric ACR."""
    return get_fabric_acquisition_report(
        reference, customer_ids=customer_ids, minimum_monthly_acr=WIDGET_MIN_MONTHLY_ACR,
    )
