# FY Archive Explorer

## Problem

The FY transition flow in `app/services/fy_cutover.py` creates archive snapshots (`FY25.db`, `FY26.db`, etc.) during fiscal year cutover, and `list_archives()` returns them to the admin panel UI. However, the archive list in the "Previous Year Archives" section of `templates/partials/_fy_management_card.html` is display-only — it shows the label, size, and date but provides no way to interact with the archives. There is no way to browse what's inside an archive without manually opening the `.db` file.

## Feature

Add a read-only **Archive Explorer** to the Fiscal Year Management card on the Admin Panel. Each archive in the "Previous Year Archives" list gets a **Browse** button. Clicking it opens a modal with two panels:

- **Left panel:** A searchable navigation tree
- **Right panel:** A detail panel that shows content for whatever is selected in the tree

The archive `.db` is opened **read-only** (SQLite URI `?mode=ro`) via a temporary SQLAlchemy engine. No writes, no risk to live or archive data.

## Navigation Tree

The tree has one drill-down path: **Seller → Customer → Category → Individual Item**

```
📊 Summary
👤 Seller Name (N customers)
├── 👥 Customer Name (TPID)
│   ├── 📝 Notes (count)
│   │   ├── Note title — date
│   │   └── Note title — date
│   ├── 🤝 Engagements (count)
│   │   ├── Engagement name
│   │   └── Engagement name
│   └── 🏔️ Milestones (count)
│       ├── Milestone name
│       └── Milestone name
📋 Unassigned (N customers)
├── 👥 Customer with no seller
│   └── ...
```

- **Summary** is a fixed node at the top (no children).
- **Sellers** are listed alphabetically with customer counts.
- **"Unassigned"** is a pseudo-seller at the bottom for customers with no `seller_id` (manually created, no MSX data).
- Every expand/collapse is lazy in the tree — sellers expand to customers, customers expand to category nodes (Notes/Engagements/Milestones), categories expand to individual items.

## Every Node Is Clickable

Every node in the tree is both expandable (if it has children) AND clickable to show something in the detail panel:

| Click on... | Tree behavior | Detail panel shows |
|---|---|---|
| **Summary** | No children | Archive stats: total sellers, customers, notes, engagements, milestones, territories, opportunities |
| **Seller** | Expands to show their customers | Seller card with name, alias, and a **clickable list of their customers** |
| **Customer** | Expands to show Notes/Engagements/Milestones category nodes | Customer card: name, TPID, territory, verticals, seller, counts for notes/engagements/milestones |
| **"Notes (12)"** category | Expands to list individual notes | Clickable list of the customer's notes with dates and previews |
| **"Engagements (3)"** category | Expands to list individual engagements | Clickable list of the customer's engagements |
| **"Milestones (5)"** category | Expands to list individual milestones | Clickable list of the customer's milestones |
| **Individual Note** | Leaf node | Full note: date, attendees, topics, partners, body content |
| **Individual Engagement** | Leaf node | Engagement name, status, linked notes (clickable) |
| **Individual Milestone** | Leaf node | Milestone name, status, tasks list, linked notes (clickable) |

## Detail Panel Links Drive Tree Navigation

When the detail panel shows clickable items (e.g., a seller's customer list, or an engagement's linked notes), clicking one should:

1. Expand that item's parent nodes in the tree
2. Select and highlight the item in the tree
3. Update the detail panel to show that item's content

This gives two navigation patterns:
- **Browsing:** expand nodes in the tree, click deeper
- **Jumping:** click links in the detail panel to navigate directly

## Search

A search input at the top of the tree panel provides **instant client-side filtering**. The tree endpoint should return enough metadata (seller names, customer names, note titles, engagement names, milestone names) for the frontend to filter without additional API calls.

| User types... | Behavior |
|---|---|
| A customer name | Auto-expands the parent seller, shows matching customers, hides non-matches |
| An engagement or note name | Expands seller → customer → shows the matching items |
| A seller name | Highlights matching sellers |
| Clears the field | Resets tree to default collapsed state |

## API Endpoints

Add to `app/routes/admin.py`, all under the existing FY cutover section:

1. **`GET /api/admin/fy/archive/<label>/tree`** — Returns the full tree skeleton in one call: summary stats, sellers with their customers, and per-customer lists of note titles/engagement names/milestone names (for search + tree rendering). This is the only call made when the modal opens.

2. **`GET /api/admin/fy/archive/<label>/customer/<id>`** — Returns full customer detail: notes (with topics, body), engagements (with linked notes), milestones (with tasks and linked notes). Called when a customer node is clicked/expanded.

3. **`GET /api/admin/fy/archive/<label>/detail/<type>/<id>`** — Returns a single note, engagement, or milestone detail. `type` is one of `note`, `engagement`, `milestone`. Called when a leaf node is clicked.

All endpoints should use a read-only `open_archive()` context manager added to `app/services/fy_cutover.py` that opens the archive `.db` with SQLite's `?mode=ro` URI flag.

## Frontend

### Files to modify

- **`templates/partials/_fy_management_card.html`** — Update the archive list in `fyArchivesList` to include a "Browse" button per archive row.
- **`templates/admin_panel.html`** — Add the archive explorer modal HTML and all the JavaScript for tree rendering, search filtering, detail panel rendering, and cross-panel navigation. Keep it in the existing `<script>` block pattern used by the rest of the admin panel. Also include the modal partial.

### Modal structure

- Full-width (`modal-xl`), ~80vh height, `data-bs-backdrop="static"` to prevent accidental close
- Left panel: fixed ~320px width, search input at top, scrollable tree below
- Right panel: flex-grow, scrollable, shows detail content

### Key behaviors

- Tree nodes use chevron icons that rotate on expand/collapse
- Active/selected node gets a highlight style
- Detail panel shows a loading spinner while fetching
- `.archive-node:hover` gets a subtle background highlight

## Integration Checklist

- [ ] `open_archive()` context manager in `app/services/fy_cutover.py`
- [ ] Archive query functions in `app/services/fy_cutover.py` (summary, tree, customer, detail)
- [ ] 3 API routes in `app/routes/admin.py`
- [ ] Archive explorer modal HTML (can be a partial or inline in admin_panel.html)
- [ ] Browse button added to archive list rows
- [ ] Tree rendering JS with expand/collapse
- [ ] Search/filter JS (client-side on cached tree data)
- [ ] Detail panel renderers for each node type
- [ ] Cross-panel navigation (detail panel links expand tree + update selection)
- [ ] Tests for the new API endpoints