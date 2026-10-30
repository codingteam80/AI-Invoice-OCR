"""QA regressions: invalid printed Bill Date, separate History currency totals."""
import ast
import json
from pathlib import Path

from ai.field_display import evidence_for_invoice
from ai.invoice_templates.meralco import normalize_meralco_new_layout
from services.currency_totals import totals_by_currency


ROOT = Path(__file__).resolve().parents[1]


def _fixture():
    return json.loads((ROOT / "tests/fixtures/inv27_meralco_inconsistent_trace_min.json").read_text())


def test_invalid_bill_date_not_turned_into_last_day_of_month():
    f = _fixture()
    extracted = dict(f["final"])
    extracted["invoice_date"] = "2021-11-30"  # plausible but invented date
    out, notes = normalize_meralco_new_layout(extracted, f["ocr"]["text"], f["ocr"]["lines"])
    assert out["invoice_date"] is None
    assert any("Invoice date verification required" in n and "31/11/2021" in n for n in notes)
    assert not any("32/13/2021" in n and "Invoice date verification" in n for n in notes)


def test_invalid_bill_date_recorded_even_if_date_was_already_missing():
    f = _fixture()
    extracted = dict(f["final"])
    extracted["invoice_date"] = None
    out, notes = normalize_meralco_new_layout(extracted, f["ocr"]["text"], f["ocr"]["lines"])
    assert out["invoice_date"] is None
    assert any("31/11/2021" in n and "Invoice date verification required" in n for n in notes)


def test_old_saved_record_with_invalid_date_is_highlighted():
    record = {
        "invoice_date": None,
        "field_evidence": {"invoice_date": {"score": None, "status": "missing", "warning": "Required field missing"}},
        "validation_issues": ["invoice_date has an invalid date format: 31/11/2021", "Missing required field: 'invoice_date'"],
    }
    evidence, _ = evidence_for_invoice(record)
    assert evidence["invoice_date"]["status"] == "review"
    assert "31/11/2021" in evidence["invoice_date"]["warning"]
    assert "Billing Period" in evidence["invoice_date"]["warning"]


def test_history_uses_currency_isolated_totals_helper():
    src = (ROOT / "ui/pages/History.py").read_text()
    ast.parse(src)
    assert "totals_by_currency(invoices)" in src
    assert "Sum of available Total Amount Due values ({currency})" in src
    assert 'total = sum(inv.get("total_amount") or 0 for inv in invoices)' not in src
    assert totals_by_currency([
        {"currency": "PHP", "total_amount": 1502.42},
        {"currency": "USD", "total_amount": 400.00},
        {"currency": "₱", "total_amount": 12.0},
        {"currency": "PHP", "total_amount": None},
    ]) == {"PHP": 1514.42, "USD": 400.00}
