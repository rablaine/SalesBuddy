"""Single admission and observation contract for worker-owned data syncs."""
from __future__ import annotations

import json
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timezone
from typing import Any

import requests
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models import Customer, Job, JobEvent, SyncStatus, UserPreference, db
from app.services import job_queue

logger = logging.getLogger(__name__)
EVENT_LIMIT = 200
ACTIVE_STATES = (Job.STATUS_PENDING, Job.STATUS_RUNNING)


class SyncAdmissionError(ValueError):
    """Invalid options, unmet prerequisites, or a conflicting admitted operation."""

    def __init__(self, message: str, status: int = 400) -> None:
        """Keep the user-facing validation message and its HTTP status."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Operation:
    """Allowlisted operation contract; execution never depends on an HTTP request."""

    name: str
    description: str
    resource: str
    defaults: dict[str, Any]
    needs_customers: bool = False
    recovery_safe: bool = False
    legacy_status: str | None = None


OPERATIONS = {
    'accounts': Operation('Accounts', 'MSX account discovery and territory alignment.',
                          'accounts', {'source': 'default', 'skip_aura': False},
                          legacy_status='accounts'),
    'accounts-selected': Operation('Selected accounts', 'Import a captured account selection.',
                                   'accounts', {'accounts': [], 'territories': []}),
    'milestones': Operation('Milestones', 'All customers, opportunities, tasks and comments.',
                            'milestones', {}, True, legacy_status='milestones'),
    'customer-milestones': Operation('Customer milestones', 'Milestones for one customer.',
                                     'milestones', {'customer_id': None, 'picker_only': False}, True),
    'customer-data': Operation('Customer MSX data', 'Opportunities, milestones and comments.',
                               'milestones', {'customer_id': None}, True),
    'revenue': Operation('ACR', 'MSXI revenue, strategic pillar whitespace and trend analysis.',
                         'revenue', {'fiscal_years': None, 'run_analysis': True}, True,
                         True, 'revenue_sync'),
    'revenue-analysis': Operation('Revenue analysis', 'Recalculate saved revenue trends.',
                                  'revenue', {}, legacy_status='revenue_analysis'),
    'u2c': Operation('U2C', 'Official MSXI baseline and local milestone matching.',
                     'u2c', {'force': True}, legacy_status='u2c_import'),
    'marketing': Operation('Marketing', 'Customer summaries, interactions and contacts.',
                           'marketing', {}, True, legacy_status='marketing'),
    'fabric': Operation('Fabric 360', 'Workloads, capacities, utilization, revenue and P to F renewals.',
                        'fabric', {}, True, True, 'fabric_consumption'),
    'fabric-revenue': Operation('Fabric revenue', 'Refresh only saved Fabric revenue.',
                                'fabric', {}, True, True),
    'sql-motions': Operation('SQL Motions', 'SQL renewals and Arc SQL version discovery.',
                             'sql-motions', {}, True, True, 'sql_motions'),
    'meetings': Operation('Meetings for a date', 'WorkIQ calendar and attendee cache.',
                          'meetings', {'date': None}, recovery_safe=True),
    'meeting-aura': Operation('Meeting sync',
                              'Pulls meeting details for today and the next four business days.',
                              'meetings', {'anchor': None, 'days_ahead': 4},
                              needs_customers=True, recovery_safe=True),
    'copilot-actions': Operation('Copilot actions', 'Refresh suggested daily action items.',
                                 'copilot-actions', {}),
    'favicons': Operation('Customer favicons', 'Refresh icons for configured websites.',
                          'favicons', {'missing_only': False}, recovery_safe=True,
                          legacy_status='favicons'),
    'milestone-dates': Operation('Milestone audit dates', 'Committed and completed MSX dates.',
                                 'milestones', {}),
    'activity-reconciliation': Operation('Activity reconciliation',
                                         'Refresh all owned MSX tasks and match saved meetings.',
                                         'milestones', {}),
    'calendar-population': Operation('Fiscal-year calendar',
                                     'Resume calendar population from its saved checkpoint.',
                                     'meetings', {'through': None}, recovery_safe=True),
    'meeting-preparation': Operation('Meeting preparation',
                                     'WorkIQ summaries and suggested milestone matches.',
                                     'meeting-preparation', {'force': False}),
}


def _iso(value: datetime | None) -> str | None:
    """Serialize SQLite naive UTC timestamps without changing their meaning."""
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else None


def is_transient_failure(error: Exception) -> bool:
    """Recognize typed network failures, including a source wrapper's explicit cause."""
    return any(isinstance(candidate, (requests.Timeout, requests.ConnectionError))
               for candidate in (error, error.__cause__))


def conflicting_job_types(job_type: str) -> list[str]:
    """Describe whole-run conflicts, including account mappings used by other imports."""
    operation = job_type.removeprefix('sync:')
    spec = OPERATIONS[operation]

    def resources(key: str) -> set[str]:
        """Preparation consumes both calendar records and milestone matching state."""
        if key in ('meeting-preparation', 'activity-reconciliation'):
            return {'meetings', 'milestones'}
        if key == 'u2c':
            return {'u2c', 'milestones'}
        return {OPERATIONS[key].resource}

    return [f'sync:{key}' for key, other in OPERATIONS.items()
            if resources(key) & resources(operation) or
            'accounts' in (spec.resource, other.resource)]


def validate_options(operation: str, options: dict | None) -> dict:
    """Capture defaults, validate types and reject unsupported operation options."""
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise SyncAdmissionError('Unknown sync operation.', 404)
    if options is None:
        options = {}
    if not isinstance(options, dict) or options.keys() - spec.defaults.keys():
        raise SyncAdmissionError('Use only the supported options for this sync.')
    result = {**spec.defaults, **options}
    for key in ('force', 'skip_aura', 'run_analysis', 'missing_only', 'picker_only'):
        if key in result and not isinstance(result[key], bool):
            raise SyncAdmissionError(f'{key} must be true or false.')
    for key in ('date', 'anchor', 'through'):
        if key in result:
            value = result[key] or date.today().isoformat()
            try:
                result[key] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError) as exc:
                raise SyncAdmissionError(f'{key} must use YYYY-MM-DD.') from exc
    if 'days_ahead' in result:
        days = result['days_ahead']
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 9:
            raise SyncAdmissionError('days_ahead must be an integer from 0 to 9.')
    if 'source' in result:
        if result['source'] not in ('default', 'alignment', 'accountteams'):
            raise SyncAdmissionError('source must be default, alignment or accountteams.')
        if result['source'] == 'default':
            from app.services.alignment import is_override_active
            result['source'] = 'alignment' if is_override_active() else 'accountteams'
    if 'fiscal_years' in result and result['fiscal_years'] is not None:
        years = result['fiscal_years']
        if (not isinstance(years, list) or not years or len(years) > 10
                or any(not isinstance(y, str) or len(y) != 4
                       or not y.startswith('FY') or not y[2:].isdigit() for y in years)):
            raise SyncAdmissionError('fiscal_years must be a list such as ["FY26", "FY27"].')
        result['fiscal_years'] = sorted(set(years))
    if 'customer_id' in result:
        cid = result['customer_id']
        if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
            raise SyncAdmissionError('Choose a customer_id for this sync.')
        customer = db.session.get(Customer, cid)
        if customer is None:
            raise SyncAdmissionError('Customer not found.', 404)
        if not customer.tpid_url:
            raise SyncAdmissionError('This customer has no MSX account link.')
    if operation == 'accounts-selected':
        if not result['accounts']:
            raise SyncAdmissionError('Provide the accounts to import.')
        for key in ('accounts', 'territories'):
            if not isinstance(result[key], list) or any(
                not isinstance(value, dict) for value in result[key]
            ):
                raise SyncAdmissionError(f'{key} must be a list of account/territory objects.')
    if operation == 'sql-motions':
        from app.services.sql_motions_sync import active_customers
        if not active_customers():
            raise SyncAdmissionError('No active customers with valid TPIDs. Import accounts first.')
    elif operation == 'u2c':
        from app.services.u2c_pull import TERRITORIES_REQUIRED_ERROR, get_territory_codes
        if not get_territory_codes():
            raise SyncAdmissionError(TERRITORIES_REQUIRED_ERROR)
    elif operation in ('fabric', 'fabric-revenue', 'revenue', 'marketing'):
        from app.services.revenue_pull import get_customer_tpids
        if not get_customer_tpids():
            raise SyncAdmissionError('No customers with valid TPIDs. Import your accounts first.')
    elif spec.needs_customers and Customer.query.first() is None:
        raise SyncAdmissionError('Import accounts first.')
    return result


def admit_sync(operation: str, options: dict | None = None, *,
               initiator: str = 'manual', request_key: str | None = None) -> Job:
    """Atomically admit one resource owner, or return its identical existing run."""
    options = validate_options(operation, options)
    if request_key is not None and (
        not isinstance(request_key, str) or not 1 <= len(request_key) <= 200
    ):
        raise SyncAdmissionError('Idempotency-Key must contain 1 to 200 characters.')
    spec = OPERATIONS[operation]
    payload = json.dumps(options, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(f'{operation}:{payload}'.encode('utf-8')).hexdigest()
    active_key = f'sync:{operation}:{digest}'
    job_type = f'sync:{operation}'
    existing_request = Job.query.filter_by(request_key=request_key).first() if request_key else None
    if existing_request is not None:
        if existing_request.job_type != job_type or existing_request.payload != payload:
            raise SyncAdmissionError('This request key belongs to a different sync request.', 409)
        return existing_request
    existing = Job.query.filter(
        Job.job_type == job_type, Job.payload == payload,
        Job.status.in_(ACTIVE_STATES)).first()
    if existing is not None:
        return existing
    job = Job(job_type=job_type, payload=payload, active_key=active_key,
              request_key=request_key, initiator=initiator,
              max_attempts=3 if spec.recovery_safe else 1)
    previous = Job.query.filter_by(job_type=job_type).order_by(Job.id.desc()).first()
    legacy = SyncStatus.query.filter_by(sync_type=spec.legacy_status).first() \
        if spec.legacy_status else None
    job.last_success_at = (
        previous.finished_at if previous and previous.status == Job.STATUS_DONE
        and not json.loads(previous.result or '{}').get('skipped')
        else previous.last_success_at if previous
        else legacy.completed_at if legacy and legacy.success else None)
    db.session.add(job)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = None
        if request_key:
            existing = Job.query.filter_by(request_key=request_key).first()
        if existing is None:
            existing = Job.query.filter_by(active_key=active_key).first()
        if existing is None:
            logger.exception('Could not admit sync %s', operation)
            raise
        if existing.job_type != job_type or existing.payload != payload:
            raise SyncAdmissionError(
                f'A conflicting {spec.resource} job already exists (job {existing.id}).', 409)
        return existing
    logger.info('Admitted %s job %s (%s)', operation, job.id, initiator)
    return job


def publish_progress(data: dict, event_type: str = 'progress') -> None:
    """Publish live progress without competing with SQLite domain writes."""
    job_id = job_queue.CURRENT_JOB_ID.get()
    if job_id is None:
        return
    _write_progress(job_id, data, event_type)


def _write_progress(job_id: int, data: dict, event_type: str) -> None:
    """Save measured progress without committing the domain service's transaction."""
    raw = dict(data)
    percent = raw.get('progress')
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        percent = max(0, min(99, int(percent)))
    else:
        percent = None
    progress = {
        **raw,
        'phase': raw.get('phase') or raw.get('status') or event_type,
        'message': raw.get('message') or raw.get('customer') or 'Sync running.',
        'percent': percent,
        'processed': raw.get('current'),
        'total': raw.get('total'),
    }
    from app.services import sync_runtime
    owner = job_queue.CURRENT_SYNC_OWNER.get()
    if owner is None:
        raise RuntimeError('Only the current sync attempt can publish live progress.')
    sync_runtime.update(owner['identity'], progress=progress, event_type=event_type)


def persist_live_status(job: Job) -> None:
    """Archive bounded live progress/events alongside the terminal durable outcome."""
    if not job.job_type.startswith('sync:'):
        return
    from app.services import sync_runtime
    live = sync_runtime.read(job)
    if live is None:
        return
    job.progress_json = json.dumps(live['progress'])
    job.progress_at = datetime.fromisoformat(live['progress_at']) if live['progress_at'] else None
    job.heartbeat_at = datetime.fromisoformat(live['heartbeat_at'])
    JobEvent.query.filter_by(job_id=job.id).delete(synchronize_session=False)
    for event in live['events']:
        db.session.add(JobEvent(
            job_id=job.id, attempt=event['attempt'], event_type=event['type'],
            data_json=event['data'], created_at=datetime.fromisoformat(event['created_at'])))


def consume_stream(stream) -> dict:
    """Consume a domain stream in the worker and translate terminal errors into failure."""
    terminal = None
    for chunk in stream:
        for block in chunk.strip().split('\n\n'):
            lines = block.splitlines()
            event_type = next((line[6:] for line in lines if line.startswith('event:')),
                              'progress').strip()
            data = json.loads('\n'.join(
                line[5:].lstrip() for line in lines if line.startswith('data:')))
            if event_type in ('error', 'vpn_blocked') or data.get('error'):
                publish_progress(data, event_type)
                if data.get('retryable') is True:
                    raise requests.ConnectionError(data.get('error') or 'Transient network failure.')
                raise job_queue.NonRetryableJobError(
                    data.get('error') or data.get('message') or 'Sync failed.',
                    result={'success': False, **data})
            if event_type == 'complete' or data.get('complete'):
                terminal = data
            else:
                publish_progress(data, event_type)
    if terminal is None:
        raise job_queue.NonRetryableJobError('Sync ended without a terminal result.')
    return terminal


def _execute_operation(operation: str, options: dict) -> dict:
    """Thin operation adapters reuse domain services instead of duplicating sync logic."""
    from flask import current_app
    if operation == 'accounts':
        from app.services.account_sync import sync_accounts_stream
        return consume_stream(sync_accounts_stream(**options))
    if operation == 'accounts-selected':
        from app.services.account_sync import import_selected_accounts
        return import_selected_accounts(**options)
    if operation == 'milestones':
        from app.services.milestone_sync import sync_all_customer_milestones_stream
        return consume_stream(sync_all_customer_milestones_stream())
    if operation in ('customer-milestones', 'customer-data'):
        from app.services.milestone_sync import (
            sync_customer_comments, sync_customer_milestones, sync_customer_opportunities,
        )
        customer = db.session.get(Customer, options['customer_id'])
        if operation == 'customer-milestones' and options.get('picker_only'):
            from app.services.milestone_picker import get_customer_milestone_choices
            publish_progress({'phase': 'fetching', 'message': 'Reading milestones from MSX.'})
            return get_customer_milestone_choices(customer)
        result = {}
        stages = {}
        functions = [sync_customer_milestones]
        if operation == 'customer-data':
            functions = [sync_customer_opportunities, sync_customer_milestones,
                         sync_customer_comments]
        for index, fn in enumerate(functions):
            publish_progress({'phase': fn.__name__, 'current': index,
                              'total': len(functions), 'progress': 100 * index / len(functions)})
            result = fn(customer)
            result = {key: sorted(value) if isinstance(value, set) else value
                      for key, value in result.items()}
            stages[fn.__name__] = result
            if result.get('success') is False:
                return {**result, 'stages': stages}
            publish_progress({'phase': fn.__name__, 'current': index + 1,
                              'total': len(functions),
                              'progress': 100 * (index + 1) / len(functions),
                              'stage_result': result})
        return {**result, 'stages': stages}
    if operation == 'revenue':
        from app.services.revenue_sync import sync_revenue
        return sync_revenue(**options, progress=publish_progress)
    if operation == 'revenue-analysis':
        from app.services.revenue_analysis import run_analysis_streaming
        for update in run_analysis_streaming():
            if update.get('complete'):
                return {'success': True, **update['stats']}
            update['progress'] = 100 * update['current'] / max(update['total'], 1)
            publish_progress(update)
        raise job_queue.NonRetryableJobError('Analysis ended without a result.')
    if operation == 'marketing':
        from app.services.marketing_sync import sync_marketing_stream
        return consume_stream(sync_marketing_stream())
    if operation == 'u2c':
        from app.services.scheduled_sync import run_u2c_import
        return run_u2c_import(**options)
    if operation in ('fabric', 'fabric-revenue'):
        from app.services.fabric_consumption import (
            refresh_fabric_revenue, sync_fabric_360,
        )
        fn = sync_fabric_360 if operation == 'fabric' else refresh_fabric_revenue
        return fn(progress=publish_progress)
    if operation == 'sql-motions':
        from app.services.sql_motions_sync import sync_sql_motions
        try:
            return sync_sql_motions(progress_callback=publish_progress)
        except Exception as exc:
            if is_transient_failure(exc):
                raise requests.ConnectionError('SQL source connection failed; retry is safe.') from exc
            raise
    if operation == 'copilot-actions':
        from app.services.copilot_actions import sync_copilot_action_items
        return sync_copilot_action_items()
    if operation == 'meetings':
        from app.services.meeting_sync import get_cached_meetings, sync_meetings_for_date
        meetings, error = sync_meetings_for_date(options['date'])
        _, synced_at = get_cached_meetings(options['date'])
        return {'success': not bool(error), 'error': error, 'meetings': meetings,
                'date': options['date'], 'from_cache': False, 'synced_at': _iso(synced_at)}
    if operation == 'meeting-aura':
        from app.services.meeting_sync import ensure_meeting_aura
        counts, errors = ensure_meeting_aura(
            start=date.fromisoformat(options['anchor']), days_ahead=options['days_ahead'])
        return {'success': not bool(errors), 'counts': counts, 'errors': errors,
                'error': '; '.join(errors.values()) if errors else None}
    if operation == 'favicons':
        from app.services.favicon_sync import sync_favicons
        return sync_favicons(publish_progress, **options)
    if operation == 'milestone-dates':
        from app.services.milestone_audit import sync_milestone_audit_dates
        result = sync_milestone_audit_dates(progress_callback=lambda current, total:
            publish_progress({'phase': 'audit', 'current': current - 1, 'total': total,
                              'progress': 100 * (current - 1) / max(total, 1),
                              'message': f'Checking milestone audit {current}/{total}.'}))
        return {**result, 'failed': len(result.get('errors', []))}
    if operation == 'activity-reconciliation':
        from app.services.activity_coverage import _sync_and_reconcile
        return _sync_and_reconcile()
    if operation == 'calendar-population':
        from app.services.activity_coverage import process_population_job
        result = process_population_job(options)
        return {'success': not bool(result.get('error')), **result}
    if operation == 'meeting-preparation':
        from app.services.activity_enrichment import prepare_enrichment, process_enrichment_job
        payload = prepare_enrichment(**options)
        result = process_enrichment_job(payload) if payload['meeting_ids'] else {'total': 0}
        return {'success': not bool(result.get('failed')), **result}
    raise job_queue.NonRetryableJobError('No implementation for this operation.')


def _run_operation(operation: str, options: dict) -> dict:
    """Recheck prerequisites and allow retry only for typed, transient, safe failures."""
    try:
        options = validate_options(operation, options)
        publish_progress({'phase': 'starting', 'message': 'Worker started this sync.'}, 'start')
        result = _execute_operation(operation, options)
        if result.get('success') is False:
            if result.get('retryable') is True and OPERATIONS[operation].recovery_safe:
                raise requests.ConnectionError(result.get('error') or 'Transient network failure.')
            raise job_queue.NonRetryableJobError(result.get('error') or
                                                 'Sync reported failed or partial results.',
                                                 result=result)
        if operation == 'milestones':
            from app.services.scheduled_sync import _revenue_sync_due, _u2c_import_due
            for followup, followup_options in (
                ('marketing', {}), ('revenue', {}) if _revenue_sync_due() else (None, {}),
                ('u2c', {'force': False}) if _u2c_import_due() else (None, {}),
            ):
                if followup:
                    try:
                        admit_sync(followup, followup_options, initiator='followup')
                    except SyncAdmissionError as exc:
                        logger.warning('Follow-up %s not admitted: %s', followup, exc)
                        publish_progress({'phase': 'followup', 'message': str(exc)}, 'warning')
        if result.get('failed'):
            result['warnings'] = [f"{result['failed']} unit(s) failed; inspect the result details."]
        return {'success': True, **result}
    except (requests.Timeout, requests.ConnectionError):
        if OPERATIONS[operation].recovery_safe:
            raise
        raise job_queue.NonRetryableJobError('Network error. Start a new sync when ready.')
    except job_queue.NonRetryableJobError:
        raise
    except SQLAlchemyError as exc:
        logger.exception('Could not save %s sync results', operation)
        raise job_queue.NonRetryableJobError(
            'Sync could not save its results. Some earlier changes may already be saved. '
            'Check backend logs before starting a new sync.') from exc
    except Exception as exc:
        raise job_queue.NonRetryableJobError(str(exc)) from exc


def register_handlers() -> None:
    """Register the allowlist in both the web and worker process."""
    for operation in OPERATIONS:
        job_queue.register_handler(
            f'sync:{operation}', lambda options, key=operation: _run_operation(key, options))


def worker_health() -> dict:
    """Distinguish a healthy/busy worker from a missing or stale heartbeat."""
    from app.services import sync_runtime
    live = sync_runtime.worker_state()
    worker = SyncStatus.query.filter_by(sync_type='worker_process').first() if not live else None
    beat = datetime.fromisoformat(live['heartbeat_at']) if live else (
        worker.heartbeat_at if worker else None)
    age = (datetime.now(timezone.utc) - beat.replace(
        tzinfo=beat.tzinfo or timezone.utc)).total_seconds() if beat else None
    alive = age is not None and age < Job.HEARTBEAT_STALE_SECONDS
    return {'alive': alive, 'heartbeat_at': _iso(beat),
            'message': 'Worker healthy. Jobs execute independently of this page.' if alive else
            'Worker unavailable. Queued jobs are saved. A supervised install restarts failed workers. '
            'In development, start or restart scripts\\dev.ps1; review backend logs if it persists.'}


def project_sync_status(sync_type: str) -> dict | None:
    """Keep existing polling surfaces aligned with the durable queue's actual owner."""
    operation = next((key for key, spec in OPERATIONS.items()
                      if spec.legacy_status == sync_type), None)
    if operation is None:
        return None
    family = [f'sync:{key}' for key, spec in OPERATIONS.items()
              if spec.resource == OPERATIONS[operation].resource]
    current_id = job_queue.CURRENT_JOB_ID.get()
    current = db.session.get(Job, current_id) if current_id is not None else None
    if current is not None and current.job_type in family:
        return None
    active = Job.query.filter(
        Job.job_type.in_(family),
        Job.status.in_(ACTIVE_STATES)).order_by(Job.id).all()
    job = next((run for run in active if run.status == Job.STATUS_RUNNING), active[0]) \
        if active else None
    if job is None:
        job = Job.query.filter_by(job_type=f'sync:{operation}').order_by(Job.id.desc()).first()
    if job is None:
        return None
    if job.id == job_queue.CURRENT_JOB_ID.get():
        return None
    snapshot = job_snapshot(job)
    state = {
        Job.STATUS_DONE: 'complete', Job.STATUS_FAILED: 'failed',
        Job.STATUS_RUNNING: 'in_progress', Job.STATUS_PENDING: 'in_progress',
    }[job.status]
    result = snapshot['result'] or {}
    last_success = (job.finished_at if job.status == Job.STATUS_DONE and not result.get('skipped')
                    else job.last_success_at)
    return {
        'state': 'incomplete' if snapshot['state'] == 'interrupted' else state,
        'started_at': job.started_at or job.created_at,
        'completed_at': last_success if result.get('skipped') else job.finished_at,
        'last_success_at': last_success,
        'items_synced': next((result[key] for key in (
            'items_synced', 'synced', 'total_items', 'fetched', 'customer_count') if key in result),
            None),
        'details': json.dumps({**result, **snapshot['progress'],
            'progress': snapshot['progress'].get(
            'percent'), 'error': snapshot['error'], 'job_id': job.id,
            'job_state': snapshot['state']}),
        'job_id': job.id,
        'job': snapshot,
    }


def job_snapshot(job: Job) -> dict:
    """Read actual persisted progress and outcome, never browser-owned state."""
    from app.services import sync_runtime
    live = sync_runtime.read(job)
    heartbeat = datetime.fromisoformat(live['heartbeat_at']) if live else job.heartbeat_at
    progress = live['progress'] if live else json.loads(job.progress_json or '{}')
    blocker = None
    state = job.status
    if state == Job.STATUS_PENDING:
        state = 'retry_waiting' if job.attempts else (
            'queued' if worker_health()['alive'] else 'waiting_for_worker')
        blocking = Job.query.filter(
            Job.id != job.id,
            Job.job_type.in_(conflicting_job_types(job.job_type)),
            db.or_(Job.status == Job.STATUS_RUNNING,
                   db.and_(Job.status == Job.STATUS_PENDING, Job.id < job.id)),
        ).order_by(Job.id).first()
        if blocking:
            state = 'waiting_for_group'
            blocker = {'id': blocking.id,
                       'name': OPERATIONS[blocking.job_type.removeprefix('sync:')].name}
    elif state == Job.STATUS_RUNNING and heartbeat:
        age = (datetime.now(timezone.utc) - heartbeat.replace(
            tzinfo=heartbeat.tzinfo or timezone.utc)).total_seconds()
        if age >= Job.HEARTBEAT_STALE_SECONDS:
            state = 'interrupted'
        elif live and live.get('waiting_for_write'):
            state = 'waiting_for_write'
            blocker = sync_runtime.write_owner()
        elif live and live.get('writing'):
            state = 'saving'
    if job.status == Job.STATUS_DONE:
        result = json.loads(job.result or '{}')
        progress = {**progress, 'phase': 'complete', 'percent': 100,
                    'message': 'Sync completed with warnings.' if result.get('warnings')
                    else 'Sync completed.'}
    return {'id': job.id, 'operation': job.job_type.removeprefix('sync:'), 'state': state,
            'status': job.status, 'options': json.loads(job.payload or '{}'),
            'initiator': job.initiator, 'attempt': job.attempts,
            'max_attempts': job.max_attempts, 'progress': progress,
            'created_at': _iso(job.created_at), 'started_at': _iso(job.started_at),
            'finished_at': _iso(job.finished_at), 'heartbeat_at': _iso(heartbeat),
            'progress_at': live['progress_at'] if live else _iso(job.progress_at),
            'blocker': blocker, 'retry_at': _iso(job.available_at),
            'error': job.last_error, 'result': json.loads(job.result) if job.result else None}


def operation_schedules() -> dict[str, dict[str, Any]]:
    """Describe actual automatic triggers without assigning times or admitting work."""
    from app.services.copilot_actions import SYNC_HOUR as copilot_hour
    from app.services.meeting_sync import SYNC_HOUR as meeting_hour
    from app.services.scheduled_sync import (
        FABRIC_SYNC_INTERVAL_DAYS, REVENUE_SYNC_INTERVAL_DAYS,
        SYNC_DAYS, U2C_IMPORT_INTERVAL_HOURS, U2C_RETRY_INTERVAL_HOURS,
    )
    from app.services.se_specialty import is_data_se

    schedules = {key: {'label': 'On demand', 'detail': 'No recurring schedule.'}
                 for key in OPERATIONS}
    preference = UserPreference.query.first()
    weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    milestone_label = ', '.join(weekdays[day] for day in sorted(SYNC_DAYS))
    if preference and preference.milestone_sync_hour is not None \
            and preference.milestone_sync_minute is not None:
        at = clock_time(preference.milestone_sync_hour, preference.milestone_sync_minute)
        milestone_label += f" · {at.strftime('%I:%M %p').lstrip('0')} local"
    milestone_detail = ('Catches up at startup after account setup. '
                        'The worker assigns a time if one has not been saved yet.')
    if preference and not preference.milestone_auto_sync:
        milestone_label, milestone_detail = 'Disabled', 'Milestone auto-sync is off in Settings.'
    elif preference is None:
        milestone_label, milestone_detail = 'Not configured', 'Complete initial app setup first.'
    schedules['milestones'] = {'label': milestone_label, 'detail': milestone_detail}
    schedules['revenue'] = {
        'label': f'Every {REVENUE_SYNC_INTERVAL_DAYS} days',
        'detail': 'Checked after successful milestone syncs; queued when revenue is due.',
    }
    schedules['marketing'] = {
        'label': 'After milestone sync',
        'detail': 'Queued after every successful milestone sync, including manual runs.',
    }
    schedules['u2c'] = {
        'label': f'Every {U2C_IMPORT_INTERVAL_HOURS} hours',
        'detail': f'Checked at startup and every five minutes, and after milestone syncs. '
                  f'Requires configured sales territories. Failed imports are eligible again '
                  f'after {U2C_RETRY_INTERVAL_HOURS} hour, except missing-territory setup '
                  f'failures, which can retry as soon as territories are available.',
    }
    schedules['fabric'] = {
        'label': f'Every {FABRIC_SYNC_INTERVAL_DAYS} days' if is_data_se() else 'On demand',
        'detail': 'Automatic refresh is for Data SEs and sellers. Checked at startup and every '
                  'five minutes; refreshes telemetry, revenue and P to F together when '
                  'either snapshot is stale or missing. Requires customer TPIDs. '
                  'Failed attempts wait one hour.',
    }
    schedules['sql-motions'] = {
        'label': 'Every 7 days' if is_data_se() else 'On demand',
        'detail': 'Automatic refresh is for Data SEs and sellers. Checked at startup and every '
                  'five minutes. Refreshes Azure Migrations and ADS Accelerate together. '
                  'Requires active customer TPIDs; failed attempts wait one hour.',
    }
    for key, hour, detail in (
        ('meeting-aura', meeting_hour, 'Requires imported customers. '
         'Catches up at startup when cached meetings are stale '
         'or missing. Automatically queued after Account Sync finishes successfully, '
         'unless its skip_aura option is true.'),
        ('copilot-actions', copilot_hour, 'Catches up at startup when due. Requires completed '
         'account setup and Copilot actions enabled in Settings.'),
    ):
        schedules[key] = {
            'label': f"Daily · {clock_time(hour).strftime('%I:%M %p').lstrip('0')} local",
            'detail': detail,
        }
    if preference and not preference.copilot_actions_enabled:
        schedules['copilot-actions'] = {
            'label': 'Disabled', 'detail': 'Copilot actions are off in Settings.',
        }
    schedules['meetings'] = {
        'label': 'Startup if needed',
        'detail': "Today's attendee data is refreshed at startup if missing while the "
                  'meeting window is already cached. Other dates are on demand.',
    }
    schedules['revenue-analysis'] = {
        'label': 'With revenue import',
        'detail': 'Trend analysis runs within revenue sync unless run_analysis is false. '
                  'This standalone operation can also be started on demand.',
    }
    automatic_triggers = {
        'accounts': [
            'Activity Coverage meeting preparation runs Account Sync once if the required '
            'account-discovery upgrade has not completed. It does not start Meeting sync.',
        ],
        'milestones': [
            'Activity Coverage meeting preparation automatically refreshes milestones '
            'before matching meetings.',
        ],
        'customer-data': ['Automatically queued after importing a customer by TPID.'],
        'meeting-aura': [
            'After Account Sync finishes successfully, Meeting sync automatically runs, '
            'unless Account Sync has skip_aura set to true.',
        ],
        'meetings': [
            'Opening an uncached meeting date in the note editor automatically fetches that date.',
        ],
    }
    for key, schedule in schedules.items():
        schedule['triggers'] = automatic_triggers.get(key, [])
    return schedules


def operation_runs(operation: str) -> tuple[Job | None, list[Job]]:
    """Select the running request before queued or finished runs for one operation."""
    query = Job.query.filter_by(job_type=f'sync:{operation}')
    active = query.filter(Job.status.in_(ACTIVE_STATES)).order_by(Job.id).all()
    latest = next((job for job in active if job.status == Job.STATUS_RUNNING), active[0]) \
        if active else query.order_by(Job.id.desc()).first()
    return latest, active


def operation_snapshots() -> dict:
    """Project active/latest runs and retained successes for every allowlisted sync."""
    operations = []
    schedules = operation_schedules()
    for key, spec in OPERATIONS.items():
        query = Job.query.filter_by(job_type=f'sync:{key}')
        latest, active = operation_runs(key)
        success = query.filter_by(status=Job.STATUS_DONE).order_by(Job.id.desc()).first()
        legacy = SyncStatus.query.filter_by(sync_type=spec.legacy_status).first() \
            if spec.legacy_status else None
        last_success = (success.last_success_at if json.loads(
            success.result or '{}').get('skipped') else success.finished_at) if success else (
            latest.last_success_at if latest else
            legacy.completed_at if legacy and legacy.success else None)
        operations.append({
            'operation': key, 'name': spec.name, 'description': spec.description,
            'defaults': spec.defaults, 'recovery_safe': spec.recovery_safe,
            'schedule': schedules[key],
            'latest': job_snapshot(latest) if latest else None,
            'queued': [job_snapshot(job) for job in active if job.id != latest.id],
            'last_success_at': _iso(last_success),
            'legacy_state': SyncStatus.get_status(spec.legacy_status) if spec.legacy_status else None,
        })
    from app.services import sync_runtime
    return {'worker': worker_health(), 'operations': operations,
            'writer': sync_runtime.write_owner()}


def run_dependency(operation: str, options: dict | None = None) -> dict:
    """Execute an admitted child serially in its parent's worker, without queue deadlock."""
    from flask import current_app

    parent_id = job_queue.CURRENT_JOB_ID.get()
    if parent_id is None:
        raise RuntimeError('Sync dependencies must be executed by the durable worker.')
    parent = db.session.get(Job, parent_id)
    worker_id = parent.claimed_by
    child = admit_sync(operation, options, initiator=f'job:{parent_id}')
    while child.status in ACTIVE_STATES:
        if child.status == Job.STATUS_RUNNING:
            publish_progress({'phase': 'dependency',
                              'message': f'Waiting for {OPERATIONS[operation].name}, job {child.id}.'})
            db.session.expire_all()
            time.sleep(0.2)
            child = db.session.get(Job, child.id)
            continue
        if child.available_at and child.available_at > job_queue._now():
            time.sleep(min(1, (child.available_at - job_queue._now()).total_seconds()))
            db.session.refresh(child)
            continue
        claimed = job_queue.claim_next(worker_id, job_id=child.id, ancestors=(parent_id,))
        if claimed is None:
            db.session.expire_all()
            time.sleep(0.2)
            db.session.refresh(child)
            continue
        db.session.refresh(child)
        job_queue._execute(child, app=current_app._get_current_object(), with_heartbeat=True)
        db.session.refresh(child)
    if child.status == Job.STATUS_FAILED:
        raise job_queue.NonRetryableJobError(child.last_error or 'Dependency failed.')
    return json.loads(child.result or '{}')


def observe_job_stream(job_id: int):
    """Legacy SSE observes persisted worker events; disconnection only stops observation."""
    cursor = 0
    yield f'event: accepted\ndata: {json.dumps({"job_id": job_id})}\n\n'
    while True:
        db.session.expire_all()
        job = db.session.get(Job, job_id)
        from app.services import sync_runtime
        live = sync_runtime.read(job)
        events = live['events'] if live else [
            {'id': event.id, 'type': event.event_type, 'data': event.data_json}
            for event in JobEvent.query.filter_by(job_id=job_id).order_by(JobEvent.id).all()]
        for event in events:
            if event['id'] <= cursor:
                continue
            cursor = event['id']
            prefix = '' if job.job_type == 'sync:accounts' else f"event: {event['type']}\n"
            yield f"{prefix}data: {event['data']}\n\n"
        if job.status not in ACTIVE_STATES:
            data = json.loads(job.result) if job.result else {
                'success': False, 'error': job.last_error, 'message': job.last_error}
            if job.job_type == 'sync:accounts':
                data['complete'] = job.status == Job.STATUS_DONE
                prefix = ''
            else:
                prefix = f'event: {"complete" if job.status == Job.STATUS_DONE else "error"}\n'
            yield f'{prefix}data: {json.dumps(data)}\n\n'
            return
        yield ': observing\n\n'
        db.session.remove()
        time.sleep(1)
