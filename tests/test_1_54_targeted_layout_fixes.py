from pathlib import Path

from ai.post_processing import (
    reconcile_statement_summary,
    reconcile_line_item_geometry,
    reconcile_customer_address_layout,
    clean_line_items,
)


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def line(text,x1,y1,x2,y2):
    return {"text":text,"bbox":box(x1,y1,x2,y2),"confidence":0.99,"engine":"paddleocr"}


def test_statement_summary_derives_globe_discount_and_keeps_addons_as_item():
    lines=[
        line("Statement Summary",1100,1000,1500,1040),
        line("Monthly Plan",1200,1200,1400,1240),
        line("2,499.00",2000,1220,2150,1260),
        line("Add-ons",1200,1260,1350,1300),
        line("99.00",2020,1280,2130,1320),
        line("Discounts",1200,1300,1360,1340),
        line("Total",1200,1360,1300,1400),
        line("Php 2,198.00",1980,1380,2200,1430),
        line("Previous Bill Activity",1100,1500,1500,1540),
    ]
    data={"subtotal":2499.0,"discount":99.0,"total_amount":2198.0,
          "line_items":[{"description":"Biz BB Plan 2499 50Mbps Unli","quantity":1,"unit_price":2499,"amount":2499},
                        {"description":"Discounts","quantity":1,"unit_price":99,"amount":99}]}
    out,notes=reconcile_statement_summary(data,lines)
    out,_=clean_line_items(out)
    assert out["subtotal"]==2598.0
    assert out["discount"]==400.0
    assert out["total_amount"]==2198.0
    assert all(i["description"].lower() != "discounts" for i in out["line_items"])
    assert any(i["description"]=="Add-ons" and i["amount"]==99.0 for i in out["line_items"])
    assert notes


def test_tri_q_utility_service_same_row_geometry_corrects_unit_and_total_price():
    lines=[
        line("NATURE OF SERVICE",600,1080,1020,1130),
        line("QUANTITY",1460,1080,1660,1130),
        line("UNIT COST",1730,1080,1950,1130),
        line("AMOUNT",2050,1080,2220,1130),
        line("UTILITY SERVICES",155,1425,583,1472),
        line("96,279.67",2048,1425,2292,1487),
        line("VATable Sales",118,2466,336,2506),
        line("85,963.99",605,2444,801,2484),
    ]
    data={"line_items":[{"description":"UTILITY SERVICES","quantity":1.0,"unit_price":85963.99,"amount":85963.99}]}
    out,notes=reconcile_line_item_geometry(data,lines)
    assert out["line_items"][0]["unit_price"]==96279.67
    assert out["line_items"][0]["amount"]==96279.67
    assert notes


def test_customer_address_geometry_excludes_terms_and_po_ref():
    lines=[
        line("ADDRESS:",100,840,350,890),
        line("Unit 2102, 21st Floor, One Corporate Center",390,830,1500,880),
        line("Meralco Ave. Ortigas",400,875,800,925),
        line("Terms :",1690,800,1860,850),
        line("30 Days",1840,805,2030,855),
        line("PO Ref No.:",1600,865,1840,915),
        line("TIN NO",100,905,280,960),
    ]
    out,notes=reconcile_customer_address_layout({"customer_address":"Meralco Ave. Ortigas"},lines)
    assert "Unit 2102" in out["customer_address"]
    assert "Meralco Ave. Ortigas" in out["customer_address"]
    assert "PO Ref" not in out["customer_address"]
    assert "30 Days" not in out["customer_address"]
    assert notes


def test_discounts_summary_row_is_not_item_but_addons_survives():
    data={"line_items":[
        {"description":"Discounts","quantity":1,"unit_price":400,"amount":400},
        {"description":"Add-ons","quantity":1,"unit_price":99,"amount":99},
    ]}
    out,_=clean_line_items(data)
    assert [x["description"] for x in out["line_items"]]==["Add-ons"]
