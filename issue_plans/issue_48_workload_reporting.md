# Issue #48 — Workload / Engagement-Based Reporting

**GitHub:** https://github.com/rablaine/NoteHelper/issues/48
**Branch:** `feature/workload-reporting`
**Estimated effort:** Medium (2–3 sessions)

---

## Goal

Add a reporting view that aggregates customers by **workload** (from milestones) and by **engagement**, so sellers can quickly answer "which accounts am I working on AVD?" or "show me all my Migration engagements."

## The Problem

When management or account teams ask "which customers are good candidates for an AVD workshop?", sellers currently have to reverse-search notes and emails manually. There's no way to see accounts grouped by workload or engagement type.

## Current State

- **Milestone.workload** field exists (`String(200)`) — populated from MSX sync (e.g., "Azure Virtual Desktop", "Azure VMware Solution", "SQL Migration")
- **Analytics page** (`templates/analytics.html`, route at `main.py:324`) shows call frequency, topics, sellers — **no workload or engagement analytics**
- Engagements link to milestones (M2M `engagements_milestones`), opportunities (M2M), and customers (FK)
- Milestones have `dollar_value`, `due_date`, `msx_status`, `monthly_usage` — rich data for aggregation

## Data Model for Reporting

Two complementary views:

### View A: Workload Aggregation (from Milestones)
```
Milestone.workload → group by workload name
  → for each workload: list of customers (via Milestone.customer_id)
  → per customer: milestone count, total $ value, upcoming due dates
```

### View B: Engagement Aggregation (from Engagements)
```
Engagement.title patterns or linked milestone workloads
  → group by workload derived from milestones linked to engagement
  → for each group: list of customers with engagement details
  → per customer: engagement status, estimated ACR, target date
```

## Implementation Plan

### Step 1 — New route and template for workload report

**File:** `app/routes/main.py` (add to existing analytics section)

Create `GET /analytics/workloads` route:

```python
@main_bp.route('/analytics/workloads')
def workload_report():
    # Query all milestones that have a workload, grouped by workload
    workloads = (
        db.session.query(
            Milestone.workload,
            func.count(func.distinct(Milestone.customer_id)).label('customer_count'),
            func.count(Milestone.id).label('milestone_count'),
            func.sum(Milestone.dollar_value).label('total_value'),
        )
        .filter(Milestone.workload.isnot(None), Milestone.workload != '')
        .group_by(Milestone.workload)
        .order_by(func.count(func.distinct(Milestone.customer_id)).desc())
        .all()
    )
    return render_template('workload_report.html', workloads=workloads)
```

### Step 2 — Expandable workload cards

**File:** `templates/workload_report.html`

Layout per the issue request:
```
┌─────────────────────────────────────────────────┐
│ Migrations (5 Accounts)              ▼ $250K    │
├─────────────────────────────────────────────────┤
│  Acme Corp       $80K   On Track    Due Apr 15  │
│  Globex Inc      $60K   At Risk     Due Mar 30  │
│  Initech LLC     $45K   On Track    Due May 01  │
│  ...                                            │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ AVD (3 Accounts)                     ▼ $120K    │
├─────────────────────────────────────────────────┤
│ (collapsed by default, click to expand)         │
└─────────────────────────────────────────────────┘
```

Each workload row is a Bootstrap accordion/collapse card:
- **Header:** Workload name, account count badge, total dollar value
- **Expanded view:** Table of customers with columns:
  - Customer name (linked to customer view)
  - Milestone count
  - Total ACR / dollar value
  - Milestone status (worst status shown)
  - Nearest due date
  - Seller name
- Sorted by dollar value descending (default), with client-side re-sort options

### Step 3 — AJAX drill-down API

Create `GET /api/analytics/workload/<workload_name>` that returns the customer detail list for a specific workload. This avoids loading all detail data upfront.

```json
{
  "workload": "Azure Virtual Desktop",
  "customers": [
    {
      "id": 5,
      "name": "Acme Corp",
      "seller_name": "Alice Smith",
      "milestones": [
        {
          "title": "AVD POC",
          "dollar_value": 80000,
          "due_date": "2026-04-15",
          "msx_status": "On Track"
        }
      ],
      "total_value": 80000,
      "engagement_title": "AVD Migration Project"
    }
  ]
}
```

### Step 4 — Engagement grouping view (secondary tab)

On the same page, add a tab to switch between "By Workload" and "By Engagement":

**By Engagement view:**
- Group active engagements by a workload tag (derived from linked milestones' workload field, or from the engagement title as fallback)
- Show: Engagement title, customer, status, estimated ACR, target date
- Same expandable card pattern

### Step 5 — Link from analytics page

Add a "Workload Report" card/button on the existing analytics page that links to `/analytics/workloads`.

Also add to the nav bar under the existing analytics dropdown if one exists, or as a sibling link.

## Files Changed

| File | Change |
|------|--------|
| `app/routes/main.py` | New `/analytics/workloads` route + `/api/analytics/workload/<name>` API |
| `templates/workload_report.html` | **New** — workload/engagement report page |
| `templates/analytics.html` | Add link card to workload report |
| `templates/base.html` | Add nav link (if nav changes needed) |

## Testing

- Test workload aggregation query with multiple customers sharing a workload
- Test empty state (no milestones with workloads)
- Test API drill-down returns correct customers
- Test engagement grouping derives workload from linked milestones

## Open Questions

1. **How should "workload" be normalized?** MSX workload names can vary (e.g., "Azure Virtual Desktop" vs "AVD"). Should we offer a mapping/alias system, or just display raw MSX values? **Leaning toward:** Raw values initially, add normalization later if it's messy.
2. **Should this be a sub-page of analytics or its own top-level page?** **Leaning toward:** Sub-page of analytics (`/analytics/workloads`) to keep nav clean.
3. **Filter by team?** Should there be a seller/territory filter? **Leaning toward:** Yes, a simple dropdown filter at the top. Most useful for managers viewing team data.
