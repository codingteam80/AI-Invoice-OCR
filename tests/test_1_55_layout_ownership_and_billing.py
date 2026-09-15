from ai.post_processing import (
    extract_statement_summary_evidence,
    reconcile_statement_summary,
    reconcile_billing_statement_balances,
    reconcile_line_item_geometry,
)
from ai.validator import validate_extraction
from models.invoice import Invoice


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_statement_summary_uses_row_ownership_not_absolute_page_width():
    lines=[
        {"text":"Statement Summary","bbox":box(420,430,600,450)},
        {"text":"Monthly Plan","bbox":box(455,520,540,540)},
        {"text":"3,499.00","bbox":box(800,520,860,540)},
        {"text":"Add-ons","bbox":box(455,544,510,560)},
        {"text":"758.00","bbox":box(810,544,855,560)},
        {"text":"Discounts","bbox":box(455,560,520,576)},
        {"text":"(59.00)","bbox":box(812,560,860,576)},
        {"text":"Total","bbox":box(443,584,485,600)},
        {"text":"Php4,198.00","bbox":box(785,584,875,600)},
        {"text":"Previous Bill Activity","bbox":box(437,634,575,650)},
    ]
    ev=extract_statement_summary_evidence(lines)
    assert ev["monthly_plan"] == 3499.0
    assert ev["add_ons"] == 758.0
    assert ev["discounts"] == 59.0
    assert ev["subtotal_before_discount"] == 4257.0
    assert ev["current_charges_total"] == 4198.0


def test_statement_summary_ignores_value_above_row_and_repairs_main_item():
    lines=[
        {"text":"Statement Summary","bbox":box(1138,1022,1555,1093)},
        {"text":"Monthly Recurring Fee (MRF)","bbox":box(1167,1190,1588,1254)},
        {"text":"2,198.00","bbox":box(2040,1219,2180,1272)},
        {"text":"Monthly Plan","bbox":box(1200,1230,1396,1283)},
        {"text":"2,499.00","bbox":box(1998,1256,2134,1305)},
        {"text":"Add-ons","bbox":box(1205,1270,1330,1320)},
        {"text":"99.00","bbox":box(2041,1292,2131,1346)},
        {"text":"Discounts","bbox":box(1199,1311,1343,1352)},
        {"text":"(0000)","bbox":box(2011,1329,2138,1386)},
        {"text":"Total","bbox":box(1169,1361,1264,1411)},
        {"text":"Php 2,198.00","bbox":box(1958,1384,2182,1448)},
        {"text":"Previous Bill Activity","bbox":box(1142,1475,1481,1539)},
    ]
    data={"line_items":[{"description":"Biz BB Plan 2499 50Mbps Unli","quantity":1,"unit_price":82626,"amount":82626}]}
    out,_=reconcile_statement_summary(data,lines)
    assert out["subtotal"] == 2598.0
    assert out["discount"] == 400.0
    assert out["current_charges_total"] == 2198.0
    main=next(i for i in out["line_items"] if i["description"].startswith("Biz BB"))
    addon=next(i for i in out["line_items"] if i["description"] == "Add-ons")
    assert main["unit_price"] == 2499.0 and main["amount"] == 2499.0
    assert addon["unit_price"] == 99.0 and addon["amount"] == 99.0


def test_billing_statement_separates_previous_balance_from_current_charges():
    lines=[
        {"text":"Previous Bill Activity","bbox":box(437,634,575,650)},
        {"text":"Remaining Balance (Due Immediately)","bbox":box(442,738,701,754)},
        {"text":"Php1,749.51","bbox":box(785,738,873,755)},
        {"text":"Amount to pay","bbox":box(435,781,539,798)},
        {"text":"Php5,947.51","bbox":box(785,782,873,798)},
    ]
    out,notes=reconcile_billing_statement_balances({"current_charges_total":4198.0,"total_amount":4198.0},lines)
    assert out["previous_balance"] == 1749.51
    assert out["total_amount"] == 5947.51
    assert any("separated" in n.lower() for n in notes)


def test_item_geometry_never_turns_due_date_into_price():
    lines=[
        {"text":"Biz BB Plan 2499 50Mbps Unli","bbox":box(500,500,900,540)},
        {"text":"08/26/26","bbox":box(1500,500,1700,540)},
    ]
    data={"line_items":[{"description":"Biz BB Plan 2499 50Mbps Unli","quantity":1,"unit_price":2499,"amount":2499}]}
    out,_=reconcile_line_item_geometry(data,lines)
    assert out["line_items"][0]["amount"] == 2499


def test_billing_validation_uses_current_charges_not_amount_to_pay():
    data={
        "invoice_number":"X1","invoice_date":"2026-08-05","vendor_name":"Vendor",
        "subtotal":4257.0,"tax_amount":0.0,"discount":59.0,"zero_rated_sales":0.0,"vat_exempt_sales":0.0,
        "current_charges_total":4198.0,"previous_balance":1749.51,"total_amount":5947.51,"currency":"PHP","line_items":[],
    }
    issues=validate_extraction(data,ocr_text="4,198.00 5,947.51")
    assert not any("does not match current_charges_total" in x for x in issues)
    assert not any("does not match total_amount/Amount to Pay" in x for x in issues)
    inv=Invoice(**data)
    assert inv.validate_totals()
