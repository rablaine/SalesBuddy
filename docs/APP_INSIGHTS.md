# App Insights Telemetry Integration

Sales Buddy ships anonymous, category-level usage telemetry to Azure Application Insights so maintainers can understand which features are actually used across all instances.

## What Gets Sent

Each HTTP request generates one custom event (`SalesBuddy.FeatureUsage`) with:

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `instance_id` | property | `5031acef-cd55-...` | Random UUID per install (not tied to any user) |
| `app_version` | property | `a1b2c3d` | Git commit hash at boot |
| `category` | property | `Notes` | Feature category (derived from blueprint) |
| `method` | property | `GET` | HTTP verb |
| `is_api` | property | `True` | Whether the request hit `/api/...` |
| `status_code` | measurement | `200` | HTTP response status |
| `response_time_ms` | measurement | `42.5` | Request duration in ms |
| `is_error` | measurement | `0.0` | `1.0` if status >= 400, else `0.0` |

## What Is NOT Sent

- IP addresses, usernames, email addresses, session tokens
- Customer names, TPIDs, or any business data
- Full endpoint paths (only the feature category)
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

### Unique Instances

```kusto
customEvents
| where name == "SalesBuddy.FeatureUsage"
| summarize
    instances = dcount(tostring(customDimensions.instance_id)),
    events = count()
    by bin(timestamp, 1d)
| render timechart
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
