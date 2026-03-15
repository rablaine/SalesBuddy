"""
Partner sharing service — serialization, upsert, and Socket.IO connection config.

Handles:
- Serializing partners to JSON for sharing
- Upserting received partners into the local database
- Providing Socket.IO connection details (gateway URL + JWT)
"""
import logging
import os
import re

from app.models import db, Partner, PartnerContact, Specialty
from app.routes.admin import fetch_favicon_for_domain
from app.routes.msx import _extract_domain


# Regex to match emoji and other non-text symbol characters
_EMOJI_RE = re.compile(
    r'['
    r'\U0001F600-\U0001F64F'  # emoticons
    r'\U0001F300-\U0001F5FF'  # symbols & pictographs
    r'\U0001F680-\U0001F6FF'  # transport & map
    r'\U0001F1E0-\U0001F1FF'  # flags
    r'\U0001F900-\U0001F9FF'  # supplemental symbols
    r'\U0001FA00-\U0001FA6F'  # chess symbols
    r'\U0001FA70-\U0001FAFF'  # symbols extended-A
    r'\U00002702-\U000027B0'  # dingbats
    r'\U0000FE00-\U0000FE0F'  # variation selectors
    r'\U0000200D'              # zero-width joiner
    r'\U000020E3'              # combining enclosing keycap
    r'\U00002600-\U000026FF'  # misc symbols
    r'\U00002B50-\U00002B55'  # stars
    r']+',
)

# Common company suffixes to strip for matching purposes
_COMPANY_SUFFIXES = re.compile(
    r'[,.]?\s+'
    r'(?:LLC|L\.?L\.?C\.?|Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited|'
    r'Co\.?|Company|LP|L\.?P\.?|LLP|L\.?L\.?P\.?|PLC|P\.?L\.?C\.?|'
    r'GmbH|AG|SA|SRL|BV|NV|Pty|Group|Holdings|International|Intl\.?)'
    r'\s*$',
    re.IGNORECASE,
)


def _normalize_company_name(name: str) -> str:
    """Normalize a company name for matching: strip emojis, suffixes, lowercase."""
    n = name.strip()
    # Strip emojis
    n = _EMOJI_RE.sub('', n)
    # Repeatedly strip suffixes (handles "Contoso Corp. LLC")
    while True:
        cleaned = _COMPANY_SUFFIXES.sub('', n)
        if cleaned == n:
            break
        n = cleaned
    # Strip trailing punctuation / whitespace
    n = n.strip(' ,.')
    return n.lower()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Socket.IO gateway URLs — direct connection (bypasses APIM)
# ---------------------------------------------------------------------------
_APIM_BASE = "https://apim-notehelper.azure-api.net"
_APP_SERVICE_BASE = "https://app-notehelper-ai.azurewebsites.net"
_APP_SERVICE_STAGING = "https://app-notehelper-ai-staging.azurewebsites.net"


def get_share_gateway_url() -> str:
    """Return the direct App Service URL for Socket.IO connections.

    Socket.IO connects directly to the App Service rather than through APIM
    because APIM doesn't transparently proxy WebSocket/long-polling without
    a dedicated WebSocket API definition.
    """
    if os.environ.get("AI_GATEWAY_URL"):
        # Local dev override
        return os.environ["AI_GATEWAY_URL"]
    if os.environ.get("AI_USE_STAGING", "").lower() in ("1", "true"):
        return _APP_SERVICE_STAGING
    return _APP_SERVICE_BASE


def get_share_token() -> str | None:
    """Get a JWT token for authenticating with the sharing hub.

    Reuses the same credential as gateway_call().
    Returns None if authentication fails.
    """
    try:
        from app.gateway_client import _get_token
        return _get_token()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Serialization — partner → JSON dict
# ---------------------------------------------------------------------------

def serialize_partner(partner: Partner) -> dict:
    """Convert a Partner ORM object to a shareable JSON dict."""
    return {
        "name": partner.name,
        "overview": partner.overview,
        "rating": partner.rating,
        "website": partner.website,
        "favicon_b64": partner.favicon_b64,
        "specialties": [s.name for s in partner.specialties],
        "contacts": [
            {
                "name": c.name,
                "email": c.email,
                "is_primary": c.is_primary,
            }
            for c in partner.contacts
        ],
    }


def serialize_all_partners() -> list[dict]:
    """Serialize the entire partner directory."""
    partners = Partner.query.order_by(Partner.name).all()
    return [serialize_partner(p) for p in partners]


# ---------------------------------------------------------------------------
# Upsert — received partner JSON → local DB
# ---------------------------------------------------------------------------


def _find_matching_partner(name: str, website: str) -> Partner | None:
    """Find an existing partner by normalized name or normalized website."""
    normalized = _normalize_company_name(name)
    partners = Partner.query.all()
    # Try name match (normalized — strips LLC, Inc, etc.)
    for p in partners:
        if _normalize_company_name(p.name) == normalized:
            return p
    # Try website match
    if website:
        for p in partners:
            if p.website and _extract_domain(p.website).lower() == website:
                return p
    return None


def upsert_partner(data: dict, sender_name: str) -> dict:
    """Upsert a single partner from received share data.

    Matching: case-insensitive name OR website.

    For existing partners:
    - Contacts: add new contacts that don't match existing emails
    - Overview: append sender's rating + overview as a section
    - Specialties: add any not already on the record
    - Website/favicon: update if different or missing

    For new partners: create as duplicate of sender's data.

    Returns: {action: "created"|"updated", name: str}
    """
    name = (data.get("name") or "").strip()
    raw_website = (data.get("website") or "").strip()
    website = _extract_domain(raw_website).lower() if raw_website else ""

    if not name:
        return {"action": "skipped", "name": "(empty)"}

    # Find existing match: normalized name OR normalized website
    existing = _find_matching_partner(name, website)

    if existing:
        return _update_existing_partner(existing, data, sender_name)
    else:
        return _create_new_partner(data)


def _update_existing_partner(partner: Partner, data: dict, sender_name: str) -> dict:
    """Update an existing partner with data from the sender."""

    # 1. Contacts — add new ones by email (case-insensitive)
    existing_emails = {
        (c.email or "").lower() for c in partner.contacts if c.email
    }
    for contact_data in data.get("contacts", []):
        email = (contact_data.get("email") or "").strip()
        if email and email.lower() not in existing_emails:
            contact = PartnerContact(
                partner_id=partner.id,
                name=contact_data.get("name", "Unknown"),
                email=email,
                is_primary=False,  # don't override existing primary
            )
            db.session.add(contact)
            existing_emails.add(email.lower())

    # 2. Overview — append sender's review as a section
    sender_rating = data.get("rating")
    sender_overview = (data.get("overview") or "").strip()
    if sender_overview or sender_rating is not None:
        section_parts = [f"\n\n--- {sender_name}'s review ---"]
        if sender_rating is not None:
            stars = "★" * sender_rating + "☆" * (5 - sender_rating)
            section_parts.append(f"Rating: {stars}")
        if sender_overview:
            section_parts.append(sender_overview)
        new_section = "\n".join(section_parts)

        if partner.overview:
            # Don't duplicate if this sender's section already exists
            if f"--- {sender_name}'s review ---" not in partner.overview:
                partner.overview = partner.overview + new_section
        else:
            partner.overview = new_section.strip()

    # 3. Specialties — add missing ones
    existing_specialty_names = {s.name.lower() for s in partner.specialties}
    for specialty_name in data.get("specialties", []):
        if specialty_name.lower() not in existing_specialty_names:
            specialty = Specialty.query.filter(
                db.func.lower(Specialty.name) == specialty_name.lower()
            ).first()
            if not specialty:
                specialty = Specialty(name=specialty_name)
                db.session.add(specialty)
                db.session.flush()
            partner.specialties.append(specialty)
            existing_specialty_names.add(specialty_name.lower())

    # 4. Website/favicon — upsert (normalize through _extract_domain)
    raw_incoming_website = (data.get("website") or "").strip()
    incoming_website = _extract_domain(raw_incoming_website) if raw_incoming_website else ""
    if incoming_website:
        existing_normalized = _extract_domain(partner.website).lower() if partner.website else ""
        if not partner.website or existing_normalized != incoming_website.lower():
            partner.website = incoming_website
            partner.favicon_b64 = data.get("favicon_b64")
        elif not partner.favicon_b64 and data.get("favicon_b64"):
            partner.favicon_b64 = data.get("favicon_b64")

    return {"action": "updated", "name": partner.name}


def _create_new_partner(data: dict) -> dict:
    """Create a new partner from received share data."""
    website = (data.get("website") or "").strip()
    website = _extract_domain(website) if website else None

    partner = Partner(
        name=data["name"].strip(),
        overview=(data.get("overview") or "").strip() or None,
        rating=data.get("rating"),
        website=website,
        favicon_b64=data.get("favicon_b64"),
    )
    db.session.add(partner)
    db.session.flush()  # get partner.id

    # Add contacts
    for contact_data in data.get("contacts", []):
        contact = PartnerContact(
            partner_id=partner.id,
            name=contact_data.get("name", "Unknown"),
            email=contact_data.get("email"),
            is_primary=contact_data.get("is_primary", False),
        )
        db.session.add(contact)

    # Add specialties
    for specialty_name in data.get("specialties", []):
        specialty = Specialty.query.filter(
            db.func.lower(Specialty.name) == specialty_name.lower()
        ).first()
        if not specialty:
            specialty = Specialty(name=specialty_name)
            db.session.add(specialty)
            db.session.flush()
        partner.specialties.append(specialty)

    return {"action": "created", "name": partner.name}


def preview_partners(partners_data: list[dict], sender_name: str) -> list[dict]:
    """Dry-run preview of what would happen if partners were imported.

    Returns a list of dicts with structured change data so the UI can render
    comprehensive detail for both new and updated partners.
    """
    previews = []
    for data in partners_data:
        name = (data.get("name") or "").strip()
        raw_website = (data.get("website") or "").strip()
        website = _extract_domain(raw_website).lower() if raw_website else ""

        if not name:
            continue

        # Find existing match (same logic as upsert_partner)
        existing = _find_matching_partner(name, website)

        specialties = data.get("specialties", [])
        contacts = data.get("contacts", [])

        if existing:
            previews.append(_preview_update(existing, data, contacts, specialties, sender_name))
        else:
            previews.append(_preview_create(name, data, contacts, specialties))

    return previews


def _preview_create(
    name: str, data: dict, contacts: list, specialties: list,
) -> dict:
    """Build preview dict for a partner that would be created."""
    raw_website = (data.get("website") or "").strip()
    clean_website = _extract_domain(raw_website) if raw_website else None
    return {
        "name": name,
        "action": "create",
        "has_changes": True,
        "incoming": {
            "website": clean_website,
            "rating": data.get("rating"),
            "overview": (data.get("overview") or "").strip() or None,
            "specialties": specialties,
            "contacts": [
                {
                    "name": c.get("name", "Unknown"),
                    "email": c.get("email"),
                    "is_primary": c.get("is_primary", False),
                }
                for c in contacts
            ],
            "favicon_b64": data.get("favicon_b64") or None,
        },
    }


def _preview_update(
    existing: "Partner", data: dict, contacts: list, specialties: list,
    sender_name: str,
) -> dict:
    """Build preview dict for a partner that would be updated."""
    changes = {}

    # New contacts
    existing_emails = {(c.email or "").lower() for c in existing.contacts if c.email}
    new_contacts = [c for c in contacts if (c.get("email") or "").lower() not in existing_emails]
    if new_contacts:
        changes["contacts"] = [
            {"name": c.get("name", "Unknown"), "email": c.get("email")}
            for c in new_contacts
        ]

    # New specialties
    existing_spec_names = {s.name.lower() for s in existing.specialties}
    new_specs = [s for s in specialties if s.lower() not in existing_spec_names]
    if new_specs:
        changes["specialties"] = new_specs

    # Sender review / overview
    sender_overview = (data.get("overview") or "").strip()
    if sender_overview and f"--- {sender_name}'s review ---" not in (existing.overview or ""):
        changes["overview"] = sender_overview

    # Rating
    incoming_rating = data.get("rating")
    if incoming_rating and incoming_rating != existing.rating:
        changes["rating"] = incoming_rating

    # Website
    raw_incoming = (data.get("website") or "").strip()
    incoming_website = _extract_domain(raw_incoming) if raw_incoming else ""
    if incoming_website and (not existing.website or _extract_domain(existing.website).lower() != incoming_website.lower()):
        changes["website"] = incoming_website

    # Favicon
    if data.get("favicon_b64") and not existing.favicon_b64:
        changes["favicon"] = True

    return {
        "name": existing.name,
        "action": "update",
        "has_changes": bool(changes),
        "changes": changes,
    }


def upsert_partners(partners_data: list[dict], sender_name: str) -> dict:
    """Upsert a list of partners from received share data.

    Returns: {created: int, updated: int, skipped: int, details: [...]}
    """
    results = {"created": 0, "updated": 0, "skipped": 0, "details": []}
    for p in partners_data:
        result = upsert_partner(p, sender_name)
        results[result["action"]] = results.get(result["action"], 0) + 1
        results["details"].append(result)

    db.session.commit()
    return results
