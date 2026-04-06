# Marketing Insights from MSX

## Goal

Surface Microsoft Marketing Insights data on customer views so sellers can see which accounts have active marketing engagement (Contact Me requests, trial signups, content downloads, event attendance) and which contacts at those accounts are most engaged - without leaving Sales Buddy.

## What the MSX UI Shows

The Marketing Insights panel in MSX Dynamics 365 displays:

1. **Summary widget** (floating panel) - aggregate counts for the account:
   - Total Accounts, Total Contacts, Contact Me requests
   - Trial Signups, Content Downloads, Events attended

2. **Breakdown by Solution Area / Sales Play** - one row per combination showing individual counts and last interaction date

3. **Per-interaction detail grid** - individual interaction events per contact:
   - Contact name, Job Role, Marketing Audience, MSX Contact (Yes/No)
   - Specific interaction description (e.g. "Digital Simulive Event - Watched On Demand: Microsoft365CopilotTrainin...")
   - Solution Play, Marketing Play, Date, URL
   - Same contact can appear multiple times (one row per interaction event)

4. **Contact-level aggregates** (on the contact entity) - per-contact totals:
   - Email interaction count, Meeting interaction count
   - Marketing audience type, Engagement level
   - Last marketing interaction date, Last solution area engaged

## MSX API Data Sources

### Summary: Three OData Layers + One Inaccessible Layer

Validated April 2026 by querying MSX EntityDefinitions (2,397 total entities, 226 `msp_*` entities) and fetching live data for Acadia Healthcare (TPID 35098087, account GUID ab42a78e-842d-4427-b594-97e508e33735).

| Layer | Entity | Granularity | Accessible via OData? |
|-------|--------|-------------|----------------------|
| Account summary | `msp_marketingengagements` | One row per TPID | Yes |
| Sales play breakdown | `msp_marketinginteractions` | One row per TPID + solution area + sales play | Yes |
| Contact aggregates | `contacts` (msp_ fields) | One row per CRM-linked contact | Yes |
| Per-interaction detail | Unknown (not a standard entity) | One row per contact per interaction event | **No** |

### 1. Account-Level Summary: `msp_marketingengagements`

**Entity set:** `msp_marketingengagements`

One record per TPID with aggregate string counts. Maps to the floating summary widget in the MSX dashboard.

**Query pattern:**
```
GET /api/data/v9.2/msp_marketingengagements
  ?$filter=msp_mstopparentid eq '{tpid}'
```

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `msp_mstopparentid` | String | Top Parent ID (TPID) |
| `msp_marketinginteractions` | String | Total interaction count (as string) |
| `msp_contentdownloads` | String | Content download count (as string) |
| `msp_trials` | String | Trial signup count (as string) |
| `msp_engagedcontacts` | String | Engaged contacts count (as string) |
| `msp_uniquedecisionmakers` | String | Unique decision makers (as string) |
| `msp_lastinteractiondate` | String | Last interaction timestamp (as string) |

**Note:** All count fields are String type, not Integer. Parse to int for display.

**Sample response (Acadia Healthcare, TPID 35098087):**
```json
{
  "msp_mstopparentid": "35098087",
  "msp_marketinginteractions": "221",
  "msp_contentdownloads": "148",
  "msp_trials": "1",
  "msp_engagedcontacts": "150",
  "msp_uniquedecisionmakers": "137",
  "msp_lastinteractiondate": "2026-04-03T14:12:42.000Z"
}
```

### 2. Per-Sales-Play Breakdown: `msp_marketinginteractions`

**Entity set:** `msp_marketinginteractions`

**Composite key:** `msp_tpidsolutionareasalesplay` (format: `{tpid}_{solutionareacode}_{salesplaycode}`)

One row per TPID + Solution Area + Sales Play combination. Sum `msp_allinteractions` across rows to get account totals for Contact Me, Trials, Content, Events.

**Query pattern:**
```
GET /api/data/v9.2/msp_marketinginteractions
  ?$select=msp_tpid,msp_salesplaycode,msp_solutionareacode,
           msp_allinteractions,msp_contacts,msp_trialsignups,
           msp_contentdownloads,msp_events,msp_uniquedecisionmakers,
           msp_highinteractioncontacts,msp_highinteractioncount,
           msp_highinteractionuniquedecisionmakers,
           msp_lastinteractiondate,msp_lasthighinteractiondate
  &$filter=msp_tpid eq '{tpid}'
  &$orderby=msp_allinteractions desc
```

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `msp_tpid` | String | Top Parent ID (links to account) |
| `msp_salesplaycode` | Picklist | Sales Play (use FormattedValue for label) |
| `msp_solutionareacode` | Picklist | Solution Area (use FormattedValue for label) |
| `msp_allinteractions` | Integer | Total interaction count |
| `msp_contacts` | Integer | "Contact Me" request count |
| `msp_trialsignups` | Integer | Trial signup count |
| `msp_contentdownloads` | Integer | Content download count |
| `msp_events` | Integer | Event attendance count |
| `msp_uniquedecisionmakers` | Integer | Unique decision makers who interacted |
| `msp_highinteractioncontacts` | Integer | Contacts with high interaction level |
| `msp_highinteractioncount` | Integer | Total high interactions |
| `msp_highinteractionuniquedecisionmakers` | Integer | Unique DMs with high interaction |
| `msp_lastinteractiondate` | DateTime | Last interaction timestamp |
| `msp_lasthighinteractiondate` | DateTime | Last high-interaction timestamp |

**Sample response (Acadia Healthcare, one row of several):**
```json
{
  "msp_tpid": "35098087",
  "msp_tpidsolutionareasalesplay": "35098087_861980004_861980037",
  "msp_solutionareacode": 861980004,
  "msp_salesplaycode@OData.Community.Display.V1.FormattedValue": "Build and Modernize AI Apps",
  "msp_salesplaycode": 861980037,
  "msp_allinteractions": 1,
  "msp_contacts": 1,
  "msp_trialsignups": 0,
  "msp_contentdownloads": 1,
  "msp_events": 0,
  "msp_uniquedecisionmakers": 1,
  "msp_highinteractioncount": 0,
  "msp_lastinteractiondate": "2024-06-03T20:43:56Z"
}
```

### 3. Contact-Level Aggregates: `contacts` entity (msp_ fields)

Per-contact aggregate engagement metrics live directly on the standard `contact` entity as `msp_*` fields. These are CRM-linked contacts only - contacts that exist in the marketing platform but aren't linked to CRM won't appear here.

**Query requires two steps:**
1. Look up account GUID from TPID: `GET /accounts?$filter=msp_mstopparentid eq '{tpid}'`
2. Query contacts: `GET /contacts?$filter=_parentcustomerid_value eq {account_guid}`

**Query pattern (step 2):**
```
GET /api/data/v9.2/contacts
  ?$select=fullname,emailaddress1,jobtitle,
           msp_lastmarketinginteractiondate,msp_noofmailinteractions,
           msp_noofmeetinginteractions,msp_marketingaudiencecode,
           msp_salesengagementlevel,msp_lastsolutionareaengaged
  &$filter=_parentcustomerid_value eq {account_guid}
  &$orderby=msp_noofmailinteractions desc
  &$top=20
```

**Note:** Filter on `_parentcustomerid_value` (the lookup field's raw GUID), NOT `parentcustomerid` (which errors).

**Key fields on contact:**

| Field | Type | Description |
|---|---|---|
| `msp_noofmailinteractions` | Integer | Email interaction count |
| `msp_noofmeetinginteractions` | Integer | Meeting interaction count |
| `msp_marketingaudiencecode` | Picklist | Audience type (e.g. "Information Technology DM") |
| `msp_salesengagementlevel` | Picklist | Engagement level (e.g. "Highly Engaged") |
| `msp_lastmarketinginteractiondate` | DateTime | Last marketing interaction date |
| `msp_lastsolutionareaengaged` | Picklist | Last solution area engaged (e.g. "AI Business Solutions") |

**Sample response (Acadia Healthcare, top contact by email interactions):**
```json
{
  "fullname": "Andrew Mizukami",
  "jobtitle": "Senior Director of IT Operations",
  "msp_noofmailinteractions": 93,
  "msp_noofmeetinginteractions": 77,
  "msp_marketingaudiencecode@OData.Community.Display.V1.FormattedValue": "Information Technology DM",
  "msp_salesengagementlevel@OData.Community.Display.V1.FormattedValue": "Highly Engaged"
}
```

### 4. Per-Interaction Detail: NOT ACCESSIBLE via OData

The MSX Marketing Insights detail grid (the table showing individual interaction events per contact) is **not available via the standard Dynamics OData API**. This was confirmed by:

1. Searching all 2,397 Dynamics entity definitions - no entity matches the per-interaction pattern
2. Checking all 226 `msp_*` entities, all `msdynmkt_*` entities, all `msdyncrm_*` entities
3. Examining `leads` (which contain some marketing data but different contacts/dates)
4. Checking `campaignresponses`, `interactionforemail`, `msp_mcem`, `msp_customer360`

The detail grid is powered by a custom PCF control or web resource in the MSX dashboard that calls a backend Marketing Intelligence API (not the Dynamics OData API). The individual interaction events (which person attended which event, downloaded which ebook, registered for which trial) live in a separate data platform.

**What this means for SalesBuddy:** We can build a useful Marketing Insights feature using the three OData-accessible layers (account summary, sales play breakdown, contact aggregates). The per-interaction detail would require either:
- Discovering the backend Marketing Intelligence API endpoint (not documented publicly)
- Building a workaround using the lead entity (which has some marketing campaign data but different granularity)
- Accepting the limitation and showing aggregate data only

## Implementation Plan

### Phase 1: API Layer

Add to `app/services/msx_api.py`:

- `get_marketing_summary(tpid: str) -> dict` - Fetches `msp_marketingengagements` for the TPID, returns account-level summary counts
- `get_marketing_breakdown(tpid: str) -> list[dict]` - Fetches `msp_marketinginteractions` rows, returns per-sales-play breakdown sorted by total interactions
- `get_marketing_contacts(tpid: str) -> list[dict]` - Two-step query (account lookup then contacts with marketing fields), returns list of CRM-linked contacts with engagement data

All functions reuse the existing `_msx_request()` helper and auth pattern.

### Phase 2: Route + API Endpoint

Add to `app/routes/customers.py` (or `msx.py`):

- `GET /api/customer/<id>/marketing-insights` - Returns JSON with all three layers. Looks up TPID from the customer record, calls all three API functions.

### Phase 3: Customer View UI

Add a "Marketing Insights" card/section to `templates/customer_view.html`:

**Account summary panel:**
- Stat boxes: Total Interactions | Trials | Content | Events (from `msp_marketingengagements`)
- Engaged contacts and unique decision makers counts
- Last interaction date

**Sales Play breakdown table:**
- Columns: Solution Area, Sales Play, Interactions, Contact Me, Trials, Content, Events, Last Date
- From `msp_marketinginteractions` rows
- Sorted by total interactions descending

**Engaged contacts list:**
- From `contact` entity marketing fields
- Each contact shows: Name, Title, Email/Meeting interaction counts, Engagement level badge, Audience type, Last interaction date
- Sorted by total interactions (mail + meeting) descending
- Only show contacts with at least 1 interaction

### Phase 4: Note Form Flyout

Add marketing insights to the customer flyout in `templates/note_form.html`:

- Compact version of the summary (just the four stat boxes)
- Top 3-5 engaged contacts
- Link to full customer view for details

## Design Notes

- **No local caching** - Marketing data changes frequently and is read-only. Fetch live from MSX on each view.
- **Loading pattern** - Fetch async after page load (same pattern as existing MSX detail fetches). Show skeleton/spinner while loading.
- **Empty state** - Many accounts will have zero marketing interactions. Show a friendly "No marketing insights available" message, not an error.
- **API cost** - Summary is 1 call, breakdown is 1 call (<20 rows typical), contacts requires 2 calls (account lookup + contacts, bounded by $top=20). Total: 3-4 API calls per view.
- **TPID required** - Marketing insights are keyed by TPID. Customers without a TPID linked can't show this data. Hide the section or show "Link a TPID to see marketing insights."
- **FormattedValue annotations** - Always use the OData `FormattedValue` annotation for picklist fields (solution area, sales play, audience, engagement level). The raw values are opaque integer codes.
- **String count fields** - The `msp_marketingengagements` entity stores counts as String type. Parse to int for display and sorting.
- **Contact scope** - The contact marketing fields only show CRM-linked contacts. The marketing platform tracks many more people (visible in the MSX dashboard detail grid) that don't have CRM contact records. The "MSX Contact: Yes/No" column in the MSX UI reflects this distinction.

## Discovery Notes (April 2026)

API patterns validated by querying live MSX for Acadia Healthcare (TPID 35098087).

**Entities searched:** All 2,397 Dynamics entity definitions enumerated via `EntityDefinitions?$select=LogicalName,EntitySetName`. Key entity groups examined:
- 226 `msp_*` entities (MSX custom)
- 148 `msdynmkt_*` entities (Dynamics Marketing, newer)
- 41 marketing-keyword entities
- 10 interaction-keyword entities
- `leads`, `campaignresponses`, `interactionforemail`
- `msp_mcem`, `msp_customer360`, `msp_msxinsights`, `msp_contactquality`

**Three working OData entities found:**
1. `msp_marketingengagements` - Account-level summary (keyed by `msp_mstopparentid`)
2. `msp_marketinginteractions` - Per-sales-play breakdown (keyed by `msp_tpid`, composite key `msp_tpidsolutionareasalesplay`)
3. `contacts` entity `msp_*` fields - Per-contact aggregates (filtered by `_parentcustomerid_value`)

**Per-interaction detail NOT found:**
- The MSX Marketing Insights detail grid (individual events per contact) is NOT backed by a standard Dynamics entity
- The `lead` entity contains some marketing campaign data (`subject` fields like "CO-HWBR-CNTNT-FY24-03Mar-01-Putting-AI-to-Work") but shows different contacts and dates than the Marketing Insights widget
- The detail grid is powered by a custom PCF control/web resource calling a separate Marketing Intelligence backend API

**Other findings:**
- `contains()` filter is not supported on Metadata Entities (HTTP 501)
- Contacts must be filtered via `_parentcustomerid_value` (raw GUID lookup), not `parentcustomerid` (errors with 400)
- `msp_tpid` does not exist on the `contact` entity - requires two-step query through `accounts` first
- `msp_marketingengagements` uses `msp_mstopparentid` as the TPID field name (not `msp_tpid`)
- `msp_marketingengagements` stores all counts as String type, not Integer
