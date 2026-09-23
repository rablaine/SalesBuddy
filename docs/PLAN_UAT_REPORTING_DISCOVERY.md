# Plan: Investigate UAT Reporting Data

**Status:** Deferred discovery
**Last updated:** 2026-09-23
**Current scope:** Explicitly out of scope for the Connect Goals Action Center

## Goal

Determine whether Sales Buddy can read enough reliable Unified Action Tracker
(UAT) data to report actionable UAT submissions attached to milestones where
the user is on the milestone team, grouped by fiscal quarter.

The submitter does not need to be the current user. This investigation must
establish the data source, milestone relationship, team scope, reporting
completeness, and access requirements before any UAT metric is proposed for
the application.

## Known context

- The Connect goal asks for three actionable UAT submissions per quarter.
- UAT means **Unified Action Tracker**, not User Acceptance Testing.
- UAT Actions are formal deal-assistance requests associated with customer work.
- At least some UAT flows begin from an MSX milestone under Deal Assistance.
- Azure AI capacity requests are one known UAT category.
- UATracker has its own authenticated API and request identifiers.
- Available tooling can search and retrieve UAT Actions, but that does not yet
  prove that Sales Buddy can enumerate every relevant action and join it to an
  MSX milestone.

## Non-goals

This discovery does not:

- Add UAT to the Connect Goals Action Center.
- Add a UAT widget, report, navigation item, or SalesIQ tool.
- File, modify, replay, close, or otherwise write a UAT Action.
- Modify MSX milestones or opportunities.
- Assume Azure AI capacity requests are the only qualifying UAT category.
- Store UATracker access tokens, customer subscription IDs, or other sensitive
  request data in the repository.
- Treat a partial or inferred count as an official metric.

Any application implementation requires a separate approved plan after this
investigation succeeds.

## Questions the investigation must answer

### Source of truth

1. Is the authoritative reporting source UATracker, MSX Dataverse, a Power BI
   dataset, or a combination?
2. Does one source expose all UAT categories, or only a specialized view such
   as Azure AI capacity?
3. Are historical actions retained long enough to report the current and prior
   fiscal quarters?
4. Is there a stable UAT Action ID that can deduplicate records across sources?

### Milestone and team scope

1. Does every qualifying UAT Action include a stable MSX milestone GUID or
   milestone number?
2. Can that identifier be joined reliably to the locally synchronized
   milestone?
3. Is the local milestone `on_my_team` state sufficient and current enough for
   reporting?
4. Can current team membership be confirmed directly from MSX when the local
   milestone cache is stale?
5. How should a UAT Action with a missing, deleted, inaccessible, or unmatched
   milestone be represented?

The intended scope is the user's **current milestone team membership**.
Submitter, requestor, creator, owner, and assignee identity do not determine
whether the action counts.

### Metric definition

1. Which UAT Action categories count toward the three-per-quarter goal?
2. Does submission alone count, or must an action reach an accepted, assigned,
   completed, or otherwise actionable state?
3. Which date controls the fiscal quarter: created, submitted, accepted, or
   completed date?
4. Do cancelled, rejected, duplicate, or withdrawn actions count?
5. Can one customer issue produce multiple qualifying actions?

These definition questions require manager or program-owner confirmation even
if the technical data is available.

### Application feasibility

1. Can the data be queried read-only with the authentication already available
   to Sales Buddy?
2. Does access require separate UATracker consent or short-lived tokens?
3. Can the UAT inventory be joined safely and completely to the user's on-team
   milestones?
4. Is the source reliable and fast enough for an on-demand report?
5. Can the result link back to the UAT Action, MSX milestone, opportunity, or
   account without exposing sensitive request details?

## Read-only probe sequence

### Phase 1: Establish representative records

1. Identify one or more known UAT Actions attached to milestones where the user
   is currently on the milestone team.
2. Record only the minimum comparison fields needed for discovery:
   - Action ID.
   - Category.
   - Created and submitted dates.
   - State.
   - Related milestone, opportunity, and account identifiers when present.
3. Do not copy descriptions, subscription IDs, quota amounts, customer secrets,
   or authentication tokens into planning artifacts.

If the user has no known action, start from a small set of known on-team
milestones and search for related UAT Actions. Stop if the relationship cannot
be confirmed from stable identifiers.

### Phase 2: Probe UATracker

Use read-only search, get, and status operations to determine:

- Whether milestone-guided filtering or lookup is supported.
- Which milestone, category, state, and timestamp fields are returned.
- Whether all action categories appear in the same search surface.
- Whether pagination, retention, and state history are available.
- Whether actions expose stable MSX relationship identifiers.

Do not invoke submit or replay operations.

### Phase 3: Probe MSX Dataverse

Use schema discovery and read-only queries to determine whether MSX stores:

- UAT Action identifiers.
- Deal Assistance request records.
- Relationships from requests to milestones and opportunities.
- Category, state, and submission dates.
- The user's current milestone team membership.

Do not assume the MSX milestone's `help needed` fields prove that a UAT was
actually submitted.

### Phase 4: Probe reporting datasets

Determine whether an accessible Power BI dataset provides a broader historical
UAT inventory than the transactional APIs.

Validate:

- Dataset scope and category coverage.
- Refresh cadence.
- Stable milestone identifiers and team-scope feasibility.
- Fiscal-quarter timestamps.
- Stable IDs for comparison with UATracker and MSX.

Treat a category-specific view, such as an Azure AI capacity triage view, as
partial until broader coverage is proven.

### Phase 5: Reconcile the sources

For the representative records:

1. Compare presence across UATracker, MSX, and reporting data.
2. Confirm that Action ID, milestone ID, date, category, and state agree.
3. Identify missing records, delayed refreshes, duplicates, unmatched
   milestones, and team-membership drift.
4. Calculate a sample fiscal-quarter count from the authoritative source.
5. Compare that count with the known actions on the user's on-team milestones.

## Feasibility gate

UAT reporting is feasible only if the investigation proves all of the
following:

- The source includes every qualifying UAT category or the official goal is
  narrowed to the categories it includes.
- Actions expose a stable milestone identifier that joins to the user's
  currently on-team milestones.
- A defensible fiscal-quarter date and inclusion-state rule are available.
- Duplicate, cancelled, and reassigned actions can be handled deterministically.
- The source is accessible read-only without committing credentials or
  requiring unsafe token handling.
- The count can be reproduced from source records and explained to the user.

If any gate fails, document the limitation and do not display a score,
attainment color, or progress bar.

## Possible future implementation

Only after the feasibility gate passes, prepare a separate implementation plan
for:

- A read-only UAT reporting service.
- Current-quarter submitted and qualifying counts.
- Target progress against three per quarter.
- Category and state breakdowns.
- Links to source records where permitted.
- Accessible provenance and freshness explanations.
- Tests covering fiscal-quarter boundaries, milestone team scope,
  deduplication, state rules, unmatched milestones, unavailable data, and
  authorization failures.

The future implementation must remain independent from the current Connect
Goals Action Center work until the user explicitly brings it back into scope.
