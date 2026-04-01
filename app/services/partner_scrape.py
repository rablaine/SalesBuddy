"""
Partner data scraping service.
Queries WorkIQ for meeting data involving a partner and extracts
contacts, specialties, and overview information.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.models import db, Partner, PartnerContact, Specialty

logger = logging.getLogger(__name__)


def scrape_partner_data(partner: Partner) -> Dict[str, Any]:
    """Query WorkIQ for partner data and return structured results.

    Args:
        partner: The Partner model instance to scrape data for.

    Returns:
        Dict with keys:
        - contacts: list of {name, email, title, is_new, existing_id, has_updates}
        - specialties: list of {name, is_new}
        - overview: suggested overview text
        - meetings_found: number of meetings referenced
        - raw_response: the raw WorkIQ response for debugging
    """
    from app.services.workiq_service import query_workiq

    partner_name = partner.name
    domain_hint = _get_domain_hint(partner)

    question = _build_scrape_prompt(partner_name, domain_hint)

    try:
        raw_response = query_workiq(question, timeout=180)
    except Exception as e:
        logger.error(f"WorkIQ query failed for partner {partner_name}: {e}")
        raise

    parsed = _parse_response(raw_response)

    # Match contacts against existing
    contacts_with_matches = _match_contacts(
        parsed.get("contacts", []),
        partner.contacts
    )

    # Match specialties against existing
    specialties_with_matches = _match_specialties(
        parsed.get("specialties", []),
        partner.specialties
    )

    return {
        "contacts": contacts_with_matches,
        "specialties": specialties_with_matches,
        "overview": parsed.get("overview", ""),
        "meetings_found": parsed.get("meetings_found", 0),
        "raw_response": raw_response,
    }


def apply_scrape_results(
    partner: Partner,
    contacts: List[Dict],
    specialties: List[str],
    overview: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply user-selected scrape results to the partner.

    Args:
        partner: The Partner to update.
        contacts: List of contact dicts to import/update.
        specialties: List of specialty names to add.
        overview: Optional new overview text (replaces existing if provided).

    Returns:
        Summary dict with counts of created/updated items.
    """
    contacts_created = 0
    contacts_updated = 0

    for c in contacts:
        existing_id = c.get("existing_id")
        if existing_id:
            # Update existing contact
            contact = db.session.get(PartnerContact, existing_id)
            if contact:
                updated = False
                if c.get("email") and not contact.email:
                    contact.email = c["email"]
                    updated = True
                if c.get("title") and not contact.title:
                    contact.title = c["title"]
                    updated = True
                if updated:
                    contacts_updated += 1
        else:
            # Create new contact
            contact = PartnerContact(
                partner_id=partner.id,
                name=(c.get("name") or "").strip(),
                email=(c.get("email") or "").strip() or None,
                title=(c.get("title") or "").strip() or None,
            )
            db.session.add(contact)
            contacts_created += 1

    specialties_created = 0
    existing_spec_names = {s.name.lower() for s in partner.specialties}
    for spec_name in specialties:
        if spec_name.lower() in existing_spec_names:
            continue
        # Find or create specialty
        spec = Specialty.query.filter(
            db.func.lower(Specialty.name) == spec_name.lower()
        ).first()
        if not spec:
            spec = Specialty(name=spec_name)
            db.session.add(spec)
            db.session.flush()
        partner.specialties.append(spec)
        existing_spec_names.add(spec_name.lower())
        specialties_created += 1

    if overview and overview.strip():
        partner.overview = overview.strip()

    db.session.commit()

    return {
        "contacts_created": contacts_created,
        "contacts_updated": contacts_updated,
        "specialties_created": specialties_created,
        "overview_updated": bool(overview and overview.strip()),
    }


def _get_domain_hint(partner: Partner) -> Optional[str]:
    """Try to derive an email domain from partner website or existing contacts."""
    if partner.website:
        domain = partner.website.lower().strip()
        domain = domain.replace("https://", "").replace("http://", "")
        domain = domain.replace("www.", "").split("/")[0]
        if domain:
            return domain

    for contact in partner.contacts:
        if contact.email and "@" in contact.email:
            return contact.email.split("@")[1].lower()

    return None


def _build_scrape_prompt(partner_name: str, domain_hint: Optional[str]) -> str:
    """Build the WorkIQ question for partner data extraction."""
    if not domain_hint:
        return (
            f"Search my meetings and emails from the last 12 months involving "
            f'people from "{partner_name}". '
            f"Return ONLY a valid JSON object with keys: "
            f"contacts (array of objects with name, email, title), "
            f"specialties (array of tech area strings), "
            f"overview (2-3 sentence summary string), "
            f"meetings_found (integer count). "
            f"Only include contacts from {partner_name}, not Microsoft employees. "
            f"Deduplicate by email. Use null for unknown titles. "
            f"Return ONLY valid JSON, no other text."
        )

    return (
        f"Search my meetings and emails from the last 12 months where someone "
        f"with an @{domain_hint} email was an attendee or participant. "
        f"Return ONLY a valid JSON object with keys: "
        f"contacts (array of objects with name, email, title), "
        f"specialties (array of short tech area strings like Fabric or SQL Migration), "
        f"overview (2-3 sentence summary of what {partner_name} does and excels at based on our interactions), "
        f"meetings_found (integer count of meetings found). "
        f"Only include people with @{domain_hint} emails, not Microsoft employees. "
        f"Deduplicate contacts by email address. Use null for unknown titles. "
        f"Return ONLY valid JSON, no other text."
    )


def _parse_response(raw: str) -> Dict[str, Any]:
    """Parse the WorkIQ response into structured data."""
    # Try to extract JSON from the response
    text = raw.strip()

    # Strip markdown code blocks if present
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    # Find JSON object in the response
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        text = text[brace_start:brace_end + 1]

    try:
        data = json.loads(text)
        contacts = data.get("contacts", [])
        meetings = data.get("meetings_found", 0)
        # If WorkIQ returned contacts but no meetings_found, infer it
        if contacts and not meetings:
            meetings = len(contacts)
        return {
            "contacts": contacts,
            "specialties": data.get("specialties", []),
            "overview": data.get("overview", ""),
            "meetings_found": meetings,
        }
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse WorkIQ JSON response: {e}")
        logger.debug(f"Raw response: {raw[:500]}")
        return {
            "contacts": [],
            "specialties": [],
            "overview": "",
            "meetings_found": 0,
        }


def _match_contacts(
    scraped: List[Dict], existing: List[PartnerContact]
) -> List[Dict]:
    """Match scraped contacts against existing ones.

    Returns enriched list with match info for each contact.
    """
    result = []
    existing_by_email = {}
    existing_by_name = {}
    for c in existing:
        if c.email:
            existing_by_email[c.email.lower()] = c
        existing_by_name[c.name.lower()] = c

    for sc in scraped:
        name = (sc.get("name") or "").strip()
        email = (sc.get("email") or "").strip()
        title = (sc.get("title") or "").strip()
        if not name:
            continue

        entry = {
            "name": name,
            "email": email or None,
            "title": title or None,
            "is_new": True,
            "existing_id": None,
            "existing_name": None,
            "has_updates": False,
            "updates": [],
        }

        # Match by email first, then by name
        match = None
        if email:
            match = existing_by_email.get(email.lower())
        if not match:
            match = existing_by_name.get(name.lower())

        if match:
            entry["is_new"] = False
            entry["existing_id"] = match.id
            entry["existing_name"] = match.name
            # Check for new data
            if email and not match.email:
                entry["has_updates"] = True
                entry["updates"].append(f"Add email: {email}")
            if title and not match.title:
                entry["has_updates"] = True
                entry["updates"].append(f"Add title: {title}")

        result.append(entry)
    return result


def _match_specialties(
    scraped: List[str], existing: List[Any]
) -> List[Dict]:
    """Match scraped specialties against existing ones."""
    existing_names = {s.name.lower() for s in existing}
    result = []
    seen = set()
    for name in scraped:
        name = (name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append({
            "name": name,
            "is_new": name.lower() not in existing_names,
        })
    return result
