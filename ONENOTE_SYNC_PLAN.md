# OneNote Sync Plan

## Overview

Sync NoteHelper call logs to OneNote as a living reference and secondary backup. Each user gets one notebook, organized by seller (one section per seller), with one page per customer account. Pages are fully replaced on every sync, making the database the single source of truth and OneNote a read-only projection.

## Architecture

### Data Flow

```
User saves call log
    |
    v
Normal DB write (user doesn't wait)
    |
    v
INSERT/UPSERT into sync_queue (customer_id, pending)
    |
    v
Background worker picks up job (~10s polling)
    |
    v
Build full HTML page from DB
    |
    v
POST (new) or PATCH (existing) to Graph API
    |
    v
Store OneNote page URL on customer record
    |
    v
Mark job complete (or retry on failure)
```

### Why Full Page Replacement?

- **Idempotent**: If a sync fails halfway, just retry the same operation. No diffing, no merge conflicts.
- **Deduplication is trivial**: 5 rapid edits to the same customer = 1 queued sync. The page always reflects current DB state.
- **Simple mental model**: OneNote page = snapshot of customer record + all call logs.

## Database Changes

### New Table: `onenote_sync_queue`

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| customer_id | INTEGER FK | References customers.id |
| action | TEXT | `create`, `update`, or `soft_delete` |
| status | TEXT | `pending`, `processing`, `complete`, `failed` |
| retry_count | INTEGER | Default 0, max 5 |
| created_at | DATETIME | When the job was queued |
| last_attempted_at | DATETIME | Last processing attempt |
| completed_at | DATETIME | When successfully completed |
| error_message | TEXT | Last error for debugging |

**Dedup strategy**: UPSERT per customer_id where status = `pending`. If a pending job already exists for that customer, just update its `created_at` timestamp. No need to queue multiple syncs for the same customer since we replace the entire page.

### New Columns on `customers`

| Column | Type | Notes |
|--------|------|-------|
| onenote_page_id | TEXT | Graph API page ID (used for PATCH) |
| onenote_page_url | TEXT | Web URL for the page (for future in-app linking) |
| onenote_last_synced | DATETIME | Last successful sync timestamp |

### New Table: `onenote_config` (or app config / env vars)

| Setting | Notes |
|---------|-------|
| notebook_id | The user's NoteHelper notebook ID |
| section_ids | JSON map of seller_id to section_id |

Could live in the existing config mechanism or as env vars. Notebook ID is created once during setup; section IDs are created on-demand when a seller is first encountered.

## Notebook Structure

```
NoteHelper (Notebook)
├── Jane Smith (Section - seller)
│   ├── Contoso Ltd (Page - customer)
│   ├── Fabrikam Inc (Page - customer)
│   └── Woodgrove Bank (Page - customer)
├── John Doe (Section - seller)
│   ├── Adventure Works (Page - customer)
│   └── Tailwind Traders (Page - customer)
└── Unassigned (Section - customers with no seller)
    └── Northwind Corp (Page - customer)
```

### Section Management

- Sections are created on-demand when a customer's seller doesn't have one yet
- Store the mapping of seller_id -> section_id in the database
- If a customer's seller changes, the page moves: delete from old section, create in new section (or just recreate -- full replacement means this is clean)
- Customers with no seller go in an "Unassigned" section

## Page HTML Template

OneNote pages are HTML with some restrictions (no custom CSS/JS, but basic structure and tables work fine).

```html
<html>
<head><title>Contoso Ltd</title></head>
<body>
  <h1>Contoso Ltd</h1>
  <table>
    <tr><td><b>TPID</b></td><td>12345</td></tr>
    <tr><td><b>Account URL</b></td><td><a href="https://...">View in MSX</a></td></tr>
    <tr><td><b>Seller</b></td><td>Jane Smith</td></tr>
    <tr><td><b>Verticals</b></td><td>Healthcare, Finance</td></tr>
    <tr><td><b>NoteHelper</b></td><td><a href="https://yourapp/customers/42">Open in NoteHelper</a></td></tr>
    <tr><td><b>Last Updated</b></td><td>2026-03-03 14:30 UTC</td></tr>
  </table>

  <hr/>
  <h2>Call Notes (12)</h2>

  <h3>2026-03-01 - Quarterly Review</h3>
  <p><b>Seller:</b> Jane Smith | <b>Topics:</b> Azure SQL, Migration</p>
  <p>Discussed migration timeline for Azure SQL. Customer wants to move
  production workloads by Q3...</p>

  <h3>2026-02-15 - Technical Deep Dive</h3>
  <p><b>Seller:</b> Jane Smith | <b>Topics:</b> AKS, Containers</p>
  <p>Walked through AKS architecture for their microservices platform...</p>
</body>
</html>
```

Call logs ordered newest-first so the most recent info is at the top.

## Background Worker

### Implementation

A background thread that starts with the Flask app, polling the sync queue:

```
while running:
    job = get_oldest_pending_job()
    if not job:
        sleep(10 seconds)
        continue

    mark_processing(job)

    try:
        customer = get_customer_with_call_logs(job.customer_id)
        html = render_onenote_page(customer)

        if job.action == 'soft_delete':
            update_page_title_as_deleted(customer)
        elif customer.onenote_page_id:
            patch_page(customer.onenote_page_id, html)
        else:
            section_id = get_or_create_section(customer.seller)
            page = create_page(section_id, html)
            customer.onenote_page_id = page.id
            customer.onenote_page_url = page.web_url

        customer.onenote_last_synced = now()
        mark_complete(job)
    except GraphAPIError:
        job.retry_count += 1
        job.error_message = str(error)
        if job.retry_count >= 5:
            mark_failed(job)
        else:
            mark_pending(job)  # back in queue
            # exponential backoff: 30s, 1m, 5m, 15m, 30m
```

### Retry Strategy

| Attempt | Backoff | Cumulative Wait |
|---------|---------|-----------------|
| 1 | 30 seconds | 30s |
| 2 | 1 minute | 1.5m |
| 3 | 5 minutes | 6.5m |
| 4 | 15 minutes | 21.5m |
| 5 | 30 minutes | 51.5m |
| 6+ | Marked `failed`, stops retrying | -- |

### Verification

Start with "trust the 200" -- if Graph API returns success, the sync is done. Only add read-back verification if we see data loss in practice. The retry logic handles transient failures (503s, timeouts), which is the more common issue.

## Customer Deletion Handling

When a customer is deleted from NoteHelper:

1. Queue a `soft_delete` action for that customer
2. Worker PATCHes the page title to: `[DELETED] Contoso Ltd`
3. Worker updates the page body to show a deletion notice at the top:
   ```html
   <div style="background: #fff3cd; padding: 10px; border: 1px solid #ffc107;">
     <b>This account was deleted from NoteHelper on 2026-03-03.</b>
     The call log history below is preserved for reference.
   </div>
   ```
4. Page stays in OneNote as a historical record -- user can manually delete it if they want

This is minimal effort since we're already doing full page replacement. We just render a slightly different template.

## Authentication

### Scopes Required

Add `Notes.ReadWrite` to the existing delegated OAuth scopes (alongside whatever MSX uses). This is the minimal OneNote permission -- read/write the user's own notebooks only.

### Token Flow

Reuse the existing Azure AD OAuth flow:
- User authenticates during setup (same login flow as MSX)
- Store refresh token (already happening for MSX)
- Add `Notes.ReadWrite` to the requested scopes
- Background worker uses the refresh token to get access tokens as needed
- If refresh token expires, mark sync as paused and surface a "re-authenticate" prompt in the admin panel

### App Registration

Same app registration as MSX, just add the OneNote scope. No additional admin consent needed for `Notes.ReadWrite` (delegated, user's own notebooks).

## Setup Flow

### Integration with Existing Setup Wizard

Since we're already talking to users about OneDrive during the backup setup step, OneNote setup fits naturally alongside it:

1. During first-run / setup wizard, after OneDrive backup step:
   - "Would you like to sync your call logs to OneNote?"
   - If yes: trigger OAuth with `Notes.ReadWrite` scope
   - Create a "NoteHelper" notebook (or find existing one)
   - Store notebook_id in config
   - Enable sync by default going forward

2. In admin panel:
   - Show OneNote sync status (enabled/disabled, last sync, queue depth, any failures)
   - Toggle to enable/disable sync
   - "Sync All Now" button for manual full resync
   - Link to the notebook in OneNote

### Environment Variables

```
ONENOTE_SYNC_ENABLED=true          # Feature flag
ONENOTE_NOTEBOOK_NAME=NoteHelper   # Name for auto-created notebook
```

## Initial Backfill

### Standalone Script: `scripts/onenote_backfill.py`

For existing users (right now, just Alex and testers) to populate OneNote with all existing customer data:

```
Usage: python scripts/onenote_backfill.py [--dry-run]

What it does:
1. Authenticates via existing stored credentials
2. Creates/finds the NoteHelper notebook
3. Creates sections for each seller
4. For each customer with call logs:
   a. Renders the full page HTML
   b. Creates the page in the appropriate section
   c. Stores onenote_page_id and onenote_page_url on the customer record
5. Reports: "Synced 47 customers across 5 seller sections. 2 failures (will retry on next app start)."

Options:
  --dry-run     Show what would be synced without making API calls
  --customer=ID Sync a single customer (for testing)
  --verbose     Show full API responses
```

### Post-Backfill

Once backfill is run and verified, the background worker takes over for all future changes. New users who go through the setup wizard get sync enabled from the start and never need the script.

## API Rate Limits

Microsoft Graph OneNote limits:
- ~4 requests/second per user for writes
- Worker should include a small delay between operations (250ms minimum)
- Respect 429 (Too Many Requests) responses and `Retry-After` headers
- Backfill script should be especially conservative (500ms-1s between pages)

## Implementation Phases

### Phase 1: Core Sync Engine
- Database migrations (sync_queue table, customer columns)
- Page HTML renderer (Jinja2 template for OneNote page content)
- Graph API client for OneNote (create notebook, create section, create/patch page)
- Background worker thread with retry logic
- Queue insertion on call log create/update
- Backfill script

### Phase 2: Setup and Admin
- Add `Notes.ReadWrite` scope to OAuth flow
- Setup wizard integration (create notebook, enable sync)
- Admin panel status card (sync status, queue depth, failures, toggle)
- Store OneNote page URL on customer records

### Phase 3: Polish
- Soft-delete handling (title + banner on customer deletion)
- Seller change detection (move page to new section)
- "Open in OneNote" link somewhere in customer view (TBD on placement)
- Error notifications in admin panel for persistent failures

## Open Decisions (Deferred)

- **In-app display of OneNote link**: We're storing the URL but not showing it yet. Could be a small icon on the customer page, a column in lists, or just admin-visible. Decide later based on how users interact with the synced pages.
- **Multi-user notebook sharing**: Currently one notebook per user. If teams want shared notebooks, that's a future conversation about permissions and conflict resolution.
- **Selective sync**: Currently syncs all customers. Could add a per-customer or per-seller toggle if the notebook gets too noisy. Probably not needed at current scale.

---

*Last Updated: March 3, 2026*
