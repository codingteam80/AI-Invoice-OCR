from ai.post_processing import reconcile_financial_layout


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def line(text,x1,y1,x2,y2):
    return {"text":text,"confidence":0.99,"bbox":box(x1,y1,x2,y2),"engine":"paddleocr"}


def test_layout_financial_corrects_page5_discount_hallucination():
    lines=[
        line("VATABLE SALES",10,10,180,30), line("2,468.00",300,10,390,30),
        line("VAT Amount",10,40,180,60), line("296.16",300,40,390,60),
        line("ZERO RATED SALES",10,70,180,90),
        line("VAT EXEMPT SALES",10,100,180,120),
        line("Less: Discount",10,130,180,150),
        line("Total Amount Due",10,160,180,180), line("2,764.16",300,160,390,180),
    ]
    data={"subtotal":2468.0,"tax_amount":296.16,"zero_rated_sales":0,"vat_exempt_sales":0,"discount":2468.0,"total_amount":2764.16}
    fixed,notes=reconcile_financial_layout(data,lines)
    assert fixed["discount"] == 0.0
    assert fixed["subtotal"] == 2468.0
    assert fixed["tax_amount"] == 296.16
    assert fixed["total_amount"] == 2764.16


def test_layout_financial_page4_withholding_and_no_zero_contamination():
    lines=[
        line("VATABLE SALES",10,10,180,30), line("85,963.99",300,10,390,30),
        line("VAT Amount",10,40,180,60), line("10,315.68",300,40,390,60),
        line("Zero Rated Sales",10,70,180,90),
        line("VAT-Exempt Sales",10,100,180,120),
        line("Total Sales",10,130,180,150), line("96,279.67",300,130,390,150),
        line("Less Withholding Tax",10,160,180,180), line("1,719.28",300,160,390,180),
        line("TOTAL AMOUNT DUE",10,190,180,210), line("94,560.39",300,190,390,210),
    ]
    data={"subtotal":85963.99,"tax_amount":10315.68,"zero_rated_sales":96279.67,"vat_exempt_sales":None,"discount":None,"total_amount":94560.39}
    fixed,notes=reconcile_financial_layout(data,lines)
    assert fixed["zero_rated_sales"] == 0.0
    assert fixed["vat_exempt_sales"] == 0.0
    assert fixed["discount"] == 1719.28
    assert fixed["total_amount"] == 94560.39
