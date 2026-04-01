# Favorites Feature - Phased Implementation Plan
**Issue:** #86 | **Design doc:** `favorites-feature.md`

---

## Phase 1 - Foundation: Model + API
**Goal:** `Favorite` table exists, all three toggle endpoints work, nothing visible in the UI yet.

### Steps
1. Add `Favorite` model to `app/models.py` (`favorites` table, `object_type`/`object_id` unique constraint, `is_favorited()` static helper)
2. `db.create_all()` in `app/__init__.py` already handles new tables - no migration code needed, but verify it runs
3. Add `POST /api/milestone/<int:id>/favorite` toggle to `app/routes/milestones.py`
4. Add `POST /api/engagement/<int:id>/favorite` toggle to `app/routes/engagements.py`
5. Create `app/routes/opportunities.py` with `POST /api/opportunity/<int:id>/favorite` toggle + register blueprint in `app/__init__.py`

### Validate
- Hit `POST /api/milestone/<id>/favorite` twice - first returns `is_favorited: true`, second returns `is_favorited: false`
- Confirm a row was inserted/deleted in the `favorites` table via the admin panel or SQLite browser
- Same for engagement and opportunity

---

## Phase 2 - Milestone Tracker
**Goal:** Star icon in every tracker row, favorites-only filter toggle in the filter bar, state persisted.

### Steps
1. In `milestone_tracker()` route, pre-fetch `favorited_ms_ids` set from `Favorite` table and pass to template
2. Add `data-favorited` attribute to the milestone `<tr>` in `milestone_tracker_content.html`
3. Add `<th>` star header column (far right, before MSX)
4. Add `<td>` star button cell per row (inline toggle, no reload)
5. Add `favoritesToggle` button to filter bar
6. Wire up JS: `toggleMilestoneFavorite()` function, update `applyFilters()`, `saveFilters()`, `restoreFilters()`, `isFiltersDefault()`, `resetFilters()`

### Validate
- Click a star - icon fills yellow, no page reload
- Click again - unfills
- Click favorites toggle - only starred rows visible
- Refresh page - favorites toggle state restores from localStorage
- Reset filters button clears favorites toggle too

---

## Phase 3 - Engagements Hub + Engagement View
**Goal:** Favorited engagements sort to top in the hub, star in each card heading, "Favorites Only" toggle, star on engagement detail page.

### Steps
1. Add `is_favorited` to the `/api/engagements/all` response dict
2. Sort engagements in `buildCard()` rendering so favorited cards render first (sort in JS before card loop)
3. Add star button to engagement card heading in `engagements_panel.html`
4. Wire up `toggleEngagementFavorite()` JS function
5. Add "Favorites Only" toggle button in the hub header - hides non-favorited cards
6. Add star button to `engagement_view.html` detail page header (calls same API)
7. Persist hub favorites-only toggle state in localStorage

### Validate
- Star an engagement - it sorts to the top of the hub
- "Favorites Only" toggle hides non-starred cards
- Unstar while in favorites-only mode - card disappears immediately
- Star button on detail page works and reflects in the hub on return

---

## Phase 4 - Opportunities
**Goal:** Star on opportunity detail page, star inline in engagement_view opp list, favorited items sort first on customer page.

### Steps
1. Pass `is_favorited` flag to `opportunity_view.html` via the route
2. Add star button to opportunity detail page header
3. Wire up `toggleOppFavorite()` on the detail page
4. Add star icon to each opportunity row in `engagement_view.html`'s opportunities section
5. On `customer_view.html`, pre-fetch favorited IDs and sort opportunities + milestones so favorited items appear first in their respective cards; add inline star icons

### Validate
- Star on opportunity detail page works inline
- Same opportunity starred when viewed inside engagement_view
- Customer page shows favorited opps at the top of the opportunities card
- Customer page shows favorited milestones at the top of the milestones card

---

## Overall Order
`Phase 1` → validate API → `Phase 2` → validate tracker → `Phase 3` → validate hub → `Phase 4` → validate all → commit, merge, close #86
