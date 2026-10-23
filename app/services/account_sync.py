"""Worker-owned MSX account discovery and import orchestration."""
import json
import logging
import math
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
from flask import current_app
from app.models import (
    Customer, InternalContact, Territory, Seller, POD, SolutionEngineer, SyncStatus,
    Vertical, db, utc_now,
)
from app.services.msx_auth import is_vpn_blocked
from app.services.msx_api import (
    scan_init, query_entity, batch_query_account_teams, build_account_url, get_user_info,
)

logger = logging.getLogger(__name__)
ACCOUNT_SYNC_VERSION = 2
_PARALLEL_WORKERS = 3
_ACCT_BATCH = 15
_TEAM_BATCH = 3

def _extract_domain(url_or_domain: str) -> str:
    """Extract a clean domain from a URL or bare domain string.

    Handles MSX websiteurl values like:
      'http://www.example.com'  -> 'example.com'
      'https://example.com/foo' -> 'example.com'
      'example.com'             -> 'example.com'
      'www.example.com'         -> 'example.com'

    Returns:
        Clean domain string, or empty string if unparseable.
    """
    if not url_or_domain:
        return ""
    raw = url_or_domain.strip()
    # Add scheme if missing so urlparse works
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
    except Exception:
        return ""
    # Strip www. prefix
    if host.startswith("www."):
        host = host[4:]
    # Basic sanity: must contain at least one dot
    if "." not in host:
        return ""
    return host.lower()

def _split_chunks(items: list, n: int) -> list:
    """Split *items* into *n* roughly-equal chunks."""
    k, m = divmod(len(items), n)
    return [
        items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)]
        for i in range(n)
    ]

def _sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"

def _derive_pod_name(territory_name: str) -> str | None:
    """Derive the virtual POD encoded in an MSX territory name."""
    parts = territory_name.split(".")
    if len(parts) < 4 or len(parts[3]) < 2:
        return None
    return f"{parts[0]} POD {parts[3][:2]}"

def _drain(q: queue.Queue) -> list:
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            break
    return events

def _account_import_sort_key(account: dict) -> tuple:
    """Sort duplicate-TPID accounts with the canonical Top account first."""
    is_top = account.get("parenting_level_code") == 861980000
    return (str(account.get("tpid") or ""), not is_top, str(account.get("id") or ""))

def _update_customer_tpid_url(customer: Customer, account: dict) -> bool:
    """Update a customer's MSX URL from the canonical imported account."""
    account_url = account.get("url")
    if not account_url or customer.tpid_url == account_url:
        return False
    customer.tpid_url = account_url
    return True

def _par_query_accounts(account_ids, batch_size, progress_q, worker_id):
    """Worker: query account details for a chunk of IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying accounts - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    accounts = {}
    batches = math.ceil(len(account_ids) / batch_size)
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            filter_parts = [f"accountid eq {aid}" for aid in batch]
            result = query_entity(
                "accounts",
                select=["accountid", "name", "msp_mstopparentid",
                        "_territoryid_value", "msp_verticalcode",
                        "msp_verticalcategorycode", "websiteurl",
                        "msp_parentinglevelcode", "_ownerid_value"],
                filter_query=" or ".join(filter_parts),
                top=batch_size + 5,
            )
            if result.get("success"):
                for rec in result.get("records", []):
                    aid = rec.get("accountid")
                    if aid:
                        accounts[aid] = rec
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "fetched": len(accounts)})
    finally:
        msx_retry_state.callback = None
    return accounts

def _par_query_territories(territory_ids, batch_size, progress_q, worker_id):
    """Worker: query territory details for a chunk of IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying territories - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    territories = {}
    batches = math.ceil(len(territory_ids) / batch_size) if territory_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(territory_ids), batch_size), start=1):
            batch = territory_ids[i:i + batch_size]
            filter_parts = [f"territoryid eq {tid}" for tid in batch]
            result = query_entity(
                "territories",
                select=["territoryid", "name", "msp_ownerid",
                        "msp_salesunitname", "msp_accountteamunitname"],
                filter_query=" or ".join(filter_parts),
                top=batch_size + 5,
            )
            if result.get("success"):
                for rec in result.get("records", []):
                    tid = rec.get("territoryid")
                    if tid:
                        territories[tid] = rec
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "fetched": len(territories)})
    finally:
        msx_retry_state.callback = None
    return territories

def _par_query_teams(account_ids, batch_size, progress_q, worker_id):
    """Worker: query account teams for a chunk of account IDs."""
    from app.services.msx_api import msx_retry_state

    def _on_retry(attempt, max_retries, wait_secs, error_type):
        progress_q.put({"retry": True, "message":
            f"Querying teams - timeout, retrying ({attempt}/{max_retries})..."})
    msx_retry_state.callback = _on_retry

    account_sellers, unique_sellers, account_ses = {}, {}, {}
    batches = math.ceil(len(account_ids) / batch_size) if account_ids else 0
    try:
        for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
            batch = account_ids[i:i + batch_size]
            teams_result = batch_query_account_teams(batch, batch_size=len(batch))
            if teams_result.get("success"):
                account_sellers.update(teams_result.get("account_sellers", {}))
                unique_sellers.update(teams_result.get("unique_sellers", {}))
                account_ses.update(teams_result.get("account_ses", {}))
            progress_q.put({"worker": worker_id, "batch": batch_num,
                            "total_batches": batches, "sellers_found": len(unique_sellers)})
    finally:
        msx_retry_state.callback = None
    return {"account_sellers": account_sellers,
            "unique_sellers": unique_sellers,
            "account_ses": account_ses}

def sync_accounts_stream(source: str = 'accountteams', skip_aura: bool = False):
    """Run account discovery with captured options, independent of any request."""
    from app.services.alignment import current_fy_label, discover_accounts_from_alignment
    _use_alignment = source == 'alignment'
    _fy_label = current_fy_label()
    _skip_post_import_aura = skip_aura
    phase = "initializing"
    try:
        SyncStatus.mark_started('accounts')
        import_start_time = time.time()
        progress_q: queue.Queue = queue.Queue()
        yield _sse({"message": "Starting parallel MSX import...", "progress": 0})

        # ----------------------------------------------------------
        # Phase 1: discover the accounts to import
        #   - alignment mode: the user's declared territories
        #   - default mode: native account access teams (scan_init)
        # ----------------------------------------------------------
        if _use_alignment:
            phase = "reading your territory alignment"
            yield _sse({
                "message": f"Using your saved territory alignment ({_fy_label})...",
                "progress": 1,
            })
            init_result = discover_accounts_from_alignment(_fy_label)
            user_info = {}
            role = "Territory alignment"
        else:
            phase = "fetching account assignments"
            yield _sse({"message": "Fetching your account assignments from MSX...", "progress": 1})

            # Wire up retry callback so SSE stream can report timeouts/retries
            from app.services.msx_api import msx_retry_state
            retry_messages = []  # Collect retry events from the callback
            def _on_retry(attempt, max_retries, wait_secs, error_type):
                retry_messages.append(
                    f"Fetching assignments - timeout, retrying ({attempt}/{max_retries})..."
                )
            msx_retry_state.callback = _on_retry
            try:
                init_result = scan_init()
            finally:
                msx_retry_state.callback = None

            # Flush any retry messages as SSE events
            for msg in retry_messages:
                yield _sse({"message": msg, "progress": 1})
            user_info = init_result.get("user", {})
            role = init_result.get("role", "Unknown")

        if not init_result.get("success"):
            error_msg = init_result.get("error", "Failed to initialize scan")
            if init_result.get("vpn_blocked") or is_vpn_blocked():
                yield _sse({"error": error_msg, "vpn_blocked": True})
            elif init_result.get("msx_outage"):
                yield _sse({"error": error_msg, "msx_outage": True})
            else:
                yield _sse({"error": error_msg})
            return

        account_ids = init_result.get("account_ids", [])
        yield _sse({
            "message": f"Found {len(account_ids)} accounts to import...",
            "user": user_info.get("name"), "role": role, "progress": 1,
        })

        if not account_ids:
            if _use_alignment:
                yield _sse({"error": "No territories selected. Save your alignment first."})
            else:
                yield _sse({"error": "No accounts found for this user."})
            return

        # ----------------------------------------------------------
        # Phase 2: Parallel account queries (3 workers)
        # ----------------------------------------------------------
        phase = "querying accounts"
        chunks = _split_chunks(account_ids, _PARALLEL_WORKERS)
        total_batches = sum(math.ceil(len(c) / _ACCT_BATCH) for c in chunks if c)
        completed = 0

        yield _sse({
            "message": f"Querying {len(account_ids)} accounts in parallel ({_PARALLEL_WORKERS} workers)...",
            "progress": 1,
        })

        accounts_raw: dict = {}
        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            futures = [
                pool.submit(_par_query_accounts, chunk, _ACCT_BATCH, progress_q, idx + 1)
                for idx, chunk in enumerate(chunks) if chunk
            ]
            while not all(f.done() for f in futures):
                time.sleep(0.3)
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    completed += 1
                    pct = 1 + int((completed / max(total_batches, 1)) * 7)
                    yield _sse({
                        "message": f"Querying accounts... ({evt['fetched']} fetched)",
                        "progress": min(pct, 8),
                    })
            for evt in _drain(progress_q):
                if evt.get('retry'):
                    yield _sse({"message": evt['message']})
                    continue
                completed += 1
                pct = 1 + int((completed / max(total_batches, 1)) * 7)
                yield _sse({
                    "message": f"Querying accounts... ({evt['fetched']} fetched)",
                    "progress": min(pct, 8),
                })
            for f in futures:
                accounts_raw.update(f.result())

        if not accounts_raw:
            yield _sse({"error": "Failed to query any accounts"})
            return

        yield _sse({
            "message": f"Retrieved {len(accounts_raw)} accounts. Getting territory details...",
            "progress": 8,
        })

        # ----------------------------------------------------------
        # Phase 3: Parallel territory queries (3 workers)
        # ----------------------------------------------------------
        phase = "querying territories"
        territory_ids = list({
            acct.get("_territoryid_value")
            for acct in accounts_raw.values()
            if acct.get("_territoryid_value")
        })

        territories_raw: dict = {}
        if territory_ids:
            t_chunks = _split_chunks(territory_ids, _PARALLEL_WORKERS)
            t_total = sum(math.ceil(len(c) / _ACCT_BATCH) for c in t_chunks if c)
            t_done = 0

            yield _sse({
                "message": f"Querying {len(territory_ids)} territories in parallel...",
                "progress": 8,
            })

            with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
                futures = [
                    pool.submit(_par_query_territories, chunk, _ACCT_BATCH, progress_q, idx + 1)
                    for idx, chunk in enumerate(t_chunks) if chunk
                ]
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain(progress_q):
                        if evt.get('retry'):
                            yield _sse({"message": evt['message']})
                            continue
                        t_done += 1
                        pct = 8 + int((t_done / max(t_total, 1)) * 1)
                        yield _sse({
                            "message": f"Querying territories... ({evt['fetched']} fetched)",
                            "progress": min(pct, 9),
                        })
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    t_done += 1
                    pct = 8 + int((t_done / max(t_total, 1)) * 1)
                    yield _sse({
                        "message": f"Querying territories... ({evt['fetched']} fetched)",
                        "progress": min(pct, 9),
                    })
                for f in futures:
                    territories_raw.update(f.result())

        yield _sse({"message": "Processing account data...", "progress": 9})

        # ----------------------------------------------------------
        # Process accounts into data structures (same as sequential)
        # ----------------------------------------------------------
        phase = "processing account data"

        # Build a TPID -> website map, preferring top-level parent's website.
        # msp_parentinglevelcode 861980000 = Top, 861980001 = Child
        tpid_website_map: dict = {}  # tpid (str) -> website domain
        for acct in accounts_raw.values():
            raw_tpid = acct.get("msp_mstopparentid")
            website = acct.get("websiteurl") or ""
            if not raw_tpid or not website:
                continue
            level_code = acct.get("msp_parentinglevelcode")
            is_top = (level_code == 861980000)
            domain = _extract_domain(website)
            if not domain:
                continue
            # Top-level parent always wins; otherwise first-come
            if raw_tpid not in tpid_website_map or is_top:
                tpid_website_map[raw_tpid] = domain

        accounts_data = []
        pod_accounts = {}
        territories_seen = {}
        verticals_seen = set()

        for account_id, acct in accounts_raw.items():
            territory_id = acct.get("_territoryid_value")
            territory_info = None
            pod_name = None

            if territory_id and territory_id in territories_raw:
                terr = territories_raw[territory_id]
                terr_name = terr.get("name", "")
                pod_name = _derive_pod_name(terr_name)
                territory_info = {
                    "id": territory_id,
                    "name": terr_name,
                    "atu": terr.get("msp_accountteamunitname"),
                }

            vertical = acct.get(
                "msp_verticalcode@OData.Community.Display.V1.FormattedValue")
            vertical_category = acct.get(
                "msp_verticalcategorycode@OData.Community.Display.V1.FormattedValue")

            # Cast TPID to int (API returns strings, column is BigInteger)
            raw_tpid = acct.get("msp_mstopparentid")
            try:
                tpid_int = int(raw_tpid) if raw_tpid else None
            except (ValueError, TypeError):
                tpid_int = None

            account_data = {
                "id": account_id,
                "name": acct.get("name"),
                "tpid": tpid_int,
                "url": build_account_url(account_id),
                "parenting_level_code": acct.get("msp_parentinglevelcode"),
                "website": tpid_website_map.get(acct.get("msp_mstopparentid")),
                "vertical": vertical,
                "vertical_category": vertical_category,
                "territory_name": territory_info.get("name") if territory_info else None,
                "seller_name": None,
                "seller_type": None,
                "pod_name": pod_name,
                "owner_id": acct.get("_ownerid_value"),
            }
            accounts_data.append(account_data)

            if territory_info and territory_info.get("name"):
                territories_seen[territory_info["name"]] = {
                    "name": territory_info["name"],
                    "atu": territory_info.get("atu"),
                    "pod_name": pod_name,
                }

            if pod_name:
                pod_accounts.setdefault(pod_name, []).append(account_id)

            if vertical and vertical.upper() != "N/A":
                verticals_seen.add(vertical)
            if vertical_category and vertical_category.upper() != "N/A":
                verticals_seen.add(vertical_category)

        accounts_data.sort(key=_account_import_sort_key)

        yield _sse({
            "message": (
                f"Found {len(accounts_data)} accounts, "
                f"{len(territories_seen)} territories, "
                f"{len(pod_accounts)} PODs"
            ),
            "progress": 9,
        })

        # ----------------------------------------------------------
        # Phase 4: Parallel team queries (3 workers)
        # ----------------------------------------------------------
        phase = "querying sellers and SEs"
        all_ids = [a["id"] for a in accounts_data]
        a_chunks = _split_chunks(all_ids, _PARALLEL_WORKERS)
        team_total = sum(math.ceil(len(c) / _TEAM_BATCH) for c in a_chunks if c)
        team_done = 0

        yield _sse({
            "message": (
                f"Fetching sellers and SEs in parallel "
                f"({_PARALLEL_WORKERS} workers, ~{team_total} batches)..."
            ),
            "progress": 9,
        })

        account_sellers: dict = {}
        sellers_seen: dict = {}
        account_ses: dict = {}

        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
            futures = [
                pool.submit(_par_query_teams, chunk, _TEAM_BATCH, progress_q, idx + 1)
                for idx, chunk in enumerate(a_chunks) if chunk
            ]
            while not all(f.done() for f in futures):
                time.sleep(0.3)
                for evt in _drain(progress_q):
                    if evt.get('retry'):
                        yield _sse({"message": evt['message']})
                        continue
                    team_done += 1
                    pct = 9 + int((team_done / max(team_total, 1)) * 51)
                    if team_done % 3 == 0 or team_done == 1:
                        yield _sse({
                            "message": f"Querying teams batch {team_done}/{team_total}...",
                            "progress": min(pct, 60),
                        })
            for evt in _drain(progress_q):
                if evt.get('retry'):
                    yield _sse({"message": evt['message']})
                    continue
                team_done += 1
                pct = 9 + int((team_done / max(team_total, 1)) * 51)
                yield _sse({
                    "message": f"Querying teams batch {team_done}/{team_total}...",
                    "progress": min(pct, 60),
                })
            for f in futures:
                r = f.result()
                account_sellers.update(r["account_sellers"])
                sellers_seen.update(r["unique_sellers"])
                account_ses.update(r["account_ses"])

        # Populate seller info on accounts
        accounts_with_sellers = 0
        for ad in accounts_data:
            if ad["id"] in account_sellers:
                seller = account_sellers[ad["id"]]
                ad["seller_name"] = seller["name"]
                ad["seller_type"] = seller["type"]
                accounts_with_sellers += 1

        # Build pod_teams
        pod_teams = {}
        for pn, acct_ids in pod_accounts.items():
            pod_teams[pn] = {"data_se": [], "infra_se": [], "apps_se": []}
            seen_ses = {"data_se": set(), "infra_se": set(), "apps_se": set()}
            for aid in acct_ids:
                if aid in account_ses:
                    for role in ("data_se", "infra_se", "apps_se"):
                        for se in account_ses[aid].get(role, []):
                            if se["name"] not in seen_ses[role]:
                                seen_ses[role].add(se["name"])
                                pod_teams[pn][role].append(se)

        yield _sse({
            "message": (
                f"Found {len(sellers_seen)} sellers, "
                f"core solution engineers for "
                f"{accounts_with_sellers}/{len(accounts_data)} accounts"
            ),
            "progress": 88,
        })

        # ----------------------------------------------------------
        # Phase 4d: Parallel alias resolution for all people
        # ----------------------------------------------------------
        phase = "resolving aliases"

        # Collect all unique user_ids from every source
        all_user_ids: set = set()
        alias_cache: dict = {}
        for info in sellers_seen.values():
            if info.get("user_id"):
                if info.get("alias"):
                    alias_cache[info["user_id"]] = {
                        "alias": info["alias"],
                        "fullname": info.get("name"),
                    }
                else:
                    all_user_ids.add(info["user_id"])
        for team in pod_teams.values():
            for se_list in team.values():
                for se_info in se_list:
                    if se_info.get("user_id"):
                        if se_info.get("alias"):
                            alias_cache[se_info["user_id"]] = {
                                "alias": se_info["alias"],
                                "fullname": se_info.get("name"),
                            }
                        else:
                            all_user_ids.add(se_info["user_id"])
        for ad in accounts_data:
            if ad.get("owner_id"):
                if ad["owner_id"] not in alias_cache:
                    all_user_ids.add(ad["owner_id"])

        alias_total = len(all_user_ids)
        alias_done = 0

        if alias_total > 0:
            yield _sse({
                "message": f"Resolving aliases (0/{alias_total})...",
                "progress": 88,
            })

            alias_workers = min(_PARALLEL_WORKERS, alias_total)
            with ThreadPoolExecutor(max_workers=alias_workers) as pool:
                futures = {
                    pool.submit(get_user_info, uid): uid
                    for uid in all_user_ids
                }
                for future in as_completed(futures):
                    alias_cache[futures[future]] = future.result()
                    alias_done += 1
                    yield _sse({
                        "message": f"Resolving aliases ({alias_done}/{alias_total})...",
                        "progress": 88 + round((alias_done / alias_total) * 2),
                    })

        def _cached_alias(user_id):
            """Look up alias from pre-resolved cache."""
            if not user_id:
                return None
            info = alias_cache.get(user_id)
            return info["alias"] if info else None

        # ----------------------------------------------------------
        # Phase 5: Database writes (sequential)
        # ----------------------------------------------------------
        phase = "creating database records"
        yield _sse({"message": "Creating PODs...", "progress": 90})

        pods_map = {}
        pods_created = 0
        for pn in pod_accounts:
            existing = POD.query.filter_by(name=pn).first()
            if existing:
                pods_map[pn] = existing
            else:
                pod = POD(name=pn)
                db.session.add(pod)
                pods_map[pn] = pod
                pods_created += 1
        db.session.flush()

        # Clear all POD associations so they get rebuilt fresh from MSX
        # This prevents stale memberships from accumulating over time.
        for pod in pods_map.values():
            pod.territories = []
            pod.solution_engineers = []
        db.session.flush()

        yield _sse({
            "message": f"Created {pods_created} new PODs (rebuilding associations)",
            "progress": 90,
        })

        # Territories
        yield _sse({"message": "Creating territories...", "progress": 90})
        territories_map = {}
        territories_created = 0
        for terr_name, terr_info in territories_seen.items():
            existing = Territory.query.filter_by(
                name=terr_name).first()
            if existing:
                territories_map[terr_name] = existing
                # Always update pod assignment from MSX (authoritative)
                if terr_info.get("pod_name"):
                    existing.pod = pods_map.get(terr_info["pod_name"])
            else:
                territory = Territory(name=terr_name)
                if terr_info.get("pod_name"):
                    territory.pod = pods_map.get(terr_info["pod_name"])
                db.session.add(territory)
                territories_map[terr_name] = territory
                territories_created += 1
        db.session.flush()

        yield _sse({
            "message": f"Created {territories_created} new territories",
            "progress": 90,
        })

        # Sellers
        yield _sse({"message": "Creating sellers...", "progress": 91})
        sellers_map = {}
        sellers_created = 0
        sellers_updated = 0
        sellers_total = len(sellers_seen)
        for seller_idx, (seller_name, seller_info) in enumerate(sellers_seen.items(), 1):
            existing = Seller.query.filter_by(
                name=seller_name).first()
            if existing:
                sellers_map[seller_name] = existing
                # Update seller_type and alias from MSX (authoritative)
                seller_changed = False
                new_type = seller_info.get("type", "Growth")
                if new_type and existing.seller_type != new_type:
                    existing.seller_type = new_type
                    seller_changed = True
                systemuser_id = seller_info.get("user_id")
                if systemuser_id and not existing.alias:
                    new_alias = _cached_alias(systemuser_id)
                    if new_alias:
                        existing.alias = new_alias
                        seller_changed = True
                if seller_changed:
                    sellers_updated += 1
            else:
                seller_type = seller_info.get("type", "Growth")
                systemuser_id = seller_info.get("user_id")
                alias = _cached_alias(systemuser_id)
                seller = Seller(
                    name=seller_name, seller_type=seller_type,
                    alias=alias,
                )
                db.session.add(seller)
                sellers_map[seller_name] = seller
                sellers_created += 1
            if seller_idx % 5 == 0 or seller_idx == sellers_total:
                yield _sse({
                    "message": f"Creating sellers ({seller_idx}/{sellers_total})...",
                    "progress": 91 + round((seller_idx / max(sellers_total, 1)) * 1),
                })
        db.session.flush()

        # Associate sellers with territories
        synced_territories = set(territories_map.values())
        for seller in Seller.query.all():
            seller.territories = [
                territory
                for territory in seller.territories
                if territory not in synced_territories
            ]
        for ad in accounts_data:
            sn = ad.get("seller_name")
            tn = ad.get("territory_name")
            if sn and tn:
                s = sellers_map.get(sn)
                t = territories_map.get(tn)
                if s and t and t not in s.territories:
                    s.territories.append(t)
        db.session.flush()

        yield _sse({
            "message": f"Created {sellers_created} new sellers, updated {sellers_updated}",
            "progress": 92,
        })

        # Solution Engineers
        yield _sse({"message": "Creating solution engineers...", "progress": 92})
        se_items_total = sum(
            len(se_list)
            for team in pod_teams.values()
            for se_list in team.values()
        )
        se_items_done = 0
        se_map = {}
        ses_created = 0
        specialty_map = {
            "data_se": "Azure Data",
            "infra_se": "Azure Core and Infra",
            "apps_se": "Azure Apps and AI",
        }
        for pn, team in pod_teams.items():
            pod = pods_map.get(pn)
            if not pod:
                continue
            for se_role, specialty in specialty_map.items():
                for se_info in team.get(se_role, []):
                    se_items_done += 1
                    if not se_info.get("name"):
                        continue
                    se_key = (se_info["name"], specialty)
                    if se_key not in se_map:
                        existing = SolutionEngineer.query.filter_by(
                            name=se_info["name"], specialty=specialty,
                        ).first()
                        if existing:
                            se_map[se_key] = existing
                        else:
                            systemuser_id = se_info.get("user_id")
                            alias = _cached_alias(systemuser_id)
                            se = SolutionEngineer(
                                name=se_info["name"], alias=alias,
                                specialty=specialty,
                            )
                            db.session.add(se)
                            se_map[se_key] = se
                            ses_created += 1
                    se = se_map.get(se_key)
                    if se and pod not in se.pods:
                        se.pods.append(pod)
                    if se_items_done % 5 == 0 or se_items_done == se_items_total:
                        yield _sse({
                            "message": f"Creating SEs ({se_items_done}/{se_items_total})...",
                            "progress": 92 + round((se_items_done / max(se_items_total, 1)) * 1),
                        })
        db.session.flush()

        yield _sse({
            "message": f"Created {ses_created} new solution engineers",
            "progress": 93,
        })

        # DAE aliases already resolved in alias_cache (Phase 4d)
        owner_alias_cache = alias_cache
        daes_created = 0
        daes_updated = 0

        # Pre-build InternalContact lookup for DAE upserts
        internal_contacts_by_alias: dict = {
            ic.alias: ic
            for ic in InternalContact.query.filter(
                InternalContact.alias.isnot(None)
            ).all()
        }

        # Verticals
        yield _sse({"message": "Creating verticals...", "progress": 96})
        verticals_map = {}
        verticals_created = 0
        for vn in verticals_seen:
            existing = Vertical.query.filter_by(
                name=vn).first()
            if existing:
                verticals_map[vn] = existing
            else:
                vertical = Vertical(name=vn)
                db.session.add(vertical)
                verticals_map[vn] = vertical
                verticals_created += 1
        db.session.flush()

        yield _sse({
            "message": f"Created {verticals_created} new verticals",
            "progress": 97,
        })

        # Customers
        yield _sse({"message": "Syncing customers...", "progress": 97})
        customers_created = 0
        customers_skipped = 0
        customers_updated = 0
        customers_unchanged = 0
        seen_tpids = set()  # In-memory dedup for same-TPID accounts in batch

        # Pre-load existing TPIDs to avoid per-row DB queries + autoflush.
        # Query ALL tpids (unique constraint is on tpid alone, not per-user).
        # Normalize to int -- old imports may have stored TPIDs as strings.
        def _safe_int(val):
            try:
                return int(val) if val is not None else None
            except (ValueError, TypeError):
                return val

        # Pre-load existing customers keyed by TPID for upsert comparison
        existing_customers_by_tpid: dict = {}
        for cust in Customer.query.all():
            normalized = _safe_int(cust.tpid)
            if normalized is not None:
                existing_customers_by_tpid[normalized] = cust

        with db.session.no_autoflush:
            logger.info(
                "Customer import: %d existing TPIDs in DB, %d accounts to process",
                len(existing_customers_by_tpid), len(accounts_data),
            )
            for idx, ad in enumerate(accounts_data, 1):
                if idx % 50 == 0:
                    yield _sse({
                        "message": f"Processing customer {idx}/{len(accounts_data)}...",
                        "progress": 99,
                    })

                tpid = ad.get("tpid")
                customer_name = ad.get("name")
                if not tpid or not customer_name:
                    customers_skipped += 1
                    continue

                # Skip duplicates within the same import batch
                if tpid in seen_tpids:
                    customers_skipped += 1
                    continue
                seen_tpids.add(tpid)

                if tpid in existing_customers_by_tpid:
                    # --- Upsert existing customer ---
                    cust = existing_customers_by_tpid[tpid]
                    changed = False

                    # Update name (could be rebrand — log it)
                    if customer_name and cust.name != customer_name:
                        logger.info(
                            "Customer name changed for TPID %s: '%s' -> '%s'",
                            tpid, cust.name, customer_name,
                        )
                        cust.name = customer_name
                        changed = True

                    # MSX account sync is authoritative for the canonical
                    # account represented by this TPID.
                    old_tpid_url = cust.tpid_url
                    if _update_customer_tpid_url(cust, ad):
                        logger.info(
                            "MSX account URL changed for TPID %s: '%s' -> '%s'",
                            tpid, old_tpid_url, cust.tpid_url,
                        )
                        changed = True

                    # Backfill website
                    if ad.get("website") and not cust.website:
                        cust.website = ad["website"]
                        changed = True

                    # Update territory (MSX is authoritative)
                    territory_name = ad.get("territory_name")
                    new_territory = territories_map.get(territory_name) if territory_name else None
                    if new_territory and cust.territory != new_territory:
                        logger.info(
                            "Territory changed for '%s' (TPID %s): '%s' -> '%s'",
                            cust.name, tpid,
                            cust.territory.name if cust.territory else None,
                            new_territory.name,
                        )
                        cust.territory = new_territory
                        changed = True

                    # Update seller (MSX is authoritative)
                    seller_name = ad.get("seller_name")
                    new_seller = sellers_map.get(seller_name) if seller_name else None
                    if new_seller and cust.seller != new_seller:
                        logger.info(
                            "Seller changed for '%s' (TPID %s): '%s' -> '%s'",
                            cust.name, tpid,
                            cust.seller.name if cust.seller else None,
                            new_seller.name,
                        )
                        cust.seller = new_seller
                        changed = True

                    # Update verticals (replace with current MSX verticals)
                    new_verticals = []
                    if ad.get("vertical") and ad["vertical"] in verticals_map:
                        new_verticals.append(verticals_map[ad["vertical"]])
                    if ad.get("vertical_category") and ad["vertical_category"] in verticals_map:
                        vert = verticals_map[ad["vertical_category"]]
                        if vert not in new_verticals:
                            new_verticals.append(vert)
                    if set(new_verticals) != set(cust.verticals):
                        cust.verticals = new_verticals
                        changed = True

                    # Update DAE (account owner) from MSX
                    owner_id = ad.get("owner_id")
                    if owner_id:
                        info = owner_alias_cache.get(owner_id)
                        alias = info["alias"] if info else None
                        fullname = info["fullname"] if info else None
                        if alias and cust.dae_alias != alias:
                            cust.dae_alias = alias
                            changed = True
                        if fullname and cust.dae_name != fullname:
                            cust.dae_name = fullname
                            changed = True
                        elif alias and not cust.dae_name:
                            cust.dae_name = alias
                            changed = True
                        # Upsert InternalContact for DAE
                        if alias and alias not in internal_contacts_by_alias:
                            ic = InternalContact(
                                name=fullname or alias,
                                alias=alias,
                                role='DAE',
                            )
                            db.session.add(ic)
                            internal_contacts_by_alias[alias] = ic
                            daes_created += 1
                        elif alias and fullname:
                            ic = internal_contacts_by_alias[alias]
                            if ic.name != fullname:
                                ic.name = fullname
                                daes_updated += 1

                    if changed:
                        customers_updated += 1
                    else:
                        customers_unchanged += 1

                    continue

                # --- Create new customer ---
                customer = Customer(
                    name=customer_name, tpid=tpid,
                    tpid_url=ad.get("url"),
                    website=ad.get("website"),
                )
                territory_name = ad.get("territory_name")
                if territory_name and territory_name in territories_map:
                    customer.territory = territories_map[territory_name]
                seller_name = ad.get("seller_name")
                if seller_name and seller_name in sellers_map:
                    customer.seller = sellers_map[seller_name]
                if ad.get("vertical") and ad["vertical"] in verticals_map:
                    customer.verticals.append(verticals_map[ad["vertical"]])
                if ad.get("vertical_category") and ad["vertical_category"] in verticals_map:
                    vert = verticals_map[ad["vertical_category"]]
                    if vert not in customer.verticals:
                        customer.verticals.append(vert)
                # DAE
                owner_id = ad.get("owner_id")
                if owner_id:
                    info = owner_alias_cache.get(owner_id)
                    if info:
                        customer.dae_alias = info["alias"]
                        customer.dae_name = info.get("fullname") or info["alias"]
                        # Upsert InternalContact for DAE
                        alias = info["alias"]
                        if alias and alias not in internal_contacts_by_alias:
                            ic = InternalContact(
                                name=info.get("fullname") or alias,
                                alias=alias,
                                role='DAE',
                            )
                            db.session.add(ic)
                            internal_contacts_by_alias[alias] = ic
                            daes_created += 1
                db.session.add(customer)
                existing_customers_by_tpid[tpid] = customer
                customers_created += 1

        try:
            db.session.commit()
        except Exception as commit_err:
            # IntegrityError safety net: rollback and retry one-by-one
            logger.warning("Batch commit failed (%s), retrying individually", commit_err)
            db.session.rollback()

            # Re-check what's already in DB after rollback
            existing_tpids_after = set()
            for row in db.session.query(Customer.tpid).all():
                n = _safe_int(row[0])
                if n is not None:
                    existing_tpids_after.add(n)

            retry_created = 0
            for idx, ad in enumerate(accounts_data, 1):
                tpid = ad.get("tpid")
                customer_name = ad.get("name")
                if not tpid or not customer_name:
                    continue
                if tpid in existing_tpids_after:
                    continue

                try:
                    cust = Customer(
                        name=customer_name, tpid=tpid,
                        tpid_url=ad.get("url"),
                    )
                    territory_name = ad.get("territory_name")
                    if territory_name and territory_name in territories_map:
                        cust.territory = territories_map[territory_name]
                    seller_name = ad.get("seller_name")
                    if seller_name and seller_name in sellers_map:
                        cust.seller = sellers_map[seller_name]
                    db.session.add(cust)
                    db.session.commit()
                    existing_tpids_after.add(tpid)
                    retry_created += 1
                except Exception:
                    db.session.rollback()

            customers_created = retry_created

        SyncStatus.mark_completed(
            'accounts', success=True,
            items_synced=customers_created,
            details=json.dumps({
                'sync_version': ACCOUNT_SYNC_VERSION,
                'source': 'alignment' if _use_alignment else 'accountteams',
                'created': customers_created,
                'updated': customers_updated,
                'unchanged': customers_unchanged,
                'skipped': customers_skipped,
            }),
        )

        # Persist the list of TPIDs seen during this sync for FY finalization
        try:
            import json as _json
            tpid_file = Path(current_app.instance_path).parent / 'data' / 'last_sync_tpids.json'
            tpid_file.write_text(_json.dumps(sorted(seen_tpids)))
            logger.info("Saved %d synced TPIDs to %s", len(seen_tpids), tpid_file)
        except Exception as e:
            logger.warning("Failed to save synced TPIDs: %s", e)

        # Stale detection: flag customers whose TPIDs disappeared from MSX
        stale_flagged = 0
        stale_cleared = 0
        known_tpids = set(existing_customers_by_tpid.keys())
        disappeared = known_tpids - seen_tpids
        reappeared = known_tpids & seen_tpids
        for tpid_val in disappeared:
            cust = existing_customers_by_tpid[tpid_val]
            if not cust.stale_since:
                cust.stale_since = utc_now()
                stale_flagged += 1
        for tpid_val in reappeared:
            cust = existing_customers_by_tpid[tpid_val]
            if cust.stale_since:
                cust.stale_since = None
                stale_cleared += 1
        if stale_flagged or stale_cleared:
            db.session.commit()
            logger.info(
                "Stale detection: %d flagged, %d cleared",
                stale_flagged, stale_cleared,
            )

        duration = round(time.time() - import_start_time, 1)
        yield _sse({
            "message": "Import complete!",
            "progress": 100,
            "complete": True,
            "summary": {
                "pods_created": pods_created,
                "territories_created": territories_created,
                "sellers_created": sellers_created,
                "sellers_updated": sellers_updated,
                "solution_engineers_created": ses_created,
                "daes_created": daes_created,
                "daes_updated": daes_updated,
                "verticals_created": verticals_created,
                "customers_created": customers_created,
                "customers_skipped": customers_skipped,
                "customers_updated": customers_updated,
                "customers_unchanged": customers_unchanged,
                "duration": duration,
            },
        })

        # Kick off the meeting sync in the background now that we have
        # customers in the DB. Onboarding users were getting a calendar
        # full of unmatched ghosts because aura ran (or had run) before
        # the account import completed. Fire-and-forget; aura manages
        # its own lock and idempotency so a concurrent scheduled run
        # is harmless.
        if not _skip_post_import_aura and Customer.query.first() is not None:
            try:
                from app.services.sync_jobs import admit_sync
                admit_sync('meeting-aura', initiator='followup')
                logger.info(
                    "Post-import: meeting sync kicked off in background"
                )
            except Exception:
                # Non-fatal: scheduled aura will still pick this up at 7 AM.
                logger.exception(
                    "Post-import: failed to start background aura"
                )

        logger.info(
            f"Parallel MSX Import complete in {duration}s: "
            f"{pods_created} PODs, {territories_created} territories, "
            f"{sellers_created} sellers ({sellers_updated} updated), {ses_created} SEs, "
            f"{daes_created} DAEs ({daes_updated} updated), "
            f"{verticals_created} verticals, "
            f"{customers_created} customers created, {customers_updated} updated, "
            f"{customers_unchanged} unchanged, {customers_skipped} skipped"
        )

    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        # Mark accounts sync as failed if it was started
        try:
            SyncStatus.mark_completed('accounts', success=False, details=str(e))
        except Exception:
            pass
        error_detail = f"[{type(e).__name__}] {e}"
        logger.exception(
            f"Error during parallel MSX import (phase: {phase}): {error_detail}"
        )
        yield _sse({"error": f"Import failed during '{phase}': {error_detail}"})


def import_selected_accounts(accounts: list[dict], territories: list[dict]) -> dict:
    """Import the captured selected account payload with existing behavior."""
    territories_data = territories
    try:
        SyncStatus.mark_started('accounts')

        territories_created = 0
        sellers_created = 0
        customers_created = 0
        customers_skipped = 0

        # Track created entities to avoid duplicates within this import
        territory_map = {}  # name -> Territory object
        seller_map = {}  # name -> Seller object

        # 1. Create territories
        for t_data in territories_data:
            territory_name = t_data.get("name")
            if not territory_name:
                continue

            # Check if territory already exists
            territory = Territory.query.filter_by(name=territory_name).first()
            if not territory:
                territory = Territory(name=territory_name)
                db.session.add(territory)
                territories_created += 1
                logger.info(f"Created territory: {territory_name}")

            territory_map[territory_name] = territory

            # Create seller from territory owner if we have the info
            seller_name = t_data.get("seller_name")
            if seller_name and seller_name not in seller_map:
                seller = Seller.query.filter_by(name=seller_name).first()
                if not seller:
                    seller = Seller(name=seller_name)
                    db.session.add(seller)
                    sellers_created += 1
                    logger.info(f"Created seller: {seller_name}")
                seller_map[seller_name] = seller

        db.session.flush()  # Flush to get IDs for the territories and sellers

        # 2. Upsert customers from accounts
        customers_updated = 0
        customers_unchanged = 0
        seen_tpids = set()  # In-memory dedup for same-TPID accounts in batch

        # Pre-load existing customers keyed by TPID for upsert comparison.
        # Normalize to int -- old imports may have stored TPIDs as strings.
        existing_customers_by_tpid: dict = {}
        for cust in Customer.query.all():
            try:
                val = int(cust.tpid) if cust.tpid is not None else None
            except (ValueError, TypeError):
                val = cust.tpid
            if val is not None:
                existing_customers_by_tpid[val] = cust

        with db.session.no_autoflush:
            for acct in accounts:
                raw_tpid = acct.get("tpid")
                # Cast to int (API may return strings, column is BigInteger)
                try:
                    tpid = int(raw_tpid) if raw_tpid else None
                except (ValueError, TypeError):
                    tpid = None
                customer_name = acct.get("name")
                territory_name = acct.get("territory_name")
                seller_name = acct.get("seller_name")

                if not tpid or not customer_name:
                    continue

                # Skip duplicates within the same import batch
                if tpid in seen_tpids:
                    customers_skipped += 1
                    continue
                seen_tpids.add(tpid)

                # Check if customer already exists by TPID
                if tpid in existing_customers_by_tpid:
                    # --- Upsert existing customer ---
                    cust = existing_customers_by_tpid[tpid]
                    changed = False

                    if customer_name and cust.name != customer_name:
                        cust.name = customer_name
                        changed = True

                    new_territory = territory_map.get(territory_name) if territory_name else None
                    if new_territory and cust.territory != new_territory:
                        cust.territory = new_territory
                        changed = True

                    new_seller = seller_map.get(seller_name) if seller_name else None
                    if new_seller and cust.seller != new_seller:
                        cust.seller = new_seller
                        changed = True

                    if changed:
                        customers_updated += 1
                    else:
                        customers_unchanged += 1

                    continue

                # Create new customer
                customer = Customer(
                    name=customer_name,
                    tpid=tpid
                )

                # Associate with territory if we have it
                if territory_name and territory_name in territory_map:
                    customer.territory = territory_map[territory_name]

                # Associate with seller if we have it
                if seller_name and seller_name in seller_map:
                    customer.seller = seller_map[seller_name]

                db.session.add(customer)
                existing_customers_by_tpid[tpid] = customer
                customers_created += 1

        db.session.commit()

        SyncStatus.mark_completed(
            'accounts', success=True,
            items_synced=customers_created,
            details=f'{customers_created} created, {customers_updated} updated, {customers_unchanged} unchanged, {customers_skipped} skipped',
        )

        logger.info(f"API Import complete: {territories_created} territories, {sellers_created} sellers, {customers_created} customers created, {customers_updated} updated, {customers_unchanged} unchanged, {customers_skipped} skipped")

        return {
            "success": True,
            "territories_created": territories_created,
            "sellers_created": sellers_created,
            "customers_created": customers_created,
            "customers_skipped": customers_skipped,
            "customers_updated": customers_updated,
            "customers_unchanged": customers_unchanged,
        }

    except Exception as e:
        db.session.rollback()
        SyncStatus.mark_completed('accounts', success=False, details=str(e))
        logger.exception("Error importing accounts from MSX")
        return {"success": False, "error": str(e)}
