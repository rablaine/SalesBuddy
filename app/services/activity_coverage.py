"""Meeting-to-MSX activity coverage workflow."""
from __future__ import annotations

import logging
import json
import os
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from flask import Flask
from sqlalchemy import case

from app.models import (
    ActivityCoveragePopulation,
    CaipActivity,
    Customer,
    Job,
    Milestone,
    MilestoneCoverageDraft,
    MsxTask,
    PrefetchedMeeting,
    db,
)
from app.services.job_queue import enqueue, job_handler
from app.services.msx_api import HOK_TASK_CATEGORIES, TASK_CATEGORIES

logger = logging.getLogger(__name__)

CUSTOMER_ENGAGEMENT = 861980000
INTERNAL = 861980012
_CATEGORY_VALUES = {item['value'] for item in TASK_CATEGORIES}
_CATEGORY_NAMES = {item['value']: item['label'] for item in TASK_CATEGORIES}


def milestone_picker_order() -> tuple[Any, ...]:
    """Return milestone picker order used by meeting preparation."""
    active_status = case(
        (Milestone.msx_status.in_({'On Track', 'At Risk', 'Blocked'}), 0),
        else_=1,
    )
    return (
        Milestone.on_my_team.desc(),
        active_status.asc(),
        Milestone.due_date.desc(),
        Milestone.title.asc(),
    )
_DATE_SYNC_ATTEMPTS = 3
_DATE_RETRY_DELAYS = (2, 5)
_POPULATION_JOB_TYPE = 'activity_coverage_population'
_POPULATION_DEDUPE_KEY = 'activity-coverage-population'
_WORKIQ_IMPORT_WORKERS = 5

_create_lock = threading.Lock()
_milestone_create_lock = threading.Lock()
_reconcile_lock = threading.Lock()
_reconcile_state_lock = threading.Lock()
_reconcile_state: dict[str, Any] = {
    'running': False,
    'phase': None,
    'scanned': 0,
    'linked': 0,
    'ambiguous': 0,
    'tasks_created': 0,
    'tasks_updated': 0,
    'error': None,
}


def _force_workiq_in_development() -> bool:
    """Return whether dev calendar imports should bypass Outlook."""
    return (
        os.environ.get('FLASK_ENV', '').strip().lower() == 'development'
        and os.environ.get('SALESBUDDY_CALENDAR_SOURCE', '').strip().lower()
        == 'workiq'
    )


def _recent_weekdays(today: date, count: int) -> list[date]:
    """Return the latest weekdays through today in ascending order."""
    dates = []
    current = today
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    return list(reversed(dates))


def _normalized_subject(value: str) -> str:
    """Normalize a meeting or activity subject for conservative comparison."""
    return ' '.join(re.findall(r'[a-z0-9]+', value.lower()))


def _subject_similarity(meeting: PrefetchedMeeting, task: MsxTask) -> float:
    """Return normalized similarity between meeting and activity subjects."""
    meeting_subject = _normalized_subject(meeting.draft_subject or meeting.subject)
    task_subject = _normalized_subject(task.subject)
    if not meeting_subject or not task_subject:
        return 0.0
    return SequenceMatcher(None, meeting_subject, task_subject).ratio()


def _task_activity_date(task: MsxTask) -> date | None:
    """Prefer linked note date because MSX task due dates are often one day later."""
    if task.note and task.note.call_date:
        return task.note.call_date.date()
    return task.due_date.date() if task.due_date else None


def fiscal_year_bounds(reference: date | None = None) -> tuple[date, date]:
    """Return current Microsoft fiscal-year start and end dates."""
    reference = reference or date.today()
    start_year = reference.year if reference.month >= 7 else reference.year - 1
    return date(start_year, 7, 1), date(start_year + 1, 6, 30)


def normalize_week_start(value: str | date | None = None) -> date:
    """Return Monday for a supplied date, defaulting to current week."""
    if isinstance(value, str):
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    else:
        parsed = value or date.today()
    return parsed - timedelta(days=parsed.weekday())


def _default_category(meeting: PrefetchedMeeting) -> int:
    subject = meeting.subject.lower()
    keyword_categories = (
        ('architecture', 861980004),
        ('whiteboard', 606820008),
        ('workshop', 861980001),
        ('demo', 861980002),
        ('proof of concept', 861980005),
        ('poc', 861980005),
        ('briefing', 861980008),
    )
    for keyword, category in keyword_categories:
        if keyword in subject:
            return category
    return (
        CUSTOMER_ENGAGEMENT
        if any(attendee.is_external for attendee in meeting.attendees)
        else INTERNAL
    )


def _default_duration(meeting: PrefetchedMeeting) -> int:
    if meeting.end_time and meeting.end_time > meeting.start_time:
        minutes = int((meeting.end_time - meeting.start_time).total_seconds() / 60)
        return max(1, min(minutes, 1440))
    return 60


def _default_description(meeting: PrefetchedMeeting) -> str:
    attendee_names = [
        attendee.name or attendee.email
        for attendee in meeting.attendees
        if attendee.name or attendee.email
    ]
    lines = [
        f"Customer meeting: {meeting.subject}",
        f"Date: {meeting.meeting_date.isoformat()}",
    ]
    if attendee_names:
        lines.append(f"Attendees: {', '.join(attendee_names)}")
    return '\n'.join(lines)


def _linked_task(meeting: PrefetchedMeeting) -> MsxTask | None:
    if meeting.activity:
        return meeting.activity
    if meeting.note_id and meeting.note:
        return meeting.note.msx_tasks.order_by(MsxTask.created_at.asc()).first()
    return None


def _status(meeting: PrefetchedMeeting, task: MsxTask | None) -> str:
    if task:
        return 'logged'
    if meeting.customer_id is None:
        return 'needs_customer'
    if meeting.milestone_id is None:
        return 'needs_milestone'
    return 'ready'


def _candidate_tasks(meeting: PrefetchedMeeting) -> list[dict[str, Any]]:
    if meeting.customer_id is None:
        return []
    tasks = (
        MsxTask.query.join(Milestone)
        .filter(Milestone.customer_id == meeting.customer_id)
        .filter(MsxTask.meeting_id.is_(None))
        .order_by(MsxTask.created_at.desc())
        .all()
    )
    tasks = [task for task in tasks if _task_activity_date(task) == meeting.meeting_date][:5]
    return [
        {
            'id': task.id,
            'subject': task.subject,
            'category': task.task_category_name,
            'milestone': task.milestone.display_text,
            'url': task.msx_task_url,
            'activity_date': _task_activity_date(task).isoformat(),
            'created_on': (
                task.msx_created_on.date().isoformat()
                if task.msx_created_on else None
            ),
        }
        for task in tasks
    ]


def reconcile_existing_activities(today: date | None = None) -> dict[str, int]:
    """Link unique, high-confidence local MSX activities to fiscal-year meetings."""
    today = today or date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    meetings = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(today, fiscal_end))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .filter(~PrefetchedMeeting.activity.has())
        .all()
    )
    tasks = MsxTask.query.join(Milestone).filter(MsxTask.meeting_id.is_(None)).all()
    meetings_by_customer_date: dict[
        tuple[int, date], list[PrefetchedMeeting]
    ] = defaultdict(list)
    for meeting in meetings:
        if meeting.customer_id:
            meetings_by_customer_date[(meeting.customer_id, meeting.meeting_date)].append(
                meeting
            )

    task_matches: dict[int, list[PrefetchedMeeting]] = {}
    meeting_matches: dict[int, list[MsxTask]] = defaultdict(list)
    for task in tasks:
        activity_date = _task_activity_date(task)
        if not activity_date or not task.milestone:
            continue
        candidates = meetings_by_customer_date.get(
            (task.milestone.customer_id, activity_date),
            [],
        )
        if task.note_id:
            confident = candidates
        else:
            confident = [
                meeting for meeting in candidates
                if meeting.milestone_id == task.milestone_id
                or _subject_similarity(meeting, task) >= 0.68
            ]
        if confident:
            task_matches[task.id] = confident
            for meeting in confident:
                meeting_matches[meeting.id].append(task)

    linked = 0
    ambiguous = 0
    for task in tasks:
        candidates = task_matches.get(task.id, [])
        if len(candidates) != 1:
            ambiguous += int(bool(candidates))
            continue
        meeting = candidates[0]
        if len(meeting_matches[meeting.id]) != 1:
            ambiguous += 1
            continue
        task.meeting_id = meeting.id
        meeting.milestone_id = task.milestone_id
        if task.note_id and meeting.note_id is None:
            meeting.note_id = task.note_id
        linked += 1

    db.session.commit()
    return {'scanned': len(tasks), 'linked': linked, 'ambiguous': ambiguous}


def get_reconciliation_status() -> dict[str, Any]:
    """Return current activity refresh and reconciliation state."""
    with _reconcile_state_lock:
        return dict(_reconcile_state)


def _sync_and_reconcile() -> None:
    """Refresh MSX tasks for known milestones, then reconcile local meetings."""
    from app.services.milestone_sync import (
        _sync_all_tasks,
        _sync_caip_activities,
        _sync_team_milestones,
    )

    with _reconcile_state_lock:
        _reconcile_state['phase'] = 'syncing'
    team_result = _sync_team_milestones()
    if not team_result.get('success'):
        raise RuntimeError(
            team_result.get('error') or 'MSX team milestone sync failed'
        )
    task_sync = _sync_all_tasks()
    try:
        while True:
            next(task_sync)
    except StopIteration as stop:
        sync_result = stop.value
    if not sync_result.get('success'):
        raise RuntimeError(sync_result.get('error') or 'MSX activity sync failed')
    activity_result = _sync_caip_activities()
    if not activity_result.get('success'):
        raise RuntimeError(
            activity_result.get('error') or 'MSX CAIP activity sync failed'
        )

    with _reconcile_state_lock:
        _reconcile_state.update({
            'phase': 'matching',
            'tasks_created': sync_result.get('tasks_created', 0),
            'tasks_updated': sync_result.get('tasks_updated', 0),
        })
    result = reconcile_existing_activities()
    with _reconcile_state_lock:
        _reconcile_state.update(result)


def _reconciliation_worker(app: Flask) -> None:
    """Run activity reconciliation inside an application context."""
    try:
        with app.app_context():
            _sync_and_reconcile()
    except Exception as exc:
        logger.exception('Activity coverage reconciliation failed')
        with _reconcile_state_lock:
            _reconcile_state['error'] = str(exc)
    finally:
        with _reconcile_state_lock:
            _reconcile_state['running'] = False
            _reconcile_state['phase'] = None
        _reconcile_lock.release()


def start_reconciliation(app: Flask) -> bool:
    """Start one background MSX refresh and reconciliation pass."""
    if not _reconcile_lock.acquire(blocking=False):
        return False
    with _reconcile_state_lock:
        _reconcile_state.update({
            'running': True,
            'phase': 'starting',
            'scanned': 0,
            'linked': 0,
            'ambiguous': 0,
            'tasks_created': 0,
            'tasks_updated': 0,
            'error': None,
        })
    thread = threading.Thread(
        target=_reconciliation_worker,
        args=(app,),
        daemon=True,
        name='activity-coverage-reconciliation',
    )
    thread.start()
    return True


def _serialize_meeting(meeting: PrefetchedMeeting) -> dict[str, Any]:
    task = _linked_task(meeting)
    duration_minutes = _default_duration(meeting)
    milestones = []
    if meeting.customer_id:
        milestones = (
            Milestone.query.filter_by(customer_id=meeting.customer_id)
            .filter(Milestone.msx_milestone_id.isnot(None))
            .order_by(*milestone_picker_order())
            .all()
        )
    return {
        'id': meeting.id,
        'subject': meeting.subject,
        'start_time': meeting.start_time,
        'end_time': meeting.end_time,
        'is_all_day': duration_minutes >= 1440,
        'meeting_date': meeting.meeting_date,
        'is_recurring': meeting.is_recurring,
        'customer': meeting.customer,
        'customer_id': meeting.customer_id,
        'matched_via': meeting.matched_via,
        'attendees': meeting.attendees,
        'status': _status(meeting, task),
        'milestone_id': meeting.milestone_id,
        'selected_milestone': meeting.selected_milestone,
        'milestones': milestones,
        'draft_subject': meeting.draft_subject or meeting.subject,
        'draft_description': (
            meeting.draft_description
            if meeting.draft_description is not None
            else _default_description(meeting)
        ),
        'draft_task_category': meeting.draft_task_category or _default_category(meeting),
        'draft_duration_minutes': (
            meeting.draft_duration_minutes or duration_minutes
        ),
        'activity': task,
        'candidate_tasks': [] if task else _candidate_tasks(meeting),
        'note_id': meeting.note_id,
        'enrichment_status': meeting.enrichment_status,
        'enrichment_summary': meeting.enrichment_summary,
        'enrichment_error': meeting.enrichment_error,
        'enriched_at': meeting.enriched_at,
        'suggested_milestone': meeting.suggested_milestone,
        'milestone_match_reason': meeting.milestone_match_reason,
    }


def get_report_data(
    week_start: date | None = None,
    view_all: bool = False,
    milestone_id: int | None = None,
) -> dict[str, Any]:
    """Return weekly or full-fiscal-year meetings plus coverage totals."""
    today = date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    first_week = normalize_week_start(fiscal_start)
    current_week = normalize_week_start(today)
    requested_week = normalize_week_start(week_start)
    selected_start = max(first_week, min(requested_week, current_week))
    selected_end = selected_start + timedelta(days=6)

    visible_start = fiscal_start if view_all else max(selected_start, fiscal_start)
    visible_end = min(today, fiscal_end) if view_all else min(selected_end, today, fiscal_end)
    visible_query = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= visible_start)
        .filter(PrefetchedMeeting.meeting_date <= visible_end)
        .filter(PrefetchedMeeting.dismissed.is_(False))
    )
    if milestone_id:
        visible_query = visible_query.filter(PrefetchedMeeting.milestone_id == milestone_id)
    visible_rows = visible_query.order_by(PrefetchedMeeting.start_time.asc()).all()
    fiscal_rows = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(fiscal_end, today))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .all()
    )
    statuses = [_status(row, _linked_task(row)) for row in fiscal_rows]
    logged = statuses.count('logged')
    total = len(statuses)
    return {
        'meetings': [_serialize_meeting(row) for row in visible_rows],
        'view_all': view_all,
        'milestone_filter': db.session.get(Milestone, milestone_id) if milestone_id else None,
        'week_start': selected_start,
        'week_end': selected_end,
        'previous_week': selected_start - timedelta(days=7),
        'next_week': selected_start + timedelta(days=7),
        'can_go_previous': selected_start > first_week,
        'can_go_next': selected_start < current_week,
        'today': today,
        'fiscal_start': fiscal_start,
        'fiscal_end': fiscal_end,
        'fiscal_year_label': f'FY{fiscal_end.year % 100:02d}',
        'summary': {
            'total': total,
            'logged': logged,
            'ready': statuses.count('ready'),
            'needs_attention': total - logged - statuses.count('ready'),
            'coverage_percent': round((logged / total) * 100) if total else 0,
        },
        'customers': Customer.query.order_by(Customer.name.asc()).all(),
        'task_categories': TASK_CATEGORIES,
    }


def _task_coverage_date(task: MsxTask) -> date:
    """Return activity date used for fiscal-year milestone coverage."""
    value = task.due_date or task.created_at
    return value.date()


def _default_milestone_draft(milestone: Milestone) -> dict[str, Any]:
    """Return editable defaults for a standalone milestone HoK activity."""
    return {
        'subject': f'{milestone.display_text} - HoK activity',
        'description': '',
        'task_category': 861980004,
        'duration_minutes': 60,
        'scheduled_start': datetime.now(timezone.utc),
    }


def _serialize_milestone_coverage(
    milestone: Milestone,
    fiscal_start: date,
    fiscal_end: date,
    tasks: list[MsxTask],
    meeting_drafts: list[PrefetchedMeeting],
    draft: MilestoneCoverageDraft | None,
) -> dict[str, Any]:
    """Serialize one on-team milestone with current and prior HoK evidence."""
    hok_tasks = sorted(
        (task for task in tasks if task.is_hok),
        key=_task_coverage_date,
        reverse=True,
    )
    current_tasks = [
        task for task in hok_tasks
        if fiscal_start <= _task_coverage_date(task) <= fiscal_end
    ]
    prior_task = next(
        (task for task in hok_tasks if _task_coverage_date(task) < fiscal_start),
        None,
    )
    draft_data = {
        'subject': draft.subject,
        'description': draft.description or '',
        'task_category': draft.task_category,
        'duration_minutes': draft.duration_minutes,
        'scheduled_start': draft.scheduled_start,
    } if draft else _default_milestone_draft(milestone)
    prepared_meetings = [
        {
            'id': meeting.id,
            'customer_id': meeting.customer_id,
            'milestone_id': meeting.milestone_id,
            'meeting_subject': meeting.subject,
            'meeting_date': meeting.meeting_date,
            'start_time': meeting.start_time,
            'activity_subject': meeting.draft_subject or meeting.subject,
            'description': (
                meeting.draft_description
                if meeting.draft_description is not None
                else _default_description(meeting)
            ),
            'task_category': meeting.draft_task_category or _default_category(meeting),
            'task_category_name': _CATEGORY_NAMES[
                meeting.draft_task_category or _default_category(meeting)
            ],
            'is_hok': (
                meeting.draft_task_category or _default_category(meeting)
            ) in HOK_TASK_CATEGORIES,
            'duration_minutes': (
                meeting.draft_duration_minutes or _default_duration(meeting)
            ),
        }
        for meeting in meeting_drafts
        if meeting.enrichment_status == 'complete'
    ]
    return {
        'id': milestone.id,
        'milestone': milestone,
        'covered': bool(current_tasks),
        'current_task': current_tasks[0] if current_tasks else None,
        'prior_task': prior_task,
        'prior_fiscal_year': (
            fiscal_year_bounds(_task_coverage_date(prior_task))[1].year % 100
            if prior_task else None
        ),
        'meeting_draft_count': len(meeting_drafts),
        'prepared_meeting_count': sum(
            meeting.enrichment_status == 'complete' for meeting in meeting_drafts
        ),
        'prepared_meetings': prepared_meetings,
        'draft': draft_data,
    }


def get_milestone_coverage_data(
    include_covered: bool = False,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return current-FY HoK coverage for locally cached on-team milestones."""
    today = date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    milestones = (
        Milestone.query
        .filter(Milestone.on_my_team.is_(True))
        .filter(Milestone.msx_milestone_id.isnot(None))
        .filter(Milestone.customer_id.isnot(None))
        .order_by(
            Milestone.due_date.is_(None),
            Milestone.due_date.asc(),
            Milestone.title.asc(),
        )
        .all()
    )
    milestone_ids = [item.id for item in milestones]
    tasks_by_milestone: dict[int, list[MsxTask]] = defaultdict(list)
    for task in MsxTask.query.filter(MsxTask.milestone_id.in_(milestone_ids)).all():
        tasks_by_milestone[task.milestone_id].append(task)
    meetings_by_milestone: dict[int, list[PrefetchedMeeting]] = defaultdict(list)
    meeting_rows = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.milestone_id.in_(milestone_ids))
        .filter(~PrefetchedMeeting.activity.has())
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .all()
    )
    for meeting in meeting_rows:
        if _linked_task(meeting) is None:
            meetings_by_milestone[meeting.milestone_id].append(meeting)
    drafts_by_milestone = {
        draft.milestone_id: draft
        for draft in MilestoneCoverageDraft.query.filter(
            MilestoneCoverageDraft.milestone_id.in_(milestone_ids)
        ).all()
    }
    all_rows = [
        _serialize_milestone_coverage(
            item,
            fiscal_start,
            fiscal_end,
            tasks_by_milestone[item.id],
            meetings_by_milestone[item.id],
            drafts_by_milestone.get(item.id),
        )
        for item in milestones
    ]
    active_rows = [row for row in all_rows if row['milestone'].is_active]
    rows = all_rows if include_inactive else active_rows
    if not include_covered:
        rows = [row for row in rows if not row['covered']]
    covered_active = sum(row['covered'] for row in active_rows)
    return {
        'milestone_rows': rows,
        'include_covered': include_covered,
        'include_inactive': include_inactive,
        'milestone_summary': {
            'active_total': len(active_rows),
            'covered': covered_active,
            'uncovered': len(active_rows) - covered_active,
            'coverage_percent': (
                round((covered_active / len(active_rows)) * 100)
                if active_rows else 0
            ),
        },
        'hok_task_categories': [
            item for item in TASK_CATEGORIES if item['value'] in HOK_TASK_CATEGORIES
        ],
        'task_categories': TASK_CATEGORIES,
        'fiscal_start': fiscal_start,
        'fiscal_end': fiscal_end,
        'fiscal_year_label': f'FY{fiscal_end.year % 100:02d}',
        'today': today,
    }


def _target_fiscal_year(due_date: datetime | None) -> str:
    """Return Microsoft fiscal-year label for a milestone target date."""
    if not due_date:
        return 'No target FY'
    _, fiscal_end = fiscal_year_bounds(due_date.date())
    return f'FY{fiscal_end.year % 100:02d}'


def get_caip_coverage_data() -> dict[str, Any]:
    """Return CAIP activity and HoK coverage for all qualifying team milestones."""
    today = date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    milestones = (
        Milestone.query
        .filter(Milestone.on_my_team.is_(True))
        .filter(Milestone.msx_milestone_id.isnot(None))
        .filter(db.func.lower(Milestone.milestone_category).in_({
            'poc/pilot',
            'production',
        }))
        .order_by(
            Milestone.due_date.is_(None),
            Milestone.due_date.desc(),
            Milestone.title.asc(),
        )
        .all()
    )
    milestone_ids = [milestone.id for milestone in milestones]
    activity_ids = {
        activity.milestone_id
        for activity in CaipActivity.query.filter(
            CaipActivity.milestone_id.in_(milestone_ids)
        ).all()
    }
    tasks_by_milestone: dict[int, list[MsxTask]] = defaultdict(list)
    for task in MsxTask.query.filter(
        MsxTask.milestone_id.in_(milestone_ids),
        MsxTask.is_hok.is_(True),
        MsxTask.statecode == 1,
        MsxTask.due_date.isnot(None),
        MsxTask.actual_end.isnot(None),
    ).all():
        if fiscal_start <= task.actual_end.date() <= min(fiscal_end, today):
            tasks_by_milestone[task.milestone_id].append(task)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = []
    for milestone in milestones:
        hok_tasks = sorted(
            tasks_by_milestone[milestone.id],
            key=lambda task: task.actual_end,
            reverse=True,
        )
        row = {
            'id': milestone.id,
            'milestone': milestone,
            'target_fy': _target_fiscal_year(milestone.due_date),
            'activity_logged': milestone.id in activity_ids,
            'hok_covered': bool(hok_tasks),
            'hok_task': hok_tasks[0] if hok_tasks else None,
        }
        rows.append(row)
        groups[row['target_fy']].append(row)

    ordered_labels = sorted(
        (label for label in groups if label != 'No target FY'),
        reverse=True,
    )
    if 'No target FY' in groups:
        ordered_labels.append('No target FY')
    activity_count = sum(row['activity_logged'] for row in rows)
    hok_count = sum(row['hok_covered'] for row in rows)
    total = len(rows)
    return {
        'caip_groups': [
            {'label': label, 'rows': groups[label]} for label in ordered_labels
        ],
        'caip_summary': {
            'total': total,
            'activities_logged': activity_count,
            'activities_percent': round(activity_count / total * 100) if total else 0,
            'hok_covered': hok_count,
            'hok_percent': round(hok_count / total * 100) if total else 0,
        },
        'fiscal_start': fiscal_start,
        'fiscal_end': fiscal_end,
        'fiscal_year_label': f'FY{fiscal_end.year % 100:02d}',
        'today': today,
    }


def update_milestone_coverage_draft(
    milestone_id: int,
    data: dict[str, Any],
) -> MilestoneCoverageDraft:
    """Validate and persist one standalone milestone HoK draft."""
    milestone = db.session.get(Milestone, milestone_id)
    if milestone is None or not milestone.on_my_team:
        raise ValueError('On-team milestone not found')
    subject = (data.get('subject') or '').strip()
    description = (data.get('description') or '').strip()
    if not subject:
        raise ValueError('Activity subject is required')
    if not description:
        raise ValueError('Describe the hands-on work performed')
    category = int(data.get('task_category') or 0)
    if category not in HOK_TASK_CATEGORIES:
        raise ValueError('Select a Hands-on-Keyboard activity type')
    duration = int(data.get('duration_minutes') or 0)
    if duration < 1 or duration > 1440:
        raise ValueError('Duration must be between 1 and 1440 minutes')
    try:
        scheduled_start = datetime.fromisoformat(data.get('scheduled_start') or '')
    except ValueError as exc:
        raise ValueError('Valid activity date and time is required') from exc
    if scheduled_start.tzinfo is not None:
        scheduled_start = scheduled_start.astimezone(timezone.utc).replace(tzinfo=None)
    fiscal_start, fiscal_end = fiscal_year_bounds()
    if not fiscal_start <= scheduled_start.date() <= min(fiscal_end, date.today()):
        raise ValueError('Activity date must be within the current fiscal year through today')

    draft = milestone.coverage_draft or MilestoneCoverageDraft(milestone=milestone)
    draft.subject = subject
    draft.description = description
    draft.task_category = category
    draft.duration_minutes = duration
    draft.scheduled_start = scheduled_start
    db.session.add(draft)
    db.session.commit()
    return draft


def _complete_activity(task: MsxTask) -> MsxTask:
    """Complete a created activity in MSX and return its local record."""
    from app.services.msx_api import close_task

    close_result = close_task(task.msx_task_id)
    if not close_result.get('success'):
        raise RuntimeError(
            close_result.get('error') or 'MSX activity completion failed'
        )
    task.statecode = 1
    task.statuscode = 5
    task.actual_end = datetime.now(timezone.utc)
    db.session.commit()
    return task


def create_milestone_hok_activity(milestone_id: int) -> MsxTask:
    """Create and complete one standalone current-FY HoK activity."""
    from app.services.msx_api import create_task

    with _milestone_create_lock:
        milestone = db.session.get(Milestone, milestone_id)
        if milestone is None or not milestone.on_my_team:
            raise ValueError('On-team milestone not found')
        fiscal_start, fiscal_end = fiscal_year_bounds()
        existing = next((
            task for task in milestone.tasks
            if task.is_hok
            and fiscal_start <= _task_coverage_date(task) <= fiscal_end
        ), None)
        if existing:
            return _complete_activity(existing)
        draft = milestone.coverage_draft
        if draft is None:
            raise ValueError('Save the HoK activity draft before creating it')

        scheduled_start = draft.scheduled_start.replace(tzinfo=timezone.utc)
        scheduled_end = scheduled_start + timedelta(minutes=draft.duration_minutes)
        result = create_task(
            milestone_id=milestone.msx_milestone_id,
            subject=draft.subject,
            task_category=draft.task_category,
            duration_minutes=draft.duration_minutes,
            description=draft.description,
            start_date=scheduled_start.isoformat(),
            due_date=scheduled_end.isoformat(),
        )
        if not result.get('success'):
            raise RuntimeError(result.get('error') or 'MSX activity creation failed')
        task = MsxTask(
            msx_task_id=result['task_id'],
            msx_task_url=result.get('task_url', ''),
            subject=draft.subject,
            description=draft.description,
            task_category=draft.task_category,
            task_category_name=_CATEGORY_NAMES[draft.task_category],
            duration_minutes=draft.duration_minutes,
            is_hok=True,
            due_date=scheduled_end.replace(tzinfo=None),
            msx_created_on=datetime.now(timezone.utc),
            milestone_id=milestone.id,
        )
        db.session.add(task)
        db.session.delete(draft)
        db.session.commit()
        return _complete_activity(task)


def update_meeting_draft(meeting_id: int, data: dict[str, Any]) -> PrefetchedMeeting:
    """Validate and persist editable matching and activity draft fields."""
    meeting = db.session.get(PrefetchedMeeting, meeting_id)
    if meeting is None:
        raise ValueError('Meeting not found')
    if _linked_task(meeting):
        raise ValueError('Meeting already has an MSX activity')

    customer_id = int(data['customer_id']) if data.get('customer_id') else None
    if customer_id and db.session.get(Customer, customer_id) is None:
        raise ValueError('Customer not found')

    milestone_id = data.get('milestone_id')
    milestone = db.session.get(Milestone, int(milestone_id)) if milestone_id else None
    if milestone and milestone.customer_id != customer_id:
        raise ValueError('Milestone does not belong to selected customer')
    meeting.customer_id = customer_id
    meeting.milestone_id = milestone.id if milestone else None

    subject = (data.get('subject') or '').strip()
    if not subject:
        raise ValueError('Activity subject is required')
    meeting.draft_subject = subject
    meeting.draft_description = (data.get('description') or '').strip()

    category = int(data.get('task_category') or 0)
    if category not in _CATEGORY_VALUES:
        raise ValueError('Valid activity type is required')
    meeting.draft_task_category = category

    duration = int(data.get('duration_minutes') or 0)
    if duration < 1 or duration > 1440:
        raise ValueError('Duration must be between 1 and 1440 minutes')
    meeting.draft_duration_minutes = duration
    db.session.commit()
    return meeting


def link_existing_activity(meeting_id: int, task_id: int) -> MsxTask:
    """Confirm an imported MSX task as coverage for a meeting."""
    meeting = db.session.get(PrefetchedMeeting, meeting_id)
    task = db.session.get(MsxTask, task_id)
    if meeting is None or task is None:
        raise ValueError('Meeting or activity not found')
    if _linked_task(meeting):
        raise ValueError('Meeting already has an MSX activity')
    if task.meeting_id and task.meeting_id != meeting.id:
        raise ValueError('Activity is already linked to another meeting')
    if meeting.customer_id and task.milestone.customer_id != meeting.customer_id:
        raise ValueError('Activity belongs to a different customer')
    task.meeting_id = meeting.id
    meeting.milestone_id = task.milestone_id
    db.session.commit()
    return task


def create_meeting_activity(meeting_id: int) -> MsxTask:
    """Create and complete one MSX activity from a saved meeting draft."""
    from app.services.msx_api import create_task

    with _create_lock:
        meeting = db.session.get(PrefetchedMeeting, meeting_id)
        if meeting is None:
            raise ValueError('Meeting not found')
        existing = _linked_task(meeting)
        if existing:
            return _complete_activity(existing)
        if not meeting.milestone_id or not meeting.selected_milestone:
            raise ValueError('Select a milestone before creating activity')
        if not meeting.selected_milestone.msx_milestone_id:
            raise ValueError('Selected milestone has no MSX ID')

        subject = meeting.draft_subject or meeting.subject
        description = (
            meeting.draft_description
            if meeting.draft_description is not None
            else _default_description(meeting)
        )
        category = meeting.draft_task_category or _default_category(meeting)
        duration = meeting.draft_duration_minutes or _default_duration(meeting)
        scheduled_start = meeting.start_time
        if scheduled_start.tzinfo is None:
            scheduled_start = scheduled_start.replace(tzinfo=timezone.utc)
        scheduled_end = scheduled_start + timedelta(minutes=duration)
        result = create_task(
            milestone_id=meeting.selected_milestone.msx_milestone_id,
            subject=subject,
            task_category=category,
            duration_minutes=duration,
            description=description or None,
            start_date=scheduled_start.isoformat(),
            due_date=scheduled_end.isoformat(),
        )
        if not result.get('success'):
            raise RuntimeError(result.get('error') or 'MSX activity creation failed')

        task = MsxTask(
            msx_task_id=result['task_id'],
            msx_task_url=result.get('task_url', ''),
            subject=subject,
            description=description or None,
            task_category=category,
            task_category_name=_CATEGORY_NAMES[category],
            duration_minutes=duration,
            is_hok=category in HOK_TASK_CATEGORIES,
            due_date=scheduled_end,
            msx_created_on=datetime.now(timezone.utc),
            note_id=meeting.note_id,
            meeting_id=meeting.id,
            milestone_id=meeting.milestone_id,
        )
        db.session.add(task)
        db.session.commit()
        return _complete_activity(task)


def _population_dates(
    populated_through: date | None,
    today: date,
) -> list[date]:
    """Return unpopulated fiscal-year weekdays through today."""
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    start = fiscal_start
    if populated_through and populated_through >= fiscal_start:
        start = populated_through + timedelta(days=1)
    end = min(today, fiscal_end)
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _population_row(today: date, create: bool = False) -> ActivityCoveragePopulation | None:
    """Return current-FY checkpoint, resetting stale prior-FY state when requested."""
    _, fiscal_end = fiscal_year_bounds(today)
    row = db.session.get(ActivityCoveragePopulation, 1)
    if row and row.fiscal_year_end != fiscal_end.year:
        if not create:
            return None
        row.fiscal_year_end = fiscal_end.year
        row.populated_through = None
        row.last_started_at = None
        row.last_completed_at = None
        row.last_error = None
    elif row is None and create:
        row = ActivityCoveragePopulation(id=1, fiscal_year_end=fiscal_end.year)
        db.session.add(row)
    return row


def get_population_status(today: date | None = None) -> dict[str, Any]:
    """Return durable checkpoint plus live fiscal population progress."""
    today = today or date.today()
    row = _population_row(today)
    populated_through = row.populated_through if row else None
    pending = _population_dates(populated_through, today)
    _, fiscal_end = fiscal_year_bounds(today)
    active_job = _active_population_job()
    job_dates: list[date] = []
    if active_job and active_job.payload:
        try:
            payload = json.loads(active_job.payload)
            job_dates = [date.fromisoformat(value) for value in payload.get('dates', [])]
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning('Could not parse calendar import job %s payload', active_job.id)
    completed_count = sum(
        populated_through is not None and value <= populated_through
        for value in job_dates
    )
    current_date = next(
        (value for value in job_dates if populated_through is None or value > populated_through),
        None,
    )

    if active_job:
        label = 'Populating'
        detail = f'{completed_count} of {len(job_dates)} days'
    elif populated_through is None:
        label = f'Import FY{fiscal_end.year % 100:02d} Calendar'
        detail = f'{len(pending)} weekdays through today'
    elif pending:
        label = 'Retry Calendar Import' if row and row.last_error else 'Catch Up Calendar'
        detail = (
            row.last_error
            if row and row.last_error
            else f'Since {populated_through.strftime("%b %d")} · {len(pending)} weekdays'
        )
    elif _force_workiq_in_development():
        label = 'Test WorkIQ Import'
        detail = f'Re-import latest {_WORKIQ_IMPORT_WORKERS} weekdays'
    else:
        label = 'Up to date'
        detail = f'Through {populated_through.strftime("%b %d")}'

    return {
        'running': bool(active_job),
        'current_date': current_date.isoformat() if current_date else None,
        'completed_count': completed_count,
        'total_dates': len(job_dates),
        'attempt': active_job.attempts if active_job else 0,
        'retrying': bool(active_job and active_job.attempts > 1),
        'label': label,
        'detail': detail,
        'can_start': (
            bool(pending) or _force_workiq_in_development()
        ) and not active_job,
        'populated_through': populated_through.isoformat() if populated_through else None,
        'last_completed_at': row.last_completed_at.isoformat() if row and row.last_completed_at else None,
        'pending_count': len(pending),
        'error': row.last_error if row else None,
    }


def _active_population_job() -> Job | None:
    """Return pending or running durable calendar population job."""
    return (
        Job.query
        .filter(Job.job_type == _POPULATION_JOB_TYPE)
        .filter(Job.status.in_([Job.STATUS_PENDING, Job.STATUS_RUNNING]))
        .order_by(Job.id.desc())
        .first()
    )


def _wait_before_retry(seconds: int) -> None:
    """Wait between source retries without busy-spinning."""
    threading.Event().wait(seconds)


def _sync_date_with_retries(target_str: str) -> str | None:
    """Sync one calendar date, returning the final error after retries."""
    from app.services.meeting_sync import sync_meetings_for_date

    last_error = None
    for attempt in range(1, _DATE_SYNC_ATTEMPTS + 1):
        try:
            _, last_error = sync_meetings_for_date(target_str)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'Activity coverage sync attempt %d failed for %s',
                attempt, target_str,
            )
            last_error = str(exc) or exc.__class__.__name__
        if not last_error:
            return None
        logger.warning(
            'Activity coverage sync attempt %d/%d failed for %s: %s',
            attempt, _DATE_SYNC_ATTEMPTS, target_str, last_error,
        )
        if attempt < _DATE_SYNC_ATTEMPTS:
            _wait_before_retry(_DATE_RETRY_DELAYS[attempt - 1])
    return last_error


def populate_fiscal_year(today: date | None = None) -> dict[str, Any]:
    """Populate fiscal weekdays, pausing safely after retry exhaustion."""
    today = today or date.today()
    row = _population_row(today, create=True)
    dates = _population_dates(row.populated_through, today)
    row.last_started_at = datetime.now(timezone.utc)
    row.last_error = None
    db.session.commit()

    for target in dates:
        target_str = target.isoformat()
        error = _sync_date_with_retries(target_str)
        if error:
            row.last_error = (
                f'Paused at {target_str} after {_DATE_SYNC_ATTEMPTS} attempts: {error}'
            )
            db.session.commit()
            return {
                'completed_count': sum(value < target for value in dates),
                'error': row.last_error,
            }
        row.populated_through = target
        row.last_error = None
        db.session.commit()

    row.last_completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return {'completed_count': len(dates), 'error': None}


def _fetch_workiq_date_with_retries(
    target: date,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch one WorkIQ date with calendar-level transient retries."""
    from app.services.meeting_prefetch import fetch_workiq_meetings_for_date

    last_error = None
    for attempt in range(1, _DATE_SYNC_ATTEMPTS + 1):
        meetings, last_error = fetch_workiq_meetings_for_date(target.isoformat())
        if not last_error:
            return meetings, None
        if attempt < _DATE_SYNC_ATTEMPTS:
            _wait_before_retry(_DATE_RETRY_DELAYS[attempt - 1])
    return None, last_error


def _populate_fiscal_year_from_workiq(today: date) -> dict[str, Any]:
    """Fetch WorkIQ dates five at a time and persist each batch serially."""
    from app.services.meeting_sync import sync_meetings_for_date

    row = _population_row(today, create=True)
    dates = _population_dates(row.populated_through, today)
    row.last_started_at = datetime.now(timezone.utc)
    row.last_error = None
    db.session.commit()
    completed_count = 0

    for offset in range(0, len(dates), _WORKIQ_IMPORT_WORKERS):
        batch = dates[offset:offset + _WORKIQ_IMPORT_WORKERS]
        with ThreadPoolExecutor(max_workers=_WORKIQ_IMPORT_WORKERS) as executor:
            fetched = list(executor.map(_fetch_workiq_date_with_retries, batch))

        for target, (meetings, fetch_error) in zip(batch, fetched):
            if fetch_error or meetings is None:
                row.last_error = (
                    f'Paused at {target.isoformat()} after '
                    f'{_DATE_SYNC_ATTEMPTS} attempts: {fetch_error}'
                )
                db.session.commit()
                raise RuntimeError(row.last_error)

            _, store_error = sync_meetings_for_date(
                target.isoformat(),
                prefetched_meetings=meetings,
            )
            if store_error:
                row.last_error = f'Paused at {target.isoformat()}: {store_error}'
                db.session.commit()
                raise RuntimeError(row.last_error)

            row.populated_through = target
            row.last_error = None
            db.session.commit()
            completed_count += 1

    row.last_completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return {'completed_count': completed_count, 'error': None, 'source': 'workiq'}


@job_handler(_POPULATION_JOB_TYPE)
def process_population_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Run durable calendar population using Outlook or parallel WorkIQ."""
    through = date.fromisoformat(payload['through'])
    from app.services.outlook_calendar import corporate_outlook_available

    force_workiq = _force_workiq_in_development()
    if force_workiq:
        logger.info('Calendar population source forced to WorkIQ in development')
    elif corporate_outlook_available():
        result = populate_fiscal_year(through)
        result['source'] = 'outlook'
        return result
    return _populate_fiscal_year_from_workiq(through)


def start_population(app: Flask) -> bool:
    """Queue one resumable durable fiscal-year population job."""
    status = get_population_status()
    if status['running'] or not status['can_start']:
        return False
    today = date.today()
    row = _population_row(today, create=_force_workiq_in_development())
    if _force_workiq_in_development() and not _population_dates(
        row.populated_through if row else None,
        today,
    ):
        test_dates = _recent_weekdays(today, _WORKIQ_IMPORT_WORKERS)
        row.populated_through = test_dates[0] - timedelta(days=1)
        row.last_completed_at = None
        row.last_error = None
        db.session.commit()
    dates = _population_dates(row.populated_through if row else None, today)
    enqueue(
        _POPULATION_JOB_TYPE,
        {
            'through': today.isoformat(),
            'dates': [value.isoformat() for value in dates],
        },
        dedupe_key=_POPULATION_DEDUPE_KEY,
        max_attempts=3,
    )
    return True