"""Shared row/column definition for invoice exports.

Used by exports/excel_export.py, exports/csv_export.py, and
exports/pdf_export.py so all three formats show the same set of columns:
every invoice field EXCEPT "Status" and "Locked" (see ui/pages/Reports.py —
those two are working-state flags for the review workflow, not invoice
data, and exporting is gated on every invoice already being locked, so a
"Locked" column in the export itself would be redundant).
"""

EXPORT_COLUMNS = [
    "ID", "Invoice #", "Vendor", "Customer", "Plate #", "Date", "Due Date", "Filename",
    "Category", "Net Amount", "VAT", "Discount", "Total Amount Due", "Currency",
]


LINE_ITEM_COLUMNS = ["Invoice #", "ID", "Description", "Quantity", "Unit Price", "Total Unit Price"]


def invoice_line_items_rows(invoices: list[dict]) -> list[dict]:
    """Flat list of every line item across `invoices`, each row tagged with
    its parent invoice's # and ID so it can be cross-referenced. Invoices
    with no line items simply contribute nothing — see ui/pages/Reports.py
    ("include Items purchased for invoices that have them")."""
    rows = []
    for inv in invoices:
        for li in (inv.get("line_items") or []):
            rows.append({
                "Invoice #": inv.get("invoice_number"),
                "ID": inv.get("id"),
                "Description": li.get("description") or "-",
                "Quantity": li.get("quantity") or 0,
                "Unit Price": li.get("unit_price") or 0,
                "Total Unit Price": li.get("amount") or 0,
            })
    return rows


def invoice_totals_row(invoices: list[dict]) -> dict:
    """Bottom summary row: sums of Net Amount, VAT, Discount, and Total
    Amount Due across `invoices`, blank everywhere else. None of these sums
    are shown on the Reports page itself (only Total Amount Due is), but
    all are still included in every exported file — see ui/pages/Reports.py."""
    row = {col: "" for col in EXPORT_COLUMNS}
    row["ID"] = "TOTAL"
    row["Net Amount"] = sum(inv.get("subtotal") or 0 for inv in invoices)
    row["VAT"] = sum(inv.get("tax_amount") or 0 for inv in invoices)
    row["Discount"] = sum(inv.get("discount") or 0 for inv in invoices)
    row["Total Amount Due"] = sum(inv.get("total_amount") or 0 for inv in invoices)
    return row


def invoice_export_row(inv: dict) -> dict:
    """Builds one export row from an invoice dict (see
    services.invoice_service._orm_to_dict for the source shape).
    Keys match EXPORT_COLUMNS exactly, in order."""
    return {
        "ID": inv.get("id"),
        "Invoice #": inv.get("invoice_number"),
        "Vendor": inv.get("vendor_name"),
        "Customer": inv.get("customer_name") or "-",
        "Plate #": inv.get("plate_number") or "-",
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
