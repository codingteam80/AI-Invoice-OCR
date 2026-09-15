from ai.post_processing import (
    normalize_vendor_display_name,
    clean_vendor_address,
    reconcile_financial_layout,
    apply_vision_corrections_with_financial_gate,
    reconcile_invoice_date,
    collect_financial_evidence,
)


def _line(text, x1, y1, x2, y2):
    return {"text": text, "confidence": 0.99, "bbox": [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]}


def test_globe_business_display_name_normalizes_even_when_vision_returns_brand_only():
    assert normalize_vendor_display_name("Globe BUSINESS") == "Globe Business: Innove Communications, Inc."


def test_vendor_address_drops_tel_and_telefax_suffix():
    raw = "79 P. Cruz Street, Brgy. Old Zaniga, Second District, 1550 City of Mandaluyong, NCR, Philippines, Tel. No.: 533-9571 * TeleFax: 534-1269"
    assert clean_vendor_address(raw) == "79 P. Cruz Street, Brgy. Old Zaniga, Second District, 1550 City of Mandaluyong, NCR, Philippines"


def test_tri_q_authoritative_total_due_and_withholding_outrank_intermediate_amount_due():
    data = {"subtotal": 85963.99, "tax_amount": 10315.68, "discount": None,
            "zero_rated_sales": None, "vat_exempt_sales": None, "total_amount": 96279.67}
    lines = [
        _line("VATable Sales", 100,100,350,140), _line("85,963.99", 600,100,800,140),
        _line("VAT Amount", 100,180,300,220), _line("10,315.68", 600,180,800,220),
        _line("Amount Due", 1700,180,1950,220), _line("96,279.67", 2080,180,2260,220),
        _line("Less:Withholding Tax", 1360,240,1750,280), _line("1,719.28", 2090,240,2250,280),
        # Slight OCR vertical skew: authoritative total value is partly above label.
        _line("94,560.39", 2070,285,2265,325), _line("TOTAL AMOUNT DUE", 1630,305,1945,342),
        _line("Zero Rated Sales",100,360,380,400), _line("VAT-Exempt Sales",100,420,400,460),
    ]
    out, notes = reconcile_financial_layout(data, lines)
    assert out["total_amount"] == 94560.39
    assert out["discount"] == 1719.28
    assert out["zero_rated_sales"] == 0.0
    assert out["vat_exempt_sales"] == 0.0
    assert notes


def test_blank_optional_rows_do_not_borrow_neighbor_vat_value():
    data = {"subtotal": 1229.40, "tax_amount": 147.53, "discount": 147.53,
            "zero_rated_sales": 147.53, "vat_exempt_sales": None, "total_amount": 1376.93}
    lines = [
        _line("VATABLE SALES",90,100,385,150), _line("PHP 1,229.40",430,110,690,160),
        _line("VAT",90,160,180,210), _line("PHP 147.53",430,180,660,230),
        _line("ZERO RATED",90,215,330,265),
        _line("VAT EXEMPT SALES",90,275,455,325),
        _line("Less W/TAX",1600,335,1820,375),
    ]
    out, _ = reconcile_financial_layout(data, lines)
    assert out["zero_rated_sales"] == 0.0
    assert out["vat_exempt_sales"] == 0.0
    assert out["discount"] == 0.0


def test_blank_discount_row_overrides_flat_text_neighbor_amount():
    data = {"subtotal": 2468.0, "tax_amount": 296.16, "discount": 2468.0,
            "zero_rated_sales": None, "vat_exempt_sales": None, "total_amount": 2764.16}
    lines = [
        _line("VATABLE SALES",100,100,380,145), _line("PHP 2,468.00",435,100,690,150),
        _line("VAT",100,160,180,205), _line("PHP 296.16",435,160,660,210),
        _line("ZERO RATED",100,220,330,260),
        _line("Less: Discount",1675,260,1950,300),
        # Neighbor amount is above the discount row, not on its row.
        _line("PHP2,468.00",1975,235,2220,275),
        _line("VAT EXEMPT SALES",100,280,455,325),
    ]
    out, _ = reconcile_financial_layout(data, lines)
    assert out["discount"] == 0.0
    assert out["zero_rated_sales"] == 0.0
    assert out["vat_exempt_sales"] == 0.0


def test_vision_cannot_replace_strong_total_due_and_withholding_with_balancing_pair():
    data = {"subtotal": 85963.99, "tax_amount": 10315.68, "discount": 1719.28,
            "zero_rated_sales": 0.0, "vat_exempt_sales": 0.0, "total_amount": 94560.39}
    lines = [
        _line("Less:Withholding Tax", 1360,100,1750,140), _line("1,719.28",2090,100,2250,140),
        _line("94,560.39",2070,155,2265,195), _line("TOTAL AMOUNT DUE",1630,175,1945,212),
        _line("Zero Rated Sales",100,230,380,270), _line("VAT-Exempt Sales",100,290,400,330),
    ]
    mismatches = [
        {"field":"subtotal","image_shows":"85963.99"},
        {"field":"tax_amount","image_shows":"10315.68"},
        {"field":"discount","image_shows":"1719.28"},
        {"field":"total_amount","image_shows":"96279.67"},
    ]
    out, locked, notes, accepted, rejected = apply_vision_corrections_with_financial_gate(
        data, {}, mismatches, ocr_lines=lines
    )
    assert out["total_amount"] == 94560.39
    assert out["discount"] == 1719.28
    assert "total_amount" in locked and "discount" in locked
    assert rejected.get("total_amount") == 96279.67


def test_ambiguous_invoice_date_uses_nearby_due_date_when_format_hint_was_missed_by_ocr():
    text = """Customer TIN\nInvoice Date\n007-848-122-00000\n08/05/26\n07/06/26 to 08/05/26\nDue Date\n08/26/26"""
    out, _ = reconcile_invoice_date({"invoice_date":"2026-05-08"}, text)
    assert out["invoice_date"] == "2026-08-05"


def test_financial_evidence_trace_classifies_blank_and_authoritative_sources():
    lines = [
        _line("ZERO RATED",100,100,300,140),
        _line("Less: Discount",100,160,320,200),
        _line("94,560.39",500,220,650,260), _line("TOTAL AMOUNT DUE",100,240,350,280),
    ]
    ev = collect_financial_evidence(lines)
    assert ev["zero_rated_sales"]["source"] == "explicit_blank"
    assert ev["discount"]["source"] == "explicit_blank"
    assert ev["total_amount"]["score"] >= 110
