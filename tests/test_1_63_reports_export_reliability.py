from datetime import datetime
from pathlib import Path
import zipfile

import pandas as pd
from pypdf import PdfReader

from exports.common import EXPORT_COLUMNS, timestamped_export_filename
from exports.csv_export import export_to_csv
from exports.excel_export import export_to_excel
from exports.pdf_export import export_to_pdf, _column_widths


def _invoice():
    return {
        "id": 1, "invoice_number": "INV-1", "vendor_name": "Vendor", "customer_name": "Customer",
        "plate_number": None, "invoice_date": "2026-09-01", "date_uploaded": "2026-09-17",
        "due_date": None, "original_filename": "invoice.pdf", "category": "Others", "subtotal": 100.0,
        "vat_exempt_sales": 0.0, "zero_rated_sales": 0.0, "tax_amount": 12.0, "withholding_tax": 2.0,
        "current_charges_total": None, "previous_balance": None, "total_amount": 110.0, "currency": "PHP",
        "line_items": [{"description": "SHOULD NOT EXPORT", "quantity": 1, "unit_price": 100, "amount": 100}],
        "locked": False,
    }


def test_timestamped_filename():
    got = timestamped_export_filename("invoices_export", "xlsx", datetime(2026, 9, 17, 9, 5, 7))
    assert got == "invoices_export_20260917_090507.xlsx"


def test_excel_has_no_line_items_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr("exports.excel_export.settings.EXPORT_DIR", str(tmp_path))
    path = export_to_excel([_invoice()], filename="test.xlsx")[0]
    with zipfile.ZipFile(path) as z:
        workbook_xml = z.read("xl/workbook.xml").decode("utf-8")
    assert "Line Items" not in workbook_xml
    assert "Invoices" in workbook_xml


def test_csv_has_only_invoice_level_file(tmp_path, monkeypatch):
    monkeypatch.setattr("exports.csv_export.settings.EXPORT_DIR", str(tmp_path))
    paths = export_to_csv([_invoice()], filename="test.csv")
    assert len(paths) == 1
    text = Path(paths[0]).read_text()
    assert "SHOULD NOT EXPORT" not in text
    assert "Vatable Sales" in text


def test_pdf_fits_all_columns_and_has_no_line_items(tmp_path, monkeypatch):
    monkeypatch.setattr("exports.pdf_export.settings.EXPORT_DIR", str(tmp_path))
    path = export_to_pdf([_invoice()], filename="test.pdf")[0]
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Invoice Report" in text
    assert "Withholding" in text
    assert "SHOULD NOT EXPORT" not in text
    assert "Items Purchased" not in text
    # Width helper always distributes exactly the available printable width.
    assert abs(sum(_column_widths(1000)) - 1000) < 1e-6
