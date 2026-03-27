"""Reports blueprint - cross-domain reports hub and individual report views."""
import logging
from datetime import datetime, timedelta, timezone, date
from flask import Blueprint, render_template, url_for, jsonify, request
from app.models import (
    db, Customer, Engagement, Note, Milestone, Seller, SolutionEngineer,
    Topic, RevenueAnalysis, HygieneNote,
    notes_engagements, notes_milestones, notes_topics,
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
                    'id': 'new-synapse-users',
                    'name': 'New Azure Synapse Analytics Users',
                    'description': (
                        'Customers who have started using Azure Synapse Analytics '
                        'in the last 6 months, grouped by seller.'
                    ),
                    'icon': 'bi-database-gear',
                    'url': url_for('revenue.report_new_synapse_users'),
                },
            ],
        },
        {
            'title': 'Workload Coverage',
            'icon': 'bi-diagram-3',
            'reports': [
                {
                    'id': 'workload-report',
                    'name': 'Workload Report',
                    'description': (
                        'Customers grouped by workload topic. Quickly find who is '
                        'working on a specific technology for workshop targeting.'
                    ),
                    'icon': 'bi-tag',
                    'url': url_for('reports.report_workload'),
                },
            ],
        },
        {
            'title': 'Data Hygiene',
            'icon': 'bi-clipboard-check',
            'reports': [
                {
                    'id': 'hygiene-report',
                    'name': 'Engagement / Milestone Hygiene',
                    'description': (
                        'Engagements without milestones and milestones without '
                        'engagements. Add notes explaining why so you can report '
                        'on gaps quickly.'
                    ),
                    'icon': 'bi-link-45deg',
                    'url': url_for('reports.report_hygiene'),
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


@bp.route('/reports/workload')
def report_workload():
    """Workload report - customers grouped by topic/workload.

    All data is loaded server-side; date filtering is done client-side
    using the call_date on each customer-topic entry.
    """
    from app.services.seller_mode import get_seller_mode_seller_id
    seller_mode_sid = get_seller_mode_seller_id()

    # Get all topics with at least one note
    topics_with_notes = (
        Topic.query
        .join(notes_topics, Topic.id == notes_topics.c.topic_id)
        .join(Note, Note.id == notes_topics.c.note_id)
        .group_by(Topic.id)
        .having(func.count(Note.id) > 0)
        .order_by(Topic.name)
        .all()
    )

    workload_groups = []
    for topic in topics_with_notes:
        customer_q = (
            Customer.query
            .join(Note, Note.customer_id == Customer.id)
            .join(notes_topics, Note.id == notes_topics.c.note_id)
            .filter(notes_topics.c.topic_id == topic.id)
        )
        if seller_mode_sid:
            customer_q = customer_q.filter(Customer.seller_id == seller_mode_sid)
        customers = customer_q.distinct().order_by(Customer.name).all()

        if not customers:
            continue

        customer_data = []
        total_acr = 0
        for cust in customers:
            active_engs = [e for e in cust.engagements if e.status == 'Active']

            # Get milestones linked to notes tagged with this topic for this customer
            topic_milestones = (
                Milestone.query
                .join(notes_milestones, Milestone.id == notes_milestones.c.milestone_id)
                .join(Note, Note.id == notes_milestones.c.note_id)
                .join(notes_topics, Note.id == notes_topics.c.note_id)
                .filter(
                    notes_topics.c.topic_id == topic.id,
                    Note.customer_id == cust.id,
                    Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
                )
                .distinct()
                .all()
            )
            cust_acr = sum(m.monthly_usage or 0 for m in topic_milestones)
            total_acr += cust_acr

            latest_note = (
                Note.query
                .join(notes_topics, Note.id == notes_topics.c.note_id)
                .filter(
                    notes_topics.c.topic_id == topic.id,
                    Note.customer_id == cust.id,
                )
                .order_by(desc(Note.call_date))
                .first()
            )

            customer_data.append({
                'customer': cust,
                'engagement_count': len(active_engs),
                'milestone_count': len(topic_milestones),
                'acr': cust_acr,
                'latest_note_date': latest_note.call_date if latest_note else None,
                'latest_note_iso': latest_note.call_date.strftime('%Y-%m-%d') if latest_note and latest_note.call_date else '',
                'committed': any(
                    m.customer_commitment == 'Committed' for m in topic_milestones
                ),
            })

        customer_data.sort(key=lambda c: c['acr'], reverse=True)

        workload_groups.append({
            'topic': topic,
            'customer_count': len(customer_data),
            'total_acr': total_acr,
            'customers': customer_data,
        })

    workload_groups.sort(key=lambda g: g['customer_count'], reverse=True)

    # Compute fiscal quarter boundaries for preset buttons
    _today = date.today()
    fy_month = (_today.month - 7) % 12
    fq_start_idx = (fy_month // 3) * 3
    q_start_month = ((fq_start_idx + 7 - 1) % 12) + 1
    fq_start = date(_today.year, q_start_month, 1)
    end_month = q_start_month + 3
    end_year = _today.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    fq_end = date(end_year, end_month, 1) - timedelta(days=1)
    # Previous quarter
    prev_month = q_start_month - 3
    prev_year = _today.year
    if prev_month <= 0:
        prev_month += 12
        prev_year -= 1
    prev_fq_start = date(prev_year, prev_month, 1)
    prev_fq_end = fq_start - timedelta(days=1)

    # FQ labels (e.g. "FQ3: Jan 1 - Mar 31")
    fq_num = (fq_start_idx // 3) + 1
    prev_fq_num = fq_num - 1 if fq_num > 1 else 4

    return render_template(
        'report_workload.html',
        workload_groups=workload_groups,
        fq_start=fq_start.isoformat(),
        fq_end=fq_end.isoformat(),
        fq_label=f'FQ{fq_num}',
        prev_fq_start=prev_fq_start.isoformat(),
        prev_fq_end=prev_fq_end.isoformat(),
        prev_fq_label=f'FQ{prev_fq_num}',
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


# =========================================================================
# Hygiene Report
# =========================================================================

@bp.route('/reports/hygiene')
def report_hygiene():
    """Engagement/Milestone hygiene report.

    Shows engagements without milestones and milestones without
    engagements so gaps can be identified and annotated quickly.
    """
    from app.services.seller_mode import get_seller_mode_seller_id
    seller_mode_sid = get_seller_mode_seller_id()

    # --- Engagements without milestones ---
    eng_q = (
        Engagement.query
        .filter(
            Engagement.status == 'Active',
            ~Engagement.milestones.any(),
        )
        .options(db.joinedload(Engagement.customer))
        .order_by(Engagement.title)
    )
    if seller_mode_sid:
        eng_q = eng_q.join(
            Customer, Engagement.customer_id == Customer.id
        ).filter(Customer.seller_id == seller_mode_sid)
    engagements_no_ms = eng_q.all()

    # --- Milestones without engagements (on my team, active statuses) ---
    ms_q = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            ~Milestone.engagements.any(),
        )
        .options(db.joinedload(Milestone.customer))
        .order_by(Milestone.title)
    )
    if seller_mode_sid:
        ms_q = ms_q.join(
            Customer, Milestone.customer_id == Customer.id
        ).filter(Customer.seller_id == seller_mode_sid)
    milestones_no_eng = ms_q.all()

    # Load hygiene notes for all displayed items
    eng_ids = [e.id for e in engagements_no_ms]
    ms_ids = [m.id for m in milestones_no_eng]

    eng_notes = {}
    if eng_ids:
        for hn in HygieneNote.query.filter(
            HygieneNote.entity_type == 'engagement',
            HygieneNote.entity_id.in_(eng_ids),
        ).all():
            eng_notes[hn.entity_id] = hn.note

    ms_notes = {}
    if ms_ids:
        for hn in HygieneNote.query.filter(
            HygieneNote.entity_type == 'milestone',
            HygieneNote.entity_id.in_(ms_ids),
        ).all():
            ms_notes[hn.entity_id] = hn.note

    return render_template(
        'report_hygiene.html',
        engagements_no_ms=engagements_no_ms,
        milestones_no_eng=milestones_no_eng,
        eng_notes=eng_notes,
        ms_notes=ms_notes,
    )


@bp.route('/api/hygiene-note', methods=['POST'])
def save_hygiene_note():
    """Save or update a hygiene note for an engagement or milestone."""
    data = request.get_json(silent=True) or {}
    entity_type = (data.get('entity_type') or '').strip()
    entity_id = data.get('entity_id')
    note_text = (data.get('note') or '').strip()

    if entity_type not in HygieneNote.ENTITY_TYPES:
        return jsonify(success=False, error='Invalid entity_type'), 400
    if not entity_id:
        return jsonify(success=False, error='entity_id is required'), 400

    try:
        entity_id = int(entity_id)
    except (ValueError, TypeError):
        return jsonify(success=False, error='Invalid entity_id'), 400

    hn = HygieneNote.query.filter_by(
        entity_type=entity_type, entity_id=entity_id
    ).first()

    if note_text:
        if hn:
            hn.note = note_text
            hn.updated_at = datetime.now(timezone.utc)
        else:
            hn = HygieneNote(
                entity_type=entity_type,
                entity_id=entity_id,
                note=note_text,
            )
            db.session.add(hn)
    else:
        # Empty note - delete the record if it exists
        if hn:
            db.session.delete(hn)

    db.session.commit()
    return jsonify(success=True)
