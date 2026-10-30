"""Financial placeholders in History must never be mistaken for stored amounts."""
from pathlib import Path

import importlib.util

# Avoid importing Streamlit at test collection time: a running UI isn't
# required to verify this pure display formatting helper.
source_file = Path(__file__).resolve().parents[1] / "ui/components/financial_display.py"
spec = importlib.util.spec_from_file_location("financial_display", source_file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HISTORY_FINANCIAL_FIELDS = module.HISTORY_FINANCIAL_FIELDS
format_history_amount = module.format_history_amount
has_unextracted_amount = module.has_unextracted_amount


def test_all_six_financial_fields_use_display_formatting():
    assert dict(HISTORY_FINANCIAL_FIELDS) == {
        "Vatable Sales": "subtotal",
        "VAT-Exempt Sales": "vat_exempt_sales",
        "Zero-Rated Sales": "zero_rated_sales",
        "VAT": "tax_amount",
        "Withholding Tax": "withholding_tax",
        "Total Amount Due": "total_amount",
    }
    history = (Path(__file__).resolve().parents[1] / "ui/pages/History.py").read_text(encoding="utf-8")
    assert "for label, key in HISTORY_FINANCIAL_FIELDS:" in history
    assert "format_history_amount(detail.get(key), currency)" in history
    assert "c2.caption(" in history


def test_unextracted_amount_shows_0_without_mutating_invoice_data():
    stored = {key: None for _, key in HISTORY_FINANCIAL_FIELDS}
    before = stored.copy()
    for _, key in HISTORY_FINANCIAL_FIELDS:
        assert format_history_amount(stored[key], "PHP") == "0.00"
    assert has_unextracted_amount(stored)
    assert stored == before


def test_existing_values_and_explicit_zero_remain_distinct():
    assert format_history_amount(3498.0, "PHP") == "3,498.00 PHP"
    assert format_history_amount(0.0, "PHP") == "0.00 PHP"
    assert format_history_amount(0.0) == "0.00"
    assert format_history_amount(-1147.90, "PHP") == "-1,147.90 PHP"
    assert format_history_amount(3917.76, "PHP") == "3,917.76 PHP"


def test_no_placeholder_note_when_all_financial_amounts_present():
    complete = {key: 0.0 for _, key in HISTORY_FINANCIAL_FIELDS}
    assert not has_unextracted_amount(complete)


def test_edit_form_preserves_missing_amounts_when_user_saves_without_changes():
    history = (Path(__file__).resolve().parents[1] / "ui/pages/History.py").read_text(encoding="utf-8")
    for key in ("subtotal", "tax_amount", "total_amount", "vat_exempt_sales", "zero_rated_sales", "withholding_tax"):
        assert f'"{key}": _preserve_optional_numeric(inv.get("{key}"), {key})' in history
    assert "if original_value is None and abs(edited) < 1e-12:" in history
