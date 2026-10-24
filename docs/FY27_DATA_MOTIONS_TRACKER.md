# FY27 Data Motions Implementation Tracker

**Status:** Current scope complete. User confirmed on October 23, 2026 that no
further refinements are planned. Future development will be scoped as needed.

This document records delivered reports, widgets, validation and known scope
boundaries. Manual workflows and data limitations below are not a follow-up backlog.

## Checklist

### 1. [x] Fabric modernization

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Customers with positive Synapse, AAS, ADX, Azure Databricks or ADF
  revenue over the last three complete calendar months, alongside Fabric spend
  for the same period.
- **Boundary:** Consumption signals are not migration-readiness assessments.
  Azure Databricks is not evidence of non-Azure Databricks.
- **Report/widget:** Fabric Modernization report, replacing Synapse Customers.
  Summary and Details widgets show report-qualified customers whose Other Analytics
  plus ADF spend exceeds Fabric spend, with viewer-controlled seller grouping.
  Dollar amounts display average monthly ACR, dividing each customer's totals by its
  available months within the three-month window, or $0 when no months are available.
  Summary includes the customer count and separate monthly Other Analytics and ADF ACR.
  Details shows Customer, Other Analytics, ADF and Fabric. Other Analytics amounts
  with positive net Synapse spend have a dotted underline and an "Includes Synapse" tooltip.
- **Revenue cutoffs:** Exclude combined Other Analytics plus ADF totals below $50,
  or below $100 with positive
  Fabric spend, using unrounded three-month net totals.
- **Notes:** One row per customer, source-product columns and Fabric Capacity
  revenue, grouped by current seller. Months count once per customer across all saved
  product rows, including products outside the modernization families. Missing product
  spend is $0. Summary totals sum customer-level monthly averages, even when customers
  have different month counts. Partial coverage remains flagged.
  Dollar amounts represent existing consumption, not projected incremental Fabric benefit.
  ADF maps Azure Data Factory v2, Data Flow and Managed Airflow. Synapse-branded
  pipelines remain in Synapse. Other Analytics retains Synapse, AAS, ADX and Databricks
  only, with no ADF double-counting.
  Report and widget validated against saved revenue. Added to the FY27 Data Motions
  dashboard and confirmed complete for this scope.

### 2. [x] Fabric acquisition

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Relevant Analytics/Data/AI footprint with no observed
  Fabric ACR in the current month plus the previous three months.
- **Boundary:** Accounts flagged stale after dropping out of account sync are excluded.
  Missing revenue observations count as $0. No observed Fabric ACR does not
  prove no Fabric adoption, and does not establish M365 licensing.
- **Report/widget:** Whitespace Analysis > Fabric Acquisition, with Summary and
  Details widgets backed by the same source, with a widget-only $2,000/month minimum
  combined AI, Databases and non-Fabric Analytics footprint. Available for Data SEs
  and sellers. The report retains customers below that minimum.
- **Notes:** Display AI, Databases and Other Analytics (excluding Fabric) from the
  source-defined strategic pillar feed. Exclude positive monthly net Fabric ACR in the
  current partial month or previous three months. Divide four-month totals by 4.
  Missing customer/month observations count as $0. The report's All customers option
  includes customers without any imported revenue records.
  Verified against saved revenue, including exact monthly amounts, specialty access,
  scoped widgets and seller grouping. Added to the FY27 Data Motions dashboard
  and confirmed complete for this scope.

### 3. [x] Fabric expansion

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Power BI-dominant or narrow-workload measured estates,
  with target workloads not observed in the available data.
- **Boundary:** Usage mix does not establish workload fit or business readiness.
  The source summary combines Data Engineering and Data Science.
- **Report/widget:** Fabric expansion, Summary / Details. Links to Fabric Consumption
  > Capacity overview > F-SKU audit, one row per customer.
- **Notes:** Single Power BI-led queue (80%+ Power BI consumption).
  Only customers with paid F-SKUs qualify; Details shows their largest F-SKU
  with P/Trial context, average capacity utilization, PBI share, and
  known workloads above 1%, with optional seller grouping in the viewer Filters panel.
  Incomplete mixes remain excluded. Added to the FY27 Data Motions dashboard
  and confirmed complete for this scope.

### 4. [x] Trial to Paid

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Customers with trial capacities and no paid F SKU.
  Summary counts eligible customers and those with P-SKUs alongside their trials.
- **Boundary:** A trial present in the latest sync is active for this workflow.
  Engagement creation and trial -> F-SKU conversion tracking remain manual.
- **Report/widget:** Trial to Paid, Summary / Details. Links to Fabric Consumption
  > Capacity overview > Trial to Paid audit. The broader Trial audit remains available.
- **Notes:** Counts distinct customers, not capacities. No additional
  Fabric ACR filter is imposed. Details lists each customer once, split below/at-or-above 0.1%
  average trial utilization. Tables show Customer, Avg used and average observed history days
  across trials, using the saved source's trailing 15-day window. Customer links
  open Fabric detail; the table scrolls within the chosen widget height.
  Added to the FY27 Data Motions dashboard and confirmed actionable.

### 5. [x] Power BI to Fabric

- [x] Coded
- [x] Added to dashboard
- **Scope:** Surface management's supplied P-to-F customer list through the
  existing report and the widget we just built. No additional discovery or
  workload-mix analysis is required for this motion.
- **Report/widget:** Upcoming P-to-F renewals, Summary / Details.
- **Notes:** Details covers all dated future renewals in the
  saved report and shows customer/product, expiration, and renewal status.
  The table scrolls within the widget, keeping the title and column headings visible.
  Summary retains the next 90 days. Added to the FY27 Data Motions dashboard;
  future layout adjustments do not affect completion.

### 6. [x] RTI

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Event Hubs, Stream Analytics and IoT customers without RTI
  usage, including accounts without Fabric capacity as an entry point to Fabric.
  Add confirmed context from notes and engagements.
- **Boundary:** These signals do not establish external Kafka inventory,
  architecture, or whether RTI should replace the current platform.
- **Report/widget:** Real-Time Intelligence report and Summary/Details widgets.
  One row per current account with zero RTI usage and positive Event Hubs,
  Stream Analytics or IoT net ACR. Customers already using RTI are excluded.
  No minimum spend cutoff.
- **Notes:** Native rolling 28-day RTI CU-hours and workload share stay
  separate from ACR across the last three complete calendar months,
  divided by three. The current month is excluded. Missing revenue observations count as $0;
  missing RTI measurements count as 0. IoT includes IoT-prefixed products and Device Update
  for IoT Hub. Dropped/stale accounts are excluded, without deleting history.
  Supports customer/seller widget scope, seller grouping and both source syncs.
  Report and widget tables show customer, streaming ACR and the combined monthly total.
  Zero RTI usage is enforced by the filter rather than repeated in zero-only columns.
  Added to the FY27 Data Motions dashboard and confirmed complete for this scope.
  Engagement creation and trial -> F-SKU conversion tracking remain manual.

### Database whitespace (added from revised guidance)

- [x] Coded
- [x] Dashboard placement confirmed by user
- **Status:** User confirmed this report/widget scope complete on October 20, 2026.
- **Starting scope:** Existing Whitespace Grid customers without Databases coverage
  and with positive AI, Analytics or Apps coverage. Developer, Infra and Security
  alone do not qualify. Reuse the latest-data window and available-month averages,
  without an additional minimum spend.
- **Report/widget:** Existing Whitespace Analysis calculation, plus Summary/Details widgets
  and a SalesIQ read tool. Details shows Customer, AI, Analytics, Apps and Total / mo.
  Analytics includes Fabric; totals and ranking use only those three adjacent pillars.
- **Notes:** Customer/seller filters and seller grouping apply to both
  widget variants. Focus whitespace excludes customers with the target pillar,
  independently of the displayed spend columns, and hides the focused pillar's
  zero-only column. The report link selects Databases
  focus with the same qualifying cohort and AI, Analytics and Apps columns,
  preserving normal report preferences.
  Candidate discovery is separate from achieved whitespace wins.

### 7. Data/AI foundation - not planned

- **Decision:** On October 20, 2026, the user confirmed that existing Fabric Acquisition
  is sufficient for AI attach. No separate report, widget or targeting view is planned.

### 8. [x] SQL migration

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Source-reported future SQL expirations from Azure Migrations,
  nearest first, including zero/unknown dollars and renewal dates beyond 90 days.
- **Boundary:** On-prem footprint means SA license cores, not server counts.
  Modeled monthly ACR is not qualified pipeline. Neither source timestamps nor
  renewal events prove a complete, current customer estate or achieved migration.
- **Report/widget:** SQL Renewals report and Summary/Details widgets share the same
  complete queue and SalesIQ tool. One atomic SQL Motions sync also pulls the Arc
  version feed. Customer/seller scope carries into the report; sellers only display
  when explicitly grouped. Source details retain freshness and quality caveats.
- **Notes:** October 21 live source audit matched all 206 active dev
  customers: 56 had next SQL expiration dates and 44 positive expiration amounts.
  The glossary defines install base as license cores with SA, not server counts.
  A second ADS Accelerate Arc source supplies SQL 2016 discovery evidence on 26
  active accounts; ESU protection/eligibility is still unverified. App-side renewal
  and Arc pulls now work using the existing isolated SalesBuddyDev Azure profile.
  See the aligned tracker's source audits for freshness, count semantics and
  remaining validation. Live shared import and native previews now verify
  56 future renewals, 8 within 90 days. User approved the saved placement and
  ten-widget FY27 Data Motions v1 template on October 21, 2026.

### 9. [x] Database expansion

- [x] Coded
- [x] Added to dashboard
- **Status:** User confirmed coded and added to the dashboard on October 21, 2026.
- **Direction:** Use the existing Revenue Analyzer for database consumption signals,
  with a report-backed widget, not a separate report or trend engine.
- **Starting scope:** The analyzer already exposes a Databases bucket and product
  drill-downs. The motion view shows pending database alerts recommending expansion
  outreach or check-ins, using the existing priority and review workflow.
- **Boundary:** Low spend, flat revenue or decline alone does not establish a
  growth opportunity. Retention signals remain distinct from expansion discovery.
- **Report/widget:** Database Expansion Summary/Details reuse the existing revenue
  widget presentation and saved analyzer query. Pending Databases alerts at priority
  20+ with Expansion Outreach or Check-in actions qualify. Monitor-only and handled
  alerts do not. Growth and risk counts and monthly impact estimates stay separate.
- **Notes:** Click-through opens Revenue Analyzer's focused preset with
  every matching row, before the normal top-50 limit, and preserves customer/seller
  scope without changing bucket preferences. No standalone report or new scoring.
  Verified 216 scoped Python and 37 JavaScript tests plus live report/widget parity:
  3 growth and 2 risk alerts. Half-width Details is saved beside Database Whitespace,
  preserving existing placements and filters; user confirmed placement and review.
  Qualification, engagement links and achieved-outcome tracking remain manual.

### 10. [x] SQL 2016 EOS / ESU

- [x] Coded
- [x] Added to dashboard
- **Starting scope:** Explicit SQL Server 2016 version observations with positive
  All Discovered values from ADS Accelerate Arc, not generic Arc consumption.
- **Boundary:** Discovery count units, ESU protection/eligibility and estate
  completeness remain unverified. Missing rows mean unknown. Do not add overlapping
  Enterprise/Standard/Non Production categories to All Discovered.
- **Report/widget:** SQL 2016 EOS / ESU report, Summary/Details widgets and shared
  SalesIQ tool. The same SQL Motions sync loads both report sources atomically.
  The EOS view includes only version discovery and subscription evidence;
  account renewal signals belong in SQL Renewals.
- **Notes:** This replaces the initial Arc bridge tile placeholder.
  Customer-confirmed Arc/ESU bridge, migrate, modernize and deferred paths remain
  manual workflows, not automatic qualification. Live cohort: 26 customers and 295
  source-defined counts. User approved the saved placement and ten-widget FY27
  Data Motions v1 template on October 21, 2026. The template installs once on upgrade
  and remains available for independent copies.

## Delivery boundaries

Customers may qualify for several motions. Do not add motion counts together
and label the result unique customers.
