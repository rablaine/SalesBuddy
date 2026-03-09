# Issue #46: Fiscal Year Roll-Up and Onboarding Procedures

**GitHub:** https://github.com/rablaine/NoteHelper/issues/46
**Filed by:** SurfEzBum
**Type:** Feature Request / Design Discussion

---

## The Problem

Microsoft's fiscal year runs July 1 – June 30. Every July, territories reshuffle, sellers get reassigned, and account portfolios change. This raises several questions:

1. **FY Finalization** — What happens to last year's data?
2. **Holding Period** — There's a ~3-4 week "red carpet" period (early July) while new assignments are finalized in MSX. Work continues but assignments are in flux.
3. **Account Continuity** — Some accounts carry over (same seller), some are warm handoffs, some are brand new.
4. **Re-onboarding** — Should new/returning accounts get a special setup experience (favicons, folder structure, etc.)?

---

## Current State of the Art

### What we already have that helps:
- **Per-customer JSON backup** (v3) — Notes, engagements, and all metadata backed up to OneDrive. One file per customer, organized by seller name.
- **Full DB backup** — `notehelper.db` is copied to OneDrive with timestamps. Can be restored manually.
- **Idempotent MSX import** — Re-running the import updates territories, sellers, customers, and accounts. It already handles re-assignment gracefully (updates `customer.seller_id`, `customer.territory_id`, etc.).
- **Onboarding wizard** — 5-step first-run modal: theme → MSX login → import accounts → sync milestones → import revenue.
- **Favicon fetch** — Batch-fetches favicons for all customers missing one.
- **Clear milestones / Clear revenue** — Admin endpoints already exist for wiping MSX-synced data.

### What we're missing:
- No way to archive or bulk-close engagements
- No way to remove departed customers (or mark them inactive)
- No change detection after MSX re-import (what's new, what's gone, what moved)
- No handoff/export mechanism for customers going to another seller
- No bulk "new account onboarding" (favicons + folder structure for a batch)
- No FY tracking on any core entity

---

## Recommended Approach: Same DB, Guided Cutover

**Key decision: We should NOT create a new DB each year.**

Rationale:
- NoteHelper is a single-user, local SQLite app. Multi-DB management adds complexity with little benefit.
- Historical notes and engagements are valuable for continuity — especially for accounts you keep.
- The backup system already provides point-in-time snapshots for archival.
- MSX import already handles the "refresh assignments" step idempotently.

Instead, we build a **FY Cutover Wizard** — a guided process that walks the user through the transition, using and extending capabilities we already have.

---

## FY Cutover Wizard — Proposed Flow

### Step 0: Pre-Flight (Automatic)
*Runs on wizard launch, before user does anything.*

- Run full customer backup → OneDrive (already implemented)
- Create timestamped DB snapshot → OneDrive (already implemented)
- Show backup confirmation with timestamps

### Step 1: Re-Import from MSX
*User clicks "Refresh Accounts" — runs the existing MSX import.*

The import already handles:
- New territories/sellers/customers → created
- Changed assignments → updated (seller_id, territory_id)
- Existing customers with no change → skipped

**New: Change Detection Report**
After the import completes, show a diff summary:

| Category | Count | Details |
|----------|-------|---------|
| 🆕 New Accounts | 12 | Accounts that appeared (not in DB before import) |
| 👋 Departed Accounts | 5 | Accounts in DB but NOT in MSX results anymore |
| 🔄 Reassigned Accounts | 3 | Accounts where seller or territory changed |
| ✅ Unchanged Accounts | 45 | Same seller, same territory, nothing changed |

This requires snapshotting the customer list before import and comparing after. The MSX import route already tracks what it touches — we'd extend it to also track additions/changes explicitly.

### Step 2: Handle Departed Accounts
*Show the list of accounts no longer on the user's book.*

For each departed account, offer options:
- **Archive** — Mark as inactive (hidden from main views, but data preserved). Can be unarchived later.
- **Export & Archive** — Generate a handoff package (JSON or markdown) with all notes + engagements, then archive. The new account owner could import this if they also use NoteHelper.
- **Keep** — Leave it active (maybe you still collaborate on it even though it's not "yours" in MSX).
- **Delete** — Permanently remove the customer and all associated data (with confirmation).

This needs a new `Customer.is_archived` boolean field (default False). Archived customers are excluded from the main customers list, homepage counts, and search results, but remain in the DB and backups.

### Step 3: Clean Up Engagements
*Show all Active/On Hold engagements and prompt for bulk resolution.*

- List all open engagements grouped by customer
- Allow bulk status change: mark stale ones as Won/Lost
- Departed customer engagements should be highlighted 
- Carried-over account engagements stay as-is

### Step 4: Onboard New Accounts
*Batch setup for the newly imported accounts.*

- **Fetch favicons** — Already implemented, just scope it to new accounts
- **Account directory creation** (Ben's suggestion) — Create a standard folder structure on the local filesystem or OneDrive for each new customer:
  ```
  {OneDrive}/Customers/{Territory}/{CustomerName}/
  ├── Notes/
  ├── Decks/
  └── Resources/
  ```
  This is a nice-to-have that could be configurable (template folder structure in preferences). Would use the same OneDrive detection logic from backup.py.
- **Milestone sync** — Run milestone sync for new accounts (already implemented)

### Step 5: Confirmation
*Summary of what was done.*

- X accounts imported (Y new, Z departed, W reassigned)
- X engagements closed
- X departed accounts archived 
- Backups confirmed at {timestamp}
- "You're ready for FY{XX}!"

---

## Data Model Changes

### New fields:

```python
# Customer model
class Customer(db.Model):
    # ... existing fields ...
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    archived_reason = db.Column(db.String(50), nullable=True)  # 'departed', 'manual', etc.
```

### Migration:
- Add `is_archived`, `archived_at`, `archived_reason` to `customers` table
- Idempotent migration (check column exists before ALTER TABLE)

### Query changes:
- All customer list queries add `.filter_by(is_archived=False)` by default
- Add "Show Archived" toggle to customers list page
- Archived customers still show in search results with a visual indicator
- Customer view page shows "Archived" banner with unarchive option

---

## Holding Period Strategy

The 3-4 week gap between FY end (June 30) and new assignments landing in MSX (mid-to-late July) is handled naturally:

1. **Keep working normally** — Nothing in NoteHelper forces a cutover. Notes, engagements, milestones all keep working.
2. **Run the wizard when ready** — The FY Cutover Wizard is available anytime from Admin Panel. User launches it when their new accounts are finalized in MSX.
3. **No hard date boundary** — The wizard is stateless. You can run it in July, August, or whenever. It just compares what's in MSX vs. what's in the DB at that moment.
4. **Warm handoffs** — During the holding period, you might take notes on accounts you're handing off. Those notes are preserved and can be exported in Step 2.

---

## Handoff Package Format

For departed accounts that need to be handed off to the new owner:

```json
{
  "format_version": "handoff_v1",
  "exported_at": "2026-07-15T10:30:00Z",
  "exported_by": "Alex Blaine",
  "customer": {
    "name": "Contoso Ltd",
    "tpid": 12345678,
    "account_context": "Strategic AI workloads on Azure...",
    "territory": "US East Enterprise",
    "notes": [ ... ],
    "engagements": [ ... ]
  }
}
```

This is nearly identical to the existing v3 backup format — we could reuse the same serializer with a different wrapper. The receiving user could import it via the existing restore flow (match by TPID).

---

## Implementation Priority

### Phase A — Core (MVP for FY27 cutover)
1. **`Customer.is_archived` field + migration** — Small, foundational
2. **Archive/unarchive UI** — Customer view + customers list filter
3. **Change detection in MSX import** — Snapshot-before, diff-after, show report
4. **FY Cutover Wizard** — Steps 0-2 + 5 (backup, import, departed accounts, summary)

### Phase B — Nice-to-Have
5. **Bulk engagement cleanup** (Step 3)
6. **Handoff package export/import**
7. **New account onboarding batch** (favicons + milestones for new accounts only)

### Phase C — Future
8. **Account directory creation** on filesystem/OneDrive
9. **FY history tracking** — Record which FY a customer was active in
10. **Year-over-year analytics** — Compare engagement/revenue across FYs

---

## Open Questions

1. **How do we detect "departed" accounts?** The MSX import knows which accounts are in the user's current book (via `scan_init()` → `msp_accountteams` query). Accounts in the DB but not in the MSX results are "departed." But what if someone manually added a customer that was never in MSX — should that be excluded from the departed list?

2. **Should archived customers count in analytics?** Probably not by default, but maybe with a toggle.

3. **What about shared/team accounts?** If multiple sellers use NoteHelper for the same customer, a departure for one seller doesn't mean the customer should be archived. This is mostly a single-user concern for now but worth noting.

4. **Folder structure template** — Should this be hardcoded or configurable? Configurable is more flexible but adds UI complexity. Could start with a sensible default and make it configurable later.

5. **When to prompt for cutover?** Should the app detect that it's July and nudge the user, or just rely on them knowing to do it? A subtle "FY Cutover available" banner in the admin panel during July/August might be nice.

---

## Estimated Effort

| Phase | Items | Estimate |
|-------|-------|----------|
| A — Core | Archive field, UI, change detection, wizard shell | Medium (2-3 sessions) |
| B — Nice-to-Have | Engagement cleanup, handoff export, batch onboarding | Medium (2-3 sessions) |
| C — Future | Folder creation, FY history, YoY analytics | Large (separate issues) |

---

## Summary

The cleanest approach is: **keep one DB forever, add archive capabilities, and build a guided wizard that wraps the MSX re-import with before/after comparison and cleanup steps.** This leverages everything we already have (backup, MSX import, favicon fetch, milestone sync) and adds just enough new capability (archive flag, change detection, cutover wizard) to make FY transitions smooth.

The holding period isn't a technical problem — it's a workflow problem. The wizard is available whenever the user is ready, and nothing breaks if they wait a few weeks.
