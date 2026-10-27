"""Common calendar window and product breakdowns for monthly ACR discovery."""

from datetime import date
from math import fsum

ACR_WINDOW_MONTHS = 3


def complete_acr_months(reference: date | None = None) -> tuple[date, ...]:
    """Return the last three complete calendar months, excluding the current month."""
    reference = reference or date.today()
    index = reference.year * 12 + reference.month - 1
    return tuple(
        date(month // 12, month % 12 + 1, 1)
        for month in range(index - ACR_WINDOW_MONTHS, index)
    )


def monthly_product_breakdown(amounts: dict[str, list[float]]) -> list[dict]:
    """Keep product credits and exact values on the same fixed divisor as category totals."""
    products = [
        {'product': product or 'Unspecified product',
         'monthly_revenue': fsum(values) / ACR_WINDOW_MONTHS}
        for product, values in amounts.items() if fsum(values) != 0
    ]
    return sorted(products, key=lambda row: (-row['monthly_revenue'], row['product'].casefold()))
