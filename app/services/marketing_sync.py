"""Marketing Insights sync service.

Fetches marketing engagement data from MSX (msp_marketingengagements,
msp_marketinginteractions, and contact msp_ fields) for all customers
with a TPID, and upserts into local tables.

Uses parallel workers for the API fetch phase, then writes to the
database sequentially. Same architecture as the milestone sync.
"""

import json
import logging
import math
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List

from app.models import (
    db, Customer, SyncStatus,
    MarketingSummary, MarketingInteraction, MarketingContact,
)
from app.services.msx_api import (
    get_marketing_summary, get_marketing_breakdown, get_marketing_contacts,
)
from app.services.msx_auth import is_vpn_blocked

logger = logging.getLogger(__name__)

SYNC_TYPE = 'marketing'
_WORKERS = 4  # Concurrent API fetch workers


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format a dict as a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _parse_datetime(val: Any) -> datetime | None:
    """Parse an ISO datetime string from MSX into a naive datetime."""
    if not val:
        return None
    try:
        if isinstance(val, str):
            # Handle "2026-04-03T14:12:42.000Z" and "2026-04-03T14:12:42Z"
            val = val.replace('Z', '+00:00')
            return datetime.fromisoformat(val).replace(tzinfo=None)
        return None
    except (ValueError, TypeError):
        return None


def _upsert_summary(customer: Customer, tpid: str, data: Dict[str, Any]) -> bool:
    """Upsert a MarketingSummary row for the customer. Returns True on success."""
    existing = MarketingSummary.query.filter_by(customer_id=customer.id).first()
    if existing:
        existing.tpid = tpid
        existing.total_interactions = data.get('total_interactions', 0)
        existing.content_downloads = data.get('content_downloads', 0)
        existing.trials = data.get('trials', 0)
        existing.engaged_contacts = data.get('engaged_contacts', 0)
        existing.unique_decision_makers = data.get('unique_decision_makers', 0)
        existing.last_interaction_date = _parse_datetime(data.get('last_interaction_date'))
        existing.synced_at = datetime.now(timezone.utc)
    else:
        summary = MarketingSummary(
            customer_id=customer.id,
            tpid=tpid,
            total_interactions=data.get('total_interactions', 0),
            content_downloads=data.get('content_downloads', 0),
            trials=data.get('trials', 0),
            engaged_contacts=data.get('engaged_contacts', 0),
            unique_decision_makers=data.get('unique_decision_makers', 0),
            last_interaction_date=_parse_datetime(data.get('last_interaction_date')),
        )
        db.session.add(summary)
    return True


def _upsert_interactions(customer: Customer, tpid: str, rows: list) -> int:
    """Upsert MarketingInteraction rows. Returns count of rows upserted."""
    if not rows:
        return 0
    # Delete existing rows for this customer, then insert fresh
    MarketingInteraction.query.filter_by(customer_id=customer.id).delete()
    count = 0
    for r in rows:
        mi = MarketingInteraction(
            customer_id=customer.id,
            tpid=tpid,
            composite_key=r.get('composite_key', ''),
            solution_area=r.get('solution_area', ''),
            sales_play=r.get('sales_play', ''),
            all_interactions=r.get('all_interactions', 0),
            contact_me=r.get('contact_me', 0),
            trial_signups=r.get('trial_signups', 0),
            content_downloads=r.get('content_downloads', 0),
            events=r.get('events', 0),
            unique_decision_makers=r.get('unique_decision_makers', 0),
            high_interaction_contacts=r.get('high_interaction_contacts', 0),
            high_interaction_count=r.get('high_interaction_count', 0),
            last_interaction_date=_parse_datetime(r.get('last_interaction_date')),
            last_high_interaction_date=_parse_datetime(r.get('last_high_interaction_date')),
        )
        db.session.add(mi)
        count += 1
    return count


def _upsert_contacts(customer: Customer, contacts: list) -> int:
    """Upsert MarketingContact rows. Returns count of rows upserted."""
    if not contacts:
        return 0
    # Delete existing rows for this customer, then insert fresh
    MarketingContact.query.filter_by(customer_id=customer.id).delete()
    count = 0
    for c in contacts:
        mc = MarketingContact(
            customer_id=customer.id,
            contact_guid=c.get('contact_guid', ''),
            contact_name=c.get('contact_name', ''),
            job_title=c.get('job_title', ''),
            email=c.get('email', ''),
            mail_interactions=c.get('mail_interactions', 0),
            meeting_interactions=c.get('meeting_interactions', 0),
            audience_type=c.get('audience_type', ''),
            engagement_level=c.get('engagement_level', ''),
            last_interaction_date=_parse_datetime(c.get('last_interaction_date')),
            last_solution_area=c.get('last_solution_area', ''),
        )
        db.session.add(mc)
        count += 1
    return count


def _fetch_worker(
    tasks: List[tuple],
    progress_q: queue.Queue,
) -> None:
    """Worker thread: fetch all 3 marketing layers for a batch of customers.

    Puts results onto progress_q as tuples:
        ('fetched', cust_id, cust_name, {summary, breakdown, contacts})
        ('retry', cust_id, cust_name, message_str)
        ('vpn', cust_id, cust_name, None)
        ('done', None, None, None)
    """
    from app.services.msx_api import msx_retry_state

    for cust_id, cust_name, tpid in tasks:
        if is_vpn_blocked():
            progress_q.put(('vpn', cust_id, cust_name, None))
            return

        def _on_retry(attempt, max_retries, wait_secs, error_type,
                      _cid=cust_id, _cn=cust_name):
            progress_q.put((
                'retry', _cid, _cn,
                f"{_cn} - Timeout, retrying ({attempt}/{max_retries})..."
            ))

        msx_retry_state.callback = _on_retry
        try:
            summary = get_marketing_summary(tpid)
            if summary.get('vpn_blocked'):
                progress_q.put(('vpn', cust_id, cust_name, None))
                return

            breakdown = get_marketing_breakdown(tpid)
            if breakdown.get('vpn_blocked'):
                progress_q.put(('vpn', cust_id, cust_name, None))
                return

            contacts = get_marketing_contacts(tpid)
            if contacts.get('vpn_blocked'):
                progress_q.put(('vpn', cust_id, cust_name, None))
                return
        finally:
            msx_retry_state.callback = None

        progress_q.put(('fetched', cust_id, cust_name, {
            'summary': summary,
            'breakdown': breakdown,
            'contacts': contacts,
        }))
    progress_q.put(('done', None, None, None))


def sync_marketing_stream() -> Generator[str, None, None]:
    """Stream marketing insight sync progress as Server-Sent Events.

    Phase 1: Parallel API fetch with _WORKERS concurrent threads.
    Phase 2: Sequential DB writes (SQLite single-writer).

    Event types:
        - start: total customer count
        - progress: per-customer fetch/write status
        - vpn_blocked: VPN block detected
        - complete: final summary
    """
    start_time = time.time()

    customers = Customer.query.filter(
        Customer.tpid.isnot(None),
        Customer.tpid != '',
    ).all()

    total = len(customers)
    if total == 0:
        yield _sse_event('complete', {
            'success': True,
            'total': 0,
            'synced': 0,
            'failed': 0,
            'message': 'No customers with TPIDs found.',
        })
        return

    SyncStatus.mark_started(SYNC_TYPE)
    yield _sse_event('start', {'total': total})

    # Build task list: (customer_id, display_name, tpid_string)
    customer_tasks = []
    customer_map = {}
    for c in customers:
        customer_tasks.append((c.id, c.get_display_name(), str(c.tpid)))
        customer_map[c.id] = c

    # -----------------------------------------------------------------
    # Phase 1: Parallel API fetch
    # -----------------------------------------------------------------
    fetch_results = {}  # cust_id -> {summary, breakdown, contacts}
    progress_q: queue.Queue = queue.Queue()
    n_workers = min(_WORKERS, len(customer_tasks))
    vpn_hit = False
    fetched = 0

    chunk_size = math.ceil(len(customer_tasks) / n_workers)
    chunks = [
        customer_tasks[i:i + chunk_size]
        for i in range(0, len(customer_tasks), chunk_size)
    ]
    actual_workers = len(chunks)

    with ThreadPoolExecutor(max_workers=actual_workers) as pool:
        for chunk in chunks:
            pool.submit(_fetch_worker, chunk, progress_q)

        done_count = 0
        while done_count < actual_workers:
            msg = progress_q.get()
            evt, cust_id, cust_name, result = msg

            if evt == 'vpn':
                vpn_hit = True
                yield _sse_event('vpn_blocked', {
                    'message': 'IP address is blocked - connect to VPN and retry.',
                })
                break
            elif evt == 'retry':
                yield _sse_event('progress', {
                    'current': fetched,
                    'total': total,
                    'customer': result,  # message string
                    'status': 'retrying',
                    'progress': int((fetched / total) * 70),
                })
            elif evt == 'fetched':
                fetch_results[cust_id] = result
                fetched += 1
                SyncStatus.update_heartbeat(SYNC_TYPE)
                pct = int((fetched / total) * 70)  # 0-70% for fetch phase
                yield _sse_event('progress', {
                    'current': fetched,
                    'total': total,
                    'customer': cust_name,
                    'status': 'fetching',
                    'progress': pct,
                })
            elif evt == 'done':
                done_count += 1

    if vpn_hit:
        SyncStatus.mark_completed(
            SYNC_TYPE, success=False, items_synced=0,
            details=json.dumps({'error': 'VPN blocked'}),
        )
        return

    # -----------------------------------------------------------------
    # Phase 2: Sequential DB writes
    # -----------------------------------------------------------------
    synced = 0
    failed = 0
    total_interactions = 0
    total_contacts = 0
    errors: List[str] = []

    for i, (cust_id, cust_name, tpid) in enumerate(customer_tasks, 1):
        customer = customer_map[cust_id]
        data = fetch_results.get(cust_id)
        SyncStatus.update_heartbeat(SYNC_TYPE)

        if not data:
            failed += 1
            errors.append(f"{cust_name}: No fetch data")
            continue

        try:
            summary_result = data['summary']
            breakdown_result = data['breakdown']
            contacts_result = data['contacts']

            # Check for API errors
            for result in [summary_result, breakdown_result, contacts_result]:
                if not result.get('success'):
                    raise ValueError(result.get('error', 'Unknown error'))

            # Upsert summary
            if summary_result.get('data'):
                _upsert_summary(customer, tpid, summary_result['data'])
                total_interactions += summary_result['data'].get('total_interactions', 0)

            # Upsert interactions
            _upsert_interactions(customer, tpid, breakdown_result.get('data', []))

            # Upsert contacts
            contact_rows = contacts_result.get('data', [])
            _upsert_contacts(customer, contact_rows)
            total_contacts += len(contact_rows)

            db.session.commit()
            synced += 1

            pct = 70 + int((i / len(customer_tasks)) * 30)  # 70-100% for write phase
            yield _sse_event('progress', {
                'current': fetched + i,
                'total': total * 2,  # fetch + write
                'customer': cust_name,
                'status': 'ok',
                'progress': pct,
            })

        except Exception as e:
            db.session.rollback()
            failed += 1
            errors.append(f"{cust_name}: {str(e)}")
            logger.exception(f"Error writing marketing data for {cust_name} (TPID {tpid})")

    duration = round(time.time() - start_time, 1)

    SyncStatus.mark_completed(
        SYNC_TYPE, success=(failed == 0), items_synced=synced,
        details=json.dumps({
            'failed': failed,
            'total_interactions': total_interactions,
            'total_contacts': total_contacts,
            'duration': duration,
            'errors': errors[:10],
        }),
    )

    yield _sse_event('complete', {
        'success': failed == 0,
        'total': total,
        'synced': synced,
        'failed': failed,
        'total_interactions': total_interactions,
        'total_contacts': total_contacts,
        'duration': duration,
        'message': f'Marketing sync complete: {synced} customers synced in {duration}s.',
    })
