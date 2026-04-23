# Ghost Aura Sync Spec

**Status:** In progress
**Branch:** `feature/prefetch-meetings-backlog`
**Supersedes:** Per-day "sync today" path and `sync_meetings_for_week`

## Problem

Today's morning sync only fetches today's meetings. Future ghosts only appear
once the user manually triggers a week sync (which has no UI button anyway).
Result: users don't see Wednesday's meetings until Wednesday morning, can't
proactively dismiss them, can't proactively note them.

We also have no UI feedback when a sync is running. If the morning aura is
mid-flight, a manual refresh just sits there silently waiting on the lock.

## Goals

- Single morning code path: "ensure aura" starting at today, covering today
  through today + 4 business days = 5 weekdays total. Saturdays and Sundays
  are walked over (not counted).
- Each aura day uses the same purge+upsert as today's sync, so dismissed
  ghosts and noted ghosts survive aura re-runs.
- The full aura runs under a single `_sync_lock` (no per-day release), to
  keep things simple and avoid duplicate WorkIQ calls.
- UI surfaces "sync in progress" only when a page loads OR when a user
  action is blocked by the lock. **No background polling.**
- Today-cell health indicator: blue when today is freshly synced, orange
  when something failed or is stale. Clicking orange triggers a refresh.

## Non-Goals

- Skipping aura days that already have data. We **always** re-sync every
  aura day. Pre-sync purge + upsert handles correctness.
- Real-time progress polling. We snapshot sync state on page load and on
  blocked actions only.
- Backfilling the past aura. `expires_at` (7 days post-meeting) handles it.

## Data Model

**No schema changes.** Reuses:
- `PrefetchedMeeting.dismissed`, `PrefetchedMeeting.note_id` for preservation
- `DailyMeetingCache.synced_at` for today-cell health check
- `DismissedRecurringMeeting` for series dismissals

## Implementation

### 1. Aura sync function (`app/services/meeting_sync.py`)

```python
def ensure_meeting_aura(
    start: date | None = None,
    days_ahead: int = 4,
) -> tuple[dict[str, int], dict[str, str]]:
    """Sync today + the next N business days as a single locked operation."""
```

- `start` defaults to `date.today()`.
- ``days_ahead`` counts BUSINESS DAYS, not calendar days. Default 4 means
  today + 4 weekdays = 5 weekdays total.
- Skips Saturday and Sunday (`d.weekday() < 5`).
- Holds `_sync_lock` for the full duration. If the lock is already held,
  returns `({}, {'_status': 'already running'})` immediately, does not block.
- For each weekday, calls `sync_meetings_for_date(day_str)` inside try/except
  so one day's failure does not abort the rest.
- Tracks state in module-level `_sync_state` dict (running flag, current_date,
  started_at) under a small `_sync_state_lock` so the status endpoint can
  snapshot it without contending with the big lock.
- Returns `(counts_by_date, errors_by_date)`.

### 2. Module-level sync state

```python
_sync_state: dict[str, Any] = {
    'running': False,
    'current_date': None,
    'started_at': None,  # ISO datetime
    'aura_dates': [],    # list of YYYY-MM-DD that this aura will cover
}
_sync_state_lock = threading.Lock()


def get_sync_state_snapshot() -> dict[str, Any]:
    """Return a copy of the current sync state. Cheap, never blocks aura."""
```

- Set to `running=True` at aura start, with `current_date` updated as the
  loop progresses.
- Cleared (`running=False`, all fields reset) in the aura's `finally` block.

### 3. Wire into existing scheduler

- `_run_sync(app)`: replace `sync_meetings_for_date(today_str)` with
  `ensure_meeting_aura()`.
- `start_meeting_sync_background(app)` startup catchup: same swap.

### 4. Retire week sync

- Delete `sync_meetings_for_week` from `meeting_sync.py`.
- Delete `POST /api/meetings/sync-week` route from `notes.py`.
- Remove any imports.

### 5. New API routes (`app/routes/notes.py`)

#### `GET /api/meetings/sync-status`
Returns `get_sync_state_snapshot()`. Always cheap, never blocks.

#### `POST /api/meetings/sync-aura`
Manually trigger an aura sync. Fires `ensure_meeting_aura()` in a background
thread (so the HTTP request returns immediately). Returns the initial state
snapshot. If a sync is already running, returns `{started: false, reason: 'already running', state: <snapshot>}`.

#### `GET /api/meetings/today-status`
Returns `{synced: bool, synced_at: iso|null, today: 'YYYY-MM-DD'}`.
- `synced=True` iff a `DailyMeetingCache` row exists for today AND its
  `synced_at` is later than today's local midnight.
- Used by the calendar to color today's cell blue (synced) or orange (not).

### 6. Frontend (`templates/index.html` + `static/`)

#### 6a. Sync-in-progress modal (loaded on initial page load)

- On `DOMContentLoaded`, fetch `/api/meetings/sync-status` once.
- If `running=true`, show a non-dismissible Bootstrap modal:
  > "Refreshing meetings ({current_date})..."
  > with a spinner and "This may take a minute or two."
- The modal polls **only itself** every 3s while open until `running=false`,
  then closes itself and reloads the calendar API. (Polling is scoped to the
  modal lifetime, not background-on-page-load.)

#### 6b. Sync-blocked feedback for user actions

When the user clicks the orange today-cell or any future "sync now" button:
- POST to the action's endpoint.
- If response is `{started: false, reason: 'already running'}`, show the
  same modal as 6a, attached to the in-flight sync. When complete, refresh.

#### 6c. Today cell coloring

- After calendar load, fetch `/api/meetings/today-status` once.
- Apply `cal-today-incomplete` class to the today cell when `synced=false`:
  - Background: `bg-warning` (orange) instead of `bg-primary` (blue).
  - Tooltip: "Today's sync is incomplete - click to refresh".
  - On hover, replace the day number with `<i class="bi bi-arrow-clockwise">`.
  - On click, POST `/api/meetings/sync-aura` with body `{anchor: today, days_ahead: 0}` so it only refreshes today, then trigger the modal flow from 6b.
- When `synced=true`:
  - Existing blue styling.
  - Tooltip: "Today's meetings are up to date".

### 7. Tests

#### `tests/test_meeting_aura.py` (NEW)

1. `test_aura_skips_weekends` - anchor on a Friday, days_ahead=4. Expect
   syncs for Fri, Mon, Tue (3 days), no Sat/Sun calls.
2. `test_aura_one_day_failure_does_not_abort_rest` - stub WorkIQ to raise
   on day 2. Verify days 1, 3, 4 still synced.
3. `test_aura_re_runs_populated_days` - pre-seed ghosts for tomorrow.
   Re-run aura. Verify ghosts purged + reinserted (different IDs), but
   dismissed/noted rows stay (same IDs).
4. `test_sync_status_reflects_running_state` - start an aura in a thread,
   block it mid-loop with an event. Assert snapshot shows
   `running=true, current_date=<expected>`. Release event, assert cleared.
5. `test_aura_lock_already_held_returns_status_marker` - hold `_sync_lock`,
   call `ensure_meeting_aura`, expect immediate return with status marker.
6. `test_today_status_synced_true_when_cache_is_today` - seed cache with
   `synced_at=now`, expect `synced=True`.
7. `test_today_status_synced_false_when_cache_is_yesterday` - seed cache
   with `synced_at=yesterday`, expect `synced=False`.
8. `test_today_status_synced_false_when_no_cache` - no cache, expect False.

#### Updates

- Delete tests for `sync_meetings_for_week` if any.

## Acceptance Criteria

- [ ] `ensure_meeting_aura()` exists, holds full lock, skips weekends, handles per-day errors
- [ ] `_sync_state` exposes running/current_date/started_at; `get_sync_state_snapshot()` works without blocking
- [ ] `_run_sync` and startup catchup call `ensure_meeting_aura()`
- [ ] `sync_meetings_for_week` and `/api/meetings/sync-week` removed; no leftover refs
- [ ] `GET /api/meetings/sync-status` returns snapshot
- [ ] `POST /api/meetings/sync-aura` fires background thread, returns initial state
- [ ] `GET /api/meetings/today-status` returns correct freshness
- [ ] On page load, modal appears when sync is running
- [ ] On blocked action, same modal appears
- [ ] Today cell turns orange when sync incomplete, blue when complete
- [ ] All 8 new tests pass
- [ ] Existing ghost / sync / prefetch tests still pass
