"""Reports blueprint - cross-domain reports hub and individual report views."""
import logging
from datetime import datetime, timedelta, timezone, date
from flask import Blueprint, render_template, url_for, jsonify
from app.models import (
    db, Customer, Engagement, Note, Milestone, Seller, SolutionEngineer,
    notes_engagements, notes_milestones,
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
    # Recently completed or committed milestones (last 2 weeks)
    # Uses committed_at/completed_at dates set by sync change detection
    milestone_wins = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,  # noqa: E712
            or_(
                Milestone.completed_at >= two_weeks_ago,
                Milestone.committed_at >= two_weeks_ago,
            ),
        )
        .order_by(desc(func.coalesce(Milestone.completed_at, Milestone.committed_at)))
        .all()
    )

    # Upcoming or overdue active milestones to follow up on
    today = date.today()
    milestone_followups = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Milestone.due_date.isnot(None),
        )
        .order_by(Milestone.due_date)
        .all()
    )
    # Split into overdue and upcoming (next 30 days)
    overdue_milestones = [m for m in milestone_followups if m.due_date.date() < today]
    upcoming_milestones = [
        m for m in milestone_followups
        if today <= m.due_date.date() <= today + timedelta(days=30)
    ]

    # --- Topic trends (last 2 weeks) ---
    topic_counts = {}
    for note in recent_notes:
        for topic in note.topics:
            if topic.name not in topic_counts:
                topic_counts[topic.name] = {'count': 0, 'customers': set()}
            topic_counts[topic.name]['count'] += 1
            if note.customer:
                topic_counts[topic.name]['customers'].add(note.customer.name)
    # Sort by frequency, convert sets to counts
    top_topics = sorted(
        [
            {'name': name, 'note_count': data['count'],
             'customer_count': len(data['customers'])}
            for name, data in topic_counts.items()
        ],
        key=lambda t: t['note_count'],
        reverse=True,
    )[:10]

    # Stats
    stats = {
        'recent_engagement_count': len(recent_engagements),
        'recent_note_count': len(recent_notes),
        'recent_customer_count': len(recent_customers),
        'open_engagement_count': len(open_engagements),
        'open_customer_count': len(all_customers),
    }

    return render_template(
        'report_one_on_one.html',
        recent_customers=recent_sorted,
        all_customers=all_sorted,
        stats=stats,
        cutoff_date=two_weeks_ago,
        milestone_wins=milestone_wins,
        overdue_milestones=overdue_milestones,
        upcoming_milestones=upcoming_milestones,
        top_topics=top_topics,
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
