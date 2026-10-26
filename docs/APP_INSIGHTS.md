# App Insights Telemetry Integration

Sales Buddy ships anonymous install metadata, page activity and request health to Azure
Application Insights. `/metrics` reports active installs, code versions, role and specialty
mix, and page popularity separately from background traffic.

## What Gets Sent

Each HTTP request generates one custom event (`SalesBuddy.FeatureUsage`) with:

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `instance_id` | property | `5031acef-cd55-...` | Random UUID per install (not tied to any user) |
| `app_version` | property | `a1b2c3d...` | Full Git commit hash loaded at boot; short hash on older installs |
| `schema_version` | property | `2` | Explicit event contract version |
| `user_role` | property | `se` | Saved install role: `se`, `dss` (seller), or `unknown` |
| `se_specialty` | property | `apps_ai` | SE specialty: `data`, `infra`, `apps_ai`, `other`, `unknown`; `not_applicable` for sellers |
| `feature` | property | `reports.whitespace` | Flask endpoint name, without route values |
| `category` | property | `Notes` | Feature category (derived from blueprint) |
| `method` | property | `GET` | HTTP verb |
| `is_api` | property | `True` | Whether the request hit `/api/...` |
| `status_code` | measurement | `200` | HTTP response status |
| `response_time_ms` | measurement | `42.5` | Request duration in ms |
| `is_error` | measurement | `0.0` | `1.0` if status >= 400, else `0.0` |

Successful page opens additionally generate `SalesBuddy.PageView` with the same dimensions
and `page`, the Flask route template (for example `/customer/<int:id>`, never `/customer/123`).
GET requests must return a successful HTML document. Redirects, errors, HEAD/POST requests,
API calls, prefetches, streamed responses and background HTML fragments do not count.
User-opened record dialogs that fetch a complete HTML page count as page opens.

`SalesBuddy.InstallProfile` sends the same install identity, full boot commit, role and
specialty on startup and every 24 hours, plus existing feature flags and aggregate entity
counts. A profile failure is retried on the next flush rather than suppressed for a day.
Profile and background-request events never establish active usage.

### Reporting definitions

- **DAU:** Exact number of distinct anonymous installs with a page open in a UTC calendar
  day. One install is counted once even if its role changes that day; the last observed
  role in that day determines its chart series.
- **Active in selected range:** Distinct page-active installs across the entire range,
  not the sum of daily counts.
- **Reporting installs:** Installs with metadata observed in the selected range, including
  background-only installs. This is not a cumulative download count.
- **Version inventory:** One latest observed version per install, with separate last-seen
  and last-page-active timestamps. Versions describe code loaded at startup; a checkout
  without an app restart does not change the reported version.
  The dashboard's version distribution adds recorded commit dates from local Git history,
  matching full or unambiguous abbreviated hashes. Missing or ambiguous hashes show Unknown.
  These are committer dates, not GitHub push times, and this local enrichment is not available
  in the Azure workbook.
- **Roles and specialties:** Saved install preferences, independent of seller filtering.
  Sellers never contribute their previously saved SE specialty to the SE mix.
- **Page popularity:** All page kinds ranked by total loads, with exact unique-install
  reach for each. No customer IDs, names, URLs or query values are shipped.
- **Time windows:** 1, 7, 30 or 90 UTC calendar days including today. Feature-flag and
  entity-count snapshots remain explicitly labeled as 30-day snapshots.

Page-based activity starts with schema 2. Old request telemetry cannot distinguish
background activity, so it is not used as a DAU fallback or backfilled page count.
Older profiles remain visible in inventory, with unknown specialty where absent.
One person with two installs counts twice; an anonymous install is not an identified person.

## What Is NOT Sent

- IP addresses, usernames, email addresses, session tokens
- Customer names, TPIDs, or any business data
- Resolved endpoint paths, route argument values and query strings
- User-agent strings

## How It Works

1. Every request passes through Flask's `after_request` hook in `app/services/telemetry.py`
2. The hook calls `queue_event()` in `app/services/telemetry_shipper.py`, which appends an App Insights envelope to an in-memory buffer
3. A background daemon thread flushes the buffer to the App Insights `/v2/track` endpoint every 30 seconds
4. If the buffer hits 200 events before the timer fires, it flushes early
5. If a flush fails, events are put back in the buffer for the next attempt

## Opt-Out

Set the environment variable to disable all central telemetry:

```
SALESBUDDY_TELEMETRY_OPT_OUT=true
```

Local telemetry (the `usage_events` table in SQLite) still works regardless.
Central shipping is also disabled in `FLASK_ENV=development`. Local automated tests inspect
buffered events using isolated databases and mocked shipping, not production telemetry.

## Metrics dashboard and authentication

Open the unadvertised `/metrics` page. Each card queries App Insights independently, and
access still depends on the signed-in account's read permission on `NoteHelper_Telemetry`.
Token acquisition first tries a bounded `az account get-access-token` read for the
telemetry tenant, then the existing default CLI account, then existing Azure SDK sign-ins.
This preserves the original fallback behavior instead of requiring a separate CLI login
to the telemetry tenant. CLI broker support is disabled in those child processes only;
saved CLI configuration and the app's existing `AZURE_CONFIG_DIR` are preserved.
The SDK fallback disables automatic interactive authentication and does not invoke CLI
again. Silent SDK cache, VS Code and broker credentials remain available.
Concurrent cards share one token acquisition and a short failure cache.
On Windows MSI installs, token reads invoke the CLI's Python host directly so a timeout
terminates the CLI rather than just its `az.cmd` wrapper.
Opening or refreshing the page never initiates interactive broker/browser authentication.
Timeouts and malformed credential responses are not treated as proof that the user signed out.
An interactive browser-based `az login` is launched only when the user explicitly clicks
Sign in. Repeated clicks join the same running attempt, preventing competing MFA challenges.
Do not change the shell's default Azure account to troubleshoot development auth.

The checked-in workbook at `infra/workbooks/salesbuddy-usage-workbook.json` uses the same
page-based activity and latest-install definitions. Editing it does not deploy it to Azure.
The SalesIQ `get_usage_metrics` read tool uses the same queries and explicit error contract.

## App Insights Connection

- **Instrumentation Key:** `56e582af-b491-4808-9641-bbb302c62948`
- **Ingestion Endpoint:** `https://centralus-2.in.applicationinsights.azure.com/`
- **Application ID:** `84a49533-d8f7-4720-a3cf-9da762f11a64`

This connection string is hardcoded (not a secret per Microsoft docs -- it's a write-only ingestion key).

## Querying Events in Azure Portal

1. Go to [portal.azure.com](https://portal.azure.com)
2. Open the Application Insights resource
3. Click **Logs** in the left sidebar (under Monitoring)
4. Run Kusto queries against the `customEvents` table

## Useful Kusto Queries

### All Recent Events

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| extend category = tostring(customDimensions.category),
         method = tostring(customDimensions.method),
         instance_id = tostring(customDimensions.instance_id),
         is_api = tostring(customDimensions.is_api),
         status_code = todouble(customMeasurements.status_code),
         response_ms = todouble(customMeasurements.response_time_ms)
| project timestamp, category, method, status_code, response_ms, is_api, instance_id
| order by timestamp desc
| take 50
```

### Events Per Category

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| summarize count() by tostring(customDimensions.category)
| order by count_ desc
```

### Error Rate by Feature

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| summarize
    total = count(),
    errors = countif(todouble(customMeasurements.is_error) == 1)
    by tostring(customDimensions.category)
| extend error_rate = round(errors * 100.0 / total, 1)
| order by total desc
```

### Average Response Time by Feature

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| summarize
    avg_ms = round(avg(todouble(customMeasurements.response_time_ms)), 1),
    p95_ms = round(percentile(todouble(customMeasurements.response_time_ms), 95), 1),
    count = count()
    by tostring(customDimensions.category)
| order by avg_ms desc
```

### Usage Over Time (daily, last 30 days)

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago(30d)
| summarize events = count() by bin(timestamp, 1d), tostring(customDimensions.category)
| render timechart
```

### Daily Active Installs

```kusto
customEvents
| where name == "SalesBuddy.PageView"
| where timestamp >= startofday(ago(6d))
| extend instance_id = tostring(customDimensions.instance_id)
| where isnotempty(instance_id)
| summarize by day = startofday(timestamp), instance_id
| summarize DAU = count() by day
| render timechart
```

### Page Popularity

```kusto
customEvents
| where name == "SalesBuddy.PageView"
| where timestamp >= startofday(ago(6d))
| extend instance_id = tostring(customDimensions.instance_id),
         feature = tostring(customDimensions.feature),
         page = tostring(customDimensions.page)
| where isnotempty(instance_id) and isnotempty(feature)
| summarize loads = count() by feature, page, instance_id
| summarize page_loads = sum(loads), active_installs = count() by feature, page
| order by page_loads desc
```

### Dead Features (zero usage in last 30 days)

```kusto
let known_categories = dynamic([
    "Admin", "AI", "Backup", "Notes", "Connect Export", "Customers",
    "General", "Milestones", "MSX Integration", "Opportunities", "Partners",
    "Pods", "Revenue", "Sellers", "Solution Engineers", "Territories", "Topics"
]);
let active = customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago(30d)
| distinct tostring(customDimensions.category);
print dead_features = set_difference(known_categories, active)
```

### Feature Trend (week-over-week)

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago(14d)
| extend category = tostring(customDimensions.category)
| summarize
    this_week = countif(timestamp > ago(7d)),
    last_week = countif(timestamp between (ago(14d) .. ago(7d)))
    by category
| extend change_pct = round(iff(last_week == 0, 100.0, (this_week - last_week) * 100.0 / last_week), 1)
| order by this_week desc
```

### Errors by Status Code

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where todouble(customMeasurements.is_error) == 1
| summarize count() by
    tostring(customDimensions.category),
    tostring(customMeasurements.status_code)
| order by count_ desc
```

### Per-Instance Breakdown

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago(7d)
| summarize
    events = count(),
    categories_used = dcount(tostring(customDimensions.category)),
    error_rate = round(countif(todouble(customMeasurements.is_error) == 1) * 100.0 / count(), 1)
    by tostring(customDimensions.instance_id)
| order by events desc
```

## Admin Panel

The admin panel (Admin > Usage Telemetry > Central Telemetry section) shows:

- **Status:** Active or Opted Out
- **Instance ID:** First 8 chars of the anonymous UUID
- **Flush stats:** Events queued, flushed, buffer size, errors
- **Flush Now button:** Manually trigger an immediate flush

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/telemetry/flush` | POST | Force-flush the buffer to App Insights |
| `/api/admin/telemetry/shipping-status` | GET | Get flush stats, instance ID, enabled state |
