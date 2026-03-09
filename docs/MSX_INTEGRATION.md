# MSX (Dynamics 365) Integration Guide

Everything we know about integrating with the MSX CRM (Dynamics 365) API — authentication, account discovery, milestones, tasks, opportunities, and team membership. Built from hard-won knowledge reverse-engineering the MSX Helper extension and trial-and-error with the OData API.

## Table of Contents

1. [Authentication](#authentication)
2. [Import Flow: Discovering Your Accounts](#import-flow-discovering-your-accounts)
3. [Fetching Account Team Members (Sellers & SEs)](#fetching-account-team-members-sellers--ses)
4. [Milestones](#milestones)
5. [Opportunities](#opportunities)
6. [Creating Tasks](#creating-tasks)
7. [HoK (Hands-on-Keyboard) Task Categories](#hok-hands-on-keyboard-task-categories)
8. [Deal Team / Access Team Membership](#deal-team--access-team-membership)
9. [Batch Requests](#batch-requests)
10. [Key Constants](#key-constants)
11. [Important Gotchas](#important-gotchas)
12. [Appendix: Device Code Flow with MSAL](#appendix-device-code-flow-with-msal)

---

## Authentication

### Overview

MSX uses Azure AD authentication. We leverage the Azure CLI's cached token — no app registration, no client secrets, no redirect URIs.

### Prerequisites

1. Azure CLI installed (`az --version`)
2. VPN connected (MSX CRM is internal to Microsoft)
3. User logged into the Microsoft corporate tenant

### Login Flow

```bash
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47
```

This opens a browser for interactive login. The token is cached locally by Azure CLI.

### Getting the Token Programmatically

```python
import subprocess
import json

CRM_RESOURCE = "https://microsoftsales.crm.dynamics.com"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"

def get_msx_token():
    """Get token via az CLI."""
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", CRM_RESOURCE,
         "--tenant", TENANT_ID,
         "--query", "accessToken",
         "-o", "tsv"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"az CLI failed: {result.stderr.strip()}")
    return result.stdout.strip()
```

### Required Headers

All API calls include these headers:

```python
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json",
    "OData-MaxVersion": "4.0",
    "OData-Version": "4.0",
    "Content-Type": "application/json",
    "Prefer": 'odata.include-annotations="*"',
    "Cache-Control": "no-cache",
    "If-None-Match": "",
}
```

The `Prefer: odata.include-annotations="*"` header is crucial — it returns formatted values for lookups and option sets (human-readable labels).

### Base URL

```
https://microsoftsales.crm.dynamics.com/api/data/v9.2
```

### Request Best Practices

MSX Helper wraps every call with:
- **Retry with exponential backoff** on 408, 429, 500, 502, 503, 504
- **Retry-After header support** for 429 throttling
- **Timeout** of 20–30s per request
- **In-memory cache** for GET responses (5 minutes for stable data like accounts)
- **OData injection prevention** — single quotes escaped: `value.replace("'", "''")`

---

## Import Flow: Discovering Your Accounts

### Goal

Find all accounts where the current user is assigned as a seller or solution engineer.

### Step 1: Get Current User ID

```
GET /api/data/v9.2/WhoAmI
```

Returns `{ UserId, BusinessUnitId, OrganizationId }`. The `UserId` is the current user's `systemuserid` GUID.

### Step 2: Query Account Team Assignments

```
GET /api/data/v9.2/msp_accountteams
    ?$filter=_msp_systemuserid_value eq {user_id}
    &$select=_msp_accountid_value,msp_qualifier2
    &$top=500
```

This entity maps users to accounts with their role (`msp_qualifier2`). A user can be assigned to the same account multiple times with different roles.

**Key Fields:**

| Field | Description |
|-------|-------------|
| `_msp_accountid_value` | Account GUID |
| `_msp_systemuserid_value` | User GUID |
| `msp_qualifier2` | Role: "Cloud & AI", "Cloud & AI-Acq", "Cloud & AI Data", etc. |
| `msp_qualifier1` | Org level: "Corporate", "Area", etc. |
| `msp_standardtitle` | Job title like "Specialists IC" |
| `msp_fullname` | User's display name |

**Relevant `msp_qualifier2` Values:**

| Value | Role |
|-------|------|
| `Cloud & AI` | Growth Seller (DSS) |
| `Cloud & AI-Acq` | Acquisition Seller |
| `Cloud & AI Data` | Data Solution Engineer |
| `Cloud & AI Infrastructure` | Infrastructure SE |
| `Cloud & AI Apps` | Apps SE |

Filter for these and extract unique `_msp_accountid_value` values — that's your account list.

### Step 3: Batch Query Account Details

```
GET /api/data/v9.2/accounts
    ?$filter=(accountid eq guid1 or accountid eq guid2 or ...)
    &$select=accountid,name,msp_mstopparentid,_territoryid_value
    &$top=100
```

| Field | Description |
|-------|-------------|
| `accountid` | Account GUID |
| `name` | Account/company name |
| `msp_mstopparentid` | TPID (Top Parent ID) — unique customer identifier |
| `_territoryid_value` | Territory GUID (lookup) |

### Step 4: Batch Query Territories

```
GET /api/data/v9.2/territories
    ?$filter=(territoryid eq guid1 or territoryid eq guid2 or ...)
    &$select=territoryid,name,msp_accountteamunitname
    &$top=100
```

**Territory Naming Convention:**

Territory names follow a pattern: `{Region}.{Segment}.{SubArea}.{Code}`

Example: `East.SMECC.MAA.0601`
- Region: East
- Segment: SMECC (SMB, Enterprise, Corporate, Commercial)
- SubArea: MAA (Mid-Atlantic Area)
- Code: 0601 (POD 06, territory 01)

Derive the POD name from the territory code:
```python
# "East.SMECC.MAA.0601" -> "East POD 06"
parts = territory_name.split(".")
region = parts[0]
pod_num = parts[-1][:2]
pod_name = f"{region} POD {pod_num}"
```

---

## Fetching Account Team Members (Sellers & SEs)

### The Problem: Record Limits and No $skip

Each account can have 250–300+ team members. MSX returns max 100 per query and **does not support `$skip`** for pagination on `msp_accountteams`.

### The Solution: Server-Side Filtering

Filter to reduce 300+ records to ~20–30 per account:

```
GET /api/data/v9.2/msp_accountteams
    ?$filter=_msp_accountid_value eq {account_id}
            and msp_qualifier1 eq 'Corporate'
            and startswith(msp_qualifier2,'Cloud ')
    &$select=msp_fullname,msp_qualifier2,msp_standardtitle,_msp_systemuserid_value
    &$top=100
```

- `msp_qualifier1 eq 'Corporate'` — Only corporate-level assignments
- `startswith(msp_qualifier2,'Cloud ')` — All Cloud & AI roles (sellers AND SEs)

### Batching Multiple Accounts

Query multiple accounts in one request:

```
GET /api/data/v9.2/msp_accountteams
    ?$filter=(_msp_accountid_value eq guid1 or _msp_accountid_value eq guid2 or ...)
            and msp_qualifier1 eq 'Corporate'
            and startswith(msp_qualifier2,'Cloud ')
    &$select=_msp_accountid_value,msp_fullname,msp_qualifier2,msp_standardtitle,_msp_systemuserid_value
    &$top=100
```

**Batch Size Calculation** (~22 records per account after filtering):
- 3 accounts: 66 records (safe)
- 4 accounts: 88 records (borderline)
- 5 accounts: 110 records (exceeds limit!)

**Use batch size of 3** to stay safely under 100.

### Identifying Sellers vs SEs

**Sellers:** `msp_qualifier2` = "Cloud & AI" or "Cloud & AI-Acq" AND `msp_standardtitle` contains "Specialists IC"

The title filter is important — without it you'll also get CSU, CSA, managers, and other roles.

```python
if qualifier2 in ("Cloud & AI", "Cloud & AI-Acq") and "Specialists IC" in standardtitle:
    seller_type = "Growth" if qualifier2 == "Cloud & AI" else "Acquisition"
```

**SEs:** Check `msp_qualifier2`:
```python
se_map = {
    "Cloud & AI Data": "data_se",
    "Cloud & AI Infrastructure": "infra_se",
    "Cloud & AI Apps": "apps_se",
}
```

### Looking Up User Aliases (Email)

Account team records include `_msp_systemuserid_value` — use it to look up email:

```
GET /api/data/v9.2/systemusers({systemuser_id})
    ?$select=domainname,internalemailaddress
```

Extract the alias from the email:
```python
email = data.get("domainname") or data.get("internalemailaddress") or ""
alias = email.split("@")[0] if "@" in email else None
```

The email also enables Teams chat deep links:
```
https://teams.microsoft.com/l/chat/0/0?users={email}
```

**On-demand approach:** Only look up aliases when creating a *new* seller or SE. If they already exist in the database, skip the lookup. This minimizes API calls after initial import.

---

## Milestones

### What Are Milestones?

Milestones (`msp_engagementmilestone`) are consumption plays — tracked customer engagements designed to drive Azure usage. They're linked to accounts via opportunities and can have tasks created against them.

### Query Milestones for an Account

```
GET /api/data/v9.2/msp_engagementmilestones
    ?$filter=_msp_parentaccount_value eq '{account_id}'
    &$select=msp_engagementmilestoneid,msp_name,msp_milestonestatus,
             msp_milestonenumber,_msp_opportunityid_value,msp_monthlyuse,
             _msp_workloadlkid_value
    &$orderby=msp_name
```

### Query Milestones for an Opportunity

```
GET /api/data/v9.2/msp_engagementmilestones
    ?$filter=_msp_opportunityid_value eq '{opportunity_id}'
    &$orderby=msp_milestonedate
```

Returns: `msp_engagementmilestoneid`, `msp_milestonenumber`, `msp_name`, `msp_milestonedate`, `msp_milestonestatus`, `msp_monthlyuse`, `msp_commitmentrecommendation`, `msp_milestonecategory`, `_ownerid_value`, `_msp_workloadlkid_value`, `_msp_opportunityid_value`.

### Search by Milestone Number

```
GET /api/data/v9.2/msp_engagementmilestones
    ?$filter=msp_milestonenumber eq '{number}'
```

### Load Single Milestone

```
GET /api/data/v9.2/msp_engagementmilestones({milestone_id})
    ?$select=msp_engagementmilestoneid,msp_milestonenumber,msp_name,
             _msp_workloadlkid_value,msp_commitmentrecommendation,
             msp_milestonecategory,msp_monthlyuse,msp_milestonedate,
             msp_milestonestatus,_ownerid_value,_msp_opportunityid_value,
             msp_forecastcommentsjsonfield,msp_forecastcomments
```

### Update a Milestone

```
PATCH /api/data/v9.2/msp_engagementmilestones({milestone_id})
Content-Type: application/json

{
    "msp_milestonedate": "2026-03-15",
    "msp_monthlyuse": 1500.00,
    "msp_forecastcommentsjsonfield": "[{\"text\":\"...\",\"author\":\"...\",\"date\":\"...\"}]",
    "msp_forecastcomments": "Plain text summary of comments"
}
```

### Load Milestones Where User is on Access Team (FetchXML)

```
GET /api/data/v9.2/msp_engagementmilestones?fetchXml={urlEncoded}
```

```xml
<fetch version="1.0" output-format="xml-platform" mapping="logical"
       distinct="true" no-lock="true">
  <entity name="msp_engagementmilestone">
    <attribute name="msp_engagementmilestoneid"/>
    <attribute name="msp_milestonenumber"/>
    <attribute name="msp_name"/>
    <attribute name="msp_milestonedate"/>
    <attribute name="msp_milestonestatus"/>
    <attribute name="msp_monthlyuse"/>
    <attribute name="msp_commitmentrecommendation"/>
    <attribute name="msp_milestonecategory"/>
    <attribute name="ownerid"/>
    <attribute name="msp_workloadlkid"/>
    <attribute name="msp_opportunityid"/>
    <link-entity name="team" from="regardingobjectid"
                 to="msp_engagementmilestoneid" link-type="inner" alias="t">
      <filter type="and">
        <condition attribute="teamtype" operator="eq" value="1"/>
        <condition attribute="teamtemplateid" operator="eq"
                   value="{MILESTONE_TEAM_TEMPLATE_ID}"/>
      </filter>
      <link-entity name="teammembership" from="teamid" to="teamid"
                   link-type="inner" alias="tm">
        <filter type="and">
          <condition attribute="systemuserid" operator="eq"
                     value="{currentUserId}"/>
        </filter>
      </link-entity>
    </link-entity>
  </entity>
</fetch>
```

### Key Fields

| Field | Description |
|-------|-------------|
| `msp_engagementmilestoneid` | Milestone GUID |
| `msp_name` | Milestone title/description |
| `msp_milestonenumber` | Internal milestone number |
| `msp_milestonestatus` | Status code (numeric) |
| `msp_milestonestatus@OData.Community.Display.V1.FormattedValue` | Status label |
| `_msp_opportunityid_value` | Linked opportunity GUID |
| `_msp_opportunityid_value@OData.Community.Display.V1.FormattedValue` | Opportunity name |
| `_msp_workloadlkid_value@OData.Community.Display.V1.FormattedValue` | Workload name |
| `msp_monthlyuse` | Monthly usage amount |

**Milestone Status Values:** On Track, At Risk, Blocked, Completed, Cancelled, Lost to Competitor, Hygiene/Duplicate

### Building Milestone URLs

```python
MSX_APP_ID = "fe0c3504-3700-e911-a849-000d3a10b7cc"

def build_milestone_url(milestone_id):
    return (
        f"https://microsoftsales.crm.dynamics.com/main.aspx"
        f"?appid={MSX_APP_ID}"
        f"&pagetype=entityrecord"
        f"&etn=msp_engagementmilestone"
        f"&id={milestone_id}"
    )
```

---

## Opportunities

### List Open Opportunities for Accounts

```
GET /api/data/v9.2/opportunities
    ?$filter=(_parentaccountid_value eq '{account_id1}' or ...) and statecode eq 0
    &$select=opportunityid,name,estimatedclosedate,msp_estcompletiondate,
             msp_consumptionconsumedrecurring,_ownerid_value,_parentaccountid_value
    &$orderby=name
    &$count=true
```

Account IDs are chunked (max 25 per request) to stay under OData URL length limits. Uses pagination — follows `@odata.nextLink` automatically.

### Search by Opportunity Number

```
GET /api/data/v9.2/opportunities
    ?$filter=msp_opportunitynumber eq '{number}'
    &$select=opportunityid,name,estimatedclosedate,msp_estcompletiondate,
             msp_consumptionconsumedrecurring,_ownerid_value,_parentaccountid_value
```

### Load Single Opportunity

```
GET /api/data/v9.2/opportunities({guid})
    ?$select=opportunityid,name,estimatedclosedate,msp_estcompletiondate,
             msp_consumptionconsumedrecurring,_ownerid_value,_parentaccountid_value,
             msp_opportunitynumber
```

### Update an Opportunity

```
PATCH /api/data/v9.2/opportunities({opportunity_id})
Content-Type: application/json

{ ...field updates... }
```

### Auto-Resolve TPID → Account URL

Given a TPID, look up the account and build a direct MSX link:

```python
def get_account_by_tpid(tpid, token):
    url = f"https://microsoftsales.crm.dynamics.com/api/data/v9.2/accounts"
    params = {
        "$filter": f"msp_mstopparentid eq '{tpid}'",
        "$select": "accountid,name"
    }
    resp = requests.get(url, params=params, headers=get_crm_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("value"):
        account_id = data["value"][0]["accountid"]
        return f"https://microsoftsales.crm.dynamics.com/main.aspx?etn=account&id={account_id}&pagetype=entityrecord"
    return None
```

---

## Creating Tasks

### Overview

Tasks are linked to milestones and credit the user for customer engagement. The task category determines whether it counts for HoK credit.

### Create a Task

```
POST /api/data/v9.2/tasks
Content-Type: application/json

{
    "subject": "Technical architecture review call",
    "msp_taskcategory": 861980004,
    "scheduleddurationminutes": 60,
    "prioritycode": 1,
    "regardingobjectid_msp_engagementmilestone@odata.bind": "/msp_engagementmilestones({milestone_id})",
    "ownerid@odata.bind": "/systemusers({user_id})"
}
```

| Field | Description |
|-------|-------------|
| `subject` | Task title |
| `msp_taskcategory` | Category code (see HoK section) |
| `scheduleddurationminutes` | Duration in minutes |
| `prioritycode` | 0=Low, 1=Normal, 2=High |
| `regardingobjectid_msp_engagementmilestone@odata.bind` | Links to milestone |
| `ownerid@odata.bind` | Task owner |
| `description` | Optional description text |

### Getting the Task ID

The created task ID is returned in the `OData-EntityId` response header:

```
OData-EntityId: https://microsoftsales.crm.dynamics.com/api/data/v9.2/tasks(12345678-...)
```

Parse with regex:
```python
match = re.search(r'tasks\(([a-f0-9-]{36})\)', entity_id_header, re.IGNORECASE)
task_id = match.group(1) if match else None
```

### List Tasks for a Milestone

```
GET /api/data/v9.2/tasks
    ?$filter=_regardingobjectid_value eq '{milestone_id}'
    &$select=subject,scheduledend,createdon,activityid,msp_taskcategory,
             scheduleddurationminutes,statecode,statuscode,_ownerid_value
    &$orderby=createdon desc
```

---

## HoK (Hands-on-Keyboard) Task Categories

These are the **only categories that count for HoK** credit:

| Category | Code | Description |
|----------|------|-------------|
| Architecture Design Session | 861980004 | Deep technical design sessions |
| Blocker Escalation | 861980006 | Escalating technical blockers |
| Briefing | 861980008 | Executive/technical briefings |
| Consumption Plan | 861980007 | Planning Azure consumption |
| Demo | 861980002 | Product/solution demos |
| PoC/Pilot | 861980005 | Proof of concept work |
| Technical Close/Win Plan | 606820005 | Technical win planning |
| Workshop | 861980001 | Hands-on workshops |

### Non-HoK Categories (for reference)

| Category | Code |
|----------|------|
| ACE | 606820000 |
| Call Back Requested | 861980010 |
| Cross Segment | 606820001 |
| Cross Workload | 606820002 |
| Customer Engagement | 861980000 |
| External (Co-creation of Value) | 861980013 |
| Internal | 861980012 |
| Negotiate Pricing | 861980003 |
| New Partner Request | 861980011 |
| Post Sales | 606820003 |
| RFP/RFI | 861980009 |
| Tech Support | 606820004 |

---

## Deal Team / Access Team Membership

### Check Membership (Primary — via team association)

```
GET /api/data/v9.2/systemusers({user_id})/teammembership_association
    ?$select=_regardingobjectid_value,teamid
    &$filter=teamtemplateid eq guid'{TEAM_TEMPLATE_ID}'
             and teamtype eq 1
             and (_regardingobjectid_value eq guid'{record_id1}' or ...)
```

Works for both opportunity and milestone teams — use the appropriate `TEAM_TEMPLATE_ID` (see [Key Constants](#key-constants)).

### Check Membership (Fallback — msp_dealteams entity)

```
GET /api/data/v9.2/msp_dealteams
    ?$filter=_msp_dealteamuserid_value eq '{user_id}'
             and (_msp_parentopportunityid_value eq '{opp_id1}' or ...)
             and statecode eq 0
    &$select=_msp_parentopportunityid_value
```

### Join a Team

```
POST /api/data/v9.2/systemusers({user_id})/Microsoft.Dynamics.CRM.AddUserToRecordTeam
Content-Type: application/json

{
    "Record": {
        "@odata.type": "Microsoft.Dynamics.CRM.opportunity",
        "opportunityid": "{record_id}"
    },
    "TeamTemplate": {
        "@odata.type": "Microsoft.Dynamics.CRM.teamtemplate",
        "teamtemplateid": "{TEAM_TEMPLATE_ID}"
    }
}
```

For milestones, replace `opportunity` → `msp_engagementmilestone` and `opportunityid` → `msp_engagementmilestoneid`.

### Leave a Team

```
POST /api/data/v9.2/systemusers({user_id})/Microsoft.Dynamics.CRM.RemoveUserFromRecordTeam
```

Same body structure as join.

---

## Batch Requests

```
POST /api/data/v9.2/$batch
Content-Type: multipart/mixed; boundary=batch_{batch_id}

--batch_{batch_id}
Content-Type: multipart/mixed; boundary=changeset_{changeset_id}

--changeset_{changeset_id}
Content-Type: application/http
Content-Transfer-Encoding: binary

POST /api/data/v9.2/systemusers({user_id})/Microsoft.Dynamics.CRM.AddUserToRecordTeam HTTP/1.1
Content-Type: application/json

{...body...}

--changeset_{changeset_id}--
--batch_{batch_id}--
```

Used for bulk join/leave team operations. Adaptive chunk sizing: starts at 15 operations per batch, adjusts between 5–25 based on measured latency.

---

## Key Constants

| Constant | Value | Notes |
|----------|-------|-------|
| CRM Base URL | `https://microsoftsales.crm.dynamics.com` | |
| API Version | `v9.2` | OData v4.0 compatible |
| Tenant ID | `72f988bf-86f1-41af-91ab-2d7cd011db47` | Microsoft corporate |
| MSX App ID | `fe0c3504-3700-e911-a849-000d3a10b7cc` | For building record URLs |
| Opportunity Team Template ID | `cc923a9d-7651-e311-9405-00155db3ba1e` | For deal team operations |
| Milestone Team Template ID | `316e4735-9e83-eb11-a812-0022481e1be0` | For milestone access team operations |

---

## Important Gotchas

### 1. No `$skip` Pagination

MSX does not support standard OData `$skip`. To get more than 100 records, either:
- Use server-side filtering to reduce the result set
- Follow `@odata.nextLink` (but it's inconsistent across entities)

For entities that do support it, Dynamics 365 returns max 5,000 records per response with `@odata.nextLink` for the next page:

```python
def get_all_pages(url, headers):
    all_records = []
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        all_records.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return all_records
```

### 2. VPN Required

All MSX API calls require VPN connection to Microsoft corpnet. Token acquisition also requires VPN.

### 3. Token Expiry

Azure CLI tokens expire after ~60–75 minutes. Cache the token and refresh when needed:
```python
if token_expiry < utc_now() + timedelta(minutes=5):
    refresh_token()
```

### 4. Lookup Field Formatting

Lookup fields (foreign keys) return two values:
- `_fieldname_value` — The raw GUID
- `_fieldname_value@OData.Community.Display.V1.FormattedValue` — Display name

You need the `Prefer: odata.include-annotations="*"` header to get the formatted values.

### 5. Filter Syntax

- GUIDs don't need quotes: `_msp_accountid_value eq abc123-def456-...`
- Strings need single quotes: `msp_qualifier1 eq 'Corporate'`
- Prefix matching: `startswith(msp_qualifier2,'Cloud ')`
- Multiple OR needs parentheses: `(a eq 1 or a eq 2) and b eq 3`
- Escape single quotes in values: `value.replace("'", "''")`

### 6. 401/403 on First Request After Token Refresh

Sometimes the first request after a token refresh fails even though the token is valid. Implement automatic retry:

```python
response = make_request(url, headers)
if response.status_code in (401, 403):
    refresh_token()
    new_headers = build_headers(get_fresh_token())
    response = make_request(url, new_headers)
```

### 7. Entity Names Are Plural

- `accounts` (not account)
- `territories` (not territory)
- `msp_engagementmilestones` (not msp_engagementmilestone)
- `tasks` (not task)
- `systemusers` (not systemuser)

### 8. App ID for MSX URLs

When building URLs to open records in MSX, include the app ID:
```
?appid=fe0c3504-3700-e911-a849-000d3a10b7cc
```
Without it, MSX might not load the correct app context.

---

## Appendix: Device Code Flow with MSAL

> **Not currently implemented** — NoteHelper uses `az login` for MSX auth. This section documents MSAL device code flow as a future alternative if we need long-lived refresh tokens or want to remove the `az` CLI dependency.

### Overview

The OAuth 2.0 Device Code Flow via MSAL for Python gives you:
- A one-time browser login triggered from the admin panel
- A refresh token cached on disk that silently renews access tokens for months
- No redirect URI needed

### Prerequisites — Azure AD App Registration

1. Azure Portal > App Registrations > New registration
2. Name: `NoteHelper CRM`, Single tenant (Microsoft corporate)
3. Redirect URI: leave blank
4. Authentication > Enable "Allow public client flows" → Yes
5. API Permissions > Dynamics CRM > Delegated > `user_impersonation`

### Implementation

```python
import msal
import os

CLIENT_ID = "your-app-client-id"
TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://microsoftsales.crm.dynamics.com/.default"]
TOKEN_CACHE_FILE = "data/msx_token_cache.json"


def _load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE, "r") as f:
            cache.deserialize(f.read())
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def _get_msal_app(cache=None):
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)


def start_device_code_flow():
    """Initiate device code flow. Returns flow object with user_code and verification_uri."""
    cache = _load_cache()
    app = _get_msal_app(cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'Unknown error')}")
    return flow


def complete_device_code_flow(flow):
    """Poll Azure AD until user completes login. Blocks up to ~15 minutes."""
    cache = _load_cache()
    app = _get_msal_app(cache)
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" in result:
        return result
    raise RuntimeError(result.get("error_description", "Authentication failed"))


def get_crm_token():
    """Get a valid CRM token using cached/refresh tokens silently."""
    cache = _load_cache()
    app = _get_msal_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("No cached MSX credentials. Admin must authenticate via Settings.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    raise RuntimeError("Token refresh failed. Admin must re-authenticate.")


def clear_crm_auth():
    """Clear all cached tokens."""
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)
```

### Token Lifecycle

| Event | What happens |
|-------|-------------|
| First auth | Device code flow → access token (60–75 min) + refresh token (months) |
| Normal API call | `acquire_token_silent()` → auto-refreshes if expired |
| Refresh token expires | `acquire_token_silent()` fails → admin re-authenticates |
| Redeploy/restart | Cache loaded from disk → silent auth picks up |
| Cache lost | Admin re-authenticates via device code flow |

---

*This doc was built through trial and error integrating with MSX. If something doesn't work or you find better approaches, update it!*
