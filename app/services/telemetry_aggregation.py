"""
Telemetry aggregation service for Sales Buddy.

Rolls up raw ``UsageEvent`` rows into compact ``DailyFeatureStats`` records
and computes feature-health metrics (popularity ranking, dead features,
trend direction).  All data stays local -- no PII, no external calls.

Usage::

    from app.services.telemetry_aggregation import (
        aggregate_daily_stats,
        get_feature_health,
    )

    # Aggregate yesterday's events (safe to call repeatedly -- idempotent)
    aggregate_daily_stats(days_back=1)

    # Get feature health report for the last 30 days
    report = get_feature_health(days=30)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, case, distinct

from app.models import db, UsageEvent, DailyFeatureStats


# ============================================================================
# Aggregation
# ============================================================================

# Default number of days of raw events to keep after aggregation.
RAW_RETENTION_DAYS = 90


def aggregate_daily_stats(
    days_back: int = 1,
    prune_raw: bool = False,
    raw_retention_days: int = RAW_RETENTION_DAYS,
) -> dict[str, Any]:
    """Roll up raw events into the ``daily_feature_stats`` table.

    For each day in the range [today - days_back, yesterday] (inclusive),
    groups events by (date, category, endpoint, method) and upserts into
    ``DailyFeatureStats``.

    Args:
        days_back: How many days to look back for un-aggregated events.
                   Pass a large number on first run to backfill.
        prune_raw: If True, delete raw events older than *raw_retention_days*.
        raw_retention_days: Number of days of raw events to keep.

    Returns:
        A dict with ``days_processed``, ``rows_upserted``, and optionally
        ``raw_events_pruned``.
    """
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days_back)

    rows_upserted = 0
    days_processed = 0

    for offset in range(days_back):
        target_date = start_date + timedelta(days=offset)
        if target_date >= today:
            # Never aggregate today -- the day isn't over yet.
            break

        day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        # Group events for this day
        rows = (
            UsageEvent.query
            .filter(UsageEvent.timestamp >= day_start, UsageEvent.timestamp < day_end)
            .with_entities(
                UsageEvent.category,
                UsageEvent.endpoint,
                UsageEvent.method,
                UsageEvent.is_api,
                func.count().label('event_count'),
                func.sum(case((UsageEvent.status_code >= 400, 1), else_=0)).label('error_count'),
                func.avg(UsageEvent.response_time_ms).label('avg_ms'),
                func.count(distinct(UsageEvent.referrer_path)).label('unique_refs'),
            )
            .group_by(
                UsageEvent.category,
                UsageEvent.endpoint,
                UsageEvent.method,
                UsageEvent.is_api,
            )
            .all()
        )

        for row in rows:
            existing = DailyFeatureStats.query.filter_by(
                date=target_date,
                category=row.category or 'Unknown',
                endpoint=row.endpoint,
                method=row.method,
            ).first()

            if existing:
                existing.event_count = row.event_count
                existing.error_count = row.error_count
                existing.avg_response_ms = round(row.avg_ms, 2) if row.avg_ms else None
                existing.unique_referrers = row.unique_refs
                existing.is_api = row.is_api
            else:
                db.session.add(DailyFeatureStats(
                    date=target_date,
                    category=row.category or 'Unknown',
                    endpoint=row.endpoint,
                    method=row.method,
                    is_api=row.is_api,
                    event_count=row.event_count,
                    error_count=row.error_count,
                    avg_response_ms=round(row.avg_ms, 2) if row.avg_ms else None,
                    unique_referrers=row.unique_refs,
                ))
            rows_upserted += 1

        days_processed += 1

    db.session.commit()

    result: dict[str, Any] = {
        'days_processed': days_processed,
        'rows_upserted': rows_upserted,
    }

    # Optional: prune old raw events
    if prune_raw and raw_retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=raw_retention_days)
        pruned = UsageEvent.query.filter(UsageEvent.timestamp < cutoff).delete()
        db.session.commit()
        result['raw_events_pruned'] = pruned

    return result


# ============================================================================
# Feature-health report
# ============================================================================

# All known feature categories that should show usage.  If a category has
# zero events it's flagged as "dead".
_KNOWN_CATEGORIES: list[str] = [
    'Admin',
    'AI',
    'Backup',
    'Notes',
    'Connect Export',
    'Customers',
    'General',
    'Milestones',
    'MSX Integration',
    'Opportunities',
    'Partners',
    'Pods',
    'Revenue',
    'Sellers',
    'Solution Engineers',
    'Territories',
    'Topics',
]


def get_feature_health(days: int = 30) -> dict[str, Any]:
    """Compute a feature-health report from aggregated daily stats.

    Uses ``DailyFeatureStats`` for completed days and supplements with
    today's raw ``UsageEvent`` data so the report is always current.

    Args:
        days: Number of days to analyse.

    Returns:
        A dict with ``feature_ranking``, ``dead_features``, ``trends``,
        and ``period``.
    """
    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=days)

    # ------- Aggregated data (completed days) ------- #
    agg_rows = (
        DailyFeatureStats.query
        .filter(DailyFeatureStats.date >= cutoff_date, DailyFeatureStats.date < today)
        .with_entities(
            DailyFeatureStats.category,
            func.sum(DailyFeatureStats.event_count).label('total'),
            func.sum(DailyFeatureStats.error_count).label('errors'),
            func.avg(DailyFeatureStats.avg_response_ms).label('avg_ms'),
        )
        .group_by(DailyFeatureStats.category)
        .all()
    )
    cat_stats: dict[str, dict] = {}
    for row in agg_rows:
        cat_stats[row.category] = {
            'total': row.total or 0,
            'errors': row.errors or 0,
            'avg_ms': round(row.avg_ms, 1) if row.avg_ms else None,
        }

    # ------- Today's live data ------- #
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    today_rows = (
        UsageEvent.query
        .filter(UsageEvent.timestamp >= today_start)
        .with_entities(
            UsageEvent.category,
            func.count().label('total'),
            func.sum(case((UsageEvent.status_code >= 400, 1), else_=0)).label('errors'),
            func.avg(UsageEvent.response_time_ms).label('avg_ms'),
        )
        .group_by(UsageEvent.category)
        .all()
    )
    for row in today_rows:
        cat = row.category or 'Unknown'
        if cat in cat_stats:
            cat_stats[cat]['total'] += (row.total or 0)
            cat_stats[cat]['errors'] += (row.errors or 0)
        else:
            cat_stats[cat] = {
                'total': row.total or 0,
                'errors': row.errors or 0,
                'avg_ms': round(row.avg_ms, 1) if row.avg_ms else None,
            }

    # ------- Feature ranking ------- #
    grand_total = sum(s['total'] for s in cat_stats.values()) or 1
    ranking = sorted(
        [
            {
                'category': cat,
                'events': stats['total'],
                'errors': stats['errors'],
                'avg_response_ms': stats['avg_ms'],
                'share_pct': round(stats['total'] / grand_total * 100, 1),
            }
            for cat, stats in cat_stats.items()
        ],
        key=lambda r: r['events'],
        reverse=True,
    )

    # ------- Dead features ------- #
    active_categories = set(cat_stats.keys())
    dead_features = sorted(set(_KNOWN_CATEGORIES) - active_categories)

    # ------- Trend analysis (first half vs second half of the period) ------- #
    mid_date = cutoff_date + timedelta(days=days // 2)

    def _half_totals(start_d: date, end_d: date) -> dict[str, int]:
        rows = (
            DailyFeatureStats.query
            .filter(DailyFeatureStats.date >= start_d, DailyFeatureStats.date < end_d)
            .with_entities(
                DailyFeatureStats.category,
                func.sum(DailyFeatureStats.event_count).label('total'),
            )
            .group_by(DailyFeatureStats.category)
            .all()
        )
        return {r.category: r.total for r in rows}

    first_half = _half_totals(cutoff_date, mid_date)
    second_half = _half_totals(mid_date, today)

    trends: list[dict[str, Any]] = []
    all_cats = set(first_half.keys()) | set(second_half.keys())
    for cat in all_cats:
        before = first_half.get(cat, 0)
        after = second_half.get(cat, 0)
        if before == 0 and after == 0:
            continue
        if before == 0:
            change_pct = 100.0
        else:
            change_pct = round((after - before) / before * 100, 1)
        trends.append({
            'category': cat,
            'first_half': before,
            'second_half': after,
            'change_pct': change_pct,
            'direction': 'up' if change_pct > 10 else ('down' if change_pct < -10 else 'stable'),
        })
    trends.sort(key=lambda t: abs(t['change_pct']), reverse=True)

    # ------- Top endpoints across all features ------- #
    top_endpoints = (
        DailyFeatureStats.query
        .filter(DailyFeatureStats.date >= cutoff_date)
        .with_entities(
            DailyFeatureStats.category,
            DailyFeatureStats.endpoint,
            DailyFeatureStats.method,
            func.sum(DailyFeatureStats.event_count).label('total'),
            func.sum(DailyFeatureStats.error_count).label('errors'),
        )
        .group_by(DailyFeatureStats.category, DailyFeatureStats.endpoint, DailyFeatureStats.method)
        .order_by(func.sum(DailyFeatureStats.event_count).desc())
        .limit(30)
        .all()
    )

    return {
        'period': {
            'days': days,
            'start': cutoff_date.isoformat(),
            'end': today.isoformat(),
        },
        'feature_ranking': ranking,
        'dead_features': dead_features,
        'trends': trends,
        'top_endpoints': [
            {
                'category': r.category,
                'endpoint': r.endpoint,
                'method': r.method,
                'events': r.total,
                'errors': r.errors,
            }
            for r in top_endpoints
        ],
    }
