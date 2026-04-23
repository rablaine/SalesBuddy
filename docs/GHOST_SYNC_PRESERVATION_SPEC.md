# Ghost Sync Preservation Spec

**Status:** In progress
**Branch:** `feature/prefetch-meetings-backlog`
**Author:** Phase 3 follow-up

## Problem

The morning prefetch (and any manual re-sync) currently re-inserts meetings via
upsert keyed on `workiq_id`. This is mostly fine, but two gaps remain:

1. **Stale ghosts hang around.** If a meeting is canceled or moved in Outlook,
   the old `PrefetchedMeeting` row for it lingers until `expires_at` (7 days).
   The user sees a ghost for a meeting that doesn't exist.
2. **No link between a ghost and the note created from it.** Clicking a matched
   ghost opens `/note/new?customer_id=&date=` which creates a fresh `Note` but
   never sets `PrefetchedMeeting.note_id`. So tomorrow's sync sees the same
   meeting as still un-noted and the ghost re-appears.

## Goals

- Pre-sync nuke-and-pave wipes ghosts for the target date BEFORE the WorkIQ
  fetch, so canceled meetings disappear.
- Single-occurrence dismissals (`dismissed = True`) survive re-sync.
- Notes created from ghosts (or back-linked) survive re-sync.
- Series dismissals already survive (separate `DismissedRecurringMeeting`
  table) — no change needed.
- Tests cover all preservation paths.

## Non-Goals

- Fuzzy back-linking notes to meetings created without the ghost click path.
  If user creates a note manually for a meeting they could have ghost-clicked,
  the ghost will still appear and they can dismiss it. (Acceptable.)
- Per-day sync API. The existing single-day and week sync already handle this.

## Data Model

**No schema changes.** `PrefetchedMeeting` already has `dismissed: bool` and
`note_id: int | None`. The fix is purely behavioral.

## Implementation

### 1. Pre-sync purge

In `app/services/meeting_sync.py::sync_meetings_for_date`, BEFORE calling
`prefetch_for_date_full`, delete:

```sql
DELETE FROM prefetched_meeting_attendees
  WHERE meeting_id IN (
    SELECT id FROM prefetched_meetings
    WHERE meeting_date = :date
      AND note_id IS NULL
      AND dismissed = 0
  );
DELETE FROM prefetched_meetings
  WHERE meeting_date = :date
    AND note_id IS NULL
    AND dismissed = 0;
```

Implemented via SQLAlchemy filters using `PrefetchedMeeting` and
`PrefetchedMeetingAttendee`. Commit before the WorkIQ call.

Log the count purged at INFO.

### 2. Ghost → note linkage

#### 2a. Frontend (`templates/index.html`)

`openGhostMeeting(g, dateStr)` currently builds `/note/new?customer_id=X&date=Y`
for matched ghosts. Append `&from_meeting=${g.id}` (always, when `g.id` is
present, even for unmatched — the note POST handler will set the link).

For the unmatched/quick-create modal path, also pass the ghost id along to
the modal so when the user picks a customer there, the resulting note POST
includes `from_meeting`. Lower priority — only do this if the existing modal
already supports passthrough hidden fields. (If complex, leave as a TODO.)

#### 2b. Backend (`app/routes/notes.py`)

In the existing `POST` handler that creates a new `Note`, after `db.session.commit()`
that gives the new note its `id`, check for `from_meeting` in form/query.
If present and parses as int, look up the `PrefetchedMeeting` and set
`meeting.note_id = new_note.id`, then commit.

Wrap in try/except so a bad/stale `from_meeting` value never breaks note
creation. Log a warning if the meeting isn't found.

### 3. Tests (`tests/test_ghost_sync_preservation.py`, NEW)

Use `monkeypatch` on `app.services.meeting_prefetch.query_workiq` to return
a controlled JSON shape. Cover:

1. `test_presync_purge_clears_undismissed_un_noted_ghosts`
   - Seed a ghost with `dismissed=False, note_id=None` for today
   - Stub WorkIQ to return empty list
   - Call `sync_meetings_for_date(today)`
   - Assert the ghost row is gone
2. `test_presync_purge_preserves_dismissed_ghost`
   - Seed ghost with `dismissed=True`
   - Stub WorkIQ to return empty
   - Call sync
   - Assert ghost row still exists, `dismissed` still True
3. `test_presync_purge_preserves_noted_ghost`
   - Seed ghost with `note_id=<real note id>`
   - Stub WorkIQ to return empty
   - Call sync
   - Assert ghost row still exists, `note_id` unchanged
4. `test_resync_preserves_dismissed_flag_when_meeting_returns`
   - Seed ghost with `dismissed=True, workiq_id=X`
   - Stub WorkIQ to return same meeting (subject/start/organizer ⇒ same hash)
   - Call sync
   - Assert row's `dismissed` still True (upsert preserved it)
5. `test_resync_preserves_note_id_when_meeting_returns`
   - Seed ghost with `note_id=N, workiq_id=X`
   - Stub WorkIQ to return same meeting
   - Call sync
   - Assert `note_id` still N
6. `test_note_creation_with_from_meeting_links_ghost`
   - Seed ghost row
   - POST to note creation route with `from_meeting=<ghost_id>` plus required
     fields
   - Assert ghost's `note_id` now equals new note's id
7. `test_note_creation_with_invalid_from_meeting_does_not_break`
   - POST with `from_meeting=999999`
   - Assert note still created, no exception, ghost untouched

### 4. Manual verification after build

Use `read_file` / a one-shot script:

- Sync today, dismiss one ghost, sync again, confirm dismissed survives.
- Click a matched ghost, save the note, sync again, confirm ghost is gone
  (because `note_id` is set and display layer filters it out).
- Cancel a "fake" meeting by stubbing it out and syncing — confirm purged.

(For real validation user will exercise in browser.)

## Acceptance Criteria

- [ ] `sync_meetings_for_date` purges undismissed/un-noted ghosts before fetch
- [ ] Purge log line shows count
- [ ] Dismissed ghosts survive re-sync
- [ ] Noted ghosts survive re-sync
- [ ] `openGhostMeeting` passes `from_meeting=<id>` to note URL
- [ ] Note POST handler reads `from_meeting` and sets `PrefetchedMeeting.note_id`
- [ ] Bad `from_meeting` value does not break note creation
- [ ] All 7 tests pass
- [ ] Existing `tests/test_ghost_meetings.py` still passes (15 tests)
- [ ] Existing `tests/test_meeting_sync.py` still passes
- [ ] Existing `tests/test_meeting_prefetch.py` still passes
