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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Generator, Tuple

from app.models import (
    db, CaipActivity, Customer, Milestone, MilestoneAudit, MsxTask,
    Opportunity, User, SyncStatus,
)
from app.services.milestone_search import get_milestone_search_text
from app.services.msx_api import (
    batch_get_milestones,
    batch_get_milestones_by_id,
    batch_get_opportunities,
    extract_account_id_from_url,
    get_milestones_by_account,
    get_milestone_audits,
    get_milestone_comments,
    get_my_deal_team_ids,
    get_my_milestone_team_ids,
    get_activities_for_milestones,
    get_opportunities_by_account,
    get_tasks_for_milestones,
    build_milestone_url,
    build_opportunity_url,
    build_task_url,
    test_connection,
    TASK_CATEGORIES,
    HVA_TASK_CATEGORIES,
)
from app.services.msx_auth import is_vpn_blocked, set_vpn_blocked

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
    
    # Mark sync as started so interrupted syncs are detectable
    SyncStatus.mark_started('milestones')

    logger.info(f"Starting milestone sync for {len(customers)} customers")

    all_seen_msx_ids = set()

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
                all_seen_msx_ids.update(
                    customer_result.get("seen_milestone_ids", set())
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

    # Sync milestones the active sync didn't return (out-of-FY, closed opps,
    # status reset, etc.) - milestone-level so sibling milestones on the same
    # opp don't get skipped.
    stale_gen = _sync_stale_milestones(all_seen_msx_ids)
    try:
        while True:
            next(stale_gen)
    except StopIteration as stop:
        stale_result = stop.value
        results["stale_milestones_updated"] = stale_result.get(
            "milestones_updated", 0
        )
        if not stale_result.get("success"):
            results["errors"].append(stale_result.get("error") or "Stale refresh failed.")

    # Calculate duration
    results["duration_seconds"] = (datetime.now(timezone.utc) - start_time).total_seconds()
    
    # Hydrate all team milestones before syncing their evidence.
    team_result = _sync_team_milestones()
    if not team_result.get("success"):
        results["errors"].append(team_result.get("error"))
    task_gen = _sync_all_tasks()
    try:
        while True:
            next(task_gen)
    except StopIteration as stop:
        task_result = stop.value
        results["tasks_created"] += task_result.get("tasks_created", 0)
        results["tasks_updated"] += task_result.get("tasks_updated", 0)
        if not task_result.get("success"):
            results["errors"].append(task_result.get("error") or "Task sync failed.")
    activity_result = _sync_caip_activities()
    results["activities_synced"] = activity_result.get("activities_synced", 0)
    if not activity_result.get("success"):
        results["errors"].append(activity_result.get("error") or "Activity sync failed.")
    _update_deal_team_memberships()
    
    # Sync comments for milestones I'm on the team for
    comment_gen = _sync_team_milestone_comments(since=start_time)
    try:
        while True:
            next(comment_gen)
    except StopIteration as stop:
        comment_result = stop.value
        results["comments_synced"] = comment_result.get("comments_synced", 0)
        if not comment_result.get("success") or comment_result.get("comments_failed"):
            results["errors"].append(comment_result.get("error") or "Comment sync failed.")

    # Sync audit trail for recently-modified milestones
    audit_gen = _sync_milestone_audits()
    try:
        while True:
            next(audit_gen)
    except StopIteration as stop:
        audit_result = stop.value
        results["audit_fields_saved"] = audit_result.get("fields_saved", 0)
        if audit_result.get("success") is False:
            results["errors"].append(audit_result["error"])

    results["success"] = results["customers_failed"] == 0 and not results["errors"]
    if not results["success"]:
        results["error"] = "; ".join(results["errors"][:5])
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


    return now


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
    # Preflight: one lightweight MSX call (WhoAmI) to confirm we can
    # actually reach MSX before fanning out into dozens of batched
    # queries. Off VPN/corpnet this fails in ~1s, so we bail immediately
    # with a clear banner instead of dispatching every batch first and
    # only noticing 15s later. Non-VPN failures fall through to the normal
    # flow, whose per-phase guards handle them as before.
    # -----------------------------------------------------------------
    conn = test_connection(quick=True)
    if not conn.get('success') and (conn.get('vpn_blocked') or is_vpn_blocked()):
        set_vpn_blocked("Couldn't reach MSX - connect to VPN/corpnet and retry.")
        yield _sse_event('vpn_blocked', {
            'message': "Couldn't reach MSX. If you're off VPN/corpnet, connect "
                       "and try again - no milestones were changed.",
        })
        return

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
    opp_fetch_failures = 0
    fetch_errors = []
    total_opp_chunks = 0
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

        # Phase 1a: parallelize chunks with 3 workers. The OData calls are
        # network-bound and independent, so concurrency yields a near-linear
        # speedup until we hit MSX throttling. 3 workers matches Phase 7
        # (audits) which is known-good.
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_chunk = {
                executor.submit(batch_get_opportunities, chunk): chunk
                for chunk in opp_chunks
            }
            completed = 0
            for future in as_completed(future_to_chunk):
                completed += 1
                SyncStatus.update_heartbeat('milestones')
                try:
                    batch_opp_result = future.result()
                except Exception as exc:
                    logger.exception("Phase 1a opp chunk failed")
                    batch_opp_result = {"success": False, "error": str(exc)}

                if not batch_opp_result.get("success"):
                    opp_fetch_failures += 1
                    fetch_errors.append(
                        batch_opp_result.get("error") or "MSX opportunity fetch failed."
                    )

                if batch_opp_result.get("success"):
                    for acct_id, opps in batch_opp_result.get(
                        "by_account", {}
                    ).items():
                        for opp in opps:
                            opp_id = opp.get("id")
                            if opp_id:
                                opp_map[opp_id] = opp
                                opp_map[opp_id]["_account_id"] = acct_id
                                opp_by_account.setdefault(
                                    acct_id, []
                                ).append(opp_id)

                pct = int((completed / total_opp_chunks) * 8)  # 0-8%
                yield _sse_event('progress', {
                    'current': completed,
                    'total': total_opp_chunks,
                    'customer': f'Opps batch {completed}/{total_opp_chunks}'
                                f' ({len(opp_map)} so far)',
                    'status': 'fetching',
                    'progress': min(pct, 8),
                })

    if is_vpn_blocked() or opp_fetch_failures:
        message = (
            "MSX opportunity fetch was incomplete. No milestone changes were saved. "
            + "; ".join(fetch_errors[:5])
        )
        SyncStatus.mark_completed('milestones', success=False, items_synced=0)
        yield _sse_event('vpn_blocked' if is_vpn_blocked() else 'error', {
            'error': message,
        })
        return

    # -----------------------------------------------------------------
    # Phase 1b: Batch fetch milestones by opportunity ID (batched OData)
    # -----------------------------------------------------------------
    # milestones_by_customer: cust_id -> [milestone_dicts with opp data]
    milestones_by_customer: Dict[int, List[dict]] = {
        cid: [] for cid, _, _ in customer_tasks
    }
    all_opp_ids = list(opp_map.keys())
    ms_fetch_failures = 0
    _MS_CHUNK = 20  # opp IDs per OData call (matches batch_get_milestones default)

    if all_opp_ids:
        opp_chunks = [
            all_opp_ids[i:i + _MS_CHUNK]
            for i in range(0, len(all_opp_ids), _MS_CHUNK)
        ]
        total_chunks = len(opp_chunks)
        ms_total = 0

        # Phase 1b: parallelize milestone chunks with 3 workers. Milestones
        # in the response are keyed by opportunity id, which we then route
        # back to the customer via opp_map. Routing happens on the main
        # thread after each future completes (no thread-safety concerns).
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_chunk = {
                executor.submit(
                    batch_get_milestones, chunk, current_fy_only=True
                ): chunk
                for chunk in opp_chunks
            }
            completed = 0
            for future in as_completed(future_to_chunk):
                completed += 1
                SyncStatus.update_heartbeat('milestones')
                try:
                    ms_batch_result = future.result()
                except Exception as exc:
                    logger.exception("Phase 1b milestone chunk failed")
                    ms_batch_result = {"success": False, "error": str(exc)}

                if not ms_batch_result.get("success"):
                    ms_fetch_failures += 1
                    fetch_errors.append(
                        ms_batch_result.get("error") or "MSX milestone fetch failed."
                    )

                if ms_batch_result.get("success"):
                    for opp_id, ms_list in ms_batch_result.get(
                        "by_opportunity", {}
                    ).items():
                        opp_data = opp_map.get(opp_id, {})
                        acct_id = opp_data.get("_account_id")
                        cust_id = (
                            acct_to_cust.get(acct_id) if acct_id else None
                        )
                        if cust_id is None:
                            continue
                        for ms in ms_list:
                            # Inject opportunity data into milestone dict
                            ms["opportunity_name"] = opp_data.get("name", "")
                            ms["opportunity_number"] = opp_data.get(
                                "number", ""
                            )
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
                            ms["opportunity_estimated_close_date"] = (
                                opp_data.get("estimated_close_date")
                            )
                            ms["opportunity_created_on"] = opp_data.get("created_on")
                            ms["opportunity_closed_on"] = opp_data.get("closed_on")
                            ms["opportunity_owner"] = opp_data.get(
                                "owner", ""
                            )
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

                pct = 8 + int((completed / total_chunks) * 68)  # 8-76%
                yield _sse_event('progress', {
                    'current': completed,
                    'total': total_chunks,
                    'customer': f'Milestones batch {completed}/{total_chunks}'
                                f' ({ms_total} so far)',
                    'status': 'fetching',
                    'progress': min(pct, 76),
                })

    if is_vpn_blocked() or ms_fetch_failures:
        message = (
            "MSX milestone fetch was incomplete. No milestone changes were saved. "
            + "; ".join(fetch_errors[:5])
        )
        SyncStatus.mark_completed('milestones', success=False, items_synced=0)
        yield _sse_event('vpn_blocked' if is_vpn_blocked() else 'error', {
            'error': message,
        })
        return

    ms_count = sum(len(v) for v in milestones_by_customer.values())
    yield _sse_event('progress', {
        'current': ms_count,
        'total': ms_count or 1,
        'customer': f'Fetched {ms_count} milestones',
        'status': 'ok',
        'progress': 76,
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
    all_seen_msx_ids = set()
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
                all_seen_msx_ids.update(
                    wr.get('seen_milestone_ids', set())
                )

                pct = 76 + int((i / write_count) * 3)  # 76-79%
                yield _sse_event('progress', {
                    'current': i,
                    'total': write_count,
                    'customer': cust_name,
                    'status': 'ok',
                    'created': wr['created'],
                    'updated': wr['updated'],
                    'progress': min(pct, 79),
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
    existing_opps = {
        opp.msx_opportunity_id.lower(): opp
        for opp in Opportunity.query.all() if opp.msx_opportunity_id
    }
    for acct_id, opp_ids in opp_by_account.items():
        cid = acct_to_cust.get(acct_id)
        if not cid:
            continue
        created_for_customer = 0
        updated_for_customer = 0
        for opp_id in opp_ids:
            opp_data = opp_map.get(opp_id)
            if not opp_data:
                continue
            existing = existing_opps.get(opp_id.lower())
            if existing:
                existing.customer_id = cid
                _update_opportunity_from_msx(existing, opp_data)
                updated_for_customer += 1
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
                existing_opps[opp_id.lower()] = opp
                created_for_customer += 1

            (existing if existing is not None else opp).cache_msx_dates(
                opp_data.get("created_on"), opp_data.get("closed_on"),
            )

        try:
            db.session.commit()
            total_opps_synced += created_for_customer
            total_opps_updated += updated_for_customer
        except Exception:
            db.session.rollback()
            errors.append(f"Could not save opportunities for customer {cid}.")
            existing_opps = {
                opp.msx_opportunity_id.lower(): opp
                for opp in Opportunity.query.all() if opp.msx_opportunity_id
            }
            logger.exception(
                "Error committing opportunities for customer %s", cid
            )

    yield _sse_event('progress', {
        'current': len(opp_map),
        'total': len(opp_map) or 1,
        'customer': 'Opportunities saved',
        'status': 'ok',
        'progress': 81,
    })
    yield _sse_event('opp_sync_end', {
        'opportunities_synced': total_opps_synced,
    })

    # -----------------------------------------------------------------
    # Phase 3: Refresh local milestones the active sync didn't return
    # (out-of-FY, closed opps, status reset to Completed, etc.)
    # -----------------------------------------------------------------
    yield _sse_event('stale_sync_start', {
        'message': 'Refreshing stale milestones...',
    })
    stale_gen = _sync_stale_milestones(all_seen_msx_ids)
    try:
        while True:
            current_s, total_s, label_s = next(stale_gen)
            SyncStatus.update_heartbeat('milestones')
            pct = 81 + int((current_s / max(total_s, 1)) * 1)  # 81-82%
            yield _sse_event('progress', {
                'current': current_s,
                'total': total_s,
                'customer': f'Stale milestones: {label_s}',
                'status': 'ok',
                'progress': min(pct, 82),
            })
    except StopIteration as stop:
        stale_result = stop.value
        total_stale_ms_updated = stale_result.get('milestones_updated', 0)
        if not stale_result.get('success'):
            errors.append(stale_result.get('error') or 'Stale milestone refresh failed.')
    yield _sse_event('stale_sync_end', {
        'milestones_updated': total_stale_ms_updated,
    })

    # Hydrate all team milestones before syncing task/activity evidence.
    team_result = _sync_team_milestones()
    if not team_result.get('success'):
        errors.append(team_result.get('error'))

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
            pct = 82 + int((batch_num / max(total_batches, 1)) * 11)  # 82-93%
            yield _sse_event('progress', {
                'current': batch_num,
                'total': total_batches,
                'customer': info,
                'status': status,
                'progress': min(pct, 93),
            })
    except StopIteration as stop:
        task_result = stop.value
    total_tasks_created = task_result.get('tasks_created', 0)
    total_tasks_updated = task_result.get('tasks_updated', 0)
    if not task_result.get('success'):
        logger.warning(f"Batched task sync failed: {task_result.get('error')}")
        errors.append(task_result.get('error') or 'Task sync failed.')
    yield _sse_event('task_sync_end', {
        'tasks_created': total_tasks_created,
        'tasks_updated': total_tasks_updated,
    })

    activity_result = _sync_caip_activities()
    total_activities_synced = activity_result.get('activities_synced', 0)
    if not activity_result.get('success'):
        errors.append(activity_result.get('error'))

    # -----------------------------------------------------------------
    # Phase 5: Team membership update (one API call)
    # -----------------------------------------------------------------
    yield _sse_event('progress', {
        'current': total,
        'total': total,
        'customer': 'Updating team memberships...',
        'status': 'ok',
        'progress': 93,
    })
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
        if not comment_result.get('success') or total_comments_failed:
            errors.append(
                comment_result.get('error') or f'{total_comments_failed} comment fetches failed.'
            )
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
            pct = 95 + int((batch_done / max(batch_total, 1)) * 5)  # 95-100%
            yield _sse_event('progress', {
                'current': batch_done,
                'total': batch_total,
                'customer': f'Audit trail batch {batch_done}/{batch_total}',
                'status': 'ok',
                'progress': min(pct, 100),
            })
    except StopIteration as stop:
        audit_result = stop.value
        audit_fields_saved = audit_result.get('fields_saved', 0)
        if audit_result.get('success') is False:
            errors.append(audit_result['error'])
    yield _sse_event('audit_sync_end', {
        'audit_fields_saved': audit_fields_saved,
    })

    duration = round(_time.time() - start_time, 1)
    sync_success = failed == 0 and not errors
    error = None if sync_success else (
        "; ".join(errors[:5]) or f"Could not sync {failed} customers."
    )

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
            'activities_synced': total_activities_synced,
            'comments_synced': total_comments_synced,
            'stale_milestones_updated': total_stale_ms_updated,
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
        'activities_synced': total_activities_synced,
        'comments_synced': total_comments_synced,
        'stale_milestones_updated': total_stale_ms_updated,
        'duration': duration,
        'errors': errors[:5],
        'error': error,
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
        if not opp_result.get("success"):
            result["success"] = False
            result["error"] = opp_result.get("error") or "Opportunity sync failed."

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
    incoming_msx_ids = [m["id"].lower() for m in msx_milestones if m.get("id")]
    existing_milestones_map: Dict[str, Milestone] = {}
    if incoming_msx_ids:
        for ms in Milestone.query.filter(
            db.func.lower(Milestone.msx_milestone_id).in_(incoming_msx_ids),
        ).all():
            existing_milestones_map[ms.msx_milestone_id.lower()] = ms
    # Also load any remaining milestones for this customer (for deactivation)
    for ms in Milestone.query.filter_by(customer_id=customer.id).filter(
        Milestone.msx_milestone_id.isnot(None),
        ~db.func.lower(Milestone.msx_milestone_id).in_(
            list(existing_milestones_map.keys()) or ['']
        ),
    ).all():
        existing_milestones_map[ms.msx_milestone_id.lower()] = ms

    # Pre-load existing opportunities to avoid per-row queries
    msx_opp_ids = [
        m["msx_opportunity_id"].lower() for m in msx_milestones
        if m.get("msx_opportunity_id")
    ]
    existing_opps_map: Dict[str, Opportunity] = {}
    if msx_opp_ids:
        for opp in Opportunity.query.filter(
            db.func.lower(Opportunity.msx_opportunity_id).in_(msx_opp_ids)
        ).all():
            existing_opps_map[opp.msx_opportunity_id.lower()] = opp

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
            milestone = existing_milestones_map.get(msx_id.lower())

            if milestone:
                _update_milestone_from_msx(milestone, msx_ms, customer.id, due_date, now)
                if "msx_opportunity_id" in msx_ms:
                    milestone.opportunity = opportunity
                result["updated"] += 1
            else:
                milestone = _create_milestone_from_msx(
                    msx_ms, customer.id, due_date, now,
                )
                if opportunity:
                    milestone.opportunity = opportunity
                db.session.add(milestone)
                existing_milestones_map[msx_id.lower()] = milestone
                result["created"] += 1

        # Handle milestones for this customer that are no longer in the MSX
        # query results.  The sync query uses open_opportunities_only=True
        # and current_fy_only=True, so a milestone can disappear from results
        # for reasons other than completion (opportunity closed, FY filter,
        # account re-parent, etc.).  We must NOT assume "Completed" - just
        # mark them as synced so we know we checked.
        normalized_seen_ids = {value.lower() for value in seen_msx_ids}
        for msx_id, existing in existing_milestones_map.items():
            if existing.customer_id != customer.id:
                continue
            if existing.msx_status not in ACTIVE_STATUSES:
                continue
            if msx_id not in normalized_seen_ids:
                existing.last_synced_at = now
                result["deactivated"] += 1

    result["seen_opportunity_ids"] = seen_opp_ids
    result["seen_milestone_ids"] = seen_msx_ids

    try:
        db.session.commit()
        result["success"] = True
    except Exception as e:
        db.session.rollback()
        result["error"] = f"Database error: {str(e)}"
        logger.exception(f"Error saving milestones for customer {customer.id}")
    
    return result


def _milestone_parent_maps() -> Tuple[Dict[str, int], Dict[str, Opportunity]]:
    """Load local account and opportunity identities for fallback reconciliation."""
    customers = {}
    for customer in Customer.query.filter(
        Customer.tpid_url.isnot(None), Customer.tpid_url != '',
    ).all():
        account_id = extract_account_id_from_url(customer.tpid_url)
        if account_id:
            customers[account_id.lower()] = customer.id
    opportunities = {
        opportunity.msx_opportunity_id.lower(): opportunity
        for opportunity in Opportunity.query.all() if opportunity.msx_opportunity_id
    }
    return customers, opportunities


def _resolve_milestone_parent(
    source: Dict[str, Any],
    milestone: Optional[Milestone],
    customers: Dict[str, int],
    opportunities: Dict[str, Opportunity],
) -> Tuple[Optional[Opportunity], Optional[int]]:
    """Resolve supplied parent identities without retaining a previous customer's link."""
    customer_id = milestone.customer_id if milestone else None
    if "msx_opportunity_id" not in source:
        opportunity = milestone.opportunity if milestone else None
    else:
        opportunity = opportunities.get((source["msx_opportunity_id"] or "").lower())
        if opportunity:
            customer_id = opportunity.customer_id
    if "msx_account_id" in source:
        customer_id = customers.get((source["msx_account_id"] or "").lower())
    if source.get("msx_opportunity_id") and (
        opportunity is not None or "msx_account_id" in source
    ):
        opportunity, _ = _upsert_opportunity(source, customer_id, opportunities)
    return opportunity, customer_id


def _sync_stale_milestones(
    seen_msx_ids: set,
) -> Generator[Tuple[int, int, str], None, Dict[str, Any]]:
    """
    Refresh local milestones the active sync didn't return.

    The active sync filters by FY date window and recent opportunities,
    so a local milestone can be missing from results for many reasons:
    parent opp closed and out of window, due date slipped, status got
    reset to Completed (often flips back to On Track later), account
    re-parented, etc.

    This pass operates at the MILESTONE level (not opportunity level).
    Stale milestone GUIDs are gathered across ALL customers, batched in
    groups of 20, and fetched in parallel by ``batch_get_milestones_by_id``
    with 3 workers. The opportunity-refresh side effect of the old
    per-customer flow is gone; opportunities that still matter come back
    in Phase 1a now that the FY-window filter replaces the open-only
    filter.

    Args:
        seen_msx_ids: msp_engagementmilestoneid GUIDs already covered by
            the active sync. Local milestones not in this set are
            considered stale and refreshed.

    Yields:
        (current_batch, total_batches, label) tuples for progress
        reporting. ``label`` looks like ``Stale batch 3/12``.

    Returns (via generator .value after StopIteration):
        Dict with success, milestones_updated, error.
    """
    result = {
        "success": True,
        "milestones_updated": 0,
        "error": "",
    }

    # Find local milestones with an MSX id that the active sync didn't
    # return. Limit to milestones whose customer has a tpid_url so we
    # can actually query MSX for them.
    stale_q = (
        db.session.query(Milestone)
        .join(Customer, Milestone.customer_id == Customer.id)
        .filter(
            Milestone.msx_milestone_id.isnot(None),
            Customer.tpid_url.isnot(None),
            Customer.tpid_url != '',
        )
    )
    if seen_msx_ids:
        stale_q = stale_q.filter(
            ~Milestone.msx_milestone_id.in_(seen_msx_ids)
        )
    stale_milestones = stale_q.all()

    if not stale_milestones:
        return result

    # Map MSX id -> local milestone, and MSX id -> customer id.
    # SQLite stores GUIDs case-insensitively when we wrote them; the
    # MSX response may return them in different case so we lowercase
    # both sides for the lookup.
    local_by_id: Dict[str, Milestone] = {
        ms.msx_milestone_id.lower(): ms
        for ms in stale_milestones
        if ms.msx_milestone_id
    }
    stale_ids = list(local_by_id.keys())

    _STALE_CHUNK = 20  # GUIDs per OData call
    chunks = [
        stale_ids[i:i + _STALE_CHUNK]
        for i in range(0, len(stale_ids), _STALE_CHUNK)
    ]
    total_batches = len(chunks)
    now = datetime.now(timezone.utc)
    customers_by_account, opportunities = _milestone_parent_maps()

    logger.info(
        f"Syncing stale milestones: {len(stale_ids)} milestones "
        f"in {total_batches} batches"
    )

    errors = []
    completed = 0
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_chunk = {
            executor.submit(batch_get_milestones_by_id, chunk): chunk
            for chunk in chunks
        }
        for future in as_completed(future_to_chunk):
            completed += 1
            yield (completed, total_batches, f"Stale batch {completed}/{total_batches}")

            if is_vpn_blocked():
                errors.append("VPN blocked during stale milestone sync")
                break

            try:
                fetch_result = future.result()
            except Exception as exc:
                logger.exception("Phase 3 stale chunk failed")
                errors.append(f"Stale milestone fetch failed: {exc}")
                continue

            if not fetch_result.get("success"):
                logger.warning(
                    "Stale chunk fetch failed: %s", fetch_result.get("error")
                )
                errors.append(fetch_result.get("error") or "Stale milestone fetch failed.")
                continue

            updated_this_batch = 0
            with db.session.no_autoflush:
                for msx_id, msx_ms in fetch_result.get("by_id", {}).items():
                    local_ms = local_by_id.get(msx_id.lower())
                    if not local_ms:
                        continue
                    opportunity, customer_id = _resolve_milestone_parent(
                        msx_ms, local_ms, customers_by_account, opportunities,
                    )
                    due_date = _parse_msx_date(msx_ms.get("due_date"))
                    _update_milestone_from_msx(
                        local_ms, msx_ms, customer_id, due_date, now
                    )
                    local_ms.opportunity = opportunity
                    updated_this_batch += 1

            if updated_this_batch:
                try:
                    db.session.commit()
                    result["milestones_updated"] += updated_this_batch
                except Exception:
                    db.session.rollback()
                    errors.append("Could not save a stale milestone batch.")
                    customers_by_account, opportunities = _milestone_parent_maps()
                    logger.exception(
                        "Error saving stale milestones (batch %d)", completed
                    )

    result["success"] = not errors
    result["error"] = "; ".join(errors[:5])
    logger.info(
        f"Stale milestone sync complete: "
        f"{result['milestones_updated']} milestones updated"
    )
    return result


def _sync_all_tasks() -> Generator[
    Tuple[int, int, str, str], None, Dict[str, Any]
]:
    """
    Sync every owned MSX task through the existing task stage.

    A complete owner query includes tasks outside the local milestone inventory.
    Existing milestone, note and meeting links remain intact.

    Yields (current_step, total_steps, info_str, state) tuples for progress.

    Returns (via generator .value after StopIteration):
        Dict with success, tasks_created, tasks_updated, error.
    """
    result = {"success": False, "tasks_created": 0, "tasks_updated": 0, "error": ""}

    yield (0, 1, "Reading all owned MSX tasks", 'fetching')
    try:
        fetch_result = get_tasks_for_milestones(None)
    except Exception as exc:
        logger.exception("Owned-task inventory fetch crashed")
        result["error"] = f"MSX task fetch failed: {exc}"
        yield (1, 1, result["error"], 'error')
        return result
    msx_tasks = fetch_result.get("tasks")
    if not fetch_result.get("success"):
        result["error"] = fetch_result.get("error") or "MSX task fetch failed"
    elif not isinstance(msx_tasks, list):
        result["error"] = "MSX did not return a complete owned-task inventory"
    if result["error"]:
        logger.warning("Owned-task inventory fetch failed: %s", result["error"])
        yield (1, 1, result["error"], 'error')
        return result

    try:
        ms_id_map = {
            ms.msx_milestone_id.lower(): ms.id
            for ms in Milestone.query.filter(Milestone.msx_milestone_id.isnot(None)).all()
        }
        existing_tasks_map = {
            task.msx_task_id: task for task in MsxTask.query.all() if task.msx_task_id
        }
        cat_lookup = {category["value"]: category for category in TASK_CATEGORIES}
        for existing in existing_tasks_map.values():
            existing.is_current_owner = False
        for remote in msx_tasks:
            task_id = remote["task_id"]
            stored = existing_tasks_map.get(task_id)
            if stored is None:
                stored = MsxTask(msx_task_id=task_id, subject=remote.get("subject") or "")
                db.session.add(stored)
                existing_tasks_map[task_id] = stored
                result["tasks_created"] += 1
            else:
                result["tasks_updated"] += 1
            category_code = remote.get("task_category")
            category = cat_lookup.get(category_code, {})
            regarding_id = (remote.get("milestone_msx_id") or "").lower()
            stored.subject = remote.get("subject") or stored.subject
            stored.description = remote.get("description")
            stored.task_category = category_code or 0
            stored.task_category_name = category.get("label")
            stored.is_hva = category.get("is_hva", False)
            stored.duration_minutes = remote.get("duration_minutes") or stored.duration_minutes or 60
            stored.due_date = _parse_msx_date(remote.get("due_date"))
            stored.msx_created_on = _parse_msx_date(remote.get("created_on"))
            stored.actual_end = _parse_msx_date(remote.get("actual_end"))
            stored.statecode = remote.get("statecode")
            stored.statuscode = remote.get("statuscode")
            stored.msx_task_url = remote.get("task_url") or stored.msx_task_url
            stored.milestone_id = ms_id_map.get(regarding_id)
            stored.is_current_owner = True
            stored.msx_regarding_id = regarding_id or None
            stored.msx_regarding_type = remote.get("regarding_type")
            stored.msx_regarding_name = remote.get("regarding_name")
        SyncStatus.mark_completed('owned_tasks', success=True, items_synced=len(msx_tasks))
        result["success"] = True
    except Exception as exc:
        db.session.rollback()
        result.update(tasks_created=0, tasks_updated=0, error=f"Error saving MSX tasks: {exc}")
        logger.exception("Error saving owned-task inventory")
        yield (1, 1, result["error"], 'error')
        return result
    yield (1, 1, f"Owned tasks synced ({result['tasks_created']} new, "
           f"{result['tasks_updated']} updated)", 'ok')
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
        c["value"]: {"name": c["label"], "is_hva": c["is_hva"]}
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
        created_on = _parse_msx_date(t.get("created_on"))
        actual_end = _parse_msx_date(t.get("actual_end"))

        existing = existing_tasks_map.get(task_id)
        if existing:
            # Update
            existing.subject = t.get("subject") or existing.subject
            existing.description = t.get("description")
            existing.task_category = category_code or existing.task_category
            existing.task_category_name = cat_info.get("name") or existing.task_category_name
            existing.is_hva = cat_info.get("is_hva", existing.is_hva)
            existing.duration_minutes = t.get("duration_minutes") or existing.duration_minutes
            existing.due_date = due_date
            existing.msx_created_on = created_on
            existing.actual_end = actual_end
            existing.statecode = t.get("statecode")
            existing.statuscode = t.get("statuscode")
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
                is_hva=cat_info.get("is_hva", False),
                duration_minutes=t.get("duration_minutes") or 60,
                due_date=due_date,
                msx_created_on=created_on,
                actual_end=actual_end,
                statecode=t.get("statecode"),
                statuscode=t.get("statuscode"),
                milestone_id=local_milestone_id,
                # note_id left NULL — synced tasks aren't linked to a note
            )
            db.session.add(new_task)
            existing_tasks_map[task_id] = new_task
            result["tasks_created"] += 1

        stored = existing_tasks_map[task_id]
        stored.is_current_owner = True
        stored.msx_regarding_id = milestone_msx_id or None
        stored.msx_regarding_type = t.get("regarding_type")
        stored.msx_regarding_name = t.get("regarding_name")

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

    # Look up globally so moved opportunities retain their local identity and links.
    existing_map = {
        opp.msx_opportunity_id.lower(): opp
        for opp in Opportunity.query.all()
        if opp.msx_opportunity_id
    }

    for msx_opp in msx_opps:
        opp_id = msx_opp.get("id")
        if not opp_id:
            continue

        existing = existing_map.get(opp_id.lower())
        if existing:
            existing.customer_id = customer.id
            _update_opportunity_from_msx(existing, msx_opp)
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
            existing_map[opp_id.lower()] = opp
            result["created"] += 1

        (existing if existing is not None else opp).cache_msx_dates(
            msx_opp.get("created_on"), msx_opp.get("closed_on"),
        )

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

    # Phase 6: parallelize per-milestone comment fetches with 3 workers.
    # Each worker just calls the MSX API and returns the JSON; the main
    # thread applies results to the Milestone rows and commits in chunks
    # of 25 to keep the SQLAlchemy session bounded on low-end laptops.
    COMMIT_EVERY = 25
    pending_since_commit = 0
    completed = 0

    def _fetch_one(ms_msx_id: str) -> Tuple[str, Optional[list], Optional[str]]:
        """Worker: returns (msx_id, comments_or_None, error_or_None)."""
        try:
            res = get_milestone_comments(ms_msx_id)
            if res.get("success"):
                return ms_msx_id, res.get("comments", []), None
            return ms_msx_id, None, res.get("error", "fetch failed")
        except Exception as e:
            return ms_msx_id, None, str(e)

    # Map MSX id -> local Milestone for fast lookup when futures complete.
    by_msx_id = {ms.msx_milestone_id: ms for ms in need_fetch if ms.msx_milestone_id}

    def _commit_pending():
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            result["success"] = False
            result["error"] = f"Database error saving comments: {str(e)}"
            logger.exception("Error committing milestone comments")

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(_fetch_one, ms.msx_milestone_id): ms
            for ms in need_fetch
            if ms.msx_milestone_id
        }
        for future in as_completed(future_map):
            completed += 1
            ms = future_map[future]
            yield (completed, total, ms.title or ms.milestone_number or "Unknown")

            if is_vpn_blocked():
                result["error"] = "VPN blocked during comment sync"
                break

            try:
                msx_id, comments, error = future.result()
            except Exception:
                result["comments_failed"] += 1
                logger.exception(
                    "Worker crash fetching comments for milestone %s",
                    ms.msx_milestone_id,
                )
                continue

            local_ms = by_msx_id.get(msx_id) or ms
            if error is not None or comments is None:
                result["comments_failed"] += 1
                logger.warning(
                    "Failed to fetch comments for milestone %s: %s",
                    msx_id, error,
                )
                continue

            local_ms.cached_comments_json = json.dumps(comments)
            local_ms.details_fetched_at = now
            result["comments_synced"] += 1
            pending_since_commit += 1

            if pending_since_commit >= COMMIT_EVERY:
                _commit_pending()
                pending_since_commit = 0

    if pending_since_commit:
        _commit_pending()

    return result


def _update_milestone_from_msx(
    milestone: Milestone,
    msx_data: Dict[str, Any],
    customer_id: Optional[int],
    due_date: Optional[datetime],
    now: datetime,
) -> None:
    """Update an existing milestone with fresh data from MSX."""
    milestone.title = msx_data.get("name") or milestone.title
    milestone.msx_status = msx_data.get("status") or milestone.msx_status
    for source, target in {
        "number": "milestone_number", "status_code": "msx_status_code",
        "customer_commitment": "customer_commitment", "milestone_category": "milestone_category",
        "opportunity_name": "opportunity_name", "workload": "workload",
        "monthly_usage": "monthly_usage", "dollar_value": "dollar_value",
        "owner_name": "owner_name",
    }.items():
        if source in msx_data:
            setattr(milestone, target, msx_data[source])
    if "due_date" in msx_data:
        milestone.due_date = due_date
    milestone.url = msx_data.get("url") or milestone.url
    milestone.customer_id = customer_id
    milestone.last_synced_at = now
    for source, target in {
        "committed_on": "committed_at", "completed_on": "completed_at",
        "created_on": "msx_created_on", "modified_on": "msx_modified_on",
    }.items():
        if source in msx_data:
            setattr(milestone, target, _parse_msx_date(msx_data[source]))
    # Cache comments if included in the bulk fetch (None = field not requested,
    # so we only update when the key is present in the dict)
    if "comments_json" in msx_data:
        milestone.cached_comments_json = msx_data["comments_json"] or "[]"
        milestone.details_fetched_at = now


def _create_milestone_from_msx(
    msx_data: Dict[str, Any],
    customer_id: Optional[int],
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
        milestone_category=msx_data.get("milestone_category", ""),
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
        owner_name=msx_data.get("owner_name"),
    )
    # Cache comments if included in the bulk fetch
    if "comments_json" in msx_data:
        ms.cached_comments_json = msx_data["comments_json"] or "[]"
        ms.details_fetched_at = now
    return ms


def _update_opportunity_from_msx(opportunity: Opportunity, source: Dict[str, Any]) -> None:
    """Apply supplied fields, including explicitly cleared optional values."""
    for key, field in {
        "number": "opportunity_number", "statecode": "statecode", "state": "state",
        "status_reason": "status_reason", "estimated_value": "estimated_value",
        "estimated_close_date": "estimated_close_date", "owner": "owner_name",
        "customer_need": "customer_need", "description": "description",
        "compete_threat": "compete_threat", "url": "msx_url",
    }.items():
        if key in source:
            setattr(opportunity, field, source[key])
    if source.get("name"):
        opportunity.name = source["name"]
    for key, field in {"created_on": "msx_created_on", "closed_on": "msx_closed_on"}.items():
        if key in source and not source[key]:
            setattr(opportunity, field, None)
    opportunity.cache_msx_dates(source.get("created_on"), source.get("closed_on"))


def _upsert_opportunity(
    msx_data: Dict[str, Any],
    customer_id: Optional[int],
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
        opportunity = existing_opps_map.get(msx_opp_id.lower())
    else:
        opportunity = Opportunity.query.filter(
            db.func.lower(Opportunity.msx_opportunity_id) == msx_opp_id.lower(),
        ).first()

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
        opportunity.customer_id = customer_id
        _update_opportunity_from_msx(opportunity, {
            key.removeprefix("opportunity_"): value
            for key, value in msx_data.items() if key.startswith("opportunity_")
        })
        opportunity.msx_url = opp_url
        opportunity.cache_msx_dates(
            msx_data.get("opportunity_created_on"), msx_data.get("opportunity_closed_on"),
        )
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
        opportunity.cache_msx_dates(
            msx_data.get("opportunity_created_on"), msx_data.get("opportunity_closed_on"),
        )
        # Track in map so later milestones sharing this opportunity find it
        if existing_opps_map is not None:
            existing_opps_map[msx_opp_id.lower()] = opportunity
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
    stats = {"audits_fetched": 0, "fields_saved": 0, "skipped": 0,
             "success": True, "error": ""}

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
        stats.update(success=False, error=result.get("error") or "Audit fetch failed.")
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
            stats.update(success=False, error="Could not save milestone audit records.")
    else:
        logger.info("No new audit records to save")

    return stats


def _sync_team_milestones() -> Dict[str, Any]:
    """Hydrate every MSX milestone where the current user is a team member."""
    result = {
        "success": False,
        "milestones_created": 0,
        "milestones_updated": 0,
        "error": "",
    }
    try:
        membership_result = get_my_milestone_team_ids()
        if not membership_result.get("success"):
            result["error"] = membership_result.get(
                "error", "Could not fetch team memberships."
            )
            return result

        team_ids = [mid.lower() for mid in membership_result["milestone_ids"]]
        if membership_result.get("pagination_complete", True):
            Milestone.query.filter(
                Milestone.msx_milestone_id.isnot(None)
            ).update({Milestone.on_my_team: False})
        if team_ids:
            Milestone.query.filter(
                db.func.lower(Milestone.msx_milestone_id).in_(team_ids)
            ).update({Milestone.on_my_team: True}, synchronize_session='fetch')

        fetch_result = batch_get_milestones_by_id(team_ids)
        if not fetch_result.get("success"):
            db.session.rollback()
            result["error"] = fetch_result.get(
                "error", "Could not fetch team milestones."
            )
            return result

        existing = {
            milestone.msx_milestone_id.lower(): milestone
            for milestone in Milestone.query.filter(
                Milestone.msx_milestone_id.isnot(None)
            ).all()
        }
        customers_by_account, opportunities = _milestone_parent_maps()
        now = datetime.now(timezone.utc)
        for milestone_msx_id, msx_data in fetch_result["by_id"].items():
            normalized_id = milestone_msx_id.lower()
            milestone = existing.get(normalized_id)
            opportunity, customer_id = _resolve_milestone_parent(
                msx_data, milestone, customers_by_account, opportunities,
            )
            due_date = _parse_msx_date(msx_data.get("due_date"))
            if milestone:
                _update_milestone_from_msx(
                    milestone, msx_data, customer_id, due_date, now
                )
                result["milestones_updated"] += 1
            else:
                milestone = _create_milestone_from_msx(
                    msx_data, customer_id, due_date, now
                )
                db.session.add(milestone)
                existing[normalized_id] = milestone
                result["milestones_created"] += 1
            milestone.opportunity = opportunity
            milestone.on_my_team = True

        db.session.commit()
        result["success"] = True
    except Exception as exc:
        db.session.rollback()
        logger.exception("Error syncing team milestones")
        result["error"] = str(exc)
    return result


def _sync_caip_activities() -> Dict[str, Any]:
    """Sync owned MSX activity evidence for all local team milestones."""
    milestones = Milestone.query.filter(
        Milestone.on_my_team.is_(True),
        Milestone.msx_milestone_id.isnot(None),
    ).all()
    if not milestones:
        return {"success": True, "activities_synced": 0}

    milestone_by_msx_id = {
        milestone.msx_milestone_id.lower(): milestone for milestone in milestones
    }
    fetch_result = get_activities_for_milestones(list(milestone_by_msx_id))
    if not fetch_result.get("success"):
        return {
            "success": False,
            "activities_synced": 0,
            "error": fetch_result.get("error", "Activity fetch failed"),
        }

    existing = {
        activity.msx_activity_id: activity
        for activity in CaipActivity.query.all()
    }
    seen_ids = set()
    for data in fetch_result.get("activities", []):
        milestone = milestone_by_msx_id.get(data["milestone_msx_id"])
        if not milestone:
            continue
        activity_id = data["activity_id"]
        seen_ids.add(activity_id)
        activity = existing.get(activity_id)
        if not activity:
            activity = CaipActivity(msx_activity_id=activity_id)
            db.session.add(activity)
        activity.activity_type = data.get("activity_type")
        activity.subject = data.get("subject")
        activity.created_on = _parse_msx_date(data.get("created_on"))
        activity.actual_end = _parse_msx_date(data.get("actual_end"))
        activity.milestone = milestone

    stale_query = CaipActivity.query.filter(
        CaipActivity.milestone_id.in_([milestone.id for milestone in milestones])
    )
    if seen_ids:
        stale_query = stale_query.filter(
            CaipActivity.msx_activity_id.notin_(seen_ids)
        )
    stale_query.delete(synchronize_session=False)
    db.session.commit()
    return {"success": True, "activities_synced": len(seen_ids)}


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
        .filter(Milestone.customer_id.isnot(None))
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
            "search_text": get_milestone_search_text(ms),
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
            "search_text": get_milestone_search_text(ms),
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
