"""Shared row/column definition for invoice exports.

All export formats use the same invoice-level columns. Review-only UI state
(Status / Locked), hidden Discount, and Items Purchased / Line Items are not
part of generated export files.
"""
from datetime import datetime

EXPORT_COLUMNS = [
    "ID", "Invoice #", "Vendor", "Customer", "Plate #", "Invoice Date", "Date Uploaded", "Due Date", "Filename",
    "Category", "Vatable Sales", "VAT-Exempt Sales", "Zero-Rated Sales", "VAT", "Withholding Tax",
    "Current Charges Total", "Previous Balance", "Total Amount Due", "Currency",
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
    row["Current Charges Total"] = sum(inv.get("current_charges_total") or 0 for inv in invoices)
    row["Previous Balance"] = sum(inv.get("previous_balance") or 0 for inv in invoices)
    row["Total Amount Due"] = sum(inv.get("total_amount") or 0 for inv in invoices)
    return row


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
        "Due Date": inv.get("due_date") or "-",
        "Filename": inv.get("original_filename") or "-",
        "Category": inv.get("category") or "-",
        "Vatable Sales": inv.get("subtotal") or 0,
        "VAT-Exempt Sales": inv.get("vat_exempt_sales") or 0,
        "Zero-Rated Sales": inv.get("zero_rated_sales") or 0,
        "VAT": inv.get("tax_amount") or 0,
        "Withholding Tax": inv.get("withholding_tax") or 0,
        "Current Charges Total": inv.get("current_charges_total") if inv.get("current_charges_total") is not None else "",
        "Previous Balance": inv.get("previous_balance") if inv.get("previous_balance") is not None else "",
        "Total Amount Due": inv.get("total_amount") or 0,
        "Currency": inv.get("currency") or "-",
    }
