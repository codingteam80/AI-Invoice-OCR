"""INV-19 (Meralco 'Billing Invoice' photo bill). Before: Generation 1227 (printed 1,719.21), Universal Charges 6592
(65.92), GEA-All 761 (7.61), Government Taxes 0.02 (304.30), Lifeline dropped -> line items summed to 9,517.30;
vendor TIN 000-115-280-0000 (printed 000-101-528-00000); customer_name '-Account Number (CAN)' and
customer_address = a list of charge labels. Fixture: tests/fixtures/inv19_meralco_billing_invoice_trace_min.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
FIX = json.loads((Path(__file__).parent / "fixtures" / "inv19_meralco_billing_invoice_trace_min.json").read_text(encoding="utf-8"))
LINES, TEXT, WRONG = FIX["ocr"]["lines"], FIX["ocr"]["text"], FIX["wrong_final"]
EXPECTED = {
    "Generation": 1719.21, "Transmission": 298.62, "System Loss": 158.92, "Distribution (Meralco)": 414.78,
    "Senior Citizen": 0.02, "Government Taxes": 304.30, "Universal Charges": 65.92, "FIT-All (Renewable)": 41.23,
    "GEA-All (Renewable)": 7.61, "Lifeline": 2.05, "Other Charges": 23.71,
}


def test_layout_detected_and_other_meralco_layouts_are_not():
    from ai.invoice_templates.meralco import is_billing_invoice_layout
    assert is_billing_invoice_layout(LINES)
    assert not is_billing_invoice_layout([l for l in LINES if l["text"] != "Billing Invoice"])
    assert not is_billing_invoice_layout(None)


def test_line_items_are_rebuilt_and_sum_to_the_printed_total():
    from ai.invoice_templates.meralco import normalize_meralco_billing_invoice
    out, notes = normalize_meralco_billing_invoice(dict(WRONG), TEXT, LINES)
    got = {i["description"]: i["amount"] for i in out["line_items"]}
    assert got == EXPECTED
    assert round(sum(got.values()), 2) == 3036.37 == out["total_amount"] == out["current_charges_total"]
    assert any("line_items rebuilt" in n for n in notes)


def test_vendor_tin_parties_and_dates():
    from ai.invoice_templates.meralco import normalize_meralco_billing_invoice
    out, _ = normalize_meralco_billing_invoice(dict(WRONG), TEXT, LINES)
    assert out["vendor_tax_id"] == "000-101-528-00000"
    assert out["vendor_address"] == "LOPEZ BLDG., ORTIGAS AVENUE, PASIG CITY"
    assert out["customer_name"] is None and out["customer_address"] is None
    assert out["invoice_date"] is None                          # only a Due Date is printed
    assert str(out["due_date"]) == "2026-04-29"


def test_decimal_repair_and_residual_logic():
    from ai.invoice_templates.meralco import _bi_solve
    rows = [("A", 6592.0, False, 0.5), ("B", 41.23, True, 0.9), ("C", 761.0, False, 0.98)]
    assert _bi_solve(rows, 114.76)[0] == [65.92, 41.23, 7.61]           # decimals restored, sum matches
    rows = [("G", 1227.0, False, 0.42), ("U", 6592.0, False, 0.57), ("X", 100.0, True, 0.9)]
    vals, why = _bi_solve(rows, 1819.21)
    assert vals == [1653.29, 65.92, 100.0] and 0 in why                   # lowest-confidence row is the residual
    assert _bi_solve([("A", 10.0, True, 0.9)], 99.0) == (None, {})        # irreconcilable -> untouched


def test_idempotent_and_noop_elsewhere():
    from ai.invoice_templates.meralco import normalize_meralco_billing_invoice
    once, _ = normalize_meralco_billing_invoice(dict(WRONG), TEXT, LINES)
    twice, notes = normalize_meralco_billing_invoice(dict(once), TEXT, LINES)
    assert twice == once and notes == []
    inv17 = json.loads((Path(__file__).parent / "fixtures" / "inv17_meralco_new_layout_trace_min.json").read_text(encoding="utf-8"))
    d = {"vendor_name": "MERALCO", "line_items": [1]}
    assert normalize_meralco_billing_invoice(dict(d), inv17["ocr"]["text"], inv17["ocr"]["lines"]) == (d, [])
    assert normalize_meralco_billing_invoice(dict(d), "", None) == (d, [])


def test_vendor_name_is_not_required_to_say_meralco():
    """v15 regression: the draft's vendor_name was 'MANILA ELECTRIC COMPANY', so a vendor-name gate skipped the pass."""
    from ai.invoice_templates.meralco import normalize_meralco_billing_invoice
    assert WRONG["vendor_name"] == "MANILA ELECTRIC COMPANY"
    out, notes = normalize_meralco_billing_invoice(dict(WRONG), TEXT, LINES)
    assert notes and out["vendor_tax_id"] == "000-101-528-00000"
    src = (Path(__file__).parent.parent / "parser" / "invoice_parser.py").read_text(encoding="utf-8")
    call = src.index("normalize_meralco_billing_invoice(cleaned")
    assert "vendor_name" not in src[src.rindex("\n    if ", 0, call):call]
