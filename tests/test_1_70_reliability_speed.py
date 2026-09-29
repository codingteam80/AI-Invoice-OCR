import json

from ai import extractor
from ai.post_processing import (
    collect_financial_evidence,
    reconcile_customer_address_layout,
    reconcile_financial_layout,
    reconcile_line_item_geometry,
    reconcile_party_ownership_layout,
)
from ai.vision_verifier import _vendor_header_field_needs_recovery
from utils.helpers import normalize_tin


def box(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def line(text, x1, y1, x2, y2):
    return {"text": text, "bbox": box(x1, y1, x2, y2), "confidence": 0.99}


def test_total_sales_geometry_owns_printed_total_despite_slight_box_skew():
    lines = [
        line("VATABLE SALES", 1300, 1850, 1550, 1900),
        line("PHP 1,229.40", 1800, 1850, 2050, 1900),
        line("VAT", 1500, 1910, 1580, 1960),
        line("PHP 147.53", 1800, 1910, 2050, 1960),
        line("Total Sales", 1608, 1946, 1827, 2006),
        # Deliberately only ~34% vertical overlap and a small x overlap,
        # matching the real North Star trace.
        line("PHP 1,376.93", 1800, 1987, 2056, 2043),
        line("ZERO RATED", 1500, 2050, 1750, 2100),
        line("VAT EXEMPT SALES", 1450, 2110, 1750, 2160),
    ]
    data = {
        "subtotal": 1229.40,
        "tax_amount": 147.53,
        "zero_rated_sales": 0.0,
        "vat_exempt_sales": 0.0,
        "discount": None,
        "withholding_tax": 0.0,
        "total_amount": 1524.46,
    }
    out, notes = reconcile_financial_layout(data, lines)
    assert out["total_amount"] == 1376.93
    assert collect_financial_evidence(lines)["total_amount"]["value"] == 1376.93
    assert any("Total Sales" in n for n in notes)


def test_retail_geometry_uses_explicit_qty_unit_total_not_pack_size():
    lines = [
        line("SANGOBION CAPX100", 838, 959, 1175, 1004),
        line("10", 889, 992, 989, 1039),
        line("P29.75", 1021, 993, 1158, 1039),
        line("P297.50 V", 1375, 993, 1559, 1042),
        # Next row barcode must not become quantity.
        line("2092500272566", 841, 1027, 1095, 1080),
    ]
    data = {"line_items": [{"description": "SANGOBION CAP X 100", "quantity": 100, "unit_price": 29.75, "amount": 297.50}]}
    out, notes = reconcile_line_item_geometry(data, lines)
    item = out["line_items"][0]
    assert item["quantity"] == 10
    assert item["unit_price"] == 29.75
    assert item["amount"] == 297.50
    assert any("arithmetic reconciles" in n for n in notes)


def test_party_ownership_swaps_header_issuer_and_lower_customer_and_moves_customer_tin():
    lines = [
        line("Globe Business", 380, 130, 980, 225),
        line("Innove Communications,Inc", 243, 288, 724, 344),
        line("Tsukiden Global Solutions Inc", 250, 573, 927, 651),
        line("ERIC C FRANCISCO", 360, 660, 700, 705),
        line("2101 One Corporate Center Julia", 360, 700, 900, 745),
        line("Vargas Cor Meralco Ave Ortigas Center", 360, 748, 1050, 790),
        line("Ortigas,Pasig", 360, 793, 610, 830),
        line("Metro Manila,1605", 360, 833, 680, 870),
        line("Customer TIN", 1336, 814, 1524, 859),
        line("007-848-122-00000", 1343, 847, 1654, 906),
        # establish page height comparable to the statement page
        line("footer", 100, 3000, 300, 3050),
    ]
    data = {
        "vendor_name": "Tsukiden Global Solutions Inc",
        "vendor_address": "2101 One Corporate Center Julia, Vargas Cor Meralco Ave Ortigas Center",
        "vendor_tax_id": "007-848-122-00000",
        "customer_name": "Innove Communications, Inc",
        "customer_address": "2101 One Corporate Center Julia, 876569970",
        "customer_tax_id": None,
    }
    out, notes = reconcile_party_ownership_layout(data, lines)
    assert out["vendor_name"] == "Innove Communications, Inc"
    assert out["customer_name"] == "Tsukiden Global Solutions Inc"
    assert out["vendor_address"] is None
    assert out["vendor_tax_id"] is None
    assert out["customer_tax_id"] == "007-848-122-00000"
    out2, _ = reconcile_customer_address_layout(out, lines)
    assert out2["customer_address"].startswith("2101 One Corporate Center Julia")
    assert "876569970" not in out2["customer_address"]
    assert any("vendor/customer swap" in n for n in notes)


def test_tin_normalizes_fused_vat_label_with_five_digit_branch():
    assert normalize_tin("VATRg000-360-916-00000") == "000-360-916-00000"
    assert normalize_tin("00036091600000") == "000-360-916-00000"


def test_garbled_short_city_tail_triggers_vendor_header_recovery():
    need_address, need_tin = _vendor_header_field_needs_recovery({
        "vendor_address": "eou Busnes PakCebu City 5c00",
        "vendor_tax_id": "VATRg000-360-916-00000",
    })
    assert need_address is True
    # TIN is recoverable by normalization, so a separate TIN recovery is not required.
    assert need_tin is False


def test_llm_correction_stops_when_validation_issue_set_stalls(monkeypatch):
    responses = iter([
        json.dumps({"invoice_number": "1"}),
        json.dumps({"invoice_number": "1", "vendor_name": "X"}),
    ])
    calls = []

    def fake_call(prompt, trace_attempt=None):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(extractor, "_call_ollama", fake_call)
    monkeypatch.setattr(extractor, "detect_invoice_template", lambda _: "generic")
    monkeypatch.setattr(extractor, "build_extraction_prompt", lambda *a, **k: "initial")
    monkeypatch.setattr(extractor, "build_correction_prompt", lambda *a, **k: "correction")
    monkeypatch.setattr(extractor, "validate_extraction", lambda *a, **k: ["same unresolved issue"])
    monkeypatch.setattr(extractor.settings, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(extractor.settings, "LLM_STOP_ON_STALLED_VALIDATION", True)
    trace = {}
    out = extractor.extract_invoice_data("some OCR text", trace=trace)
    assert out["vendor_name"] == "X"
    assert len(calls) == 2  # initial + exactly one correction
    assert trace["stopped_on_stalled_validation"]["reason"].startswith("validation issue set unchanged")
