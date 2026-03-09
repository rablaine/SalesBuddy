# Issue #51 — Active Engagements Front Page Tab

**GitHub:** https://github.com/rablaine/NoteHelper/issues/51
**Branch:** `feature/active-engagements-tab`
**Estimated effort:** Small-Medium (1–2 sessions)

---

## Goal

Add a third tab to the homepage pill navigation showing all active engagements across customers, giving sellers instant visibility into ongoing work.

## Current State

- Homepage (`templates/index.html`) has two tabs: **Calendar** and **Milestones Due**
- Tabs use Bootstrap 5 pills nav (`#noteViewTabs`) with AJAX-loaded content
- Tab selection persists via `localStorage` key `notehelper_home_tab`
- The `index()` route in `app/routes/main.py` passes `stats` and `has_milestones` — no engagement data
- Engagement cards already render on `customer_view.html` with status badges, story completeness bars, and quick stats — we can reuse that pattern

## Engagement Model Reference

- **Statuses:** `['Active', 'On Hold', 'Won', 'Lost']`
- **Key fields:** `title`, `status`, `customer_id`, `estimated_acr`, `target_date`, story fields (6)
- **Relationships:** `customer`, `notes` (M2M), `opportunities` (M2M), `milestones` (M2M)
- **Properties:** `story_completeness` (0–100), `linked_note_count`

## Implementation Plan

### Step 1 — API endpoint for active engagements

**File:** `app/routes/main.py` (or a new `app/routes/engagements.py` if one exists)

Create `GET /api/engagements/active` that returns:
```json
[
  {
    "id": 1,
    "title": "AVD Migration",
    "status": "Active",
    "customer_name": "Acme Corp",
    "customer_id": 5,
    "seller_name": "Alice Smith",
    "estimated_acr": "$50K",
    "target_date": "2026-04-15",
    "story_completeness": 67,
    "linked_note_count": 3,
    "opportunity_count": 2,
    "milestone_count": 1,
    "updated_at": "2026-03-05T14:30:00Z"
  }
]
```

**Query logic:**
- `Engagement.query.filter(Engagement.status.in_(['Active', 'On Hold']))`
- Eager load `customer.seller`, `notes`, `opportunities`, `milestones`
- Sort by `updated_at DESC` (most recently worked first)
- Support optional query param `?status=Active` to filter

### Step 2 — Add the tab pill to index.html

Add a third nav pill after Milestones Due:
```html
<li class="nav-item" role="presentation">
  <button class="nav-link" id="engagements-tab" data-bs-toggle="pill"
          data-bs-target="#engagements-view" type="button" role="tab">
    <i class="bi bi-kanban"></i> Engagements
    <span class="badge bg-success ms-1" id="engagements-count"></span>
  </button>
</li>
```

**Visibility:** Always show (unlike Milestones which requires sync). Even if no engagements exist, the tab shows an empty state with a "Create your first engagement" CTA.

### Step 3 — Engagement tab pane content

Add a new `tab-pane` div with id `engagements-view`. Render a card-based list (reuse the pattern from `customer_view.html` engagement cards):

For each engagement:
- **Customer name** (linked to customer view) + **engagement title** (linked to engagement view)
- **Status badge** (Active=green, On Hold=warning)
- **Story completeness** progress bar
- **Quick stats row:** notes count, opportunity count, milestone count
- **Last activity date** (from `updated_at`)
- **Estimated ACR** and **target date** if set

**Empty state:** "No active engagements yet. Create one from any customer page."

### Step 4 — AJAX loading + tab persistence

Follow the existing pattern (Calendar/Milestones):
- On tab show, fetch `/api/engagements/active` via `fetch()`
- Render the HTML client-side using the JSON response
- Cache in a variable to avoid re-fetching on every tab switch
- Include in the localStorage tab persistence logic (`notehelper_home_tab`)

### Step 5 — Filter/sort controls (lightweight)

Add a simple toolbar above the engagement list:
- **Status filter:** radio buttons for "Active" / "On Hold" / "All" (default: All active statuses)
- **Sort:** dropdown for "Last Updated" / "Target Date" / "Customer Name"
- Client-side filtering/sorting (the dataset will be small enough)

## Files Changed

| File | Change |
|------|--------|
| `app/routes/main.py` | New `/api/engagements/active` endpoint |
| `templates/index.html` | Add tab pill + tab pane + JS fetch logic |

## Testing

- Test the API endpoint returns correct engagements filtered by status
- Test empty state (no engagements)
- Test engagement with/without linked milestones/opportunities
- Test tab persistence via localStorage mock

## Open Questions

1. ~~Should we include "Won" engagements?~~ **Decision: No** — only Active and On Hold. Won/Lost are historical.
2. Should clicking an engagement card go to the engagement view or the customer view? **Leaning toward:** engagement view directly, with a breadcrumb back to customer.
3. Should the count badge on the tab pill show the total count of active engagements? **Leaning toward:** Yes, similar to how other apps show notification counts.
