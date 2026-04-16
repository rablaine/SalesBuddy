"""
Milestone sync service for Sales Buddy.

Pulls active (uncommitted) milestones from MSX for all customers
and upserts them into the local database. Uses batched OData queries
for opportunities and milestones, then writes to the database sequentially.
"""
import json
import logging
import math
import queue
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Generator, Tuple

from app.models import db, Customer, Milestone, MilestoneAudit, MsxTask, Opportunity, User, SyncStatus
from app.services.msx_api import (
    batch_get_milestones,
    batch_get_opportunities,
    extract_account_id_from_url,
    get_milestones_by_account,
    get_milestone_audits,
    get_milestone_comments,
    get_my_deal_team_ids,
    get_my_milestone_team_ids,
    get_opportunities_by_account,
    get_tasks_for_milestones,
    build_milestone_url,
    build_opportunity_url,
    build_task_url,
    TASK_CATEGORIES,
    HOK_TASK_CATEGORIES,
)
from app.services.msx_auth import is_vpn_blocked

logger = logging.getLogger(__name__)

# Active milestone statuses (uncommitted - the ones we're working to commit)
ACTIVE_STATUSES = {'On Track', 'At Risk', 'Blocked'}


def sync_all_customer_milestones() -> Dict[str, Any]:
    """
    Sync active milestones from MSX for all customers with a tpid_url.
    
    Loops through every customer that has an MSX account link (tpid_url),
    fetches their milestones from MSX, and upserts into the local database.
        
    Returns:
        Dict with sync results:
        - success: bool
        - customers_synced: int (customers successfully queried)
        - customers_skipped: int (customers without tpid_url)
        - customers_failed: int (customers where MSX query failed)
        - milestones_created: int
        - milestones_updated: int
        - milestones_deactivated: int (marked as no longer active in MSX)
        - errors: list of error strings
        - duration_seconds: float
    """
    start_time = datetime.now(timezone.utc)

    results = {
        "success": True,
        "customers_synced": 0,
        "customers_skipped": 0,
        "customers_failed": 0,
        "milestones_created": 0,
        "milestones_updated": 0,
        "milestones_deactivated": 0,
        "opportunities_created": 0,
        "tasks_created": 0,
        "tasks_updated": 0,
        "errors": [],
        "duration_seconds": 0,
    }
    
    # Get all customers with MSX account links
    customers = Customer.query.filter(
        Customer.tpid_url.isnot(None),
        Customer.tpid_url != '',
    ).all()
    
    if not customers:
        results["success"] = True
        results["errors"].append("No customers with MSX account links found.")
        return results
    
    # Mark sync as started so interrupted syncs are detectable
    SyncStatus.mark_started('milestones')

    logger.info(f"Starting milestone sync for {len(customers)} customers")

    all_seen_opp_ids = set()

    for customer in customers:
        # Bail early if VPN block was detected during this sync
        if is_vpn_blocked():
            results["errors"].append("VPN/IP block detected — remaining customers skipped.")
            break

        # Update heartbeat so page loads can tell the sync is still running
        SyncStatus.update_heartbeat('milestones')

        try:
            customer_result = sync_customer_milestones(customer)
            
            if customer_result["success"]:
                results["customers_synced"] += 1
                results["milestones_created"] += customer_result["created"]
                results["milestones_updated"] += customer_result["updated"]
                results["milestones_deactivated"] += customer_result["deactivated"]
                results["opportunities_created"] += customer_result.get(
                    "opportunities_created", 0
                )
                results["tasks_created"] += customer_result.get(
                    "tasks_created", 0
                )
                results["tasks_updated"] += customer_result.get(
                    "tasks_updated", 0
                )
                all_seen_opp_ids.update(
                    customer_result.get("seen_opportunity_ids", set())
                )
            else:
                results["customers_failed"] += 1
                results["errors"].append(
                    f"{customer.get_display_name()}: {customer_result['error']}"
                )
        except Exception as e:
            results["customers_failed"] += 1
            results["errors"].append(
                f"{customer.get_display_name()}: {str(e)}"
            )
            logger.exception(f"Error syncing milestones for customer {customer.id}")

    # Sync milestones for stale opportunities not covered by the active sync
    stale_gen = _sync_stale_opportunity_milestones(all_seen_opp_ids)
    try:
        while True:
            next(stale_gen)
    except StopIteration as stop:
        stale_result = stop.value
        results["stale_milestones_updated"] = stale_result.get(
            "milestones_updated", 0
        )
        results["stale_opportunities_refreshed"] = stale_result.get(
            "opportunities_refreshed", 0
        )

    # Calculate duration
    results["duration_seconds"] = (datetime.now(timezone.utc) - start_time).total_seconds()
    
    # If all customers failed, mark as failure
    if results["customers_synced"] == 0 and results["customers_failed"] > 0:
        results["success"] = False
    
    # Update team membership flags
    _update_team_memberships()
    _update_deal_team_memberships()
    
    # Sync comments for milestones I'm on the team for
    comment_gen = _sync_team_milestone_comments(since=start_time)
    try:
        while True:
            next(comment_gen)
    except StopIteration as stop:
        comment_result = stop.value
        results["comments_synced"] = comment_result.get("comments_synced", 0)

    # Sync audit trail for recently-modified milestones
    audit_gen = _sync_milestone_audits()
    try:
        while True:
            next(audit_gen)
    except StopIteration as stop:
        audit_result = stop.value
        results["audit_fields_saved"] = audit_result.get("fields_saved", 0)

    logger.info(
        f"Milestone sync complete: {results['customers_synced']} synced, "
        f"{results['customers_failed']} failed, "
        f"{results['milestones_created']} created, "
        f"{results['milestones_updated']} updated"
    )
    
    SyncStatus.mark_completed(
        'milestones',
        success=results['success'],
        items_synced=results['milestones_created'] + results['milestones_updated'],
        details=json.dumps({
            'synced': results['customers_synced'],
            'failed': results['customers_failed'],
            'created': results['milestones_created'],
            'updated': results['milestones_updated'],
        }),
    )
    
    return results


def _ms_fetch_worker(
    tasks: List[tuple],
    progress_q: queue.Queue,
) -> None:
    """
    Worker thread: fetch milestones from MSX for a batch of customers.

    Puts results onto progress_q as tuples of
    ('fetched', cust_id, cust_name, msx_result),
    ('retry', cust_id, cust_name, message_str),
    or ('vpn', cust_id, cust_name, None).
    Sends ('done', None, None, None) when finished.
    """
    from app.services.msx_api import msx_retry_state

    for cust_id, cust_name, account_id in tasks:
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
            result = get_milestones_by_account(
                account_id,
                open_opportunities_only=True,
                current_fy_only=True,
            )
        finally:
            msx_retry_state.callback = None
        progress_q.put(('fetched', cust_id, cust_name, result))
    progress_q.put(('done', None, None, None))


def sync_all_customer_milestones_stream(
) -> Generator[str, None, None]:
    """
    Stream milestone sync progress as Server-Sent Events.

    Uses batched OData queries for opportunities and milestones,
    then writes to the database sequentially.

    Event types:
        - start: total customer count
        - progress: per-customer fetch/write result
        - vpn_blocked: VPN block detected
        - complete: final summary (includes opportunities_created)
    """
    start_time = _time.time()

    customers = Customer.query.filter(
        Customer.tpid_url.isnot(None),
        Customer.tpid_url != '',
    ).all()

    total = len(customers)
    if total == 0:
        yield _sse_event('complete', {
            'success': True,
            'total': 0,
            'synced': 0,
            'failed': 0,
            'created': 0,
            'updated': 0,
            'deactivated': 0,
            'opportunities_created': 0,
            'message': 'No customers with MSX account links found.',
        })
        return

    # Mark sync as started so interrupted syncs are detectable
    SyncStatus.mark_started('milestones')
    yield _sse_event('start', {'total': total})

    # -----------------------------------------------------------------
    # Prep: extract account IDs (fast, main thread)
    # -----------------------------------------------------------------
    customer_tasks = []   # [(cust_id, cust_name, account_id), ...]
    customer_map = {}     # cust_id -> Customer
    skip_ids = set()      # customers where account_id extraction failed

    for c in customers:
        account_id = extract_account_id_from_url(c.tpid_url)
        if account_id:
            customer_tasks.append((c.id, c.get_display_name(), account_id))
            customer_map[c.id] = c
        else:
            skip_ids.add(c.id)

    # -----------------------------------------------------------------
    # Phase 1a: Batch fetch open opportunities (batched OData)
    # -----------------------------------------------------------------
    acct_to_cust: Dict[str, int] = {}
    for cid, cname, acct in customer_tasks:
        acct_to_cust[acct] = cid
    opp_account_ids = list(acct_to_cust.keys())

    # opp_map: msx_opportunity_id -> opp dict (with account routing)
    opp_map: Dict[str, dict] = {}
    # opp_by_account: account_id -> [opp_ids] (for routing to customers)
    opp_by_account: Dict[str, List[str]] = {a: [] for a in opp_account_ids}
    vpn_hit = False
    total_opps_synced = 0
    total_opps_updated = 0
    _OPP_CHUNK = 15  # accounts per OData call

    if opp_account_ids:
        yield _sse_event('opp_sync_start', {
            'message': 'Fetching opportunities...',
            'total': len(opp_account_ids),
        })
        opp_chunks = [
            opp_account_ids[i:i + _OPP_CHUNK]
            for i in range(0, len(opp_account_ids), _OPP_CHUNK)
        ]
        total_opp_chunks = len(opp_chunks)

        for chunk_idx, chunk in enumerate(opp_chunks, 1):
            batch_opp_result = batch_get_opportunities(
                chunk, open_only=True,
            )
            SyncStatus.update_heartbeat('milestones')

            if batch_opp_result.get("success"):
                for acct_id, opps in batch_opp_result.get(
                    "by_account", {}
                ).items():
                    for opp in opps:
                        opp_id = opp.get("id")
                        if opp_id:
                            opp_map[opp_id] = opp
                            opp_map[opp_id]["_account_id"] = acct_id
                            opp_by_account.setdefault(acct_id, []).append(
                                opp_id
                            )

            pct = int((chunk_idx / total_opp_chunks) * 16)  # 0-16%
            yield _sse_event('progress', {
                'current': chunk_idx,
                'total': total_opp_chunks,
                'customer': f'Opps batch {chunk_idx}/{total_opp_chunks}'
                            f' ({len(opp_map)} so far)',
                'status': 'fetching',
                'progress': min(pct, 16),
            })


    # -----------------------------------------------------------------
    # Phase 1b: Batch fetch milestones by opportunity ID (batched OData)
    # -----------------------------------------------------------------
    # milestones_by_customer: cust_id -> [milestone_dicts with opp data]
    milestones_by_customer: Dict[int, List[dict]] = {
        cid: [] for cid, _, _ in customer_tasks
    }
    all_opp_ids = list(opp_map.keys())
    _MS_CHUNK = 20  # opp IDs per OData call (matches batch_get_milestones default)

    if all_opp_ids:
        opp_chunks = [
            all_opp_ids[i:i + _MS_CHUNK]
            for i in range(0, len(all_opp_ids), _MS_CHUNK)
        ]
        total_chunks = len(opp_chunks)
        ms_total = 0

        for chunk_idx, chunk in enumerate(opp_chunks, 1):
            ms_batch_result = batch_get_milestones(
                chunk, current_fy_only=True,
            )
            SyncStatus.update_heartbeat('milestones')

            if ms_batch_result.get("success"):
                for opp_id, ms_list in ms_batch_result.get(
                    "by_opportunity", {}
                ).items():
                    opp_data = opp_map.get(opp_id, {})
                    acct_id = opp_data.get("_account_id")
                    cust_id = acct_to_cust.get(acct_id) if acct_id else None
                    if cust_id is None:
                        continue
                    for ms in ms_list:
                        # Inject opportunity data into milestone dict
                        ms["opportunity_name"] = opp_data.get("name", "")
                        ms["opportunity_number"] = opp_data.get("number", "")
                        ms["opportunity_statecode"] = opp_data.get(
                            "statecode"
                        )
                        ms["opportunity_state"] = opp_data.get("state")
                        ms["opportunity_status_reason"] = opp_data.get(
                            "status_reason", ""
                        )
                        ms["opportunity_estimated_value"] = opp_data.get(
                            "estimated_value"
                        )
                        ms["opportunity_estimated_close_date"] = opp_data.get(
                            "estimated_close_date"
                        )
                        ms["opportunity_owner"] = opp_data.get("owner", "")
                        ms["opportunity_customer_need"] = opp_data.get(
                            "customer_need", ""
                        )
                        ms["opportunity_description"] = opp_data.get(
                            "description", ""
                        )
                        ms["opportunity_compete_threat"] = opp_data.get(
                            "compete_threat", ""
                        )
                        milestones_by_customer[cust_id].append(ms)
                        ms_total += 1

            pct = 16 + int((chunk_idx / total_chunks) * 61)  # 16-77%
            yield _sse_event('progress', {
                'current': chunk_idx,
                'total': total_chunks,
                'customer': f'Milestones batch {chunk_idx}/{total_chunks}'
                            f' ({ms_total} so far)',
                'status': 'fetching',
                'progress': min(pct, 77),
            })

    ms_count = sum(len(v) for v in milestones_by_customer.values())
    yield _sse_event('progress', {
        'current': ms_count,
        'total': ms_count or 1,
        'customer': f'Fetched {ms_count} milestones',
        'status': 'ok',
        'progress': 77,
    })

    # -----------------------------------------------------------------
    # Phase 2: Sequential DB writes (milestones + opportunities)
    # -----------------------------------------------------------------
    synced = 0
    failed = len(skip_ids)
    total_created = 0
    total_updated = 0
    total_deactivated = 0
    total_opps_created = 0
    total_tasks_created = 0
    total_tasks_updated = 0
    total_stale_ms_updated = 0
    total_stale_opps_refreshed = 0
    errors: List[str] = []
    all_seen_opp_ids = set()

    write_count = len(customer_tasks)
    for i, (cust_id, cust_name, acct_id) in enumerate(customer_tasks, 1):
        customer = customer_map[cust_id]
        ms_list = milestones_by_customer.get(cust_id, [])
        SyncStatus.update_heartbeat('milestones')

        try:
            wr = _apply_customer_milestones(customer, ms_list)
            if wr['success']:
                synced += 1
                total_created += wr['created']
                total_updated += wr['updated']
                total_deactivated += wr['deactivated']
                total_opps_created += wr['opportunities_created']
                all_seen_opp_ids.update(
                    wr.get('seen_opportunity_ids', set())
                )

                pct = 77 + int((i / write_count) * 5)  # 77-82%
                yield _sse_event('progress', {
                    'current': i,
                    'total': write_count,
                    'customer': cust_name,
                    'status': 'ok',
                    'created': wr['created'],
                    'updated': wr['updated'],
                    'progress': min(pct, 82),
                })
            else:
                failed += 1
                errors.append(f"{cust_name}: {wr['error']}")
        except Exception as e:
            failed += 1
            errors.append(f"{cust_name}: {str(e)}")
            logger.exception(f"Error saving milestones for customer {cust_id}")

    # -----------------------------------------------------------------
    # Phase 2 (cont): Upsert opportunities into DB (catches milestone-less opps)
    # -----------------------------------------------------------------
    for acct_id, opp_ids in opp_by_account.items():
        cid = acct_to_cust.get(acct_id)
        if not cid:
            continue
        existing_map = {
            opp.msx_opportunity_id: opp
            for opp in Opportunity.query.filter_by(customer_id=cid).all()
            if opp.msx_opportunity_id
        }
        for opp_id in opp_ids:
            opp_data = opp_map.get(opp_id)
            if not opp_data:
                continue
            existing = existing_map.get(opp_id)
            if existing:
                existing.name = opp_data.get("name") or existing.name
                existing.opportunity_number = (
                    opp_data.get("number") or existing.opportunity_number
                )
                existing.statecode = opp_data.get("statecode")
                existing.state = opp_data.get("state") or existing.state
                existing.status_reason = (
                    opp_data.get("status_reason") or existing.status_reason
                )
                existing.estimated_value = opp_data.get("estimated_value")
                existing.estimated_close_date = opp_data.get(
                    "estimated_close_date"
                )
                existing.owner_name = (
                    opp_data.get("owner") or existing.owner_name
                )
                existing.customer_need = (
                    opp_data.get("customer_need") or existing.customer_need
                )
                existing.description = (
                    opp_data.get("description") or existing.description
                )
                existing.compete_threat = (
                    opp_data.get("compete_threat") or existing.compete_threat
                )
                existing.msx_url = opp_data.get("url") or existing.msx_url
                total_opps_updated += 1
            else:
                opp = Opportunity(
                    msx_opportunity_id=opp_id,
                    name=opp_data.get("name", "Unknown Opportunity"),
                    customer_id=cid,
                    opportunity_number=opp_data.get("number"),
                    statecode=opp_data.get("statecode"),
                    state=opp_data.get("state"),
                    status_reason=opp_data.get("status_reason"),
                    estimated_value=opp_data.get("estimated_value"),
                    estimated_close_date=opp_data.get("estimated_close_date"),
                    owner_name=opp_data.get("owner"),
                    customer_need=opp_data.get("customer_need"),
                    description=opp_data.get("description"),
                    compete_threat=opp_data.get("compete_threat"),
                    msx_url=opp_data.get("url"),
                )
                db.session.add(opp)
                existing_map[opp_id] = opp
                total_opps_synced += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Error committing opportunities for customer %s", cid
            )

    yield _sse_event('progress', {
        'current': len(opp_map),
        'total': len(opp_map) or 1,
        'customer': 'Opportunities saved',
        'status': 'ok',
        'progress': 82,
    })
    yield _sse_event('opp_sync_end', {
        'opportunities_synced': total_opps_synced,
    })

    # -----------------------------------------------------------------
    # Phase 3: Sync stale opportunities not covered by active sync
    # -----------------------------------------------------------------
    yield _sse_event('stale_sync_start', {
        'message': 'Syncing stale opportunity milestones...',
    })
    stale_gen = _sync_stale_opportunity_milestones(all_seen_opp_ids)
    try:
        while True:
            current_s, total_s, cust_name_s = next(stale_gen)
            SyncStatus.update_heartbeat('milestones')
            yield _sse_event('progress', {
                'current': current_s,
                'total': total_s,
                'customer': f'Stale opps: {cust_name_s}',
                'status': 'ok',
                'progress': 82,
            })
    except StopIteration as stop:
        stale_result = stop.value
        total_stale_ms_updated = stale_result.get('milestones_updated', 0)
        total_stale_opps_refreshed = stale_result.get('opportunities_refreshed', 0)
    yield _sse_event('stale_sync_end', {
        'milestones_updated': total_stale_ms_updated,
        'opportunities_refreshed': total_stale_opps_refreshed,
    })

    # -----------------------------------------------------------------
    # Phase 4: Batched task sync (per-batch progress)
    # -----------------------------------------------------------------
    yield _sse_event('task_sync_start', {
        'message': 'Syncing tasks for milestones...',
    })
    task_gen = _sync_all_tasks()
    try:
        while True:
            batch_num, total_batches, info, status = next(task_gen)
            SyncStatus.update_heartbeat('milestones')
            pct = 82 + int((batch_num / max(total_batches, 1)) * 10)  # 82-92%
            yield _sse_event('progress', {
                'current': batch_num,
                'total': total_batches,
                'customer': info,
                'status': status,
                'progress': min(pct, 92),
            })
    except StopIteration as stop:
        task_result = stop.value
    total_tasks_created = task_result.get('tasks_created', 0)
    total_tasks_updated = task_result.get('tasks_updated', 0)
    if not task_result.get('success'):
        logger.warning(f"Batched task sync failed: {task_result.get('error')}")
    yield _sse_event('task_sync_end', {
        'tasks_created': total_tasks_created,
        'tasks_updated': total_tasks_updated,
    })

    # -----------------------------------------------------------------
    # Phase 5: Team membership update (one API call)
    # -----------------------------------------------------------------
    yield _sse_event('progress', {
        'current': total,
        'total': total,
        'customer': 'Updating team memberships...',
        'status': 'ok',
        'progress': 92,
    })
    _update_team_memberships()
    _update_deal_team_memberships()

    # -----------------------------------------------------------------
    # Phase 4: Sync comments for milestones I'm on the team for
    # -----------------------------------------------------------------
    total_comments_synced = 0
    total_comments_failed = 0
    yield _sse_event('comment_sync_start', {
        'message': 'Syncing forecast comments for team milestones...',
    })
    comment_gen = _sync_team_milestone_comments(
        since=datetime.fromtimestamp(start_time, tz=timezone.utc),
    )
    try:
        while True:
            current_ms, total_ms, ms_title = next(comment_gen)
            SyncStatus.update_heartbeat('milestones')
            pct = 95 + int((current_ms / max(total_ms, 1)) * 0)  # 95% (instant)
            yield _sse_event('progress', {
                'current': current_ms,
                'total': total_ms,
                'customer': f'Comments: {ms_title}',
                'status': 'ok',
                'progress': 95,
            })
    except StopIteration as stop:
        comment_result = stop.value
        total_comments_synced = comment_result.get('comments_synced', 0)
        total_comments_failed = comment_result.get('comments_failed', 0)
        total_comments_skipped = comment_result.get('comments_skipped', 0)
    yield _sse_event('comment_sync_end', {
        'comments_synced': total_comments_synced,
        'comments_skipped': total_comments_skipped,
        'comments_failed': total_comments_failed,
    })

    # Sync audit trail for recently-modified milestones
    yield _sse_event('audit_sync_start', {
        'message': 'Syncing audit trail for recently-modified milestones...',
    })
    audit_gen = _sync_milestone_audits()
    audit_fields_saved = 0
    try:
        while True:
            batch_done, batch_total = next(audit_gen)
            SyncStatus.update_heartbeat('milestones')
            pct = 95 + int((batch_done / max(batch_total, 1)) * 4)  # 95-99%
            yield _sse_event('progress', {
                'current': batch_done,
                'total': batch_total,
                'customer': f'Audit trail batch {batch_done}/{batch_total}',
                'status': 'ok',
                'progress': min(pct, 99),
            })
    except StopIteration as stop:
        audit_result = stop.value
        audit_fields_saved = audit_result.get('fields_saved', 0)
    yield _sse_event('audit_sync_end', {
        'audit_fields_saved': audit_fields_saved,
    })

    duration = round(_time.time() - start_time, 1)
    sync_success = synced > 0 or failed == 0

    SyncStatus.mark_completed(
        'milestones',
        success=sync_success,
        items_synced=total_created + total_updated + total_stale_ms_updated,
        details=json.dumps({
            'synced': synced, 'failed': failed,
            'created': total_created, 'updated': total_updated,
            'deactivated': total_deactivated,
            'opportunities_created': total_opps_created,
            'opportunities_synced': total_opps_synced,
            'tasks_created': total_tasks_created,
            'tasks_updated': total_tasks_updated,
            'comments_synced': total_comments_synced,
            'stale_milestones_updated': total_stale_ms_updated,
            'stale_opportunities_refreshed': total_stale_opps_refreshed,
        }),
    )

    yield _sse_event('complete', {
        'success': sync_success,
        'total': total,
        'synced': synced,
        'failed': failed,
        'created': total_created,
        'updated': total_updated,
        'deactivated': total_deactivated,
        'opportunities_created': total_opps_created,
        'opportunities_synced': total_opps_synced,
        'tasks_created': total_tasks_created,
        'tasks_updated': total_tasks_updated,
        'comments_synced': total_comments_synced,
        'stale_milestones_updated': total_stale_ms_updated,
        'stale_opportunities_refreshed': total_stale_opps_refreshed,
        'duration': duration,
        'errors': errors[:5],
    })


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format a dict as a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def sync_customer_milestones(
    customer: Customer,
) -> Dict[str, Any]:
    """
    Sync milestones from MSX for a single customer.
    
    Fetches active milestones from MSX and upserts them into the database.
    Milestones that are no longer returned by MSX (e.g., completed/cancelled)
    get their status updated.
    
    Args:
        customer: The Customer model instance.
        
    Returns:
        Dict with:
        - success: bool
        - created: int
        - updated: int
        - deactivated: int
        - opportunities_created: int
        - error: str (if failed)
    """
    fetch_result = _fetch_customer_milestones(customer)
    if not fetch_result.get("success"):
        return {
            "success": False, "created": 0, "updated": 0,
            "deactivated": 0, "opportunities_created": 0,
            "tasks_created": 0, "tasks_updated": 0,
            "error": fetch_result.get("error", "Unknown MSX error"),
        }
    result = _apply_customer_milestones(
        customer, fetch_result.get("milestones", [])
    )

    # Sync tasks after milestones are committed
    if result.get("success"):
        task_result = _sync_customer_tasks(customer)
        result["tasks_created"] = task_result.get("tasks_created", 0)
        result["tasks_updated"] = task_result.get("tasks_updated", 0)
        if not task_result.get("success"):
            logger.warning(
                f"Task sync failed for {customer.get_display_name()}: "
                f"{task_result.get('error')}"
            )

    # Sync opportunities directly (catches opps with no milestones)
    if customer.tpid_url:
        opp_result = sync_customer_opportunities(customer)
        result["opportunities_synced"] = opp_result.get("created", 0)
        result["opportunities_updated"] = (
            result.get("opportunities_updated", 0) + opp_result.get("updated", 0)
        )

    return result


def _fetch_customer_milestones(customer: Customer) -> Dict[str, Any]:
    """
    Fetch milestones from MSX for a single customer (API only, no DB writes).

    Args:
        customer: The Customer model instance (needs tpid_url).

    Returns:
        Dict from get_milestones_by_account with success, milestones, error.
    """
    account_id = extract_account_id_from_url(customer.tpid_url)
    if not account_id:
        return {"success": False, "error": "Could not extract account ID from tpid_url"}
    return get_milestones_by_account(
        account_id,
        open_opportunities_only=True,
        current_fy_only=True,
    )


def _apply_customer_milestones(
    customer: Customer,
    msx_milestones: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Write pre-fetched milestone data to DB for a single customer.

    Creates/updates milestones and opportunities, deactivates milestones
    no longer returned by MSX.

    Pre-loads existing milestones and opportunities into dicts to avoid
    per-row queries that trigger autoflush (which caused hangs on large
    datasets).

    Args:
        customer: The Customer model instance.
        msx_milestones: List of milestone dicts from MSX API.

    Returns:
        Dict with success, created, updated, deactivated,
        opportunities_created, error.
    """
    result = {
        "success": False, "created": 0, "updated": 0,
        "deactivated": 0, "opportunities_created": 0, "error": "",
    }

    now = datetime.now(timezone.utc)
    seen_msx_ids = set()
    seen_opp_ids = set()

    # Pre-load existing milestones globally by msx_milestone_id to catch
    # milestones that were created under a different customer (e.g., via
    # note save before sync existed, or after a customer re-parent in MSX).
    incoming_msx_ids = [m.get("id") for m in msx_milestones if m.get("id")]
    existing_milestones_map: Dict[str, Milestone] = {}
    if incoming_msx_ids:
        for ms in Milestone.query.filter(
            Milestone.msx_milestone_id.in_(incoming_msx_ids),
        ).all():
            existing_milestones_map[ms.msx_milestone_id] = ms
    # Also load any remaining milestones for this customer (for deactivation)
    for ms in Milestone.query.filter_by(customer_id=customer.id).filter(
        Milestone.msx_milestone_id.isnot(None),
        ~Milestone.msx_milestone_id.in_(list(existing_milestones_map.keys()) or ['']),
    ).all():
        existing_milestones_map[ms.msx_milestone_id] = ms

    # Pre-load existing opportunities to avoid per-row queries
    msx_opp_ids = [
        m.get("msx_opportunity_id") for m in msx_milestones
        if m.get("msx_opportunity_id")
    ]
    existing_opps_map: Dict[str, Opportunity] = {}
    if msx_opp_ids:
        for opp in Opportunity.query.filter(
            Opportunity.msx_opportunity_id.in_(msx_opp_ids)
        ).all():
            existing_opps_map[opp.msx_opportunity_id] = opp

    with db.session.no_autoflush:
        for msx_ms in msx_milestones:
            msx_id = msx_ms.get("id")
            if not msx_id:
                continue

            seen_msx_ids.add(msx_id)
            opp_msx_id = msx_ms.get("msx_opportunity_id")
            if opp_msx_id:
                seen_opp_ids.add(opp_msx_id)
            due_date = _parse_msx_date(msx_ms.get("due_date"))

            # Upsert the parent Opportunity using pre-loaded map
            opportunity, opp_is_new = _upsert_opportunity(
                msx_ms, customer.id, existing_opps_map
            )
            if opp_is_new:
                result["opportunities_created"] += 1

            # Find existing milestone from pre-loaded map
            milestone = existing_milestones_map.get(msx_id)

            if milestone:
                _update_milestone_from_msx(milestone, msx_ms, customer.id, due_date, now)
                if opportunity:
                    milestone.opportunity = opportunity
                result["updated"] += 1
            else:
                milestone = _create_milestone_from_msx(
                    msx_ms, customer.id, due_date, now,
                )
                if opportunity:
                    milestone.opportunity = opportunity
                db.session.add(milestone)
                existing_milestones_map[msx_id] = milestone
                result["created"] += 1

        # Handle milestones for this customer that are no longer in the MSX
        # query results.  The sync query uses open_opportunities_only=True
        # and current_fy_only=True, so a milestone can disappear from results
        # for reasons other than completion (opportunity closed, FY filter,
        # account re-parent, etc.).  We must NOT assume "Completed" - just
        # mark them as synced so we know we checked.
        for msx_id, existing in existing_milestones_map.items():
            if existing.customer_id != customer.id:
                continue
            if existing.msx_status not in ACTIVE_STATUSES:
                continue
            if msx_id not in seen_msx_ids:
                existing.last_synced_at = now
                result["deactivated"] += 1

    result["seen_opportunity_ids"] = seen_opp_ids

    try:
        db.session.commit()
        result["success"] = True
    except Exception as e:
        db.session.rollback()
        result["error"] = f"Database error: {str(e)}"
        logger.exception(f"Error saving milestones for customer {customer.id}")
    
    return result


def _sync_stale_opportunity_milestones(
    seen_opp_ids: set,
) -> Generator[Tuple[int, int, str], None, Dict[str, Any]]:
    """
    Refresh milestones for opportunities not covered by the active sync.

    The active sync uses open_opportunities_only=True and
    current_fy_only=True, so it skips milestones on closed/won/lost
    opportunities and those outside the current fiscal year.  This
    function finds those stale opportunities in our database and fetches
    fresh milestone data from MSX so our metrics stay accurate.

    Args:
        seen_opp_ids: MSX opportunity GUIDs already covered by the
            active sync.

    Yields:
        (current, total, customer_name) tuples for progress reporting.

    Returns (via generator .value after StopIteration):
        Dict with success, milestones_updated, opportunities_refreshed.
    """
    result = {
        "success": True,
        "milestones_updated": 0,
        "opportunities_refreshed": 0,
        "customers_queried": 0,
        "error": "",
    }

    # Find opportunities in our DB that weren't seen in the active sync
    # and have at least one milestone with an MSX ID.
    stale_q = (
        db.session.query(Opportunity)
        .join(Milestone, Milestone.opportunity_id == Opportunity.id)
        .filter(
            Opportunity.msx_opportunity_id.isnot(None),
            Opportunity.customer_id.isnot(None),
            Milestone.msx_milestone_id.isnot(None),
        )
        .distinct()
    )
    if seen_opp_ids:
        stale_q = stale_q.filter(
            ~Opportunity.msx_opportunity_id.in_(seen_opp_ids)
        )
    stale_opps = stale_q.all()

    if not stale_opps:
        return result

    # Group by customer (only customers with tpid_url)
    customer_ids = {opp.customer_id for opp in stale_opps}
    customers = Customer.query.filter(
        Customer.id.in_(customer_ids),
        Customer.tpid_url.isnot(None),
        Customer.tpid_url != '',
    ).all()

    if not customers:
        return result

    customer_map = {c.id: c for c in customers}

    stale_by_customer: Dict[int, set] = {}
    for opp in stale_opps:
        if opp.customer_id in customer_map:
            stale_by_customer.setdefault(opp.customer_id, set()).add(
                opp.msx_opportunity_id
            )

    total = len(stale_by_customer)
    now = datetime.now(timezone.utc)

    logger.info(
        f"Syncing stale opportunities: {sum(len(v) for v in stale_by_customer.values())} "
        f"opportunities across {total} customers"
    )

    for i, (cust_id, stale_opp_msx_ids) in enumerate(
        stale_by_customer.items(), 1
    ):
        customer = customer_map[cust_id]
        yield (i, total, customer.get_display_name())

        if is_vpn_blocked():
            result["error"] = "VPN blocked during stale opportunity sync"
            break

        account_id = extract_account_id_from_url(customer.tpid_url)
        if not account_id:
            continue

        # Fetch ALL milestones for this account (no open/FY filter)
        fetch_result = get_milestones_by_account(account_id)
        if not fetch_result.get("success"):
            logger.warning(
                f"Stale opp sync failed for {customer.get_display_name()}: "
                f"{fetch_result.get('error')}"
            )
            continue

        result["customers_queried"] += 1

        # Pre-load existing milestones for this customer
        existing_milestones = {
            ms.msx_milestone_id: ms
            for ms in Milestone.query.filter_by(customer_id=cust_id).filter(
                Milestone.msx_milestone_id.isnot(None),
            ).all()
        }

        # Pre-load existing opportunities for update
        existing_opps = {
            opp.msx_opportunity_id: opp
            for opp in Opportunity.query.filter(
                Opportunity.msx_opportunity_id.in_(stale_opp_msx_ids)
            ).all()
        }

        refreshed_opp_ids: set = set()

        with db.session.no_autoflush:
            for msx_ms in fetch_result.get("milestones", []):
                opp_id = msx_ms.get("msx_opportunity_id")
                if opp_id not in stale_opp_msx_ids:
                    continue

                msx_id = msx_ms.get("id")
                if not msx_id:
                    continue

                # Update milestone if it exists in our DB
                milestone = existing_milestones.get(msx_id)
                if milestone:
                    due_date = _parse_msx_date(msx_ms.get("due_date"))
                    _update_milestone_from_msx(
                        milestone, msx_ms, cust_id, due_date, now
                    )
                    result["milestones_updated"] += 1

                # Update the opportunity from expanded data (once per opp)
                if opp_id not in refreshed_opp_ids:
                    _upsert_opportunity(msx_ms, cust_id, existing_opps)
                    refreshed_opp_ids.add(opp_id)
                    result["opportunities_refreshed"] += 1

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.exception(
                f"Error saving stale milestones for customer {cust_id}"
            )

    logger.info(
        f"Stale opportunity sync complete: "
        f"{result['milestones_updated']} milestones updated, "
        f"{result['opportunities_refreshed']} opportunities refreshed"
    )
    return result


def _sync_all_tasks() -> Generator[
    Tuple[int, int, str, str], None, Dict[str, Any]
]:
    """
    Batch-sync all MSX tasks, yielding progress per API batch.

    Collects all milestone MSX IDs across all customers, fetches tasks
    in batched API calls (75 IDs per request), upserting MsxTask records
    after each batch.

    Yields (batch_num, total_batches, info_str) tuples for progress.

    Returns (via generator .value after StopIteration):
        Dict with success, tasks_created, tasks_updated, error.
    """
    result = {"success": False, "tasks_created": 0, "tasks_updated": 0, "error": ""}

    # Collect all synced milestone MSX IDs -> local milestone ID
    all_milestones = Milestone.query.filter(
        Milestone.msx_milestone_id.isnot(None),
    ).all()

    if not all_milestones:
        result["success"] = True
        return result

    ms_id_map: Dict[str, int] = {
        ms.msx_milestone_id.lower(): ms.id for ms in all_milestones
    }

    all_msx_ids = list(ms_id_map.keys())
    batch_size = 75
    total_batches = math.ceil(len(all_msx_ids) / batch_size)

    # Pre-load existing MsxTask records for faster upserts
    existing_tasks_map: Dict[str, MsxTask] = {}
    for task in MsxTask.query.all():
        if task.msx_task_id:
            existing_tasks_map[task.msx_task_id] = task

    cat_lookup = {
        c["value"]: {"name": c["label"], "is_hok": c["is_hok"]}
        for c in TASK_CATEGORIES
    }

    for batch_num in range(total_batches):
        batch_start = batch_num * batch_size
        batch_ids = all_msx_ids[batch_start:batch_start + batch_size]

        yield (
            batch_num + 1,
            total_batches,
            f"Tasks batch {batch_num + 1}/{total_batches} ({len(batch_ids)} milestones)",
            'fetching',
        )

        fetch_result = get_tasks_for_milestones(batch_ids)
        if not fetch_result.get("success"):
            logger.warning(
                f"Task batch {batch_num + 1} failed: {fetch_result.get('error')}"
            )
            if not result["error"]:
                result["error"] = fetch_result.get("error", "Task fetch failed")
            yield (
                batch_num + 1,
                total_batches,
                f"Tasks batch {batch_num + 1}/{total_batches} - failed",
                'error',
            )
            continue

        msx_tasks = fetch_result.get("tasks", [])
        batch_created = 0
        batch_updated = 0
        for t in msx_tasks:
            task_id = t["task_id"]
            milestone_msx_id = t.get("milestone_msx_id", "").lower()
            local_milestone_id = ms_id_map.get(milestone_msx_id)
            if not local_milestone_id:
                continue

            category_code = t.get("task_category")
            cat_info = cat_lookup.get(category_code, {})
            due_date = _parse_msx_date(t.get("due_date"))

            existing = existing_tasks_map.get(task_id)
            if existing:
                existing.subject = t.get("subject") or existing.subject
                existing.description = t.get("description")
                existing.task_category = category_code or existing.task_category
                existing.task_category_name = (
                    cat_info.get("name") or existing.task_category_name
                )
                existing.is_hok = cat_info.get("is_hok", existing.is_hok)
                existing.duration_minutes = (
                    t.get("duration_minutes") or existing.duration_minutes
                )
                existing.due_date = due_date
                existing.msx_task_url = t.get("task_url") or existing.msx_task_url
                existing.milestone_id = local_milestone_id
                result["tasks_updated"] += 1
                batch_updated += 1
            else:
                new_task = MsxTask(
                    msx_task_id=task_id,
                    msx_task_url=t.get("task_url"),
                    subject=t.get("subject", ""),
                    description=t.get("description"),
                    task_category=category_code or 0,
                    task_category_name=cat_info.get("name"),
                    is_hok=cat_info.get("is_hok", False),
                    duration_minutes=t.get("duration_minutes") or 60,
                    due_date=due_date,
                    milestone_id=local_milestone_id,
                )
                db.session.add(new_task)
                existing_tasks_map[task_id] = new_task
                result["tasks_created"] += 1
                batch_created += 1

        yield (
            batch_num + 1,
            total_batches,
            f"Tasks batch {batch_num + 1}/{total_batches} done"
            f" ({batch_created} new, {batch_updated} updated)",
            'ok',
        )

    try:
        db.session.commit()
        result["success"] = True
    except Exception as e:
        db.session.rollback()
        result["error"] = f"Database error saving tasks: {str(e)}"
        logger.exception("Error saving batched tasks")

    return result


def _sync_customer_tasks(
    customer: Customer,
) -> Dict[str, Any]:
    """
    Fetch and upsert the current user's MSX tasks for a customer's milestones.

    Queries MSX for tasks owned by the current user that are linked to any
    of this customer's synced milestones, then creates or updates local
    MsxTask records.

    Args:
        customer: The Customer model instance.

    Returns:
        Dict with:
        - success: bool
        - tasks_created: int
        - tasks_updated: int
        - error: str if failed
    """
    result = {"success": False, "tasks_created": 0, "tasks_updated": 0, "error": ""}

    # Collect all milestone MSX IDs for this customer
    milestones = Milestone.query.filter_by(customer_id=customer.id).filter(
        Milestone.msx_milestone_id.isnot(None),
    ).all()

    if not milestones:
        result["success"] = True
        return result

    # Build a lookup from MSX milestone GUID -> local Milestone.id
    ms_id_map: Dict[str, int] = {
        ms.msx_milestone_id.lower(): ms.id for ms in milestones
    }
    msx_ids = list(ms_id_map.keys())

    # Fetch user's tasks from MSX
    fetch_result = get_tasks_for_milestones(msx_ids)
    if not fetch_result.get("success"):
        result["error"] = fetch_result.get("error", "Task fetch failed")
        return result

    msx_tasks = fetch_result.get("tasks", [])
    if not msx_tasks:
        result["success"] = True
        return result

    # Pre-load existing MsxTask records by msx_task_id
    existing_task_ids = [t["task_id"] for t in msx_tasks]
    existing_tasks_map: Dict[str, MsxTask] = {}
    for task in MsxTask.query.filter(
        MsxTask.msx_task_id.in_(existing_task_ids)
    ).all():
        existing_tasks_map[task.msx_task_id] = task

    # Category lookup for enrichment
    cat_lookup = {
        c["value"]: {"name": c["label"], "is_hok": c["is_hok"]}
        for c in TASK_CATEGORIES
    }

    for t in msx_tasks:
        task_id = t["task_id"]
        milestone_msx_id = t.get("milestone_msx_id", "").lower()
        local_milestone_id = ms_id_map.get(milestone_msx_id)
        if not local_milestone_id:
            continue

        category_code = t.get("task_category")
        cat_info = cat_lookup.get(category_code, {})
        due_date = _parse_msx_date(t.get("due_date"))

        existing = existing_tasks_map.get(task_id)
        if existing:
            # Update
            existing.subject = t.get("subject") or existing.subject
            existing.description = t.get("description")
            existing.task_category = category_code or existing.task_category
            existing.task_category_name = cat_info.get("name") or existing.task_category_name
            existing.is_hok = cat_info.get("is_hok", existing.is_hok)
            existing.duration_minutes = t.get("duration_minutes") or existing.duration_minutes
            existing.due_date = due_date
            existing.msx_task_url = t.get("task_url") or existing.msx_task_url
            existing.milestone_id = local_milestone_id
            result["tasks_updated"] += 1
        else:
            new_task = MsxTask(
                msx_task_id=task_id,
                msx_task_url=t.get("task_url"),
                subject=t.get("subject", ""),
                description=t.get("description"),
                task_category=category_code or 0,
                task_category_name=cat_info.get("name"),
                is_hok=cat_info.get("is_hok", False),
                duration_minutes=t.get("duration_minutes") or 60,
                due_date=due_date,
                milestone_id=local_milestone_id,
                # note_id left NULL — synced tasks aren't linked to a note
            )
            db.session.add(new_task)
            existing_tasks_map[task_id] = new_task
            result["tasks_created"] += 1

    try:
        db.session.commit()
        result["success"] = True
    except Exception as e:
        db.session.rollback()
        result["error"] = f"Database error saving tasks: {str(e)}"
        logger.exception(f"Error saving tasks for customer {customer.id}")

    return result


def sync_customer_comments(customer: Customer) -> Dict[str, Any]:
    """Sync forecast comments from MSX for all milestones of a single customer.

    Fetches comments individually for each milestone that has an msx_milestone_id.

    Args:
        customer: The Customer model instance.

    Returns:
        Dict with success, comments_synced, comments_failed.
    """
    result = {
        "success": True,
        "comments_synced": 0,
        "comments_failed": 0,
    }

    milestones = Milestone.query.filter(
        Milestone.customer_id == customer.id,
        Milestone.msx_milestone_id.isnot(None),
    ).all()

    if not milestones:
        return result

    now = datetime.now(timezone.utc)

    for ms in milestones:
        try:
            comment_result = get_milestone_comments(ms.msx_milestone_id)
            if comment_result.get("success"):
                ms.cached_comments_json = json.dumps(
                    comment_result.get("comments", [])
                )
                ms.details_fetched_at = now
                result["comments_synced"] += 1
            else:
                result["comments_failed"] += 1
                logger.warning(
                    "Failed to fetch comments for milestone %s: %s",
                    ms.msx_milestone_id, comment_result.get("error"),
                )
        except Exception:
            result["comments_failed"] += 1
            logger.exception(
                "Error fetching comments for milestone %s",
                ms.msx_milestone_id,
            )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        result["success"] = False
        logger.exception("Error committing milestone comments for customer %s", customer.id)

    return result


def sync_customer_opportunities(customer: Customer) -> Dict[str, Any]:
    """Sync open opportunities from MSX for a single customer.

    Fetches opportunities directly from the MSX opportunities endpoint
    and upserts them. This catches opportunities that have no milestones
    (which the milestone sync would miss).

    Args:
        customer: The Customer model instance (needs tpid_url).

    Returns:
        Dict with success, created, updated counts.
    """
    result = {"success": False, "created": 0, "updated": 0}

    account_id = extract_account_id_from_url(customer.tpid_url)
    if not account_id:
        result["error"] = "Could not extract account ID from tpid_url"
        return result

    opp_result = get_opportunities_by_account(account_id, open_only=True)
    if not opp_result.get("success"):
        result["error"] = opp_result.get("error", "Unknown error")
        return result

    msx_opps = opp_result.get("opportunities", [])
    if not msx_opps:
        result["success"] = True
        return result

    # Pre-load existing opportunities for this customer
    existing_map = {
        opp.msx_opportunity_id: opp
        for opp in Opportunity.query.filter_by(customer_id=customer.id).all()
        if opp.msx_opportunity_id
    }

    for msx_opp in msx_opps:
        opp_id = msx_opp.get("id")
        if not opp_id:
            continue

        existing = existing_map.get(opp_id)
        if existing:
            existing.name = msx_opp.get("name") or existing.name
            existing.opportunity_number = msx_opp.get("number") or existing.opportunity_number
            existing.statecode = msx_opp.get("statecode")
            existing.state = msx_opp.get("state") or existing.state
            existing.status_reason = msx_opp.get("status_reason") or existing.status_reason
            existing.estimated_value = msx_opp.get("estimated_value")
            existing.estimated_close_date = msx_opp.get("estimated_close_date")
            existing.owner_name = msx_opp.get("owner") or existing.owner_name
            existing.msx_url = msx_opp.get("url") or existing.msx_url
            result["updated"] += 1
        else:
            opp = Opportunity(
                msx_opportunity_id=opp_id,
                name=msx_opp.get("name", "Unknown Opportunity"),
                customer_id=customer.id,
                opportunity_number=msx_opp.get("number"),
                statecode=msx_opp.get("statecode"),
                state=msx_opp.get("state"),
                status_reason=msx_opp.get("status_reason"),
                estimated_value=msx_opp.get("estimated_value"),
                estimated_close_date=msx_opp.get("estimated_close_date"),
                owner_name=msx_opp.get("owner"),
                msx_url=msx_opp.get("url"),
            )
            db.session.add(opp)
            existing_map[opp_id] = opp
            result["created"] += 1

    try:
        db.session.commit()
        result["success"] = True
    except Exception:
        db.session.rollback()
        result["error"] = "Database error saving opportunities"
        logger.exception("Error committing opportunities for customer %s", customer.id)

    return result


def _sync_team_milestone_comments(
    since: Optional[datetime] = None,
) -> Generator[
    Tuple[int, int, str], None, Dict[str, Any]
]:
    """
    Sync forecast comments from MSX for milestones where the user is on the team.

    Skips milestones whose comments were already cached during this sync
    (details_fetched_at >= since).

    Args:
        since: If provided, skip milestones with details_fetched_at >= this time
               (they already got comments from the bulk milestone fetch).

    Yields (current, total, milestone_title) tuples for progress reporting,
    then returns the final result dict via generator return value.

    Returns (via generator .value after StopIteration):
        Dict with:
        - success: bool
        - comments_synced: int (milestones whose comments were updated)
        - comments_skipped: int (already cached from bulk fetch)
        - comments_failed: int
        - error: str if completely failed
    """
    result = {
        "success": True,
        "comments_synced": 0,
        "comments_skipped": 0,
        "comments_failed": 0,
        "error": "",
    }

    # Get on-my-team milestones with MSX IDs that were part of this sync.
    # The bulk fetch (Phase 1) uses open_opportunities_only + current_fy_only,
    # so we limit Phase 4 to milestones with a recent last_synced_at to avoid
    # individually fetching comments for old/closed milestones.
    since_naive = since.replace(tzinfo=None) if (since and since.tzinfo) else since
    base_filter = Milestone.query.filter(
        Milestone.on_my_team.is_(True),
        Milestone.msx_milestone_id.isnot(None),
    )
    if since_naive:
        base_filter = base_filter.filter(Milestone.last_synced_at >= since_naive)
    team_milestones = base_filter.all()

    if not team_milestones:
        return result

    # Filter out milestones already cached during this sync
    if since_naive:
        need_fetch = [
            ms for ms in team_milestones
            if not ms.details_fetched_at or ms.details_fetched_at < since_naive
        ]
        result["comments_skipped"] = len(team_milestones) - len(need_fetch)
    else:
        need_fetch = team_milestones

    if not need_fetch:
        logger.info(
            f"All {len(team_milestones)} team milestones already have "
            f"fresh comments from bulk fetch - skipping comment sync"
        )
        return result

    total = len(need_fetch)
    now = datetime.now(timezone.utc)

    for i, ms in enumerate(need_fetch, 1):
        yield (i, total, ms.title or ms.milestone_number or "Unknown")

        if is_vpn_blocked():
            result["error"] = "VPN blocked during comment sync"
            break

        try:
            comment_result = get_milestone_comments(ms.msx_milestone_id)
            if comment_result.get("success"):
                ms.cached_comments_json = json.dumps(
                    comment_result.get("comments", [])
                )
                ms.details_fetched_at = now
                result["comments_synced"] += 1
            else:
                result["comments_failed"] += 1
                logger.warning(
                    f"Failed to fetch comments for milestone {ms.msx_milestone_id}: "
                    f"{comment_result.get('error')}"
                )
        except Exception as e:
            result["comments_failed"] += 1
            logger.exception(
                f"Error fetching comments for milestone {ms.msx_milestone_id}"
            )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        result["success"] = False
        result["error"] = f"Database error saving comments: {str(e)}"
        logger.exception("Error committing milestone comments")

    return result


def _update_milestone_from_msx(
    milestone: Milestone,
    msx_data: Dict[str, Any],
    customer_id: int,
    due_date: Optional[datetime],
    now: datetime,
) -> None:
    """Update an existing milestone with fresh data from MSX."""
    milestone.title = msx_data.get("name") or milestone.title
    milestone.milestone_number = msx_data.get("number") or milestone.milestone_number
    milestone.msx_status = msx_data.get("status") or milestone.msx_status
    milestone.msx_status_code = msx_data.get("status_code")
    milestone.customer_commitment = msx_data.get("customer_commitment") or milestone.customer_commitment
    milestone.opportunity_name = msx_data.get("opportunity_name") or milestone.opportunity_name
    milestone.workload = msx_data.get("workload") or milestone.workload
    milestone.monthly_usage = msx_data.get("monthly_usage")
    milestone.due_date = due_date
    milestone.dollar_value = msx_data.get("dollar_value")
    milestone.url = msx_data.get("url") or milestone.url
    milestone.customer_id = customer_id
    milestone.last_synced_at = now
    milestone.committed_at = _parse_msx_date(msx_data.get("committed_on"))
    milestone.completed_at = _parse_msx_date(msx_data.get("completed_on"))
    milestone.msx_created_on = _parse_msx_date(msx_data.get("created_on"))
    milestone.msx_modified_on = _parse_msx_date(msx_data.get("modified_on"))
    # Cache comments if included in the bulk fetch (None = field not requested,
    # so we only update when the key is present in the dict)
    if "comments_json" in msx_data:
        milestone.cached_comments_json = msx_data["comments_json"] or "[]"
        milestone.details_fetched_at = now


def _create_milestone_from_msx(
    msx_data: Dict[str, Any],
    customer_id: int,
    due_date: Optional[datetime],
    now: datetime,
) -> Milestone:
    """Create a new Milestone from MSX data."""
    ms = Milestone(
        msx_milestone_id=msx_data["id"],
        milestone_number=msx_data.get("number", ""),
        url=msx_data.get("url", ""),
        title=msx_data.get("name", ""),
        msx_status=msx_data.get("status", "Unknown"),
        msx_status_code=msx_data.get("status_code"),
        customer_commitment=msx_data.get("customer_commitment", ""),
        opportunity_name=msx_data.get("opportunity_name", ""),
        workload=msx_data.get("workload", ""),
        monthly_usage=msx_data.get("monthly_usage"),
        due_date=due_date,
        dollar_value=msx_data.get("dollar_value"),
        last_synced_at=now,
        customer_id=customer_id,
        committed_at=_parse_msx_date(msx_data.get("committed_on")),
        completed_at=_parse_msx_date(msx_data.get("completed_on")),
        msx_created_on=_parse_msx_date(msx_data.get("created_on")),
        msx_modified_on=_parse_msx_date(msx_data.get("modified_on")),
    )
    # Cache comments if included in the bulk fetch
    if "comments_json" in msx_data:
        ms.cached_comments_json = msx_data["comments_json"] or "[]"
        ms.details_fetched_at = now
    return ms


def _upsert_opportunity(
    msx_data: Dict[str, Any],
    customer_id: int,
    existing_opps_map: Optional[Dict[str, Opportunity]] = None,
) -> Tuple[Optional[Opportunity], bool]:
    """
    Upsert an Opportunity record from milestone data.
    
    The milestone API returns the parent opportunity GUID and name.
    We create or update the Opportunity record so milestones can FK to it.
    
    When existing_opps_map is provided, uses it for lookups instead of
    querying the database per-row (avoids autoflush hangs).
    
    Args:
        msx_data: Milestone dict from MSX API (contains msx_opportunity_id, opportunity_name).
        customer_id: The customer this opportunity belongs to.
        existing_opps_map: Optional pre-loaded {msx_opportunity_id: Opportunity} dict.
            If provided, new opportunities are also added to this map.
        
    Returns:
        Tuple of (Opportunity instance or None, True if newly created).
    """
    msx_opp_id = msx_data.get("msx_opportunity_id")
    if not msx_opp_id:
        return None, False
    
    opp_name = msx_data.get("opportunity_name", "Unknown Opportunity")

    # Use pre-loaded map if available, otherwise fall back to DB query
    if existing_opps_map is not None:
        opportunity = existing_opps_map.get(msx_opp_id)
    else:
        opportunity = Opportunity.query.filter_by(msx_opportunity_id=msx_opp_id).first()

    # Expanded opportunity fields from $expand=msp_OpportunityId
    opp_number = msx_data.get("opportunity_number") or None
    opp_statecode = msx_data.get("opportunity_statecode")
    opp_state = msx_data.get("opportunity_state") or None
    opp_status_reason = msx_data.get("opportunity_status_reason") or None
    opp_value = msx_data.get("opportunity_estimated_value")
    opp_close_date = msx_data.get("opportunity_estimated_close_date") or None
    opp_owner = msx_data.get("opportunity_owner") or None
    opp_customer_need = msx_data.get("opportunity_customer_need") or None
    opp_description = msx_data.get("opportunity_description") or None
    opp_compete = msx_data.get("opportunity_compete_threat") or None
    opp_url = build_opportunity_url(msx_opp_id)

    if opportunity:
        # Update fields from the expanded opportunity data
        opportunity.name = opp_name or opportunity.name
        opportunity.customer_id = customer_id
        if opp_number:
            opportunity.opportunity_number = opp_number
        if opp_statecode is not None:
            opportunity.statecode = opp_statecode
            opportunity.state = opp_state
        if opp_status_reason:
            opportunity.status_reason = opp_status_reason
        if opp_value is not None:
            opportunity.estimated_value = opp_value
        if opp_close_date:
            opportunity.estimated_close_date = opp_close_date
        if opp_owner:
            opportunity.owner_name = opp_owner
        if opp_customer_need:
            opportunity.customer_need = opp_customer_need
        if opp_description:
            opportunity.description = opp_description
        if opp_compete:
            opportunity.compete_threat = opp_compete
        opportunity.msx_url = opp_url
        return opportunity, False
    else:
        opportunity = Opportunity(
            msx_opportunity_id=msx_opp_id,
            name=opp_name,
            customer_id=customer_id,
            opportunity_number=opp_number,
            statecode=opp_statecode,
            state=opp_state,
            status_reason=opp_status_reason,
            estimated_value=opp_value,
            estimated_close_date=opp_close_date,
            owner_name=opp_owner,
            customer_need=opp_customer_need,
            description=opp_description,
            compete_threat=opp_compete,
            msx_url=opp_url,
        )
        db.session.add(opportunity)
        # Track in map so later milestones sharing this opportunity find it
        if existing_opps_map is not None:
            existing_opps_map[msx_opp_id] = opportunity
        else:
            # Flush to get the ID assigned so we can FK to it (legacy path)
            db.session.flush()
        return opportunity, True


def _parse_msx_date(date_str: Optional[str]) -> Optional[datetime]:
    """
    Parse a date string from MSX OData response.
    
    MSX returns dates in ISO 8601 format like "2025-06-30T00:00:00Z".
    
    Args:
        date_str: Date string from MSX, or None.
        
    Returns:
        datetime object or None if parsing fails.
    """
    if not date_str:
        return None
    try:
        # Handle ISO 8601 format with or without Z suffix
        date_str = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(date_str.replace("+00:00", ""))
    except (ValueError, AttributeError):
        logger.warning(f"Could not parse MSX date: {date_str}")
        return None


def _sync_milestone_audits() -> Generator:
    """
    Fetch audit trail from MSX for milestones modified in the last 2 weeks
    and save individual field changes to the MilestoneAudit table.

    Yields:
        Tuples of (batch_completed, total_batches) for progress reporting.

    Returns (via StopIteration.value):
        Dict with counts: audits_fetched, fields_saved, skipped.
    """
    two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)
    stats = {"audits_fetched": 0, "fields_saved": 0, "skipped": 0}

    # Find milestones with MSX IDs that were modified recently.
    # Only fall back to updated_at when msx_modified_on is NULL (avoids
    # matching every milestone that was just touched by the sync).
    milestones = (
        Milestone.query
        .filter(
            Milestone.msx_milestone_id.isnot(None),
            db.or_(
                Milestone.msx_modified_on >= two_weeks_ago,
                db.and_(
                    Milestone.msx_modified_on.is_(None),
                    Milestone.updated_at >= two_weeks_ago,
                ),
            ),
        )
        .all()
    )
    if not milestones:
        logger.info("No recently-modified milestones - skipping audit sync")
        return stats

    guid_to_id = {
        ms.msx_milestone_id.lower(): ms.id for ms in milestones
    }
    guids = list(guid_to_id.keys())
    logger.info(f"Fetching audits for {len(guids)} recently-modified milestones")

    # Use a thread-safe queue to relay progress from the parallel workers
    progress_q = queue.Queue()

    def _on_batch(completed: int, total: int) -> None:
        progress_q.put((completed, total))

    # Start the parallel fetch in a background thread so we can yield progress
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(get_milestone_audits, guids, 5, _on_batch)
        # Yield progress as batches complete
        while not future.done():
            try:
                batch_done, batch_total = progress_q.get(timeout=0.5)
                yield (batch_done, batch_total)
            except queue.Empty:
                continue
        # Drain any remaining progress events
        while not progress_q.empty():
            batch_done, batch_total = progress_q.get_nowait()
            yield (batch_done, batch_total)
        result = future.result()

    if not result.get("success"):
        logger.warning(f"Audit fetch failed: {result.get('error')}")
        return stats

    # Collect existing (audit_id, field_name) pairs so we can skip duplicates
    existing_pairs = set(
        (row[0], row[1]) for row in
        db.session.query(MilestoneAudit.audit_id, MilestoneAudit.field_name).all()
    )

    new_records = []
    for guid, audits in result["audits"].items():
        milestone_id = guid_to_id.get(guid.lower())
        if not milestone_id:
            continue
        for audit in audits:
            stats["audits_fetched"] += 1
            audit_id = audit["audit_id"]

            # Parse changedata JSON
            change_data_str = audit.get("change_data", "")
            if not change_data_str:
                continue
            try:
                change_data = json.loads(change_data_str)
            except (json.JSONDecodeError, TypeError):
                # Relationship changes (N:N links) aren't JSON - skip them
                continue

            changed_on = _parse_msx_date(audit.get("changed_on"))
            changed_by = audit.get("changed_by", "")
            operation = audit.get("operation", 2)

            for attr in change_data.get("changedAttributes", []):
                field_name = attr.get("logicalName", "")
                if not field_name:
                    continue
                # Only save fields we have a human-readable label for
                if field_name not in MilestoneAudit.FIELD_LABELS:
                    continue
                if (audit_id, field_name) in existing_pairs:
                    stats["skipped"] += 1
                    continue
                new_records.append(MilestoneAudit(
                    milestone_id=milestone_id,
                    audit_id=audit_id,
                    changed_on=changed_on,
                    changed_by=changed_by,
                    operation=operation,
                    field_name=field_name,
                    old_value=str(attr.get("oldValue", "")) if attr.get("oldValue") is not None else None,
                    new_value=str(attr.get("newValue", "")) if attr.get("newValue") is not None else None,
                ))
                stats["fields_saved"] += 1
                existing_pairs.add((audit_id, field_name))

    if new_records:
        try:
            db.session.add_all(new_records)
            db.session.commit()
            logger.info(
                f"Audit sync: {stats['fields_saved']} field changes saved, "
                f"{stats['skipped']} audits skipped (already existed)"
            )
        except Exception as e:
            db.session.rollback()
            logger.exception("Error saving milestone audits")
            stats["fields_saved"] = 0
    else:
        logger.info("No new audit records to save")

    return stats


def _update_team_memberships() -> None:
    """
    Update the on_my_team flag for all milestones based on MSX access teams.

    Makes one API call to get all milestone team memberships, then updates
    the on_my_team column.  If the API returned a complete data set
    (pagination_complete=True), milestones NOT in the set are marked False.
    If pagination was interrupted, we only ADD memberships — never remove —
    to avoid incorrectly stripping flags from partial data.
    """
    try:
        result = get_my_milestone_team_ids()
        if not result.get("success"):
            logger.warning(
                f"Could not fetch team memberships: {result.get('error')}"
            )
            return

        my_ids = result["milestone_ids"]
        pagination_complete = result.get("pagination_complete", True)
        logger.info(
            f"Updating on_my_team for {len(my_ids)} milestones "
            f"(pagination_complete={pagination_complete})"
        )

        if pagination_complete:
            # Full data — safe to set unmatched milestones to False,
            # but only those that actually have an MSX ID (local-only are untouched)
            Milestone.query.filter(
                Milestone.msx_milestone_id.isnot(None)
            ).update({Milestone.on_my_team: False})

        # Set matched milestones to True (always safe, even with partial data)
        if my_ids:
            Milestone.query.filter(
                db.func.lower(Milestone.msx_milestone_id).in_(my_ids)
            ).update({Milestone.on_my_team: True}, synchronize_session='fetch')

        db.session.commit()
    except Exception as e:
        logger.exception("Error updating team memberships")
        db.session.rollback()


def _update_deal_team_memberships() -> None:
    """
    Update the on_deal_team flag for all opportunities based on MSX deal teams.

    Makes one API call to get all deal team memberships, then bulk-updates
    the on_deal_team column. Mirrors _update_team_memberships() logic.
    """
    try:
        result = get_my_deal_team_ids()
        if not result.get("success"):
            logger.warning(
                f"Could not fetch deal team memberships: {result.get('error')}"
            )
            return

        my_ids = result["opportunity_ids"]
        logger.info(f"Updating on_deal_team for {len(my_ids)} opportunities")

        # Clear all, then set matched ones
        Opportunity.query.filter(
            Opportunity.msx_opportunity_id.isnot(None)
        ).update({Opportunity.on_deal_team: False})

        if my_ids:
            Opportunity.query.filter(
                db.func.lower(Opportunity.msx_opportunity_id).in_(my_ids)
            ).update({Opportunity.on_deal_team: True}, synchronize_session='fetch')

        db.session.commit()
    except Exception as e:
        logger.exception("Error updating deal team memberships")
        db.session.rollback()


def get_milestone_tracker_data() -> Dict[str, Any]:
    """
    Get milestone data formatted for the tracker page.
    
    Returns active milestones grouped by urgency, sorted by dollar value
    (largest first within each group).
    
    Returns:
        Dict with:
        - milestones: list of milestone dicts with customer/seller info
        - summary: dict with totals and counts
        - last_sync: datetime of most recent sync, or None
    """
    # Query all milestones with eager-loaded relationships
    milestones = (
        Milestone.query
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
            db.joinedload(Milestone.customer).joinedload(Customer.territory),
            db.joinedload(Milestone.opportunity),
        )
        .all()
    )
    
    # Build the data structure
    now = datetime.now(timezone.utc)
    tracker_items = []
    
    total_monthly_usage = 0
    past_due_count = 0
    this_week_count = 0
    
    for ms in milestones:
        urgency = ms.due_date_urgency
        if urgency == 'past_due':
            past_due_count += 1
        elif urgency == 'this_week':
            this_week_count += 1
        
        if ms.monthly_usage and ms.monthly_usage > 0:
            total_monthly_usage += ms.monthly_usage
        
        # Days until due
        days_until = None
        fiscal_quarter = ""
        fiscal_year = ""
        if ms.due_date:
            due = ms.due_date if ms.due_date.tzinfo else ms.due_date.replace(tzinfo=timezone.utc)
            days_until = (due - now).days
            # Microsoft fiscal year starts July 1
            # Q1 = Jul-Sep, Q2 = Oct-Dec, Q3 = Jan-Mar, Q4 = Apr-Jun
            month = ms.due_date.month
            year = ms.due_date.year
            if month >= 7:
                fy = year + 1
                q = 1 if month <= 9 else 2
            else:
                fy = year
                q = 3 if month <= 3 else 4
            fiscal_quarter = f"FY{fy % 100:02d} Q{q}"
            fiscal_year = f"FY{fy % 100:02d}"
        
        # Extract area prefix from workload (e.g., "Infra" from "Infra: Windows")
        workload_area = ""
        if ms.workload and ':' in ms.workload:
            workload_area = ms.workload.split(':', 1)[0].strip()
        elif ms.workload:
            workload_area = ms.workload.strip()
        
        tracker_items.append({
            "id": ms.id,
            "title": ms.display_text,
            "milestone_number": ms.milestone_number,
            "status": ms.msx_status,
            "status_sort": ms.status_sort_order,
            "opportunity_name": ms.opportunity_name,
            "workload": ms.workload,
            "workload_area": workload_area,
            "monthly_usage": ms.monthly_usage,
            "due_date": ms.due_date,
            "dollar_value": ms.dollar_value,
            "days_until_due": days_until,
            "fiscal_quarter": fiscal_quarter,
            "fiscal_year": fiscal_year,
            "urgency": urgency,
            "url": ms.url,
            "msx_milestone_id": ms.msx_milestone_id,
            "last_synced_at": ms.last_synced_at,
            "on_my_team": ms.on_my_team,
            "customer_commitment": ms.customer_commitment or "",
            "customer": {
                "id": ms.customer.id if ms.customer else None,
                "name": ms.customer.get_display_name() if ms.customer else "Unknown",
                "favicon_b64": ms.customer.favicon_b64 if ms.customer else None,
                "tpid_url": ms.customer.tpid_url if ms.customer else None,
            } if ms.customer else None,
            "seller": {
                "id": ms.customer.seller.id,
                "name": ms.customer.seller.name,
            } if ms.customer and ms.customer.seller else None,
            "territory": {
                "id": ms.customer.territory.id,
                "name": ms.customer.territory.name,
            } if ms.customer and ms.customer.territory else None,
            "opportunity": {
                "id": ms.opportunity.id,
                "name": ms.opportunity.name,
            } if ms.opportunity else None,
        })
    
    # Default sort: largest monthly usage first
    tracker_items.sort(key=lambda x: -(x["monthly_usage"] or 0))
    
    # Get last sync time
    last_sync = (
        db.session.query(db.func.max(Milestone.last_synced_at))
        .filter(Milestone.last_synced_at.isnot(None))
        .scalar()
    )
    
    # Get unique sellers for filter dropdown
    seller_ids = set()
    sellers = []
    for item in tracker_items:
        if item["seller"] and item["seller"]["id"] not in seller_ids:
            seller_ids.add(item["seller"]["id"])
            sellers.append(item["seller"])
    sellers.sort(key=lambda s: s["name"])
    
    # Get unique workload areas for filter dropdown
    areas = sorted(set(
        item["workload_area"] for item in tracker_items
        if item["workload_area"]
    ))
    
    # Get unique fiscal quarters for filter dropdown, sorted chronologically
    quarters = sorted(set(
        item["fiscal_quarter"] for item in tracker_items
        if item["fiscal_quarter"]
    ))
    
    return {
        "milestones": tracker_items,
        "summary": {
            "total_count": len(tracker_items),
            "total_monthly_usage": total_monthly_usage,
            "past_due_count": past_due_count,
            "this_week_count": this_week_count,
        },
        "last_sync": last_sync,
        "sellers": sellers,
        "areas": areas,
        "quarters": quarters,
    }


def get_milestone_tracker_data_for_seller(seller_id: int) -> Dict[str, Any]:
    """
    Get milestone tracker data filtered for a specific seller's customers.
    
    Returns the same format as get_milestone_tracker_data, but only includes
    milestones for customers assigned to the specified seller.
    
    Args:
        seller_id: The ID of the seller to filter by
        
    Returns:
        Dict with milestones, summary, areas, quarters (no sellers list needed)
    """
    from app.models import Seller
    
    # Get seller's customer IDs
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return {
            "milestones": [],
            "summary": {
                "total_count": 0,
                "total_monthly_usage": 0,
                "past_due_count": 0,
                "this_week_count": 0,
            },
            "areas": [],
            "quarters": [],
        }
    
    customer_ids = [c.id for c in seller.customers]
    if not customer_ids:
        return {
            "milestones": [],
            "summary": {
                "total_count": 0,
                "total_monthly_usage": 0,
                "past_due_count": 0,
                "this_week_count": 0,
            },
            "areas": [],
            "quarters": [],
        }
    
    # Query milestones for this seller's customers (only ones we're on the team for)
    milestones = (
        Milestone.query
        .filter(
            Milestone.customer_id.in_(customer_ids),
            Milestone.on_my_team == True,
        )
        .options(
            db.joinedload(Milestone.customer).joinedload(Customer.seller),
            db.joinedload(Milestone.customer).joinedload(Customer.territory),
            db.joinedload(Milestone.opportunity),
        )
        .all()
    )
    
    # Build the data structure (same format as get_milestone_tracker_data)
    now = datetime.now(timezone.utc)
    tracker_items = []
    
    total_monthly_usage = 0
    past_due_count = 0
    this_week_count = 0
    
    for ms in milestones:
        urgency = ms.due_date_urgency
        if urgency == 'past_due':
            past_due_count += 1
        elif urgency == 'this_week':
            this_week_count += 1
        
        if ms.monthly_usage and ms.monthly_usage > 0:
            total_monthly_usage += ms.monthly_usage
        
        # Days until due
        days_until = None
        fiscal_quarter = ""
        if ms.due_date:
            due = ms.due_date if ms.due_date.tzinfo else ms.due_date.replace(tzinfo=timezone.utc)
            days_until = (due - now).days
            # Microsoft fiscal year starts July 1
            month = ms.due_date.month
            year = ms.due_date.year
            if month >= 7:
                fy = year + 1
                q = 1 if month <= 9 else 2
            else:
                fy = year
                q = 3 if month <= 3 else 4
            fiscal_quarter = f"FY{fy % 100:02d} Q{q}"
        
        # Extract area prefix from workload
        workload_area = ""
        if ms.workload and ':' in ms.workload:
            workload_area = ms.workload.split(':', 1)[0].strip()
        elif ms.workload:
            workload_area = ms.workload.strip()
        
        tracker_items.append({
            "id": ms.id,
            "title": ms.display_text,
            "milestone_number": ms.milestone_number,
            "status": ms.msx_status,
            "status_sort": ms.status_sort_order,
            "opportunity_name": ms.opportunity_name,
            "workload": ms.workload,
            "workload_area": workload_area,
            "monthly_usage": ms.monthly_usage,
            "due_date": ms.due_date,
            "dollar_value": ms.dollar_value,
            "days_until_due": days_until,
            "fiscal_quarter": fiscal_quarter,
            "urgency": urgency,
            "url": ms.url,
            "msx_milestone_id": ms.msx_milestone_id,
            "on_my_team": ms.on_my_team,
            "customer_commitment": ms.customer_commitment or "",
            "customer": {
                "id": ms.customer.id if ms.customer else None,
                "name": ms.customer.get_display_name() if ms.customer else "Unknown",
                "favicon_b64": ms.customer.favicon_b64 if ms.customer else None,
                "tpid_url": ms.customer.tpid_url if ms.customer else None,
            } if ms.customer else None,
            "seller": {
                "id": ms.customer.seller.id,
                "name": ms.customer.seller.name,
            } if ms.customer and ms.customer.seller else None,
        })
    
    # Sort by due date (closest first, nulls last) to match seller view expectation
    far_future = datetime(9999, 12, 31, tzinfo=timezone.utc)
    tracker_items.sort(key=lambda x: x["due_date"].replace(tzinfo=timezone.utc) if x["due_date"] else far_future)
    
    # Get unique workload areas
    areas = sorted(set(
        item["workload_area"] for item in tracker_items
        if item["workload_area"]
    ))
    
    # Get unique fiscal quarters
    quarters = sorted(set(
        item["fiscal_quarter"] for item in tracker_items
        if item["fiscal_quarter"]
    ))
    
    return {
        "milestones": tracker_items,
        "summary": {
            "total_count": len(tracker_items),
            "total_monthly_usage": total_monthly_usage,
            "past_due_count": past_due_count,
            "this_week_count": this_week_count,
        },
        "areas": areas,
        "quarters": quarters,
    }
