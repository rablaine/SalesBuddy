# Customer M&A Handling (Issue #41)

## Problem

When customers undergo mergers, acquisitions, or restructuring in MSX, their TPIDs can change, disappear, or get consolidated. SalesBuddy currently keys customers by TPID with no change-tracking, so M&A events silently orphan data (notes, engagements, milestones, revenue, etc.) on stale customer records while creating new empty ones.

14 tables reference `customer_id` - all need to be re-pointed during a merge.

## M&A Scenarios

### Scenario 1: TPID Reassignment (Subsidiary Absorption)

A customer we actively track keeps its MSX account GUID but gets a new TPID because it was absorbed under a larger parent. It goes from `parentaccountlevel = Top` to `Child`.

**What happens today:** The old TPID stops appearing in MSX sync results (it's no longer top-level). The new parent TPID may or may not be in our DB. The old Customer record sits orphaned with all its notes and data. If the new parent TPID shows up, it gets created as a fresh empty customer.

**Detection challenge:** We don't store MSX account GUIDs, so we can't see that "account X now lives under TPID Y." We CAN detect that a known TPID disappeared from sync results.

**Proposed approach:**
- During sync, track which known TPIDs are still present in MSX results
- After sync, flag any Customer whose TPID was NOT seen as "potentially merged/stale"
- Surface these in the admin panel with a "Merge Into..." action
- User picks the destination customer (the new parent) and we migrate all data

### Scenario 2: Customer Disappears, New Entity Created

During M&A, an old TPID simply vanishes from MSX. A new TPID is created for the merged entity. No automated link between old and new.

**What happens today:** Old customer sits in DB forever with all its data. New customer gets created empty on next sync. User has no way to connect them.

**Proposed approach:**
- Admin panel "Merge Customer" tool
- User selects source customer (old) and destination customer (new)
- All linked data migrates from source to destination
- Source customer is soft-deleted or hard-deleted after migration
- Audit log entry created for traceability

### Scenario 3: Two Tracked Customers Merge

We actively track both Company A (TPID 1000) and Company B (TPID 2000). They merge in real life. MSX creates TPID 3000 for the combined entity, and both 1000 and 2000 become children or disappear.

**What happens today:** Both old customers orphaned. New TPID 3000 created empty.

**Proposed approach:**
- Same admin merge tool, but support merging multiple sources into one destination
- Notes from both companies land on the merged customer
- Revenue data may need special handling (historical data from both entities)

### Scenario 4: Customer Splits (Divestiture)

A large customer (TPID 1000) spins off a division. MSX creates TPID 2000 for the new entity. TPID 1000 still exists but is smaller.

**What happens today:** New TPID 2000 gets created as empty customer. Notes that were about the spun-off division stay on the parent. No way to move specific notes.

**Proposed approach:**
- This is lower priority - splits are less common than merges
- Could support selective note migration (move specific notes/engagements to a different customer)
- For now, users can manually re-tag notes via the note edit form

### Scenario 5: TPID Stays, Name Changes Significantly

Company rebrands or the MSX name record gets corrected. TPID stays the same.

**Already handled.** The sync updates customer name on TPID match and logs "Customer name changed for TPID X." The `nickname` field preserves any user-set display name. No action needed.

### Scenario 6: Delayed TPID Migration

Customer gets acquired but keeps its old TPID for weeks/months while MSX catches up. Then one day the TPID flips. User has been adding notes the whole time.

**Same as Scenario 1** but highlights the importance of making the merge tool easy to use since this can happen at any time, not just during sync.

## Implementation Plan

### Phase 1: Sync-Time Stale Detection

**Goal:** Detect customers whose TPIDs disappeared from MSX.

1. Before sync writes, snapshot all known TPIDs from DB
2. After sync, compare: `known_tpids - seen_tpids = disappeared_tpids`
3. Add a `stale_since` (DateTime, nullable) column to Customer
4. Set `stale_since = utc_now()` for disappeared TPIDs (clear it if TPID reappears)
5. Show a warning badge on stale customers in the customer list
6. Don't auto-delete anything - just flag

**Caveat:** A TPID might disappear because the user lost assignment in MSX, not because of M&A. The stale flag is a hint, not a certainty. The admin merge tool handles the actual decision.

### Phase 2: Admin Merge Tool

**Goal:** Let the user merge one customer into another.

**UI:** Admin panel section or a dedicated page.
- Source customer selector (the one being absorbed/deleted)
- Destination customer selector (the surviving entity)
- Preview: show counts of what will be migrated (X notes, Y engagements, Z milestones...)
- Confirm button with clear warning
- After merge, redirect to destination customer view

**Backend migration logic** (`merge_customer(source_id, dest_id)`):
1. Move all linked records (14 tables) from source to destination:
   - `notes` - update `customer_id`
   - `engagements` - update `customer_id`
   - `milestones` - update `customer_id`
   - `opportunities` - update `customer_id`
   - `customer_contacts` - update `customer_id` (check for dupes by email?)
   - `customer_revenue_data` - update `customer_id`
   - `product_revenue_data` - update `customer_id`
   - `revenue_analyses` - update `customer_id`
   - `marketing_summaries` - update `customer_id` (unique constraint - merge or replace?)
   - `marketing_interactions` - update `customer_id`
   - `marketing_contacts` - update `customer_id` (check for dupes?)
   - `u2c_snapshot_items` - update `customer_id` (or leave as historical?)
   - `customers_verticals` - merge (union of both sets, skip dupes)
   - `customers_csams` - merge (union, skip dupes)
2. Preserve source's `account_context` by appending to destination's (with a separator/header)
3. If source had a `nickname` and destination doesn't, carry it over
4. Delete the source Customer record
5. Log the merge action

**Edge cases:**
- `marketing_summaries` has `uq_marketing_summary_customer` unique constraint - can't have two summaries for same customer. Delete source's summary after merge (destination's is more current).
- `customer_contacts` - dedupe by email/name, keep the one with more data.
- Revenue data - could have overlapping fiscal periods. Keep both rows (different source TPIDs may have contributed different revenue).

### Phase 3: Stale Customer Dashboard (Admin)

**Goal:** Admin panel view showing all stale customers with quick-action merge buttons.

- Table of customers with `stale_since` set, sorted oldest first
- Each row shows: customer name, TPID, stale since date, linked data counts
- Quick actions: "Merge Into..." (opens merge flow), "Dismiss" (clears stale flag), "Delete" (if no linked data)

### Phase 4 (Future): Multi-Source Merge

Support merging 2+ source customers into one destination (Scenario 3). Same logic as Phase 2 but with a multi-select source picker.

## Data Migration Checklist

When implementing `merge_customer()`, update ALL of these:

| Table | FK Column | Nullable | Unique Constraints | Strategy |
|-------|-----------|----------|-------------------|----------|
| `notes` | `customer_id` | Yes | None | Move |
| `engagements` | `customer_id` | No | None | Move |
| `milestones` | `customer_id` | Yes | None | Move |
| `opportunities` | `customer_id` | Yes | None | Move |
| `customer_contacts` | `customer_id` | No | None | Move + dedupe |
| `customer_revenue_data` | `customer_id` | Yes | None | Move |
| `product_revenue_data` | `customer_id` | Yes | None | Move |
| `revenue_analyses` | `customer_id` | Yes | None | Move |
| `marketing_summaries` | `customer_id` | No | `uq_marketing_summary_customer` | Replace (keep dest) |
| `marketing_interactions` | `customer_id` | No | None | Move |
| `marketing_contacts` | `customer_id` | No | None | Move + dedupe |
| `u2c_snapshot_items` | `customer_id` | Yes | None | Move |
| `customers_verticals` | `customer_id` | No (PK) | Composite PK | Merge (union) |
| `customers_csams` | `customer_id` | No (PK) | Composite PK | Merge (union) |

## Open Questions

- [ ] Should we store MSX account GUIDs to enable automated TPID change detection in the future?
- [ ] Should merged customer data include a "migrated from TPID X" annotation on moved notes?
- [ ] Revenue data with overlapping periods from two source TPIDs - sum them or keep separate?
- [ ] Should the merge tool be available outside admin panel (e.g., on customer view page)?
- [ ] Do we need an undo/rollback for merges?
