"""Shared date windows, milestone highlights and customer ordering for 1:1 preparation."""

from datetime import date, datetime, time, timedelta, timezone

from app.models import Customer, Milestone, db


MILESTONE_FOCUS_OPTIONS = (
    ('all', 'All milestones'),
    ('10', 'Top 10 ACR'),
    ('15', 'Top 15 ACR'),
)


def get_quarter_milestone_highlights(
    focus: str = 'all', *, reference: date | None = None, seller_id: int | None = None,
) -> tuple[list[Milestone], datetime, datetime]:
    """Rank the existing active, on-my-team quarter-or-overdue milestone population."""
    if focus not in ('all', '10', '15'):
        raise ValueError('Choose all milestones, Top 10 ACR, or Top 15 ACR.')
    today = reference or date.today()
    start_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = datetime.combine(date(today.year, start_month, 1), time.min)
    end_month = start_month + 3
    end_year = today.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    quarter_end = datetime.combine(
        date(end_year, end_month, 1) - timedelta(days=1), time(23, 59, 59),
    )
    query = Milestone.query.filter(
        Milestone.customer_id.isnot(None),
        Milestone.on_my_team.is_(True),
        Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
        Milestone.due_date.isnot(None),
        db.or_(
            db.and_(Milestone.due_date >= quarter_start, Milestone.due_date <= quarter_end),
            Milestone.due_date < datetime.combine(today, time.min),
        ),
    ).options(db.joinedload(Milestone.customer).joinedload(Customer.seller))
    if seller_id is not None:
        query = query.filter(Milestone.customer.has(Customer.seller_id == seller_id))
    query = query.order_by(db.func.coalesce(Milestone.monthly_usage, 0).desc(), Milestone.id)
    if focus != 'all':
        query = query.limit(int(focus))
    return query.all(), quarter_start, quarter_end


def get_activity_cutoffs(
    days: int = 14, reference: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return UTC timestamp and naive local call-date cutoffs for a rolling window."""
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError('Choose a reporting window between 1 and 90 days.')
    now = reference or datetime.now(timezone.utc)
    return (
        now.astimezone(timezone.utc) - timedelta(days=days),
        now.astimezone().replace(tzinfo=None) - timedelta(days=days),
    )


def sort_customer_activity(customers: list[dict], order: str = 'recent') -> list[dict]:
    """Order customer groups and their engagements without changing their populations."""
    if order not in {'recent', 'acr', 'seller', 'territory'}:
        raise ValueError('Choose recent activity, estimated ACR, seller, or territory sorting.')
    for group in customers:
        group['estimated_acr'] = sum(eng.estimated_acr or 0 for eng in group['engagements'])
        if order == 'acr':
            group['engagements'].sort(
                key=lambda eng: (-(eng.estimated_acr or 0), eng.title.casefold()),
            )

    def key(group: dict) -> tuple:
        """Use raw values, deterministic customer ties, and missing owners last."""
        customer = group['customer']
        name = customer.name.casefold()
        if order == 'acr':
            return (-group['estimated_acr'], name)
        if order in {'seller', 'territory'}:
            owner = getattr(customer, order)
            return (owner is None, owner.name.casefold() if owner else '', name)
        dates = (
            [note.call_date for note in group['notes'] if note.call_date]
            if 'notes' in group else
            [eng.last_note_date for eng in group['engagements'] if eng.last_note_date]
        )
        latest = max(dates, default=datetime.min)
        return (
            -latest.toordinal(), -latest.hour, -latest.minute,
            -latest.second, -latest.microsecond, name,
        )

    return sorted(customers, key=key)
