"""
Partner sharing service — serialization, upsert, and Socket.IO connection config.

Handles:
- Serializing partners to JSON for sharing
- Upserting received partners into the local database
- Providing Socket.IO connection details (gateway URL + JWT)
"""
import logging
import os

from app.models import db, Partner, PartnerContact, Specialty
from app.routes.admin import fetch_favicon_for_domain
from app.routes.msx import _extract_domain

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
    website = (data.get("website") or "").strip().lower()

    if not name:
        return {"action": "skipped", "name": "(empty)"}

    # Find existing match: case-insensitive name OR website
    existing = None
    if website:
        existing = Partner.query.filter(
            db.or_(
                db.func.lower(Partner.name) == name.lower(),
                db.func.lower(Partner.website) == website,
            )
        ).first()
    else:
        existing = Partner.query.filter(
            db.func.lower(Partner.name) == name.lower()
        ).first()

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

    # 4. Website/favicon — upsert
    incoming_website = (data.get("website") or "").strip()
    if incoming_website:
        if not partner.website or partner.website.lower() != incoming_website.lower():
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
