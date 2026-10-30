"""Client-approved invoice field visibility policy.

Unrequested metadata stays in extraction, SQLite and internal validation, but is
not displayed in client-facing invoice details or export reports.
"""
from __future__ import annotations


INTERNAL_ONLY_INVOICE_FIELDS = frozenset({
    "due_date", "tax_rate", "payment_terms", "customer_contact",
    "current_charges_total", "previous_balance",
})

# The approved document fields in History. The upload filename is shown as
# the detail heading rather than duplicated as a labeled detail field.
HISTORY_APPROVED_DOCUMENT_FIELDS = (
    ("Vendor", "vendor_name"),
    ("Vendor Address", "vendor_address"),
    ("Vendor TIN", "vendor_tax_id"),
    ("Customer", "customer_name"),
    ("Customer Address", "customer_address"),
    ("Customer TIN", "customer_tax_id"),
    ("Plate #", "plate_number"),
    ("Invoice #", "invoice_number"),
    ("Invoice Date", "invoice_date"),
    ("Date Uploaded", "date_uploaded"),
    ("Category", "category"),
)


# Optional approved entries remain omitted when the invoice did not provide
# them, as in earlier builds. All other approved labels remain in the panel.
HISTORY_OPTIONAL_DOCUMENT_KEYS = frozenset({
    "customer_name", "customer_address", "customer_tax_id", "plate_number",
})


def approved_history_document_rows(invoice: dict) -> list[tuple[str, str, str]]:
    """Build the exact client-approved left-column fields in display order."""
    rows = []
    for label, key in HISTORY_APPROVED_DOCUMENT_FIELDS:
        if key == "date_uploaded":
            value = invoice.get("date_uploaded") or str(invoice.get("created_at") or "")[:10]
        else:
            value = invoice.get(key)
        text = str(value).strip() if value is not None else ""
        if key in HISTORY_OPTIONAL_DOCUMENT_KEYS and text in {"", "-", "—"}:
            continue
        rows.append((label, key, text or "-"))
    return rows


def history_invoice_title(invoice: dict) -> str:
    """Use the uploaded filename as the heading, with a safe fallback."""
    filename = str(invoice.get("original_filename") or "").strip()
    if filename:
        return filename
    invoice_number = str(invoice.get("invoice_number") or "").strip()
    if invoice_number:
        return f"Invoice {invoice_number}"
    return f"Invoice ID {invoice.get('id') or 'unknown'}"

# Additional client-facing fields displayed below the main financial amounts
# on the History detail page. Keep this explicit to avoid leaking future
# internal fields merely because they happen to be present in the database.
HISTORY_SECONDARY_FIELDS = (("Currency", "currency"),)


def visible_field_evidence(evidence: dict | None) -> dict:
    """Suppress verification notices for metadata that the UI does not show.

    Leave saved evidence untouched: downstream confidence and validation
    still have access to all original data.
    """
    return {
        key: value for key, value in (evidence or {}).items()
        if str(key).split(".", 1)[0] not in INTERNAL_ONLY_INVOICE_FIELDS
    }


def visible_validation_issues(issues: list[str] | None) -> list[str]:
    """Keep internal-only metadata *validation notices* out of the UI.

    Financial and invoice-date discrepancies continue to be displayed. Only
    messages whose subject is exclusively one of the hidden optional fields
    are removed from client-facing summaries. Stored warnings stay intact.
    """
    public = []
    for issue in issues or []:
        value = str(issue)
        words = value.lstrip().split(None, 1)
        subject = words[0].rstrip(":") if words else ""
        if subject not in INTERNAL_ONLY_INVOICE_FIELDS:
            public.append(value)
    return public
