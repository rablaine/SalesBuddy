# DSS (Seller) Mode for Sales Buddy

## Status: Spec (Final Draft)

**Date:** March 18, 2026
**Last Updated:** March 19, 2026

**Context:** Sales Buddy is currently designed for Solution Engineers (SEs) who manage multiple sellers and need to see all their accounts organized by seller. Digital Sales Specialists (DSSs/sellers) have different needs - they ARE the seller, so seller-centric navigation and grouping is redundant for them.

---

## Architecture: Unified Seller Mode

The core concept is a **unified "seller mode"** that works for both true sellers (DSSs) and SEs viewing a single seller's perspective. Both use the same rendering logic - the only difference is permanence.

### Three App States

| User Role | Seller Mode State | Behavior |
|-----------|-------------------|----------|
| **DSS** | Always `(active, "me")` | Permanent seller mode. "me" resolves to their own Seller record via `my_seller_id`. Cannot exit. No banner. |
| **SE** (normal) | `(inactive, null)` | Full SE view with all sellers, territories, grouping, filters, etc. Current behavior. |
| **SE** (viewing seller) | `(active, seller_id)` | Temporarily collapsed to a single seller's view. Banner at top shows who + exit button. |

### Data Model Changes

**New columns on `UserPreference`:**

| Column | Type | Purpose |
|--------|------|---------|
| `user_role` | `String(10)`, nullable | `'se'` or `'dss'`. Null until set during onboarding. **Permanent once set - cannot be changed after onboarding.** |
| `my_seller_id` | `Integer`, FK to `sellers.id`, nullable | The Seller record that represents "me" for a DSS. Set automatically during account sync by matching the `az login` identity alias to a Seller alias. Also persisted so the app works if the user later opens the app without an active `az login` session. |
| `my_seller_alias` | `String(100)`, nullable | The alias from the `az login` identity, stored at onboarding/sync time. Used to resolve `my_seller_id` after account sync, and as a fallback identifier if the Seller record doesn't exist yet. |

**Session state (SE seller mode only):**

| Key | Type | Purpose |
|-----|------|---------|
| `seller_mode_seller_id` | `int` or `None` | Set when an SE activates seller mode for a specific seller. Cleared on exit. |

### Context Processor

Every request, the context processor injects these into all templates:

```python
{
    'user_role': 'se' | 'dss',           # Never null in template context (null defaults to 'se')
    'seller_mode': True | False,          # True if DSS, or if SE has active seller mode
    'seller_mode_seller': Seller | None,  # The active seller object (DSS's "me" or SE's chosen seller)
}
```

**Resolution logic:**
1. Read `pref.user_role` from DB. If null, treat as `'se'` for rendering purposes.
2. If `user_role == 'dss'` and `my_seller_id` is set: `seller_mode=True`, `seller_mode_seller=Seller(my_seller_id)`
3. If `user_role == 'se'` and `session['seller_mode_seller_id']` is set: `seller_mode=True`, `seller_mode_seller=Seller(session value)`
4. Otherwise: `seller_mode=False`, `seller_mode_seller=None`

**No data migration needed for existing users.** The DB column is nullable and null defaults to SE rendering in the context processor. This is safe because:
- **Existing users** (you, testers) already completed onboarding with SE behavior - null renders identically to explicit `'se'`
- **New users** see the wizard modal (`first_run_modal_dismissed=False`) which blocks interaction until they pick a role in Step 1. The null-to-SE default only affects rendering behind the modal, which they can't see or interact with.
- The DB value stays null until explicitly set, so `pref.user_role IS NULL` still means "hasn't chosen yet" if you ever need to distinguish that from "chose SE"

### Server-Side Query Scoping

When `seller_mode=True`, all data-fetching routes **filter server-side** by the active seller:

- `/notes` - only notes for customers belonging to the active seller
- `/customers` - only customers with `seller_id == active_seller.id`
- `/engagements` - only engagements for the active seller's customers
- `/search` - results scoped to the active seller's customers
- `/revenue/dashboard` - revenue data scoped to the active seller
- `/` (index) - revenue alerts and recent notes scoped to the active seller

This means when an SE enters seller mode, they temporarily see **only** that seller's data - useful for 1:1 syncs where the SE wants to see exactly what the DSS sees.

---

## Onboarding: Setup Wizard Changes

### Step 1 Redesign - Role Selection (Primary) + Theme (Secondary)

Currently Step 1 is just the dark mode toggle. The new design makes **role selection the primary, prominent focus** with theme as a small secondary element.

**New layout for Step 1:**

```
+----------------------------------------------------------+
|  Let's get you set up!                                    |
|                                                           |
|  What's your role?                                        |
|                                                           |
|  +----------------------------------------------------+  |
|  |  [person icon]  Digital Sales Specialist (DSS)      |  |
|  |  I manage my own accounts directly                  |  |
|  +----------------------------------------------------+  |
|                                                           |
|  +----------------------------------------------------+  |
|  |  [people icon]  Solution Engineer (SE)              |  |
|  |  I support multiple sellers and their accounts      |  |
|  +----------------------------------------------------+  |
|                                                           |
+----------------------------------------------------------+
|                                                           |
|  Choose Your Theme            [sun] Light [===] Dark      |
+----------------------------------------------------------+
```

**Behavior:**
- Two clickable cards, radio-style selection (one at a time, with a highlighted border on selection)
- **No default** - neither card is pre-selected
- **"Next" button is disabled** until a role is selected
- Theme toggle sits at the bottom, small and secondary - defaults to current dark mode preference
- Role selection saved immediately via `POST /api/preferences/user-role`
- Role is **permanent** - once set, it cannot be changed in Settings or anywhere else

### Step 3: Account Sync

**No changes to the import mechanism.** The MSX account import already scopes to the authenticated user's account associations. DSSs automatically get only their accounts because that's all they're associated with in MSX.

**DSS identity capture:** During the account sync step, if `user_role == 'dss'`, the app:
1. Reads the `az login` identity alias (already available from the auth flow in Step 2)
2. Stores it in `UserPreference.my_seller_alias`
3. After account import completes and Seller records are created, matches `my_seller_alias` to a Seller's `alias` field
4. Sets `UserPreference.my_seller_id` to the matched Seller's ID
5. If no match found (edge case - DSS's alias doesn't match any imported seller), shows a warning and lets the user pick from a dropdown of imported sellers

### Revenue Export Instructions (Steps 3 tip, 4 tip, 5)

The revenue CSV export instructions already say the generic:
> "In Filters (right), find **ServiceCompGrouping** and select your buckets"

**No change needed** to instruction text - it's already role-agnostic.

### Revenue Bucket Generalization (Backend)

The hardcoded `buckets = ['Core DBs', 'Analytics', 'Modern DBs']` in `revenue_customer_view()` must be replaced with a dynamic query of actual imported buckets from `CustomerRevenueData` for the given customer. This ensures the revenue views work for any team's bucket selections, not just the data team's three buckets.

---

## SE Seller Mode - Entry and Exit

### Activation - "View as Seller" Buttons

Two locations (SE only, never shown to DSSs):

**1. Seller View Page** (`/seller/<id>`)
- Button in the header row, next to "New Customer" and "Edit Seller"
- Styled as a secondary/outline button: `[eye icon] View as Seller`
- Placement: to the left of "New Customer" in the header button group

**2. Sellers List Page** (`/sellers`)
- Button per seller row, to the right of "New Customer"
- Styled as a small secondary/outline button: `[eye icon] View as Seller`
- Placement: right-aligned in each seller's row, after the customer count

**Activation flow:**
```
Click "View as Seller"
  -> POST /api/seller-mode/activate/<seller_id>
  -> Sets session['seller_mode_seller_id'] = seller_id
  -> Redirects to / (home page, now scoped to that seller)
```

### SE Seller Mode Banner

When an SE is in seller mode, a persistent slim banner appears below the navbar on every page:

```
[eye icon] Viewing as: Amy Kingzett (Growth) - East.SMECC.HLA.0509    [X Exit Seller Mode]
```

- Styled as a slim `alert-info` bar, not dismissible (only exit button removes it)
- Shows seller name, type badge, and territory
- Exit button on the right
- **DSSs never see this banner** - their mode is permanent and implicit

### Deactivation

```
Click "Exit Seller Mode" (in banner)
  -> POST /api/seller-mode/deactivate
  -> Clears session['seller_mode_seller_id']
  -> Redirects to / (home page, back to full SE view)
```

---

## What Changes in Seller Mode

Everything below applies identically to DSS permanent mode AND SE temporary seller mode.

### Navigation

| Feature | SE Mode (current) | Seller Mode |
|---------|-------------------|-------------|
| Sellers nav menu | Visible | **Hidden** |
| Seller view page | Accessible | **Hidden/redirects** - the whole app IS the seller view |
| Home page (index) | All sellers' alerts + notes | **Scoped** to active seller only |
| Customers list | Grouped by seller, seller filters | **Flat list**, no seller grouping or filters, scoped to active seller |
| Search page | Seller dropdown filter | **No seller filter**, results scoped to active seller |
| Engagements hub | Shows seller column | **No seller column**, scoped to active seller |
| Revenue dashboard | Seller dropdown, all sellers | **No seller dropdown**, scoped to active seller |
| Notes list | Seller badges on notes | **No seller badges**, scoped to active seller |

### Customer Management

| SE Mode | Seller Mode |
|---------|-------------|
| Customer form has seller dropdown picker | **No seller dropdown** - seller auto-assigned to active seller |
| Customer form has territory dropdown | **Territory dropdown still shown** - pulls from all territories in DB. Auto-selects if only one territory exists. |
| Customer view shows seller badge | **No seller badge** |

### Note Management

| SE Mode | Seller Mode |
|---------|-------------|
| Note form shows "Seller: Name" | **No seller display** |
| Note form has milestone picker (search + AI match) | **Opportunity picker** instead (search + AI match) |
| Note view shows seller badge | **No seller badge** |
| Note view shows linked milestones | **Shows linked opportunities** instead |
| Notes list shows seller badges | **No seller badges** |

### Revenue

| SE Mode | Seller Mode |
|---------|-------------|
| Revenue dashboard has seller dropdown | **No seller dropdown**, auto-scoped |
| Revenue customer view shows all buckets | Same, but using **dynamic bucket query** instead of hardcoded list |

### What Stays the Same (Both Modes)

- Note creation/editing (Quill editor, topics, partners, engagements, tasks)
- Engagement management (tasks are role-agnostic)
- Milestone tracker (DSSs still care about milestones - they just don't attach them to notes)
- Revenue analyzer features (trend charts, analysis, alerts - just scoped differently)
- Customer view page content (notes, opportunities, milestones - all universal)
- Partner directory
- Pod management
- AI features (auto-fill, auto-tag, summarize)
- Draft system
- Territory management
- Solution engineer management (DSSs may still reference SEs assigned to their accounts)

---

## MSX Integration - Role-Based Behavior

MSX behavior is based on `user_role`, NOT on seller mode view state. An SE temporarily in seller mode still behaves as an SE for MSX purposes.

### Note-to-MSX Entity Attachment

The fundamental difference: **SEs attach notes to milestones, DSSs attach notes to opportunities.**

| Aspect | SE (current) | DSS (new) |
|--------|-------------|----------|
| Note form picker | Milestone search + AI match | **Opportunity search + AI match** |
| Entity loaded for customer | `GET /api/msx/milestones-for-customer/<id>` | **`GET /api/msx/opportunities-for-customer/<id>`** (new) |
| AI matching | `POST /api/ai/match-milestone` | **`POST /api/ai/match-opportunity`** (new, or parameterized) |
| Note model relationship | `notes_milestones` many-to-many | **`notes_opportunities`** many-to-many (new) |
| Note view display | Shows linked milestones with status | **Shows linked opportunities with status/value** |

### MSX Writeback Policy

**No automatic MSX writebacks for DSSs at launch.** Specifically:

| Action | SE Behavior | DSS Behavior |
|--------|------------|-------------|
| Comment on milestone/opportunity | Auto-posts `add_milestone_comment()` on note save | **No auto-post.** User can explicitly write back via the opportunity view page comment form. |
| Join team | Auto-joins milestone team | **No auto-join.** User can explicitly join deal team from opportunity view. |
| Task creation | Auto-creates tasks linked to milestones | **No task creation at launch.** Not implemented for opportunities yet. |

This matches the existing dev behavior where the `MSX_WRITE_ENABLED` environment variable controls whether writebacks happen. DSSs simply don't trigger any automatic writebacks on note save - but the explicit comment/writeback buttons on the opportunity view page still work.

### Opportunity Comment System

Opportunity comment CRUD is **already fully built** (edit + delete shipped):
- `POST /api/opportunity/<id>/comment` - add comment
- `PUT /api/opportunity/<id>/comment` - edit comment
- `DELETE /api/opportunity/<id>/comment` - delete comment

These are explicit user-initiated actions from the opportunity view page, not automatic writebacks.

### Note-Opportunity Attachment - Implementation Details

**New data model:**
- `notes_opportunities` association table (many-to-many, like `notes_milestones`)
- Note model gets `opportunities` relationship

**New/updated endpoints:**
- `GET /api/msx/opportunities-for-customer/<id>` - list open opportunities for a customer from MSX (similar to `milestones-for-customer` but returns opportunities)
- `POST /api/ai/match-opportunity` - AI matching for opportunities (could share the existing match-milestone endpoint with a `type` parameter, or be separate - implementation detail)

**Note form adaptation:**
- When `user_role == 'dss'`: show opportunity picker section instead of milestone picker
- Search, select, and AI-match work the same way, just targeting opportunities
- Selected opportunities show: name, number, status, estimated value, owner
- When `user_role == 'se'`: milestone picker unchanged

**Note save adaptation (`_handle_milestone_and_task`):**
- When `user_role == 'dss'`: reads opportunity form data, creates/updates Opportunity records, associates via `notes_opportunities`
- **No auto-writeback** (no `add_opportunity_comment`, no `add_user_to_deal_team`)
- When `user_role == 'se'`: existing milestone flow unchanged

---

## Implementation Phases

### Phase 1: Data Model + Onboarding

1. Add `user_role`, `my_seller_id`, `my_seller_alias` columns to `UserPreference` model
2. Add idempotent migration in `app/migrations.py`
3. Redesign wizard Step 1: role cards (primary) + theme toggle (secondary)
4. Add `POST /api/preferences/user-role` endpoint
5. Disable "Next" in wizard until role is selected
6. During account sync (Step 3), if DSS: capture alias, match to Seller, set `my_seller_id`
7. Tests for role selection API, migration, DSS identity matching

### Phase 2: Seller Mode Infrastructure

1. Context processor: inject `user_role`, `seller_mode`, `seller_mode_seller` into all templates
2. DSS auto-activation: if `user_role == 'dss'` and `my_seller_id` set, permanent seller mode
3. SE session-based seller mode: `POST /api/seller-mode/activate/<id>` and `/deactivate`
4. "View as Seller" button on seller view page (header, next to "New Customer" and "Edit Seller")
5. "View as Seller" button on sellers list page (per-row, right of "New Customer")
6. Seller mode banner in `base.html` (SE only)
7. Tests for activation/deactivation, context processor logic, session handling

### Phase 3: UI Adaptation (Template Conditionals)

Apply `{% if not seller_mode %}...{% endif %}` across all affected templates:

```
base.html              - Sellers nav menu item, seller mode banner injection point
index.html             - Scope revenue alerts and recent notes to active seller
customers_list.html    - Hide seller grouping/badges, scope query
customer_form.html     - Hide seller dropdown, auto-assign seller. Territory: auto-select if 1, dropdown if >1
customer_view.html     - Hide seller badge
notes_list.html        - Hide seller badges, scope query
note_form.html         - Hide seller display, auto-assign seller
note_view.html         - Hide seller badge
search.html            - Hide seller filter dropdown, scope results
engagements_hub.html   - Hide seller column, scope query
revenue_dashboard.html - Hide seller dropdown, auto-scope
```

Server-side query scoping in corresponding route handlers:
```
app/routes/notes.py        - filter by seller_id via customer relationship
app/routes/customers.py    - filter by seller_id
app/routes/engagements.py  - filter by seller's customers
app/routes/search.py       - scope search to seller's customers
app/routes/revenue.py      - scope dashboard to seller
app/routes/main.py         - scope index page data
```

Tests for each scoped route.

### Phase 4: Note-Opportunity Attachment (DSS Note Flow)

1. Add `notes_opportunities` association table and Note.opportunities relationship
2. Add `GET /api/msx/opportunities-for-customer/<id>` endpoint (list open opportunities from MSX)
3. Add AI opportunity matching (parameterize existing match-milestone endpoint or create new)
4. Adapt note form: show opportunity picker when `user_role == 'dss'`, milestone picker when SE
5. Adapt note save: handle opportunity form data for DSSs (create/update Opportunity records, associate)
6. Adapt note view/list: show linked opportunities instead of milestones for DSS notes
7. **No automatic MSX writebacks** on note save for DSSs (no auto-comment, no auto-team-join, no task creation)
8. Tests for opportunity lookup, AI matching, note-opportunity association, note save flow

### Phase 5: Revenue Bucket Generalization

1. Replace hardcoded `['Core DBs', 'Analytics', 'Modern DBs']` in `revenue_customer_view()` with dynamic query
2. Query `SELECT DISTINCT bucket FROM customer_revenue_data WHERE customer_id = ?`
3. Update code comments in models.py and revenue_import.py that reference specific bucket names (they're just examples)
4. Tests for dynamic bucket resolution

---

## Template Conditionals - Quick Reference

The primary template variable is `seller_mode` (bool). Use `{% if not seller_mode %}` to wrap SE-only UI:

```jinja2
{# Hide sellers nav item #}
{% if not seller_mode %}
<li><a href="/sellers">Sellers</a></li>
{% endif %}

{# Hide seller badge on customer #}
{% if not seller_mode %}
<span class="badge bg-primary">{{ customer.seller.name }}</span>
{% endif %}

{# Auto-assign seller on customer form #}
{% if seller_mode %}
<input type="hidden" name="seller_id" value="{{ seller_mode_seller.id }}">
{% else %}
<select name="seller_id">...</select>
{% endif %}

{# Territory picker - always shown, auto-select if only one #}
{% if territories|length == 1 %}
<input type="hidden" name="territory_id" value="{{ territories[0].id }}">
<span>{{ territories[0].name }}</span>
{% else %}
<select name="territory_id">...</select>
{% endif %}
```

For route-level query scoping, the pattern is:

```python
if seller_mode_seller:
    customers = Customer.query.filter_by(seller_id=seller_mode_seller.id).all()
else:
    customers = Customer.query.all()
```

---

## DSS Identity Resolution - Full Flow

```
Onboarding Step 1:
  User selects "DSS" role
  -> POST /api/preferences/user-role {role: 'dss'}
  -> Saves UserPreference.user_role = 'dss'

Onboarding Step 2 (Connect to MSX):
  User completes az login
  -> App validates Microsoft corp account (not personal)
  -> App verifies VPN/MSX connectivity (existing vpn-check)
  -> ONLY after both validations pass:
     -> Reads identity alias from az account show
     -> Stores in UserPreference.my_seller_alias
  -> If validation fails, alias is NOT stored (user must retry sign-in)

Onboarding Step 3 (Account Import):
  MSX import creates Seller records
  -> After import completes, match my_seller_alias to Seller.alias
  -> If match found: set UserPreference.my_seller_id = matched_seller.id
  -> If no match: show warning + dropdown of imported sellers for manual selection

Subsequent app loads:
  Context processor checks user_role == 'dss'
  -> Looks up Seller via my_seller_id (persisted in DB)
  -> Sets seller_mode=True, seller_mode_seller=Seller
  -> Works even without active az login (identity stored in DB)
```

---

## Settings Page

- The role (DSS/SE) is **not editable** in Settings. It's set once during onboarding and locked.
- The only way to change it is to re-run the onboarding wizard (reset onboarding).
- Settings page can display the current role as read-only info.

---

## Dev-Only: Admin Panel Role Toggle

When `FLASK_ENV=development`, the admin panel shows a **"DSS Mode Testing"** card that lets the developer switch between SE and DSS mode without re-running onboarding.

**Card contents:**
- Current role display (SE or DSS)
- Toggle button: "Switch to DSS Mode" / "Switch to SE Mode"
- When switching to DSS: sets `user_role='dss'`, sets `my_seller_id` to seller ID 7 (hardcoded in dev toggle, manually adjustable)
- When switching to SE: sets `user_role='se'`, clears `my_seller_id` and `my_seller_alias`, clears any session seller mode
- **Only visible in development** - uses `{% if config.ENV == 'development' %}` or equivalent
- No confirmation dialog needed - this is a dev tool for rapid testing

**Endpoint:** `POST /api/admin/dev-toggle-role` (guarded by `FLASK_ENV == 'development'` check in route)

---

## Answered Questions

1. **Should DSS mode still allow creating sellers?** No. DSSs are only concerned with their own customers and territories.
2. **Do DSSs ever need the "group by seller" view?** No.
3. **Opportunity comments don't have edit/delete yet - do they need it before DSS launch?** Done - already shipped.
4. **Can DSS/SE role be changed after onboarding?** No. Role is permanent once selected. Re-run onboarding to change.
5. **How do pods work in DSS mode?** Assuming msp_accountteam gets fixed, that populates the pod automatically. DSSs need pods too.
6. **Does the account sync change for DSS vs SE?** No. MSX import already scopes to the authenticated user's associations.
7. **How does the app know which Seller is "me" for a DSS?** Automatic - matches the `az login` alias to a Seller record. Persisted in `my_seller_id` so it works without active login.
8. **Where do "View as Seller" buttons go?** Both the seller view page (header row) and sellers list page (per-row).
9. **Is query scoping server-side or just UI hiding?** Server-side. Routes filter data to the active seller when in seller mode.
10. **What about territory picker in seller mode?** Always shown. Auto-selects if only one territory exists in the DB. Dropdown if multiple.
11. **Does the home page scope in seller mode?** Yes. Revenue alerts and recent notes filter to the active seller.
12. **Do DSSs get automatic MSX writebacks on note save?** No. No auto-comments, no auto-team-join, no auto-task creation. DSSs can explicitly write back from the opportunity view page.
13. **Do DSSs attach milestones or opportunities to notes?** Opportunities. The milestone picker is replaced with an opportunity picker in the note form for DSSs.
14. **Is task creation implemented for opportunities?** Not at launch. Task creation is SE/milestone-only for now.
15. **When is the DSS alias stored?** Only after validating Microsoft corp account sign-in AND successful MSX/VPN connectivity. Never stored on failed auth.
16. **Can I test DSS mode as a developer?** Yes. In development (`FLASK_ENV=development`), the admin panel has a toggle to switch between SE and DSS mode (hardcoded to seller ID 7) without re-running onboarding.

