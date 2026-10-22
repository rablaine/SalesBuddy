"""HVA-first inventory over the existing complete owned-task sync."""

from datetime import date, datetime, time, timedelta, timezone
from math import ceil

from sqlalchemy import case, func, or_
from sqlalchemy.orm import joinedload

from app.models import Customer, Milestone, MsxTask, SyncStatus
from app.services.msx_api import HVA_TASK_CATEGORIES, build_task_url

STATUS_LABELS = {0: "Open", 1: "Completed", 2: "Canceled"}
STATUS_FILTERS = {"open": 0, "completed": 1, "canceled": 2}
DATE_FIELDS = {
    "created": MsxTask.msx_created_on,
    "due": MsxTask.due_date,
    "completed": MsxTask.actual_end,
}


def _iso(value: datetime | None) -> str | None:
    """Serialize cached UTC dates without substituting the local import date."""
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(
        timezone.utc,
    ).isoformat() if value else None


def _serialize_hva(task: MsxTask) -> dict:
    """Serialize shared cached activity details for the inventory and note editor."""
    milestone = task.milestone
    customer = milestone.customer if milestone else None
    return {
        "id": task.id, "msx_task_id": task.msx_task_id, "subject": task.subject,
        "description": task.description, "url": build_task_url(task.msx_task_id),
        "category": task.task_category_name or "Unknown category",
        "category_code": task.task_category,
        "status": STATUS_LABELS.get(task.statecode, "Unknown"),
        "statecode": task.statecode, "created": _iso(task.msx_created_on),
        "due": _iso(task.due_date), "completed": _iso(task.actual_end),
        "customer_id": customer.id if customer else None,
        "customer": customer.name if customer else None,
        "milestone_id": milestone.id if milestone else None,
        "related": milestone.display_text if milestone else task.msx_regarding_name,
    }


def get_milestone_hvas(msx_ids: list[str]) -> dict[str, list[dict]]:
    """Read all cached HVAs on selected milestones, including historical owners."""
    result = {msx_id.lower(): [] for msx_id in msx_ids}
    if not result:
        return result
    tasks = MsxTask.query.join(Milestone).filter(
        func.lower(Milestone.msx_milestone_id).in_(result),
        MsxTask.is_hva.is_(True), MsxTask.task_category.in_(HVA_TASK_CATEGORIES),
    ).options(
        joinedload(MsxTask.milestone).joinedload(Milestone.customer),
    ).order_by(MsxTask.msx_created_on.desc().nullslast(), MsxTask.id.desc()).all()
    for task in tasks:
        row = _serialize_hva(task)
        row["duration_minutes"] = task.duration_minutes
        result[task.milestone.msx_milestone_id.lower()].append(row)
    return result


def get_hva_inventory(
    search: str = "", status: str = "", customer_id: int | None = None,
    category: int | None = None, date_field: str = "created",
    date_from: str | None = None, date_to: str | None = None,
    sort: str = "created", direction: str = "desc", page: int = 1, per_page: int = 50,
) -> dict:
    """Read owned HVA tasks with global sorting, full-scope counts and bounded pagination."""
    if status not in ("", "unknown", *STATUS_FILTERS):
        raise ValueError("Choose a valid HVA status.")
    if date_field not in DATE_FIELDS:
        raise ValueError("Choose Created, Due or Completed for the date window.")
    if (isinstance(page, bool) or not isinstance(page, int) or page < 1
            or isinstance(per_page, bool) or not isinstance(per_page, int)
            or not 1 <= per_page <= 100):
        raise ValueError("Page must be positive and page size must be between 1 and 100.")
    if customer_id is not None and (
        isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id < 1
    ):
        raise ValueError("Choose a valid customer.")
    if category is not None and (
        isinstance(category, bool) or not isinstance(category, int)
        or category not in HVA_TASK_CATEGORIES
    ):
        raise ValueError("Choose an HVA category.")
    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
        end_exclusive = end + timedelta(days=1) if end else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Use valid YYYY-MM-DD dates for the date window.") from exc
    if start and end and start > end:
        raise ValueError("The date window must end on or after its start.")

    query = MsxTask.query.outerjoin(Milestone).outerjoin(Customer).filter(
        MsxTask.is_hva.is_(True), MsxTask.is_current_owner.is_(True),
        MsxTask.task_category.in_(HVA_TASK_CATEGORIES),
    )
    if customer_id is not None:
        query = query.filter(Milestone.customer_id == customer_id)
    if category is not None:
        query = query.filter(MsxTask.task_category == category)
    if status == "unknown":
        query = query.filter(or_(MsxTask.statecode.is_(None), MsxTask.statecode.notin_([0, 1, 2])))
    elif status:
        query = query.filter(MsxTask.statecode == STATUS_FILTERS[status])
    search = search.strip()
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(
            MsxTask.subject.ilike(pattern), MsxTask.description.ilike(pattern),
            MsxTask.task_category_name.ilike(pattern), Customer.name.ilike(pattern),
            Milestone.title.ilike(pattern), Milestone.milestone_number.ilike(pattern),
            MsxTask.msx_regarding_name.ilike(pattern),
        ))
    if start:
        query = query.filter(DATE_FIELDS[date_field] >= datetime.combine(start, time.min))
    if end_exclusive:
        query = query.filter(DATE_FIELDS[date_field] < datetime.combine(end_exclusive, time.min))

    status_expression = case(
        *[(MsxTask.statecode == code, label) for code, label in STATUS_LABELS.items()],
        else_="Unknown",
    )
    sort_columns = {
        **DATE_FIELDS, "subject": func.lower(MsxTask.subject),
        "status": status_expression, "category": func.lower(MsxTask.task_category_name),
        "customer": func.lower(Customer.name),
        "related": func.lower(func.coalesce(
            Milestone.title, Milestone.milestone_number, MsxTask.msx_regarding_name,
        )),
    }
    if sort not in sort_columns or direction not in ("asc", "desc"):
        raise ValueError("Choose a valid sort column and direction.")
    counts = dict(query.with_entities(MsxTask.statecode, func.count(MsxTask.id))
                  .group_by(MsxTask.statecode).all())
    summary = {label.lower(): counts.get(code, 0) for code, label in STATUS_LABELS.items()}
    summary["unknown"] = sum(count for code, count in counts.items() if code not in STATUS_LABELS)
    summary["total"] = sum(counts.values())
    pages = max(1, ceil(summary["total"] / per_page))
    page = min(page, pages)
    column = sort_columns[sort]
    ordered = column.asc() if direction == "asc" else column.desc()
    tasks = query.options(
        joinedload(MsxTask.milestone).joinedload(Milestone.customer),
    ).order_by(ordered.nullslast(), MsxTask.id.desc()).offset((page - 1) * per_page).limit(
        per_page,
    ).all()
    rows = [_serialize_hva(task) for task in tasks]
    source = SyncStatus.get_status("owned_tasks")
    return {
        "rows": rows, "summary": summary, "page": page, "pages": pages, "per_page": per_page,
        "complete_inventory": source["state"] == "complete",
        "last_synced": _iso(source["completed_at"]),
    }
