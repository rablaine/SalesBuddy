"""Shared literal search text for locally stored milestone data."""

from app.models import Milestone


def get_milestone_search_text(milestone: Milestone) -> str:
    """Include every stored milestone field and its related display names."""
    values = [
        str(value)
        for column in Milestone.__table__.columns
        if (value := getattr(milestone, column.name)) is not None
    ]
    if milestone.customer:
        customer = milestone.customer
        values.extend([customer.name, customer.get_display_name()])
        if customer.seller:
            values.append(customer.seller.name)
        if customer.territory:
            values.append(customer.territory.name)
    if milestone.opportunity:
        values.append(milestone.opportunity.name)
    if milestone.due_date:
        values.append(milestone.due_date.strftime('%b %d, %Y'))
    return ' '.join(value for value in values if value).lower()
