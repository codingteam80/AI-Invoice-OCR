"""Currency-isolated invoice aggregation; no implicit foreign exchange conversion.

Unknown/invalid currency is kept separate so totals never mix units.  "PHP"
and the peso symbols are treated as the same currency, not three buckets.
"""
from collections import defaultdict
from math import isfinite
from parser.currency_parser import normalize_currency, _ISO_CODES


def currency_code(value):
    code = normalize_currency(str(value or ""))
    return code if code in _ISO_CODES else "UNSPECIFIED"


def totals_by_currency(invoices, field="total_amount"):
    totals = defaultdict(float)
    for invoice in invoices:
        value = invoice.get(field)
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(amount):
            totals[currency_code(invoice.get("currency"))] += amount
    return {code: round(value, 2) for code, value in sorted(totals.items())}


def invoices_for_currency(invoices, currency):
    return [inv for inv in invoices if currency_code(inv.get("currency")) == currency]
