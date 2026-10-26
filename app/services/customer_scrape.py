"""
Customer contact scraping service.
Queries WorkIQ for meeting data involving a customer's domain and extracts contacts.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.models import db, Customer, CustomerContact

logger = logging.getLogger(__name__)


def scrape_customer_contacts(customer: Customer) -> Dict[str, Any]:
    """Query WorkIQ for customer contacts and return structured results.

    Args:
        customer: The Customer model instance to scrape contacts for.

    Returns:
        Dict with keys:
        - contacts: list of {name, email, title, is_new, existing_id, has_updates, updates}
        - meetings_found: number of meetings referenced
        - raw_response: raw WorkIQ response for debugging
    """
    from app.services.workiq_service import query_workiq

    domain_hint = _get_domain_hint(customer)
    if not domain_hint:
        raise ValueError("No email domain available. Add a website or contact with an email first.")

    question = _build_scrape_prompt(customer.name, domain_hint)

    try:
        raw_response = query_workiq(question, timeout=180,
                                    operation='customer_scrape')
    except Exception as e:
        logger.error(f"WorkIQ query failed for customer {customer.name}: {e}")
        raise

    parsed = _parse_response(raw_response)

    contacts_with_matches = _match_contacts(
        parsed.get("contacts", []),
        customer.contacts
    )

    return {
        "contacts": contacts_with_matches,
        "meetings_found": parsed.get("meetings_found", 0),
        "raw_response": raw_response,
    }


def apply_customer_contacts(
    customer: Customer,
    contacts: List[Dict],
) -> Dict[str, Any]:
    """Apply user-selected scrape results to the customer.

    Args:
        customer: The Customer to update.
        contacts: List of contact dicts to import/update.

    Returns:
        Summary dict with counts.
    """
    contacts_created = 0
    contacts_updated = 0

    for c in contacts:
        existing_id = c.get("existing_id")
        if existing_id:
            contact = db.session.get(CustomerContact, existing_id)
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
            contact = CustomerContact(
                customer_id=customer.id,
                name=(c.get("name") or "").strip(),
                email=(c.get("email") or "").strip() or None,
                title=(c.get("title") or "").strip() or None,
            )
            db.session.add(contact)
            contacts_created += 1

    db.session.commit()

    return {
        "contacts_created": contacts_created,
        "contacts_updated": contacts_updated,
    }


def _get_domain_hint(customer: Customer) -> Optional[str]:
    """Try to derive an email domain from customer website or existing contacts."""
    if customer.website:
        domain = customer.website.lower().strip()
        domain = domain.replace("https://", "").replace("http://", "")
        domain = domain.replace("www.", "").split("/")[0]
        if domain:
            return domain

    for contact in customer.contacts:
        if contact.email and "@" in contact.email:
            return contact.email.split("@")[1].lower()

    return None


def _build_scrape_prompt(customer_name: str, domain_hint: str) -> str:
    """Build the WorkIQ question for customer contact extraction."""
    return (
        f"Search my meetings and emails from the last 12 months where someone "
        f"with an @{domain_hint} email was an attendee or participant. "
        f"Return ONLY one valid JSON object inside a single ```json code block with keys: "
        f"contacts (array of objects with name, email, title), "
        f"meetings_found (integer count of meetings found). "
        f"Only include people with @{domain_hint} emails, not Microsoft employees. "
        f"Only include contacts whose name is present in the source; do not invent names. "
        f"Deduplicate contacts by email address. Use null for unknown titles. "
        f"Return the JSON code block once, with no other text."
    )


def _parse_response(raw: str) -> Dict[str, Any]:
    """Parse the WorkIQ response into structured data."""
    from app.services.workiq_service import repair_json_control_chars, _record_workiq_call

    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    brace_start = text.find("{")
    if brace_start >= 0:
        text = text[brace_start:]

    # WorkIQ sometimes emits raw newline bytes inside JSON string values
    # (e.g. a contact name split across lines). Escape them so json.loads
    # doesn't fail with "Invalid control character".
    text = repair_json_control_chars(text)

    try:
        # The CLI can append a second, line-wrapped copy of its answer.
        # Decode the first complete object, not both copies as one document.
        data, _ = json.JSONDecoder().raw_decode(text)
        return _normalize_response(data)
    except ValueError as e:
        logger.warning(f"Failed to parse WorkIQ JSON response: {e}")
        _record_workiq_call(
            'customer_scrape', 'parse_failed', failure_type='parse_customer_json',
        )
        raise ValueError("WorkIQ returned unreadable results. Try again.") from e


def _normalize_response(data: Any) -> Dict[str, Any]:
    """Validate contact results and normalize CLI-wrapped names and titles."""
    if not isinstance(data, dict) or not isinstance(data.get("contacts"), list):
        raise ValueError("Expected a contacts array")

    meetings = data.get("meetings_found", data.get("meetingsfound", 0))
    if type(meetings) is not int or meetings < 0:
        raise ValueError("Expected a nonnegative meeting count")

    contacts = []
    for contact in data["contacts"]:
        if not isinstance(contact, dict):
            raise ValueError("Expected a contact object")
        entry = {}
        for field in ("name", "email", "title"):
            value = contact.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Expected a string or null for contact {field}")
            entry[field] = " ".join(value.split()) if value else None
        if not entry["name"]:
            logger.warning("Skipping a WorkIQ contact without a name")
            continue
        contacts.append(entry)

    if data["contacts"] and not contacts:
        raise ValueError("No contacts had usable names")
    return {"contacts": contacts, "meetings_found": meetings}


def _match_contacts(
    scraped: List[Dict], existing: List[CustomerContact]
) -> List[Dict]:
    """Match scraped contacts against existing ones."""
    existing_by_email = {}
    existing_by_name = {}
    for c in existing:
        if c.email:
            existing_by_email[c.email.lower()] = c
        existing_by_name[c.name.lower()] = c

    result = []
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

        match = None
        if email:
            match = existing_by_email.get(email.lower())
        if not match:
            match = existing_by_name.get(name.lower())

        if match:
            entry["is_new"] = False
            entry["existing_id"] = match.id
            entry["existing_name"] = match.name
            if email and not match.email:
                entry["has_updates"] = True
                entry["updates"].append(f"Add email: {email}")
            if title and not match.title:
                entry["has_updates"] = True
                entry["updates"].append(f"Add title: {title}")

        result.append(entry)
    return result
