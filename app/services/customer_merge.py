"""
Customer merge service for M&A handling.

Merges all data from a source customer into a destination customer,
then deletes the source. Handles unique constraints, M2M deduplication,
and account context preservation.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.models import (
    db, Customer, CustomerContact, Note, Engagement, Milestone, Opportunity,
    CustomerRevenueData, ProductRevenueData, RevenueAnalysis,
    MarketingSummary, MarketingInteraction, MarketingContact, U2CSnapshotItem,
    customers_verticals, customers_csams,
)

logger = logging.getLogger(__name__)


def get_merge_preview(source_id: int, dest_id: int) -> dict:
    """Preview what will be migrated when merging source into dest.

    Args:
        source_id: ID of the customer being absorbed.
        dest_id: ID of the surviving customer.

    Returns:
        Dict with counts of records that will be moved, plus customer info.
    """
    source = Customer.query.get(source_id)
    dest = Customer.query.get(dest_id)
    if not source or not dest:
        return {"error": "Customer not found"}
    if source_id == dest_id:
        return {"error": "Cannot merge a customer into itself"}

    return {
        "source": {
            "id": source.id,
            "name": source.get_display_name(),
            "tpid": source.tpid,
        },
        "destination": {
            "id": dest.id,
            "name": dest.get_display_name(),
            "tpid": dest.tpid,
        },
        "counts": _count_linked_records(source_id),
    }


def _count_linked_records(customer_id: int) -> dict:
    """Count all records linked to a customer."""
    return {
        "notes": Note.query.filter_by(customer_id=customer_id).count(),
        "engagements": Engagement.query.filter_by(customer_id=customer_id).count(),
        "milestones": Milestone.query.filter_by(customer_id=customer_id).count(),
        "opportunities": Opportunity.query.filter_by(customer_id=customer_id).count(),
        "contacts": CustomerContact.query.filter_by(customer_id=customer_id).count(),
        "revenue_data": CustomerRevenueData.query.filter_by(customer_id=customer_id).count(),
        "product_revenue": ProductRevenueData.query.filter_by(customer_id=customer_id).count(),
        "revenue_analyses": RevenueAnalysis.query.filter_by(customer_id=customer_id).count(),
        "marketing_summary": MarketingSummary.query.filter_by(customer_id=customer_id).count(),
        "marketing_interactions": MarketingInteraction.query.filter_by(customer_id=customer_id).count(),
        "marketing_contacts": MarketingContact.query.filter_by(customer_id=customer_id).count(),
        "u2c_items": U2CSnapshotItem.query.filter_by(customer_id=customer_id).count(),
    }


def merge_customer(source_id: int, dest_id: int) -> dict:
    """Merge all data from source customer into destination customer.

    Moves all linked records (notes, engagements, milestones, etc.) from
    source to destination, handling unique constraints and M2M dedup.
    Deletes the source customer after migration.

    Args:
        source_id: ID of the customer being absorbed (will be deleted).
        dest_id: ID of the surviving customer (receives all data).

    Returns:
        Dict with counts of migrated records and status.

    Raises:
        ValueError: If source or dest not found, or IDs are equal.
    """
    source = Customer.query.get(source_id)
    dest = Customer.query.get(dest_id)

    if not source:
        raise ValueError(f"Source customer {source_id} not found")
    if not dest:
        raise ValueError(f"Destination customer {dest_id} not found")
    if source_id == dest_id:
        raise ValueError("Cannot merge a customer into itself")

    logger.info(
        "Merging customer '%s' (ID %d, TPID %s) into '%s' (ID %d, TPID %s)",
        source.name, source.id, source.tpid,
        dest.name, dest.id, dest.tpid,
    )

    migrated = {}

    # 1. Move simple FK records (bulk update customer_id)
    migrated["notes"] = Note.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["engagements"] = Engagement.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["milestones"] = Milestone.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["opportunities"] = Opportunity.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["revenue_data"] = CustomerRevenueData.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["product_revenue"] = ProductRevenueData.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["revenue_analyses"] = RevenueAnalysis.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})
    migrated["u2c_items"] = U2CSnapshotItem.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})

    # 2. Contacts - move, deduping by email
    migrated["contacts"] = _merge_contacts(source_id, dest_id)

    # 3. Marketing summary - unique constraint on customer_id, keep dest's
    migrated["marketing_summary"] = _merge_marketing_summary(source_id, dest_id)

    # 4. Marketing interactions - move all (unique on composite_key, not customer_id)
    migrated["marketing_interactions"] = MarketingInteraction.query.filter_by(
        customer_id=source_id).update({"customer_id": dest_id})

    # 5. Marketing contacts - move, deduping by contact_guid
    migrated["marketing_contacts"] = _merge_marketing_contacts(source_id, dest_id)

    # 6. M2M: verticals (union, skip dupes)
    migrated["verticals"] = _merge_m2m(
        customers_verticals, "customer_id", "vertical_id", source_id, dest_id)

    # 7. M2M: CSAMs (union, skip dupes)
    migrated["csams"] = _merge_m2m(
        customers_csams, "customer_id", "csam_id", source_id, dest_id)

    # 8. Preserve account_context from source
    if source.account_context:
        separator = f"\n\n--- Merged from {source.get_display_name()} (TPID {source.tpid}) ---\n\n"
        if dest.account_context:
            dest.account_context = dest.account_context + separator + source.account_context
        else:
            dest.account_context = source.account_context

    # 9. Carry over nickname if dest doesn't have one
    if source.nickname and not dest.nickname:
        dest.nickname = source.nickname

    # 10. Delete source customer
    source_name = source.get_display_name()
    source_tpid = source.tpid
    db.session.delete(source)
    db.session.commit()

    logger.info(
        "Merge complete: '%s' (TPID %s) merged into '%s' (TPID %s). Migrated: %s",
        source_name, source_tpid, dest.name, dest.tpid, migrated,
    )

    return {
        "success": True,
        "source_name": source_name,
        "source_tpid": source_tpid,
        "dest_name": dest.get_display_name(),
        "dest_id": dest.id,
        "migrated": migrated,
    }


def _merge_contacts(source_id: int, dest_id: int) -> int:
    """Move contacts from source to dest, skipping duplicates by email."""
    dest_emails = {
        c.email.lower()
        for c in CustomerContact.query.filter_by(customer_id=dest_id).all()
        if c.email
    }
    moved = 0
    for contact in CustomerContact.query.filter_by(customer_id=source_id).all():
        if contact.email and contact.email.lower() in dest_emails:
            db.session.delete(contact)  # Duplicate - remove
        else:
            contact.customer_id = dest_id
            if contact.email:
                dest_emails.add(contact.email.lower())
            moved += 1
    return moved


def _merge_marketing_summary(source_id: int, dest_id: int) -> int:
    """Handle marketing summary merge (unique constraint on customer_id)."""
    source_summary = MarketingSummary.query.filter_by(customer_id=source_id).first()
    if not source_summary:
        return 0
    dest_summary = MarketingSummary.query.filter_by(customer_id=dest_id).first()
    if dest_summary:
        # Keep destination's summary (more current), delete source's
        db.session.delete(source_summary)
    else:
        # Move source's summary to dest
        source_summary.customer_id = dest_id
    return 1


def _merge_marketing_contacts(source_id: int, dest_id: int) -> int:
    """Move marketing contacts, deduping by contact_guid."""
    dest_guids = {
        c.contact_guid
        for c in MarketingContact.query.filter_by(customer_id=dest_id).all()
    }
    moved = 0
    for contact in MarketingContact.query.filter_by(customer_id=source_id).all():
        if contact.contact_guid in dest_guids:
            db.session.delete(contact)
        else:
            contact.customer_id = dest_id
            dest_guids.add(contact.contact_guid)
            moved += 1
    return moved


def _merge_m2m(table, customer_col: str, other_col: str,
               source_id: int, dest_id: int) -> int:
    """Merge M2M association table rows (union, skip dupes)."""
    # Get existing dest associations
    dest_rows = db.session.execute(
        table.select().where(table.c[customer_col] == dest_id)
    ).fetchall()
    dest_other_ids = {row[1] for row in dest_rows}  # column index 1 = other_id

    # Get source associations
    source_rows = db.session.execute(
        table.select().where(table.c[customer_col] == source_id)
    ).fetchall()

    moved = 0
    for row in source_rows:
        other_id = row[1]
        if other_id not in dest_other_ids:
            db.session.execute(
                table.insert().values(**{customer_col: dest_id, other_col: other_id})
            )
            moved += 1

    # Delete all source associations
    db.session.execute(
        table.delete().where(table.c[customer_col] == source_id)
    )
    return moved
