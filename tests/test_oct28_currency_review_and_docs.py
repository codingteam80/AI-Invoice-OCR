"""Source-based QA regression tests for Oct 28 changes (no live API)."""
from datetime import date, timedelta
from pathlib import Path
import sqlite3

from ai.post_processing import reconcile_customer_name
from ai.field_evidence import build_field_evidence
from ai.validator import validate_extraction
from parser.currency_parser import normalize_currency
from services.currency_totals import totals_by_currency, currency_code
from exports.common import invoice_totals_rows
from config.settings import settings


def test_customer_column_metadata_does_not_replace_bill_to_name():
    text = ("NOTARIZE, INC. DBA PROOF\nBill to\nInvoice number\nDate\n"
            "Payment terms\nABC Company\nNOTARIZE185857\n02/22/2026\nNet 30\n")
    out, notes = reconcile_customer_name({"customer_name": "INVOICE NUMBER"}, text)
    assert out["customer_name"] == "ABC Company"
    assert notes


def test_customer_name_does_not_take_a_date_or_heading_as_customer():
    text = "Invoice No.\nBill to\nDate\nPayment terms\nSample Corp\n2026-09-14\nNet 30"
    out, _ = reconcile_customer_name({"customer_name": None}, text)
    assert out["customer_name"] == "Sample Corp"


def test_future_date_is_visible_warning_even_with_strong_source_ocr():
    upcoming = (date.today() + timedelta(days=65)).isoformat()
    d = {"invoice_number": "123", "vendor_name": "ABC", "invoice_date": upcoming,
         "total_amount": 400.0, "currency": "PHP"}
    issues = validate_extraction(d)
    assert any("future" in x for x in issues)
    ev = build_field_evidence(d, "Date: " + upcoming, validation_issues=issues)
    assert ev["invoice_date"]["status"] == "review"
    assert "future" in ev["invoice_date"]["warning"].lower()


def test_all_four_missing_required_fields_flagged():
    d = {"invoice_number": None, "vendor_name": "Unknown Vendor", "invoice_date": None,
         "total_amount": None, "currency": "PHP"}
    issues = validate_extraction(d)
    assert sum("Missing required field" in issue for issue in issues) == 4
    ev = build_field_evidence(d, "", validation_issues=issues)
    for field in ("invoice_number", "invoice_date", "vendor_name", "total_amount"):
        assert ev[field]["warning"]
        assert ev[field]["status"] in {"missing", "review"}


def test_philippine_peso_markers_but_not_company_names():
    for value in ("PHP", "₱", "P", "P1,200.00", "P 90.00", "Philippine Peso"):
        assert normalize_currency(value) == "PHP"
    assert normalize_currency("USD") == "USD"
    assert normalize_currency("$") == "USD"
    assert normalize_currency("PARKING") != "PHP"


def test_mixed_currency_totals_and_export_rows_are_separate():
    invoices = [
        {"currency": "PHP", "total_amount": 120, "subtotal": 105, "tax_amount": 15},
        {"currency": "₱", "total_amount": 30, "subtotal": 30, "tax_amount": 0},
        {"currency": "USD", "total_amount": 400, "subtotal": 400, "tax_amount": 0},
    ]
    assert totals_by_currency(invoices) == {"PHP": 150.0, "USD": 400.0}
    rows = invoice_totals_rows(invoices)
    assert [(r["Currency"], r["Total Amount Due"]) for r in rows] == [("PHP", 150), ("USD", 400)]
    assert all(r["ID"] == "TOTAL" for r in rows)


def test_groq_api_defaults_are_settable(monkeypatch):
    # Effective selection is still controlled by .env/Settings; inspect the
    # declared fallback values rather than assuming this test process has no env.
    settings_source = (Path(__file__).resolve().parents[1] / "config" / "settings.py").read_text()
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for expected in ("https://api.groq.com/openai/v1", "qwen/qwen3.8-27b"):
        assert expected in settings_source and expected in example
    assert "DEFAULT_CURRENCY=PHP" in example


def test_revised_docs_and_no_duplicate_opencv():
    root = Path(__file__).resolve().parents[1]
    assert "opencv-contrib-python==" in (root / "requirements.txt").read_text()
    assert "\nopencv-python==" not in (root / "requirements.txt").read_text()
    for path in ("README.md", "SCANNER_PRINT_SETUP.md", "SETUP_SERVER.md", "docs/user_manual.md"):
        assert "invoice" in (root / path).read_text().lower()


def test_old_saved_evidence_receives_future_date_overlay():
    from ai.field_display import evidence_for_invoice
    future = (date.today() + timedelta(days=30)).isoformat()
    fields = {"invoice_number": "X1", "vendor_name": "ABC", "invoice_date": future,
              "total_amount": 20, "field_evidence": {"invoice_date": {"score": 0.95, "status": "supported", "warning": ""}}}
    out, _ = evidence_for_invoice(fields)
    assert out["invoice_date"]["status"] == "review"
    assert "future" in out["invoice_date"]["warning"].lower()
    # The previously stored evidence must not be rewritten.
    assert fields["field_evidence"]["invoice_date"]["status"] == "supported"
