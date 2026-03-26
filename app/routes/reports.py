"""Reports blueprint - cross-domain reports hub and individual report views."""
import logging
from datetime import datetime, timedelta, timezone, date
from flask import Blueprint, render_template, url_for, jsonify
from app.models import (
    db, Customer, Engagement, Note, Milestone, Seller, SolutionEngineer,
    Topic, RevenueAnalysis, notes_engagements, notes_milestones,
)
from sqlalchemy import func, desc, or_

logger = logging.getLogger(__name__)

bp = Blueprint('reports', __name__)


@bp.route('/reports')
def reports_hub():
    """Reports hub - lists all available reports grouped by goal."""
    report_groups = [
        {
            'title': 'Meeting Prep',
            'icon': 'bi-people',
            'reports': [
                {
                    'id': 'one-on-one',
                    'name': '1:1 Manager / SE Report',
                    'description': (
                        'Active engagements and recent notes for 1:1 meeting prep. '
                        'Shows what you have been working on and where milestones stand.'
                    ),
                    'icon': 'bi-chat-left-text',
                    'url': url_for('reports.report_one_on_one'),
                },
            ],
        },
        {
            'title': 'Revenue Analysis',
            'icon': 'bi-currency-dollar',
            'reports': [
                {
                    'id': 'revenue-reports',
                    'name': 'Revenue Reports',
                    'description': (
                        'Bespoke reports built from imported revenue data '
                        '(new product users, churn risk, etc.).'
                    ),
                    'icon': 'bi-bar-chart-line',
                    'url': url_for('revenue.reports_list'),
                },
            ],
        },
    ]

    return render_template('reports_hub.html', report_groups=report_groups)


@bp.route('/reports/one-on-one')
def report_one_on_one():
    """1:1 Manager / SE meeting prep report.

    Two views:
    - Last 2 weeks: engagements with recent note activity, plus standalone notes
    - All time: all open engagements grouped by customer
    """
    now = datetime.now(timezone.utc)
    two_weeks_ago = now - timedelta(days=14)

    # --- Recent activity (last 2 weeks) ---
    # Notes with call_date in last 2 weeks are the source of truth for activity
    recent_notes = (
        Note.query
        .filter(Note.call_date >= two_weeks_ago)
        .order_by(desc(Note.call_date))
        .all()
    )

    # Engagements linked to those recent notes (not filtered by updated_at)
    recent_engagement_ids = set()
    recent_engagements = []
    for note in recent_notes:
        for eng in note.engagements:
            if eng.id not in recent_engagement_ids:
                recent_engagement_ids.add(eng.id)
                recent_engagements.append(eng)

    # Build recent activity grouped by customer
    recent_customers = {}
    for eng in recent_engagements:
        cust = eng.customer
        if cust and cust.id not in recent_customers:
            recent_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
                'notes': [],
            }
        if cust:
            recent_customers[cust.id]['engagements'].append(eng)

    for note in recent_notes:
        cust = note.customer
        if cust and cust.id not in recent_customers:
            recent_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
                'notes': [],
            }
        if cust:
            recent_customers[cust.id]['notes'].append(note)

    # Sort recent customers by most recent note call_date
    recent_sorted = sorted(
        recent_customers.values(),
        key=lambda c: max(
            [n.call_date for n in c['notes'] if n.call_date] +
            [datetime.min]
        ),
        reverse=True,
    )

    # --- All open engagements ---
    open_engagements = (
        Engagement.query
        .filter(Engagement.status == 'Active')
        .all()
    )

    # Group by customer
    all_customers = {}
    for eng in open_engagements:
        cust = eng.customer
        if cust and cust.id not in all_customers:
            all_customers[cust.id] = {
                'customer': cust,
                'engagements': [],
            }
        if cust:
            all_customers[cust.id]['engagements'].append(eng)

    # Sort all-open by most recent linked note call_date
    all_sorted = sorted(
        all_customers.values(),
        key=lambda c: max(
            [eng.last_note_date or datetime.min for eng in c['engagements']] +
            [datetime.min]
        ),
        reverse=True,
    )

    # --- Milestone highlights (on_my_team only) ---
    # Recently committed milestones (last 2 weeks)
    # Uses committed_at date set by sync change detection
    milestone_commitments = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.committed_at >= two_weeks_ago,
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(Milestone.committed_at))
        .all()
    )

    # Upcoming or overdue active milestones to follow up on
    today = date.today()

    # Current fiscal quarter milestones (on my team, active, with due date)
    from datetime import time as _time
    fy_month = (today.month - 7) % 12
    fq_start = (fy_month // 3) * 3
    q_start_month = ((fq_start + 7 - 1) % 12) + 1
    quarter_start = datetime.combine(date(today.year, q_start_month, 1), _time.min)
    end_month = q_start_month + 3
    end_year = today.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    quarter_end = datetime.combine(
        date(end_year, end_month, 1) - timedelta(days=1), _time(23, 59, 59)
    )

    quarter_milestones = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Milestone.due_date.isnot(None),
            Milestone.due_date >= quarter_start,
            Milestone.due_date <= quarter_end,
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(func.coalesce(Milestone.monthly_usage, 0)))
        .all()
    )

    # --- Topic trends (last 2 weeks + FY sparkline) ---
    # Current 2-week counts for the card
    topic_counts = {}
    for note in recent_notes:
        for topic in note.topics:
            if topic.name not in topic_counts:
                topic_counts[topic.name] = {'id': topic.id, 'count': 0, 'customers': set()}
            topic_counts[topic.name]['count'] += 1
            if note.customer:
                topic_counts[topic.name]['customers'].add(note.customer.name)

    # FY sparkline: monthly note counts per topic since FY start
    fy_year = today.year if today.month >= 7 else today.year - 1
    fy_start = datetime(fy_year, 7, 1)
    # Build list of month keys from FY start to current month
    fy_months = []
    m_year, m_month = fy_year, 7
    while (m_year, m_month) <= (today.year, today.month):
        fy_months.append((m_year, m_month))
        m_month += 1
        if m_month > 12:
            m_month = 1
            m_year += 1
    fy_month_labels = [
        datetime(y, m, 1).strftime('%b') for y, m in fy_months
    ]

    # Query all notes from FY start with their topics
    fy_notes = (
        Note.query
        .filter(Note.call_date >= fy_start)
        .options(db.joinedload(Note.topics))
        .all()
    )
    # Build per-topic monthly counts
    topic_monthly = {}  # topic_name -> {(year, month): count}
    for note in fy_notes:
        key = (note.call_date.year, note.call_date.month)
        for topic in note.topics:
            if topic.name not in topic_monthly:
                topic_monthly[topic.name] = {}
            topic_monthly[topic.name][key] = topic_monthly[topic.name].get(key, 0) + 1

    # Sort by 2-week frequency, attach sparkline data
    top_topics = sorted(
        [
            {
                'id': data['id'],
                'name': name,
                'note_count': data['count'],
                'customer_count': len(data['customers']),
                'sparkline': [
                    topic_monthly.get(name, {}).get(mk, 0) for mk in fy_months
                ],
            }
            for name, data in topic_counts.items()
        ],
        key=lambda t: t['note_count'],
        reverse=True,
    )[:10]

    # --- Revenue alerts reviewed in last 2 weeks ---
    reviewed_alerts = (
        RevenueAnalysis.query
        .filter(
            RevenueAnalysis.review_status.in_(['reviewed', 'actioned']),
            RevenueAnalysis.reviewed_at >= two_weeks_ago,
        )
        .order_by(desc(RevenueAnalysis.reviewed_at))
        .all()
    )

    # Stats
    commitments_acr = sum(m.monthly_usage or 0 for m in milestone_commitments)
    stats = {
        'recent_engagement_count': len(recent_engagements),
        'recent_note_count': len(recent_notes),
        'recent_customer_count': len(recent_customers),
        'open_engagement_count': len(open_engagements),
        'open_customer_count': len(all_customers),
        'commitments_acr': commitments_acr,
    }

    return render_template(
        'report_one_on_one.html',
        recent_customers=recent_sorted,
        all_customers=all_sorted,
        stats=stats,
        cutoff_date=two_weeks_ago,
        milestone_commitments=milestone_commitments,
        quarter_milestones=quarter_milestones,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        today=today,
        reviewed_alerts=reviewed_alerts,
        top_topics=top_topics,
        fy_month_labels=fy_month_labels,
    )


@bp.route('/api/reports/sync-milestone-dates', methods=['POST'])
def sync_milestone_dates():
    """Fetch audit history from MSX to populate committed_at/completed_at dates.

    Hits the Dynamics 365 audit endpoint for each on-my-team milestone
    to find the actual transition dates. Only called on-demand from the
    1:1 report page.
    """
    from app.services.milestone_audit import sync_milestone_audit_dates

    try:
        result = sync_milestone_audit_dates()
        return jsonify(result)
    except Exception as e:
        logger.exception("Milestone audit date sync failed")
        return jsonify({"success": False, "error": str(e)}), 500
