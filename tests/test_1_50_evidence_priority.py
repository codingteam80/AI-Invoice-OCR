from ai.post_processing import (
    reconcile_financial_layout,
    apply_vision_corrections_with_financial_gate,
    _amounts_on_line,
)
from parser.currency_parser import to_float
from utils.date_utils import to_iso


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_short_unambiguous_us_date_8_24_26():
    assert to_iso("8/24/26") == "2026-08-24"


def test_ambiguous_short_slash_date_keeps_day_first_policy():
    assert to_iso("03/04/26") == "2026-04-03"


def test_malformed_money_is_not_partially_parsed():
    assert to_float("1.9603.40") is None
    assert _amounts_on_line("Less: Discount 1.9603.40", allow_bare_integers=True) == []


def test_tax_percentage_is_not_used_as_tax_amount():
    data={"subtotal":10500.0,"tax_amount":1260.0,"total_amount":11760.0}
    lines=[
        {"text":"VAT","bbox":box(10,100,60,120)},
        {"text":"12%","bbox":box(80,100,120,120)},
        {"text":"Tax Amount","bbox":box(10,140,100,160)},
        {"text":"1,260.00","bbox":box(130,140,210,160)},
    ]
    out,_=reconcile_financial_layout(data,lines)
    assert out["tax_amount"] == 1260.0


def test_north_star_false_vision_discount_is_rejected_and_identity_derives_zero():
    data={
        "subtotal":2468.0,"tax_amount":296.16,"discount":2468.0,
        "zero_rated_sales":None,"vat_exempt_sales":0.0,"total_amount":2764.16,
    }
    corrections={"discount":296.16}
    mismatches=[
        {"field":"subtotal","image_shows":"2,468.00"},
        {"field":"tax_amount","image_shows":"296.16"},
        {"field":"discount","image_shows":"296.16"},
        {"field":"total_amount","image_shows":"2,764.16"},
    ]
    out,locked,notes,accepted,rejected=apply_vision_corrections_with_financial_gate(data,corrections,mismatches)
    assert out["discount"] == 0.0
    assert "discount" in locked
    assert rejected["discount"] == 296.16
    assert out["subtotal"] + out["tax_amount"] - out["discount"] == out["total_amount"]


def test_gliptic_vision_core_wins_and_locks_against_layout_overwrite():
    data={
        "subtotal":180.0,"tax_amount":935.7,"discount":368.7,
        "zero_rated_sales":0.0,"vat_exempt_sales":0.0,"total_amount":747.0,
    }
    corrections={"subtotal":24464.29,"tax_amount":2935.71,"total_amount":27400.0}
    mismatches=[
        {"field":"subtotal","image_shows":"24,464.29"},
        {"field":"tax_amount","image_shows":"2,935.71"},
        {"field":"discount","image_shows":"-368.7"},
        {"field":"zero_rated_sales","image_shows":"0"},
        {"field":"vat_exempt_sales","image_shows":"0"},
        {"field":"total_amount","image_shows":"27,400.00"},
    ]
    out,locked,_,_,_=apply_vision_corrections_with_financial_gate(data,corrections,mismatches)
    assert out["subtotal"] == 24464.29
    assert out["tax_amount"] == 2935.71
    assert out["total_amount"] == 27400.0
    assert out["discount"] == 0.0
    assert {"subtotal","tax_amount","total_amount","discount"}.issubset(locked)

    # A weaker OCR layout now suggests the old wrong values; locks must win.
    lines=[
        {"text":"VATABLE SALES","bbox":box(10,100,120,120)},
        {"text":"180.00","bbox":box(150,100,220,120)},
        {"text":"VAT Amount","bbox":box(10,140,120,160)},
        {"text":"935.70","bbox":box(150,140,220,160)},
        {"text":"TOTAL AMOUNT DUE","bbox":box(10,180,130,200)},
        {"text":"747.00","bbox":box(150,180,220,200)},
    ]
    out2,_=reconcile_financial_layout(out,lines,locked_fields=locked)
    assert out2["subtotal"] == 24464.29
    assert out2["tax_amount"] == 2935.71
    assert out2["total_amount"] == 27400.0


def test_sycip_bad_vision_tax_rate_cannot_replace_balanced_tax_amount():
    data={
        "subtotal":10500.0,"tax_amount":1260.0,"discount":0.0,
        "zero_rated_sales":0.0,"vat_exempt_sales":0.0,"total_amount":11760.0,
    }
    corrections={"tax_amount":12.0}
    mismatches=[
        {"field":"subtotal","image_shows":"10,500.00"},
        {"field":"tax_amount","image_shows":"12.0"},
        {"field":"total_amount","image_shows":"11,760.00"},
    ]
    out,_,_,_,rejected=apply_vision_corrections_with_financial_gate(data,corrections,mismatches)
    assert out["tax_amount"] == 1260.0
    assert rejected["tax_amount"] == 12.0
