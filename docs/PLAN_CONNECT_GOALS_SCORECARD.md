# Plan: Connect Goals Action Center and Future Custom Reports

**Status:** Proposed
**Last updated:** 2026-09-23
**Reference:** `Connect.html` in the repository root

## Goal

Build a Connect Goals Action Center that answers two questions:

1. How am I doing against the goals Sales Buddy can help with?
2. What can I do in Sales Buddy right now to improve the result?

This is not just a scorecard. It is a compact, goal-oriented launch point for
reports and workflows that help the user act.

Each widget should:

- Summarize one actionable goal.
- Show the most useful current signal, trend, or gap.
- Explain what needs attention.
- Open a full report that supports investigation and action.
- Preserve context when moving from the widget to the report.
- Reflect improvements after the user completes the work.

The U2C workflow is the model:

1. The widget shows current committed percentage, the 40% target, and quarter
   progression.
2. The widget identifies whether attainment is on pace and how much remains.
3. Clicking it opens U2C Attainment with the same workload and seller context.
4. The full report identifies uncommitted milestones.
5. The user adds relevant milestones or engagements to seller 1:1 agendas with
   a discussion note.
6. Future official refreshes show whether the work progressed.

Every included goal should have a similarly complete loop. A number without a
useful next action does not belong on this page.

## Scope rule

Only include goals where Sales Buddy can actively assist.

A goal qualifies when the app can do at least one of the following:

- Identify records that need attention.
- Help prioritize the next action.
- Open or provide the workflow needed to act.
- Prepare an evidence-backed customer or manager conversation.
- Track whether the underlying work progressed.
- Draft a grounded deliverable from records already in Sales Buddy.

Do not include goals merely because they appear in Connect. If Sales Buddy
cannot help the user make progress, the goal stays out of this report.

## Explicitly excluded

The first release will not include:

- Official agent DAU.
- Training completion deadlines.
- Certifications.
- Learning hours.
- UAT submissions.
- Compliance and trust.
- Stage 4 consumption plan coverage.
- Published success-story attainment.
- DCSA or partner collaboration scoring.
- Opportunity influence beyond the milestone-team coverage metric.
- Frontier or agent-use scoring.
- Composite territory-execution scoring.

These goals do not currently have an actionable, defensible Sales Buddy
workflow. They should not appear as manual cards, unavailable cards, or
placeholders.

If a useful integration or workflow is added later, the corresponding goal can
be reconsidered.

UAT reporting has a separate deferred discovery plan in
`docs/PLAN_UAT_REPORTING_DISCOVERY.md`. It remains out of scope for this Action
Center work unless that investigation succeeds and the user explicitly reopens
it.

## Relationship to existing reports

This new page will not replace:

- U2C Attainment.
- Activity Coverage.
- Connect Impact.
- 1:1 Manager / SE Report.
- Persistent 1:1 workspaces.
- Connect Export.
- Whitespace Analysis.
- Revenue Analyzer.
- MSX Workspace.

The Action Center is the overview and routing layer above them.

| Surface | Purpose |
|---|---|
| Action Center widget | Show the goal highlight, gap, and recommended action |
| Backing report | Explain the result and identify records requiring work |
| Action workflow | Let the user do something about those records |
| Existing entity pages | Hold detailed customer, engagement, milestone, and opportunity context |

Where an existing report already supplies the necessary investigation and
actions, reuse it. Where it only supplies data, extend it with the missing
workflow. Where no report exists, build a focused report rather than cramming a
large table into the widget.

## Product shape

### Fixed page now, reusable widgets from the beginning

The first release is an opinionated Connect Goals page with a fixed set and
order of widgets. Users will not configure or rearrange it yet.

The implementation should still treat each goal as a reusable widget:

- One data provider.
- One summary presentation.
- One destination report.
- Zero or more contextual actions.

This gives the future custom report system real, proven widgets without making
the first release wait for a page builder.

### Not a uniform card grid

The page should feel like a concise execution brief, not a generic dashboard.

- Use a denser lead section for the highest-priority goals.
- Give trend-oriented widgets more horizontal space.
- Use compact exception lists for goals driven by uncovered records.
- Make the entire widget heading or clear call to action open the report.
- Show the next useful action in plain language.
- Avoid decorative metrics, repeated icons, and identical card dimensions.
- Support both existing light and dark themes using Bootstrap-native patterns.

### Widget information hierarchy

Every widget should provide:

1. **Goal:** the outcome the widget supports.
2. **Signal:** the current result or most important highlight.
3. **Visual:** a meaningful graph or visual summary when the backing data
   supports one.
4. **Direction:** trend, pace, coverage gap, or change over time.
5. **Attention:** the records or conditions holding the result back.
6. **Action:** what the user can do next.
7. **Destination:** the report or workflow that supports the action.
8. **Freshness:** when the source was last updated.

The widget is not expected to reproduce the full report. It should show enough
to decide whether opening the report is worth the user's attention.

### Report-backed visuals

Use graphs and visual summaries whenever the report already has meaningful data
for one. The widget should render a compact form of the same evidence, not a
decorative chart added to make the page look busy.

Examples:

- U2C includes the existing quarter conversion progression graph for the scope
  derived from the user's current-FY prioritized buckets.
- A coverage widget may use a covered-versus-uncovered bar when that comparison
  is more useful than a trend.
- A whitespace widget may show separate Fabric and database progress when both
  series have meaningful definitions.

Do not force a graph onto every widget. A ranked exception list or coverage bar
is better when there is no meaningful time series.

Widget visuals and full reports must consume the same provider data so their
numbers cannot drift.

### Goal progress visual

Widgets with an explicit numeric target should share one compact progress
language:

- Display the current result as the widget's largest number.
- Place a thin horizontal progress bar directly below it.
- Fill the bar to the current absolute percentage or normalized goal progress.
- Draw a narrow white goal marker at the target position.
- Give the marker a contrasting outline or shadow so it remains visible in
  both light and dark themes.
- Label the target in text so the marker is not the only explanation.
- Color the large result and bar using the widget's attainment status.
- Include a text status such as `On pace`, `Watch`, or `Behind` so color is
  never the only signal.
- Give the value and bar an accessible explanation available on both mouse
  hover and keyboard focus.

For percentage goals, use a 0 to 100 bar. A 40% U2C goal therefore places the
goal marker at 40%, while the fill shows the actual U2C percentage.

For count goals, normalize the fill against the target. The goal marker sits at
100% of the normalized bar, while the large value shows the real count, such as
`1 of 2`.

Do not render a goal bar when the widget has no explicit target. Use the summary
shape that best explains that report instead.

### Time-aware attainment

Some goals, especially quarterly progression goals, should be judged against
where the user should be today rather than only against the final target.

For a linear period goal:

```text
period_progress = elapsed_days / total_period_days
expected_today = final_target * period_progress
pace_ratio = actual / expected_today
```

Example for a 40% quarterly U2C goal:

- Halfway through the quarter, expected U2C is 20%.
- Actual U2C of 20% is on pace and should display green.
- Actual U2C above 20% is ahead of pace.
- Reaching 40% at any point means the goal is achieved.

Use a stepped semantic scale:

- **Green:** achieved, or at least 100% of expected-to-date pace.
- **Yellow:** 80% to 99% of expected-to-date pace.
- **Orange:** 60% to 79% of expected-to-date pace.
- **Red:** below 60% of expected-to-date pace.

Protect the first day of a period from division by zero by treating elapsed
time as at least one day.

Time pacing is opt-in per widget. Current-state coverage goals, such as being on
50% of milestone teams, should compare directly with their target unless the
metric owner confirms that they are intended to ramp linearly through a period.

Keep the visible UI concise. Do not permanently print the expected-to-date
calculation under every metric.

Instead, the value and progress bar should expose a hover-and-focus explanation
such as:

> 20% committed. Green because 20% is on pace with the 20% expected by today.
> The final quarterly target is 40%.

This explanation must be available to keyboard and assistive-technology users,
not only through a mouse-only tooltip.

### Accessible values and visuals

Every widget value, progress visual, and chart needs a complete text
alternative.

- Use an accessible tooltip or popover triggered by both hover and focus for
  pace and color explanations.
- Connect the tooltip content with `aria-describedby`.
- Make non-interactive visual summaries keyboard-focusable when focus is needed
  to reveal their explanation.
- Give progress bars `role="progressbar"` with `aria-valuemin`,
  `aria-valuemax`, `aria-valuenow`, and a descriptive `aria-valuetext`.
- Give charts an accessible name and a concise summary of the trend, target,
  current value, and important dates.
- Provide a visually hidden text summary for canvas-based charts because canvas
  content itself is not a sufficient accessible representation.
- Ensure chart point details available on mouse hover are also reachable through
  a keyboard-accessible data summary or table.
- Do not use color alone to communicate status or series identity.
- Do not rely on HTML `alt` attributes for non-image widgets. Use semantic ARIA
  labeling and described-by text appropriate to the rendered element.

Example U2C chart summary:

> Data U2C rose from 0% on July 1 to 20% on August 15. The current result is on
> pace for the 40% September 30 target.

## Proposed goal widgets

The v1 widget set is limited to:

1. Quarterly U2C progression.
2. Milestone team coverage.
3. Milestone HoK coverage, including a qualifying technical-validation activity
   breakdown.
4. Fabric and database whitespace wins.

The remaining concepts later in this section are retained only as deferred
research notes. They are not approved v1 widgets or implementation phases.

### V1 scope: FY27 Data priorities

V1 implements the FY27 Data priorities and targets documented in the supplied
Connect. It does not attempt to infer or configure equivalent goals for other
solution areas.

Use one explicit, versioned FY27 Data scope:

- U2C workload prefix: `Data:`.
- Milestone Tracker area: `Data`.
- Activity Coverage area: `Data`.
- Whitespace revenue buckets: `Databases` and `Fabric`.

Keep this scope in one typed configuration used by all four providers. Do not
build a generic revenue-bucket-to-milestone-workload mapping for v1.

Users outside the FY27 Data scope should not be shown misleading calculations.
Supporting other roles requires a later, separately defined goal configuration.

### Annual prioritized-bucket prerequisite

Use the existing `UserPreference.compensated_buckets` selection as the source
of truth for confirming the user's FY27 Data revenue priorities. Do not create
a second solution-area preference for v1.

The selection contains MSXI `ServiceCompGrouping` buckets and is already:

- Editable on Revenue Import after revenue data is available.
- Saved primarily to `salesbuddy_revenue_bucket_filter` in local storage.
- Mirrored to `UserPreference.compensated_buckets` as a database fallback.
- Invalidated when MSXI bucket taxonomy changes through
  `bucket_taxonomy_version`.

The missing piece is fiscal-year confirmation. Buckets change year to year, and
the current model does not record which fiscal year or taxonomy version the
user confirmed.

Add these preference fields through an idempotent migration:

- `compensated_buckets_fiscal_year` `String(10)`, such as `FY27`.
- `compensated_buckets_confirmed_taxonomy_version` `Integer`.

Whenever any bucket picker saves a non-empty selection:

1. Persist the bucket list using the existing database field and local-storage
   key.
2. Stamp the current Microsoft fiscal year.
3. Stamp the current server-side `bucket_taxonomy_version`.
4. Clear the existing taxonomy notice.

The Action Center may render widgets only when:

- Both `Databases` and `Fabric` are selected.
- The saved fiscal-year stamp matches the current Microsoft fiscal year.
- The confirmed taxonomy version matches the current taxonomy version.
- Every selected bucket still exists in the imported revenue taxonomy.

If any condition fails, render a setup gate instead of the widgets.

### Setup gate

The setup gate should live at `/reports/connect-goals`; do not redirect the user
through an unrelated page before explaining why setup is required.

Reuse one shared prioritized-bucket picker partial and save service across:

- Action Center setup.
- Revenue Import.
- Onboarding.
- Any future Settings editor.

The gate should:

- Explain that Connect metrics depend on the user's annual workload priorities.
- Explain that v1 implements the FY27 Data priorities.
- Display the current fiscal year.
- Show the current MSXI bucket taxonomy in a searchable checklist.
- Require at least one selection.
- Save the selection and render the Action Center immediately.
- Offer a Revenue Import or sync action when no buckets are available yet.

After setup, show the active prioritized buckets near the Action Center title
with an Edit action. Editing uses the same shared picker.

Do not rely on local storage to decide whether the gate is satisfied. The
server-rendered route must use the database selection and its fiscal-year and
taxonomy stamps. This keeps the gate reliable across windows, cache clearing,
and browser fallback.

### Provider scope

Every v1 provider consumes the shared FY27 Data scope configuration. Do not
repeat `Data:`, `Data`, `Databases`, or `Fabric` across individual providers.

The configuration is intentionally specific rather than a claim that MSXI
revenue buckets can be translated generically into MSX milestone workloads.

### 1. Quarterly U2C progression

**Connect alignment**

- Drive milestone progress and technical decisions.
- Reach 40% uncommitted-to-committed each quarter across aligned data
  workloads.

**Widget**

- Current committed ACR percentage against 40%.
- The existing Data quarter-to-date progression graph.
- Remaining count and ACR.
- Pace indicator based on elapsed quarter, without inventing a forecast.
- Highest-impact seller or workload gap.

**Backing report**

- Existing U2C Attainment report.

**Action loop**

- Audit remaining uncommitted milestones.
- Filter by seller, customer, or workload.
- See whether each milestone is already linked to an engagement.
- Add the milestone or all linked engagements to the assigned seller's 1:1
  agenda with a custom discussion note.

**Status**

- Existing end-to-end workflow. This should establish the widget contract.

**Definition**

- Denominator: frozen starting ACR from the official quarterly U2C snapshot for
  the workload scope derived from the user's prioritized buckets.
- Numerator: snapshot ACR belonging to that same cohort that has since moved to
  Committed or Completed.
- Unit: dollars, not milestone count.
- Due-date scope: already defined by the official snapshot, which includes the
  quarter's eligible milestones.

### 2. Milestone team coverage

**Connect alignment**

- Maintain technical influence across at least 50% of the aligned territory.
- Engage early enough to influence milestone progression.

**Widget**

- Percentage of in-scope milestone ACR where the user is on the milestone team.
- On-team ACR and total in-scope ACR.
- On-team milestone count and total milestone count as supporting context.
- Highest-ACR milestones where the user is not on the team.
- Breakdown by seller or workload when useful.

**Working definition pending manager confirmation**

- Population: active FY27 Data milestones due in the current or next Microsoft
  fiscal year.
- Denominator: total milestone ACR across that population.
- Numerator: milestone ACR where the user is on the milestone team.
- Unit: ACR weighting, not milestone count.

**Backing report**

- Existing Milestone Tracker.

**Action loop**

- Open the tracker prefiltered to the user's prioritized workload scope, the
  agreed fiscal period, and milestones where the user is off team.
- Review the highest-priority uncovered milestones.
- Join the appropriate milestone teams using the existing MSX action.
- Reopen the widget with the updated count and percentage.

**Required report work**

- Before navigating, write the desired tracker filter state to the existing
  `salesbuddy_milestone_filters` local-storage key.
- Populate the existing `quarters`, `seller`, `status`, `area`, `urgency`,
  `myTeam`, `commitment`, `allQuarters`, and `favoritesOnly` fields.
- Navigate directly to `/reports/milestone-tracker` and let its existing
  restoration logic apply the state.
- Treat that filter state as the user's new tracker state. Do not add temporary
  restoration behavior or a parallel query-string system unless real usage
  demonstrates a need.
- Implement the current-plus-next-FY and ACR-weighted working definition.
- Keep the fiscal scope and weighting centralized so manager confirmation can
  adjust the calculation without rewriting the widget.

### 3. Milestone HoK coverage

**Connect alignment**

- Maintain 100% milestone activity tracking with emphasis on high-value
  activities currently labeled HoK in Sales Buddy.
- Log demos and Technical Workshops in MSX.
- Maintain consistent execution across active milestones.

**Widget**

- Percentage of in-scope, on-team milestones with at least one qualifying HoK
  in the relevant fiscal year.
- Covered and uncovered milestone counts.
- Counts for qualifying technical-validation HoKs, including demos, Technical
  Workshops, architecture work, prototypes, MVPs, and pilots where those values
  can be identified from official MSX activity categories.
- Oldest or highest-ACR uncovered milestones.

**Backing report**

- Existing Activity Coverage report.

**Action loop**

- Open Activity Coverage filtered to uncovered milestones in the user's
  prioritized workload scope and agreed fiscal scope.
- Review uncovered milestones.
- Match relevant meetings to milestones.
- Create or reconcile MSX activities.
- Use the existing assisted activity draft and enrichment workflow.
- Reopen the widget with the updated HoK coverage.

**Current implementation behavior**

- Activity Coverage currently starts with locally cached, on-team milestones
  that have an MSX milestone ID and a linked customer.
- It then limits its headline population to active milestones.
- A milestone is covered when it has at least one task marked `is_hok` within
  the current fiscal year.
- The current implementation does not filter the milestone population itself
  by milestone due date, even though the coverage activity must be in the
  current fiscal year.

**Working definition pending manager confirmation**

- Population: active, on-team FY27 Data milestones due in the current or next
  Microsoft fiscal year.
- Coverage: at least one qualifying HoK activity during the current fiscal
  year.
- Technical-validation subset:
  - Technical Workshop.
  - Demo.
  - L300+ Demo.
  - Architecture Design Session.
  - PoC/Pilot.
- Report technical-validation activity counts without inventing an attainment
  percentage when the Connect goal supplies no numeric target.

**Qualifying HoK categories**

- Assessment.
- RFP/RFI.
- Technical Workshop.
- Workshop.
- Solution Whiteboarding.
- Architecture Design Session.
- Demo.
- L300+ Demo.
- Rapid Prototyping.
- PoC/Pilot.
- Technical Close/Win Plan.
- Blocker Escalation.
- Consumption Plan.

`Assessment` and `RFP/RFI` already exist in `TASK_CATEGORIES` but are currently
marked non-HoK. `Briefing` currently qualifies as HoK but is not part of the
approved FY27 list.

Update the category definitions as follows:

- Add the existing Assessment and RFP/RFI MSX category codes to
  `HOK_TASK_CATEGORIES`.
- Remove the Briefing category code from `HOK_TASK_CATEGORIES`.
- Set Assessment and RFP/RFI `is_hok` flags to true.
- Set the Briefing `is_hok` flag to false while leaving Briefing available as a
  normal MSX task category.
- Update focused category and activity-enrichment tests for all three changes.

**Required report work**

- Implement the current-plus-next-FY milestone population.
- Keep the due-date and activity-period rules centralized so manager
  confirmation can adjust them without rewriting the widget.
- Add prioritized-workload and fiscal-scope filters.
- Persist Activity Coverage filter state in local storage if it does not already
  have an equivalent mechanism, then write the widget's desired state before
  navigating.

### 4. Fabric and database whitespace wins

**Connect alignment**

- Produce at least one Fabric and one database whitespace win per half.
- Identify and advance workload expansion.
- Increase Fabric and database adoption.

**Widget**

- One progress lane for `Databases` and one for `Fabric`.
- Current-half wins against a target of at least one per bucket.
- For the current FY27 Data selection, separate `Databases` and `Fabric`
  progress.
- Confirmed sustained wins and still-ramping activations for the current half.

**Definition**

A customer and bucket produce one sustained win when:

1. The first positive-ACR month occurs within the half being measured.
2. The same customer and bucket had $0 total ACR in each of the three complete
   months immediately preceding that positive month.
3. The same customer and bucket remain above $0 for at least three consecutive
   months beginning with the activation month and remain above $0 through the
   latest available month.
4. The bucket is `Databases` or `Fabric`.

Count each customer and bucket once per half. The target is at least one
qualifying win in each prioritized bucket during each fiscal half.

Use a rolling three-month zero-ACR lookback, not only a fixed start-of-half
snapshot. There is no minimum positive ACR threshold. Show one- and two-month
positive sequences as ramping progress, but do not count them as wins. If
consumption later returns to $0, the activation no longer qualifies, even when
it previously completed three positive months.

Attribute a confirmed win to the fiscal half containing its first positive
month, even when the confirmation month falls later.

Treat an absent customer/bucket/month row as zero only when a successful full
revenue sync covers that customer and fiscal month. If any required month is
outside the imported range or the sync is incomplete, mark qualification as
unknown rather than manufacturing a zero.

**Backing report**

- Existing Whitespace Analysis report.
- Keep its current actionable-customer behavior. The user can clear "Show all
  customers (including fully covered)" to focus on accounts with a selected
  bucket gap.
- Do not require a new wins view or other changes to Whitespace Analysis for
  the initial Action Center implementation.

**Action loop**

- Open the existing Whitespace Analysis to review actionable customer gaps.
- Decide which accounts and follow-up workflow best fit the user's current
  priorities.
- Detect the win when qualifying consumption appears after the zero-ACR
  baseline.

**Required service work**

- Add a shared half-year whitespace-win calculation over monthly revenue
  history for the widget.
- Use the annual prioritized-bucket prerequisite rather than a separate
  Fabric/database configuration.

## Deferred widget concepts, not included in v1

The following concepts do not currently have a defensible numeric goal,
complete source data, or sufficiently reliable attribution. Do not implement
them as part of v1.

### 5. DCSA committed milestone continuity

**Connect alignment**

- Engage the DCSA on aligned accounts with committed milestones.
- Complete a technical-plan handoff with clear ownership, stakeholders, and
  next steps.

**Widget**

- Committed milestones requiring continuity follow-up.
- Counts with and without an active engagement.
- Counts with incomplete engagement story fields or open next actions.
- Highest-ACR continuity risks.

**Backing report**

- New DCSA Continuity report, reusing milestone and engagement data.

**Action loop**

- Review committed milestones that lack a complete continuity record.
- Link or create an engagement.
- Complete the engagement's problem, impact, solution, outcome, and target date.
- Record DCSA stakeholders using engagement contacts or a structured DCSA
  involvement field.
- Create follow-up actions.
- Add the engagement to the relevant seller's 1:1 agenda.

**Required data work**

- Add a structured, minimal way to identify DCSA involvement and handoff state.
- Define the conditions for "continuity ready."
- Do not infer DCSA participation from free-text keywords alone.

### 6. DCSA whitespace and workload expansion

**Connect alignment**

- Partner with the DCSA to identify and advance database and Fabric expansion.
- Produce qualified scenarios, milestones, or progression.

**Widget**

- DCSA-aligned whitespace candidates.
- Candidates without a documented customer scenario.
- Candidates with an engagement but no linked milestone or opportunity.
- Recent progression from candidate to active execution.

**Backing report**

- A DCSA view of the focused Whitespace Wins report, not a duplicate query.

**Action loop**

- Filter whitespace candidates to DCSA-aligned accounts.
- Open or create the engagement.
- Document the customer scenario and business outcome.
- Link or create the milestone or opportunity when appropriate.
- Add the item to a seller 1:1 for coordinated follow-up.

**Dependency**

- Depends on the structured DCSA involvement model and the whitespace workflow.

### 7. Opportunity influence and attachment

**Connect alignment**

- Engage earlier in pipeline creation and progression.
- Maintain opportunity team attachment after milestone team coverage is
  handled by the dedicated Milestone Team Coverage widget.

**Widget**

- Aligned opportunities where the user is and is not on the deal team.
- Uncovered opportunities with the strongest pipeline, revenue, milestone, or
  whitespace signal.
- Recent opportunity attachment gains.

**Backing report**

- New Technical Influence Coverage report, reusing aligned customer,
  opportunity, milestone, revenue, and whitespace services.

**Action loop**

- Rank uncovered accounts by potential and urgency.
- Inspect their opportunities and milestones.
- Join the appropriate opportunity or milestone team through the existing MSX
  Workspace actions.
- Create or link an engagement.
- Add the customer or engagement to seller 1:1 follow-up where needed.

**Required definition work**

- Confirm whether opportunity attachment is an additional expectation or only
  supporting context for the milestone-based 50% metric.
- Keep Stage 1 context as a prioritization signal only until a reliable stage
  field exists.

### 8. Partner technical coverage

**Connect alignment**

- Work with partners early.
- Maintain a useful group of go-to technical partners.
- Attach partner technical teams to engaged opportunities and milestones.

**Widget**

- Active customer engagements with and without partner involvement.
- Most frequently engaged partners.
- High-value active engagements missing a partner.
- Partner coverage trend.

**Backing report**

- New Partner Coverage report, reusing partners, partner contacts, notes,
  engagements, milestones, and opportunities.

**Action loop**

- Review high-value engagements without partner involvement.
- Find a partner by specialty.
- Open the partner record and relevant contacts.
- Associate the partner with a customer note or engagement workflow.
- Capture the partner role and next action.

**Required data work**

- Add structured engagement-to-partner involvement if note-level partner tags
  are not sufficient.
- Distinguish a partner mention from an active technical attachment.
- Do not present local partner associations as official MSX partner attach.

### 9. Frontier-led technical execution

**Connect alignment**

- Use Frontier narratives and relevant demonstrations in customer
  conversations.
- Move from feature demonstration to active solutioning and technical decision.
- Deliver demos, workshops, prototypes, MVPs, and pilots.

**Widget**

- Recent technical engagements by activity type.
- Active engagements with discovery but no validation activity.
- Validation activity lacking a documented outcome or next decision.
- High-impact candidates for the next demo, workshop, or prototype.

**Backing report**

- New Technical Execution report built on Activity Coverage, engagements, and
  MSX activity categories.

**Action loop**

- Identify engagements missing the next technical validation step.
- Open the engagement story and customer context.
- Prepare the next meeting or validation activity.
- Create the MSX activity and link it to the milestone.
- Capture the outcome and next technical decision afterward.

**Required definition work**

- Define supported validation categories.
- Decide how Frontier use is explicitly recorded without unreliable keyword
  guessing.
- Treat Frontier as a workflow attribute or confirmation, not an AI inference.

### 10. Customer impact and success-story candidates

**Connect alignment**

- Publish MCAPS Wins and Success Stories.
- Demonstrate delivered impact and business outcomes.

**Widget**

- Highest-impact committed or completed customer outcomes for the current half.
- Engagements with strong outcome evidence.
- Candidates missing one or two story elements.
- Count of story-ready candidates.

**Backing report**

- Evolve Connect Impact into a Success Story Candidates report or add a
  goal-focused view backed by a shared impact service.

**Action loop**

- Review candidate customers, engagements, milestones, and notes.
- Complete missing business problem, impact, solution, outcome, and measurable
  result fields.
- Generate an evidence-grounded draft from confirmed records.
- Copy or export the draft for the external publication workflow.

**Boundary**

- Sales Buddy can identify and prepare the story.
- It cannot verify that MCAPS published it unless a future integration provides
  publication status.
- Therefore the widget measures story readiness and preparation progress, not
  official publication attainment.

### 11. Territory execution priorities

**Connect alignment**

- Contribute toward Azure territory attainment.
- Prioritize the highest-impact opportunities.
- Address technical execution risks that could affect consumption.

**Widget**

- Revenue trend and current alerts.
- At-risk or blocked milestone ACR.
- Highest-value open opportunities or engagements needing technical action.
- Accounts with both declining revenue and active pipeline.

**Backing report**

- A focused territory execution view combining Revenue Analyzer, MSX Workspace,
  milestone status, and engagement actions.

**Action loop**

- Identify the accounts most likely to affect execution.
- Open the customer, opportunity, milestone, or engagement.
- Review blockers and next actions.
- Add priority engagements to seller 1:1 agendas.
- Track resolution through milestones, activities, and revenue movement.

**Boundary**

- The widget assists with territory execution.
- It should not claim official quota attainment unless Sales Buddy receives the
  official quota and attainment source.

## Widget contract

Create a shared widget provider contract in
`app/services/connect_goal_widgets.py`.

Each provider should return:

```python
{
    'key': 'quarterly_u2c',
    'title': 'Quarterly U2C',
    'goal': 'Reach 40% committed across aligned data workloads.',
    'status': 'needs_attention',
    'primary_value': 31.4,
    'primary_unit': 'percent',
    'target_value': 40.0,
    'progress_min': 0.0,
    'progress_max': 100.0,
    'pace_mode': 'linear_period',
    'period_start': '2026-07-01',
    'period_end': '2026-09-30',
    'expected_value_today': 36.5,
    'pace_ratio': 0.86,
    'status_tone': 'warning',
    'status_label': 'Watch',
    'status_explanation': (
        '31.4% committed. Watch because 36.5% is expected by today. '
        'The final quarterly target is 40%.'
    ),
    'trend': [],
    'visual': {
        'type': 'line',
        'series': [],
        'accessible_summary': (
            'Data U2C progression from July 1 through September 23.'
        ),
    },
    'highlights': [],
    'attention_count': 12,
    'attention_label': 'remaining milestones',
    'next_action': 'Review remaining milestones with sellers.',
    'report_url': '/reports/u2c',
    'report_label': 'Work remaining milestones',
    'destination_filter_key': 'salesbuddy_u2c_filters',
    'destination_filter_state': {
        'status': 'remaining',
        'seller': '',
        'workload': 'Data',
    },
    'secondary_actions': [],
    'freshness': '2026-09-23T12:00:00Z',
    'source_label': 'Official MSXi U2C snapshot',
}
```

The contract must support different visual shapes without forcing every widget
to have the same fields:

- Metric plus trend.
- Coverage plus exception list.
- Two-part progress, such as Fabric and database.
- Ranked action queue.
- Readiness funnel.

Required fields should remain small. Optional typed sections should carry the
different content shapes.

Providers own summary and prioritization logic. Templates must not recalculate
results.

## Report integration contract

Every widget must define:

- Its backing report.
- The filter state the report should open with.
- The destination report's local-storage key, when it has persistent filters.
- The primary action available in the report.
- The condition that causes the widget to improve after the action.

Example:

```python
{
    'report_endpoint': 'reports.report_u2c',
    'destination_filter_key': 'salesbuddy_u2c_filters',
    'destination_filter_state': {
        'status': 'remaining',
        'seller': '',
        'workload': 'Data',
    },
    'primary_workflow': 'add_to_seller_one_on_one',
    'improvement_signal': 'official_commitment_change',
}
```

On activation, the widget writes the complete destination filter state and then
navigates to the backing report. The backing report restores the same state it
already uses for normal user-selected filters.

Do not add query-string synchronization, temporary state restoration, or
special return-navigation behavior in the first release. Reconsider only after
real usage shows a concrete problem.

## Shared action patterns

Several reports need the same actions. Extract and reuse these patterns instead
of implementing slightly different versions:

- Add milestone or engagement to seller 1:1 with a custom note.
- Open or create an engagement from a customer, milestone, or opportunity.
- Link records to an engagement.
- Create or reconcile an MSX activity.
- Join an opportunity or milestone team.
- Add or update engagement story fields.
- Create a follow-up action item.
- Open filtered customer, seller, partner, milestone, or opportunity context.

The reports should call shared service operations. Widget providers should only
describe available actions and URLs.

## Page structure

### Execution brief

Lead with the few goals that need attention now, not a row of oversized totals.

- Quarter and half-year context.
- Source freshness summary.
- "Needs your attention" list drawn from widget providers.
- Direct links into the exact filtered workflows.

### Goal sections

Group widgets by the work they support:

1. Progress opportunities.
2. Deliver technical execution.
3. Expand workloads and revenue.
4. Scale through sellers, DCSA, and partners.
5. Capture impact.

This is more useful than mirroring the long Connect goal text word for word.
Each widget can cite the Connect goal or manager ask it supports.

### Widget behavior

- The summary remains visible without interaction.
- Clicking the title or main call to action opens the backing report.
- Before navigation, the widget writes the intended destination filter state.
- Secondary actions are explicit buttons or links, not hidden hover controls.
- Small exception previews may expand inline, but full work happens in the
  backing report.
- Empty states should explain what source or setup is needed and link to the
  relevant import, sync, or configuration page.

## Architecture

### Shared services first

Before building widgets, move reusable query logic out of routes and SalesIQ
tools:

- U2C summary and trend remain in `u2c_snapshot.py`.
- Activity and HVA coverage remain in `activity_coverage.py`.
- Whitespace and revenue calculations remain in revenue services.

The page route, backing reports, widget providers, and SalesIQ tools must call
the same services.

### Fixed widget catalog

Keep the first widget catalog as typed Python definitions. Each entry includes:

- Stable widget key.
- Supported Connect goals.
- Provider.
- Backing report endpoint.
- Default width and presentation type.
- Availability requirements.
- Display order.

Do not add database-backed layout configuration in the first release.

### No generic manual evidence system in the first release

The earlier plan proposed a general `PerformanceEvidence` model. That is not
needed for the action-center scope and should not be built now.

Any persistence should belong to the workflow it supports:

- Activity records for HVA coverage.
- Revenue history for whitespace wins.
- 1:1 agenda items for seller follow-up.

This keeps the app centered on doing the work instead of maintaining a second
performance-tracking database.

### Route and navigation

- Page: `/reports/connect-goals`
- APIs: `/api/reports/connect-goals/...`
- Blueprint: existing `reports` blueprint.
- Template: `templates/report_connect_goals.html`.
- Add the page to the Reports navigation dropdown.
- Add it to the Connect Prep group in `reports_hub()`.
- Keep existing reports accessible as independent pages.

### Contextual F1 help

The existing contextual page-help shortcut is **F1**. Add a route-specific
entry for `/reports/connect-goals` in `static/js/page-help.js`.

The help page should explain what v1 tracks and the goal definition for each
metric. It must not repeat the user's current live values from the widgets.

List:

1. **Quarterly U2C progression:** reach 40% committed ACR from the official
   frozen quarterly starting cohort.
2. **Milestone team coverage:** be on the team for at least 50% of the agreed
   in-scope milestone population.
3. **Milestone HoK coverage:** maintain qualifying HoK coverage across 100% of
   the agreed in-scope, on-team milestone population, with demos, Technical
   Workshops, and other qualifying validation activities called out.
4. **Whitespace wins:** produce at least one qualifying Databases win and one
   qualifying Fabric win per fiscal half, where the customer had zero ACR in
   each of the three immediately preceding complete months.

Also explain:

- What U2C, HoK, ACR, and whitespace mean.
- That the current prioritized buckets define workload scope.
- Which report each widget opens.
- That freshness differs by source and is shown on the widget.
- That goals excluded from v1 are intentionally not represented by placeholder
  scores.

Keep the help content static and definition-focused so it remains useful when
live provider data is unavailable.

### SalesIQ

Register `report_connect_goals` as a thin wrapper over the widget provider
service.

The response should include:

- Current highlights.
- Items needing attention.
- Recommended next actions.
- Backing report links or entity references where supported.
- Source freshness.

Update the SalesIQ coverage test.

## Implementation order

### Phase 1: Prove the widget model with existing workflows

1. Define the widget contract and fixed catalog.
2. Add fiscal-year and taxonomy confirmation to the existing compensated-bucket
   preference.
3. Extract and reuse a shared prioritized-bucket picker and save service.
4. Add the Action Center setup gate and shared FY27 Data scope configuration.
5. Implement U2C using existing summary, trend, filters, and 1:1 action.
6. Add the Milestone Tracker local-storage filter handoff and implement
   Milestone Team
   Coverage with its existing join-team action.
7. Promote Assessment and RFP/RFI into the qualifying HoK category set, remove
   Briefing from that set, and update focused tests.
8. Implement Milestone HoK Coverage using Activity Coverage, including the
   qualifying technical-validation HoK breakdown.
9. Add the half-year whitespace-win calculation and widget, linking to the
   existing Whitespace Analysis without changing that report.
10. Build the fixed Action Center page with the four v1 widgets.
11. Add contextual F1 help, navigation, SalesIQ, and focused tests.

This produces a useful first release without waiting for every new report.

### Phase 2: Observe and prepare for custom pages

Use the fixed Action Center through a meaningful review cycle.

Record:

- Which widget calls to action are used.
- Which backing report filters are useful.
- Which widgets lead to completed actions.
- Which widget shapes are reusable.
- Which priorities change or become irrelevant.
- Which existing reports users want summarized elsewhere.

Use this evidence to define the custom report builder.

## Future custom report pages

Custom page creation remains out of scope until the fixed Action Center and its
backing workflows are implemented.

### Concept

Users will create pages from the same proven widget catalog:

- Add or remove widgets.
- Configure supported filters.
- Arrange and resize widgets.
- Save named layouts.
- Start from templates such as Connect Goals, Manager 1:1, Quarter Execution,
  and Workload Expansion.

The custom system changes placement and selection. It does not create a second
implementation of widget data or actions.

### Reuse model

- **Service:** owns detailed query and workflow logic.
- **Backing report:** supports investigation and action.
- **Widget provider:** summarizes the service and points to the report.
- **Widget renderer:** displays one of the supported summary shapes.
- **Fixed Action Center:** chooses an opinionated set and order.
- **Custom report:** stores the user's chosen widgets, settings, and positions.

### Future persistence

Potential models:

`CustomReport`

- `id`
- `name`
- `description`
- `is_default`
- `created_at`
- `updated_at`

`CustomReportWidget`

- `id`
- `report_id`
- `widget_key`
- `title_override`
- `settings_json`
- `grid_x`
- `grid_y`
- `grid_width`
- `grid_height`
- `display_order`

Use versioned widget settings. If a widget becomes unavailable, show an
actionable unavailable state instead of silently removing it.

### Editing experience

- Separate View and Edit modes.
- Searchable widget catalog.
- Drag and resize in Edit mode only.
- Keyboard-accessible move and resize controls.
- Explicit Save and Cancel.
- Duplicate a report before experimenting.
- No drag handles or configuration controls in normal View mode.

## Working definitions pending manager confirmation

Build v1 with these provisional decisions, keeping the calculation rules
centralized so confirmed definitions can be applied without changing widget
presentation:

1. Milestone team coverage uses active FY27 Data milestones due in the current
   or next fiscal year.
2. Milestone team coverage is ACR-weighted, not count-weighted.
3. HoK coverage uses active, on-team FY27 Data milestones due in the current or
   next fiscal year.
4. HoK coverage requires a qualifying HoK activity during the current fiscal
   year.
5. The qualifying HoK and technical-validation category lists are defined in
   the Milestone HoK Coverage section.
6. A whitespace win uses a rolling three-complete-month zero-ACR lookback,
   followed by three consecutive positive months with no minimum ACR threshold.

Manager confirmation questions and answer space live in
`docs/FY27_CONNECT_MANAGER_QUESTIONS.md`.

## Testing

### Provider tests

- Contract validation for every widget shape.
- Summary values match backing service values.
- Goal-marker positions and normalized count progress.
- Linear-period expected-to-date and pace calculations.
- Status thresholds at 60%, 80%, and 100% of expected pace.
- Non-paced coverage goals compare directly with their final target.
- Visual data matches the backing report provider.
- Accessible summaries contain the current value, target, and trend direction.
- Missing and stale data produce actionable states.
- No duplicate counting across linked records.
- Every provider uses the shared FY27 Data scope configuration.

### Annual setup tests

- A legacy bucket selection without a fiscal-year stamp shows the setup gate.
- A prior-FY selection shows the setup gate after fiscal-year rollover.
- A selection confirmed against an older taxonomy version shows the setup gate.
- A selection containing a bucket no longer present in imported revenue data
  shows the setup gate and identifies the invalid selection.
- A current-FY selection containing both `Databases` and `Fabric`, confirmed
  against the current taxonomy, renders widgets.
- A current-FY selection missing either required FY27 Data bucket shows the
  setup gate and identifies the missing bucket.
- Saving from the shared picker stamps the current fiscal year and taxonomy
  version.
- Saving updates both the database fallback and existing local-storage key.
- No imported buckets produces an actionable Revenue Import or sync state.

### Workflow tests

- Widget destination opens the expected report and filters.
- Widget navigation writes the destination report's existing local-storage
  filter shape.
- Report actions update the correct underlying records.
- U2C items add the correct milestone or engagements to the correct seller 1:1.
- The Whitespace widget opens the existing Whitespace Analysis without changing
  that report.
- Team-join and activity actions enforce record scope.

### Route and UI tests

- Action Center loads with complete, partial, and empty source data.
- Widgets do not render until prioritized buckets are confirmed for the current
  fiscal year and taxonomy.
- The active prioritized buckets and Edit action are visible after setup.
- Contextual F1 help lists the four v1 metric definitions and goals without
  embedding current provider values.
- Each widget has a clear report destination and next action.
- Explicit goals show a readable progress bar, target marker, status text, and
  appropriate semantic color.
- Goal status remains understandable without color.
- Hovering or focusing a paced value explains why it has its current color.
- Progress bars expose complete ARIA values and text.
- Every canvas chart has a useful accessible name and hidden text summary.
- Chart information remains available without mouse hover.
- Excluded goals do not render.
- Existing reports remain independently usable.
- Light and dark themes remain readable.
- Keyboard navigation reaches every widget action.

### SalesIQ tests

- Tool registration and coverage.
- Tool output matches widget providers.
- Recommendations link to the same underlying action queues.

## Completion criteria

The first release is complete when:

- It contains only goals Sales Buddy can actively help progress.
- It requires a valid current-FY prioritized-bucket selection before calculating
  or rendering goal widgets.
- Revenue Import, onboarding, and Action Center use one shared bucket picker and
  persistence service.
- U2C, Milestone Team Coverage, Activity / HoK Coverage, and Whitespace Wins are
  live.
- Every widget has a backing report and a concrete action loop.
- Widget values and backing reports use shared service calculations.
- Context survives navigation from widget to report.
- Existing U2C-to-1:1 and activity workflows remain intact.
- Excluded goals do not appear as manual or unavailable placeholders.
- SalesIQ exposes the same highlights and recommended actions.
- The widget contract is reusable by future reports and custom pages.
- The page is useful before custom layout editing exists.
