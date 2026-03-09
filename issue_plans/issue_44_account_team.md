# Issue #44 — Expand Account Team References (MSX Import)

**GitHub:** https://github.com/rablaine/NoteHelper/issues/44
**Branch:** `feature/account-team`
**Estimated effort:** Medium (2–3 sessions)

---

## Goal

Import the full account team from MSX (CSAM, DCSA, PSS, Security DSS/DSE, Biz Apps, ATU, etc.) alongside the Cloud sellers and SEs we already import. Display them on the customer view so sellers can quickly see who's aligned to each account.

## How MSX Account Teams Work

The `msp_accountteams` OData entity has one row per person per account. Key fields:

| Field | Purpose | Example values |
|-------|---------|----------------|
| `_msp_accountid_value` | Account GUID (FK) | |
| `msp_fullname` | Person's name | "Jane Doe" |
| `msp_qualifier1` | Org level | "Corporate", "Area", "Subsidiary" |
| `msp_qualifier2` | Role/org qualifier | "Cloud & AI", "Customer Success", "Security", etc. |
| `msp_standardtitle` | Job title | "Specialists IC", "Specialists Mgr", "CSU IC", etc. |
| `_msp_systemuserid_value` | User GUID (for alias lookup) | |

### What We Already Import

Current filter in `batch_query_account_teams()` ([msx_api.py:1679](app/services/msx_api.py#L1679)):
```
msp_qualifier1 eq 'Corporate' AND startswith(msp_qualifier2, 'Cloud ')
```

This gets ~20-30 records per account and we extract:
- **Sellers** — `qualifier2` in `{"Cloud & AI", "Cloud & AI-Acq"}` + `standardtitle` contains `"Specialists IC"`
- **SEs** — `qualifier2` in `{"Cloud & AI Data", "Cloud & AI Infrastructure", "Cloud & AI Apps"}`

### What We Need to Add

Other team members live under different `qualifier2` values. The full account team can have 250-300+ members, but we only need the key roles. Based on the issue request:

| Role | Expected `qualifier2` | Expected `standardtitle` | Notes |
|------|-----------------------|--------------------------|-------|
| CSAM | `"Customer Success"` | Contains `"CSU"` or `"CSAM"` | Customer Success Account Manager |
| DCSA | `"Customer Success"` or unknown | Contains `"DCSA"` | Digital Cloud Solution Advisor |
| PSS | `"Partner"` or similar | Contains `"PSS"` | Partner Solutions Specialist |
| Security DSS | `"Security"` | Contains `"Specialists IC"` | Security Domain Specialist |
| Security DSE | `"Security"` | Contains `"DSE"` or `"Engineer"` | Security Domain Solution Engineer |
| ATU Lead | Various | Contains `"ATU"` or `"Manager"` | Account Technology Unit lead |

**⚠️ We need to discover the exact qualifier values.** The plan includes a discovery step to dump raw team data and map the real values.

## Implementation Plan

### Step 1 — Discovery: dump raw account team data

Before we can map roles, we need to see what `qualifier1`, `qualifier2`, and `standardtitle` values actually exist for a real account.

**Add a temporary debug endpoint** or use the existing `query_entity` to pull the full team for one account without the Cloud filter:

```python
# One-shot query: all team members for a single account (no qualifier filter)
result = query_entity(
    "msp_accountteams",
    select=["msp_fullname", "msp_qualifier1", "msp_qualifier2",
            "msp_standardtitle", "_msp_systemuserid_value"],
    filter_query=f"_msp_accountid_value eq {account_id} and msp_qualifier1 eq 'Corporate'",
    top=500
)
```

This will give us the full Corporate-level team. We inspect the output and build the real role mapping. We can either:
- Add a `/api/msx/debug/account-team/<account_id>` admin-only endpoint (remove after mapping)
- Or run it as a one-off script via `flask shell`

### Step 2 — AccountTeamMember model

**File:** `app/models.py`

```python
class AccountTeamMember(db.Model):
    """A team member associated with a customer account, imported from MSX."""
    __tablename__ = 'account_team_members'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    alias = db.Column(db.String(100), nullable=True)      # Microsoft alias (from email)
    role = db.Column(db.String(100), nullable=False)       # "CSAM", "DCSA", "PSS", etc.
    msx_qualifier2 = db.Column(db.String(200), nullable=True)  # Raw MSX qualifier for debugging
    msx_title = db.Column(db.String(200), nullable=True)       # Raw MSX standardtitle
    msx_user_id = db.Column(db.String(100), nullable=True)     # MSX systemuser GUID
    source = db.Column(db.String(50), default='msx', nullable=False)  # 'msx' or 'manual'
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    customer = db.relationship('Customer', back_populates='team_members')

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'name', 'role', name='uq_team_member'),
    )
```

Add to Customer:
```python
team_members = db.relationship('AccountTeamMember', back_populates='customer',
                               cascade='all, delete-orphan', lazy='select')
```

**Why store `msx_qualifier2` and `msx_title`?** So we can refine the role mapping later without re-importing. If a CSAM gets misclassified, we can see why.

### Step 3 — Migration

**File:** `app/migrations.py`

Idempotent: check if `account_team_members` table exists, `db.create_all()` handles it. Add unique constraint migration if needed.

### Step 4 — Extend `batch_query_account_teams()` to fetch broader roles

**File:** `app/services/msx_api.py`

**Option A — Widen the existing query (preferred):**

Change the server-side filter from:
```python
startswith(msp_qualifier2,'Cloud ')
```
to:
```python
(startswith(msp_qualifier2,'Cloud ') or msp_qualifier2 eq 'Customer Success' or msp_qualifier2 eq 'Security' or msp_qualifier2 eq 'Partner')
```

This keeps the `msp_qualifier1 eq 'Corporate'` restriction (avoiding Area/Sub duplicates) but pulls in the additional roles. Expected increase: maybe 10-20 more records per account, still well under 100.

**If we hit the 100-record limit:** Reduce `batch_size` from 5 to 2-3 accounts per query, or split into two separate queries (one for Cloud, one for non-Cloud).

**Option B — Separate query for extended team:**

Run a second `batch_query_account_teams_extended()` that specifically targets the non-Cloud roles. Cleaner separation but adds API calls.

**Recommendation:** Start with Option A. The filter is a simple OR extension, and we can observe record counts to decide if splitting is needed.

### Step 5 — Add role extraction to `process_record()`

**File:** `app/services/msx_api.py` — in `batch_query_account_teams()`

Extend the existing `process_record()` function to capture additional roles:

```python
# After existing seller and SE extraction...

# Extended team roles
team_role_map = {
    # (qualifier2, title_contains) -> friendly role name
    # These will be refined after Step 1 discovery
    ("Customer Success", "CSAM"): "CSAM",
    ("Customer Success", "CSU IC"): "CSAM",
    ("Customer Success", "DCSA"): "DCSA",
    ("Security", "Specialists IC"): "Security DSS",
    ("Security", "Engineer"): "Security DSE",
    ("Partner", "PSS"): "PSS",
}

# Catch-all: any unmatched Corporate team member gets stored with raw values
for (q2_match, title_match), role_name in team_role_map.items():
    if qualifier2 == q2_match and title_match in standardtitle:
        if acct_id not in account_team:
            account_team[acct_id] = []
        account_team[acct_id].append({
            "name": name,
            "role": role_name,
            "qualifier2": qualifier2,
            "standardtitle": standardtitle,
            "user_id": user_id,
        })
        break
```

Add `account_team` to the return dict.

### Step 6 — Write team members during import

**File:** `app/routes/msx.py` — in the import stream, after sellers/SEs

```python
# Account Team Members
yield _sse({"message": "Creating account team members...", "progress": 95})
team_created = 0
for acct_id, members in account_team_data.items():
    customer = customers_by_msx_id.get(acct_id)
    if not customer:
        continue
    for member in members:
        existing = AccountTeamMember.query.filter_by(
            customer_id=customer.id, name=member["name"], role=member["role"]
        ).first()
        if existing:
            # Update alias if we have it now
            if not existing.alias and member.get("user_id"):
                existing.alias = get_user_alias(member["user_id"])
        else:
            alias = get_user_alias(member["user_id"]) if member.get("user_id") else None
            atm = AccountTeamMember(
                customer_id=customer.id,
                name=member["name"],
                alias=alias,
                role=member["role"],
                msx_qualifier2=member.get("qualifier2"),
                msx_title=member.get("standardtitle"),
                msx_user_id=member.get("user_id"),
                source='msx',
            )
            db.session.add(atm)
            team_created += 1
db.session.flush()
```

**Stale member cleanup:** If someone is removed from the MSX team, we should mark or remove them. Options:
- Delete `source='msx'` members not seen in this import (aggressive)
- Add an `is_active` flag and deactivate missing ones (safer)
- Do nothing for now and let users manually remove (simplest)

**Recommendation:** Delete missing MSX-sourced members during import (they came from MSX, MSX is authoritative). Manual members (`source='manual'`) are never touched.

### Step 7 — Account Team section on customer view

**File:** `templates/customer_view.html`

Add an "Account Team" card, either in the sidebar or as a section in the main content:

```
┌─────────────────────────────────────────────────┐
│ 👥 Account Team                    [+ Add]      │
├─────────────────────────────────────────────────┤
│ ┌──────────┐                                    │
│ │ CSAM     │  Jane Doe (janedoe)     [MSX]      │
│ │ DCSA     │  Bob Smith (bobs)       [MSX]      │
│ │ PSS      │  Carol White (carolw)   [MSX]      │
│ │ Sec DSS  │  Dave Park (davep)      [MSX]      │
│ │ Notes    │  Custom contact (manual) [✕]       │
│ └──────────┘                                    │
└─────────────────────────────────────────────────┘
```

- Role shown as a badge (color-coded per role category)
- Name + alias displayed
- `[MSX]` tag on auto-imported members (read-only, can't delete from UI)
- `[✕]` delete button on manual members only
- `[+ Add]` opens modal for manual team member entry
- Group/sort by role
- Show the existing seller and SEs here too — unified team view

### Step 8 — Manual team member add/edit (for roles not in MSX)

**File:** `app/routes/customers.py`

API routes for manual CRUD:

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/customers/<id>/team` | GET | List all team members |
| `/api/customers/<id>/team` | POST | Add manual team member |
| `/api/customers/<id>/team/<member_id>` | DELETE | Remove manual team member |

MSX-sourced members are managed by the import — no edit/delete via API.

### Step 9 — Backup/restore integration

**File:** `app/services/backup.py`

Add `team_members` to customer export:
```json
"team_members": [
  {
    "name": "Jane Doe",
    "alias": "janedoe",
    "role": "CSAM",
    "source": "msx",
    "msx_qualifier2": "Customer Success",
    "msx_title": "CSU IC"
  }
]
```

On restore: create `AccountTeamMember` records, dedup by `(customer_id, name, role)`. Source is preserved.

## Files Changed

| File | Change |
|------|--------|
| `app/models.py` | New `AccountTeamMember` model + Customer relationship |
| `app/migrations.py` | Idempotent migration for `account_team_members` table |
| `app/services/msx_api.py` | Widen filter + add role extraction in `batch_query_account_teams()` |
| `app/routes/msx.py` | Write `AccountTeamMember` records during import + stale cleanup |
| `app/routes/customers.py` | Manual team member CRUD API |
| `templates/customer_view.html` | Account Team card |
| `app/services/backup.py` | Export/restore team members |

## Testing

- Test role extraction with mock MSX data for each qualifier2/title combo
- Test dedup logic (same person, same role, same customer)
- Test stale cleanup removes MSX members no longer in team, preserves manual
- Test manual CRUD endpoints
- Test backup/restore includes team members
- Test customer view renders team members grouped by role

## Execution Order

1. **Step 1 first** — Run discovery query to get real qualifier values, then refine the role mapping before writing any code
2. Steps 2-3 (model + migration)
3. Steps 4-5 (MSX query extension + role extraction)
4. Step 6 (import write phase)
5. Step 7 (customer view UI)
6. Steps 8-9 (manual CRUD + backup) — can be done as fast follow

## Open Questions

1. **What are the exact `msp_qualifier2` / `msp_standardtitle` values for CSAM, DCSA, PSS, Security?** — Resolved by Step 1 discovery dump. We'll refine the role mapping with real data before implementing.
2. **Should we store ALL Corporate team members or only specific roles?** — **Leaning toward:** Only specific roles we map. Unknown roles are ignored (but we can log them for future review).
3. **Stale member handling?** — **Decision:** Delete MSX-sourced members not seen in latest import. Manual members (`source='manual'`) are never auto-removed.
4. **Should we link existing SolutionEngineer/Seller models to the team display?** — **Leaning toward:** Yes, show the existing seller and SEs on the Account Team card too (derived from seller_id FK and SE→POD→territory chain) alongside the new AccountTeamMember records. Single unified view.
