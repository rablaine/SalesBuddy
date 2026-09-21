"""
Programmatic MSXI U2C (Uncommitted-to-Committed) snapshot pull.

Reads the same Power BI visual that backs the MSX Insights "Uncommitted to
Committed" milestone detail table, so the baseline we store matches the official
report row for row instead of being re-derived from our own milestone cache.

The report is scoped by the territories configured in Sales Buddy: MSXi filters
``DimCustomer.SalesTerritory`` to the selected sales territories exactly the way
the report's territory slicer does.

Auth flow mirrors ``revenue_pull`` (no browser, no manual token):
1. Acquire a Power BI token (``analysis.windows.net/powerbi/api``) via the Azure
   CLI credential - the same ``az login`` we use for the AI gateway.
2. GET the report's ``modelsAndExploration``; its response hands back an
   **MWCToken** (the dedicated-capacity workload token) minted for our identity.
3. POST the semantic query to the capacity's QES ``public/query`` endpoint with
   that MWCToken. Row-level security scopes results to our MSX-assigned accounts.

Results are read-only. Nothing here writes to the database - ``u2c_snapshot``
owns the write side.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class U2CPullError(Exception):
    """Raised when a programmatic U2C snapshot pull fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CORP_TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
_PBI_RESOURCE = "https://analysis.windows.net/powerbi/api"
_CLUSTER = "https://df-msit-scus-redirect.analysis.windows.net"

# The MSX Insights "Uncommitted to Committed" report + dataset, and the visual
# id of the milestone detail table on the C2C page.
_REPORT_ID = "4cbcaf97-7a93-4db9-a1d8-fdaa5b1d17f5"
_DATASET_ID = "0968ed6a-aa37-4de9-a781-5f6ea0db72ef"
_VISUAL_ID = "b913f00e22d540ebac90"
_MODEL_ID_FALLBACK = 6659445

# A report-level filter the published report carries: one bad snapshot load is
# excluded by date id. Kept so our rows match the portal exactly.
_EXCLUDED_SNAPSHOT_DATE_ID = "20260721"

_AUTH_SCHEME = "Bearer"

# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------
_pbi_token: Optional[str] = None
_pbi_expiry: float = 0.0


def _decode_jwt(tok: str) -> dict:
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - a malformed token is simply "no claims"
        return {}


def _get_pbi_token() -> str:
    """Acquire (and cache) a Power BI AAD token via the Azure CLI credential."""
    global _pbi_token, _pbi_expiry
    if _pbi_token and time.time() < _pbi_expiry - 60:
        return _pbi_token

    from azure.identity import AzureCliCredential, DefaultAzureCredential

    scope = f"{_PBI_RESOURCE}/.default"
    for kwargs in ({"tenant_id": _CORP_TENANT}, {}):
        try:
            tok = AzureCliCredential(**kwargs).get_token(scope)
            _pbi_token, _pbi_expiry = tok.token, tok.expires_on
            return _pbi_token
        except Exception as exc:  # noqa: BLE001 - try next strategy
            logger.warning("AzureCliCredential (%s) failed: %s", kwargs or "default", exc)
    try:
        tok = DefaultAzureCredential().get_token(scope)
        _pbi_token, _pbi_expiry = tok.token, tok.expires_on
        return _pbi_token
    except Exception as exc:  # noqa: BLE001
        raise U2CPullError(
            "Could not acquire a Power BI token. Run `az login` with your "
            "Microsoft corporate account and make sure you're on the VPN."
        ) from exc


# ---------------------------------------------------------------------------
# MWCToken mint (from modelsAndExploration) + capacity resolution
# ---------------------------------------------------------------------------
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")

_mwc_token: Optional[str] = None
_mwc_expiry: float = 0.0
_qes_url: Optional[str] = None
_model_id: int = _MODEL_ID_FALLBACK
_MWC_MINT_ATTEMPTS = 3


def _is_mwc(tok: str) -> bool:
    claims = _decode_jwt(tok)
    return claims.get("tokenType") == "MwcToken" or "pbidedicated" in str(claims.get("iss", ""))


def _qes_url_from_mwc(mwc: str) -> str:
    """Build the capacity's QES query URL from the MWCToken's own claims."""
    claims = _decode_jwt(mwc)
    capacity = claims.get("customerCapacityObjectId")
    fqdn = claims.get("rolloutFqdn") or "msit.pbidedicated.windows.net"
    if not capacity:
        raise U2CPullError("MWCToken missing capacity id")
    suffix = fqdn.split(".", 1)[1] if "." in fqdn else "pbidedicated.windows.net"
    host = capacity.replace("-", "") + "." + suffix
    return (f"https://{host}/webapi/capacities/{capacity}/workloads/QES/"
            f"QueryExecutionService/automatic/public/query")


def _mint_mwc(session: requests.Session) -> str:
    """Mint an MWCToken by loading the report's models; cache until near expiry."""
    global _mwc_token, _mwc_expiry, _qes_url, _model_id
    if _mwc_token and time.time() < _mwc_expiry - 60 and _qes_url:
        return _mwc_token

    url = f"{_CLUSTER}/explore/reports/{_REPORT_ID}/modelsAndExploration?preferReadOnlySession=true"
    headers = {
        "Authorization": f"{_AUTH_SCHEME} {_get_pbi_token()}",
        "accept": "application/json",
        # The report is embedded by MSX Insights; the embed host env header is
        # what the portal sends and what the service expects for this artifact.
        "x-powerbi-hostenv": "Embed for Organization",
        "origin": "https://msit.powerbi.com",
        "referer": "https://msit.powerbi.com/",
    }
    resp = None
    for attempt in range(_MWC_MINT_ATTEMPTS):
        try:
            resp = session.get(url, headers=headers, timeout=(20, 180))
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == _MWC_MINT_ATTEMPTS - 1:
                raise U2CPullError(
                    f"modelsAndExploration connection failed after "
                    f"{_MWC_MINT_ATTEMPTS} attempts: {exc}"
                ) from exc
            time.sleep(1.5 * (attempt + 1))
    if resp is None or not resp.ok:
        code = resp.status_code if resp is not None else "no response"
        body = resp.text[:200] if resp is not None else ""
        raise U2CPullError(f"modelsAndExploration {code}: {body}")

    mwc = next((c for c in _JWT_RE.findall(resp.text) if _is_mwc(c)), None)
    if not mwc:
        raise U2CPullError("No MWCToken in modelsAndExploration response")

    # Resolve the numeric model id for this dataset (fall back to the known one).
    try:
        for m in (resp.json().get("models") or []):
            if m.get("dbName") == _DATASET_ID and m.get("id"):
                _model_id = int(m["id"])
                break
    except Exception:  # noqa: BLE001 - keep the fallback model id
        pass

    _mwc_token = mwc
    _mwc_expiry = float(_decode_jwt(mwc).get("exp") or (time.time() + 1500))
    _qes_url = _qes_url_from_mwc(mwc)
    return _mwc_token


def clear_token_cache() -> None:
    """Clear cached Power BI + MWC tokens (call after a fresh ``az login``)."""
    global _pbi_token, _pbi_expiry, _mwc_token, _mwc_expiry, _qes_url
    _pbi_token = _mwc_token = _qes_url = None
    _pbi_expiry = _mwc_expiry = 0.0


# ---------------------------------------------------------------------------
# Semantic-query builders + DSR decode
# ---------------------------------------------------------------------------
def _src(s: str, p: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}}


def _col(s: str, p: str, n: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}, "Name": n}


def _mea(s: str, p: str, n: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}, "Name": n}


def _in(s: str, p: str, vals: list[str]) -> dict:
    return {"Condition": {"In": {
        "Expressions": [_src(s, p)],
        "Values": [[{"Literal": {"Value": v}}] for v in vals]}}}


def _not_in(s: str, p: str, vals: list[str]) -> dict:
    return {"Condition": {"Not": {"Expression": {"In": {
        "Expressions": [_src(s, p)],
        "Values": [[{"Literal": {"Value": v}}] for v in vals]}}}}}


def _lit(value: str) -> str:
    """Quote a string literal the way the Power BI query language expects."""
    return "'" + value.replace("'", "''") + "'"


def _decode(data: dict) -> list[dict]:
    """Expand Power BI's DSR (delta-compressed or object form) into row dicts.

    The visual asks for a subtotal row, so the payload carries two projection
    levels: ``DM0`` holds the grand total and the deepest ``DM*`` holds the
    detail rows. Only the deepest level is returned.
    """
    ds_list = ((data or {}).get("dsr") or {}).get("DS") or []
    if not ds_list:
        return []
    ds = ds_list[0]
    select = ((data or {}).get("descriptor") or {}).get("Select") or []
    names = [s.get("Name") for s in select]

    dm: list[dict] = []
    for level in (ds.get("PH") or []):
        for key, value in level.items():
            if key.startswith("DM") and value:
                dm = value
    if not dm:
        return []

    dicts = ds.get("ValueDicts") or {}
    schema = dm[0].get("S") or []
    order = []
    for s in schema:
        idx = next((i for i, d in enumerate(select) if d.get("Value") == s.get("N")), -1)
        order.append(idx if idx >= 0 else 0)

    rows: list[dict] = []
    prev: list[Any] = [None] * len(schema)
    for r in dm:
        c = r.get("C") or []
        reuse = r.get("R") or 0
        nulls = r.get("Ø") or 0
        out: list[Any] = [None] * len(schema)
        ci = 0
        for i in range(len(schema)):
            bit = 1 << i
            key = schema[i].get("N")
            if nulls & bit:
                v = None
            elif reuse & bit:
                v = prev[i]
            elif key in r:
                v = r[key]
            else:
                v = c[ci] if ci < len(c) else None
                ci += 1
            dn = schema[i].get("DN")
            if v is not None and dn and dn in dicts and isinstance(v, int) and not isinstance(v, bool):
                dictionary = dicts[dn]
                if 0 <= v < len(dictionary):
                    v = dictionary[v]
            out[i] = v
        prev = out
        rows.append({names[order[i]]: out[i] for i in range(len(schema))})
    return rows


# ---------------------------------------------------------------------------
# Query definition
# ---------------------------------------------------------------------------
# Column order matters: the binding projects positionally.
_SELECT_SPECS: list[tuple[str, str, str, bool]] = [
    # (source, property, output key, is_measure)
    ("d", "TranslatedAccountName", "customer_name", False),
    ("f", "MilestoneName", "milestone_name", False),
    ("m", "$ Starting Uncommitted Pipeline", "starting_acr", True),
    ("m", "$ Uncommited to Commited Pipeline (Total)", "converted_acr", True),
    ("f", "PrevCommitmentRecommendation", "starting_commitment", False),
    ("f", "CommitmentRecommendation", "current_commitment", False),
    ("f", "MilestoneOwnerAlias", "owner_alias", False),
    ("f", "MilestoneOwnerRole", "owner_role", False),
    ("f", "MilestoneOwnerManagerAlias", "owner_manager_alias", False),
    ("f", "PrevMilestoneStatus", "starting_status", False),
    ("f", "MilestoneStatus", "current_status", False),
    ("f", "PrevEstDate", "starting_due_date", False),
    ("f", "EstDate", "current_due_date", False),
    ("f", "PrevSalesStageName", "starting_sales_stage", False),
    ("f", "SalesStageName", "current_sales_stage", False),
    ("f", "MilestoneCategory", "milestone_category", False),
    ("f", "NonRecurring", "non_recurring", False),
    ("f", "MilestonePartnerName", "partner_name", False),
    ("f", "MilestoneNumber", "milestone_number", False),
    ("f", "OpportunityNumber", "opportunity_number", False),
]

_FROM = [
    {"Name": "d", "Entity": "DimCustomerStartOfQuarter", "Type": 0},
    {"Name": "f", "Entity": "FactAzureConsumptionPipelineC2CSnapshots", "Type": 0},
    {"Name": "m", "Entity": "Measures | Pipeline", "Type": 0},
    {"Name": "d1", "Entity": "DimDate", "Type": 0},
    {"Name": "p", "Entity": "Parameter | Field Hierarchy", "Type": 0},
    {"Name": "d11", "Entity": "DimCustomer", "Type": 0},
    {"Name": "d2", "Entity": "DimAccountSummaryGrouping", "Type": 0},
    {"Name": "d3", "Entity": "DimMilestone", "Type": 0},
    {"Name": "d4", "Entity": "DimWorkload", "Type": 0},
]


def _u2c_query(territories: list[str], fiscal_year: str, due_quarter: str) -> dict:
    """Build the milestone-detail semantic query for the given territories.

    Args:
        territories: ``DimCustomer.SalesTerritory`` values (e.g. ``East.SMECC.MAA.0101``).
        fiscal_year: MSXi fiscal year label, e.g. ``FY27``.
        due_quarter: MSXi due-quarter label, e.g. ``FY27-Q1``.
    """
    select = [
        _mea(s, p, n) if is_measure else _col(s, p, n)
        for s, p, n, is_measure in _SELECT_SPECS
    ]
    return {
        "Version": 2,
        "From": _FROM,
        "Select": select,
        "Where": [
            _in("d1", "FiscalYear", [_lit(fiscal_year)]),
            # The field-hierarchy parameter is what makes the pipeline measures
            # resolve at all, so it is not optional.
            _in("p", "Parameter | Field Hierarchy", [
                _lit("FieldBigArea"), _lit("FieldWWRegion"),
                _lit("FieldArea"), _lit("FieldAccountabilityUnit"),
            ]),
            _in("f", "PrevMilestoneCategory", [_lit("POC/Pilot"), _lit("Production")]),
            _not_in("f", "PrevMilestoneStatus", [
                _lit("Cancelled"), _lit("Hygiene/Duplicate"), _lit("Lost to Competitor"),
            ]),
            _not_in("d11", "SegmentGroup", [_lit("MS Elevate"), _lit("SME&C SMB")]),
            _not_in("f", "PrevPositiveNegativePipeline", [_lit("Negative Milestones")]),
            _in("f", "PrevDueQuarter", [_lit(due_quarter)]),
            _in("d1", "QtrRel", [_lit("CQ")]),
            _in("f", "SnapshotDateID", [_lit("current")]),
            _in("d11", "SalesTerritory", [_lit(t) for t in territories]),
            _in("f", "PrevCommitmentRecommendation", [_lit("Uncommitted")]),
            _not_in("f", "PrevSalesStageName", ["null", _lit("Listen & Consult")]),
            _not_in("d2", "FieldWWRegion", [
                _lit("Corp HQ"), _lit("EMEA HQ"),
                _lit("Field HQ - SME&C"), _lit("Field HQ - Enterprise"),
            ]),
            _in("d1", "FYRel", [
                _lit("FY"), _lit("FY+1"), _lit("FY-1"), _lit("FY-2"), _lit("FY-3"),
            ]),
            _not_in("d2", "FieldSummarySegment", [
                "null", _lit("Non-Transactional"), _lit("UNKNOWN"),
            ]),
            _not_in("d3", "Workload", [_lit("Data: MIDP ISV Marketplace")]),
            _not_in("d4", "Workload", [_lit("Data: MIDP ISV Marketplace")]),
            _in("d4", "WorkloadType", [_lit("Azure")]),
            _not_in("f", "SnapshotDateID", [_lit(_EXCLUDED_SNAPSHOT_DATE_ID)]),
        ],
        "OrderBy": [{"Direction": 1, "Expression": _src("d", "TranslatedAccountName")}],
    }


# ---------------------------------------------------------------------------
# QES query execution
# ---------------------------------------------------------------------------
# The capacity caps every response at 30,000 rows regardless of the window we
# ask for. Truncation is signalled by IC=false plus an RT restart token, which
# we replay to fetch the next page.
_MAX_WINDOW = 30000
_MAX_PAGES = 50


def _qes_post_page(session: requests.Session, query: dict,
                   restart: Optional[list] = None,
                   retries: int = 2) -> tuple[list[dict], Optional[list]]:
    """POST one page. Returns (rows, restart_token_for_next_page_or_None)."""
    mwc = _mint_mwc(session)
    window: dict[str, Any] = {"Count": _MAX_WINDOW}
    if restart:
        window["RestartTokens"] = restart
    body = {
        "version": "1.0.0",
        "queries": [{
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                "Query": query,
                "Binding": {
                    "Primary": {"Groupings": [{"Projections": list(range(len(query["Select"])))}]},
                    "DataReduction": {"DataVolume": 3, "Primary": {"Window": window}},
                    "Version": 1,
                },
                "ExecutionMetricsKind": 1,
            }}]},
            "QueryId": "",
            "ApplicationContext": {"DatasetId": _DATASET_ID,
                                   "Sources": [{"ReportId": _REPORT_ID, "VisualId": _VISUAL_ID}]},
        }],
        "cancelQueries": [], "modelId": _model_id,
        "userPreferredLocale": "en-US", "allowLongRunningQueries": True,
    }
    rid = str(uuid.uuid4())
    headers = {
        "authorization": f"MWCToken {mwc}",
        "content-type": "application/json;charset=UTF-8",
        "activityid": str(uuid.uuid4()), "requestid": rid,
        "x-ms-parent-activity-id": rid, "x-ms-root-activity-id": rid,
        "x-ms-workload-resource-moniker": _DATASET_ID,
        "origin": "https://msit.powerbi.com", "referer": "https://msit.powerbi.com/",
    }
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = session.post(_qes_url, headers=headers, data=json.dumps(body), timeout=(20, 300))
            if resp.status_code == 401:
                clear_token_cache()  # token expired mid-run - re-mint and retry
                headers["authorization"] = f"MWCToken {_mint_mwc(session)}"
                raise U2CPullError("QES 401 (token refreshed)")
            if not resp.ok:
                raise U2CPullError(f"QES {resp.status_code}: {resp.text[:200]}")
            res = json.loads(resp.text.lstrip("\ufeff"))["results"][0]["result"]
            if "error" in res:
                raise U2CPullError("QES error: " + json.dumps(res["error"])[:200])
            data = res.get("data") or {}
            ds = ((data.get("dsr") or {}).get("DS") or [{}])[0]
            rows = _decode(data)
            # IC (IsComplete) false means more rows exist; RT is the cursor.
            next_rt = ds.get("RT") if ds.get("IC") is False else None
            if ds.get("IC") is False and not next_rt:
                raise U2CPullError(
                    "QES truncated the result but returned no restart token; "
                    "refusing to use a partial snapshot."
                )
            return rows, next_rt
        except Exception as exc:  # noqa: BLE001 - retry with backoff
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def _qes_post(session: requests.Session, query: dict, retries: int = 2) -> list[dict]:
    """Run a query to completion, following restart tokens across pages."""
    out: list[dict] = []
    seen: set[str] = set()
    restart: Optional[list] = None
    for _ in range(_MAX_PAGES):
        rows, restart = _qes_post_page(session, query, restart=restart, retries=retries)
        for row in rows:
            identity = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            if identity not in seen:
                seen.add(identity)
                out.append(row)
        if not restart:
            return out
    raise U2CPullError(
        f"QES pagination exceeded {_MAX_PAGES} pages ({len(out)} rows); aborting."
    )


# ---------------------------------------------------------------------------
# Fiscal quarter translation
# ---------------------------------------------------------------------------
_EPOCH = datetime(1970, 1, 1)


def msxi_quarter_labels(fq_label: str) -> tuple[str, str]:
    """Translate a Sales Buddy FQ label into the MSXi filter labels.

    Args:
        fq_label: Sales Buddy fiscal quarter, e.g. ``'FY27 Q1'``.

    Returns:
        Tuple of (fiscal year label, due quarter label), e.g. ``('FY27', 'FY27-Q1')``.

    Raises:
        U2CPullError: If the label cannot be parsed.
    """
    match = re.fullmatch(r"\s*(FY\d{2})\s*[- ]?\s*Q([1-4])\s*", (fq_label or "").upper())
    if not match:
        raise U2CPullError(f"Unrecognised fiscal quarter label: {fq_label!r}")
    fy, q = match.group(1), match.group(2)
    return fy, f"{fy}-Q{q}"


def _to_date(value: Any) -> Optional[date]:
    """Convert a Power BI epoch-millisecond value into a date."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    try:
        return (_EPOCH + timedelta(milliseconds=float(value))).date()
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Public pull
# ---------------------------------------------------------------------------
def get_territory_codes() -> list[str]:
    """Return the sales territory codes configured in Sales Buddy."""
    from app.models import Territory

    codes = []
    for (name,) in Territory.query.with_entities(Territory.name).all():
        cleaned = (name or "").strip()
        if cleaned and cleaned not in codes:
            codes.append(cleaned)
    return codes


def pull_u2c_milestones(fq_label: str,
                        territories: Optional[list[str]] = None) -> list[dict]:
    """Pull the official MSXi U2C milestone baseline for a fiscal quarter.

    Args:
        fq_label: Sales Buddy fiscal quarter label, e.g. ``'FY27 Q1'``.
        territories: Sales territory codes to scope to. Defaults to the
            territories configured in Sales Buddy.

    Returns:
        One dict per milestone with the keys named in ``_SELECT_SPECS``, with the
        two date columns converted to ``date`` objects and ACR values coerced to
        floats.

    Raises:
        U2CPullError: If no territories are configured or the query fails.
    """
    codes = territories if territories is not None else get_territory_codes()
    if not codes:
        raise U2CPullError(
            "No territories configured. Add your sales territories in Sales Buddy "
            "before importing the official snapshot."
        )

    fiscal_year, due_quarter = msxi_quarter_labels(fq_label)
    session = requests.Session()
    session.trust_env = False  # ignore env proxy that stalls the capacity handshake
    rows = _qes_post(session, _u2c_query(codes, fiscal_year, due_quarter))

    out: list[dict] = []
    for row in rows:
        record = dict(row)
        record["starting_due_date"] = _to_date(row.get("starting_due_date"))
        record["current_due_date"] = _to_date(row.get("current_due_date"))
        for key in ("starting_acr", "converted_acr"):
            try:
                record[key] = float(row.get(key) or 0.0)
            except (TypeError, ValueError):
                record[key] = 0.0
        out.append(record)

    logger.info(
        "MSXi U2C pull for %s across %d territories returned %d milestones",
        fq_label, len(codes), len(out),
    )
    return out
