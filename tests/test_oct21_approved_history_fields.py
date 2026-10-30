"""Client-approved History fields, filename heading and report privacy."""

import ast
import csv
from pathlib import Path

from config.field_visibility import (
    HISTORY_APPROVED_DOCUMENT_FIELDS, HISTORY_SECONDARY_FIELDS,
    INTERNAL_ONLY_INVOICE_FIELDS, approved_history_document_rows,
    history_invoice_title, visible_field_evidence,
)
from exports.common import EXPORT_COLUMNS, invoice_export_row, invoice_totals_row
from exports.csv_export import export_to_csv
from exports.excel_export import export_to_excel
from exports.pdf_export import export_to_pdf


_UNAPPROVED = {"Customer Contact", "Current Charges", "Current Charges Total", "Previous Balance",
               "Due Date", "Tax Rate", "Payment Terms"}


def example_invoice():
    return {
        "id": 3, "invoice_number": "ISINVWA000039616110", "original_filename": "INV-39.png",
        "vendor_name": "SHOPEEPAY PHILIPPINES, INC.", "vendor_address": "Sample address",
        "vendor_tax_id": "009-336-505-00000", "customer_name": "CUSTOMER",
        "customer_address": "Sample city", "customer_tax_id": "111-222-333",
        "plate_number": "ABC 123", "invoice_date": "2026-01-31", "date_uploaded": "2026-10-21",
        "category": "Others", "currency": "PHP", "status": "processed", "locked": 0,
        "subtotal": 13.39, "vat_exempt_sales": 0.0, "zero_rated_sales": 0.0,
        "tax_amount": 1.61, "withholding_tax": 0.0, "total_amount": 15.0,
        "customer_contact": "Secret Contact", "current_charges_total": 1111.11,
        "previous_balance": 2222.22, "due_date": "2099-01-01", "tax_rate": 27.3,
        "payment_terms": "Secret Terms",
        "line_items": [
            {"description": "Current Charges", "quantity": 1, "unit_price": 11.1, "amount": 11.1},
            {"description": "Previous Balance", "quantity": 1, "unit_price": -3.0, "amount": -3.0},
        ],
    }


def test_heading_uses_filename_without_repeating_invoice_number():
    assert history_invoice_title(example_invoice()) == "INV-39.png"
    assert history_invoice_title({"invoice_number": "001"}) == "Invoice 001"
    assert history_invoice_title({"id": 3}) == "Invoice ID 3"


def test_exact_approved_detail_labels_and_plate_field():
    approved = [label for label, _ in HISTORY_APPROVED_DOCUMENT_FIELDS]
    assert approved == [
        "Vendor", "Vendor Address", "Vendor TIN", "Customer", "Customer Address",
        "Customer TIN", "Plate #", "Invoice #", "Invoice Date", "Date Uploaded", "Category",
    ]
    fields = approved_history_document_rows(example_invoice())
    assert [label for label, _key, _val in fields] == approved
    assert ("Plate #", "plate_number", "ABC 123") in fields
    assert ("Invoice #", "invoice_number", "ISINVWA000039616110") in fields
    assert not any(label in _UNAPPROVED | {"Filename"} for label, _, _ in fields)
    assert HISTORY_SECONDARY_FIELDS == (("Currency", "currency"),)


def test_optional_approved_fields_do_not_fabricate_empty_values():
    source = example_invoice()
    source.update(customer_name="", customer_address=None, customer_tax_id="—", plate_number="")
    labels = [label for label, _key, _ in approved_history_document_rows(source)]
    assert "Customer" not in labels
    assert "Customer Address" not in labels
    assert "Customer TIN" not in labels
    assert "Plate #" not in labels
    assert "Invoice #" in labels


def test_unapproved_fields_hidden_from_review_but_not_saved_data():
    assert {"customer_contact", "current_charges_total", "previous_balance"} <= INTERNAL_ONLY_INVOICE_FIELDS
    evidence = {
        "customer_contact": {"warning": "Not in OCR"},
        "current_charges_total": {"warning": "Check total"},
        "previous_balance": {"warning": "Check previous balance"},
        "vendor_address": {"warning": "Check"},
        "line_items.0.amount": {"warning": "Check amount"},
    }
    copy = dict(evidence)
    assert set(visible_field_evidence(evidence)) == {"vendor_address", "line_items.0.amount"}
    assert evidence == copy


def test_history_view_uses_approved_rows_and_filename_title_not_filename_detail():
    source = Path(__file__).resolve().parents[1] / "ui/pages/History.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    rendered_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(isinstance(call.func, ast.Attribute) and call.func.attr == "subheader"
               and call.args and isinstance(call.args[0], ast.Call)
               and isinstance(call.args[0].func, ast.Name)
               and call.args[0].func.id == "history_invoice_title" for call in rendered_calls)
    assert any(isinstance(call.func, ast.Name) and call.func.id == "approved_history_document_rows"
               for call in rendered_calls)
    source_code = source.read_text(encoding="utf-8")
    assert 'c1.write(f"**Filename:**' not in source_code
    assert 'f"Invoice {detail[\'invoice_number\']}"' not in source_code


def test_line_items_retained_as_genuine_items_with_no_duplicate_synthetic_rows():
    inv = example_invoice()
    before = [dict(row) for row in inv["line_items"]]
    approved_history_document_rows(inv)
    invoice_export_row(inv)
    assert inv["line_items"] == before
    assert [row["description"] for row in inv["line_items"]] == ["Current Charges", "Previous Balance"]


def test_client_exports_exclude_hidden_statement_totals_without_changing_source():
    inv = example_invoice()
    original = inv.copy()
    assert not any(label in EXPORT_COLUMNS for label in _UNAPPROVED)
    row = invoice_export_row(inv)
    assert not any(label in row for label in _UNAPPROVED)
    totals = invoice_totals_row([inv])
    assert totals["Total Amount Due"] == 15.0
    assert inv == original


def test_csv_excel_pdf_exclude_unapproved_summary_amounts(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from pypdf import PdfReader
    inv = example_invoice()
    monkeypatch.setattr("exports.csv_export.settings.EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr("exports.excel_export.settings.EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr("exports.pdf_export.settings.EXPORT_DIR", str(tmp_path))
    csv_path = export_to_csv([inv], filename="out.csv")[0]
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == EXPORT_COLUMNS
        assert next(reader)["Total Amount Due"]
    xlsx_path = export_to_excel([inv], filename="out.xlsx")[0]
    wb = load_workbook(xlsx_path, read_only=True)
    assert list(next(wb["Invoices"].values)) == EXPORT_COLUMNS
    wb.close()
    pdf_path = export_to_pdf([inv], filename="out.pdf")[0]
    pdf_text = " ".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    for text in (Path(csv_path).read_text(encoding="utf-8"), pdf_text):
        assert "Current Charges Total" not in text
        assert "Previous Balance" not in text
        assert "1111.11" not in text and "2222.22" not in text
        assert "Secret Contact" not in text
