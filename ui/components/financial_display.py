"""Presentation-only formatting for invoice financial values.

An unextracted amount (None) is *not* a confirmed zero in the database.
History can display 0.00 for readability without changing that distinction.
"""
from __future__ import annotations


# These are the six monetary fields displayed in the History detail panel.
HISTORY_FINANCIAL_FIELDS = (
    ("Vatable Sales", "subtotal"),
    ("VAT-Exempt Sales", "vat_exempt_sales"),
    ("Zero-Rated Sales", "zero_rated_sales"),
    ("VAT", "tax_amount"),
    ("Withholding Tax", "withholding_tax"),
    ("Total Amount Due", "total_amount"),
)


def format_history_amount(value: float | None, currency: str | None = None) -> str:
    """Format a History financial value, using 0.00 for an absent value.

    Only the *display* uses 0.00. Callers must not persist this placeholder
    as an amount, because missing and an explicit numeric zero are different.
    """
    if value is None:
        return "0.00"
    return f"{value:,.2f} {currency or ''}".strip()


def has_unextracted_amount(detail: dict) -> bool:
    """Whether the panel needs a single unobtrusive placeholder explanation."""
    return any(detail.get(key) is None for _, key in HISTORY_FINANCIAL_FIELDS)
