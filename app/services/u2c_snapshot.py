"""
U2C (Uncommitted to Committed) snapshot service.

Takes a point-in-time snapshot of all uncommitted milestones on open
opportunities at the start of each fiscal quarter.  The snapshot forms
a fixed baseline so attainment (how many milestones moved to Committed)
can be tracked throughout the quarter.

Snapshots are created automatically on the 5th of each FQ start month
(Jul, Oct, Jan, Apr) during the scheduled milestone sync, or manually
from the admin panel / U2C report page.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app.models import (
    Customer, Milestone, Opportunity, U2CSnapshot, U2CSnapshotItem,
    U2CSnapshotVersion, db,
)

logger = logging.getLogger(__name__)

# Microsoft fiscal quarter start months (calendar month -> FQ)
FQ_START_MONTHS = {7, 10, 1, 4}
SNAPSHOT_DAY = 5  # Day of month to take snapshot


def current_fiscal_quarter(ref_date: date | None = None) -> str:
    """Return the fiscal quarter label for a given date (default: today).

    Microsoft FY starts July 1.
    Returns e.g. 'FY26 Q4' for April 2026.
    """
    d = ref_date or date.today()
    month = d.month
    year = d.year
    if month >= 7:
        fy = year + 1
        q = 1 if month <= 9 else 2
    else:
        fy = year
        q = 3 if month <= 3 else 4
    return f"FY{fy % 100:02d} Q{q}"


def fiscal_quarter_date_range(fq_label: str) -> tuple[date, date]:
    """Return (start_date, end_date) for a fiscal quarter label like 'FY26 Q4'.

    Args:
        fq_label: Fiscal quarter label, e.g. 'FY26 Q4'.

    Returns:
        Tuple of (first day of quarter, last day of quarter).
    """
    parts = fq_label.replace('FY', '').split(' Q')
    fy = int(parts[0]) + 2000
    q = int(parts[1])
    # Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun
    quarter_starts = {1: (7, fy - 1), 2: (10, fy - 1), 3: (1, fy), 4: (4, fy)}
    start_month, start_year = quarter_starts[q]
    start = date(start_year, start_month, 1)
    end_month = start_month + 3
    end_year = start_year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    end = date(end_year, end_month, 1) - timedelta(days=1)
    return start, end


def previous_fiscal_quarter(fq_label: str) -> str:
    """Return the fiscal quarter label immediately before the given one.

    Args:
        fq_label: Fiscal quarter label, e.g. 'FY27 Q1'.

    Returns:
        The preceding quarter, e.g. 'FY26 Q4'.
    """
    parts = fq_label.replace('FY', '').split(' Q')
    fy = int(parts[0])
    q = int(parts[1])
    if q == 1:
        return f"FY{(fy - 1) % 100:02d} Q4"
    return f"FY{fy % 100:02d} Q{q - 1}"


def is_snapshot_due() -> bool:
    """Return True if today is the 5th of a FQ start month and no snapshot exists yet."""
    today = date.today()
    if today.day != SNAPSHOT_DAY:
        return False
    if today.month not in FQ_START_MONTHS:
        return False
    fq = current_fiscal_quarter(today)
    return not U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()


def create_snapshot(fq_label: str | None = None) -> dict:
    """Create a U2C snapshot for the given fiscal quarter.

    Captures all uncommitted milestones on open opportunities for customers
    that have synced milestones.

    Args:
        fq_label: Fiscal quarter to snapshot (e.g. 'FY26 Q4').
                  Defaults to current fiscal quarter.

    Returns:
        Dict with 'success', 'snapshot_id', 'total_items', 'total_monthly_acr'.
    """
    fq = fq_label or current_fiscal_quarter()

    existing = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
    if existing:
        return {
            'success': False,
            'error': f'Snapshot already exists for {fq}',
            'snapshot_id': existing.id,
        }

    # Query uncommitted milestones on open opportunities
    milestones = (
        Milestone.query
        .join(Opportunity, Milestone.opportunity_id == Opportunity.id)
        .join(Customer, Milestone.customer_id == Customer.id)
        .filter(
            Milestone.customer_commitment == 'Uncommitted',
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Opportunity.statecode == 0,  # Open
        )
        .options(
            db.joinedload(Milestone.customer),
            db.joinedload(Milestone.opportunity),
        )
        .all()
    )

    snapshot = U2CSnapshot(
        fiscal_quarter=fq,
        snapshot_date=datetime.now(timezone.utc),
    )
    db.session.add(snapshot)
    db.session.flush()  # Get snapshot.id

    total_acr = 0.0
    for ms in milestones:
        acr = ms.monthly_usage or 0.0
        item = U2CSnapshotItem(
            snapshot_id=snapshot.id,
            milestone_id=ms.id,
            customer_id=ms.customer_id,
            customer_name=ms.customer.name if ms.customer else 'Unknown',
            milestone_title=ms.title or ms.milestone_number or 'Untitled',
            milestone_number=ms.milestone_number,
            workload=ms.workload,
            due_date=ms.due_date,
            monthly_acr=acr,
            opportunity_name=ms.opportunity.name if ms.opportunity else None,
            msx_status=ms.msx_status,
        )
        db.session.add(item)
        total_acr += acr

    snapshot.total_items = len(milestones)
    snapshot.total_monthly_acr = round(total_acr, 2)
    db.session.commit()

    logger.info(
        "U2C snapshot created for %s: %d milestones, $%.2f total monthly ACR",
        fq, len(milestones), total_acr,
    )

    return {
        'success': True,
        'snapshot_id': snapshot.id,
        'fiscal_quarter': fq,
        'total_items': len(milestones),
        'total_monthly_acr': round(total_acr, 2),
    }


def _resolve_local_records(row: dict) -> tuple[Milestone | None, Customer | None,
                                               Opportunity | None]:
    """Match an MSXi U2C row to our local milestone / customer / opportunity.

    Matching is by MSX business key (milestone number, opportunity number) and
    falls back to an exact case-insensitive customer-name match, since the MSXi
    report covers every milestone in the territory - including ones on accounts
    we have never synced.

    Args:
        row: One row from ``u2c_pull.pull_u2c_milestones``.

    Returns:
        Tuple of (milestone, customer, opportunity); any element may be None.
    """
    milestone = None
    if row.get('milestone_number'):
        milestone = (
            Milestone.query
            .filter_by(milestone_number=row['milestone_number'])
            .order_by(Milestone.id.desc())
            .first()
        )

    opportunity = None
    if row.get('opportunity_number'):
        opportunity = (
            Opportunity.query
            .filter_by(opportunity_number=row['opportunity_number'])
            .first()
        )
    if opportunity is None and milestone is not None:
        opportunity = milestone.opportunity

    customer = milestone.customer if milestone else None
    if customer is None and opportunity is not None:
        customer = opportunity.customer
    if customer is None and row.get('customer_name'):
        customer = Customer.query.filter(
            db.func.lower(Customer.name) == row['customer_name'].strip().lower()
        ).first()

    return milestone, customer, opportunity


def import_official_snapshot(fq_label: str | None = None,
                             territories: list[str] | None = None,
                             replace: bool = False,
                             version: str | None = None) -> dict:
    """Import the official MSX Insights U2C baseline for a fiscal quarter.

    Pulls the same milestone detail table the MSXi "Uncommitted to Committed"
    report shows, scoped to the territories configured in Sales Buddy, and
    stores it as the snapshot for that quarter.  The baseline comes from the
    report's "starting" columns, so a re-import reproduces the same baseline and
    only refreshes MSXi's current view of each milestone.

    Args:
        fq_label: Fiscal quarter to import (e.g. 'FY27 Q1').  Defaults to the
            current fiscal quarter.
        territories: Sales territory codes to scope to.  Defaults to the
            territories configured in Sales Buddy.
        replace: Replace an existing snapshot for this quarter.  Required when
            one already exists, so a manual snapshot is never silently dropped.
        version: MSXi snapshot version to import.  Defaults to the newest
            retained load.  Pass an explicit ``yyyymmdd`` member to import a
            specific weekly version, which is how a rolled-over quarter gets
            closed out.

    Returns:
        Dict with 'success' plus snapshot totals, or 'error' on failure.
    """
    from app.services.u2c_pull import (
        CURRENT_VERSION, U2CPullError, pull_u2c_milestones,
    )

    fq = fq_label or current_fiscal_quarter()
    pull_version = version or CURRENT_VERSION

    existing = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
    if existing and not replace:
        return {
            'success': False,
            'error': f'Snapshot already exists for {fq}',
            'snapshot_id': existing.id,
            'existing_source': existing.source,
            'needs_replace': True,
        }

    try:
        rows = pull_u2c_milestones(fq, territories=territories,
                                   version=pull_version)
    except U2CPullError as exc:
        logger.warning("Official U2C import for %s failed: %s", fq, exc)
        return {'success': False, 'error': str(exc)}

    if not rows:
        return {
            'success': False,
            # An empty pull is never destructive - we return here, before the
            # existing snapshot is touched. The automated refresh relies on
            # that, so keep this check above the delete.
            'error': (
                f'MSXi returned no uncommitted milestones for {fq}. '
                'Check that the quarter and your territories are correct.'
            ),
            'empty': True,
        }

    resolved = _resolve_version_label(fq, rows, pull_version, territories)

    if existing:
        db.session.delete(existing)
        db.session.flush()

    snapshot = U2CSnapshot(
        fiscal_quarter=fq,
        snapshot_date=datetime.now(timezone.utc),
        source=U2CSnapshot.SOURCE_MSXI,
    )
    db.session.add(snapshot)
    db.session.flush()  # Get snapshot.id

    matched, total_acr = _write_snapshot_items(snapshot, rows)
    _stamp_snapshot_pull(snapshot, rows, resolved)
    if resolved:
        record_version_totals(snapshot, resolved, rows)
    db.session.commit()

    logger.info(
        "Official MSXi U2C snapshot imported for %s (version %s): %d milestones "
        "(%d matched locally), $%.2f total monthly ACR",
        fq, resolved or pull_version, len(rows), matched, total_acr,
    )

    return {
        'success': True,
        'snapshot_id': snapshot.id,
        'fiscal_quarter': fq,
        'source': snapshot.source,
        'msxi_version': resolved,
        'total_items': len(rows),
        'matched_locally': matched,
        'unmatched': len(rows) - matched,
        'total_monthly_acr': round(total_acr, 2),
        'replaced': bool(existing),
    }


def _resolve_version_label(fq: str, rows: list[dict], pull_version: str,
                           territories: list[str] | None) -> str | None:
    """Turn the version we asked for into the dated member we actually got.

    ``'current'`` is an alias, so it has to be resolved by probing before it can
    be stored. Failing to resolve is not fatal - we just don't know the date.
    """
    from app.services.u2c_pull import (
        CURRENT_VERSION, U2CPullError, resolve_current_version,
    )

    if pull_version != CURRENT_VERSION:
        return pull_version
    try:
        return resolve_current_version(fq, territories=territories,
                                       current_rows=rows)
    except U2CPullError as exc:
        logger.warning("Could not resolve MSXi version for %s: %s", fq, exc)
        return None


def _write_snapshot_items(snapshot: U2CSnapshot,
                          rows: list[dict]) -> tuple[int, float]:
    """Replace a snapshot's items with the given MSXi rows.

    Args:
        snapshot: The snapshot to populate. Existing items are removed first.
        rows: Rows from ``u2c_pull.pull_u2c_milestones``.

    Returns:
        Tuple of (milestones matched to local records, total starting ACR).
    """
    U2CSnapshotItem.query.filter_by(snapshot_id=snapshot.id).delete()
    db.session.flush()

    total_acr = 0.0
    matched = 0
    for row in rows:
        milestone, customer, opportunity = _resolve_local_records(row)
        if milestone:
            matched += 1

        acr = float(row.get('starting_acr') or 0.0)
        due = row.get('starting_due_date')
        item = U2CSnapshotItem(
            snapshot_id=snapshot.id,
            milestone_id=milestone.id if milestone else None,
            customer_id=customer.id if customer else None,
            customer_name=row.get('customer_name') or 'Unknown',
            milestone_title=(
                row.get('milestone_name') or row.get('milestone_number') or 'Untitled'
            ),
            milestone_number=row.get('milestone_number'),
            workload=milestone.workload if milestone else None,
            due_date=datetime.combine(due, datetime.min.time()) if due else None,
            monthly_acr=acr,
            opportunity_name=(
                opportunity.name if opportunity
                else (milestone.opportunity_name if milestone else None)
            ),
            msx_status=row.get('starting_status'),
            opportunity_number=row.get('opportunity_number'),
            owner_alias=row.get('owner_alias'),
            msxi_commitment=row.get('current_commitment'),
            msxi_status=row.get('current_status'),
            msxi_converted_acr=float(row.get('converted_acr') or 0.0),
        )
        db.session.add(item)
        total_acr += acr

    snapshot.total_items = len(rows)
    snapshot.total_monthly_acr = round(total_acr, 2)
    return matched, total_acr


def _stamp_snapshot_pull(snapshot: U2CSnapshot, rows: list[dict],
                         version: str | None) -> None:
    """Record which MSXi load a snapshot's items came from."""
    from app.services.u2c_pull import fingerprint_rows

    snapshot.content_fingerprint = fingerprint_rows(rows)
    snapshot.last_refreshed_at = datetime.now(timezone.utc)
    if version:
        snapshot.msxi_version = version


def record_version_totals(snapshot: U2CSnapshot, version: str,
                          rows: list[dict]) -> U2CSnapshotVersion | None:
    """Store one weekly version's aggregates for the attainment trend.

    Dated MSXi versions are immutable, so an already-stored version is left
    alone. Returns the new row, or None if it already existed or the version
    isn't a parseable date (e.g. the ``'current'`` alias, which we only store
    once resolved to a real date).
    """
    from app.services.u2c_pull import version_to_date

    version_date = version_to_date(version)
    if version_date is None:
        return None

    existing = U2CSnapshotVersion.query.filter_by(
        snapshot_id=snapshot.id, msxi_version=version,
    ).first()
    if existing:
        return None

    entry = U2CSnapshotVersion(
        snapshot_id=snapshot.id,
        msxi_version=version,
        version_date=version_date,
        total_items=len(rows),
        total_starting_acr=round(
            sum(float(r.get('starting_acr') or 0.0) for r in rows), 2),
        total_converted_acr=round(
            sum(float(r.get('converted_acr') or 0.0) for r in rows), 2),
    )
    db.session.add(entry)
    return entry


def backfill_version_history(snapshot: U2CSnapshot,
                             territories: list[str] | None = None,
                             weeks: int | None = None) -> int:
    """Store every retained weekly version we don't already have.

    MSXi keeps roughly seven weeks of loads, so calling this the first time we
    see a quarter recovers most of its attainment trend immediately. Versions
    already stored are skipped - they can never change.

    Returns:
        Number of new version rows stored.
    """
    from app.services.u2c_pull import (
        DEFAULT_HISTORY_WEEKS, U2CPullError, pull_version_series,
    )

    known = {
        v.msxi_version for v in
        U2CSnapshotVersion.query.filter_by(snapshot_id=snapshot.id).all()
    }
    try:
        series = pull_version_series(
            snapshot.fiscal_quarter,
            territories=territories,
            weeks=weeks or DEFAULT_HISTORY_WEEKS,
            skip=known,
        )
    except U2CPullError as exc:
        logger.warning("U2C version backfill for %s failed: %s",
                       snapshot.fiscal_quarter, exc)
        return 0

    stored = 0
    for version, rows in series:
        if record_version_totals(snapshot, version, rows) is not None:
            stored += 1
    if stored:
        db.session.commit()
    logger.info("Backfilled %d weekly U2C versions for %s",
                stored, snapshot.fiscal_quarter)
    return stored


# ---------------------------------------------------------------------------
# Automated daily refresh
# ---------------------------------------------------------------------------
# Outcomes reported by refresh_official_snapshot(). These are the four things
# MSXi's behaviour can mean, and they must stay distinguishable: MSXi answers
# "quarter hasn't rolled over", "gap week", and "the model changed under us"
# all with the same empty result set.
OUTCOME_IMPORTED = 'imported'            # new data stored
OUTCOME_UNCHANGED = 'unchanged'          # same weekly load we already had
OUTCOME_NOT_ROLLED_OVER = 'not_rolled_over'  # still publishing the prior quarter
OUTCOME_BROKEN = 'broken'                # auth, territories, or schema problem


def refresh_official_snapshot(territories: list[str] | None = None) -> dict:
    """Refresh the official U2C baseline. The daily automation entry point.

    Pulls the current fiscal quarter from MSXi. Because MSXi only ever serves
    whichever quarter is current *to it*, an empty result is meaningful rather
    than an error, and rollover needs no calendar arithmetic: the outgoing
    quarter is finished exactly when it stops returning rows, which is the same
    moment the new one starts returning them.

    Args:
        territories: Territory codes to scope to. Defaults to the configured ones.

    Returns:
        Dict with 'success', 'outcome' (one of the ``OUTCOME_*`` constants),
        and outcome-specific detail.
    """
    from app.services.u2c_pull import (
        CURRENT_VERSION, U2CPullError, fingerprint_rows, pull_u2c_milestones,
    )

    fq = current_fiscal_quarter()
    try:
        rows = pull_u2c_milestones(fq, territories=territories)
    except U2CPullError as exc:
        logger.warning("U2C refresh for %s failed: %s", fq, exc)
        return {'success': False, 'outcome': OUTCOME_BROKEN,
                'fiscal_quarter': fq, 'error': str(exc)}

    if not rows:
        return _refresh_previous_quarter(fq, territories)

    existing = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
    is_new_quarter = existing is None

    if existing and existing.content_fingerprint == fingerprint_rows(rows):
        # Same weekly load. Note that we looked, but don't churn the items.
        existing.last_refreshed_at = datetime.now(timezone.utc)
        db.session.commit()
        return {
            'success': True,
            'outcome': OUTCOME_UNCHANGED,
            'fiscal_quarter': fq,
            'snapshot_id': existing.id,
            'msxi_version': existing.msxi_version,
            'total_items': existing.total_items,
        }

    result = import_official_snapshot(
        fq, territories=territories, replace=True, version=CURRENT_VERSION,
    )
    if not result.get('success'):
        result['outcome'] = OUTCOME_BROKEN
        return result

    result['outcome'] = OUTCOME_IMPORTED
    snapshot = U2CSnapshot.query.get(result['snapshot_id'])

    if snapshot:
        # Backfill on every import, not just the first sight of a quarter. A
        # version can come back empty transiently (observed: 20260901 returned
        # nothing on one probe and full data an hour later), and since we only
        # skip versions already stored, re-probing lets those gaps heal. Imports
        # only happen when the content actually changed - roughly weekly - so
        # this costs a handful of queries per week, not per day.
        result['versions_backfilled'] = backfill_version_history(
            snapshot, territories=territories)

    if is_new_quarter and snapshot:
        # First sight of this quarter means the previous one has rolled over.
        result['closed_out'] = close_out_quarter(
            previous_fiscal_quarter(fq), territories=territories)

    return result


def _refresh_previous_quarter(fq: str, territories: list[str] | None) -> dict:
    """Handle an empty current-quarter pull.

    Either MSXi hasn't rolled over yet - in which case the previous quarter is
    still live and worth refreshing, which keeps its eventual final numbers
    accurate - or something is broken and we must not touch stored data.
    """
    from app.services.u2c_pull import U2CPullError, pull_u2c_milestones

    prev_fq = previous_fiscal_quarter(fq)
    try:
        prev_rows = pull_u2c_milestones(prev_fq, territories=territories)
    except U2CPullError as exc:
        logger.warning("U2C fallback pull for %s failed: %s", prev_fq, exc)
        return {'success': False, 'outcome': OUTCOME_BROKEN,
                'fiscal_quarter': fq, 'error': str(exc)}

    if not prev_rows:
        # Neither quarter has data. MSXi does not distinguish "no rows" from
        # "you asked for something that no longer exists", so this is as close
        # as we get to an error signal - surface it instead of failing quietly.
        logger.error(
            "U2C refresh found no data for %s or %s - check az login, VPN, "
            "configured territories, and whether the MSXi model changed.",
            fq, prev_fq,
        )
        return {
            'success': False,
            'outcome': OUTCOME_BROKEN,
            'fiscal_quarter': fq,
            'error': (
                f'MSXi returned no U2C data for {fq} or {prev_fq}. Check that '
                "you're signed in and on VPN, that your territories are "
                'configured, and that the MSXi report still works in the portal.'
            ),
        }

    logger.info("MSXi hasn't rolled over to %s yet - refreshing %s instead",
                fq, prev_fq)
    result = import_official_snapshot(prev_fq, territories=territories,
                                      replace=True)
    result['outcome'] = (OUTCOME_NOT_ROLLED_OVER if result.get('success')
                         else OUTCOME_BROKEN)
    result['awaiting_quarter'] = fq
    return result


def rematch_snapshot_items(snapshot: U2CSnapshot) -> int:
    """Re-link a snapshot's items to local milestones without re-pulling MSXi.

    The MSXi report covers every milestone in the territory, including ones on
    accounts we haven't synced yet, so items can start out unmatched. A later
    milestone sync fills those gaps - but MSXi only publishes weekly, so waiting
    for the next import would leave attainment falling back to MSXi's view for
    up to a week. This is a free local pass that closes that window.

    Args:
        snapshot: Snapshot whose items should be re-resolved.

    Returns:
        Number of items that gained a local milestone link.
    """
    newly_matched = 0
    items = U2CSnapshotItem.query.filter_by(snapshot_id=snapshot.id).all()
    for item in items:
        if item.milestone_id:
            continue
        milestone, customer, opportunity = _resolve_local_records({
            'milestone_number': item.milestone_number,
            'opportunity_number': item.opportunity_number,
            'customer_name': item.customer_name,
        })
        if milestone:
            item.milestone_id = milestone.id
            item.workload = milestone.workload
            newly_matched += 1
        if customer and not item.customer_id:
            item.customer_id = customer.id
        if opportunity and not item.opportunity_name:
            item.opportunity_name = opportunity.name

    if newly_matched:
        db.session.commit()
        logger.info("Re-matched %d previously unmatched U2C items for %s",
                    newly_matched, snapshot.fiscal_quarter)
    return newly_matched


def rematch_current_snapshot() -> int:
    """Re-link the current quarter's snapshot items. No-op if none exists."""
    snapshot = U2CSnapshot.query.filter_by(
        fiscal_quarter=current_fiscal_quarter()).first()
    if snapshot is None:
        return 0
    return rematch_snapshot_items(snapshot)


def close_out_quarter(fq_label: str,
                      territories: list[str] | None = None) -> dict:
    """Capture a rolled-over quarter's final numbers, then freeze it.

    MSXi stops serving a quarter once the next one becomes current, but retained
    weekly versions from *before* the rollover still hold that quarter's data
    for roughly seven weeks. Walking those versions newest-first finds the last
    load published while the quarter was live - its true final state - even if
    the machine was switched off when the rollover happened.

    Best-effort by design: if no retained version still resolves, whatever the
    daily refresh last captured stands, which is at most a day stale.

    Args:
        fq_label: The quarter that just ended, e.g. 'FY26 Q4'.
        territories: Territory codes, defaulting to the configured ones.

    Returns:
        Dict describing what happened. Never raises.
    """
    from app.services.u2c_pull import (
        DEFAULT_HISTORY_WEEKS, U2CPullError, candidate_versions,
        pull_u2c_milestones,
    )

    snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq_label).first()
    if snapshot is None:
        return {'success': False, 'reason': 'no_snapshot', 'fiscal_quarter': fq_label}
    if snapshot.is_final:
        return {'success': True, 'reason': 'already_final', 'fiscal_quarter': fq_label}

    for version in candidate_versions(DEFAULT_HISTORY_WEEKS):
        try:
            rows = pull_u2c_milestones(fq_label, territories=territories,
                                       version=version)
        except U2CPullError as exc:
            logger.warning("Close-out probe %s for %s failed: %s",
                           version, fq_label, exc)
            continue
        if not rows:
            continue

        matched, total_acr = _write_snapshot_items(snapshot, rows)
        _stamp_snapshot_pull(snapshot, rows, version)
        record_version_totals(snapshot, version, rows)
        snapshot.is_final = True
        db.session.commit()
        logger.info(
            "Closed out %s from MSXi version %s: %d milestones, $%.2f ACR",
            fq_label, version, len(rows), total_acr,
        )
        return {
            'success': True,
            'fiscal_quarter': fq_label,
            'msxi_version': version,
            'total_items': len(rows),
            'matched_locally': matched,
            'finalised': True,
        }

    # Nothing retained still resolves. Freeze what we already have.
    snapshot.is_final = True
    db.session.commit()
    logger.info(
        "No retained MSXi version still serves %s - freezing the last captured "
        "snapshot (version %s) as final.",
        fq_label, snapshot.msxi_version or 'unknown',
    )
    return {
        'success': True,
        'fiscal_quarter': fq_label,
        'msxi_version': snapshot.msxi_version,
        'finalised': True,
        'reason': 'no_retained_version',
    }


def get_attainment(snapshot_id: int, workload_prefix: str | None = None) -> dict:
    """Calculate U2C attainment for a snapshot.

    Checks the current status of each snapshotted milestone to see if it
    has moved to Committed or Completed since the snapshot was taken.

    Args:
        snapshot_id: ID of the U2CSnapshot to analyze.
        workload_prefix: Optional workload prefix filter (e.g. 'Data:' to
                         filter to Data milestones only).

    Returns:
        Dict with attainment stats and item details.
    """
    snapshot = U2CSnapshot.query.get(snapshot_id)
    if not snapshot:
        return {'success': False, 'error': 'Snapshot not found'}

    items_query = U2CSnapshotItem.query.filter_by(snapshot_id=snapshot_id)
    if workload_prefix:
        items_query = items_query.filter(
            U2CSnapshotItem.workload.like(f'{workload_prefix}%')
        )
    items = items_query.all()

    # Also filter by due date within the snapshot's fiscal quarter
    q_start, q_end = fiscal_quarter_date_range(snapshot.fiscal_quarter)
    from datetime import time as _time
    q_start_dt = datetime.combine(q_start, _time.min)
    q_end_dt = datetime.combine(q_end, _time(23, 59, 59))

    target_total = 0.0
    committed_total = 0.0
    attainment_total = 0.0
    committed_items = []
    remaining_items = []

    for item in items:
        # Only include milestones due within the snapshot's fiscal quarter
        if not item.due_date or not (q_start_dt <= item.due_date <= q_end_dt):
            continue

        target_total += item.monthly_acr

        # Check current live milestone status
        live_ms = Milestone.query.get(item.milestone_id) if item.milestone_id else None
        is_committed = False
        current_status = item.msx_status  # fallback to snapshot status
        current_commitment = 'Uncommitted'
        live_acr = item.monthly_acr  # fallback to snapshot ACR
        source = 'snapshot'

        if live_ms:
            source = 'live'
            current_status = live_ms.msx_status
            current_commitment = live_ms.customer_commitment or 'Uncommitted'
            live_acr = live_ms.monthly_usage or 0.0
            is_committed = (
                current_commitment == 'Committed'
                or current_status in ('Completed',)
            )
        elif item.msxi_commitment or item.msxi_status:
            # Official import with no local milestone (an account we don't sync):
            # fall back to MSXi's own view of where the milestone landed.
            source = 'msxi'
            current_status = item.msxi_status or item.msx_status
            current_commitment = item.msxi_commitment or 'Uncommitted'
            is_committed = (
                current_commitment == 'Committed'
                or current_status in ('Completed',)
            )
            if is_committed and item.msxi_converted_acr:
                live_acr = item.msxi_converted_acr

        # Look up seller via customer relationship
        seller_name = None
        seller_id = None
        if item.customer and item.customer.seller:
            seller_name = item.customer.seller.name
            seller_id = item.customer.seller_id

        item_data = {
            'id': item.id,
            'milestone_id': item.milestone_id,
            'customer_name': item.customer_name,
            'customer_id': item.customer_id,
            'seller_name': seller_name,
            'seller_id': seller_id,
            'milestone_title': item.milestone_title,
            'milestone_number': item.milestone_number,
            'workload': item.workload,
            'due_date': item.due_date.isoformat() if item.due_date else None,
            'monthly_acr': item.monthly_acr,
            'live_acr': live_acr,
            'opportunity_name': item.opportunity_name,
            'owner_alias': item.owner_alias,
            'snapshot_status': item.msx_status,
            'current_status': current_status,
            'current_commitment': current_commitment,
            'status_source': source,
            'is_committed': is_committed,
        }

        if is_committed:
            committed_total += item.monthly_acr   # snapshot ACR for U2C%
            attainment_total += live_acr           # live ACR for Attainment%
            committed_items.append(item_data)
        else:
            remaining_items.append(item_data)

    # Sort remaining by monthly ACR descending (highest value first)
    remaining_items.sort(key=lambda x: x['monthly_acr'], reverse=True)

    u2c_pct = round((committed_total / target_total * 100), 1) if target_total > 0 else 0.0
    attainment_pct = round(
        (attainment_total / target_total * 100), 1
    ) if target_total > 0 else 0.0

    return {
        'success': True,
        'fiscal_quarter': snapshot.fiscal_quarter,
        'snapshot_date': snapshot.snapshot_date.isoformat(),
        'source': snapshot.source,
        'source_label': snapshot.source_label,
        'target_total': round(target_total, 2),
        'committed_total': round(committed_total, 2),
        'attainment_total': round(attainment_total, 2),
        'u2c_pct': u2c_pct,
        'attainment_pct': attainment_pct,
        'committed_count': len(committed_items),
        'remaining_count': len(remaining_items),
        'total_in_scope': len(committed_items) + len(remaining_items),
        'committed_items': committed_items,
        'remaining_items': remaining_items,
    }


def get_workload_prefixes(snapshot_id: int) -> list[str]:
    """Return distinct workload prefixes for a snapshot's items.

    Extracts the part before the first colon (e.g. 'Data' from 'Data: Fabric').
    """
    items = U2CSnapshotItem.query.filter_by(snapshot_id=snapshot_id).all()
    prefixes = set()
    for item in items:
        if item.workload and ':' in item.workload:
            prefixes.add(item.workload.split(':')[0].strip())
    return sorted(prefixes)
