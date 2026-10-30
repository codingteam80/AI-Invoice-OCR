"""Aggregation queries used by the dashboard/reports UI."""
from collections import defaultdict
from services.invoice_service import list_invoices
from services.currency_totals import invoices_for_currency


def spend_by_vendor(limit: int = 500, currency: str | None = None) -> dict:
    invoices = list_invoices(limit=limit)
    if currency is not None:
        invoices = invoices_for_currency(invoices, currency)
    totals = defaultdict(float)
    for inv in invoices:
        totals[inv["vendor_name"]] += inv.get("total_amount") or 0
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def spend_by_month(limit: int = 500, date_basis: str = "invoice", currency: str | None = None) -> dict:
    """Group by the actual invoice month (or explicitly selected upload month).

    A single upload day often contains old invoices from several years. The
    previous implementation grouped only by date_uploaded and therefore
    created one point for the entire batch, which a line chart cannot draw.
    """
    invoices = list_invoices(limit=limit)
    if currency is not None:
        invoices = invoices_for_currency(invoices, currency)
    totals = defaultdict(float)
    for inv in invoices:
        if date_basis == "uploaded":
            date_str = inv.get("date_uploaded") or (inv.get("created_at") or "")[:10]
        else:
            date_str = inv.get("invoice_date")
        if not date_str:
            continue
        month_key = str(date_str)[:7]
        if not __import__('re').fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month_key):
            continue
        totals[month_key] += inv.get("total_amount") or 0
    return dict(sorted(totals.items()))


def spend_by_category(limit: int = 500, currency: str | None = None) -> dict:
    invoices = list_invoices(limit=limit)
    if currency is not None:
        invoices = invoices_for_currency(invoices, currency)
    totals = defaultdict(float)
    for inv in invoices:
        cat = inv.get("category") or "Others"
        totals[cat] += inv.get("total_amount") or 0
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def status_breakdown(limit: int = 500) -> dict:
    invoices = list_invoices(limit=limit)
    counts = defaultdict(int)
    for inv in invoices:
        counts[inv.get("status", "unknown")] += 1
    return dict(counts)
