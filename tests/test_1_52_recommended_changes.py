from ai.post_processing import (
    apply_identity_corrections_with_gate,
    apply_vision_corrections_with_financial_gate,
    reconcile_single_unreliable_financial_field,
    reconcile_invoice_date,
)
from ai.vision_verifier import _extract_labeled_tin_from_header_text, _address_from_header_ocr
from utils.date_utils import parse_date_with_context


def _line(text, x1, y1, x2, y2):
    return {"text": text, "confidence": 0.99, "bbox": [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]}


def test_context_mmddyy_wins_for_ambiguous_invoice_date():
    text = """Customer TIN\nInvoice Date\n08/05/26\nBilling Period (mm/dd/yy)\nDue Date\n08/26/26"""
    out, notes = reconcile_invoice_date({"invoice_date": "2026-05-08"}, text)
    assert out["invoice_date"] == "2026-08-05"
    assert notes
    assert parse_date_with_context("08/05/26", "format mm/dd/yy").isoformat() == "2026-08-05"


def test_identity_gate_keeps_explicit_vendor_tin_over_client_number():
    text = """Sycip, Gorres, Velayo & Co.\nVAT Reg. TIN\n000-502-547-00000\nClient No.:\n0011670080\nClient VAT/TIN\n007-848-122-000"""
    data = {"vendor_tax_id": "000-502-547-00000", "customer_tax_id": "007-848-122-000"}
    vision = {"vendor_tax_id": "0011670080"}
    out, filtered, locked, notes, accepted, rejected = apply_identity_corrections_with_gate(data, vision, text)
    assert out["vendor_tax_id"] == "000-502-547-00000"
    assert "vendor_tax_id" in locked
    assert "vendor_tax_id" not in filtered
    assert rejected["vendor_tax_id"] == "0011670080"


def test_header_tin_requires_explicit_tin_label_and_address_cues():
    header = """Innove Communications, Inc.\n876569970\n2nd Street corner 7th Avenue\nBonifacio Global City, Taguig, Philippines 1634\nVAT Reg TIN: 000-360-916-00000"""
    assert _extract_labeled_tin_from_header_text(header) == "000-360-916-00000"
    assert _extract_labeled_tin_from_header_text("Account Number\n876569970") is None
    addr = _address_from_header_ocr(header)
    assert "2nd Street" in addr
    assert "Taguig" in addr


def test_gliptic_tax_equals_discount_cancellation_is_rejected_semantically():
    data = {
        "subtotal": 2657.0,
        "tax_amount": 935.70,
        "discount": 2845.70,
        "zero_rated_sales": 0.0,
        "vat_exempt_sales": 0.0,
        "total_amount": 747.0,
    }
    corrections = {
        "subtotal": 27400.0,
        "tax_amount": 2935.71,
        "discount": 2935.71,
        "total_amount": 27400.0,
    }
    mismatches = [
        {"field":"subtotal","image_shows":"27400.0"},
        {"field":"tax_amount","image_shows":"2935.71"},
        {"field":"discount","image_shows":"2935.71"},
        {"field":"total_amount","image_shows":"27400.0"},
    ]
    # No strong same-row numeric discount evidence in OCR.
    lines = [
        _line("VATable Sales", 100,100,250,130),
        _line("Less: Discount", 100,160,260,190),
        _line("Add: VAT", 100,220,200,250),
        _line("2,935.71", 500,220,620,250),
        _line("Total Amount Due",100,280,300,310),
        _line("27,400.00",500,280,640,310),
    ]
    out, locked, notes, accepted, rejected = apply_vision_corrections_with_financial_gate(
        data, corrections, mismatches, ocr_lines=lines
    )
    assert out["subtotal"] == 24464.29
    assert out["tax_amount"] == 2935.71
    assert out["discount"] == 0.0
    assert out["total_amount"] == 27400.0
    assert {"subtotal","tax_amount","discount","total_amount"}.issubset(locked)


def test_emerald_single_weak_vat_exempt_is_derived_from_identity():
    data = {
        "subtotal": 250.0,
        "tax_amount": 30.0,
        "discount": 0.0,
        "zero_rated_sales": 0.0,
        "vat_exempt_sales": 0.0,
        "total_amount": 2247.40,
    }
    lines = [
        _line("VATable Sales",100,100,250,130), _line("250.00",500,100,580,130),
        _line("VAT",100,140,170,170), _line("30.00",500,140,570,170),
        _line("Zero-Rated Sales",100,180,280,210),
        _line("VAT-Exempt Sales",100,220,300,250),
        _line("Less: Discount",100,260,260,290),
        _line("TOTAL AMOUNT DUE",100,300,320,330), _line("2,247.40",500,300,610,330),
    ]
    out, notes, locks = reconcile_single_unreliable_financial_field(
        data, lines, vision_accepted={"vat_exempt_sales": 1907.40}, locked_fields=set()
    )
    assert out["vat_exempt_sales"] == 1967.40
    assert "vat_exempt_sales" in locks
    assert notes
