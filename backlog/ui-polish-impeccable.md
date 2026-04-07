# UI Polish with Impeccable Design Skills

## Overview

Install [Impeccable](https://impeccable.style/) design skills to systematically audit and polish the existing SalesBuddy UI. Impeccable works on top of Bootstrap - no framework swap needed. It provides structured design commands (`/audit`, `/polish`, `/normalize`, `/typeset`, etc.) that assess and fix typography, spacing, color, contrast, layout, and visual hierarchy.

## Install

```
npx skills add pbakaus/impeccable
```

Drops skill files into the workspace that VS Code Copilot picks up automatically. Update with `npx skills update`.

## Approach

1. **Audit first** - Run `/audit` on high-traffic pages to get severity-rated findings (P0-P3) across typography, color/contrast, layout, visual details, and responsiveness
2. **Normalize** - Use `/normalize` to establish consistent baseline styles (spacing scale, type scale, color usage) across pages
3. **Polish page by page** - Use `/polish` on individual pages, starting with the most-used ones
4. **Keep Bootstrap** - All changes stay within Bootstrap's utility classes and component system

## Pages to Audit (priority order)

- [ ] `customer_view.html` - most-used page, lots of data density
- [ ] `customers_list.html` - first thing users see after index
- [ ] `notes_list.html` / `note_view.html` - core workflow
- [ ] `engagement_view.html` / `engagements_hub.html`
- [ ] `milestone_tracker.html` / `milestones_list.html`
- [ ] `reports_hub.html` and individual report pages
- [ ] `admin_panel.html`
- [ ] `base.html` (navbar, sidebar, global styles)
- [ ] `revenue_dashboard.html`
- [ ] Forms (`customer_form.html`, `note_form.html`, `engagement_form.html`, etc.)

## Available Commands

| Command | Use For |
|---------|---------|
| `/audit` | Full diagnostic with severity scores |
| `/polish` | Apply targeted improvements |
| `/normalize` | Establish consistent baseline |
| `/typeset` | Fix typography hierarchy and scale |
| `/arrange` | Improve layout and spacing |
| `/colorize` | Fix color usage and contrast |
| `/critique` | Nielsen heuristic scoring + persona testing |
| `/harden` | Edge cases, empty states, error states |
| `/quieter` | Reduce visual noise |
| `/bolder` | Increase visual impact where needed |
