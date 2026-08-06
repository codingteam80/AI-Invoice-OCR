"""Aggregation queries used by the dashboard/reports UI."""
from collections import defaultdict
from services.invoice_service import list_invoices


def spend_by_vendor(limit: int = 500) -> dict:
    invoices = list_invoices(limit=limit)
    totals = defaultdict(float)
    for inv in invoices:
        totals[inv["vendor_name"]] += inv.get("total_amount") or 0
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def spend_by_month(limit: int = 500) -> dict:
    invoices = list_invoices(limit=limit)
    totals = defaultdict(float)
    for inv in invoices:
        date_str = inv.get("invoice_date")
        if not date_str:
            continue
        month_key = date_str[:7]  # YYYY-MM
        totals[month_key] += inv.get("total_amount") or 0
    return dict(sorted(totals.items()))


def spend_by_category(limit: int = 500) -> dict:
    invoices = list_invoices(limit=limit)
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
