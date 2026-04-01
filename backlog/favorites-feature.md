# Feature Plan: Favorites for Milestones, Engagements, and Opportunities
**GitHub Issue:** #86

## Overview

Add a "favorite" flag to Milestones, Engagements, and Opportunities. A star icon on each item lets sellers and SEs bookmark their most important items regardless of ACR. Each object type gets a way to surface favorites first or filter to favorites only.

---

## Scope

| Object | Where the star appears | Favorites behavior |
|---|---|---|
| Milestone | Tracker row (far right, before MSX link) | Filter toggle in tracker filter bar |
| Engagement | Hub card heading + `engagement_view.html` | Favorited cards sort to top; toggle in hub to show only favorited |
| Opportunity | `opportunity_view.html` header + inline in `engagement_view` opp list + customer page opp/milestone cards | Sorts to top in customer page cards |

---

## Data Model Changes

### New `Favorite` model (`app/models.py`)

A single polymorphic favorites table. No columns added to existing models.

```python
class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    object_type = db.Column(db.String(50), nullable=False)   # 'milestone', 'engagement', 'opportunity'
    object_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    __table_args__ = (
        db.UniqueConstraint('object_type', 'object_id', name='uq_favorite'),
    )

    @staticmethod
    def is_favorited(object_type, object_id):
        return Favorite.query.filter_by(object_type=object_type, object_id=object_id).first() is not None
```

### Migration (`app/migrations.py`)

`db.create_all()` creates the new `favorites` table automatically on first run - no `_add_column_if_not_exists` needed since it's a brand new table.

---

## API Endpoints

Three POST toggle endpoints that add/remove a row from the `favorites` table.

**Milestone** (`app/routes/milestones.py`):
```
POST /api/milestone/<int:id>/favorite
Response: { "success": true, "is_favorited": true/false }
```

**Engagement** (`app/routes/engagements.py`):
```
POST /api/engagement/<int:id>/favorite
Response: { "success": true, "is_favorited": true/false }
```

**Opportunity** - see question #5 below:
```
POST /api/opportunity/<int:id>/favorite
Response: { "success": true, "is_favorited": true/false }
```

Each toggle checks `Favorite.query.filter_by(object_type=..., object_id=id).first()` - if it exists, delete it; if not, create it. No columns touched on the source models.

---

## UI Changes

### 1. Milestone Tracker (`templates/partials/milestone_tracker_content.html`)

**Star column** - far right before the MSX link column. New `<th>` in the header and matching `<td>` per row.

**Table header:**
```html
<th class="text-center" title="Favorited"><i class="bi bi-star"></i></th>
```

**Table row `<tr>`** - add data attribute (resolved at render time via a set passed from the route):
```html
data-favorited="{{ 'true' if ms.id in favorited_milestone_ids else 'false' }}"
```
The route pre-fetches all favorited milestone IDs for the current view into a set: `favorited_milestone_ids = {f.object_id for f in Favorite.query.filter_by(object_type='milestone')}`. This avoids N+1 queries.

**Table row `<td>`** - new cell with inline toggle (no page reload):
```html
<td class="text-center" onclick="event.stopPropagation();">
    <button class="btn btn-sm py-0 px-1 favorite-btn"
            data-milestone-id="{{ ms.id }}"
            title="{{ 'Unfavorite' if ms.id in favorited_milestone_ids else 'Favorite' }}">
        <i class="bi {{ 'bi-star-fill text-warning' if ms.id in favorited_milestone_ids else 'bi-star text-muted' }}"></i>
    </button>
</td>
```

**Filter bar** - add a "Favorites" toggle to the existing filter row, either as a new dropdown option on the `myTeamFilter` select or as a standalone checkbox button. Likely cleanest as a separate compact toggle button:
```html
<div class="col-md-auto">
    <button type="button" class="btn btn-sm btn-outline-secondary" data-role="favoritesToggle" title="Show favorites only">
        <i class="bi bi-star"></i>
    </button>
</div>
```
When active it turns `btn-warning` and filters to `data-favorited="true"` rows only.

**JS** - `applyFilters()` gains a new condition:
```js
var showFavoritesOnly = favoritesToggle.classList.contains('btn-warning');
if (showFavoritesOnly && row.dataset.favorited !== 'true') visible = false;
```

A `toggleFavorite(btn, id)` function calls the API, then flips the icon and `data-favorited` in-place (no reload).

---

### 2. Engagements Hub (`templates/partials/engagements_panel.html`)

The hub renders engagement cards entirely via JavaScript (`buildCard()`). Changes needed:

- **`/api/engagements/all` response** - add `is_favorited` to each engagement dict
- **Sort order** - favorited engagements bubble to the top of the list before any other sort criteria
- **`buildCard(eng)`** - add a star button in the card heading area (visible without expanding):
```js
`<button class="btn btn-sm py-0 px-1 border-0 favorite-btn" 
         data-engagement-id="${eng.id}"
         onclick="event.stopPropagation(); toggleEngagementFavorite(this, ${eng.id});"
         title="${eng.is_favorited ? 'Unfavorite' : 'Favorite'}">
    <i class="bi ${eng.is_favorited ? 'bi-star-fill text-warning' : 'bi-star text-muted'}"></i>
</button>`
```
- **Filter toggle in the hub header** - a compact "Favorites Only" toggle button. When active, hides all non-favorited cards. Toggling a card's star while in favorites-only mode removes it from view immediately (matches the behavior of team-filter in the tracker).
- **`engagement_view.html`** - star button also appears in the engagement detail page header, same as opportunity_view.

---

### 3. Opportunity View (`templates/opportunity_view.html`)

The opportunity detail page has a button group in the header. The route passes `is_favorited = Favorite.is_favorited('opportunity', opp.id)`:
```html
<button class="btn btn-outline-secondary"
        id="oppFavoriteBtn"
        onclick="toggleOppFavorite({{ opp.id }})">
    <i class="bi {{ 'bi-star-fill text-warning' if is_favorited else 'bi-star' }}"></i>
    {{ 'Favorited' if is_favorited else 'Favorite' }}
</button>
```
JS toggles the icon and text in place.

### 4. Opportunity inline in `engagement_view.html`

The opportunities sub-list on an engagement detail page gets a star icon per row, same toggle mechanic as above (calls the same API). No filtering needed here.

### 5. Customer page opportunity/milestone cards

On the customer view, opportunity and milestone cards are sorted so favorited items appear first. An empty `bi-star` icon on each card lets you toggle favorite inline. No "show only favorites" filter needed here - just the sort order change is sufficient.

---

## Route Files

- Milestone toggle: `app/routes/milestones.py`
- Engagement toggle: `app/routes/engagements.py`
- Opportunity toggle: new `app/routes/opportunities.py` blueprint (registered in `app/__init__.py`)

---

## Persistence & Filter State

Milestone favorites toggle state persisted to `localStorage` alongside existing filter state (`favoritesOnly: bool` in the `FILTER_KEY` object).

Engagements hub "favorites only" toggle state persisted similarly in the hub's own localStorage key.
