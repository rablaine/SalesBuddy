# Building dashboard widgets

This is the shared reference for people and coding agents building Sales Buddy
widgets. A widget is a reusable view of a report or metric, not a separate copy of
its business logic.

**Start here:** choose an existing report/service to reuse, define the decision the
widget helps someone make, and follow the checklist below. Agents should also read
the [widget instructions](../.github/instructions/dashboard-widgets.instructions.md)
and the [project conventions](../.github/copilot-instructions.md).

For an agent working in this repository, a useful starting request is:

> Read docs/DASHBOARD_WIDGET_AUTHORING.md. Build a report-backed widget for [goal].
> First identify its existing data service, supported filters, shared loaders,
> setup requirements, sync dependencies, and actionable full-report destination.
> Preserve saved dashboards and add scoped regression tests.

## Contents

- [Design the widget's contract](#design-the-widgets-contract)
- [Understand the loading pipeline](#understand-the-loading-pipeline)
- [Register the widget and its variants](#register-the-widget-and-its-variants)
- [Expose and implement filters](#expose-and-implement-filters)
- [Make data loading reusable](#make-data-loading-reusable)
- [Gate configuration and show sync progress](#gate-configuration-and-show-sync-progress)
- [Build the presentation](#build-the-presentation)
- [Link to an actionable full view](#link-to-an-actionable-full-view)
- [Wire all relevant surfaces](#wire-all-relevant-surfaces)
- [Verify before handing it over](#verify-before-handing-it-over)
- [Working examples and file map](#working-examples-and-file-map)

## Design the widget's contract

Write down these decisions before implementing:

| Question | Required answer |
|---------|-----------------|
| User decision | Who should the user contact, investigate, assess, or act on next? |
| Population | Exact inclusion/exclusion rules, thresholds, date window and ordering. |
| Meaning | Units, currency period, denominator, missing-value meaning and caveats. |
| Source | Saved models/imports, completeness/freshness evidence and required syncs. |
| Shared reads | Existing named loaders to reuse; parameters that distinguish their results. |
| Filters | Supported customer/seller scope, optional seller grouping and any unsupported controls. |
| Setup | Choices that must be confirmed before data is calculated or synced. |
| Presentations | Summary and Details, supported widths, useful default heights. |
| Destination | A matching full report, preferably with an existing action/review workflow. |

Distinguish a signal from a confirmed outcome. A modelled opportunity is not
qualified pipeline; a heuristic priority is not a source measurement; missing data
is not necessarily zero or proof of no exposure. Explain these in the full report
and retain only essential context in the widget.

## Understand the loading pipeline

```text
Code-owned widget definition + user-owned saved placement
    -> check setup requirements and source readiness
    -> resolve supported customer/seller scope
    -> run the report-backed provider
         -> reuse shared read-only loaders within this render
    -> render the selected variant in the common widget frame
    -> open the full report for investigation or action
```

There are three different declarations, not one universal query registry:

| Mechanism | Purpose |
|-----------|---------|
| `WidgetDefinition` | Presentation, provider, supported filters and gates. |
| `WIDGET_SOURCES` | Which saved imports must exist, in prerequisite order. |
| Named loaders with `@request_cached` | Reuse actual query results across different widget providers. |

**`WIDGET_SOURCES` does not batch queries.** Queries execute on first use of a
loader; the current dashboard does not discover arbitrary SQL, pre-execute every
query, or run every provider concurrently. A provider cache also avoids repeating
the same widget key/customer scope, but it cannot recognize duplicated queries in
different providers. Shared loaders are how authors expose that reuse.

## Register the widget and its variants

Add a stable `domain.purpose` key to `WIDGET_DEFINITIONS` in
[dashboards.py](../app/services/dashboards.py). Follow the existing RTI registration:

```python
WidgetDefinition(
    'fabric.rti', 'Real-Time Intelligence',
    'Streaming customers without RTI usage.',
    'Real-Time Intelligence',
    (
        WidgetVariant(
            'metric', 'Summary', 'partials/widgets/rti.html',
            (3, 4, 6, 12), MIN_WIDGET_HEIGHT, 4,
        ),
        WidgetVariant(
            'detailed', 'Details', 'partials/widgets/rti.html',
            (6, 12), MIN_WIDGET_HEIGHT, 5,
        ),
    ),
    get_rti_widget,
    scope='RTI / rolling 28 days; ACR / last 3 complete months',
    supported_filters=(*FILTER_LABELS, 'group_by_seller'),
)
```

This is an example of an existing registration, not another entry to add.
Replace the identity, provider, copy and template when building a new widget.

- `report` is a descriptive library label; the provider's `destination` is the URL.
- `scope` is descriptive text, not an access restriction or executable filter.
- `supported_filters` describes real behavior, not controls the author hopes to add later.
- Declare `requirements` only when the corresponding setup flow is implemented.
- Use `specialty='data'` where appropriate. Preserve consistent access checks in
  the underlying service, full report/API, read tools and sync paths too.
- Catalogue listing must stay metadata-only. Never calculate widget values while
  listing the library.
- Keys and variant keys are persisted in user placements. Do not casually rename
  or remove them, alter existing layouts, or install new placements without approval.

The grid has 12 columns. Widths 3/4/6/12 mean quarter/third/half/full width.
Rows are 64px with 12px gaps. Declare only footprints the presentation actually
supports. The `_report_variants()` helper supplies standard Summary/Details
variants; use explicit `WidgetVariant` entries when a different height is justified.

## Expose and implement filters

Use [dashboard_filters.py](../app/services/dashboard_filters.py), not an independent
filter engine.

### The declaration and the provider must agree

To support both customer and seller scope:

```python
supported_filters=tuple(FILTER_LABELS)
```

Add `'group_by_seller'` only when the template implements grouping. Catalogue
metadata, the Filters UI and the rendered instance expose these capabilities.
Unsupported scope controls are reported as ignored, not permission to silently
claim the data was filtered.

Users set widget scope after placement in the Filters flyout, not in the library
picker. The dashboard's `filter_config.live_filters` separately chooses which live
controls are enabled. Declaring support does not force those controls onto every
saved dashboard.

Published dashboards remember viewer customer/seller selections and seller grouping
in localStorage under `sb.dashboard.filters.<dashboard-id>`, separately from saved
scope and editor drafts. Restore controls and refresh widget data together on page
load or return, respecting currently enabled and supported filters. Explicit live
filter URL parameters take precedence over browser preferences. Reset filters
remembers the cleared selection; draft previews do not overwrite viewer preferences.

Providers accept this shape:

```python
def get_rti_widget(
    reference: date | None = None, *, customer_ids: tuple[int, ...] | None = None,
) -> dict:
    """Reuse report qualification, scope and grouping without another cutoff."""
    report = get_rti_report(reference, customer_ids=customer_ids)
    return {
        **report,
        'details': {'rows': report['rows'], 'seller_groups': report['seller_groups']},
    }
```

The dashboard combines permanent dashboard scope, permanent widget scope, and
enabled temporary live filters. Selections are OR within a filter and AND across
filters/layers. Seller selections resolve to customer IDs; the provider normally
does not need a separate `seller_ids` argument.

| Value at the provider boundary | Meaning |
|-------------------------------|---------|
| `customer_ids=None` | No additional dashboard customer restriction. Preserve service access rules. |
| `customer_ids=()` | Explicitly no matching customers. Return an empty scoped result. |
| `customer_ids=(1, 2)` | Restrict the entire result to those customers. |

An empty selection list in the saved filter configuration means unrestricted.
That is different from the resolved empty tuple. Never write `if customer_ids:`
when deciding whether to apply a scope filter; use `if customer_ids is not None:`.

Scope must reach **rows, totals, counts, charts, history and exports**, not just the
visible table. Preserve intended frozen ownership in historical metrics such as
U2C rather than substituting today's customer ownership.

### Seller filtering is not seller display

Use `instance.group_by_seller`, not the presence of seller data or a selected seller,
to decide whether to show seller identity.

- Default widgets show no seller names, badges, columns or labels.
- A seller filter, seller mode, or a one-seller result does not enable grouping.
- Build groups with `group_rows_by_seller()`. Use the
  [shared seller header](../templates/partials/widgets/_seller_group_header.html)
  once per group, not seller badges repeated in every customer row.
- Set `seller_group_columns` to the actual column count. Sort data inside each
  group and retain separate identities for sellers with the same name.

### Adding a new kind of filter

Do not merely add a string to `supported_filters`. Extend normalization,
validation, scope resolution, catalogue/instance metadata, the Filters controller,
draft persistence, preview context, full-report URL parsing and tests as applicable.
Reject malformed values explicitly and keep permanent scope distinct from temporary
presentation choices.

## Make data loading reusable

Put business logic and saved-data reads in [services](../app/services). Reports,
widgets and SalesIQ tools should call the same calculations rather than copying
qualification rules into routes, templates or JavaScript.

Use [request_cache.py](../app/services/request_cache.py) for expensive shared reads.
The current Fabric overview illustrates the pattern:

```python
from app.services.request_cache import request_cached

@request_cached
def get_fabric_capacity_overview() -> dict:
    """Load the shared saved capacity and workload overview."""
    # Keep the existing query/assembly implementation in this one loader.
```

The snippet shows where the decorator belongs; it is not a replacement function
body. See the actual implementation in
[fabric_consumption.py](../app/services/fabric_consumption.py). Expansion, Trial to
Paid and RTI all call that **same function**. Connect team/HVA coverage similarly
share `_load_milestones()` in [connect_goals.py](../app/services/connect_goals.py).

Rules for a shared loader:

1. Give it a meaningful dataset name and explicit inputs. Reuse an existing loader
   instead of creating a second function with equivalent SQL.
2. Arguments must be hashable. Use dates, IDs and normalized tuples rather than
   lists/dictionaries. Include every input that changes the result: scope, period,
   source/import identity, or other semantic choices as applicable.
3. Keep call conventions consistent. The current key is the function, positional
   arguments and sorted keyword arguments; omitted/default/positional forms are
   not automatically canonicalized into one key.
4. Treat returned dictionaries, lists and ORM objects as read-only. Copy before
   filtering, sorting in place, changing destinations or adding presentation fields.
5. Do not mix reporting windows, denominators or null semantics just to share a query.
   Sharing an unrestricted base dataset is valid only if each consumer reliably
   applies its required scope before producing any output.
6. Read only the required columns and records. Prefer SQL aggregation, projection
   and eager relationship loading over large ORM loads or per-row lookups.
7. Never sync a remote source in a provider, loader, template, or report GET.

`get_dashboard_data()` establishes `shared_read_scope()`. Reuse lasts only for that
render and is released even if rendering fails. Outside that explicit scope, without
a Flask request, or during write requests, decorated reads execute normally.
Standalone reports and workers therefore do not acquire a persistent stale cache.
Exceptions propagate and are not cached.

Return structured presentation data from the provider. Do not defer query execution
or lazy relationship traversal to Jinja rendering.

Do not open a global cache, keep a SQLAlchemy session across requests, or run
providers concurrently against a shared session.

**Summary is not automatically cheaper.** Providers currently receive reference
date and customer scope, not a variant argument, and can serve both presentations.
Keep their shared data lean. If a new widget genuinely requires variant-specific
loading, extend the common loader/provider contract and tests explicitly rather
than inventing an unsupported argument or omitting fields.

## Gate configuration and show sync progress

Use the common widget frame and its existing controllers. Do not build a separate
polling loop, fake progress bar, or sync-on-render implementation inside a widget.

### Reusing a source

Declare the widget's ordered dependencies in `WIDGET_SOURCES` in
[dashboard_sync.py](../app/services/dashboard_sync.py). Reuse the existing source
keys and operations. Two widgets backed by the same sync should join the same job,
not create duplicate work.

The common flow is:

1. Check declared setup and available choices.
2. If setup choices must first be imported, load those prerequisites.
3. Once choices exist, require confirmation before remaining source syncs.
4. Queue or join the next missing source, then show in-widget progress.
5. Refresh the widget when ready without publishing a draft or changing its layout.

Progress supports measured percent or an indeterminate state. Failures remain
visible, with explicit retry and a Sync Status link. While preparing, queued or
running, show only progress and the automatic-update message, not sync actions.
Show the actions after an error and hide them again when retry begins.
A successful empty import is
ready; a missing, skipped, partial or invalid-for-period import is not automatically
equivalent to valid empty data. Do not trigger an endless resync for zero results.

### Configuration requirements are not fully generic yet

`WidgetDefinition.requirements` references loaders in `WIDGET_REQUIREMENTS`.
The current setup implementation is **compensation-buckets-specific**: readiness
uses `available_buckets`, and the shared setup UI uses fiscal-year confirmation
and the existing Connect Goals save flow.

SE onboarding collects an explicit specialty (including Other) and saves it with
annual bucket confirmation when the user finishes or closes an edited setup review.
The exit action stays disabled until both a specialty and at least one available
bucket are selected, with a hover and keyboard-focus explanation. Failed or empty
revenue imports must be retried before an SE can finish bucket setup. An unchanged
review does not write preferences. Bucket confirmation uses the same preference
endpoints and records the current fiscal year and taxonomy version.
Sellers do not need SE specialty or compensated
bucket choices: the shared setup check uses all available buckets without changing
their stored SE preferences. Fiscal-year support and source readiness still apply.

Reuse `'compensation_buckets'` only when that is genuinely the requirement.
For a different prerequisite, extend the readiness state, form renderer, validated
load/save endpoints, controller and refresh tests together. Registering a new
requirement name alone will not create a working configuration flow. Do not force
unrelated settings into the bucket-selection schema.

Presentation variants, scope filters and required settings are different things.
`DashboardWidget` has no generic widget-settings field today, and its `filters`
schema only accepts supported scope selections. For new per-widget options,
design validated persistence, migrations if needed, editor controls, draft
round-tripping and preview context together; do not hide arbitrary settings in
`filters` or browser-only state and claim they are saved.

### Introducing a genuinely new sync source

**Required for every new report or widget import:** integrate with the central
durable sync system before shipping. This applies even to manual-only imports and
reports without a dashboard widget. Do not create a report-specific background
thread, independent job table, direct importer HTTP endpoint, or separate progress
and retry system. HTTP launchers and schedulers admit or join central jobs; the
worker performs the remote reads and writes.

Follow an existing complete implementation rather than just adding `WIDGET_SOURCES`:

- Add the saved model/import evidence and any idempotent migrations/indexes.
- Implement validated ingestion and atomic publication of a usable snapshot.
  Retain the previous complete data if a refresh fails.
- Register the operation, handler dispatch, resource conflicts, prerequisites,
  options and recovery behavior in [sync_jobs.py](../app/services/sync_jobs.py).
  Handlers run through [job_queue.py](../app/services/job_queue.py), not web threads.
- Extend `SOURCE_MODELS`, `SOURCE_OPERATIONS` and/or readiness checks as appropriate;
  source aliases, completion scope and period validation must match the real operation.
- Add the operation to the [Sync Status console](../app/routes/jobs.py).
- If automatic refresh is required, wire specialty/freshness-aware scheduling in
  [scheduled_sync.py](../app/services/scheduled_sync.py) and its worker startup hook.
- Use the normal progress/heartbeat facilities. Respect active jobs, retry cooldowns,
  resource locks and recovery rules.
- Test that manual/report launchers, widgets and scheduled triggers reuse the same
  active job, do not execute imports inline, and expose persisted progress, failures
  and completion through the common APIs.

Existing reference integrations:

| Operation | Sources owned by the central worker |
|-----------|-------------------------------------|
| `fabric` | Fabric 360 telemetry and revenue, plus P-to-F renewals. `fabric-revenue` is an on-demand partial refresh sharing the Fabric resource lock. |
| `sql-motions` | Azure Migrations renewal data and ADS Accelerate Arc SQL-version discovery, published as one complete SQL snapshot. Both SQL reports/widgets reuse this operation. |

Both full imports appear in Sync Status and have startup/five-minute freshness
checks for eligible Data SEs and sellers, with a seven-day refresh interval and
one-hour failed-attempt cooldown. Reuse these operations rather than adding a
second sync for another view of the same sources.

### Specialty-based features and scheduled syncs

**Sellers receive everything for every SE specialty**, including tabs, reports,
widgets, APIs, configuration access and automated syncs. A seller's saved
`se_specialty` is retained for switching back to SE mode; it must not limit seller
eligibility.

Use `has_specialty_access(specialty)` from
[se_specialty.py](../app/services/se_specialty.py), or `is_data_se()` for Data.
These helpers include `user_role == 'dss'` regardless of the saved specialty.
Do not use a raw `se_specialty == 'data'` check or require `user_role == 'se'`
for a Data-specialty automatic import.

For every specialty-based recurring import:

- Recheck the common eligibility helper on each scheduler cycle, before admission.
- Recheck eligibility when a queued **scheduled** job begins, before remote reads.
  Role/specialty may have changed while the job waited. Skip ineligible scheduled
  work explicitly; do not silently mark a source as imported or gate manual work
  solely because automatic scheduling is unavailable.
- Keep Sync Status schedule metadata, report status copy and actual scheduling
  consistent. Feature access and scheduling must not disagree for sellers.
- Preserve source-specific account prerequisites, freshness intervals and failed
  attempt cooldowns; specialty eligibility does not bypass them.
- Test the matching SE specialty, each nonmatching SE specialty, and sellers with
  every saved specialty. Cover role/specialty changes between admission and
  execution, no duplicate active jobs, and no remote work from report/widget GETs.

For Fabric and SQL Motions specifically, Data SEs and **all sellers** receive
weekly refreshes with startup catch-up. Infra, Apps and AI, and Other SEs do not
receive those automatic imports. This policy is independent of milestone
auto-sync settings.

The common sync endpoint is
`POST /api/reports/dashboards/widgets/<key>/sync`. It accepts only a boolean `retry`
option, checks setup/readiness, and admits the next missing operation. The common
setup loader is `GET /api/reports/dashboards/requirements/<key>`.

## Build the presentation

Create a partial under [templates/partials/widgets](../templates/partials/widgets).
The common frame handles gates and supplies `instance`, including:

| Field | Use |
|-------|-----|
| `instance.data` | Provider result for a ready widget. |
| `instance.variant_key` | Usually `'metric'` or `'detailed'`. |
| `instance.group_by_seller` | Explicit presentation choice. |
| `instance.filters` / `filter_info` | Validated scope and capability information. |
| `instance.requirements` / `sync` | Common setup/sync gate, not a second local controller. |
| `widget_id_prefix` | Unique IDs, ARIA references and sort identity per placement/preview. |

Provider result shapes differ between widget families; there is no universal
mandatory `rows`/`details` schema. Choose a clear shape that the widget template,
full report and tests agree on. Keep source-not-loaded, unavailable measurement,
known zero, and ready-but-no-matching-rows distinct.

Presentation rules:

- Include a readable title, concise purpose/period context, and a Full report link.
- Summary should prioritize a few useful metrics; Details should expose the work queue.
  Any deliberate cohort difference or top-N cap must be clear and tested. Do not
  calculate portfolio totals from only the displayed top rows.
- Keep essential uncertainty visible, but avoid repeated boilerplate such as
  "Source notes in full report." Put detailed provenance/quality notes in the report.
- Use existing native report/widget styles, semantic positive/danger colors, and
  accessible labels. Do not encode meaning with color alone.
- Use internal scrolling for Details tables. Keep the rightmost header and data
  cells at least **16px** from the vertical scrollbar; a scrollbar gutter is not padding.
- Use unique sortable-table keys, raw numeric/date sort values, and stable defaults.
  If columns change meaning/order, version the sort key so persisted indices stay safe.
- Grouped headings must be fixed during sorting. Full-report exports must preserve
  group labels and avoid accidentally including hidden evidence in summary cells.
- Avoid nested evidence tables that contaminate the parent sort/export selectors.
- Reuse row/header partials where report and widget columns truly match. Do not
  simplify an extracted report partial and accidentally drop its existing behavior.
- Multiple copies and injected previews must work. Do not rely only on
  `DOMContentLoaded` or assume scripts assigned through `innerHTML` execute.
  Integrate interactive behavior with the existing fragment mounting/refresh lifecycle.
- Desktop Electron is the target. Preserve the current desktop viewport and user
  theme; do not add mobile adaptations or change persisted theme for testing.

## Link to an actionable full view

The widget is the entry point, not the end of the workflow.

Prefer an existing report preset when it already provides the necessary review
workflow, rather than building another competing report. Database Expansion, for
example, opens Revenue Analyzer with its database-specific view and review actions.

The destination should:

- Open the relevant population, period and ordering, not an unrelated report landing page.
- Offer useful next steps: open a customer/engagement/opportunity/milestone, inspect
  source evidence, assess a workload, or use an existing review/logging workflow.
- Preserve the dashboard's resolved scope where supported. Use
  `scope_report_destination()` and `report_customer_scope()` rather than concatenating
  an unvalidated list of IDs. An explicit empty scope must stay empty.
- Preserve the user's unrelated saved report preferences. A linked preset should
  not silently overwrite their normal default view.
- Keep writes explicit through existing POST flows; opening or filtering a report
  must not modify customer records or mark signals reviewed.

Scope-preserving destinations currently use an explicit widget-key allowlist in
[dashboard_filters.py](../app/services/dashboard_filters.py). A new URL parameter
does not make a report scoped: implement parsing, calculations, rendering and tests
end to end before adding its key. If a destination is intentionally broader,
make that limitation clear instead of implying scope parity.

## Wire all relevant surfaces

| Surface | When to change it |
|---------|-------------------|
| [Widget catalogue](../app/services/dashboards.py) | Every new widget: identity, provider, variants, filters, requirements and access. |
| [Domain service](../app/services) | Shared report/widget calculations and named data loaders. |
| [Source readiness](../app/services/dashboard_sync.py) | Widget needs saved source imports or a new operation. |
| [Report routes](../app/routes/reports.py) | New full report/API or a scoped preset. Use `/reports/` and `/api/reports/`. |
| [Reports navigation](../templates/base.html) and `reports_hub()` | Add every new report to `navReports` and the hub's `report_groups`. |
| [Breadcrumbs](../app/services/breadcrumb_reports.py) and [help](../static/js/page-help.js) | Add the report's navigation context and useful explanations. |
| [SalesIQ tools](../app/services/salesiq_tools.py) | New queryable report/entity: a thin `@tool` wrapper over the same service, with access checks. |
| [Dashboard templates](../app/services/dashboard_templates.py) | Only when asked to ship/update a starter. Preserve user-owned dashboards and installation markers. |
| [README](../README.md) and [changelog](../CHANGELOG.md) | Document the delivered capability and user-visible behavior. |

Reports that merely analyze revenue belong in the reports blueprint, not the
revenue-management blueprint. Ordinary widgets do not need new schema: saved
placements already store their key, geometry, variant and filters.

## Verify before handing it over

Use isolated pytest data for automated tests and actual live dev data for browser
validation. Run scoped files, never the entire suite as part of widget development.

- [ ] Provider/report/tool use the same qualification, totals, periods and null semantics.
- [ ] No source data, successful empty import, zero, missing measurement and failed sync
  have distinct, truthful states.
- [ ] Customer and seller filters affect all outputs; disjoint selections and `()` stay empty.
- [ ] Grouping off shows no seller identity; grouping on uses shared headers without duplication.
- [ ] Full-report click-through applies the intended preset/scope and provides a useful next step.
- [ ] Setup blocks calculation/sync in the right order and confirmation refreshes dependents.
- [ ] Shared operations join active work; failures require explicit retry; progress is real.
- [ ] Both variants and every advertised width work, including real library previews.
- [ ] Duplicate placements have independent IDs, sort state and filter context.
- [ ] Internal scroll, right padding, sorting, grouped exports and long labels remain usable.
- [ ] Shared loaders run once for identical inputs, never mix scopes, and leave cached data
  unmodified. A new render, write request or failed render cannot retain stale data.
- [ ] Profile representative dashboards before/after query changes. Record repeat-run
  medians, query counts and output parity; do not judge performance from one cold request.
- [ ] Source refresh and previews preserve saved widget IDs, layout revision, filters and drafts.
- [ ] New reports appear in navigation/hub, breadcrumbs/help and SalesIQ coverage.
  Update `test_tool_coverage` for a new report/entity and catalogue expectations for
  a new widget; retain checks rather than loosening them.

Relevant scoped commands, selecting only files affected by the change:

```powershell
& C:\dev\SalesBuddy\venv\Scripts\Activate.ps1
pytest tests\test_dashboards.py tests\test_dashboard_filters.py tests\test_dashboard_sync.py tests\test_dashboard_requirements.py tests\test_request_cache.py
pytest tests\test_salesiq_tools.py
node --test tests\js\ui_dashboard_filters.test.cjs tests\js\ui_dashboard_sync.test.cjs tests\js\ui_dashboard_requirements.test.cjs
```

Add the domain's own report/widget tests. For browser checks use the live dev server
at `http://127.0.0.1:5000`, started/restarted with `scripts\dev.ps1`, not the installed
production app on port 5151 or a fabricated demo dataset.

## Working examples and file map

| Need | Start with |
|------|------------|
| Straightforward report-backed provider, Summary/Details and seller grouping | [RTI service](../app/services/rti.py), [RTI widget](../templates/partials/widgets/rti.html), [RTI tests](../tests/test_rti.py). |
| Two widgets sharing one atomic source sync, with scoped full reports | [SQL service](../app/services/sql_motions.py), [SQL sync](../app/services/sql_motions_sync.py), [SQL report](../templates/report_sql_motion.html), [SQL tests](../tests/test_sql_motions.py). |
| Shared query/assembly reuse across different widgets | [Fabric service](../app/services/fabric_consumption.py), [cache helper](../app/services/request_cache.py), [cache tests](../tests/test_request_cache.py). |
| Scope-sensitive shared milestone population and historical ownership | [Connect Goals](../app/services/connect_goals.py), [filter tests](../tests/test_connect_goal_filters.py). |
| Existing actionable report preset rather than a new report | `get_database_expansion_widget()` in [Revenue Analyzer](../app/services/revenue_analysis.py). |
| Setup, background sync, progress and refresh | [Common frame](../templates/partials/dashboard_widget_content.html), [sync controller](../static/js/ui-dashboard-sync.js), [setup controller](../static/js/ui-dashboard-requirements.js), [readiness tests](../tests/test_dashboard_sync.py), [setup tests](../tests/test_dashboard_requirements.py). |
| Real-data library previews and persistent editor behavior | [Library controller](../static/js/ui-dashboard-library.js), [draft controller](../static/js/ui-dashboard-drafts.js), [layout tests](../tests/test_dashboard_layout.py). |

Keep this guide and its linked contracts accurate when extending the framework.
Do not copy a widget's business-specific exceptions into every new widget.
