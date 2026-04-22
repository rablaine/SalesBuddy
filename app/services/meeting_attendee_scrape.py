"""
Meeting attendee scraping service.
Queries WorkIQ for attendees of a specific meeting and categorizes them
against known contacts, sellers, SEs, and partner domains.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.models import (
    db, Customer, CustomerContact, Partner, PartnerContact,
    Seller, SolutionEngineer,
)

logger = logging.getLogger(__name__)


def scrape_meeting_attendees(
    meeting_title: str,
    meeting_date: str,
    customer_id: Optional[int] = None,
    partner_ids: Optional[List[int]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Query WorkIQ for attendees of a specific meeting and categorize them.

    Args:
        meeting_title: Title of the meeting.
        meeting_date: Date string (YYYY-MM-DD).
        customer_id: Optional customer ID for domain matching.
        partner_ids: Optional list of partner IDs already on the note.
        force_refresh: If True, skip the prefetch cache and call WorkIQ live.
            The cache lacks transcript-derived titles, so the user can opt
            in to a slower live call to pick those up.

    Returns:
        Dict with categorized attendees ready for review. Includes
        ``source`` ("cache" or "workiq") so the UI can show a badge.
    """
    from app.services.workiq_service import query_workiq
    from app.services.meeting_prefetch import get_cached_attendees

    raw_response = ''
    source = 'workiq'
    parsed_attendees: List[Dict[str, Any]] = []

    if not force_refresh:
        cached = get_cached_attendees(meeting_title, meeting_date)
        if cached is not None:
            logger.info(
                "Attendee scrape: cache hit for %r on %s (%d attendees)",
                meeting_title, meeting_date, len(cached),
            )
            parsed_attendees = cached
            source = 'cache'

    if source != 'cache':
        question = _build_attendee_prompt(meeting_title, meeting_date)
        try:
            raw_response = query_workiq(question, timeout=180,
                                        operation='attendee_scrape')
        except Exception as e:
            logger.error(f"WorkIQ attendee query failed: {e}")
            raise
        parsed_attendees = _parse_response(raw_response)

    # Build domain maps for matching
    customer = db.session.get(Customer, customer_id) if customer_id else None
    partners = []
    if partner_ids:
        partners = Partner.query.filter(Partner.id.in_(partner_ids)).all()

    categorized = _categorize_attendees(
        parsed_attendees,
        customer=customer,
        partners=partners,
    )

    return {
        "attendees": categorized,
        "raw_response": raw_response,
        "source": source,
    }


def _build_attendee_prompt(meeting_title: str, meeting_date: str) -> str:
    """Build WorkIQ question for meeting attendee extraction."""
    # Sanitize title - remove characters that break shell escaping
    safe_title = meeting_title.replace("|", "-").replace("`", "").replace("$", "")
    safe_title = safe_title.replace('"', '').replace("'", "")
    return (
        f"List all attendees of my meeting called {safe_title} "
        f"on {meeting_date}. "
        f"Also check the meeting transcript for any introductions where attendees "
        f"stated their job title or role. "
        f"Return ONLY a valid JSON object with key: "
        f"attendees (array of objects with name, email, and title). "
        f"Include ALL attendees including Microsoft employees. "
        f"For title, only include it if mentioned in the transcript, otherwise null. "
        f"Deduplicate by email. Return ONLY valid JSON, no other text."
    )


def _parse_response(raw: str) -> List[Dict[str, str]]:
    """Parse WorkIQ response into list of attendee dicts."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        text = text[brace_start:brace_end + 1]

    try:
        data = json.loads(text)
        return data.get("attendees", [])
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse WorkIQ attendee JSON: {e}")
        try:
            from app.services.telemetry_shipper import queue_workiq_failure
            queue_workiq_failure('attendee_scrape', 'json_parse_failed')
        except Exception:
            pass
        return []


def _get_customer_domains(customer: Optional[Customer]) -> set:
    """Get email domains associated with a customer."""
    domains = set()
    if not customer:
        return domains
    if customer.website:
        d = customer.website.lower().replace("https://", "").replace("http://", "")
        d = d.replace("www.", "").split("/")[0]
        if d:
            domains.add(d)
    for c in customer.contacts:
        if c.email and "@" in c.email:
            domains.add(c.email.split("@")[1].lower())
    return domains


def _get_partner_domain_map(partners: List[Partner]) -> Dict[str, Partner]:
    """Build domain -> Partner mapping from partner websites and contact emails."""
    domain_map = {}
    for p in partners:
        if p.website:
            d = p.website.lower().replace("https://", "").replace("http://", "")
            d = d.replace("www.", "").split("/")[0]
            if d:
                domain_map[d] = p
        for c in p.contacts:
            if c.email and "@" in c.email:
                d = c.email.split("@")[1].lower()
                if d not in domain_map:
                    domain_map[d] = p

    # Also check ALL partners in DB for domain matching
    all_partners = Partner.query.all()
    for p in all_partners:
        if p.website:
            d = p.website.lower().replace("https://", "").replace("http://", "")
            d = d.replace("www.", "").split("/")[0]
            if d and d not in domain_map:
                domain_map[d] = p
        for c in p.contacts:
            if c.email and "@" in c.email:
                d = c.email.split("@")[1].lower()
                if d not in domain_map:
                    domain_map[d] = p

    return domain_map


def _fuzzy_match_domain(domain: str, domain_map: Dict[str, 'Partner']) -> Optional['Partner']:
    """Try to match a domain to an existing partner via base name overlap.

    For example, simformsolutions.com matches simform.com because
    'simform' (base of simform.com) is contained in 'simformsolutions'.
    """
    base = domain.split(".")[0]
    if len(base) < 3:
        return None
    for known_domain, partner in domain_map.items():
        known_base = known_domain.split(".")[0]
        if len(known_base) < 3:
            continue
        if known_base in base or base in known_base:
            return partner
    return None


def _fuzzy_match_customer_domain(domain: str, customer_domains: set) -> bool:
    """Try to match a domain to customer domains via base name overlap.

    For example, redsailconsultants.com matches redsail.com because
    'redsail' (base of redsail.com) is contained in 'redsailconsultants'.
    Uses the same bidirectional substring algorithm as partner fuzzy matching.
    """
    base = domain.split(".")[0]
    if len(base) < 3:
        return False
    for known_domain in customer_domains:
        known_base = known_domain.split(".")[0]
        if len(known_base) < 3:
            continue
        if known_base in base or base in known_base:
            return True
    return False


def _categorize_attendees(
    attendees: List[Dict[str, str]],
    customer: Optional[Customer] = None,
    partners: Optional[List[Partner]] = None,
) -> List[Dict[str, Any]]:
    """Categorize each attendee into a type with match info.

    Categories:
    - microsoft: matched to seller/SE by alias
    - customer_contact: matched to existing customer contact or domain
    - partner_contact: matched to existing partner contact or domain
    - new_partner: unknown external domain, suggest creating partner
    - skip: self or already in attendees
    """
    partners = partners or []
    customer_domains = _get_customer_domains(customer)
    partner_domain_map = _get_partner_domain_map(partners)

    # Build lookup maps for existing contacts
    customer_contacts_by_email = {}
    customer_contacts_by_name = {}
    if customer:
        for c in customer.contacts:
            if c.email:
                customer_contacts_by_email[c.email.lower()] = c
            customer_contacts_by_name[c.name.lower()] = c

    partner_contacts_by_email = {}
    all_partner_contacts = PartnerContact.query.all()
    for c in all_partner_contacts:
        if c.email:
            partner_contacts_by_email[c.email.lower()] = c

    sellers_by_alias = {}
    sellers_by_name = {}
    for s in Seller.query.all():
        if s.alias:
            sellers_by_alias[s.alias.lower()] = s
        sellers_by_name[s.name.lower()] = s

    ses_by_alias = {}
    ses_by_name = {}
    for se in SolutionEngineer.query.all():
        if se.alias:
            ses_by_alias[se.alias.lower()] = se
        ses_by_name[se.name.lower()] = se

    result = []
    seen_emails = set()
    new_partner_domains = {}  # domain -> list of attendee indices

    for att in attendees:
        name = (att.get("name") or "").strip()
        email = (att.get("email") or "").strip().lower()
        title = (att.get("title") or "").strip()
        if not name and not email:
            continue
        if email in seen_emails:
            continue
        seen_emails.add(email)

        domain = email.split("@")[1] if "@" in email else ""
        alias = email.split("@")[0] if "@" in email else ""

        entry = {
            "name": name,
            "email": email or None,
            "title": title or None,
            "category": "unknown",
            "checked": True,
            "ref_type": None,   # attendee type for NoteAttendee
            "ref_id": None,     # FK id
            "partner_id": None, # for partner_contact category
            "partner_name": None,
            "is_new_contact": True,
            "has_updates": False,
            "updates": [],
            "existing_title": None,
            "new_partner_domain": None,
        }

        # Microsoft employees -> match to seller or SE
        if domain == "microsoft.com":
            # Try alias match (exact and without dots for vanity emails)
            alias_nodots = alias.replace(".", "")
            seller = (sellers_by_alias.get(alias)
                      or sellers_by_alias.get(alias_nodots))
            se = (ses_by_alias.get(alias)
                  or ses_by_alias.get(alias_nodots))
            # Fallback: match by name
            if not seller and not se and name:
                seller = sellers_by_name.get(name.lower())
            if not seller and not se and name:
                se = ses_by_name.get(name.lower())
            if seller:
                entry["category"] = "microsoft"
                entry["ref_type"] = "seller"
                entry["ref_id"] = seller.id
                entry["is_new_contact"] = False
                entry["name"] = seller.name
            elif se:
                entry["category"] = "microsoft"
                entry["ref_type"] = "se"
                entry["ref_id"] = se.id
                entry["is_new_contact"] = False
                entry["name"] = se.name
            else:
                entry["category"] = "microsoft"
                entry["ref_type"] = "external"
                entry["is_new_contact"] = True
                entry["checked"] = True  # Default checked - save as external attendee
            result.append(entry)
            continue

        # Check existing customer contacts
        existing_cc = customer_contacts_by_email.get(email)
        if not existing_cc and name:
            existing_cc = customer_contacts_by_name.get(name.lower())
        if existing_cc:
            entry["category"] = "customer_contact"
            entry["ref_type"] = "customer_contact"
            entry["ref_id"] = existing_cc.id
            entry["is_new_contact"] = False
            entry["name"] = existing_cc.name
            entry["existing_title"] = existing_cc.title
            # Check for title update
            title = (att.get("title") or "").strip()
            if title and not existing_cc.title:
                entry["has_updates"] = True
                entry["updates"].append(f"Add title: {title}")
                entry["title"] = title
            elif title and existing_cc.title and title.lower() != existing_cc.title.lower():
                entry["has_updates"] = True
                entry["updates"].append(f"Update title: {existing_cc.title} -> {title}")
                entry["title"] = title
            result.append(entry)
            continue

        # Check existing partner contacts
        existing_pc = partner_contacts_by_email.get(email)
        if existing_pc:
            entry["category"] = "partner_contact"
            entry["ref_type"] = "partner_contact"
            entry["ref_id"] = existing_pc.id
            entry["partner_id"] = existing_pc.partner_id
            entry["partner_name"] = existing_pc.partner.name if existing_pc.partner else None
            entry["is_new_contact"] = False
            entry["name"] = existing_pc.name
            entry["existing_title"] = existing_pc.title
            # Check for title update
            title = (att.get("title") or "").strip()
            if title and not existing_pc.title:
                entry["has_updates"] = True
                entry["updates"].append(f"Add title: {title}")
                entry["title"] = title
            elif title and existing_pc.title and title.lower() != existing_pc.title.lower():
                entry["has_updates"] = True
                entry["existing_title"] = existing_pc.title
                entry["updates"].append(f"Update title: {existing_pc.title} -> {title}")
                entry["title"] = title
            result.append(entry)
            continue

        # Match domain to customer (exact then fuzzy)
        if domain in customer_domains or _fuzzy_match_customer_domain(
            domain, customer_domains
        ):
            entry["category"] = "customer_contact"
            entry["ref_type"] = "customer_contact"
            entry["is_new_contact"] = True
            result.append(entry)
            continue

        # Match domain to known partner (exact then fuzzy)
        matched_partner = partner_domain_map.get(domain)
        if not matched_partner:
            matched_partner = _fuzzy_match_domain(domain, partner_domain_map)
        if matched_partner:
            entry["category"] = "partner_contact"
            entry["partner_id"] = matched_partner.id
            entry["partner_name"] = matched_partner.name
            entry["is_new_contact"] = True
            result.append(entry)
            continue

        # Unknown external domain -> suggest new partner
        if domain:
            entry["category"] = "new_partner"
            entry["new_partner_domain"] = domain
            if domain not in new_partner_domains:
                new_partner_domains[domain] = []
            new_partner_domains[domain].append(len(result))
            result.append(entry)
            continue

        # Fallback
        entry["category"] = "unknown"
        entry["checked"] = False
        result.append(entry)

    # Group related new_partner domains (e.g., simform.com + simformsolutions.com)
    if len(new_partner_domains) > 1:
        _merge_related_domains(new_partner_domains, result)

    return result


def _merge_related_domains(
    domain_groups: Dict[str, list], attendees: List[Dict]
) -> None:
    """Merge new_partner domains that share a common base name.

    For example, simform.com and simformsolutions.com both have base 'simform'.
    Groups them under the shorter domain.
    """
    domains = sorted(domain_groups.keys())
    merged = {}  # domain -> canonical domain

    for i, d1 in enumerate(domains):
        if d1 in merged:
            continue
        base1 = d1.split(".")[0]
        if len(base1) < 3:
            continue  # Skip very short bases to avoid false positives
        for d2 in domains[i + 1:]:
            if d2 in merged:
                continue
            base2 = d2.split(".")[0]
            # Check if one base contains the other
            if base1 in base2 or base2 in base1:
                # Use the shorter domain as canonical
                canonical = d1 if len(d1) <= len(d2) else d2
                other = d2 if canonical == d1 else d1
                merged[other] = canonical

    # Update attendee entries to point to canonical domain
    for att in attendees:
        if att.get("category") == "new_partner" and att.get("new_partner_domain") in merged:
            att["new_partner_domain"] = merged[att["new_partner_domain"]]
