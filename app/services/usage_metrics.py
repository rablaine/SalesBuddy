"""Anonymous usage insights shared by the metrics dashboard and read tools."""
from __future__ import annotations

import logging

from app.services.app_insights import AppInsightsError, is_auth_error, query_to_dicts
from app.services.update_checker import get_commit_dates

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KQL queries. Page activity starts at the first selected UTC calendar day.
# ---------------------------------------------------------------------------

def _page_events(days: int) -> str:
    """Select explicit page opens; legacy request traffic cannot prove human activity."""
    return f"""
customEvents
| where name == "SalesBuddy.PageView"
| where timestamp >= startofday(ago({days - 1}d))
| extend instance_id = tostring(customDimensions.instance_id),
         feature = tostring(customDimensions.feature),
         page = tostring(customDimensions.page),
         user_role = case(tostring(customDimensions.user_role) == "dss", "Seller",
                          tostring(customDimensions.user_role) == "se", "SE", "Unknown")
| where isnotempty(instance_id)
"""


def _latest_installs(days: int) -> str:
    """Keep one latest metadata record per reporting install, including legacy profiles."""
    return f"""
customEvents
| where timestamp >= startofday(ago({days - 1}d))
| where name == "SalesBuddy.InstallProfile"
    or (name in ("SalesBuddy.FeatureUsage", "SalesBuddy.PageView")
        and tostring(customDimensions.schema_version) == "2")
| extend instance_id = tostring(customDimensions.instance_id)
| where isnotempty(instance_id)
| summarize arg_max(timestamp, *) by instance_id
| project instance_id, last_seen_utc = timestamp,
          app_version = iff(isempty(tostring(customDimensions.app_version)),
                            "unknown", tostring(customDimensions.app_version)),
          user_role = case(tostring(customDimensions.user_role) == "dss", "Seller",
                           tostring(customDimensions.user_role) == "se", "SE", "Unknown"),
          se_specialty = case(tostring(customDimensions.user_role) == "dss", "Not applicable",
                              tostring(customDimensions.user_role) != "se", "Unknown",
                              tostring(customDimensions.se_specialty) == "data", "Data",
                              tostring(customDimensions.se_specialty) == "infra", "Infrastructure",
                              tostring(customDimensions.se_specialty) == "apps_ai", "Apps and AI",
                              tostring(customDimensions.se_specialty) == "other", "Other", "Unknown")
"""


def _q_activity_summary(days: int) -> str:
    """Count distinct active installs exactly before aggregating across the period."""
    return f"""
{_page_events(days)}
| summarize last_active = max(timestamp), page_loads = count() by instance_id
| summarize active_installs = count(),
            active_today = countif(last_active >= startofday(now())),
            page_loads = sum(page_loads)
"""


def _q_dau(days: int) -> str:
    return f"""
let Activity = {_page_events(days)}
| summarize arg_max(timestamp, user_role) by day = startofday(timestamp), instance_id
| summarize DAU = count() by timestamp = day, user_role;
range timestamp from startofday(ago({days - 1}d)) to startofday(now()) step 1d
| join kind=leftouter Activity on timestamp
| project timestamp, user_role = coalesce(user_role, "Unknown"), DAU = coalesce(DAU, long(0))
| order by timestamp asc
"""


def _q_installs_by_role(days: int) -> str:
    """Group reporting installs by their latest saved role."""
    return f"""
{_latest_installs(days)}
| summarize installs = count() by user_role
| order by installs desc
"""


def _q_specialties(days: int) -> str:
    """Only SE installs contribute to the specialty mix, never sellers' saved specialties."""
    return f"""
{_latest_installs(days)}
| where user_role == "SE"
| summarize installs = count() by se_specialty
| order by installs desc, se_specialty asc
"""


def _q_install_inventory(days: int) -> str:
    """List every reporting install with its latest code and separate page activity."""
    return f"""
let Activity = {_page_events(days)}
| summarize last_active_utc = max(timestamp), page_loads = count() by instance_id;
{_latest_installs(days)}
| join kind=leftouter Activity on instance_id
| project instance_id, app_version, user_role, se_specialty, last_seen_utc,
          last_active_utc, page_loads = coalesce(page_loads, long(0))
| order by last_seen_utc desc, instance_id asc
"""


def _q_page_loads(days: int) -> str:
    """Rank all pages, counting each install once per page for reach."""
    return f"""
{_page_events(days)}
| where isnotempty(feature)
| summarize loads = count() by feature, page, instance_id
| summarize page_loads = sum(loads), active_installs = count() by feature, page
| order by page_loads desc, feature asc
"""


def _q_flag_adoption() -> str:
    return """
let LatestProfile = customEvents
| where name == "SalesBuddy.InstallProfile"
| where timestamp > ago(30d)
| extend instance_id = tostring(customDimensions.instance_id)
| summarize arg_max(timestamp, *) by instance_id;
LatestProfile
| summarize
    total = dcount(instance_id),
    msx_writeback     = dcountif(instance_id, tostring(customDimensions.msx_auto_writeback) == "true"),
    copilot_actions   = dcountif(instance_id, tostring(customDimensions.copilot_actions_enabled) == "true"),
    stale_milestones  = dcountif(instance_id, tostring(customDimensions.show_stale_milestones) == "true"),
    hygiene_tasks     = dcountif(instance_id, tostring(customDimensions.show_hygiene_tasks) == "true"),
    auto_sync         = dcountif(instance_id, tostring(customDimensions.milestone_auto_sync) == "true"),
    workiq_connect    = dcountif(instance_id, tostring(customDimensions.workiq_connect_impact) == "true"),
    custom_workiq_prompt = dcountif(instance_id, tostring(customDimensions.has_workiq_prompt) == "true")
| project Feature = pack_array("MSX writeback","Copilot actions","Stale milestones","Hygiene tasks","Milestone auto-sync","WorkIQ connect impact","Custom WorkIQ prompt"),
          Installs = pack_array(msx_writeback, copilot_actions, stale_milestones, hygiene_tasks, auto_sync, workiq_connect, custom_workiq_prompt),
          Total = total
| mv-expand Feature to typeof(string), Installs to typeof(long)
| extend AdoptionPct = round(100.0 * Installs / Total, 1)
| project Feature, Installs, Total, AdoptionPct
| order by AdoptionPct desc
"""


def _q_adoption_by_role() -> str:
    return """
let LatestProfile = customEvents
| where name == "SalesBuddy.InstallProfile"
| where timestamp > ago(30d)
| extend instance_id = tostring(customDimensions.instance_id)
| summarize arg_max(timestamp, *) by instance_id
| extend user_role = tostring(customDimensions.user_role);
LatestProfile
| where user_role in ("se", "dss")
| summarize
    installs = dcount(instance_id),
    msx_writeback_pct = round(100.0 * dcountif(instance_id, tostring(customDimensions.msx_auto_writeback) == "true")    / dcount(instance_id), 1),
    copilot_pct       = round(100.0 * dcountif(instance_id, tostring(customDimensions.copilot_actions_enabled) == "true") / dcount(instance_id), 1),
    stale_pct         = round(100.0 * dcountif(instance_id, tostring(customDimensions.show_stale_milestones) == "true")   / dcount(instance_id), 1),
    auto_sync_pct     = round(100.0 * dcountif(instance_id, tostring(customDimensions.milestone_auto_sync) == "true")     / dcount(instance_id), 1)
    by user_role
| order by user_role asc
"""


def _q_entity_counts() -> str:
    return """
let LatestProfile = customEvents
| where name == "SalesBuddy.InstallProfile"
| where timestamp > ago(30d)
| extend instance_id = tostring(customDimensions.instance_id)
| summarize arg_max(timestamp, *) by instance_id
| extend user_role = tostring(customDimensions.user_role);
LatestProfile
| summarize
    p50_notes      = percentile(toreal(customMeasurements.note_count), 50),
    p90_notes      = percentile(toreal(customMeasurements.note_count), 90),
    p50_customers  = percentile(toreal(customMeasurements.customer_count), 50),
    p90_customers  = percentile(toreal(customMeasurements.customer_count), 90),
    p50_engagements= percentile(toreal(customMeasurements.engagement_count), 50),
    p90_engagements= percentile(toreal(customMeasurements.engagement_count), 90),
    p50_milestones = percentile(toreal(customMeasurements.milestone_count), 50),
    p90_milestones = percentile(toreal(customMeasurements.milestone_count), 90)
    by user_role
| order by user_role asc
"""


def _q_top_features(days: int) -> str:
    return f"""
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago({days}d)
| extend instance_id = tostring(customDimensions.instance_id),
         feature = tostring(customDimensions.feature)
| where isnotempty(feature)
| summarize
    requests = count(),
    installs = dcount(instance_id)
    by feature
| order by installs desc, requests desc
| take 30
"""


def _q_reports(days: int) -> str:
    return f"""
{_page_events(days)}
| where feature startswith "reports."
| where isnotempty(feature)
| extend report = iff(feature startswith "reports.", substring(feature, 8), feature)
| summarize loads = count() by report, instance_id
| summarize report_views = sum(loads), installs = count() by report
| order by installs desc, report_views desc
"""


def _q_power_features(days: int) -> str:
    return f"""
let LatestProfile = customEvents
| where name == "SalesBuddy.InstallProfile"
| where timestamp > ago(30d)
| extend instance_id = tostring(customDimensions.instance_id)
| summarize arg_max(timestamp, *) by instance_id
| extend user_role = tostring(customDimensions.user_role)
| project instance_id, user_role;
let interesting = dynamic([
    "revenue.dashboard", "revenue.import",
    "main.action_items_hub", "milestones.tracker",
    "ai.fill_my_day", "engagements.hub",
    "partners.list", "projects.list"
]);
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago({days}d)
| extend feature = tostring(customDimensions.feature),
         instance_id = tostring(customDimensions.instance_id)
| where feature in (interesting)
| join kind=leftouter LatestProfile on instance_id
| summarize
    installs_using = dcount(instance_id),
    se_installs    = dcountif(instance_id, user_role == "se"),
    dss_installs   = dcountif(instance_id, user_role == "dss"),
    unknown_installs = dcountif(instance_id, user_role == "unknown" or isempty(user_role))
    by feature
| order by installs_using desc
"""


def _q_errors(days: int) -> str:
    return f"""
customEvents
| where name == "SalesBuddy.FeatureUsage"
| where timestamp > ago({days}d)
| extend feature = tostring(customDimensions.feature),
         is_error = toreal(customMeasurements.is_error)
| summarize
    requests = count(),
    errors = countif(is_error == 1.0),
    error_pct = round(100.0 * countif(is_error == 1.0) / count(), 2),
    p50_ms = percentile(toreal(customMeasurements.response_time_ms), 50),
    p95_ms = percentile(toreal(customMeasurements.response_time_ms), 95)
    by feature
| where requests > 20
| order by error_pct desc, p95_ms desc
| take 25
"""


def _q_workiq_uptime_timeline(days: int) -> str:
    """Hourly bucket showing whether WorkIQ subprocess succeeded.

    Shows one cell per hour, colored by status.
    Powered by SalesBuddy.WorkIQCall events that workiq_service emits at
    every call site (status='ok' or 'server_down'). Parser failure and recovery
    events are excluded - parser drift is a separate concern from uptime.
    """
    return f"""
customEvents
| where name == "SalesBuddy.WorkIQCall"
| where timestamp > ago({days}d)
| extend status = tostring(customDimensions.status)
| where status in ("ok", "server_down")
| summarize events = count() by bin(timestamp, 1h), status
| order by timestamp asc
"""


def _q_workiq_parse_failures(days: int) -> str:
    """Per-(operation, failure_type) parse failure counts.

    A non-empty result means a WorkIQ output format drifted and the
    matching parser needs updating. Each row identifies which
    query_workiq() caller couldn't read the response.
    """
    return f"""
customEvents
| where name == "SalesBuddy.WorkIQCall"
| where timestamp > ago({days}d)
| extend status = tostring(customDimensions.status),
         operation = tostring(customDimensions.operation),
         failure_type = tostring(customDimensions.failure_type),
         instance_id = tostring(customDimensions.instance_id)
| where status == "parse_failed"
| summarize
    failures = count(),
    installs_affected = dcount(instance_id)
    by operation, failure_type
| order by failures desc
"""


def _q_workiq_uptime_summary(days: int) -> str:
    """Single-row uptime % over the window for the card header."""
    return f"""
customEvents
| where name == "SalesBuddy.WorkIQCall"
| where timestamp > ago({days}d)
| extend status = tostring(customDimensions.status)
| where status in ("ok", "server_down")
| summarize
    total = count(),
    ok = countif(status == "ok"),
    server_down = countif(status == "server_down")
| extend uptime_pct = iff(total > 0, round(100.0 * ok / total, 1), real(null))
"""


def _q_app_versions(days: int) -> str:
    """Count the latest observed version once per reporting install."""
    return f"""
{_latest_installs(days)}
| summarize installs = count() by app_version
| order by installs desc
"""


def _safe_query(title: str, kql: str, timespan: str) -> dict:
    """Run a query, returning ``{rows, error}`` so one failure can't 500 the page."""
    try:
        rows = query_to_dicts(kql, timespan=timespan)
        return {'title': title, 'rows': rows, 'error': None}
    except AppInsightsError as exc:
        logger.warning(f'Metrics card "{title}" failed: {exc}')
        return {
            'title': title,
            'rows': [],
            'error': str(exc),
            'status_code': exc.status_code,
            # The UI uses this flag to show a "sign in to BlaineCorp"
            # button instead of just the raw error text.
            'auth_required': is_auth_error(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(f'Metrics card "{title}" raised unexpectedly')
        return {'title': title, 'rows': [], 'error': str(exc)}


def _coerce_days(raw) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 7
    return days if days in (1, 7, 30, 90) else 7


# Card key -> (title, builder(days) -> kql, timespan_resolver(days) -> timespan).
# Cards whose query depends on `days` use the dynamic timespan; cards that
# always look at the last 30d use a fixed P30D so the App Insights backend
# can short-circuit the scan.
def _card_specs(days: int) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
    """Return (specs, ordered_keys).

    The metrics page renders cards in ``ordered_keys`` order. New cards
    should be added in the desired position rather than appended.
    """
    timespan = f'P{days}D'
    specs = {
        'activity_summary':      ('Activity summary',           _q_activity_summary(days),       timespan),
        'dau':                   ('Daily active installs',      _q_dau(days),                    timespan),
        'page_loads':            ('Page loads',                  _q_page_loads(days),             timespan),
        'install_inventory':     ('Install inventory',           _q_install_inventory(days),      timespan),
        'installs_by_role':      ('Reporting installs by role',  _q_installs_by_role(days),        timespan),
        'se_specialties':        ('SE specialty mix',            _q_specialties(days),             timespan),
        'app_versions':          ('App version distribution',    _q_app_versions(days),            timespan),
        'workiq_uptime':         ('WorkIQ uptime',              _q_workiq_uptime_timeline(days), timespan),
        'workiq_uptime_summary': ('WorkIQ uptime summary',      _q_workiq_uptime_summary(days),  timespan),
        'workiq_parse_failures': ('WorkIQ parser failures',     _q_workiq_parse_failures(days),  timespan),
        'flag_adoption':         ('Feature flag adoption',      _q_flag_adoption(),              'P30D'),
        'adoption_by_role':      ('Adoption by role',           _q_adoption_by_role(),           'P30D'),
        'entity_counts':         ('Entity-count distribution',  _q_entity_counts(),              'P30D'),
        'top_features':          ('Top 30 features',            _q_top_features(days),           timespan),
        'reports':               ('Reports opened',             _q_reports(days),                timespan),
        'power_features':        ('Power-feature adoption', _q_power_features(days), f'P{max(days, 30)}D'),
        'errors':                ('Error rate & latency',       _q_errors(days),                 timespan),
    }
    return specs, list(specs.keys())


def get_metric_card(card_id: str, days: int = 7) -> dict:
    """Query a known insight, preserving explicit auth and query errors for all consumers."""
    specs, _ = _card_specs(_coerce_days(days))
    spec = specs.get(card_id)
    if spec is None:
        raise ValueError(f'Unknown metrics card: {card_id}')
    title, kql, timespan = spec
    result = _safe_query(title, kql, timespan)
    if card_id == 'app_versions' and result['rows']:
        commit_dates = get_commit_dates([row['app_version'] for row in result['rows']])
        result['rows'] = [
            {
                'app_version': row['app_version'],
                'commit_date': commit_dates[row['app_version']],
                'installs': row['installs'],
            }
            for row in result['rows']
        ]
    return {
        'card_id': card_id,
        'title': result['title'],
        'rows': result['rows'],
        'error': result.get('error'),
        'status_code': result.get('status_code'),
        'auth_required': result.get('auth_required', False),
    }
