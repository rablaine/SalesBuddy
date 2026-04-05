# MSX Workspace - Replicate MSX Helper in SalesBuddy

## Summary

[MSX Helper](https://github.com/mitulashah/msx-helper) is an Electron/React desktop app that gives SEs a fast interface for managing Dynamics 365 opportunities, milestones, tasks, and team membership. It's a polished tool, but it requires a separate install, separate auth, and doesn't integrate with any of the note-taking, customer context, or reporting that SalesBuddy already provides.

SalesBuddy already has **all the backend plumbing** - MSX auth, OData queries, milestone/opportunity models, task creation, team membership, forecast comments - we just need a single rich report page ("MSX Workspace") that surfaces it interactively.

**Goal:** One report page that replaces the need to install MSX Helper entirely. Everything MSX Helper does, but integrated with SalesBuddy's customer/note/engagement context.

---

## What MSX Helper Does (Feature Inventory)

### Accounts Tab
- Browse accounts by configured TPIDs
- Filter by name/TPID, show only parent accounts
- Bulk-select accounts to scope opportunity/milestone views
- Cache account list for fast return visits

### Opportunities Tab
- List active opportunities across selected accounts
- Columns: name, number (7-#########), owner, account, solution play, close date, consumed recurring
- Filter by: account, solution play, deal team membership, favorites
- Search by: opportunity number, GUID
- Detect deal team membership (access team template `cc923a9d-7651-e311-9405-00155db3ba1e`)
- Favorite/unfavorite opportunities
- Recent opportunities list (last 10 viewed)
- Column visibility/sort preferences (persisted)
- Open Teams chat with opportunity owner

### Milestones Tab
- List milestones for selected opportunity(s) or search by number/GUID
- Columns: name, number, status, status reason, category, monthly use, date, owner
- Filter by: status (On Track/At Risk/Blocked/Complete/Cancelled/Not Started), account, opportunity, team membership, favorites
- Group by opportunity (collapsible sections)
- Join/leave milestone access team (template `316e4735-9e83-eb11-a812-0022481e1be0`)
- Bulk join/leave teams with adaptive batching
- Edit milestone date, monthly use inline
- View/manage forecast comments (JSON in `msp_forecastcomments` field)
- Lazy-load comment count and last modified date

### Activities Panel (HOK Tasks)
- List tasks for a selected milestone
- Columns: subject, description, category, due date, status, owner
- Create task with category, subject, description, due date
- Edit task (owner-restricted)
- Close task (via CloseTask action or Close method)
- Delete task (owner-restricted)
- Bulk create tasks across multiple selected milestones
- Task categories with configurable default due dates:
  - Technical Close/Win Plan (606820005) - 20 days
  - Architecture Design Session (861980004) - 10 days
  - Architecture Design Session (861980006) - 15 days
  - Consumption Plan (861980007) - 20 days
  - Demo (861980002) - 15 days
  - PoC/Pilot (861980005) - 60 days
  - Workshop (861980001) - 30 days

### Tags System (Local Only)
- Create/rename/delete tags with custom colors
- Assign tags to opportunities, milestones, accounts
- Tag snapshots capture entity state at tagging time
- Pinned tags, recent tags, merge tags
- Not persisted to CRM - localStorage only

### Settings
- CRM URL configuration
- Tenant ID configuration
- Account TPID management
- Activity due date defaults per task category
- Settings export/import (JSON)
- Token status display with expiration countdown

---

## What SalesBuddy Already Has

| Feature | Status | Notes |
|---------|--------|-------|
| MSX auth (az CLI token) | Done | `msx_auth.py` - token caching, refresh, VPN detection |
| WhoAmI / connection test | Done | `msx_api.py::test_connection()` |
| Account lookup by TPID | Done | `msx_api.py::lookup_account_by_tpid()` |
| Account team discovery | Done | Sellers, SEs, CSAMs, DSS |
| Opportunity fetch & caching | Done | `msx_api.py::get_opportunities_by_account()`, `Opportunity` model |
| Milestone fetch & caching | Done | `msx_api.py::get_milestones_by_account()`, `Milestone` model |
| Milestone access team membership | Done | `msx_api.py::get_my_milestone_team_ids()` |
| Task creation in MSX | Done | `msx_api.py::create_task()`, `MsxTask` model |
| Forecast comments caching | Done | `MilestoneComment` model, `cached_comments_json` |
| Milestone audit trail | Done | `MilestoneAudit` model |
| Milestone list/view pages | Done | `routes/milestones.py` |
| Opportunity list/view pages | Done | `routes/opportunities.py` |
| MSX admin (sync, clear) | Done | `routes/msx.py` |
| Deal team membership detect | Partial | Milestone teams done, opportunity deal team TBD |
| Inline milestone editing | Not built | Need PATCH to `msp_engagementmilestones` |
| Task edit/close/delete | Not built | Need PATCH/DELETE/CloseTask endpoints |
| Bulk team join/leave | Not built | Need batch $batch requests |
| Opportunity deal team join | Not built | Different template ID |
| Rich interactive filtering UI | Not built | Current pages are basic list views |
| Tags system | Not needed | SalesBuddy has its own topic/tag system |
| Settings export/import | Not needed | SalesBuddy has its own prefs system |

---

## Implementation Plan

### Route: `/reports/msx-workspace`

Single report page under the `reports` blueprint (per route placement rules). Three interactive tabs within the page, similar to MSX Helper's layout.

### Phase 1: Opportunity Browser

**New template:** `templates/report_msx_workspace.html`

**Opportunities tab with:**
- Table of active opportunities from synced data (already in our `Opportunity` model)
- Client-side filtering: account, solution play, deal team status, search by number
- Sort by any column (name, number, close date, owner, account)
- Click opportunity to expand inline detail (description, customer need, compete threat)
- Link to SalesBuddy opportunity view (existing page) and MSX URL
- "Refresh from MSX" button to re-sync opportunities for selected customers
- Show which opportunities the user is on the deal team for

**Backend:** Mostly reads from existing cached data. One new API endpoint:
- `GET /api/reports/msx-workspace/opportunities` - JSON endpoint returning filtered opportunity data with deal team status

### Phase 2: Milestone Browser + Team Management

**Milestones tab with:**
- Table of milestones, filterable by status, account, opportunity
- Status filter defaults: On Track, At Risk, Blocked (matching MSX Helper)
- Group-by-opportunity toggle
- Join/leave milestone team buttons (already have the API call)
- Bulk select + bulk join team
- Show forecast comment count and last modified
- Click to expand: show cached comments, audit trail
- Link to SalesBuddy milestone view and MSX URL

**Backend additions:**
- `POST /api/msx/milestone/<id>/join-team` - join access team
- `POST /api/msx/milestone/<id>/leave-team` - leave access team
- `POST /api/msx/milestones/bulk-join-team` - bulk join with adaptive batching
- `GET /api/reports/msx-workspace/milestones` - JSON endpoint with filtering

### Phase 3: Task Management (HOK Activities)

**Activities panel (shown when milestone selected):**
- List tasks for selected milestone (from `MsxTask` model + fresh MSX query)
- Create task form: category dropdown, subject, description, due date (auto-calculated from category defaults)
- Edit task inline (owner-restricted)
- Close task button
- Delete task button (owner-restricted)
- Bulk create: select multiple milestones, create same task across all

**Backend additions to `msx_api.py`:**
- `update_task(task_id, fields)` - PATCH to `/tasks(id)`
- `close_task(task_id)` - POST CloseTask action with fallback
- `delete_task(task_id)` - DELETE `/tasks(id)`
- `bulk_create_tasks(milestone_ids, task_data)` - batch create

**New routes:**
- `POST /api/msx/task/create` (already exists, may need enhancement)
- `PATCH /api/msx/task/<id>/update`
- `POST /api/msx/task/<id>/close`
- `DELETE /api/msx/task/<id>/delete`

### Phase 4: Inline Milestone Editing + Comments

**Inline editing:**
- Click milestone date to edit inline, saves via PATCH
- Click monthly use to edit inline, saves via PATCH

**Forecast comments:**
- Expand milestone to see full comment thread
- Add new comment (appends to JSON array, PATCHes back)
- Edit own comments
- Delete own comments

**Backend additions to `msx_api.py`:**
- `update_milestone(milestone_id, fields)` - PATCH to `/msp_engagementmilestones(id)`
- Comments managed client-side (parse JSON, modify, send full array back via PATCH)

### Phase 5: Opportunity Deal Team

**Deal team detection and management:**
- Show "On Deal Team" / "Not On Deal Team" badge per opportunity
- Join/leave opportunity deal team (template `cc923a9d-7651-e311-9405-00155db3ba1e`)
- Requires: `AddUserToRecordTeam` / `RemoveUserFromRecordTeam` against opportunity entity

**Backend additions:**
- `get_my_deal_team_ids()` - query deal team memberships
- `join_deal_team(opportunity_id)` - POST AddUserToRecordTeam
- `leave_deal_team(opportunity_id)` - POST RemoveUserFromRecordTeam

---

## UI Design Notes

- Bootstrap 5 table with sticky header, client-side sort/filter
- Tab pills within the page (Opportunities | Milestones | Activities)
- Milestone status badges using existing SalesBuddy color conventions
- Filter bar at top of each tab (dropdowns + toggles)
- Activities panel slides in from right or shows below when a milestone is selected
- Inline editing via contenteditable or small modal popover
- Loading spinners for MSX API calls (they can be slow on VPN)
- Toast notifications for success/error on write operations

---

## What We Skip (MSX Helper Features Not Needed)

| Feature | Why Skip |
|---------|----------|
| Tags system | SalesBuddy has its own topic system that's richer |
| Settings export/import | SalesBuddy has its own preferences |
| Theme selection | SalesBuddy has its own dark mode |
| Token countdown UI | SalesBuddy already shows MSX auth status in admin |
| Extension system | SalesBuddy uses blueprints, not an extension registry |
| Electron auto-update | We're a web app |
| Column drag-to-reorder | Nice-to-have, not MVP |
| Keyboard shortcuts | Can add later if users want them |
| Recent/favorites | SalesBuddy already has engagement/note history |
| Teams chat integration | Low value, users already have Teams open |

---

## SalesIQ Tool Coverage

Per tool registry rules, add tools for the new report/API endpoints:
- `get_msx_workspace_opportunities` - query opportunities with filters
- `get_msx_workspace_milestones` - query milestones with filters
- `get_milestone_tasks` - list tasks for a milestone

---

## Effort Estimate

| Phase | Scope | Complexity |
|-------|-------|------------|
| Phase 1: Opportunity browser | Template + 1 API endpoint | Low |
| Phase 2: Milestone browser + teams | Template tabs + 3-4 API endpoints | Medium |
| Phase 3: Task CRUD | 4 new API endpoints + UI forms | Medium |
| Phase 4: Inline editing + comments | PATCH endpoints + inline UI | Medium |
| Phase 5: Deal team management | 3 API endpoints + UI badges | Low |

Phases 1-2 give 80% of the value. Phases 3-5 are incremental wins.

---

## Key MSX API Reference

### CRM Constants
- **CRM URL:** `https://microsoftsales.crm.dynamics.com`
- **API Base:** `/api/data/v9.2/`
- **Tenant:** `72f988bf-86f1-41af-91ab-2d7cd011db47`
- **Milestone Team Template:** `316e4735-9e83-eb11-a812-0022481e1be0`
- **Deal Team Template:** `cc923a9d-7651-e311-9405-00155db3ba1e`

### Task Categories (msp_taskcategory values)
| Category | Value | Default Days |
|----------|-------|-------------|
| Technical Close/Win Plan | 606820005 | 20 |
| Architecture Design Session | 861980004 | 10 |
| Architecture Design Session (alt) | 861980006 | 15 |
| Consumption Plan | 861980007 | 20 |
| Demo | 861980002 | 15 |
| PoC/Pilot | 861980005 | 60 |
| Workshop | 861980001 | 30 |
| Blocker Escalation | (TBD) | (TBD) |

### Write Operations Reference

**Create Task:**
```
POST /api/data/v9.2/tasks
{ "subject", "description", "msp_taskcategory", "scheduledend", 
  "_regardingobjectid_value": "milestone-id", "_ownerid_value": "user-id" }
```

**Update Task:**
```
PATCH /api/data/v9.2/tasks(task-id)
{ "subject", "description", "scheduledend", "statuscode" }
```

**Close Task (primary):**
```
POST /api/data/v9.2/CloseTask
{ "TaskClose": { "subject": "Task Closed", "activityid@odata.bind": "/tasks(task-id)" }, "Status": 5 }
```

**Close Task (fallback):**
```
POST /api/data/v9.2/tasks(task-id)/Microsoft.Dynamics.CRM.Close
{ "Status": 5 }
```

**Delete Task:**
```
DELETE /api/data/v9.2/tasks(task-id)
```

**Update Milestone:**
```
PATCH /api/data/v9.2/msp_engagementmilestones(milestone-id)
{ "msp_milestonedate", "msp_monthlyuse", "msp_forecastcomments" }
```

**Join Team:**
```
POST /api/data/v9.2/systemusers(user-id)/Microsoft.Dynamics.CRM.AddUserToRecordTeam
{ "Record": { "@odata.type": "...", id }, "TeamTemplate": { "@odata.type": "...", id } }
```

**Leave Team:**
```
POST /api/data/v9.2/systemusers(user-id)/Microsoft.Dynamics.CRM.RemoveUserFromRecordTeam
{ "Record": { "@odata.type": "...", id }, "TeamTemplate": { "@odata.type": "...", id } }
```

### Retry Policy (Match MSX Helper)
- Retryable: 408, 429, 500, 502, 503, 504
- Non-retryable: 401, 403, 400
- Max retries: 3 (reads), 0 (writes)
- Backoff: exponential (500ms * 2^attempt)
- 429: respect Retry-After header
