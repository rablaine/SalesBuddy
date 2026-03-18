# MSX Account Import via Access Team Query

## Status: Backlog (Fallback - pending MSX fix for msp_accountteam)

**Date:** March 18, 2026
**Context:** MSX broke the `msp_accountteam` endpoint (support ticket open). This fallback uses the Account Access Team membership query discovered in Fiddler trace (frame 160 of accountsview.saz). It returns all accounts where a given user is on the Account Access Team.

---

## Feature Overview

Two user flows depending on role:

### Flow 1: Seller - "Import My Accounts"
- Seller clicks "Import My Accounts"
- App uses seller's own auth token (or their systemuserid) to query MSX
- Returns all accounts where they're on the Access Team
- Creates/updates Customer records with TPID, name, city, segment, vertical, ATU
- Assigns the seller to each imported customer

### Flow 2: SE - "Import My Sellers' Accounts"
- SE provides a CSV list of seller aliases (e.g. `remar, jsmith, akingzett`)
- App resolves each alias to a systemuserid via `domainname` lookup
- Queries accounts for each seller
- Creates customers with seller and territory alignments
- If SE doesn't have the seller list, they can fall back to "Import My Accounts" and assign sellers later

---

## Technical Implementation

### Step 1: Resolve User Alias to systemuserid

**Endpoint:** `GET /api/data/v9.0/systemusers`

Search strategy (try in order until match):
1. `$filter=internalemailaddress eq '{email}'` (full email like Reynaldo.Martinez@microsoft.com)
2. `$filter=domainname eq '{alias}@microsoft.com'` (alias form like remar@microsoft.com)
3. `$filter=startswith(domainname,'{alias}')` (prefix match, check for ambiguity)

**Select fields:** `systemuserid,fullname,domainname,internalemailaddress`

**Response example:**
```json
{
  "systemuserid": "4bfac108-98a9-e911-a85c-000d3a1bb0da",
  "fullname": "Reynaldo Martinez",
  "domainname": "remar@microsoft.com",
  "internalemailaddress": "Reynaldo.Martinez@microsoft.com"
}
```

### Step 2: Query Accounts by Access Team Membership

**Endpoint:** `GET /api/data/v9.0/accounts?fetchXml=...`

**FetchXML template:**
```xml
<fetch version="1.0" mapping="logical" distinct="true"
    returntotalrecordcount="true" page="1" count="5000" no-lock="true">
  <entity name="account">
    <attribute name="name"/>
    <attribute name="msp_accountnumber"/>
    <attribute name="accountid"/>
    <attribute name="address1_city"/>
    <attribute name="address1_composite"/>
    <attribute name="ownerid"/>
    <attribute name="msp_endcustomersegmentcode"/>
    <attribute name="msp_endcustomersubsegmentcode"/>
    <attribute name="msp_verticalcode"/>
    <attribute name="msp_subverticalcode"/>
    <attribute name="msp_verticalcategorycode"/>
    <attribute name="msp_parentinglevelcode"/>
    <attribute name="msp_mstopparentid"/>
    <attribute name="msp_hq"/>
    <attribute name="msp_gpid"/>
    <attribute name="msp_gpname"/>
    <attribute name="statecode"/>
    <order attribute="name" descending="false"/>
    <filter type="and">
      <condition attribute="statecode" operator="eq" value="0"/>
    </filter>
    <!-- Territory join for ATU name -->
    <link-entity name="territory" from="territoryid" to="territoryid"
        link-type="outer" alias="terr">
      <attribute name="msp_accountteamunitname"/>
    </link-entity>
    <!-- Access Team membership chain -->
    <link-entity name="team" from="regardingobjectid" to="accountid"
        link-type="inner" alias="ac">
      <filter type="and">
        <condition attribute="teamtype" operator="eq" value="1"/>
        <condition attribute="teamtemplateid" operator="eq"
            value="{3FCC1CFC-3E43-E311-9405-00155DB3BA1E}"/>
      </filter>
      <link-entity name="teammembership" from="teamid" to="teamid"
          intersect="true">
        <link-entity name="systemuser" from="systemuserid" to="systemuserid"
            alias="aa">
          <filter type="and">
            <!-- For caller's own accounts, use operator="eq-userid" with no value -->
            <!-- For a specific seller, use operator="eq" with their GUID -->
            <condition attribute="systemuserid" operator="eq"
                value="{SYSTEMUSERID_GUID}"/>
          </filter>
        </link-entity>
      </link-entity>
    </link-entity>
  </entity>
</fetch>
```

**Key constants:**
- Team template GUID: `{3FCC1CFC-3E43-E311-9405-00155DB3BA1E}` (Account Access Team Template)
- teamtype: `1` (Access team)
- statecode: `0` (Active accounts only)

### Step 3: Parse Response into Customer Records

**Field mapping (MSX -> SalesBuddy):**

| MSX Field | SalesBuddy Customer Field | Notes |
|-----------|--------------------------|-------|
| `name` | `name` | Account name |
| `msp_accountnumber` | `tpid` | TPID identifier |
| `accountid` | `msx_account_id` | Dynamics GUID (for linking) |
| `msp_mstopparentid` | `top_parent_tpid` | Numeric top parent |
| `address1_city` | `city` | |
| `msp_endcustomersegmentcode` (formatted) | `segment` | e.g. "SME&C - Corporate Commercial" |
| `msp_endcustomersubsegmentcode` (formatted) | `subsegment` | |
| `msp_verticalcode` (formatted) | `vertical` | e.g. "IT Services & Business Advisory" |
| `msp_subverticalcode` (formatted) | `subvertical` | |
| `msp_verticalcategorycode` (formatted) | `vertical_category` | |
| `terr.msp_accountteamunitname` | Territory name | ATU like "East.SMECC.HLA" |
| `_ownerid_value` (formatted) | - | Account owner name (not seller assignment) |

**Formatted values:** Dynamics returns both raw codes and display strings. Use the `@OData.Community.Display.V1.FormattedValue` suffixed keys for human-readable segment/vertical names.

---

## What This Query Does NOT Give Us

- **Seller role on the account** (ATS, ATM, CSA, etc.) - we know they're on the Access Team but not what role
- **Acquisition vs. managed flag** - not present in this query
- **Other team members** - query is scoped to one user at a time

If `msp_accountteam` gets fixed, it would provide role assignments. Until then, the SE flow handles this by letting the SE specify which aliases are their sellers.

---

## Proof of Concept

Working script: `scripts/msx_account_lookup.py`

```
python scripts/msx_account_lookup.py remar@microsoft.com   # 34 accounts
python scripts/msx_account_lookup.py alexbla@microsoft.com  # 299 accounts
```

Tested and confirmed working as of March 18, 2026.

---

## UI Sketch

### Settings or Admin page:
```
[Import My Accounts]  <- Uses eq-userid (caller's own token)

-- OR --

Import Sellers' Accounts:
[ remar, akingzett, jsmith ]  <- CSV of aliases
[Import]
```

### Import results:
- Show count of new vs. already-existing accounts
- Auto-create Territory records from ATU names if they don't exist
- Auto-assign sellers to their accounts
- Flag any accounts that appear for multiple sellers (shared accounts)
