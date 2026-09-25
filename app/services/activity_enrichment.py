"""Durable WorkIQ enrichment and milestone matching for activity coverage."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any

from app.gateway_client import gateway_call
from app.models import Job, Milestone, PrefetchedMeeting, SyncStatus, UserPreference, db
from app.services.activity_coverage import fiscal_year_bounds
from app.services.job_queue import enqueue, job_handler
from app.services.msx_api import HVA_TASK_CATEGORIES
from app.services.workiq_service import get_meeting_summary

logger = logging.getLogger(__name__)

JOB_TYPE = 'activity_coverage_enrichment'
JOB_DEDUPE_KEY = 'activity-coverage-enrichment'
MAX_WORKERS = 5
ACCOUNT_SYNC_VERSION = 2

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_COMPLETE = 'complete'
STATUS_FAILED = 'failed'

_CATEGORY_KEYWORDS = (
    (('technical close', 'win plan', 'close plan'), 606820005),
    (('rapid prototype', 'prototype'), 606820006),
    (('whiteboard', 'solution design'), 606820008),
    (('architecture', 'design session'), 861980004),
    (('l300', 'level 300'), 606820009),
    (('technical workshop',), 606820007),
    (('workshop', 'training', 'enablement'), 861980001),
    (('proof of concept', 'poc', 'pilot'), 861980005),
    (('demo',), 861980002),
    (('escalat', 'blocker'), 861980006),
    (('consumption', 'adoption'), 861980007),
    (('assessment',), 861980014),
    (('rfp', 'rfi'), 861980009),
)
_FALLBACK_CATEGORY_KEYWORDS = (
    (('briefing', 'discovery', 'overview', 'kickoff', 'update'), 861980008),
    (('pricing', 'negotiate'), 861980003),
    (('support case', 'technical support', 'tech support'), 606820004),
    (('partner request',), 861980011),
    (('post sales', 'post-sales'), 606820003),
    (('internal sync', 'internal meeting'), 861980012),
)
_DEFAULT_CATEGORY = 861980000


def _category_for_text(text: str) -> int:
    """Prefer an HVA category, then use the closest non-HVA fallback."""
    lowered = text.lower()
    for keywords, category in _CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            assert category in HVA_TASK_CATEGORIES
            return category
    for keywords, category in _FALLBACK_CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return _DEFAULT_CATEGORY


def _milestone_payload(milestone: Milestone) -> dict[str, Any]:
    """Serialize a local milestone for external matching without ORM access."""
    return {
        'local_id': milestone.id,
        'id': milestone.msx_milestone_id,
        'name': milestone.display_text,
        'status': milestone.msx_status or '',
        'opportunity': milestone.opportunity.name if milestone.opportunity else '',
        'workload': milestone.workload or '',
        'on_my_team': bool(milestone.on_my_team),
    }


def _match_milestone(summary: str, milestones: list[dict[str, Any]]) -> dict[str, Any]:
    """Match on-team milestones first, then allow an off-team fallback."""
    for candidates in (
        [item for item in milestones if item['on_my_team']],
        [item for item in milestones if not item['on_my_team']],
    ):
        if not candidates:
            continue
        result = gateway_call('/v1/match-milestone', {
            'call_notes': summary[:3000],
            'milestones': [
                {
                    'id': item['id'],
                    'name': item['name'],
                    'status': item['status'],
                    'opportunity': item['opportunity'],
                    'workload': item['workload'],
                }
                for item in candidates
            ],
        })
        matched_msx_id = result.get('milestone_id')
        if not matched_msx_id:
            continue
        matched = next(
            (item for item in candidates if item['id'] == matched_msx_id),
            None,
        )
        if matched:
            return {
                'milestone_id': matched['local_id'],
                'reason': result.get('reason', ''),
                'on_my_team': matched['on_my_team'],
            }
    return {
        'milestone_id': None,
        'reason': 'No customer milestone was a strong content match.',
        'on_my_team': False,
    }


def _enrich_external(payload: dict[str, Any]) -> dict[str, Any]:
    """Run WorkIQ and gateway calls without touching the database session."""
    summary_data = get_meeting_summary(
        payload['subject'],
        payload['meeting_date'],
        custom_prompt=payload.get('custom_prompt'),
        extract_impact=False,
    )
    summary = (summary_data.get('summary') or '').strip()
    retry_suggested = bool(summary_data.get('retry_suggested'))
    unusable = (
        not summary
        or summary.startswith('Error')
        or summary.startswith('Summary request timed out')
    )
    if unusable and payload['attempt'] < 2:
        raise RuntimeError('WorkIQ did not return a usable meeting summary')

    if unusable:
        summary = (
            f"Transcript summary unavailable. Meeting: {payload['subject']} "
            f"on {payload['meeting_date']}."
        )
    task_subject = (summary_data.get('task_subject') or '').strip()
    task_description = (summary_data.get('task_description') or '').strip()
    if not task_subject:
        task_subject = payload['subject'][:500]
    if not task_description:
        task_description = summary

    match_context = '\n\n'.join([
        f"Meeting: {payload['subject']}",
        summary,
        f"Suggested follow-up: {task_subject}. {task_description}",
    ])
    match = _match_milestone(match_context, payload['milestones'])
    category = _category_for_text(
        f"{payload['subject']} {summary} {task_subject} {task_description}"
    )
    return {
        'summary': summary,
        'task_subject': task_subject,
        'task_description': task_description,
        'task_category': category,
        'milestone_id': match['milestone_id'],
        'match_reason': match['reason'],
        'matched_on_team': match['on_my_team'],
        'used_fallback': unusable or retry_suggested,
    }


def _active_job() -> Job | None:
    """Return an unfinished enrichment job, if one exists."""
    return (
        Job.query
        .filter(Job.job_type == JOB_TYPE)
        .filter(Job.status.in_([Job.STATUS_PENDING, Job.STATUS_RUNNING]))
        .order_by(Job.id.desc())
        .first()
    )


def _account_sync_is_current() -> bool:
    """Return whether improved account discovery completed successfully."""
    status = SyncStatus.query.filter_by(sync_type='accounts').first()
    if not status or not SyncStatus.is_complete('accounts') or not status.details:
        return False
    try:
        details = json.loads(status.details)
    except (TypeError, json.JSONDecodeError):
        return False
    return details.get('sync_version') == ACCOUNT_SYNC_VERSION


def _ensure_current_account_sync() -> bool:
    """Run improved account discovery once before refreshing milestones."""
    if _account_sync_is_current():
        return False

    from app.routes.msx import run_account_sync_headless

    run_account_sync_headless()
    if not _account_sync_is_current():
        raise RuntimeError('Improved MSX account sync did not complete')
    return True


def _refresh_local_milestones() -> dict[str, Any]:
    """Refresh the batched local MSX cache before building match candidates."""
    from app.services.milestone_sync import sync_all_customer_milestones_stream

    completed = None
    for event in sync_all_customer_milestones_stream():
        lines = event.splitlines()
        event_type = next(
            (line.removeprefix('event: ') for line in lines if line.startswith('event: ')),
            '',
        )
        data_line = next(
            (line.removeprefix('data: ') for line in lines if line.startswith('data: ')),
            None,
        )
        data = json.loads(data_line) if data_line else {}
        if event_type == 'vpn_blocked':
            raise RuntimeError(data.get('message') or 'MSX milestone refresh was blocked')
        if event_type == 'complete':
            completed = data

    if not completed:
        raise RuntimeError('MSX milestone refresh did not complete')
    if not completed.get('success'):
        raise RuntimeError('MSX milestone refresh failed')
    return completed


def start_enrichment(force: bool = False) -> dict[str, Any]:
    """Queue eligible fiscal-year meetings, optionally replacing prior preparation."""
    active = _active_job()
    if active:
        return {'started': False, 'job_id': active.id, 'queued': 0}

    PrefetchedMeeting.query.filter_by(enrichment_status=STATUS_RUNNING).update({
        PrefetchedMeeting.enrichment_status: STATUS_FAILED,
        PrefetchedMeeting.enrichment_error: 'Previous enrichment was interrupted',
    })
    fiscal_start, fiscal_end = fiscal_year_bounds()
    query = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(date.today(), fiscal_end))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .filter(PrefetchedMeeting.customer_id.isnot(None))
        .filter(~PrefetchedMeeting.activity.has())
    )
    if not force:
        query = query.filter(db.or_(
            PrefetchedMeeting.enrichment_status.is_(None),
            PrefetchedMeeting.enrichment_status == STATUS_FAILED,
        ))
    meetings = query.all()
    for meeting in meetings:
        if force:
            meeting.milestone_id = None
            meeting.draft_subject = None
            meeting.draft_description = None
            meeting.draft_task_category = None
            meeting.enrichment_summary = None
            meeting.suggested_milestone_id = None
            meeting.milestone_match_reason = None
            meeting.enrichment_attempts = 0
            meeting.enriched_at = None
        meeting.enrichment_status = STATUS_QUEUED
        meeting.enrichment_error = None
    db.session.commit()
    if not meetings:
        return {'started': False, 'job_id': None, 'queued': 0}

    try:
        job = enqueue(
            JOB_TYPE,
            {'meeting_ids': [meeting.id for meeting in meetings]},
            dedupe_key=JOB_DEDUPE_KEY,
            max_attempts=3,
        )
    except Exception:
        for meeting in meetings:
            meeting.enrichment_status = STATUS_FAILED
            meeting.enrichment_error = 'Could not queue meeting preparation'
        db.session.commit()
        raise
    return {'started': True, 'job_id': job.id, 'queued': len(meetings)}


def get_enrichment_status() -> dict[str, Any]:
    """Return durable enrichment counts and current queue state."""
    fiscal_start, fiscal_end = fiscal_year_bounds()
    rows = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(date.today(), fiscal_end))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .filter(PrefetchedMeeting.customer_id.isnot(None))
        .filter(~PrefetchedMeeting.activity.has())
        .all()
    )
    counts = {
        status: sum(1 for row in rows if row.enrichment_status == status)
        for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETE, STATUS_FAILED)
    }
    active = _active_job()
    prepared = counts[STATUS_COMPLETE]
    phase = 'idle'
    if active:
        if active.status == Job.STATUS_PENDING:
            phase = 'queued'
        elif not _account_sync_is_current():
            phase = 'syncing_accounts'
        elif counts[STATUS_RUNNING] == 0 and prepared == 0:
            phase = 'refreshing_msx'
        else:
            phase = 'preparing'
    return {
        **counts,
        'total': len(rows),
        'prepared': prepared,
        'remaining': len(rows) - prepared,
        'running_job': bool(active),
        'job_id': active.id if active else None,
        'phase': phase,
    }


@job_handler(JOB_TYPE)
def process_enrichment_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Enrich queued meetings concurrently and persist each completed result."""
    meeting_ids = [int(value) for value in payload.get('meeting_ids', [])]
    PrefetchedMeeting.query.filter(
        PrefetchedMeeting.id.in_(meeting_ids),
        PrefetchedMeeting.enrichment_status == STATUS_RUNNING,
    ).update({
        PrefetchedMeeting.enrichment_status: STATUS_QUEUED,
        PrefetchedMeeting.enrichment_error: 'Resuming interrupted preparation',
    }, synchronize_session=False)
    db.session.commit()
    _ensure_current_account_sync()
    _refresh_local_milestones()
    meetings = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.id.in_(meeting_ids))
        .filter(PrefetchedMeeting.enrichment_status == STATUS_QUEUED)
        .all()
    )
    custom_prompt = None
    preference = UserPreference.query.first()
    if preference and preference.workiq_summary_prompt:
        custom_prompt = preference.workiq_summary_prompt

    work_items: list[dict[str, Any]] = []
    for meeting in meetings:
        milestones = (
            Milestone.query
            .filter(Milestone.customer_id == meeting.customer_id)
            .filter(Milestone.msx_milestone_id.isnot(None))
            .order_by(Milestone.on_my_team.desc(), Milestone.due_date.desc())
            .all()
        )
        meeting.enrichment_status = STATUS_RUNNING
        meeting.enrichment_attempts = (meeting.enrichment_attempts or 0) + 1
        work_items.append({
            'meeting_id': meeting.id,
            'subject': meeting.subject,
            'meeting_date': meeting.meeting_date.isoformat(),
            'attempt': meeting.enrichment_attempts,
            'custom_prompt': custom_prompt,
            'milestones': [_milestone_payload(item) for item in milestones],
        })
    db.session.commit()

    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(work_items) or 1)) as executor:
        futures = {
            executor.submit(_enrich_external, item): item['meeting_id']
            for item in work_items
        }
        for future in as_completed(futures):
            meeting = db.session.get(PrefetchedMeeting, futures[future])
            if meeting is None:
                continue
            try:
                result = future.result()
                meeting.enrichment_summary = result['summary']
                meeting.suggested_milestone_id = result['milestone_id']
                meeting.milestone_match_reason = result['match_reason']
                if meeting.milestone_id is None:
                    meeting.milestone_id = result['milestone_id']
                if meeting.draft_subject is None:
                    meeting.draft_subject = result['task_subject']
                if meeting.draft_description is None:
                    meeting.draft_description = result['task_description']
                if meeting.draft_task_category is None:
                    meeting.draft_task_category = result['task_category']
                meeting.enrichment_status = STATUS_COMPLETE
                meeting.enrichment_error = None
                meeting.enriched_at = datetime.now(timezone.utc)
                completed += 1
            except Exception as exc:
                meeting.enrichment_status = STATUS_FAILED
                meeting.enrichment_error = str(exc)[:2000]
                failed += 1
            db.session.commit()

    return {'total': len(work_items), 'completed': completed, 'failed': failed}