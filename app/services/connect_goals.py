"""FY27 Data Connect Goals calculations.

The Action Center, SalesIQ tool, and tests consume this module so widget values
cannot drift from one surface to another.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from app.models import (
    Customer,
    CustomerRevenueData,
    Milestone,
    MsxTask,
    SyncStatus,
    U2CSnapshot,
    UserPreference,
    db,
)
from app.services.activity_coverage import fiscal_year_bounds
from app.services.u2c_snapshot import (
    current_fiscal_quarter,
    fiscal_quarter_date_range,
    get_attainment,
    get_attainment_trend,
)

FY27_DATA_SCOPE = {
    'fiscal_year': 'FY27',
    'workload_prefix': 'Data:',
    'workload_area': 'Data',
    'revenue_buckets': ('Databases', 'Fabric'),
}
ACTIVE_MILESTONE_STATUSES = ('On Track', 'At Risk', 'Blocked')
TEAM_COVERAGE_TARGET = 50.0
HOK_COVERAGE_TARGET = 100.0
U2C_TARGET = 40.0
VALIDATION_CATEGORIES = {
    606820007: 'Technical Workshop',
    861980002: 'Demo',
    606820009: 'L300+ Demo',
    861980004: 'Architecture Design Session',
    861980005: 'PoC/Pilot',
}


def current_fiscal_year_label(reference: date | None = None) -> str:
    """Return the Microsoft fiscal-year label containing ``reference``."""
    _, fiscal_end = fiscal_year_bounds(reference)
    return f'FY{fiscal_end.year % 100:02d}'


def _selected_buckets(preference: UserPreference | None) -> list[str]:
    """Return the stored bucket selection as a validated list."""
    if not preference or not preference.compensated_buckets:
        return []
    try:
        selected = json.loads(preference.compensated_buckets)
    except (TypeError, ValueError):
        return []
    return selected if isinstance(selected, list) else []


def get_setup_state(reference: date | None = None) -> dict[str, Any]:
    """Return whether annual FY27 Data priorities are confirmed."""
    preference = UserPreference.query.first()
    selected = _selected_buckets(preference)
    required = list(FY27_DATA_SCOPE['revenue_buckets'])
    available = sorted(
        bucket
        for (bucket,) in db.session.query(CustomerRevenueData.bucket).distinct().all()
        if bucket
    )
    fiscal_year = current_fiscal_year_label(reference)
    missing_required = [bucket for bucket in required if bucket not in selected]
    unavailable_selected = [bucket for bucket in selected if bucket not in available]
    confirmed_version = (
        preference.compensated_buckets_confirmed_taxonomy_version
        if preference else None
    )
    taxonomy_version = preference.bucket_taxonomy_version if preference else 0
    confirmed_fiscal_year = (
        preference.compensated_buckets_fiscal_year if preference else None
    )

    reasons = []
    if fiscal_year != FY27_DATA_SCOPE['fiscal_year']:
        reasons.append('This v1 report is configured for FY27 Data priorities.')
    if missing_required:
        reasons.append(
            f"Select the required FY27 Data buckets: {', '.join(missing_required)}."
        )
    if confirmed_fiscal_year != fiscal_year:
        reasons.append(f'Confirm priorities for {fiscal_year}.')
    if confirmed_version != taxonomy_version:
        reasons.append('Review priorities against the current MSXI bucket taxonomy.')
    if unavailable_selected:
        reasons.append(
            f"Remove unavailable buckets: {', '.join(unavailable_selected)}."
        )
    if not available:
        reasons.append('Import or sync revenue data before configuring priorities.')

    return {
        'ready': not reasons,
        'reasons': reasons,
        'fiscal_year': fiscal_year,
        'configured_fiscal_year': FY27_DATA_SCOPE['fiscal_year'],
        'selected_buckets': selected,
        'required_buckets': required,
        'missing_required': missing_required,
        'available_buckets': available,
        'taxonomy_version': taxonomy_version,
        'confirmed_taxonomy_version': confirmed_version,
    }


def _pace_status(actual: float, expected: float) -> tuple[str, str, float]:
    """Return semantic tone, label, and expected-pace ratio."""
    if expected <= 0:
        return 'neutral', 'Quarter just started', 1.0
    if actual <= 0 and expected < 1:
        return 'neutral', 'Quarter just started', 1.0
    ratio = actual / expected
    if ratio >= 1:
        return 'success', 'On pace', ratio
    if ratio >= 0.8:
        return 'warning', 'Near pace', ratio
    if ratio >= 0.6:
        return 'orange', 'Behind pace', ratio
    return 'danger', 'Needs attention', ratio


def _goal_status(value: float, target: float) -> tuple[str, str, float]:
    """Return semantic tone, label, and attainment ratio for a fixed goal."""
    ratio = value / target if target > 0 else 0.0
    if ratio >= 1:
        return 'success', 'Goal reached', ratio
    if ratio >= 0.8:
        return 'warning', 'Near goal', ratio
    if ratio >= 0.6:
        return 'orange', 'Progressing', ratio
    return 'danger', 'Needs attention', ratio


def get_u2c_widget(reference: date | None = None) -> dict[str, Any]:
    """Return FY27 Data U2C attainment and trend."""
    reference = reference or date.today()
    fiscal_quarter = current_fiscal_quarter(reference)
    snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fiscal_quarter).first()
    if not snapshot:
        return {
            'key': 'u2c',
            'available': False,
            'title': 'Quarterly U2C progression',
            'message': f'No official {fiscal_quarter} U2C snapshot is available.',
            'destination': '/reports/u2c',
        }

    attainment = get_attainment(snapshot.id, FY27_DATA_SCOPE['workload_prefix'])
    trend = get_attainment_trend(snapshot.id, FY27_DATA_SCOPE['workload_area'])
    quarter_start, quarter_end = fiscal_quarter_date_range(fiscal_quarter)
    elapsed_days = max(0, min((reference - quarter_start).days + 1,
                              (quarter_end - quarter_start).days + 1))
    total_days = (quarter_end - quarter_start).days + 1
    expected_today = round(U2C_TARGET * (elapsed_days / total_days), 1)
    tone, label, ratio = _pace_status(attainment['u2c_pct'], expected_today)
    status = SyncStatus.get_status('u2c_import')

    return {
        'key': 'u2c',
        'available': True,
        'title': 'Quarterly U2C progression',
        'value': attainment['u2c_pct'],
        'unit': '%',
        'target': U2C_TARGET,
        'expected_today': expected_today,
        'pace_ratio': round(ratio, 2),
        'status_tone': tone,
        'status_label': label,
        'status_explanation': (
            f"{attainment['u2c_pct']:.1f}% committed against "
            f"{expected_today:.1f}% expected by today and a {U2C_TARGET:.0f}% "
            f"quarter target."
        ),
        'fiscal_quarter': fiscal_quarter,
        'committed_acr': attainment['committed_total'],
        'starting_acr': attainment['target_total'],
        'remaining_count': attainment['remaining_count'],
        'trend': trend,
        'period_start': quarter_start.isoformat(),
        'period_end': quarter_end.isoformat(),
        'freshness': status.get('completed_at') or snapshot.snapshot_date,
        'source_state': status.get('state'),
        'destination': '/reports/u2c',
    }


def _milestone_scope(reference: date | None = None):
    """Return the provisional active FY27 Data milestone query."""
    fiscal_start, fiscal_end = fiscal_year_bounds(reference)
    next_fiscal_end = date(fiscal_end.year + 1, 6, 30)
    return (
        Milestone.query
        .filter(Milestone.msx_milestone_id.isnot(None))
        .filter(Milestone.customer_id.isnot(None))
        .filter(Milestone.msx_status.in_(ACTIVE_MILESTONE_STATUSES))
        .filter(Milestone.due_date.isnot(None))
        .filter(Milestone.due_date >= datetime.combine(fiscal_start, datetime.min.time()))
        .filter(Milestone.due_date <= datetime.combine(next_fiscal_end, datetime.max.time()))
        .filter(Milestone.workload.like(f"{FY27_DATA_SCOPE['workload_prefix']}%"))
    )


def get_team_coverage_widget(reference: date | None = None) -> dict[str, Any]:
    """Return ACR-weighted milestone team coverage."""
    milestones = _milestone_scope(reference).all()
    total_acr = sum(max(milestone.monthly_usage or 0.0, 0.0) for milestone in milestones)
    on_team = [milestone for milestone in milestones if milestone.on_my_team]
    on_team_acr = sum(max(milestone.monthly_usage or 0.0, 0.0) for milestone in on_team)
    coverage = round((on_team_acr / total_acr) * 100, 1) if total_acr else 0.0
    tone, label, goal_ratio = _goal_status(coverage, TEAM_COVERAGE_TARGET)
    status = SyncStatus.get_status('milestones')

    return {
        'key': 'team_coverage',
        'available': bool(milestones),
        'title': 'Milestone team coverage',
        'value': coverage,
        'unit': '%',
        'target': TEAM_COVERAGE_TARGET,
        'goal_ratio': round(goal_ratio, 2),
        'status_tone': tone,
        'status_label': label,
        'status_explanation': (
            f'{coverage:.1f}% of in-scope milestone ACR is on milestones where '
            f'you are on the team. That is {goal_ratio * 100:.0f}% of the '
            f'provisional {TEAM_COVERAGE_TARGET:.0f}% goal.'
        ),
        'on_team_acr': round(on_team_acr, 2),
        'total_acr': round(total_acr, 2),
        'on_team_count': len(on_team),
        'total_count': len(milestones),
        'off_team_count': len(milestones) - len(on_team),
        'freshness': status.get('completed_at'),
        'source_state': status.get('state'),
        'destination': '/reports/milestone-tracker',
        'destination_filter': {
            'area': ['Data'],
            'myTeam': 'off',
            'seller': '',
            'status': list(ACTIVE_MILESTONE_STATUSES),
            'urgency': '',
            'commitment': '',
            'quarters': [
                'FY27 Q1', 'FY27 Q2', 'FY27 Q3', 'FY27 Q4',
                'FY28 Q1', 'FY28 Q2', 'FY28 Q3', 'FY28 Q4',
            ],
            'favoritesOnly': False,
        },
    }


def _task_date(task: MsxTask) -> date:
    """Return the date used to place an MSX task in a fiscal year."""
    return (task.due_date or task.created_at).date()


def get_hok_coverage_widget(reference: date | None = None) -> dict[str, Any]:
    """Return current-FY HoK coverage for the provisional milestone scope."""
    reference = reference or date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(reference)
    milestones = _milestone_scope(reference).filter(
        Milestone.on_my_team.is_(True)
    ).all()
    milestone_ids = [milestone.id for milestone in milestones]
    tasks = (
        MsxTask.query
        .filter(MsxTask.milestone_id.in_(milestone_ids))
        .filter(MsxTask.is_hok.is_(True))
        .all()
    ) if milestone_ids else []
    current_tasks = [
        task for task in tasks
        if fiscal_start <= _task_date(task) <= fiscal_end
    ]
    covered_ids = {task.milestone_id for task in current_tasks}
    covered = sum(milestone.id in covered_ids for milestone in milestones)
    total = len(milestones)
    coverage = round((covered / total) * 100, 1) if total else 0.0
    tone, label, goal_ratio = _goal_status(coverage, HOK_COVERAGE_TARGET)
    validation_counts = {
        label: sum(task.task_category == category for task in current_tasks)
        for category, label in VALIDATION_CATEGORIES.items()
    }
    status = SyncStatus.get_status('milestones')

    return {
        'key': 'hok_coverage',
        'available': bool(milestones),
        'title': 'Milestone HoK coverage',
        'value': coverage,
        'unit': '%',
        'target': HOK_COVERAGE_TARGET,
        'goal_ratio': round(goal_ratio, 2),
        'status_tone': tone,
        'status_label': label,
        'status_explanation': (
            f'{covered} of {total} active, on-team Data milestones due this or '
            'next fiscal year have a qualifying HoK this fiscal year. '
            f'That is {goal_ratio * 100:.0f}% of the {HOK_COVERAGE_TARGET:.0f}% goal.'
        ),
        'covered': covered,
        'uncovered': total - covered,
        'total_count': total,
        'validation_counts': validation_counts,
        'freshness': status.get('completed_at'),
        'source_state': status.get('state'),
        'destination': (
            '/reports/activity-coverage?lens=milestones&coverage=fy&scope=fy27-data'
        ),
    }


def _month_offset(value: date, months: int) -> date:
    """Return the first day of a month offset from ``value``."""
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _month_distance(start: date, end: date) -> int:
    """Return the number of whole month boundaries from start to end."""
    return (end.year - start.year) * 12 + end.month - start.month


def _whitespace_history_bounds(
    win_month: date,
    earliest_month: date,
    latest_month: date,
) -> tuple[date, date]:
    """Return an eight-month window centered on the four-month win sequence."""
    history_start = _month_offset(win_month, -5)
    history_end = _month_offset(win_month, 2)

    if history_end > latest_month:
        shift = _month_distance(latest_month, history_end)
        history_start = _month_offset(history_start, -shift)
        history_end = latest_month
    if history_start < earliest_month:
        shift = _month_distance(history_start, earliest_month)
        history_start = earliest_month
        history_end = min(_month_offset(history_end, shift), latest_month)

    return history_start, history_end


def _current_half(reference: date) -> tuple[date, date, str]:
    """Return fiscal-half month bounds and label for ``reference``."""
    if reference.month >= 7:
        fiscal_year = reference.year + 1
        return date(reference.year, 7, 1), date(reference.year, 12, 1), (
            f'FY{fiscal_year % 100:02d} H1'
        )
    return date(reference.year, 1, 1), date(reference.year, 6, 1), (
        f'FY{reference.year % 100:02d} H2'
    )


def get_whitespace_widget(reference: date | None = None) -> dict[str, Any]:
    """Return rolling-three-month whitespace wins for the current fiscal half."""
    reference = reference or date.today()
    half_start, half_end, half_label = _current_half(reference)
    buckets = FY27_DATA_SCOPE['revenue_buckets']
    rows = (
        CustomerRevenueData.query
        .filter(CustomerRevenueData.customer_id.isnot(None))
        .filter(CustomerRevenueData.bucket.in_(buckets))
        .order_by(CustomerRevenueData.month_date)
        .all()
    )
    global_months = {
        month
        for (month,) in db.session.query(CustomerRevenueData.month_date).distinct().all()
    }
    revenue_by_key: dict[tuple[int, str, date], float] = defaultdict(float)
    for row in rows:
        revenue_by_key[(row.customer_id, row.bucket, row.month_date)] += row.revenue or 0.0

    wins: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in buckets}
    customer_names = {row.customer_id: row.customer_name for row in rows}
    customer_favicons = {
        customer.id: customer.favicon_b64
        for customer in Customer.query.filter(
            Customer.id.in_(customer_names)
        ).all()
    }
    customer_bucket_pairs = {(row.customer_id, row.bucket) for row in rows}
    earliest_month = min(global_months) if global_months else None
    latest_month = max(global_months) if global_months else None
    latest_allowed = min(
        half_end,
        date(reference.year, reference.month, 1),
        latest_month or half_end,
    )
    for customer_id, bucket in customer_bucket_pairs:
        month = half_start
        while month <= latest_allowed:
            current_revenue = revenue_by_key[(customer_id, bucket, month)]
            prior_months = [_month_offset(month, offset) for offset in (-3, -2, -1)]
            has_complete_history = all(prior in global_months for prior in prior_months)
            prior_is_zero = all(
                revenue_by_key[(customer_id, bucket, prior)] <= 0
                for prior in prior_months
            )
            if current_revenue > 0 and has_complete_history and prior_is_zero:
                history = []
                history_start, history_end = _whitespace_history_bounds(
                    month,
                    earliest_month or month,
                    latest_allowed,
                )
                history_month = history_start
                while history_month <= history_end:
                    history.append({
                        'month': history_month.isoformat(),
                        'label': history_month.strftime('%b%y'),
                        'revenue': round(
                            revenue_by_key[(customer_id, bucket, history_month)],
                            2,
                        ),
                    })
                    history_month = _month_offset(history_month, 1)
                wins[bucket].append({
                    'customer_id': customer_id,
                    'customer_name': customer_names[customer_id],
                    'favicon_b64': customer_favicons.get(customer_id),
                    'bucket': bucket,
                    'first_positive_month': month.isoformat(),
                    'revenue': round(current_revenue, 2),
                    'history': history,
                })
                break
            month = _month_offset(month, 1)

    bucket_progress = []
    for bucket in buckets:
        wins[bucket].sort(
            key=lambda item: (-item['revenue'], item['customer_name'].lower())
        )
        count = len(wins[bucket])
        bucket_progress.append({
            'bucket': bucket,
            'wins': count,
            'target': 1,
            'complete': count >= 1,
        })
    complete = all(item['complete'] for item in bucket_progress)
    statuses = [
        SyncStatus.get_status('revenue_sync'),
        SyncStatus.get_status('revenue_import'),
    ]
    status = max(
        statuses,
        key=lambda item: item.get('completed_at') or datetime.min,
    )
    return {
        'key': 'whitespace',
        'available': bool(rows),
        'title': 'Fabric and database whitespace wins',
        'status_tone': 'success' if complete else 'warning',
        'status_label': 'Goal reached' if complete else 'Wins still needed',
        'status_explanation': (
            'A win is the first positive ACR month after three complete zero-ACR '
            'months for the same customer and bucket.'
        ),
        'half_label': half_label,
        'bucket_progress': bucket_progress,
        'wins': wins,
        'freshness': status.get('completed_at') or latest_month,
        'data_through': latest_month,
        'source_state': status.get('state'),
        'destination': '/reports/whitespace',
    }


def get_connect_goals(reference: date | None = None) -> dict[str, Any]:
    """Return setup state and all FY27 Data Action Center widgets."""
    setup = get_setup_state(reference)
    widgets = []
    if setup['ready']:
        widgets = [
            get_u2c_widget(reference),
            get_team_coverage_widget(reference),
            get_hok_coverage_widget(reference),
            get_whitespace_widget(reference),
        ]
    return {
        'setup': setup,
        'scope': FY27_DATA_SCOPE,
        'widgets': widgets,
    }
