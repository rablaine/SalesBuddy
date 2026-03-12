# MSX Account Team Role Identification

How to identify specific roles (DAE, DSS, CSAM) from the MSX Dynamics 365 CRM API.

**Last verified:** March 2026  
**API:** `https://microsoftsales.crm.dynamics.com/api/data/v9.2`  
**Entity:** `msp_accountteams` (logical name: `msp_accountteam`)

---

## Account Lookup

Accounts are looked up by TPID using the `msp_mstopparentid` field (not `msp_tpid`):

```
GET /accounts?$filter=msp_mstopparentid eq '{tpid}'&$select=accountid,name,_ownerid_value
```

## Team Member Query

All team members for an account:

```
GET /msp_accountteams?$filter=_msp_accountid_value eq {account_guid}&$top=500
```

> **Important:** Always set `$top=500`. CRM defaults to 5000 which exceeds the max page size.

---

## DAE (Digital Account Executive)

**Filter:**
```
msp_standardtitle eq 'Digital Account Management IC'
```

**Identifying fields:**
| Field | Value |
|---|---|
| `msp_standardtitle` | `Digital Account Management IC` |
| `msp_rolename` | `ATU Account Executive` |
| `msp_roletype` | `ATU` |
| `msp_qualifier1` | `Corporate` |
| `msp_qualifier2` | `N/A` |

**Shortcut:** The account record's `_ownerid_value` points directly to the DAE's `systemuserid`. The account owner IS the DAE.

**Alias extraction:** `msp_internalemailaddress.split("@")[0]`

**Example (TPID 1190336):** BREWERDEEDRA

---

## DSS (Digital Solution Specialists)

**Filter:**
```
msp_standardtitle eq 'Digital Solution Area Specialists IC'
```

**Identifying fields:**
| Field | Value |
|---|---|
| `msp_standardtitle` | `Digital Solution Area Specialists IC` |
| `msp_roletype` | `SMEC` |
| `msp_qualifier1` | `Corporate` |
| `msp_qualifier2` | *(varies — this is the specialty)* |

**Specialty mapping via `msp_qualifier2`:**

| `msp_qualifier2` value | Specialty |
|---|---|
| `AI Biz Sol-AI Biz Process` | AI Business Process |
| `AI Biz Sol-AI Workforce` | Workforce AI |
| `Security` | Security |
| `Modern Work` | Modern Work |
| `Azure Infrastructure` | Azure Infra |
| *(others)* | *(check value directly)* |

The `msp_qualifier2` field cleanly distinguishes which DSS covers which solution area. Each DSS on an account will have a unique `msp_qualifier2` value.

**Alias extraction:** `msp_internalemailaddress.split("@")[0]`

**Example (TPID 1190336):**
- ACARLISLE → `AI Biz Sol-AI Biz Process`
- SCYPRIEN → `AI Biz Sol-AI Workforce`
- WAREANDREW → `Security`

---

## CSAM (Customer Success Account Manager)

**Filter:**
```
msp_standardtitle eq 'Customer Success Account Mgmt IC'
```

**Identifying fields:**
| Field | Value |
|---|---|
| `msp_standardtitle` | `Customer Success Account Mgmt IC` |
| `msp_rolename` | `Customer Success Unit` |
| `msp_roletype` | `CSU` |
| `msp_qualifier1` | `SME&C` |
| `msp_qualifier2` | `N/A` |

### ⚠️ Cannot Identify Primary CSAM

The API returns **all** CSAMs assigned to an account, but there is **no field to distinguish the primary CSAM** from the rest. This was exhaustively investigated:

- All CSAMs on a given account have **identical** role field values (same standardtitle, rolename, roletype, qualifier1, qualifier2)
- `msp_accountteam` has exactly 17 attributes — all strings, lookups, and GUIDs. **Zero timestamps, zero booleans, zero integers** that could serve as a rank or priority
- No ordering guarantee in the API response
- The account record itself has **no CSAM-related fields**

**Dead ends investigated:**
| Approach | Result |
|---|---|
| `msp_salesterritoryvirtualteams` (has `msp_primaryspecialist` boolean) | Returns 0 records — empty or access-restricted |
| `msp_customersuccessplan` | HTTP 403 — missing `prvReadmsp_customersuccessplan` privilege |
| `msp_customer360` | Only 2 trivial attributes, 0 records |
| Territory matching on `systemuser` | All CSAMs have NULL territory |
| Timestamp ordering | No timestamp fields exist on `msp_accountteam` |
| Account record fields | No csam/success/delivery/primary/assigned fields |

**Practical impact:** An account can have 0 CSAMs (e.g., TPID 19574573 had none) or many (e.g., TPID 1190336 had 15). When multiple exist, the user must manually identify which one is their primary.

---

## Other Roles Observed

Not all team members are DAE/DSS/CSAM. Other `msp_roletype` values seen in the data:

| `msp_roletype` | Description |
|---|---|
| `ATU` | Account Team Unit (includes DAE) |
| `SMEC` | Specialist / SME&C (includes DSS) |
| `CSU` | Customer Success Unit (includes CSAM, CSA) |
| `ES` | Enterprise Services / Consulting |
| `OCP-SW` | Partner (One Commercial Partner) |
| `M&O` | Marketing & Operations |
| `Other` | Various other roles |

**Note:** CSU includes both CSAMs (`Customer Success Account Mgmt IC`) and CSAs (`Digital Cloud Solution Architecture IC/M`). These are different roles — filter on `msp_standardtitle`, not just `msp_roletype`.

---

## msp_accountteam Attribute Reference

The entity has exactly 17 custom attributes (all strings, lookups, or GUIDs):

| Attribute | Type | Purpose |
|---|---|---|
| `msp_accountteamid` | GUID | Primary key |
| `_msp_accountid_value` | Lookup | Account reference |
| `msp_fullname` | String | Team member's full name |
| `msp_internalemailaddress` | String | Email (alias extraction source) |
| `msp_rolename` | String | Role name |
| `msp_roletype` | String | Role type category |
| `msp_standardtitle` | String | Standard title (primary role identifier) |
| `msp_title` | String | Display title |
| `msp_qualifier1` | String | Qualifier 1 (e.g., Corporate, SME&C) |
| `msp_qualifier2` | String | Qualifier 2 (e.g., specialty for DSS) |
| `_msp_systemuserid_value` | Lookup | CRM systemuser reference |
| `msp_aadid` | String | Azure AD object ID |
| `msp_isprimary` | String | *(Always empty in observed data)* |
| `msp_accountname` | String | Account name (denormalized) |
| `msp_unit` | String | Business unit |
| `msp_segment` | String | Segment |
| `msp_subsegment` | String | Sub-segment |

---

## OData Query Tips

- **Always use `$top=500`** — CRM's default page size exceeds the 500-record max
- **Build raw URL strings** — do not use `urllib.parse.quote()` or `requests` params dict for OData filters
- **Authentication:** `az account get-access-token --resource https://microsoftsales.crm.dynamics.com --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47`
- **Include annotations** for formatted values: `Prefer: odata.include-annotations="*"`
- **Formatted value access:** `record["_field_value@OData.Community.Display.V1.FormattedValue"]`
- **Entity set names:** Use plural form in URLs (`msp_accountteams`, not `msp_accountteam`)
- **Metadata discovery:** `startswith` filter is NOT supported on `EntityDefinitions` — fetch all and filter client-side
