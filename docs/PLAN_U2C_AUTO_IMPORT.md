# Plan: Automated U2C Official Import + Attainment History

**Status:** Draft / not yet implemented
**Last updated:** 2026-09-21
**Branch:** suggested `feature/u2c-auto-import` (after `feature/u2c-official-snapshot-import` merges)

## Goal

Make the U2C Attainment report maintain itself:

1. Refresh the official MSXi baseline **daily**, with no calendar triggers.
2. Handle quarter rollover automatically, including a proper **close-out** of the
   outgoing quarter.
3. Keep a **permanent** weekly attainment history per quarter so the report can
   draw a trend line that does not roll off when MSXi expires old versions.
4. Delete locally-built snapshots entirely. Official-only from here.

## Measured facts this plan is built on

All verified 2026-09-21 by direct read-only queries against MSXi. Probe scripts
live in the session folder (`probe_u2c_*.py`) and are worth keeping around for
the October verification below.

| Fact | Evidence |
|---|---|
| MSXi serves **only the current quarter** | `FY26 Q4` and `FY26 Q3` return 0 rows under every filter combination tried, including stripping `QtrRel`, `FYRel` and `FiscalYear` |
| `SnapshotDateID` (the report's "Snapshot Version" slicer) accepts explicit `yyyymmdd` members | `20260915`, `20260908`, `20260825`, `20260818`, `20260811`, `20260804` all return 42 rows |
| `'current'` is an **alias for the newest retained version** | `current` is byte-identical to `20260915` |
| Loads are **weekly, on Tuesdays** | Retained members are all Tuesdays |
| Retention is **~7 weeks** | Floor was `20260804`; `20260728` and older return 0 |
| **Gaps exist** in the version series | `20260721` is explicitly excluded by the report as a bad load. Separately, `20260901` returned 0 rows on one probe and full data an hour later - **empty results can be transient**, so gaps must be re-probed rather than recorded as permanent |
| The baseline is **immutable** across versions | `starting_acr` = 130,878 in every version |
| Attainment is **non-monotonic** | `converted_acr` ran 6,000 → 4,000 → 4,600 → 6,978 → 29,078; conversions get restated downward |
| The `Where` filters are **load-bearing on the measures** | Dropping `QtrRel` kept 42 rows but moved `starting_acr` from 130,878 to 878,679 |
| A bad/unknown column name returns **0 rows, not an error** | 7 of 8 probed column names silently returned 0 |

Two consequences drive the whole design:

- **Historical quarters can only ever come from our own stored data.** MSXi will
  not serve them again. But for ~7 weeks after rollover we *can* still reach the
  outgoing quarter through an explicit pre-rollover version, so close-out does
  not depend on catching the last day live.
- **"0 rows" is dangerously ambiguous** - it means "not rolled over yet" OR
  "gap week" OR "MSXi changed the schema under us." Every zero-row path needs an
  explicit branch; none may silently `return`.

## Schema

### New table: `u2c_snapshot_versions`

The permanent attainment time series. One row per (quarter, MSXi weekly version).
Rows are written once and never updated - a dated MSXi version is immutable - so
this table outlives MSXi's 7-week retention and is what the trend graph reads.

```python
class U2CSnapshotVersion(db.Model):
    __tablename__ = 'u2c_snapshot_versions'

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('u2c_snapshots.id'),
                            nullable=False)
    msxi_version = db.Column(db.String(20), nullable=False)   # '20260915'
    version_date = db.Column(db.Date, nullable=False)         # parsed, for sorting
    total_items = db.Column(db.Integer, nullable=False, default=0)
    total_starting_acr = db.Column(db.Float, nullable=False, default=0.0)
    total_converted_acr = db.Column(db.Float, nullable=False, default=0.0)
    captured_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        db.UniqueConstraint('snapshot_id', 'msxi_version',
                            name='uq_u2c_version_per_snapshot'),
    )
```

Aggregates only - no per-item history. The graph needs totals, and per-item
history would multiply row count by ~7 for no current use case.

### `u2c_snapshots` additions

| Column | Purpose |
|---|---|
| `msxi_version` `String(20)` | Which MSXi version the current item rows came from |
| `last_refreshed_at` `DateTime` | When we last successfully pulled |
| `content_fingerprint` `String(64)` | Hash of the row payload, for free change detection |
| `is_final` `Boolean` default False | Set at close-out; quarter will never change again |

Migration is additive (`_add_column_if_not_exists` + `db.create_all()` for the new
table). No drops.

## Service changes

### `u2c_pull.py`

- `pull_u2c_milestones(fq_label, territories=None, version='current')` - thread the
  version through to the `SnapshotDateID` filter. Default `'current'` preserves
  today's behavior.
- `resolve_current_version() -> str | None` - figure out which dated member
  `'current'` equals. Probe candidate Tuesdays newest-first and compare
  fingerprints against the `current` payload we already have. Normally resolves on
  the first probe. Must tolerate gaps by continuing backwards.
- `list_available_versions(fq_label, weeks=10) -> list[str]` - probe backwards,
  skipping gaps, returning members that actually return rows.
- Add a prominent comment on `_u2c_query`'s `Where` list: **these filters change
  the measure values, not just the row set - do not "simplify" them.**

### `u2c_snapshot.py`

- `refresh_official_snapshot()` - the daily entry point. See flow below.
- `backfill_version_history(fq)` - for any retained version not already in
  `u2c_snapshot_versions`, pull it and store aggregates. Runs on **every**
  import, not just the first sight of a quarter: empty results can be transient,
  and since stored versions are skipped, re-probing lets gaps heal. Imports only
  happen when content changed (~weekly), so the cost is bounded.
- `close_out_quarter(prev_fq)` - walk versions backwards from the rollover date,
  import the newest one that still returns rows as the prior quarter's final item
  set, mark `is_final = True`.
- **Delete** `create_snapshot()`, `is_snapshot_due()`, `SNAPSHOT_DAY`, and the
  `FQ_START_MONTHS` snapshot trigger.

### Daily flow

```
daily tick (and once at startup, after any due milestone sync):
  if last successful u2c import < ~20h ago: skip

  fq   = current_fiscal_quarter()
  rows = pull(fq, version='current')

  if rows:
      if fingerprint(rows) == snapshot.content_fingerprint:
          outcome = 'unchanged'            # same weekly load, just touch last_checked
      else:
          upsert snapshot items (replace)  # cascade delete-orphan handles items
          resolve + store msxi_version, fingerprint
          record aggregate row in u2c_snapshot_versions
          outcome = 'imported'
      if this is a NEW quarter (no prior snapshot for fq):
          backfill_version_history(fq)
          close_out_quarter(previous_fq)   # best-effort, see October caveat
  else:
      rows = pull(previous_fq, version='current')
      if rows:
          refresh the previous quarter      # overlap window, MSXi hasn't rolled
          outcome = 'not_rolled_over'
      else:
          outcome = 'broken'                # auth / territories / schema change
          surface an error in the UI - do NOT fail silently

  SyncStatus('u2c_import').mark_completed(details={fq, rows, matched,
                                                   msxi_version, outcome})
```

Rollover needs no date math: **the outgoing quarter is done exactly when it stops
returning rows**, which is the same tick the new quarter starts returning them.

### `scheduled_sync.py`

- Replace `_check_u2c_snapshot()` with `_u2c_import_due()` + `_run_u2c_import()`,
  keyed off `SyncStatus('u2c_import')` age - the same pattern as the existing
  `_revenue_sync_due()`, which was written precisely so a machine that was off
  still catches up on its next run.
- Own daily cadence, not chained to the MWF milestone sync (the pull is a single
  Power BI query, ~10-20s). **But** when both are due, run U2C *after* the
  milestone sync - `_resolve_local_records()` matches MSXi rows against local
  milestones, so importing first inflates the unmatched count.
- Startup catchup is the same predicate, not a separate code path.

## Removing local snapshots

Dropping the local path deletes more than it looks like:

- `create_snapshot()`, `is_snapshot_due()`, `SNAPSHOT_DAY`, `FQ_START_MONTHS`
- The "Take Local ... Snapshot" button and its JS in `report_u2c.html`
- **The entire stale-milestone banner**, `milestones_fresh`, the sync threshold,
  and the embedded `syncMilestones()` SSE client - all of it existed only to gate
  the local snapshot button
- The `report_u2c()` read of `UserPreference.last_milestone_sync`, which removes
  one of the four consumers listed in
  [PLAN_REMOVE_LAST_MILESTONE_SYNC.md](PLAN_REMOVE_LAST_MILESTONE_SYNC.md)
- `U2CSnapshot.source` / `SOURCE_LOCAL` / `source_label` and the "Official MSXi"
  badge become vestigial - everything is official. Keep the column as a tombstone,
  strip the UI.

**One-time migration:** delete all `source='local'` snapshots. Past local quarters
are gone for good and cannot be reconstructed - MSXi will not serve them. This is
an accepted, deliberate loss (confirmed by the repo owner 2026-09-21); users have
database backups if they want the old numbers. The changelog must say so plainly.

FY26 Q4 and earlier are unrecoverable regardless of this decision - their MSXi
versions aged out in August. The quarter dropdown starts at FY27 Q1 and builds up.

## Attainment trend graph

Read `u2c_snapshot_versions` for the selected quarter, ordered by `version_date`:
plot `total_converted_acr` against the flat `total_starting_acr` baseline. A
burn-up of the quarter.

- Populated ~6 weeks deep the moment a quarter is first imported, then one point
  per week.
- **Permanent.** Once stored, points never roll off, so by end of quarter the
  graph covers the whole quarter even though MSXi only ever retained 7 weeks.
- The line **can go down** - conversions get restated. Don't force monotonic
  rendering and don't be alarmed by a dip.
- Gap weeks are simply missing points; plot against real dates so a gap reads as a
  gap rather than compressing the x-axis.

## Implementation phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Schema: `u2c_snapshot_versions` + `u2c_snapshots` columns + migrations | Done |
| 2 | `u2c_pull` version support: `version=` param, `resolve_current_version()`, `list_available_versions()` | Done |
| 3 | `refresh_official_snapshot()` + `backfill_version_history()` + `SyncStatus('u2c_import')` | Done |
| 4 | Scheduler: daily `_u2c_import_due()`, startup catchup, remove `_check_u2c_snapshot()` | Done |
| 5 | Remove local snapshots + stale banner + one-time migration + changelog | Done |
| 6 | Attainment trend graph on the report | Done |
| 7 | October: verify rollover, then finish `close_out_quarter()` | Pending Oct |

Phases 1-6 are independent of the October unknown. Phase 7 is the only piece that
must wait.

Notes from implementation:

- `resolve_current_version()` matched on the first probe against live MSXi
  (`current` = `20260915`), and the backfill recovered six additional weeks
  immediately, so a fresh install gets a usable trend straight away.
- `rematch_snapshot_items()` was added beyond the original plan. The content
  fingerprint only covers MSXi's data, so a milestone synced *after* an import
  would otherwise stay unmatched until MSXi's next weekly publish. The re-match
  is a free local pass run before every refresh.
- A failed import retries within the hour rather than waiting a full day, which
  covers the cold-boot auth case that was originally deferred.
- The trend is sliced by workload, because a territory-wide total isn't what
  anyone is measured on. Workload comes from `DimWorkload` (`d4.Workload`),
  which projects cleanly - 42 rows, measures unchanged. **Do not use
  `d3.Workload`** (`DimMilestone`): it cross-joins to 5,166 rows with every
  workload carrying the full total.
- That also fixed a latent bug. Workload previously came only from the local
  milestone, so unmatched MSXi rows had `NULL` and were silently dropped by the
  `LIKE` in every filtered view, understating filtered totals.
- `U2CSnapshotVersionItem` stores per-milestone detail per version rather than
  workload-level aggregates. Same collection cost - the backfill already pulled
  and discarded those rows - and it leaves the door open to slicing the trend by
  seller or customer later without another schema change.
- `fingerprint_rows()` includes workload. It has to cover every field we
  persist: when workload was added to the projection, the "unchanged"
  short-circuit meant stored items kept their stale local values, so the cards
  filtered on old data while the chart filtered on new and the page contradicted
  itself. Any future projected column must be added to the hash too.

## The October verification

**The one thing that cannot be verified before the FY27 Q2 rollover (~Oct 1):**
whether an old version still resolves once the calendar has moved on. Asking for
`FY27 Q1` with version `20260929` in early October means `PrevDueQuarter = FY27-Q1`
while MSXi's `CQ` is FY27 Q2. Either the version scopes the date context and it
works, or `QtrRel = CQ` fights it and we need `CQ-1` or another date filter - and
since those filters change the measures, any swap must be validated against the
portal numbers, not assumed.

**Procedure:** in early October, re-run `probe_u2c_version_values.py` against
`FY27 Q1` using late-September versions. Ten minutes, settles it.

Until then `close_out_quarter()` is **best-effort**: attempt the backfill, and if
it returns zero rows, fall back to the last snapshot captured live while the
quarter was current. Either way the daily refresh means that fallback is at most
one day stale.

The repo owner plans to be present at the changeover so live output can be
inspected and the design adjusted if MSXi behaves unexpectedly.

## Testing

- Version-aware pull: `version=` reaches the `SnapshotDateID` filter correctly.
- **Empty pull is non-destructive.** `import_official_snapshot()` currently returns
  on `if not rows:` *before* `db.session.delete(existing)`. Automation makes that
  ordering load-bearing - pin it with a test.
- Rollover: current-FQ empty + previous-FQ populated refreshes the previous
  quarter and creates nothing new.
- Broken: both empty produces the `'broken'` outcome and touches no data.
- Fingerprint match short-circuits the write path.
- `u2c_snapshot_versions` unique constraint prevents duplicate weekly points.
- Non-monotonic `converted_acr` is stored as-is, not clamped.
- Per the repo's SalesIQ rule, add a read tool for the version history and update
  `test_salesiq_tools.py::test_tool_coverage`.

## Out of scope

- Per-item weekly history (aggregates only).
- Reconstructing FY26 Q4 or earlier. Not possible.
- Cold-boot auth timing (`az login` / VPN not ready at startup). The age-based
  retry makes it self-healing; revisit only if it proves annoying in practice.
