"""
Milestone sync service for NoteHelper.

Pulls active (uncommitted) milestones from MSX for all customers
and upserts them into the local database. Uses 3 concurrent workers
for the MSX API query phase, then writes to the database sequentially.
"""
import json
import logging
import math
import queue
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Generator, Tuple

from app.models import db, Customer, Milestone, MsxTask, Opportunity, User, SyncStatus
from app.services.msx_api import (
    extract_account_id_from_url,
    get_milestones_by_account,
    get_my_milestone_team_ids,
    get_tasks_for_milestones,
    build_milestone_url,
    build_task_url,
    TASK_CATEGORIES,
    HOK_TASK_CATEGORIES,
)
from app.services.msx_auth import is_vpn_blocked

logger = logging.getLogger(__name__)

# Active milestone statuses (uncommitted — the ones we're working to commit)
ACTIVE_STATUSES = {'On Track', 'At Risk', 'Blocked'}

# Number of concurrent workers for MSX API queries
_MILESTONE_WORKERS = 3


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
    
    for customer in customers:
        # Bail early if VPN block was detected during this sync
        if is_vpn_blocked():
            results["errors"].append("VPN/IP block detected — remaining customers skipped.")
            break

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
    
    # Calculate duration
    results["duration_seconds"] = (datetime.now(timezone.utc) - start_time).total_seconds()
    
    # If all customers failed, mark as failure
    if results["customers_synced"] == 0 and results["customers_failed"] > 0:
        results["success"] = False
    
    # Update team membership flags
    _update_team_memberships()
    
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
    ('fetched', cust_id, cust_name, msx_result) or
    ('vpn', cust_id, cust_name, None).
    Sends ('done', None, None, None) when finished.
    """
    for cust_id, cust_name, account_id in tasks:
        if is_vpn_blocked():
            progress_q.put(('vpn', cust_id, cust_name, None))
            return
        result = get_milestones_by_account(
            account_id,
            open_opportunities_only=True,
            current_fy_only=True,
        )
        progress_q.put(('fetched', cust_id, cust_name, result))
    progress_q.put(('done', None, None, None))


def sync_all_customer_milestones_stream(
) -> Generator[str, None, None]:
    """
    Stream milestone sync progress as Server-Sent Events.

    Uses 3 concurrent workers for the MSX API query phase, then writes
    to the database sequentially.

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
    # Phase 1: Parallel MSX queries (3 workers)
    # -----------------------------------------------------------------
    fetch_results = {}    # cust_id -> msx_result dict
    progress_q = queue.Queue()
    n_workers = min(_MILESTONE_WORKERS, len(customer_tasks)) if customer_tasks else 0
    vpn_hit = False
    fetched = 0

    if n_workers > 0:
        chunk_size = math.ceil(len(customer_tasks) / n_workers)
        chunks = [
            customer_tasks[i:i + chunk_size]
            for i in range(0, len(customer_tasks), chunk_size)
        ]
        actual_workers = len(chunks)

        with ThreadPoolExecutor(max_workers=actual_workers) as pool:
            for chunk in chunks:
                pool.submit(_ms_fetch_worker, chunk, progress_q)

            done_count = 0
            while done_count < actual_workers:
                msg = progress_q.get()
                evt, cust_id, cust_name, result = msg

                if evt == 'vpn':
                    vpn_hit = True
                    remaining = total - fetched - len(skip_ids)
                    yield _sse_event('vpn_blocked', {
                        'message': 'IP address is blocked -- connect to VPN and retry.',
                        'skipped': remaining,
                    })
                    break
                elif evt == 'fetched':
                    fetch_results[cust_id] = result
                    fetched += 1
                    pct = int((fetched / total) * 70)  # 0-70%
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
            'milestones', success=False, items_synced=0,
            details=json.dumps({'error': 'VPN blocked'}),
        )
        return

    # -----------------------------------------------------------------
    # Phase 2: Sequential DB writes
    # -----------------------------------------------------------------
    synced = 0
    failed = len(skip_ids)
    total_created = 0
    total_updated = 0
    total_deactivated = 0
    total_opps_created = 0
    total_tasks_created = 0
    total_tasks_updated = 0
    errors: List[str] = []

    write_count = len(customer_tasks)
    for i, (cust_id, cust_name, _acct) in enumerate(customer_tasks, 1):
        customer = customer_map[cust_id]
        fetch_data = fetch_results.get(cust_id)

        if not fetch_data or not fetch_data.get('success'):
            failed += 1
            err = fetch_data.get('error', 'Fetch failed') if fetch_data else 'No data'
            errors.append(f"{cust_name}: {err}")
            pct = 70 + int((i / write_count) * 25)  # 70-95%
            yield _sse_event('progress', {
                'current': fetched + i,
                'total': total,
                'customer': cust_name,
                'status': 'error',
                'error': err,
                'progress': pct,
            })
            continue

        try:
            wr = _apply_customer_milestones(
                customer, fetch_data.get('milestones', [])
            )
            if wr['success']:
                synced += 1
                total_created += wr['created']
                total_updated += wr['updated']
                total_deactivated += wr['deactivated']
                total_opps_created += wr['opportunities_created']

                # Sync tasks for this customer's milestones
                task_result = _sync_customer_tasks(customer)
                total_tasks_created += task_result.get('tasks_created', 0)
                total_tasks_updated += task_result.get('tasks_updated', 0)
                if not task_result.get('success'):
                    logger.warning(
                        f"Task sync failed for {cust_name}: "
                        f"{task_result.get('error')}"
                    )

                pct = 70 + int((i / write_count) * 25)
                yield _sse_event('progress', {
                    'current': fetched + i,
                    'total': total,
                    'customer': cust_name,
                    'status': 'ok',
                    'created': wr['created'],
                    'updated': wr['updated'],
                    'progress': pct,
                })
            else:
                failed += 1
                errors.append(f"{cust_name}: {wr['error']}")
        except Exception as e:
            failed += 1
            errors.append(f"{cust_name}: {str(e)}")
            logger.exception(f"Error saving milestones for customer {cust_id}")

    # -----------------------------------------------------------------
    # Phase 3: Team membership update (one API call)
    # -----------------------------------------------------------------
    _update_team_memberships()

    duration = round(_time.time() - start_time, 1)
    sync_success = synced > 0 or failed == 0

    SyncStatus.mark_completed(
        'milestones',
        success=sync_success,
        items_synced=total_created + total_updated,
        details=json.dumps({
            'synced': synced, 'failed': failed,
            'created': total_created, 'updated': total_updated,
            'deactivated': total_deactivated,
            'opportunities_created': total_opps_created,
            'tasks_created': total_tasks_created,
            'tasks_updated': total_tasks_updated,
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
        'tasks_created': total_tasks_created,
        'tasks_updated': total_tasks_updated,
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

    # Pre-load existing milestones for this customer to avoid per-row queries
    existing_milestones_map: Dict[str, Milestone] = {}
    for ms in Milestone.query.filter_by(customer_id=customer.id).filter(
        Milestone.msx_milestone_id.isnot(None),
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

        # Deactivate milestones for this customer that are no longer in MSX
        for msx_id, existing in existing_milestones_map.items():
            if existing.msx_status not in ACTIVE_STATUSES:
                continue
            if msx_id not in seen_msx_ids:
                if existing.notes:
                    existing.last_synced_at = now
                    continue
                existing.msx_status = "Completed"
                existing.last_synced_at = now
                result["deactivated"] += 1

    try:
        db.session.commit()
        result["success"] = True
    except Exception as e:
        db.session.rollback()
        result["error"] = f"Database error: {str(e)}"
        logger.exception(f"Error saving milestones for customer {customer.id}")
    
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
    milestone.opportunity_name = msx_data.get("opportunity_name") or milestone.opportunity_name
    milestone.workload = msx_data.get("workload") or milestone.workload
    milestone.monthly_usage = msx_data.get("monthly_usage")
    milestone.due_date = due_date
    milestone.dollar_value = msx_data.get("dollar_value")
    milestone.url = msx_data.get("url") or milestone.url
    milestone.customer_id = customer_id
    milestone.last_synced_at = now


def _create_milestone_from_msx(
    msx_data: Dict[str, Any],
    customer_id: int,
    due_date: Optional[datetime],
    now: datetime,
) -> Milestone:
    """Create a new Milestone from MSX data."""
    return Milestone(
        msx_milestone_id=msx_data["id"],
        milestone_number=msx_data.get("number", ""),
        url=msx_data.get("url", ""),
        title=msx_data.get("name", ""),
        msx_status=msx_data.get("status", "Unknown"),
        msx_status_code=msx_data.get("status_code"),
        opportunity_name=msx_data.get("opportunity_name", ""),
        workload=msx_data.get("workload", ""),
        monthly_usage=msx_data.get("monthly_usage"),
        due_date=due_date,
        dollar_value=msx_data.get("dollar_value"),
        last_synced_at=now,
        customer_id=customer_id,
    )


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

    if opportunity:
        # Update name in case it changed
        opportunity.name = opp_name or opportunity.name
        opportunity.customer_id = customer_id
        return opportunity, False
    else:
        opportunity = Opportunity(
            msx_opportunity_id=msx_opp_id,
            name=opp_name,
            customer_id=customer_id,
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


def _update_team_memberships() -> None:
    """
    Update the on_my_team flag for all milestones based on MSX access teams.

    Makes one API call to get all milestone team memberships, then bulk-updates
    the on_my_team column. Milestones the user is on get True, all others get
    False. Failures are logged but don't block the sync.
    """
    try:
        result = get_my_milestone_team_ids()
        if not result.get("success"):
            logger.warning(
                f"Could not fetch team memberships: {result.get('error')}"
            )
            return

        my_ids = result["milestone_ids"]
        logger.info(f"Updating on_my_team for {len(my_ids)} milestones")

        # Bulk update: set all to False first, then True for matches
        Milestone.query.update({Milestone.on_my_team: False})

        if my_ids:
            Milestone.query.filter(
                db.func.lower(Milestone.msx_milestone_id).in_(my_ids)
            ).update({Milestone.on_my_team: True}, synchronize_session='fetch')

        db.session.commit()
    except Exception as e:
        logger.exception("Error updating team memberships")
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
    # Query active milestones with eager-loaded relationships
    milestones = (
        Milestone.query
        .filter(Milestone.msx_status.in_(ACTIVE_STATUSES))
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
            "customer": {
                "id": ms.customer.id if ms.customer else None,
                "name": ms.customer.get_display_name() if ms.customer else "Unknown",
                "favicon_b64": ms.customer.favicon_b64 if ms.customer else None,
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
