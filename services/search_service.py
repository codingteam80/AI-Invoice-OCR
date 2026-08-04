"""Thin wrapper around repository search, kept separate for future full-text/AI search."""
from services.invoice_service import search_invoices


def search(keyword: str) -> list[dict]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    return search_invoices(keyword)
