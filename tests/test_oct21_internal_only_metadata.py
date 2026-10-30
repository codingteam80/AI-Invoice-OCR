"""Unrequested optional metadata stays internal, not in the customer UI/exports."""
import csv
from pathlib import Path

from config.field_visibility import (
    HISTORY_SECONDARY_FIELDS, INTERNAL_ONLY_INVOICE_FIELDS,
    visible_field_evidence, visible_validation_issues,
)
from exports.common import EXPORT_COLUMNS, invoice_export_row
from exports.csv_export import export_to_csv
from exports.excel_export import export_to_excel
from exports.pdf_export import export_to_pdf


def _sample_invoice():
    return {
        "id": 7, "invoice_number": "RECEIPT-7", "vendor_name": "Test Vendor",
        "customer_name": "Client", "date_uploaded": "2026-10-21",
        "invoice_date": "2026-10-20", "original_filename": "sample.png",
        "due_date": "2099-02-19", "tax_rate": 22.345, "payment_terms": "NET_SEVENTY_SEVEN",
        "subtotal": 100.0, "tax_amount": 12.0, "total_amount": 112.0,
        "currency": "PHP", "category": "Others",
    }


def test_internal_fields_hidden_from_client_detail_and_review_notifications():
    assert {"due_date", "tax_rate", "payment_terms"} <= INTERNAL_ONLY_INVOICE_FIELDS
    assert not any(field in INTERNAL_ONLY_INVOICE_FIELDS for _, field in HISTORY_SECONDARY_FIELDS)
    evidence = {
        "vendor_address": {"status": "review", "warning": "Check address"},
        "due_date": {"status": "review", "warning": "Check due date"},
        "payment_terms": {"status": "review", "warning": "Check terms"},
        "tax_rate": {"status": "review", "warning": "Check rate"},
        "line_items.0.unit_price": {"status": "review", "warning": "Check amount"},
    }
    filtered = visible_field_evidence(evidence)
    assert list(filtered) == ["vendor_address", "line_items.0.unit_price"]
    assert len(evidence) == 5  # original evidence is not mutated


def test_common_export_excludes_internal_only_fields_without_mutating_source():
    source = _sample_invoice()
    before = dict(source)
    assert all(label not in EXPORT_COLUMNS for label in ("Due Date", "Tax Rate", "Payment Terms"))
    row = invoice_export_row(source)
    assert all(label not in row for label in ("Due Date", "Tax Rate", "Payment Terms"))
    assert source == before


def test_invoice_csv_omits_optional_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("exports.csv_export.settings.EXPORT_DIR", str(tmp_path))
    path = export_to_csv([_sample_invoice()], filename="invoice.csv")[0]
    text = Path(path).read_text(encoding="utf-8")
    assert "Due Date" not in text and "Payment Terms" not in text and "Tax Rate" not in text
    assert "2099-02-19" not in text and "NET_SEVENTY_SEVEN" not in text and "22.345" not in text
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == EXPORT_COLUMNS
        assert next(reader)["Invoice #"] == "RECEIPT-7"


def test_invoice_excel_omits_optional_metadata(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    monkeypatch.setattr("exports.excel_export.settings.EXPORT_DIR", str(tmp_path))
    path = export_to_excel([_sample_invoice()], filename="invoice.xlsx")[0]
    wb = load_workbook(path, read_only=True, data_only=True)
    rows = list(wb["Invoices"].values)
    assert list(rows[0]) == EXPORT_COLUMNS
    flat_text = " ".join(str(c) for row in rows for c in row)
    assert "2099-02-19" not in flat_text
    assert "22.345" not in flat_text
    assert "NET_SEVENTY_SEVEN" not in flat_text
    wb.close()


def test_invoice_pdf_omits_optional_metadata(tmp_path, monkeypatch):
    from pypdf import PdfReader
    monkeypatch.setattr("exports.pdf_export.settings.EXPORT_DIR", str(tmp_path))
    path = export_to_pdf([_sample_invoice()], filename="invoice.pdf")[0]
    content = " ".join(p.extract_text() or "" for p in PdfReader(path).pages)
    for hidden in ("Due Date", "Tax Rate", "Payment Terms", "2099-02-19", "NET_SEVENTY_SEVEN", "22.345"):
        assert hidden not in content
    assert "RECEIPT-7" in content


def test_internal_only_metadata_validation_notices_are_not_shown():
    issues = [
        "due_date 'INVALID' is not a valid calendar date",
        "payment_terms says Net 30 but due_date mismatches",
        "total_amount does not match subtotal plus VAT",
        "invoice_date is in the future",
    ]
    assert visible_validation_issues(issues) == issues[2:]
    assert len(issues) == 4  # original retained for validation/auditing
