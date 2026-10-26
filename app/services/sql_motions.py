"""Shared saved-data queues for SQL renewals and version-specific EOS discovery."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from math import fsum

from sqlalchemy.orm import joinedload

from app.models import Customer, SqlArcVersion, SqlMotionImport, SqlRenewal
from app.services.dashboard_filters import group_rows_by_seller
from app.services.se_specialty import has_specialty_access
from app.services.seller_mode import get_seller_mode_seller_id

SQL_2016_VERSION = 'sql server 2016'
SQL_2016_DATA_WARNING = (
    'SQL Server 2016 data is known to be incorrect. '
    'Do not rely on these counts or customer lists.'
)
ALL_DISCOVERED = 'all discovered'
MOTIONS = {
    'renewals': ('SQL Renewals', '/reports/sql-renewals'),
    'eos': ('SQL 2016 EOS / ESU', '/reports/sql-eos'),
}


def _utc(value: datetime | None) -> str | None:
    """Serialize saved UTC timestamps without local-time reinterpretation."""
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(
        timezone.utc,
    ).isoformat() if value else None


def _customers(customer_ids: tuple[int, ...] | None) -> dict[int, Customer]:
    """Intersect current account, explicit dashboard and active seller scope."""
    query = Customer.query.options(joinedload(Customer.seller)).filter(
        Customer.stale_since.is_(None),
    )
    if customer_ids is not None:
        query = query.filter(Customer.id.in_(customer_ids))
    seller_id = get_seller_mode_seller_id()
    if seller_id is not None:
        query = query.filter(Customer.seller_id == seller_id)
    return {customer.id: customer for customer in query.all()}


def _identity(customer: Customer) -> dict:
    """Use current customer ownership rather than captured seller labels."""
    return {
        'id': customer.id, 'customer_id': customer.id, 'name': customer.name,
        'tpid': customer.tpid, 'destination': f'/customer/{customer.id}',
        'seller': {'id': customer.seller.id, 'name': customer.seller.name}
        if customer.seller else None,
    }


def _renewals(snapshot_id: int, customers: dict[int, Customer]) -> dict[int, SqlRenewal]:
    """Read the complete latest account-level source records without a row limit."""
    return {row.customer_id: row for row in SqlRenewal.query.filter(
        SqlRenewal.import_id == snapshot_id, SqlRenewal.customer_id.in_(customers),
    ).all()}


def _renewal_rows(
    customers: dict[int, Customer], records: dict[int, SqlRenewal], reference: date,
) -> list[dict]:
    """Keep every upcoming renewal, including zero and unknown dollar amounts."""
    rows = [{
        **_identity(customers[customer_id]),
        'expiration_date': record.expiration_date.isoformat(),
        'expiration_amount': record.expiration_amount,
        'install_base_cores': record.install_base_cores,
        'sql_iaas_cores': record.sql_iaas_cores,
        'monthly_opportunity': record.monthly_opportunity,
    } for customer_id, record in records.items()
        if record.expiration_date is not None and record.expiration_date >= reference]
    return sorted(rows, key=lambda row: (
        row['expiration_date'], row['name'].casefold(), row['id'],
    ))


def _eos_rows(
    snapshot_id: int, customers: dict[int, Customer],
) -> list[dict]:
    """Find explicit SQL 2016 observations without inferring ESU entitlement."""
    grouped: dict[int, list[SqlArcVersion]] = {}
    for record in SqlArcVersion.query.filter(
        SqlArcVersion.import_id == snapshot_id, SqlArcVersion.customer_id.in_(customers),
    ).all():
        if (record.sql_version.strip().casefold() == SQL_2016_VERSION
                and record.discovery_type.strip().casefold() == ALL_DISCOVERED
                and record.discovered_count is not None and record.discovered_count > 0):
            grouped.setdefault(record.customer_id, []).append(record)
    rows = []
    for customer_id, records in grouped.items():
        subscriptions = [{
            'id': record.subscription_id, 'name': record.subscription_name,
            'discovered_count': record.discovered_count,
            'processed_at': _utc(record.processed_at),
        } for record in records]
        rows.append({
            **_identity(customers[customer_id]),
            'discovered_count': fsum(record.discovered_count for record in records),
            'subscription_count': len({r.subscription_id for r in records if r.subscription_id})
            if any(r.subscription_id for r in records) else None,
            'subscriptions': subscriptions,
            'esu_coverage': 'Unknown',
        })
    return sorted(rows, key=lambda row: (
        -row['discovered_count'], row['name'].casefold(), row['id'],
    ))


def get_sql_motion_report(
    motion: str, reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Use one saved, complete two-source snapshot for report, widget and tool."""
    if motion not in MOTIONS:
        raise ValueError('Choose a supported SQL motion.')
    reference = reference or date.today()
    title, destination = MOTIONS[motion]
    available = has_specialty_access('data')
    snapshot = (
        SqlMotionImport.query.order_by(SqlMotionImport.id.desc()).first() if available else None
    )
    result = {
        'motion': motion, 'title': title, 'destination': destination, 'available': available,
        'snapshot': None, 'rows': [], 'seller_groups': [], 'count': 0,
        'discovered_count': 0, 'notices': [],
        'data_warning': SQL_2016_DATA_WARNING if motion == 'eos' else None,
    }
    if motion == 'renewals':
        result.update({
            'within_90_count': 0, 'expiration_amount': None, 'missing_amount_count': 0,
        })
    if not available:
        result['notices'] = ['SQL motions are available for Data SEs and sellers.']
        return result
    if snapshot is None:
        return result
    metadata = json.loads(snapshot.source_metadata or '{}')
    sources = metadata.get('sources', {})
    if motion == 'eos':
        sources = {'ads_accelerate': sources.get('ads_accelerate', {})}
    result['snapshot'] = {
        'id': snapshot.id, 'imported_at': _utc(snapshot.imported_at),
        'customer_count': snapshot.customer_count, 'sources': sources,
    }
    result['notices'] = list(metadata.get('notices', []))
    customers = _customers(customer_ids)
    rows = (
        _renewal_rows(customers, _renewals(snapshot.id, customers), reference)
        if motion == 'renewals' else _eos_rows(snapshot.id, customers)
    )
    result.update({
        'rows': rows, 'seller_groups': group_rows_by_seller(rows), 'count': len(rows),
    })
    if motion == 'renewals':
        known_amounts = [row['expiration_amount'] for row in rows
                         if row['expiration_amount'] is not None]
        end = (reference + timedelta(days=90)).isoformat()
        result.update({
            'within_90_count': sum(
                reference.isoformat() <= row['expiration_date'] <= end for row in rows
            ),
            'expiration_amount': fsum(known_amounts) if known_amounts else None,
            'missing_amount_count': sum(row['expiration_amount'] is None for row in rows),
        })
    else:
        result['discovered_count'] = fsum(row['discovered_count'] for row in rows)
    return result


def get_sql_renewals_widget(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Expose the same full future-renewal cohort as the backing report."""
    return get_sql_motion_report('renewals', reference, customer_ids=customer_ids)


def get_sql_eos_widget(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Expose explicit SQL 2016 discovery, not inferred unprotected deployments."""
    return get_sql_motion_report('eos', reference, customer_ids=customer_ids)
