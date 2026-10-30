"""Shared row/column definition for invoice exports.

All export formats use the same invoice-level columns. Due Date, Tax Rate,
Payment Terms, Customer Contact, Current Charges Total, and Previous Balance
are internal-only metadata, not columns in client exports. Review-only UI state
(Status / Locked), hidden Discount, and Items Purchased / Line Items are not
part of generated export files.
"""
from datetime import datetime

EXPORT_COLUMNS = [
    "ID", "Invoice #", "Vendor", "Customer", "Plate #", "Invoice Date", "Date Uploaded", "Filename",
    "Category", "Vatable Sales", "VAT-Exempt Sales", "Zero-Rated Sales", "VAT", "Withholding Tax",
    "Total Amount Due", "Currency",
]


def timestamped_export_filename(prefix: str, extension: str, now: datetime | None = None) -> str:
    """Return a collision-resistant, human-readable export filename."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}.{extension.lstrip('.')}"


def invoice_totals_row(invoices: list[dict]) -> dict:
    """Bottom summary row for exported financial fields."""
    row = {col: "" for col in EXPORT_COLUMNS}
    row["ID"] = "TOTAL"
    row["Vatable Sales"] = sum(inv.get("subtotal") or 0 for inv in invoices)
    row["VAT-Exempt Sales"] = sum(inv.get("vat_exempt_sales") or 0 for inv in invoices)
    row["Zero-Rated Sales"] = sum(inv.get("zero_rated_sales") or 0 for inv in invoices)
    row["VAT"] = sum(inv.get("tax_amount") or 0 for inv in invoices)
    row["Withholding Tax"] = sum(inv.get("withholding_tax") or 0 for inv in invoices)
    row["Total Amount Due"] = sum(inv.get("total_amount") or 0 for inv in invoices)
    return row


def invoice_totals_rows(invoices: list[dict]) -> list[dict]:
    """One totals row per currency; never add PHP to USD or unknown currency."""
    from services.currency_totals import currency_code
    currencies = sorted({currency_code(inv.get("currency")) for inv in invoices})
    if not currencies:
        return [invoice_totals_row([])]
    rows = []
    for code in currencies:
        subset = [inv for inv in invoices if currency_code(inv.get("currency")) == code]
        row = invoice_totals_row(subset)
        row["ID"] = "TOTAL"
        row["Currency"] = code
        rows.append(row)
    return rows


def invoice_export_row(inv: dict) -> dict:
    """Build one invoice-level export row."""
    return {
        "ID": inv.get("id"),
        "Invoice #": inv.get("invoice_number"),
        "Vendor": inv.get("vendor_name"),
        "Customer": inv.get("customer_name") or "-",
        "Plate #": inv.get("plate_number") or "-",
        "Invoice Date": inv.get("invoice_date") or "-",
        "Date Uploaded": inv.get("date_uploaded") or (inv.get("created_at") or "")[:10] or "-",
        "Filename": inv.get("original_filename") or "-",
        "Category": inv.get("category") or "-",
        "Vatable Sales": inv.get("subtotal") or 0,
        "VAT-Exempt Sales": inv.get("vat_exempt_sales") or 0,
        "Zero-Rated Sales": inv.get("zero_rated_sales") or 0,
        "VAT": inv.get("tax_amount") or 0,
        "Withholding Tax": inv.get("withholding_tax") or 0,
        "Total Amount Due": inv.get("total_amount") if inv.get("total_amount") is not None else "",
        "Currency": inv.get("currency") or "-",
    }
