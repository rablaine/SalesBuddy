"""Reports blueprint - cross-domain reports hub and individual report views."""
import logging
from datetime import datetime, timedelta, timezone, date
from flask import Blueprint, render_template, url_for, jsonify, request
from app.models import (
    db, Customer, Engagement, Note, Milestone, MilestoneAudit, Seller,
    SolutionEngineer, SyncStatus,
    Topic, RevenueAnalysis, HygieneNote, CustomerRevenueData, ProductRevenueData,
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
            'title': 'Data Hygiene',
            'icon': 'bi-clipboard-check',
            'reports': [
                {
                    'id': 'milestone-tracker',
                    'name': 'Milestone Tracker',
                    'description': (
                        'Surface milestones across all accounts. Find milestones '
                        'you are not aligned to yet and get on the team.'
                    ),
                    'icon': 'bi-flag-fill',
                    'url': url_for('milestones.milestone_tracker'),
                },
                {
                    'id': 'whats-new',
                    'name': "What's New",
                    'description': (
                        'Milestones created or updated in the last 2 weeks. '
                        'Filter by seller to see what changed on your accounts.'
                    ),
                    'icon': 'bi-megaphone',
                    'url': url_for('reports.report_whats_new'),
                },
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
        {
            'title': 'Revenue Analysis',
            'icon': 'bi-currency-dollar',
            'reports': [
                {
                    'id': 'revenue-analyzer',
                    'name': 'Revenue Analyzer',
                    'description': (
                        'Revenue trends, alerts, and drilldowns across all customers. '
                        'Track declining accounts and expansion opportunities.'
                    ),
                    'icon': 'bi-graph-up',
                    'url': url_for('revenue.revenue_dashboard'),
                },
                {
                    'id': 'whitespace',
                    'name': 'Whitespace Analysis',
                    'description': (
                        'Find gaps in customer technology adoption. See which '
                        'customers are missing spend in key buckets and identify '
                        'outreach opportunities.'
                    ),
                    'icon': 'bi-grid-3x3-gap',
                    'url': url_for('reports.report_whitespace'),
                },
                {
                    'id': 'new-synapse-users',
                    'name': 'New Synapse Customers',
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

    today_dt = datetime.combine(today, _time.min)
    quarter_milestones = (
        Milestone.query
        .filter(
            Milestone.on_my_team == True,  # noqa: E712
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Milestone.due_date.isnot(None),
            db.or_(
                # Due this quarter
                db.and_(Milestone.due_date >= quarter_start,
                        Milestone.due_date <= quarter_end),
                # Overdue from before the quarter (still active)
                Milestone.due_date < today_dt,
            ),
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
        milestone_acr_entries = {}  # accumulate id->monthly_usage across customers
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
            milestone_ids = [m.id for m in topic_milestones]
            for m in topic_milestones:
                milestone_acr_entries[m.id] = m.monthly_usage or 0

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
                'milestone_ids': milestone_ids,
            })

        customer_data.sort(key=lambda c: c['acr'], reverse=True)

        workload_groups.append({
            'topic': topic,
            'customer_count': len(customer_data),
            'total_acr': total_acr,
            'customers': customer_data,
            'milestone_acr_entries': milestone_acr_entries,
        })

    workload_groups.sort(key=lambda g: g['customer_count'], reverse=True)

    # Build a deduplicated map of milestone_id -> monthly_usage for the grand total
    # Each milestone may appear in multiple groups (when tagged with multiple topics)
    # so we merge all per-group maps - last write wins, but values are identical since
    # monthly_usage is a property of the milestone, not the topic relationship.
    milestone_acr_map: dict = {}
    for group in workload_groups:
        milestone_acr_map.update(group['milestone_acr_entries'])

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
        milestone_acr_map=milestone_acr_map,
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

    # --- Milestones without engagements (active statuses, all team status) ---
    ms_q = (
        Milestone.query
        .filter(
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


# =============================================================================
# What's New Report
# =============================================================================

@bp.route('/reports/whats-new')
def report_whats_new():
    """What's New report - milestones created or updated in a configurable window."""
    from app.services.seller_mode import get_seller_mode_seller_id

    now = datetime.now(timezone.utc)
    days = request.args.get('days', 14, type=int)
    days = max(1, min(days, 90))  # Clamp to 1-90
    cutoff = now - timedelta(days=days)
    seller_mode_sid = get_seller_mode_seller_id()

    # --- Milestones created in MSX in the window ---
    # Use msx_created_on (from MSX Dataverse) when available, fall back to local created_at
    created_q = (
        Milestone.query
        .filter(
            db.or_(
                Milestone.msx_created_on >= cutoff,
                db.and_(Milestone.msx_created_on.is_(None), Milestone.created_at >= cutoff),
            )
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(db.func.coalesce(Milestone.msx_created_on, Milestone.created_at)))
    )
    created_milestones = created_q.all()

    # --- Milestones modified in MSX in the window (exclude newly created) ---
    # Use msx_modified_on when available, fall back to local updated_at
    updated_q = (
        Milestone.query
        .filter(
            db.or_(
                db.and_(
                    Milestone.msx_modified_on >= cutoff,
                    db.or_(Milestone.msx_created_on < cutoff, Milestone.msx_created_on.is_(None)),
                ),
                db.and_(
                    Milestone.msx_modified_on.is_(None),
                    Milestone.updated_at >= cutoff,
                    Milestone.created_at < cutoff,
                ),
            )
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
        )
        .order_by(desc(db.func.coalesce(Milestone.msx_modified_on, Milestone.updated_at)))
    )
    updated_milestones = updated_q.all()

    # Build seller list for filter (union of sellers from both lists)
    seller_ids_seen = set()
    sellers = []
    areas_seen = set()
    for ms in created_milestones + updated_milestones:
        if ms.customer and ms.customer.seller:
            s = ms.customer.seller
            if s.id not in seller_ids_seen:
                seller_ids_seen.add(s.id)
                sellers.append(s)
        if ms.workload:
            area = ms.workload.split(':', 1)[0].strip() if ':' in ms.workload else ms.workload.strip()
            if area:
                areas_seen.add(area)
    sellers.sort(key=lambda s: s.name)
    areas = sorted(areas_seen)

    # Build audit change-type data for updated milestones
    audit_changes = {}  # milestone_id -> set of field labels
    audit_dates = {}    # milestone_id -> {label: latest_changed_on as ISO string}
    change_types_seen = set()
    if updated_milestones:
        updated_ids = [ms.id for ms in updated_milestones]
        audits = (
            MilestoneAudit.query
            .filter(MilestoneAudit.milestone_id.in_(updated_ids))
            .order_by(desc(MilestoneAudit.changed_on))
            .all()
        )
        for a in audits:
            label = a.field_label
            audit_changes.setdefault(a.milestone_id, set()).add(label)
            change_types_seen.add(label)
            # Track the most recent date per milestone per label
            ms_dates = audit_dates.setdefault(a.milestone_id, {})
            if label not in ms_dates and a.changed_on:
                ms_dates[label] = a.changed_on.strftime('%Y-%m-%dT%H:%M:%S')

    # Convert sets to sorted lists for template
    audit_changes = {k: sorted(v) for k, v in audit_changes.items()}
    change_types = sorted(change_types_seen)

    return render_template(
        'report_whats_new.html',
        created_milestones=created_milestones,
        updated_milestones=updated_milestones,
        sellers=sellers,
        areas=areas,
        seller_mode_sid=seller_mode_sid,
        audit_changes=audit_changes,
        audit_dates=audit_dates,
        change_types=change_types,
        days=days,
    )


# =============================================================================
# Whitespace Report
# =============================================================================

@bp.route('/reports/whitespace')
def report_whitespace():
    """Whitespace report: find gaps in customer technology adoption."""
    has_revenue_data = SyncStatus.is_complete('revenue_import')
    return render_template('report_whitespace.html', has_revenue_data=has_revenue_data)


@bp.route('/api/reports/whitespace')
def api_whitespace_grid():
    """Return whitespace grid data for selected buckets.

    Query params:
        buckets: comma-separated bucket names
        min_revenue: minimum total revenue to count as "has spend" (default 0)

    Returns JSON with:
        customers: list of {id, name, nickname, buckets: {bucket: latest_revenue}}
        buckets: list of selected bucket names
    """
    bucket_param = request.args.get('buckets', '')
    if not bucket_param:
        return jsonify(customers=[], buckets=[])
    selected_buckets = [b.strip() for b in bucket_param.split(',') if b.strip()]
    min_revenue = float(request.args.get('min_revenue', 0))

    # Find the latest month in the data
    latest_month = db.session.query(
        func.max(CustomerRevenueData.month_date)
    ).scalar()
    if not latest_month:
        return jsonify(customers=[], buckets=selected_buckets)

    # Get the trailing 3 months for a more stable view
    trailing_start = latest_month - timedelta(days=62)  # ~2 months back

    # Average revenue per customer per bucket over the trailing 3 months
    spend_query = (
        db.session.query(
            CustomerRevenueData.customer_id,
            CustomerRevenueData.bucket,
            func.avg(CustomerRevenueData.revenue).label('avg_revenue')
        )
        .filter(
            CustomerRevenueData.customer_id.isnot(None),
            CustomerRevenueData.bucket.in_(selected_buckets),
            CustomerRevenueData.month_date >= trailing_start,
        )
        .group_by(CustomerRevenueData.customer_id, CustomerRevenueData.bucket)
        .all()
    )

    # Build customer -> bucket -> avg_revenue mapping
    customer_buckets = {}
    for row in spend_query:
        cid = row.customer_id
        if cid not in customer_buckets:
            customer_buckets[cid] = {}
        if row.avg_revenue and row.avg_revenue > min_revenue:
            customer_buckets[cid][row.bucket] = round(row.avg_revenue, 2)

    # Filter customers based on show_all parameter
    show_all = request.args.get('show_all', '') == '1'
    if show_all:
        # Show all customers that have any spend in selected buckets
        target_ids = [cid for cid, bkts in customer_buckets.items() if bkts]
    else:
        # Only customers with at least one bucket but missing at least one
        target_ids = [
            cid for cid, bkts in customer_buckets.items()
            if 0 < len(bkts) < len(selected_buckets)
        ]

    if not target_ids:
        return jsonify(customers=[], buckets=selected_buckets)

    # Fetch customer details
    customers = Customer.query.filter(
        Customer.id.in_(target_ids)
    ).order_by(Customer.name).all()

    result = []
    for c in customers:
        result.append({
            'id': c.id,
            'name': c.name,
            'nickname': c.nickname,
            'buckets': customer_buckets.get(c.id, {}),
        })

    return jsonify(customers=result, buckets=selected_buckets)


@bp.route('/api/reports/whitespace/reverse/<int:customer_id>')
def api_whitespace_reverse(customer_id: int):
    """Return buckets and products a customer does NOT have spend in.

    Returns JSON with:
        customer: {id, name}
        missing_buckets: list of bucket names with zero spend
        missing_products: {bucket: [product_names]} for buckets they DO use
    """
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify(error='Customer not found'), 404

    # All distinct buckets in the database
    all_buckets = {
        r[0] for r in
        db.session.query(CustomerRevenueData.bucket).distinct().all()
        if r[0]
    }

    # All distinct products per bucket
    all_products_rows = (
        db.session.query(ProductRevenueData.bucket, ProductRevenueData.product)
        .distinct()
        .all()
    )
    all_products_by_bucket = {}
    for bucket, product in all_products_rows:
        if bucket and product:
            all_products_by_bucket.setdefault(bucket, set()).add(product)

    # This customer's buckets (any non-zero spend ever)
    customer_buckets = {
        r[0] for r in
        db.session.query(CustomerRevenueData.bucket)
        .filter(
            CustomerRevenueData.customer_id == customer_id,
            CustomerRevenueData.revenue > 0,
        )
        .distinct()
        .all()
        if r[0]
    }

    # This customer's products per bucket
    customer_products_rows = (
        db.session.query(ProductRevenueData.bucket, ProductRevenueData.product)
        .filter(
            ProductRevenueData.customer_id == customer_id,
            ProductRevenueData.revenue > 0,
        )
        .distinct()
        .all()
    )
    customer_products_by_bucket = {}
    for bucket, product in customer_products_rows:
        if bucket and product:
            customer_products_by_bucket.setdefault(bucket, set()).add(product)

    # Missing buckets
    missing_buckets = sorted(all_buckets - customer_buckets)

    # Missing products within buckets they DO use
    missing_products = {}
    for bucket in customer_buckets:
        all_prods = all_products_by_bucket.get(bucket, set())
        cust_prods = customer_products_by_bucket.get(bucket, set())
        missing = sorted(all_prods - cust_prods)
        if missing:
            missing_products[bucket] = missing

    return jsonify(
        customer={'id': customer.id, 'name': customer.name},
        missing_buckets=missing_buckets,
        missing_products=missing_products,
    )


@bp.route('/api/reports/whitespace/penetration')
def api_whitespace_penetration():
    """Return bucket penetration stats.

    Query params:
        buckets: comma-separated bucket names (optional, defaults to all)

    Returns JSON list of:
        {bucket, customers_with_spend, total_customers, penetration_pct}
    """
    bucket_param = request.args.get('buckets', '')
    selected_buckets = (
        [b.strip() for b in bucket_param.split(',') if b.strip()]
        if bucket_param else None
    )

    # Total customers with any revenue data
    total_customers = (
        db.session.query(func.count(func.distinct(CustomerRevenueData.customer_id)))
        .filter(CustomerRevenueData.customer_id.isnot(None))
        .scalar()
    ) or 0

    if total_customers == 0:
        return jsonify([])

    # Customers with spend per bucket
    query = (
        db.session.query(
            CustomerRevenueData.bucket,
            func.count(func.distinct(CustomerRevenueData.customer_id))
        )
        .filter(
            CustomerRevenueData.customer_id.isnot(None),
            CustomerRevenueData.revenue > 0,
        )
    )
    if selected_buckets:
        query = query.filter(CustomerRevenueData.bucket.in_(selected_buckets))
    bucket_counts = query.group_by(CustomerRevenueData.bucket).all()

    result = []
    for bucket, count in sorted(bucket_counts, key=lambda x: x[0]):
        pct = round(count / total_customers * 100, 1) if total_customers else 0
        result.append({
            'bucket': bucket,
            'customers_with_spend': count,
            'total_customers': total_customers,
            'penetration_pct': pct,
        })

    return jsonify(result)


@bp.route('/api/reports/whitespace/penetration/customers')
def api_whitespace_penetration_customers():
    """Return customers with and without spend in a specific bucket.

    Query params:
        bucket: bucket name (required)

    Returns JSON:
        {bucket, with_spend: [{id, name, nickname}], without_spend: [{id, name, nickname}]}
    """
    bucket = request.args.get('bucket', '').strip()
    if not bucket:
        return jsonify(error='Bucket name is required'), 400

    # All customer IDs with any revenue data
    all_customer_ids = {
        r[0] for r in
        db.session.query(func.distinct(CustomerRevenueData.customer_id))
        .filter(CustomerRevenueData.customer_id.isnot(None))
        .all()
    }

    # Customer IDs with spend > 0 in this bucket
    with_spend_ids = {
        r[0] for r in
        db.session.query(func.distinct(CustomerRevenueData.customer_id))
        .filter(
            CustomerRevenueData.customer_id.isnot(None),
            CustomerRevenueData.bucket == bucket,
            CustomerRevenueData.revenue > 0,
        )
        .all()
    }

    without_spend_ids = all_customer_ids - with_spend_ids

    # Fetch customer details
    with_spend = (
        Customer.query.filter(Customer.id.in_(with_spend_ids))
        .order_by(Customer.name).all()
    ) if with_spend_ids else []
    without_spend = (
        Customer.query.filter(Customer.id.in_(without_spend_ids))
        .order_by(Customer.name).all()
    ) if without_spend_ids else []

    return jsonify(
        bucket=bucket,
        with_spend=[
            {'id': c.id, 'name': c.name, 'nickname': c.nickname}
            for c in with_spend
        ],
        without_spend=[
            {'id': c.id, 'name': c.name, 'nickname': c.nickname}
            for c in without_spend
        ],
    )
