# Navbar Redesign: Intent-Based Navigation

## Problem
The current navbar exposes database entities (Customers, Notes, Partners, Engagements, Milestones, Revenue, Reports). Real usage patterns tell a different story - most work starts from the Dashboard or Seller pages, not from entity lists.

## Actual User Mental Model (from usage patterns)

| Intent | Where I Actually Go | Navbar Needed? |
|--------|---------------------|----------------|
| "What do I need to do today?" | **Home** - action items card | Yes (logo click) |
| "How is [seller] doing?" | **Seller page** - via sellers navbar item | **Yes - Sellers is top-level for SEs** |
| "What milestones need attention?" | **Home** - milestones card | No - it's on home |
| "How is revenue trending?" | **Home** - revenue alerts card | No - it's on home |
| "Check on a customer?" | **Seller page** > customers card, or Ctrl+K | Ctrl+K is customer lookup, not seller |
| "Find an engagement?" | **Home** - engagements tab | No - it's on home |
| "Prep for my 1:1" | **Reports** > 1:1 report | Yes |
| "New note" | **New Note button** from anywhere | Yes - persistent action button |

## Key Insight: The Dashboard + Seller Pages Are the Two Hubs

Almost everything routes through the dashboard or a seller page. The entity list pages (Customers, Notes, Partners) are lookup/browsing tools, not daily workflows.

## Role-Based Top-Level Nav

- **SE mode:** Sellers is top-level (the primary way SEs navigate to work)
- **DSS mode:** Customers is top-level (DSSs ARE the seller, they think in customers)
- Ctrl+K is customer search, not seller search

## Sellers Page Redesign

Current sellers page has too much per card (new customer button, pod tag). Redesign:
- **Remove:** New Customer button (exists on individual seller page if needed), Pod tag
- **Keep:** View as seller button, territory, growth/acquisition indicator, name, email
- **New:** Each seller is a compact 1-line header row, expandable to show their customers
- **Add:** Expand/Collapse All button
- **Effect:** This becomes the Customers list for SEs (customers grouped by seller), eliminating the need for a separate Customers navbar item

## Seller Page Customer Card Enhancement
- Add sort options: by name, by last note date
- This is how SEs find customers (from the seller page, not from a flat customer list)

## Reclassifying Current Navbar Items

### Top-Level (role-dependent)
- **Sellers** (SE mode) / **Customers** (DSS mode)
- **Reports** - meeting prep, analysis, all structured views

### Moves to Reports
- **Milestone Tracker** - report-like (discovery, time filtering, alignment checks). Daily-use milestone cards already on home + seller pages. Milestones have no manual import step so this is clean.
- **Revenue Analyzer** - trend analysis, drilldowns, manager review. Daily-use revenue alerts already on home + seller pages. Import decoupled to its own page.

### Browse / Collapsed Dropdown
- Customers (SE mode) / Sellers (DSS mode)
- Notes (call history)
- Partners
- Engagements & Projects hub
- Topics, Territories, Pods

### Persistent Action Button
- **+ New Note** - visible from every page in the navbar

## The Revenue Solution (3/28/2026)

Revenue import and revenue analysis are **decoupled**:

### Revenue Analyzer -> Reports
The revenue dashboard/alerts page moves to Reports alongside Milestone Tracker. It's a reporting/analysis tool - trend charts, drilldowns, manager review prep. Daily-use revenue alerts already show on home page and seller pages.

### Revenue Import Stays Its Own Page
The import flow has specific multi-step instructions (go to MSXi report, configure fields, export, convert to CSV). This needs its own page with those instructions, the upload button, bucket picker, and import history. It's a user action, not an admin action.

**Where it lives:** TBD - could be Browse dropdown, admin panel, or just linked from the monthly reminder. The reminder banner makes the import page discoverable regardless of where it sits in the nav.

### Monthly Revenue Import Reminder (New Feature)
- User setting (default on): revenue import reminders
- Check: does the database contain data for the most recently completed month? If not, show reminder.
- Revenue data is month-granularity. A month's data isn't valid until finalized (month is over). Importing on the 1st or the 31st gets the same result.
- "Last month's revenue data has been finalized. Import your latest CSV to keep trends accurate."
- Links: [Import Now] (goes to import page) | [Dismiss] (hides until next month)
- This solves the "how do users know to import?" problem without needing revenue import in the navbar

## Proposed Navbar (Updated 3/28/2026)

### SE Mode
```
[Logo/Home]  Sellers  Reports v  [Browse v]  [Ctrl+K]  [+ New Note]
```

### DSS Mode
```
[Logo/Home]  Customers  Reports v  [Browse v]  [Ctrl+K]  [+ New Note]  [Seller: Martinez v]
```

### Reports Dropdown
- 1:1 Manager / SE Report
- Revenue Analyzer (moved from top-level)
- Milestone Tracker (moved from top-level)
- Workload Report
- Hygiene Report
- New Synapse Users

### Browse Dropdown
- Customers (SE) / Sellers (DSS)
- Notes (Call History)
- Engagements & Projects
- Partners
- Revenue Import
- Topics
- Territories / Pods

## Decisions (3/27-28/2026)

1. **Search:** Ctrl+K modal stays. It's customer search, not seller search. Maybe make trigger more visible.
2. **"New Note"** button in navbar. Already offers general note as an option.
3. **Milestone Tracker -> Reports:** Confirmed. No manual import, purely viewing/reporting.
4. **Revenue Analyzer -> Reports:** Confirmed. Decoupled from import. Daily alerts already on home/seller pages.
5. **Revenue Import stays its own page.** Has step-by-step instructions that need space. Not an admin action.
6. **Monthly revenue reminder:** New feature. Banner when DB is missing data for the most recently completed month. Links to import page. User setting to disable.
7. **Projects:** Need a dashboard tab (like the engagements tab on home page).
8. **Engagements Hub:** Tuck into Browse once projects have a dashboard tab.
9. **Sellers is CRITICAL for SEs.** Top-level, non-negotiable.
10. **SE: Sellers top-level, DSS: Customers top-level.** Role-dependent primary nav item.
11. **Sellers page redesign:** Compact 1-line rows, expandable to show customers. Remove new customer button and pod tag. Keep view-as-seller, territory, growth/acquisition, name, email. Expand/collapse all. Effectively becomes the grouped customers list.
12. **Seller page customer card:** Add sort by name / last note date.

## Still Needs Thought

- Whether Reports hub page still exists or if the navbar dropdown IS the hub
- Where exactly revenue import page lives in nav (Browse? Its own thing? Just linked from reminder?)
- Fill My Day page - where does this live?

## Implementation Phases

### Phase 1: Sellers Page Redesign ✅ (3/28/2026)
Prereq for removing Customers from navbar. Compact the sellers list so each seller is a 1-line row that expands to show their customers inline.
- [x] Redesign sellers list: 2-line rows (name + email/territories), grouped by Growth/Acquisition
- [x] Expandable customer list under each seller (with favicon, last note date)
- [x] Expand/Collapse All button
- [x] Remove New Customer button from seller cards (exists on individual seller page)
- [x] Remove Pod tag from seller cards
- [x] Keep View as Seller button
- [x] Move Customers to More dropdown for SE mode
- [x] Move Notes and Engagements to More dropdown (both modes)
- [x] Fix seller_view Details card flex-shrink

### Phase 2: Seller Page Customer Card Sort ✅ (3/28/2026)
- [x] Add sort options on seller view page: by name, by last note date
- [x] Persist sort preference (localStorage)

### Phase 3: Projects Tab on Dashboard ✅ (3/28/2026)
- [x] Add a Projects tab alongside the existing Engagements tab on the home page
- [x] Show active projects with status, type, open tasks, due date
- [x] Lazy-load via /api/projects/active API
- [x] Tab persistence in localStorage

### Phase 4: Move Milestone Tracker + Revenue Analyzer to Reports ✅ (3/28/2026)
- [x] Add Milestone Tracker to Reports hub (Data Hygiene group, first item)
- [x] Add Revenue Analyzer to Reports hub (Revenue Analysis group, first item)
- [x] Existing URLs unchanged (no broken bookmarks)

### Phase 5: Navbar Restructure ✅ (3/28/2026)
The main event. Rearrange the navbar to match intent-based navigation.
- [x] SE mode: Sellers top-level (with dropdown) | DSS mode: Customers top-level
- [x] Reports dropdown: All Reports, 1:1, Revenue Analyzer, Milestone Tracker, Workload, Hygiene, New Synapse Users
- [x] Browse dropdown: Customers/Sellers (opposite of top-level), Notes, Engagements & Projects, Partners, Revenue Import, Topics, Territories, PODs, Search, Analytics, Connect Export, Solution Engineers
- [x] Remove Partners, Milestones, Revenue from top-level nav
- [x] Rename More to Browse with restructured contents
- [x] Update active nav highlighting for new dropdown structure

### Phase 6: Monthly Revenue Import Reminder ✅ (3/28/2026)
- [x] User setting: revenue_import_reminder (default on) with migration
- [x] Check if DB has data for the most recently completed month
- [x] Banner on dashboard: "Last month's revenue data has been finalized. Import your latest CSV to keep trends accurate."
- [x] Links: [Import Now] goes to import page | [X] dismisses
- [x] Dismiss state persists in localStorage until next month (keyed by year-month)

### Phase 7: Revenue Refactor ✅ (3/28/2026)
Revenue is now a report, not a core feature. Refactor URLs and UX to match.

- [x] Move revenue import route to `/import/revenue` (old `/revenue/import` still works)
- [x] Remove Revenue Analyzer breadcrumb from import page
- [x] Fix post-import button: "Go Home" links to `/` instead of Revenue Analyzer
- [x] Products at top-level: `/products` and `/product/<name>` (old URLs still work)
- [x] Customer revenue at `/customer/<id>/revenue` (old URL still work)
- [x] Customer bucket products at `/customer/<id>/revenue/bucket/<bucket>` (old URL still works)
- [x] Remove `target="_blank"` from Revenue Analyzer customer links (opens in same tab)
- [x] Rename "View in Sales Buddy" to "View Customer Hub" / "View Seller Hub" across all revenue templates

## Implementation Notes

- The navbar is in `templates/base.html` around lines 340-400
- Seller dropdown already exists in the top-right
- Bootstrap 5 navbar with `navbar-nav` items
- Mobile responsive - dropdowns work on mobile via Bootstrap collapse
- Need to keep PWA title stripping logic working
- Mode badge (SE/DSS) positioning needs to work with new layout
- Sellers page redesign is a prereq for removing Customers from navbar
