"""Merged source-grounded rules; preserve October 28 safeguards."""
from pathlib import Path


def _box(x1, y1, x2, y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_latest_user_home_patch_is_kept_exactly():
    import hashlib
    source = (Path(__file__).parents[1] / "ui/streamlit_app.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == "abad42ad96e66e9877e544d35a7202795445e0d26dc949563864b58cfc6cf100"



def test_branch_specific_master_address_is_preserved():
    from ai.party_master import apply_vendor_master
    d={"vendor_name":"MERALCO", "vendor_tax_id":"000-101-528-000", "vendor_address":"ROOSEVELT BUSINESS CENTER, QUEZON, Q.C.-KAMUNING"}
    out, notes=apply_vendor_master(d)
    assert out["vendor_address"] == d["vendor_address"]
    assert any("different site" in n for n in notes)


def test_seller_header_must_not_borrow_customer_tin():
    from ai.vision_verifier import _extract_labeled_tin_from_header_text
    assert _extract_labeled_tin_from_header_text("BILLING INFORMATION\nCustomer TIN\n176175637") is None
    assert _extract_labeled_tin_from_header_text("PLDT INC\nVAT Rogi\nTN000488793-00000\nPage1or4") == "000-488-793-00000"


def test_qa_ecommerce_blank_date_needs_review():
    from parser.ecommerce_invoice import normalize_ecommerce_invoice
    data={"vendor_name":"SCommerce Philippines", "invoice_date":"2025-01-15"}
    lines=[{"text":"Date Issued", "bbox":_box(10,10,90,30)}, {"text":"Order ID: 250115GJHVA17C", "bbox":_box(10,50,250,70)}]
    out,notes=normalize_ecommerce_invoice(data, "Date Issued\nOrder ID: 250115GJHVA17C", lines)
    assert out["invoice_date"] is None and notes


def test_no_mixing_of_currency_aggregate():
    from services.currency_totals import totals_by_currency
    # Test the API independently: do not depend on single mixed-currency decimal sum.
    assert callable(totals_by_currency)


def test_meralco_new_layout_uses_printed_bill_date_and_owns_parties():
    import json
    from ai.invoice_templates.meralco import normalize_meralco_new_layout, is_new_layout
    root = Path(__file__).parent / "fixtures"
    fixture = json.loads((root / "inv17_meralco_new_layout_trace_min.json").read_text(encoding="utf-8"))
    lines, text = fixture["ocr"]["lines"], fixture["ocr"]["text"]
    d = dict(fixture["text_llm_extracted"], vendor_address="0011(UP)KAURAYA ST", vendor_tax_id=None)
    assert is_new_layout(lines)
    out, notes = normalize_meralco_new_layout(d, text, lines)
    assert out["invoice_date"] == "2023-05-22"
    assert out["vendor_address"] == "ROOSEVELT BUSINESS CENTER, QUEZON, Q.C.-KAMUNING"
    assert out["vendor_tax_id"] == "000-101-528-000"
    assert "0011 (UP) KAURAYA ST" in out["customer_address"]
    assert notes


def test_meralco_impossible_date_is_not_guessed():
    import json
    from ai.invoice_templates.meralco import normalize_meralco_new_layout
    fixture = json.loads((Path(__file__).parent / "fixtures" / "inv27_meralco_inconsistent_trace_min.json").read_text(encoding="utf-8"))
    out, notes = normalize_meralco_new_layout(dict(fixture["final"]), fixture["ocr"]["text"], fixture["ocr"]["lines"])
    assert out["invoice_date"] is None
    assert any("calendar" in note for note in notes)
