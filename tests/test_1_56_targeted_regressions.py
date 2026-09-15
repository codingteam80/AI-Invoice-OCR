from pathlib import Path

from ai.post_processing import (
    clean_customer_address,
    normalize_vendor_display_name,
    reconcile_line_item_geometry,
    clean_line_items,
    reconcile_billing_current_period_financials,
    apply_vision_corrections_with_financial_gate,
)
import ai.vision_verifier as vv


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_customer_address_strips_leading_long_account_number():
    raw = "876569970 One Corporate Center Julia Vargas Cor Meralco Ave Ortigas Center, Pasig"
    assert clean_customer_address(raw) == "One Corporate Center Julia Vargas Cor Meralco Ave Ortigas Center, Pasig"


def test_customer_address_keeps_normal_street_number():
    raw = "1209 Ayala Avenue, Makati City"
    assert clean_customer_address(raw) == raw


def test_globe_vendor_normalization_tolerates_innovate_ocr_variant():
    assert normalize_vendor_display_name("The Globe Tower Cebu SamarL Innovate Communications, Inc") == "Globe Business: Innove Communications, Inc."


def test_line_item_geometry_uses_total_amount_column_not_qty():
    lines = [
        {"text":"TOTAL AMOUNT","bbox":box(1960,1132,2274,1188)},
        {"text":"SERVICE FEE (TICKET)","bbox":box(197,1344,580,1411)},
        {"text":"1","bbox":box(1712,1395,1746,1436)},
        {"text":"1,229.40","bbox":box(2150,1398,2300,1459)},
    ]
    data={"line_items":[{"description":"SERVICE FEE (TICKET)","quantity":1,"unit_price":1,"amount":1}]}
    out,_=reconcile_line_item_geometry(data,lines)
    assert out["line_items"][0]["unit_price"] == 1229.40
    assert out["line_items"][0]["amount"] == 1229.40


def test_monthly_plan_aggregate_is_dropped_when_concrete_items_reconcile():
    data={
        "current_charges_total":4198.0,
        "discount":59.0,
        "line_items":[
            {"description":"GFiber Biz Plus 3499 Unli Sym 500Mbps","quantity":1,"unit_price":3499,"amount":3499},
            {"description":"Monthly Plan","quantity":1,"unit_price":4198,"amount":4198},
            {"description":"Add-ons","quantity":1,"unit_price":758,"amount":758},
        ]
    }
    out,_=clean_line_items(data)
    names=[x["description"] for x in out["line_items"]]
    assert "Monthly Plan" not in names
    assert "Add-ons" in names
    assert any("GFiber" in n for n in names)


def test_billing_current_period_does_not_turn_previous_balance_into_tax():
    data={
        "subtotal":4257.0,
        "discount":59.0,
        "current_charges_total":4198.0,
        "previous_balance":1749.51,
        "total_amount":5947.51,
        "tax_amount":1749.51,
    }
    out,notes=reconcile_billing_current_period_financials(data,[])
    assert out["tax_amount"] is None
    assert any("Previous balance is not tax" in n for n in notes)


def test_gliptic_image_anchors_override_small_balanced_ocr_set():
    data={
        "subtotal":4295.0,"tax_amount":586.2,"discount":0.0,
        "zero_rated_sales":0.0,"vat_exempt_sales":0.0,"total_amount":4881.2,
    }
    lines=[
        {"text":"Zero-Rated Sales","bbox":box(10,100,180,120)},
        {"text":"VAT-Exempt Sales","bbox":box(10,140,180,160)},
        {"text":"Less: Withholding Tax","bbox":box(10,180,220,200)},
    ]
    # Explicit blank rows are represented by the evidence collector because no
    # numeric same-row value is present.
    mismatches=[
        {"field":"subtotal","image_shows":"24,464.29"},
        {"field":"tax_amount","image_shows":"586.20"},
        {"field":"zero_rated_sales","image_shows":"0"},
        {"field":"vat_exempt_sales","image_shows":"0"},
        {"field":"total_amount","image_shows":"27,400.00"},
    ]
    corrections={"subtotal":24464.29,"total_amount":27400.0}
    out,locked,notes,accepted,rejected=apply_vision_corrections_with_financial_gate(data,corrections,mismatches,ocr_lines=lines)
    assert out["subtotal"] == 24464.29
    assert out["tax_amount"] == 2935.71
    assert out["total_amount"] == 27400.0
    assert out["discount"] == 0.0
    assert {"subtotal","tax_amount","total_amount"} <= locked


def test_date_verifier_does_not_skip_because_of_atp_or_accreditation_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(vv.settings, "VISION_VERIFICATION_ENABLED", True)
    monkeypatch.setattr(vv.settings, "HANDWRITTEN_DATE_VERIFY_ENABLED", True, raising=False)
    crop=tmp_path/"crop.png"; crop.write_bytes(b"x")
    monkeypatch.setattr(vv, "_write_date_crop", lambda image_path, lines: str(crop))
    monkeypatch.setattr(vv, "_post_generate", lambda prompt,b64,trace: '{"invoice_date":"8/24/26"}')
    lines=[
        {"text":"Date:","bbox":box(10,10,100,30)},
        {"text":"Date of ATP: November 26 2025","bbox":box(10,100,300,120)},
        {"text":"Accreditation Date:12-10-2021 Expiration Date:12-09-2026","bbox":box(10,130,500,150)},
    ]
    trace={}
    iso,_=vv.verify_handwritten_invoice_date("dummy.png",lines,trace=trace)
    assert iso == "2026-08-24"
    assert trace.get("skipped_reason") is None
