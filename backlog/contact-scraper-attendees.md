# Contact Scraper & Note Attendees

## Overview

Two related features:
1. **Contact Scraper** - Scrape contacts from WorkIQ meetings for customers and partners, with a review/import UI
2. **Note Attendees** - Tag people who attended a call on the note itself

## Current State

- `CustomerContact` model: id, customer_id, name, email, title, created_at
- `PartnerContact` model: id, partner_id, name, title, email, is_primary, created_at
- `SolutionEngineer` model: id, name, alias, specialty (has `get_email()`)
- `Seller` model: id, name, alias, seller_type (has `get_email()`)
- `Note` model: **no attendee/participant fields** - just customer_id + call_date + content
- WorkIQ integration exists: fetches meetings by date, gets summaries via AI gateway
- Gateway client authenticates via Azure CLI credential, routes through APIM

## Architecture

```
WorkIQ Meeting Data (via gateway)
        |
        v
  AI Prompt: "Extract unique external participants from meetings with [company]"
        |
        v
  JSON response: [{name, email, title, domain}, ...]
        |
        v
  Review UI: user picks contacts to import, edits fields, resolves duplicates
        |
        v
  CustomerContact / PartnerContact records created/updated
```

For note attendees:
```
Note form
  |
  +-- Manual lookup (search SEs, customer contacts, partner contacts, sellers)
  |
  +-- WorkIQ auto-detect (parse attendees from meeting transcript)
  |
  v
NoteAttendee join table (note_id + polymorphic person reference)
```

---

## Phase 1: Data Model & Attendee Basics

### 1a. NoteAttendee model

Add a join table to track who attended a call. Polymorphic approach: each attendee row references one of several person types.

```python
class NoteAttendee(db.Model):
    __tablename__ = 'note_attendees'

    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=False)

    # Polymorphic: exactly one of these should be set
    customer_contact_id = db.Column(db.Integer, db.ForeignKey('customer_contacts.id'), nullable=True)
    partner_contact_id = db.Column(db.Integer, db.ForeignKey('partner_contacts.id'), nullable=True)
    solution_engineer_id = db.Column(db.Integer, db.ForeignKey('solution_engineers.id'), nullable=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True)

    # For ad-hoc attendees not in any contact list (e.g., one-off external guests)
    external_name = db.Column(db.String(200), nullable=True)
    external_email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
```

Relationships on Note: `note.attendees` (back_populates)

Helper property on NoteAttendee: `display_name`, `email`, `person_type` (returns "customer_contact", "partner_contact", "se", "seller", or "external")

### 1b. Migration

Idempotent migration in `app/migrations.py` to create the `note_attendees` table.

### 1c. API endpoints

- `GET /api/note/<id>/attendees` - List attendees for a note
- `POST /api/note/<id>/attendees` - Add attendee (body: `{type: "customer_contact", id: 5}` or `{type: "external", name: "...", email: "..."}`)
- `DELETE /api/note/<id>/attendees/<attendee_id>` - Remove attendee

### 1d. Tests

- Model tests for NoteAttendee CRUD
- API tests for add/remove/list attendees
- Test polymorphic resolution (display_name, person_type)

---

## Phase 2: Attendee UI on Note Form

### 2a. Attendee section on note form

Add an "Attendees" section below the existing metadata fields on the note form. Shows badge pills for each attendee with an X to remove.

Badge colors by type:
- Customer contacts: `bg-success` (green) with person icon
- Partner contacts: purple (matching existing partner badge style)
- SEs: `bg-info` (teal) with tools icon
- Sellers: `bg-primary` (blue) with person icon
- External: `bg-secondary` (gray)

### 2b. Attendee search/add

A search input that searches across all person types at once:
- Customer contacts (for the note's customer)
- Partner contacts (for partners tagged on the note)
- Solution engineers
- Sellers
- Freeform "Add external" option at bottom

Single API endpoint: `GET /api/attendee-search?q=...&customer_id=...&partner_ids=...`
- Returns grouped results: `{customer_contacts: [...], partner_contacts: [...], ses: [...], sellers: [...]}`
- Customer contacts filtered to the note's customer
- Partner contacts filtered to partners tagged on the note
- SEs and sellers: full list, filtered by query

### 2c. Display on note view

Show attendees as a row of badge pills on the note view page (below the metadata). Clicking an attendee navigates to their parent entity (customer page, partner page, etc.).

### 2d. Tests

- Test attendee search API returns correct types
- Test attendee display on note view

---

## Phase 3: Contact Scraper - AI Extraction

### 3a. Gateway prompt for contact extraction

New gateway endpoint or prompt type: given a company name and time range, extract unique external contacts from WorkIQ meetings.

Prompt strategy:
```
Given the following meeting transcripts/attendee lists involving [company_name],
extract all unique participants whose email domain matches [domain_hint].

Return as JSON array:
[{"name": "Jane Smith", "email": "jane@xoriant.com", "title": "CTO"}, ...]

Rules:
- Deduplicate by email (keep the most complete name/title)
- Exclude Microsoft employees (@microsoft.com)
- If title is not available, set to null
- Only include people from the specified domain
```

This requires the gateway to:
1. Fetch meetings from WorkIQ for the given time range that mention the company
2. Extract attendee lists from those meetings
3. Use AI to deduplicate and clean the results

### 3b. Backend endpoint

`POST /api/contacts/scrape` with body:
```json
{
    "entity_type": "customer",  // or "partner"
    "entity_id": 42,
    "domain_hint": "xoriant.com",  // optional, auto-derived from customer/partner
    "months_back": 12
}
```

Returns:
```json
{
    "contacts": [
        {"name": "Jane Smith", "email": "jane@xoriant.com", "title": "CTO", "existing_match": null},
        {"name": "Bob Lee", "email": "bob@xoriant.com", "title": null, "existing_match": {"id": 5, "name": "Robert Lee", "email": "bob@xoriant.com"}}
    ],
    "meetings_scanned": 14,
    "domain": "xoriant.com"
}
```

The `existing_match` field shows if we already have a contact with the same email or name, so the UI can show merge/update options.

### 3c. Domain resolution

For customers: Try to derive domain from existing contact emails, or from the customer website field if available.
For partners: Same - check existing contact emails or partner website field.

If no domain can be determined, prompt the user to enter it.

### 3d. Tests

- Test contact extraction prompt building
- Test existing_match deduplication logic
- Test domain resolution from existing contacts/websites

---

## Phase 4: Contact Scraper - Review & Import UI

### 4a. Scrape button on customer/partner view

Add a "Scrape Contacts from WorkIQ" button (or similar) on:
- Customer view page (contacts card)
- Partner view page (contacts card)

Button triggers the scrape, shows a spinner, then opens the review UI.

### 4b. Review modal/page

A modal (or inline expandable section) showing:
- Summary: "Found 8 contacts from 14 meetings in the last 12 months"
- Table of scraped contacts with columns: Import (checkbox), Name (editable), Email (editable), Title (editable), Status

Status column:
- **New** - No matching contact exists, will create
- **Match found** - Existing contact with same email. Shows current vs. scraped values. User can choose: Update, Skip, or keep as-is
- **Name match** - Same name but different email. User confirms if it's the same person

Each row has a checkbox to include/exclude from import. All checked by default except exact duplicates.

### 4c. Import action

"Import Selected" button:
- Creates new CustomerContact/PartnerContact records for new contacts
- Updates existing records where user chose "Update"
- Skips where user chose "Skip"
- Shows summary: "Created 5, updated 2, skipped 1"

### 4d. Tests

- Test import creates correct records
- Test update vs. skip logic
- Test UI renders review table correctly

---

## Phase 5: WorkIQ Auto-Detect Attendees on Notes

### 5a. Extend WorkIQ meeting import

When importing a meeting via WorkIQ (auto-fill or manual), also extract the attendee list from the meeting data.

Modify the WorkIQ summary prompt to include:
```
Also extract the meeting attendees. For each attendee, provide:
- name, email

Return attendees in a separate "attendees" key in the JSON response.
```

### 5b. Auto-match attendees to existing contacts

After getting the attendee list from WorkIQ:
1. Match `@microsoft.com` emails to sellers/SEs by alias
2. Match customer domain emails to customer contacts by email
3. Match partner domain emails to partner contacts by email
4. Anything unmatched becomes an "external" attendee or is offered for quick-add

### 5c. "Import Attendees" button on note form

When WorkIQ returns meeting data with attendees:
- Show a small callout: "8 attendees detected"
- Clicking it opens a quick picker showing matched people with checkboxes
- User confirms which attendees to tag on the note

### 5d. Tests

- Test attendee matching logic (Microsoft employees to sellers/SEs, external to contacts)
- Test auto-fill flow includes attendees

---

## Phase 6: Polish & Integration

### 6a. Note list/view enhancements

- Show attendee count or avatars on note list items
- Attendee names in note search index (so you can search "who was on calls with Jane")

### 6b. Reports integration

- "Who have we met with?" report or filter on customer view
- Contact frequency: which customer contacts we meet with most often

### 6c. Contact scraper from note form flyout

Add an "Import from WorkIQ" button inside the customer contacts flyout and partner flyout on the note form, so users can scrape contacts without leaving the note.

---

## Open Questions

1. **Domain resolution**: What if a customer has contacts from multiple domains (e.g., after an acquisition)? Allow multiple domain hints?
2. **WorkIQ data format**: Does WorkIQ return structured attendee lists, or do we need AI to parse them from transcript text?
3. **Privacy**: Should we store meeting attendee data even if they're not explicitly added as contacts? Or only store what the user confirms?
4. **Rate limiting**: How many meetings can we scan at once via WorkIQ? May need pagination for heavy users.
5. **Attendee ordering**: Should attendees on a note have an order (e.g., primary contact first)?

## Build Order

Phase 1 and 2 are independent of the scraper and can ship first - they give users manual attendee tagging immediately. Phase 3-5 layer on the AI-powered scraping. Phase 6 is polish.
