# DSS (Seller) Mode for Sales Buddy

## Status: Backlog

**Date:** March 18, 2026
**Context:** Sales Buddy is currently designed for Solution Engineers (SEs) who manage multiple sellers and need to see all their accounts organized by seller. Digital Sales Specialists (DSSs/sellers) have different needs - they ARE the seller, so seller-centric navigation and grouping is redundant for them.

---

## Concept

A mode toggle in Settings/Preferences: **"I am a Solution Engineer"** vs **"I am a Seller"**

Stored in `UserPreference` (new field, e.g. `user_role = 'se' | 'dss'`). The app adapts its navigation, layout, and MSX interactions based on this setting.

---

## What Changes in DSS Mode

### Navigation - Hide/Simplify

| Feature | SE Mode (current) | DSS Mode |
|---------|-------------------|----------|
| Sellers menu | Visible - list of all sellers | **Hidden** - they are the seller |
| Seller view page | Dashboard per seller | **Hidden** - Revenue + Milestones in main nav are already scoped to them |
| Customers list | Grouped by seller, seller filters | **Flat list** - no seller grouping or filters |
| Search page | Seller dropdown filter | **No seller filter** - all results are implicitly theirs |
| Engagements hub | Shows seller column | **No seller column** - all engagements are theirs |
| Revenue dashboard | Seller dropdown, per-seller drill-down | **Single view** - already scoped to one seller |
| Notes list | Seller badges on notes | **No seller badges** - all notes are theirs |

### Customers - Simpler Relationship

| SE Mode | DSS Mode |
|---------|----------|
| Customer has `seller_id` FK, SE picks seller from dropdown | Customer belongs directly to the DSS, no seller dropdown needed |
| Customer form has seller picker with territory auto-link | **No seller picker** - customer is auto-assigned to the DSS |
| Note form shows "Seller: Amy Kingzett" | **No seller display** on notes |

### MSX Integration - Different Comment Targets

| Action | SE Mode | DSS Mode |
|--------|---------|----------|
| Call log syncs to MSX | **Milestone comments** (via `add_milestone_comment`) | **Opportunity comments** (via `add_opportunity_comment`) |
| Task creation | Linked to milestones | **Linked to opportunities** (needs new `create_task` variant) |
| Auto-join team | Joins milestone team | **Joins deal team** (already have `add_user_to_deal_team`) |

### What Stays the Same

- Note creation/editing (Quill editor, topics, partners, engagements, tasks)
- Engagement management (local engagement tasks are role-agnostic)
- Milestone tracker (DSSs still care about milestones, just don't own them)
- Revenue analyzer (works for any seller name)
- Customer view page (notes, opportunities, milestones - all universal)
- Partner directory
- Pod management
- AI features (auto-fill, auto-tag, summarize)
- Draft system

---

## Implementation Approach

### Phase 1: Mode Toggle + UI Adaptation

1. **Add `user_role` to UserPreference** (`se` or `dss`, default `se`)
2. **Settings page** - radio button or toggle to select role
3. **Pass role to templates** via context processor or `g.user_role`
4. **Conditionally hide in `base.html` nav:**
   - Sellers menu item
   - Seller filters in search/customers
5. **Conditionally hide in templates:**
   - Seller grouping in `customers_list.html` (use `{% if user_role != 'dss' %}`)
   - Seller column in `engagements_hub.html`
   - Seller badges in `notes_list.html`
   - Seller dropdown in `search.html`
   - Seller picker in `customer_form.html`

### Phase 2: DSS-Specific Behaviors

1. **Auto-assign seller**: In DSS mode, when a customer is created, auto-set `seller_id` to a "self" seller record (or skip seller entirely)
2. **Opportunity comments**: When saving a note linked to an opportunity, post to `add_opportunity_comment` instead of milestone comment
3. **Opportunity-linked tasks**: Add task creation that targets opportunities (similar to milestone task creation, different MSX entity)

### Phase 3: Onboarding

1. **First-run wizard** asks "Are you a seller or solution engineer?"
2. **Account import** adapts:
   - DSS: "Import My Accounts" (uses `eq-userid`, see `backlog/msx-account-import.md`)
   - SE: "Import My Sellers' Accounts" (CSV of aliases)

---

## Template Conditionals - Quick Reference

The primary mechanism is `{% if user_role != 'dss' %}...{% endif %}` wrapping seller-specific UI blocks:

```
base.html           - Sellers nav menu item
customers_list.html - "Group by seller" sort mode, seller badges in table
customer_form.html  - Seller dropdown
customer_view.html  - Seller badge display
notes_list.html     - Seller badges on note cards
note_form.html      - "Seller: Name" display
note_view.html      - Seller badge
search.html         - Seller filter dropdown, seller grouping in results
engagements_hub.html - Seller column toggle
revenue_dashboard.html - Seller dropdown filter (or auto-scope to self)
```

These are all display-level changes - the underlying data model doesn't change, just what's visible and what's auto-populated.

---

## MSX Comment Routing Decision Tree

```
Note saved with linked milestone/opportunity
  |
  ├─ SE mode
  |   └─ Has milestone? → add_milestone_comment()
  |   └─ Join milestone team → add_user_to_milestone_team()
  |
  └─ DSS mode
      └─ Has opportunity? → add_opportunity_comment()
      └─ Join deal team → add_user_to_deal_team()
      └─ Has milestone? → Still sync to milestone (they care about tracking)
```

Note: Opportunity comment system already exists (`app/routes/opportunities.py`) with POST endpoints and cached JSON. It just doesn't have edit/delete like milestones do - that would need parity work.

---

## Effort Estimate

- **Phase 1** (UI toggle + hiding): Small - mostly template conditionals and one new preference field
- **Phase 2** (behavior changes): Medium - opportunity task creation is new, comment routing needs care
- **Phase 3** (onboarding): Ties into the account import feature (separate backlog item)

---

## Open Questions - Answered

1. **Should DSS mode still allow creating sellers?** No. DSSs are only concerned with their own customers and territories. Account sync creates their customers for them.
2. **Do DSSs ever need the "group by seller" view?** No.
3. **Opportunity comments don't have edit/delete yet - do they need it before DSS launch?** Yes - being built now (feature/opportunity-comment-edit-delete branch).
4. **Should we support mode-switching?** Yes - needs to be switchable for testing both UIs. Also prompted on the very first page of the new user onboarding flow so users don't miss it.
5. **How do pods work in DSS mode?** Assuming msp_accountteam gets fixed, that populates the pod automatically. DSSs need pods too. Can import extra DSS-specific data (other DSSs, SEs) if they want to use pod features.
