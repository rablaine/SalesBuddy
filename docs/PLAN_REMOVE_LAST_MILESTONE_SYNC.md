# Plan: Remove `UserPreference.last_milestone_sync`

**Status:** Draft / not yet implemented
**Last updated:** 2026-09-21
**Branch:** do this on its own feature branch (suggested: `refactor/drop-last-milestone-sync`)

## Goal

Delete every read and write of `UserPreference.last_milestone_sync` and make
`SyncStatus('milestones')` the single source of truth for "when did the milestone
sync last run".

## Why

`last_milestone_sync` was historically written by **only** the scheduler
([scheduled_sync.py](../app/services/scheduled_sync.py)). Manual syncs - the SSE
sync users actually click from the milestone tracker and the U2C report - never
touched it. Anything reading the field therefore saw a timestamp that was
routinely days stale even seconds after a successful sync.

That caused a real user-facing bug: the U2C Attainment report's "stale milestone
import" banner read the pref field, so a manual sync could never clear it. Only a
scheduled Mon/Wed/Fri sync could. See the fix on
`feature/u2c-official-snapshot-import`, which pointed the U2C route at
`SyncStatus` and added `_stamp_last_milestone_sync()` as a stopgap so manual syncs
keep the legacy field current.

This plan removes the stopgap and the field entirely rather than maintaining two
timestamps that can disagree.

There is already precedent for the target pattern in the same file:
`_revenue_sync_due()` is keyed off `SyncStatus('revenue_sync')` specifically so
"a machine that was off on the chosen day still catches up on its next run".

## Background: three competing "last sync" values

The app currently answers "when did milestones last sync?" three different ways.
This plan collapses #2 into #1 and leaves #3 alone.

1. **`SyncStatus('milestones')`** - written by both sync paths (batch and
   streaming). Tracks `started_at` / `completed_at` / `heartbeat_at` / `success` /
   `items_synced` / `details`. Authoritative. Already surfaced in the admin panel
   via `/api/admin/sync-status/<type>`.
2. **`UserPreference.last_milestone_sync`** - the field being removed.
3. **`max(Milestone.last_synced_at)`** -
   [milestone_sync.py](../app/services/milestone_sync.py) computes this for the
   milestone tracker header. It is a *different* question ("when were rows last
   touched"), it is legitimately useful, and it stays.

## Inventory

Every reference in the repo as of 2026-09-21.

### Schema

| Location | What |
|---|---|
| [models.py:1563](../app/models.py) | Column def on `UserPreference` |
| [migrations.py:274](../app/migrations.py) | `_add_column_if_not_exists(..., 'last_milestone_sync', 'DATETIME')` |

### Writes

| Location | What |
|---|---|
| [scheduled_sync.py:154-158](../app/services/scheduled_sync.py) | `_run_sync()` stamps after a scheduled sync, deliberately "regardless of success (avoid retry storms)" |
| [milestone_sync.py](../app/services/milestone_sync.py) | `_stamp_last_milestone_sync()` helper (added 2026-09-21 as a stopgap) |
| [milestone_sync.py](../app/services/milestone_sync.py) | Two call sites: end of `sync_all_customer_milestones()` and end of `sync_all_customer_milestones_stream()` |

### Reads

| Location | What |
|---|---|
| [scheduled_sync.py:84-87](../app/services/scheduled_sync.py) | `_missed_sync()` - startup catchup decision |
| [scheduled_sync.py:119-123](../app/services/scheduled_sync.py) | `_should_sync()` - scheduled tick decision |
| [admin.py:1318-1319](../app/routes/admin.py) | `/api/admin/tasks/milestone-sync/status` returns `last_sync`; rendered by `loadMilestoneSyncStatus()` in [admin_panel.html](../templates/admin_panel.html) as the `(last: ...)` label |
| [reports.py](../app/routes/reports.py) | `report_u2c()` display-only fallback for `last_milestone_sync` (added 2026-09-21 alongside the stopgap) |

### Tests

| Location | What |
|---|---|
| [test_workiq_integration.py](../tests/test_workiq_integration.py) | 5 references setting up `_should_sync` / `_missed_sync` scenarios |
| [test_u2c.py](../tests/test_u2c.py) | `test_manual_sync_stamps_legacy_last_sync` - delete outright, it only exists to test the stopgap |

## Approach

Add one helper and point all four readers at it.

```python
# app/services/scheduled_sync.py (or a shared place if reports.py wants it too)
def _last_milestone_sync_utc() -> datetime | None:
    """Last time the milestone sync finished, successful or not.

    Returns an aware UTC datetime, or None if it has never finished.
    """
    status = SyncStatus.get_status('milestones')
    completed = status.get('completed_at')
    if completed is None:
        return None
    return completed if completed.tzinfo else completed.replace(tzinfo=timezone.utc)
```

Then:

1. `_missed_sync()` and `_should_sync()` take no `pref` timestamp - they call the
   helper. Keep reading `milestone_sync_hour` / `milestone_sync_minute` /
   `milestone_auto_sync` from `pref`; those are real user settings and stay.
2. `admin.py` returns `_last_milestone_sync_utc()` (isoformat) instead of the pref
   field. The admin panel JS appends `'Z'` before parsing, so keep emitting a
   naive-looking UTC isoformat or update the JS - pick one and be consistent.
3. `report_u2c()` drops the `UserPreference` import and the `display_last_sync`
   fallback; `last_sync` from `SyncStatus` is all it needs. The
   "freshness requires success" rule does **not** change - only the display
   fallback goes away.
4. Delete `_stamp_last_milestone_sync()` and both call sites.
5. Delete the pref write in `_run_sync()`.
6. Leave the DB column in place. Per the repo's migration rules we do not
   `DROP COLUMN`. Add a comment on [models.py:1563](../app/models.py) marking it
   deprecated/unused, or delete the attribute from the model and leave the column
   orphaned in SQLite - decide before starting (see Open questions).

## Behavior changes to expect

1. **Retry-storm protection is preserved.** `SyncStatus.mark_completed()` sets
   `completed_at` even when `success=False` (state becomes `'failed'`), so reading
   `completed_at` regardless of state is a 1:1 match for today's "stamp regardless
   of success".

2. **A *crashed* sync now behaves differently, and better.**
   `SyncStatus.mark_started()` nulls `completed_at`, so a sync killed mid-run
   (process death, machine sleep, VPN drop that kills the thread) leaves no
   completion. The scheduler will treat that as "missed" and retry on the next
   tick or at startup. Today it would *not* retry, because the pref field was
   stamped by a previous run and nothing knows the latest attempt died. This is
   the intended improvement, but it is a change - call it out in the changelog.

3. **Manual syncs now count.** After this change, a hand-run sync suppresses the
   next scheduled run for that day, because the scheduler finally sees it. Today
   a user who syncs manually at 9am still eats a full scheduled sync at the
   configured time. This is the whole point, but it means the MWF sync will
   *appear* to "skip" more often. Make sure the admin panel wording doesn't read
   as a failure.

## Verification

- `pytest tests/test_workiq_integration.py tests/test_u2c.py` - the scheduler
  tests need rewriting to seed `SyncStatus` instead of the pref field.
- Add a regression test: a successful manual sync (via `SyncStatus.mark_started` +
  `mark_completed`) makes `_should_sync()` return `False` for the rest of the day.
- Add a regression test: an interrupted sync (`mark_started` with no
  `mark_completed`) makes `_missed_sync()` return `True`.
- Manual: run a sync from the milestone tracker, then load Admin > the milestone
  sync card and confirm the `(last: ...)` label updates immediately.
- Manual: load the U2C report and confirm the stale banner reflects the sync you
  just ran.
- Final grep for `last_milestone_sync` - the only surviving hit should be the
  (commented) column definition and the migration that created it.

## Open questions

1. **Keep or drop the model attribute?** Leaving the column but deleting the
   Python attribute is cleanest for code hygiene but means the migration helper
   still creates a column nothing maps. Leaving the attribute with a
   `# DEPRECATED - unused, see PLAN_REMOVE_LAST_MILESTONE_SYNC.md` comment is more
   discoverable. Recommendation: keep the attribute, comment it, and revisit
   during a future schema cleanup pass.
2. **Should `_should_sync` ignore *failed* syncs?** Today's behavior (and the
   plan's) is that a failed sync still counts as "ran", to avoid retry storms when
   MSX is unreachable. An alternative is a short retry window (e.g. retry a failed
   sync once after 1 hour). Out of scope here; note it and move on.
3. **Naming.** If `report_u2c()` also wants the helper, it should live somewhere
   neutral rather than in `scheduled_sync`. A `@classmethod` on `SyncStatus`
   (`SyncStatus.last_completed_utc('milestones')`) is probably the right home and
   would be reusable for `revenue_sync`, `marketing`, and the future U2C import.

## Out of scope

- The milestone tracker's `max(Milestone.last_synced_at)` header value.
- Any change to the MWF schedule itself or to `milestone_auto_sync` /
  `milestone_sync_hour` / `milestone_sync_minute`.
- The U2C official-import automation (tracked separately).
