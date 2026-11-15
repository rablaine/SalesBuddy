"""Provision local milestone records from metadata already read by another tool."""

import math
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.models import Customer, Milestone, db
from app.services.msx_api import build_milestone_url

TEXT_FIELDS = {
    "name": ("title", 500),
    "number": ("milestone_number", 50),
    "status": ("msx_status", 50),
    "opportunity_name": ("opportunity_name", 500),
    "workload": ("workload", 200),
}
MAX_INTEGER = 2**63 - 1


def _validate_payload(data: dict) -> tuple[int, int, str, dict]:
    """Validate identifiers and optional metadata without imposing MSX business rules."""
    for field in ("customer_id", "tpid"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_INTEGER:
            raise ValueError(f"{field} must be a positive JSON integer")

    raw_guid = data.get("msx_milestone_id")
    if not isinstance(raw_guid, str):
        raise ValueError("msx_milestone_id must be a valid GUID")
    try:
        guid = UUID(raw_guid.strip().lower())
    except ValueError as exc:
        raise ValueError("msx_milestone_id must be a valid GUID") from exc
    if not guid.int:
        raise ValueError("msx_milestone_id must not be the nil GUID")

    metadata = {}
    for field, (column, max_length) in TEXT_FIELDS.items():
        value = data.get(field)
        if value is not None:
            if not isinstance(value, str) or len(value) > max_length:
                raise ValueError(f"{field} must be text of at most {max_length} characters or null")
            value = value.strip() or None
        metadata[column] = value

    due_date = data.get("due_date")
    if due_date is not None:
        if not isinstance(due_date, str):
            raise ValueError("due_date must be an ISO 8601 date or datetime, or null")
        try:
            parsed = datetime.fromisoformat(due_date)
            due_date = (
                parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )
        except (ValueError, OverflowError) as exc:
            raise ValueError("due_date must be an ISO 8601 date or datetime, or null") from exc
    metadata["due_date"] = due_date

    usage = data.get("monthly_usage")
    if usage is not None:
        if isinstance(usage, bool) or not isinstance(usage, (int, float)):
            raise ValueError("monthly_usage must be a finite number or null")
        try:
            usage = float(usage)
        except OverflowError as exc:
            raise ValueError("monthly_usage must be a finite number or null") from exc
        if not math.isfinite(usage):
            raise ValueError("monthly_usage must be a finite number or null")
    metadata["monthly_usage"] = usage
    return data["customer_id"], data["tpid"], str(guid), metadata


def _find_cached_milestones(guid: str) -> list[Milestone]:
    """Match canonical GUIDs and legacy case, brace, URN, or compact spellings."""
    normalized = db.func.lower(db.func.trim(Milestone.msx_milestone_id))
    for part in ("urn:uuid:", "{", "}", "-"):
        normalized = db.func.replace(normalized, part, "")
    return Milestone.query.filter(normalized == UUID(guid).hex).all()


def _cache_response(
    matches: list[Milestone], customer_id: int, tpid: int, guid: str, created: bool = False,
) -> tuple[dict, int]:
    """Return a local identity only when it belongs unambiguously to this customer."""
    if len(matches) != 1 or matches[0].customer_id != customer_id:
        return {
            "success": False,
            "error": "Milestone is already cached for a different or missing customer, "
                     "or has multiple local records",
            "retryable": False,
        }, 409
    return {
        "success": True,
        "local_milestone_id": matches[0].id,
        "msx_milestone_id": guid,
        "customer_id": customer_id,
        "tpid": tpid,
        "created": created,
    }, 201 if created else 200


def cache_milestone(data: dict) -> tuple[dict, int]:
    """Validate customer identity and commit a new record, or reuse without mutation."""
    customer_id, tpid, guid, metadata = _validate_payload(data)
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return {"success": False, "error": "Customer not found", "retryable": False}, 404
    if customer.tpid != tpid:
        return {
            "success": False, "error": "customer_id does not match tpid", "retryable": False,
        }, 409

    matches = _find_cached_milestones(guid)
    if matches:
        return _cache_response(matches, customer_id, tpid, guid)

    url = build_milestone_url(guid)
    if Milestone.query.filter_by(url=url).first() is not None:
        return {
            "success": False,
            "error": "Milestone URL is already cached without a matching MSX GUID",
            "retryable": False,
        }, 409

    milestone = Milestone(
        msx_milestone_id=guid,
        customer_id=customer_id,
        url=url,
        **metadata,
    )
    db.session.add(milestone)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # A concurrent request may have committed the same canonical GUID first.
        matches = _find_cached_milestones(guid)
        if not matches:
            raise
        return _cache_response(matches, customer_id, tpid, guid)
    return _cache_response([milestone], customer_id, tpid, guid, created=True)
