"""Shared row/column definition for invoice exports.

Used by exports/excel_export.py, exports/csv_export.py, and
exports/pdf_export.py so all three formats show the same set of columns:
every invoice field EXCEPT "Status" and "Locked" (see ui/pages/Reports.py —
those two are working-state flags for the review workflow, not invoice
data, and exporting is gated on every invoice already being locked, so a
"Locked" column in the export itself would be redundant).
"""

EXPORT_COLUMNS = [
    "ID", "Invoice #", "Vendor", "Customer", "Date", "Due Date", "Filename",
    "Category", "Net Amount", "VAT", "Discount", "Total Amount Due", "Currency",
]


def invoice_export_row(inv: dict) -> dict:
    """Builds one export row from an invoice dict (see
    services.invoice_service._orm_to_dict for the source shape).
    Keys match EXPORT_COLUMNS exactly, in order."""
    return {
        "ID": inv.get("id"),
        "Invoice #": inv.get("invoice_number"),
        "Vendor": inv.get("vendor_name"),
        "Customer": inv.get("customer_name") or "-",
        "Date": inv.get("invoice_date") or "-",
        "Due Date": inv.get("due_date") or "-",
        "Filename": inv.get("original_filename") or "-",
        "Category": inv.get("category") or "-",
        "Net Amount": inv.get("subtotal") or 0,
        "VAT": inv.get("tax_amount") or 0,
        "Discount": inv.get("discount") or 0,
        "Total Amount Due": inv.get("total_amount") or 0,
        "Currency": inv.get("currency") or "-",
    }
