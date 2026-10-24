# Bootstrap removal audit

October 1, 2026. Read-only dependency audit for the upcoming redesign.
No application code, assets, preferences, or records were changed.

## Final implementation update, October8

Checkpoints91-105 remove the framework CSS, bundle, widget APIs and `--bs-*`
runtime dependencies. Application-owned browser normalization and native controls
replace them; the independent Bootstrap Icons library remains intentionally.
The historical inventory below describes the October1 baseline, not current runtime.
The first-pass redesign was completed and approved on October 9, 2026.
Its temporary checkpoint evidence has been retired. Native window-state
restoration is implemented separately from the framework removal.

## Decision summary

**Full removal is feasible in the redesign, but it is an app-wide migration,
not a homepage change.** Most of the volume is repeated styling vocabulary.
The harder work is replacing a small set of behavioral primitives correctly
and migrating their existing consumers.

Recommend targeting a Bootstrap-free finished redesign, using shared
application CSS and shared interaction helpers within the existing Flask/Jinja
architecture. Do not combine this with a SPA conversion or business-logic rewrite.
Migrate in checkpoints on the feature branch; do not remove the global assets
while pages or controls still depend on them.

This report inventories dependencies, not current accessibility defects or
performance scores. Static references are not counts of live controls, unique
screens, or executed code paths. Effort estimates require implementing and
testing the highest-risk workflows first.

## Scope and measurement

The baseline includes 805 tracked files. Runtime presentation scope contains
103 HTML templates (81 outside `partials`, 22 inside) and six first-party static
JavaScript files. Python, tests, manifests, instructions, scripts, and supporting
documentation were also checked for presentation coupling.

Excluded from runtime totals: the offline design archive, third-party Chart.js,
ignored dependencies/build outputs, and uses of "bootstrap" meaning startup
initialization rather than the UI framework.

Styling counts come from matching known component, grid, and utility tokens
inside literal `class` attributes, including embedded HTML strings. This is a
heuristic source scan, not an HTML/Jinja execution: custom similarly named classes
can match, and dynamically computed classes can be missed.

| Dependency | Measured footprint |
| --- | --- |
| Bootstrap-associated component/grid/utility classes | 95 of 103 templates |
| Component-class token occurrences | 5,809 in 94 templates |
| Grid-class token occurrences | 587 in 75 templates |
| Utility-class token occurrences | 8,855 in 94 templates |
| Combined component/grid/utility token occurrences | 15,251; not 15,251 unique components |
| Bootstrap CSS variable names, `--bs-*` | Present in 26 templates |
| `data-bs-theme` theme coupling | Present in 11 templates |
| Bootstrap Icons literal `bi-*` references | 2,050 references, 204 distinct names, across 108 runtime source files |

## 1. Asset loading and global CSS

[base.html](../templates/base.html#L35) loads Bootstrap 5.3.0 CSS and Bootstrap
Icons 1.11.0 CSS. It also loads
[Bootstrap's bundled JavaScript](../templates/base.html#L1501).
These are CDN loads, not Python packages or an Electron npm Bootstrap dependency.
The bundle supplies Bootstrap's JavaScript components and its Popper dependency.

The production app has no tracked standalone application CSS file; much of its
styling lives in template `<style>` blocks. The shared template contains
substantial Bootstrap variable overrides and component overrides, with additional
page-specific rules.

Removal must account for:

- Bootstrap Reboot's inherited base styles: box sizing, typography, margins,
  form controls, tables, and other browser normalization.
- Grid, flex, spacing, visibility, sizing, border, alignment, and text utilities.
- Buttons, forms/validation, cards, badges, tables, lists, alerts, pagination,
  progress indicators, spinners, navigation, tabs, overlays, and close controls.
- Dynamic class changes such as visibility and validation states. Replacing the
  stylesheet without migrating class-dependent JavaScript leaves behavior broken
  even when no `bootstrap.*` call is involved.
- Bootstrap-variable consumers in charts, conditional colors, and report styles.

Build the application's tokens, base styles, semantic components, and necessary
layout utilities first. Do not copy the whole Bootstrap stylesheet into a
"custom" compatibility file and call that removal.

## 2. Behavioral replacement obligations

The runtime counter was checked against all 109 scoped files, including direct
children of `templates` and `static/js`, and its aggregates were reconciled with
per-file counts. There are **121 literal component references** and **101
lifecycle-event string occurrences**. These include instance lookup/guards,
not just constructor calls.

| Family | Literal `bootstrap.Component` references | Declarative controls / dismissal | Lifecycle event occurrences |
| --- | --- | --- | ---: |
| Modal | 90 across 26 files | 24 toggle attributes; 102 dismiss attributes across 31 files | 59 |
| Collapse | 3 across 2 files | 24 toggle attributes across 15 files | 12 |
| Offcanvas | 18 across 2 files | 7 dismiss attributes; opening is programmatic in the scanned literals | 22 |
| Tab | 6 across 2 files | 9 tab and 4 pill toggle attributes | 6 |
| Popover | 4 across 2 files | 1 `setAttribute` toggle configuration, counted separately from markup attributes | 2 |
| Dropdown | 0 | 13 toggle attributes across 8 files; zero constructor references does not mean unused | 0 |
| Alert | 0 | 2 dismiss attributes across 2 files | 0 |
| Toast | 0 | 1 dismiss attribute, plus app-owned notification markup/lifecycles | 0 |

The lexical patterns were `bootstrap.Component`, `event.bs.component`, quoted
`data-bs-toggle`/`data-bs-dismiss` attributes, and literal `setAttribute`
configurations. No literal Tooltip, Carousel, ScrollSpy, or Button component
references or corresponding toggle attributes were found. These measurements
do not count dynamically computed strings or claim every matched source site
executes on every page.

Configuration matters too: the source contains `data-bs-backdrop` (9
occurrences), `data-bs-keyboard` (1), `data-bs-scroll` (7),
`data-bs-auto-close` (7), and `data-bs-parent` (1). Preserve per-control backdrop,
keyboard, body-scroll, dropdown-dismissal, and exclusive-collapse behavior.
Do not give every production dialog the mockup's generic backdrop-dismiss policy.
Attribute-name counts can include source selectors, not just rendered attributes.

| Family | What must continue to work | Representative integration |
| --- | --- | --- |
| Modals | Show/hide APIs, dismiss buttons, Escape, backdrop policy, focus trapping/return, body scroll handling, transition-completion hooks, lazy-loaded contents, instance disposal. | [Homepage](../templates/index.html#L2085), [action-item hub](../templates/action_items_hub.html#L51), [manager report](../static/js/manager_one_on_one.js#L207) |
| Nested modal workflows | Hide the parent, move a child outside its container, then restore the parent after the child closes. Preserve focus and record context. | [Milestone partial](../templates/partials/milestone_view_content.html#L455), [project partial](../templates/partials/project_view_content.html#L127) |
| Collapse/disclosure | Toggle bodies, expanded state/ARIA, header state, persisted open/closed preferences, and lifecycle callbacks. | [Customer list](../templates/customers_list.html#L440), [admin](../templates/admin_panel.html#L2201) |
| Dropdown menus | Click/outside-click dismissal, correct placement, Escape and keyboard navigation, focus behavior, and menu choices. Declarative controls also need replacement. | [Shared navigation](../templates/base.html#L748), [milestone tracker](../templates/partials/milestone_tracker_content.html) |
| Offcanvas panels | Panel open/close, backdrop and keyboard behavior, stacking interactions, and lifecycle events. These are real app workflows, regardless of the new homepage's centered modals. | [Shared shell](../templates/base.html#L4085), [note editor](../templates/note_form.html#L1157) |
| Tabs/pills | Selected tab/panel state, keyboard and ARIA behavior, and activation hooks used for content side effects. | [Homepage tab controls](../templates/index.html#L636), [activation hook](../templates/index.html#L1313) |
| Popover | Shared-shell popover integration and revenue bucket selector content, positioning, event hookup when opened, outside-dismiss/focus behavior. | [Shared shell](../templates/base.html), [revenue bucket filter](../static/js/revenue-bucket-filter.js#L211) |
| Alerts and notifications | Dismiss controls, notification markup/visibility, and existing application-managed toast lifecycles. Not every toast uses Bootstrap's Toast API. The unused duplicate sharing client was removed during final reconciliation. | [Sharing notifications](../static/js/share.js), [MSX workspace toast](../templates/report_msx_workspace.html#L387) |

Do not replace these families with independent bespoke implementations on every
page. Native `<dialog>`, disclosure, and popover capabilities are candidates,
but existing workflow semantics and keyboard behavior still need shared adapters
and regression checks. Matching a method name is not behavioral equivalence.

### Highest-risk consumers

1. **Note editor:** recording/meeting workflows, previews, overlays, modal/offcanvas
   handoffs, dynamic content, and a large amount of script-driven UI.
2. **Shared shell and navigation:** global dialogs, startup/help workflows, and
   controls that appear on every page.
3. **Milestone/project/workspace partials:** nested dialogs, initialization when
   inserted dynamically, and restore-parent behavior.
4. **Admin:** many controls and persisted collapsible sections; not a page that
   can safely be ignored until after the global stylesheet is removed.
5. **Homepage and manager report:** lazy-loaded detail partials, tab side effects,
   repeated open/close, and disposal of old contents.

Bootstrap modal events cannot simply disappear: some listeners are the points
where editors initialize, deferred data loads happen, or parents are reopened.
Provide replacement lifecycle hooks and migrate consumers explicitly.

## 3. JavaScript-created presentation

All six first-party static JavaScript files contain styling/icon coupling:

- [manager_one_on_one.js](../static/js/manager_one_on_one.js): detail dialogs,
  dynamically rendered report/editor content, validation and visibility classes.
- [page-help.js](../static/js/page-help.js): generated help dialog markup.
- [revenue-bucket-filter.js](../static/js/revenue-bucket-filter.js): popover and
  generated selector markup.
- [share.js](../static/js/share.js): sharing dialogs, review content, notifications.
- [share.js](../static/js/share.js): consolidated native partner-sharing dialogs and notifications.
- [milestone-multi-picker.js](../static/js/milestone-multi-picker.js): icon
  coupling in the application-owned picker, not necessarily a Bootstrap JS component.

Changing only template markup is insufficient. Include HTML strings, class
assignments, selectors, and event names in the migration.

## 4. Bootstrap Icons is a separate decision

Bootstrap Icons does not require the Bootstrap CSS/JS framework. Framework
removal and icon replacement are technically separate.

For a finished application with **no Bootstrap-named runtime dependency**,
replace the icon font/CDN too. There are 204 distinct literal icon names, not
204 distinct behaviors. An icon mapping/shared renderer avoids hand-designing
every replacement and keeps dynamic metadata working.

The coupling extends into Python:

- [reports.py](../app/routes/reports.py#L263): report/group icon metadata.
- [main.py](../app/routes/main.py#L869): entity and recently-viewed icon metadata.
- [notes.py](../app/routes/notes.py#L1802) and
  [engagements.py](../app/routes/engagements.py#L924): display metadata.
- [revenue.py](../app/routes/revenue.py#L1022): report icon metadata.

Also preserve application-owned logo, favicon, customer/partner images, and
Copilot artwork. These do not become obsolete because Bootstrap is removed.
If reusing open-source icon artwork locally, retain the applicable attribution.

## 5. Backend and persistence coupling

The business services, SQLite schema, APIs, MSX integration, and Electron
supervision do not fundamentally require Bootstrap.

However, presentation changes are not exclusively HTML/JS:

- The Python icon metadata above must map to the new icon system.
- [MSX error notifications](../app/services/milestone_tracking.py#L86) emit an
  `alert-link` and use `closest('.alert')` in a retry interaction. Preserve the
  retry workflow when replacing alert markup.
- Theme preference currently persists a nullable `dark_mode` Boolean. Keep its
  behavior while introducing a family preference and migrating DOM/CSS theme
  selectors. Removing Bootstrap is not a reason to reset preferences or records.
- Semantic color names such as success/warning/primary may be retained as
  application meanings; they are not database dependencies on the library.

## 6. Tests, instructions, and unrelated matches

Tests currently assert Bootstrap-specific markup and symbols. Examples:

- [Manager-report collapse controls](../tests/test_manager_one_on_one.py#L829).
- [Shared widget prerequisite setup](../tests/test_dashboard_requirements.py).
- [Hygiene CSS variables](../tests/test_hygiene_report.py#L114).
- [UX button/icon selectors](../tests/test_ux_improvements.py#L233).
- [Manager-report JS harness](../tests/js/manager_one_on_one.test.cjs): Bootstrap
  modal mocks and class-dependent test DOM.

Update assertions to the replacement's semantic and behavioral contracts.
Do not delete coverage simply to get rid of framework-specific strings. Icon
assertions also occur in activity, contact, seller, milestone, favicon, team,
calendar-planning, and view tests.

Update the Bootstrap-native guidance in
[.impeccable.md](../.impeccable.md) and
[project instructions](../.github/copilot-instructions.md)
when the new implementation is selected. Otherwise future work will reintroduce it.

Do **not** remove unrelated startup uses of the word "bootstrap": Azure profile
initialization, Python/pip installation, Power BI query bootstrap, and the
shell-update "bootstrap commit" convention are not UI dependencies.

The design archive is historical reference material, not a runtime dependency.
Retain factual references to the old framework there; identify them explicitly
when checking the finished runtime removal.

## 7. Recommended implementation checkpoints

1. Establish application CSS tokens/base styles, an icon abstraction, and
   shared behavior contracts. Preserve Flask/Jinja and existing business behavior.
2. Prove the hardest slice: real note editor and milestone/note partials in
   both full-page and modal contexts, including nested actions and repeated opens.
3. Migrate shared navigation/overlays, then homepage, entity pages, reports,
   settings/admin, and all JavaScript-generated presentation. Temporary coexistence
   on the feature branch is acceptable; the finished release is not partial removal.
4. Migrate theme selectors/variables, icons and backend presentation strings,
   tests, and development guidance. Preserve saved preferences and semantic colors.
5. Remove CDN CSS, icon font if in scope, and Bootstrap bundle only after all
   runtime consumers have replacements. Run a final source inventory and desktop
   acceptance pass.

### Release gate

- No Bootstrap CSS/JS downloads, `bootstrap.*` calls, `*.bs.*` lifecycle
  listeners, or dependent `data-bs-*` controls remain in runtime code.
- No unresolved `--bs-*` or Bootstrap-only style/class dependencies remain.
  Generic class names alone do not prove a dependency, but a hidden compatibility
  clone does not satisfy the intent of full removal.
- If icons are included, no Bootstrap Icons font/download or `bi-*` coupling
  remains in runtime metadata, selectors, markup, or generated content.
- Test keyboard navigation, focus return/trapping, outside/Escape dismissal,
  unsaved edits, nested overlays, scroll handling, lazy content, and repeated opens.
- Test complete workflows in all selected theme pairs, in Electron and the
  supported desktop browser path, including shorter/scaled work areas.
- Use scoped automated tests during implementation; do not weaken existing
  behavioral coverage or run tests against user data.

## Appendix: template styling footprint

All paths below are relative to `templates`. C = classified component-class
occurrences. L/U = grid plus utility occurrences. I = icon-class occurrences,
including both `bi` and individual glyph classes, not rendered icon counts.
Theme lists direct variable/mode coupling. Zero rows can inherit dependencies
through includes or the shared base.

| Template | C | L/U | I | Theme |
| --- | ---: | ---: | ---: | --- |
| _forecast_comments_js.html | 29 | 20 | 18 | |
| _forecast_comments.html | 12 | 23 | 6 | |
| action_items_hub.html | 35 | 38 | 16 | |
| admin_ai_logs.html | 15 | 46 | 18 | |
| admin_favicons.html | 28 | 49 | 16 | |
| admin_panel.html | 538 | 1154 | 463 | tokens, mode |
| analytics.html | 38 | 91 | 20 | |
| base.html | 313 | 350 | 232 | tokens, mode |
| connect_export.html | 74 | 91 | 42 | |
| customer_form.html | 46 | 18 | 8 | |
| customer_view.html | 180 | 282 | 135 | tokens, mode |
| customers_list.html | 80 | 111 | 67 | tokens |
| engagement_form.html | 60 | 91 | 23 | |
| engagement_view.html | 0 | 0 | 0 | |
| engagements_hub.html | 31 | 76 | 22 | |
| fill_my_day.html | 88 | 144 | 70 | tokens |
| index.html | 200 | 363 | 143 | tokens, mode |
| internal_contact_form.html | 23 | 13 | 6 | |
| internal_contacts_list.html | 17 | 19 | 12 | |
| metrics.html | 35 | 55 | 12 | tokens, mode |
| milestone_form.html | 12 | 10 | 2 | |
| milestone_tracker.html | 47 | 72 | 64 | tokens, mode |
| milestone_view.html | 0 | 0 | 0 | |
| milestones_list.html | 11 | 13 | 6 | |
| note_form.html | 638 | 928 | 441 | tokens, mode |
| note_template_form.html | 10 | 13 | 6 | |
| note_view.html | 0 | 0 | 0 | |
| notes_list.html | 32 | 45 | 18 | tokens, mode |
| one_on_one_hub.html | 16 | 57 | 12 | tokens |
| one_on_one_workspace.html | 0 | 0 | 0 | |
| opportunity_view.html | 0 | 0 | 0 | |
| partials/_action_item_detail_modal.html | 55 | 46 | 56 | tokens |
| partials/_action_items_hub.html | 56 | 169 | 35 | |
| partials/_customer_opportunities.html | 0 | 27 | 9 | |
| partials/_dashboard_settings.html | 17 | 10 | 10 | |
| partials/_engagement_view_content.html | 154 | 292 | 95 | tokens, mode |
| partials/_fy_management_card.html | 42 | 108 | 44 | |
| partials/_project_form_fields.html | 18 | 16 | 2 | |
| partials/_report_export.html | 7 | 0 | 7 | |
| partials/activity_coverage_caip.html | 8 | 27 | 15 | tokens |
| partials/activity_coverage_milestones.html | 50 | 96 | 31 | tokens |
| partials/customer_favicon.html | 0 | 0 | 2 | |
| partials/engagements_panel.html | 35 | 91 | 28 | |
| partials/milestone_tracker_content.html | 98 | 169 | 55 | |
| partials/milestone_view_content.html | 138 | 172 | 64 | |
| partials/note_view_content.html | 64 | 93 | 52 | |
| partials/one_on_one_workspace_content.html | 125 | 212 | 47 | tokens |
| partials/opportunity_view_content.html | 47 | 73 | 35 | |
| partials/partner_favicon.html | 0 | 0 | 2 | |
| partials/project_view_content.html | 46 | 48 | 19 | |
| partials/revenue_alert_detail.html | 22 | 65 | 12 | |
| partials/revenue_alerts_content.html | 18 | 48 | 24 | tokens |
| partials/review_action.html | 47 | 47 | 40 | |
| partner_form.html | 37 | 30 | 21 | tokens |
| partner_view.html | 141 | 144 | 73 | |
| partners_list.html | 17 | 23 | 13 | |
| pod_form.html | 17 | 36 | 12 | |
| pod_view.html | 32 | 54 | 24 | |
| pods_list.html | 7 | 28 | 6 | |
| project_form.html | 7 | 6 | 4 | |
| project_view.html | 0 | 0 | 0 | |
| report_activity_coverage.html | 83 | 146 | 60 | tokens |
| report_connect_impact.html | 27 | 62 | 10 | |
| report_hygiene.html | 114 | 117 | 28 | tokens |
| report_manager_one_on_one.html | 96 | 207 | 25 | tokens |
| report_marketing_insights.html | 48 | 110 | 20 | |
| report_msx_workspace.html | 191 | 221 | 130 | |
| report_one_on_one.html | 116 | 300 | 68 | tokens |
| report_u2c.html | 87 | 213 | 58 | tokens |
| report_whats_new.html | 64 | 105 | 40 | tokens |
| report_whitespace.html | 102 | 142 | 36 | |
| report_workload.html | 46 | 81 | 10 | |
| reports_hub.html | 8 | 15 | 6 | |
| revenue_bucket_products.html | 22 | 47 | 12 | |
| revenue_config.html | 67 | 39 | 6 | |
| revenue_customer_view.html | 36 | 110 | 18 | |
| revenue_dashboard.html | 66 | 82 | 40 | |
| revenue_import.html | 38 | 63 | 24 | |
| revenue_product_view.html | 28 | 48 | 14 | |
| revenue_products_list.html | 12 | 21 | 8 | |
| revenue_report_synapse_customers.html | 32 | 55 | 26 | |
| revenue_reports_list.html | 12 | 15 | 7 | |
| revenue_seller_alerts.html | 22 | 19 | 10 | |
| revenue_seller_product_view.html | 16 | 27 | 12 | |
| revenue_seller_products.html | 16 | 28 | 8 | |
| search.html | 39 | 74 | 24 | |
| seller_form.html | 40 | 29 | 10 | |
| seller_view.html | 109 | 198 | 70 | tokens |
| sellers_list.html | 18 | 67 | 24 | mode |
| settings.html | 63 | 55 | 34 | mode |
| solution_engineer_form.html | 20 | 15 | 4 | |
| solution_engineer_view.html | 28 | 29 | 8 | |
| solution_engineers_list.html | 10 | 20 | 10 | |
| specialties_list.html | 18 | 15 | 10 | |
| specialty_form.html | 16 | 11 | 4 | |
| specialty_view.html | 28 | 24 | 8 | |
| territories_list.html | 14 | 24 | 12 | |
| territory_form.html | 12 | 12 | 4 | |
| territory_view.html | 33 | 38 | 10 | |
| topic_form.html | 12 | 7 | 4 | |
| topic_view.html | 28 | 36 | 16 | |
| topics_list.html | 11 | 14 | 8 | |
