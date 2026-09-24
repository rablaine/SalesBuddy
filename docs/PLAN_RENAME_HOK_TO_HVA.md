# Plan: Rename HoK to HVA Across Sales Buddy

**Status:** Proposed
**Last updated:** 2026-09-23

## Goal

Replace the user-facing and internal Sales Buddy term **HoK**
(Hands-on-Keyboard) with **HVA** (High-Value Activity) everywhere.

After this change, Sales Buddy should consistently call qualifying technical
activities HVAs. New code, UI, documentation, tests, exports, SalesIQ tools, and
integrations should use HVA terminology.

## Scope

This is a codebase-wide terminology and symbol migration, not a label-only
change.

The current repository contains approximately 300 case-sensitive HoK references
across about 35 files, including:

- Models and persisted field names.
- Database migrations.
- MSX API parsing and activity-category logic.
- Activity Coverage services and templates.
- Note creation.
- Milestone tasks.
- SalesIQ tools and ontology.
- Tests.
- Documentation.
- Changelog history.
- Help text and JavaScript.

Before implementation, run a fresh repository-wide search for:

- `HoK`
- `HOK`
- `hok`
- `Hands-on-Keyboard`
- `hands-on-keyboard`
- User-visible phrases such as `hands-on work`

Review every result, including comments, docstrings, tests, scripts, templates,
configuration, and documentation.

## Terminology

Use:

- **HVA** in headings, labels, badges, and abbreviations.
- **High-Value Activity** when spelling out the term.
- `hva` in Python, JavaScript, API, and database identifiers.
- `is_hva` for the qualifying-activity Boolean.
- `HVA_TASK_CATEGORIES` for the accepted category-code set.

Do not retain HoK aliases in user-facing copy.

## Data migration

The current database includes persisted HoK identifiers such as
`msx_tasks.is_hok`. The rename must preserve existing user data.

Preferred migration:

1. Add the new `is_hva` column idempotently.
2. Copy every existing `is_hok` value into `is_hva`.
3. Update models, services, routes, templates, APIs, backups, and tests to use
   `is_hva`.
4. Keep the old column only as a temporary migration artifact if SQLite
   compatibility makes safe removal disproportionate.
5. Stop reading and writing the old column immediately after migration.

If the old column remains physically present, document it as a retired storage
artifact. It must not remain in application symbols, API payloads, UI copy, or
active query logic.

Update backup and restore mappings so:

- Old backups containing `is_hok` import safely into `is_hva`.
- New backups export HVA terminology.
- Restore remains idempotent.

## API and integration compatibility

Audit all internal and external payloads containing HoK keys.

For persisted or externally consumed payloads:

- Prefer a versioned migration to `is_hva`.
- Accept a legacy `is_hok` input only where required to restore old backups or
  process an older queued record.
- Emit only the new `is_hva` field.
- Do not maintain indefinite dual-write behavior.

MSX may use its own activity names or category codes. Do not alter official MSX
values. Rename only Sales Buddy's classification and presentation of those
activities.

## Code changes

Rename all relevant symbols, including examples such as:

- `HOK_TASK_CATEGORIES` to `HVA_TASK_CATEGORIES`.
- `is_hok` to `is_hva`.
- `hok_tasks` to `hva_tasks`.
- `hok_covered` to `hva_covered`.
- `hok_percent` to `hva_percent`.
- `hok_task_categories` to `hva_task_categories`.
- HoK-specific functions, serializers, response keys, CSS hooks, IDs, test
  fixtures, and local-storage keys.

Use semantic rename tooling where possible, then perform a complete text search
to catch template strings, payload keys, comments, and documentation.

## UI and report changes

Update all affected user experiences:

- Activity Coverage headings, summaries, filters, empty states, and actions.
- CAIP coverage.
- Milestone activity drafts.
- Note form and meeting enrichment copy.
- Milestone task badges and descriptions.
- SalesIQ answers and tool descriptions.
- Page help.
- Exports and downloaded reports.

The rename must not change which activities qualify. Category semantics remain
unchanged unless a separate requirements decision explicitly changes them.

## Documentation and historical text

Update active documentation to HVA terminology.

For changelog entries:

- Preserve the historical meaning but update terminology so current users do
  not encounter an obsolete product term.
- Do not alter tagged merge hashes or entry structure.

## Tests

Update all affected test names, fixtures, request payloads, response assertions,
and documentation assertions.

Required focused coverage includes:

- Existing `is_hok` rows migrate to `is_hva`.
- Old backup data restores correctly.
- New backups use HVA keys.
- Qualifying category behavior remains unchanged.
- Activity Coverage calculations remain identical.
- Activity creation marks the correct HVA classification.
- SalesIQ tools return HVA terminology.
- No user-facing HoK copy remains.

Run the existing scoped test files covering:

- Activity Coverage.
- Milestone tasks.
- MSX integration.
- Milestone Tracker.
- Backup and restore.
- SalesIQ tools and ontology.
- Notes and meeting enrichment where affected.

Do not run the full test suite.

## Final verification

Search the entire repository again for every old form:

- `HoK`
- `HOK`
- `hok`
- `Hands-on-Keyboard`
- `hands-on-keyboard`

Any remaining match must be either:

- A deliberate legacy database or backup compatibility reference.
- External MSX terminology that Sales Buddy does not control.

Document each intentional survivor. All other matches must be removed or
renamed before completion.

## Delivery

This is user-visible terminology, API shape, and persisted-data behavior.

- Add a changelog entry.
- Implement on a feature branch.
- Validate migration and backup compatibility.
- Ask the user to test Activity Coverage and activity creation.
- Commit and push the feature branch.
- Merge only after explicit user approval.
