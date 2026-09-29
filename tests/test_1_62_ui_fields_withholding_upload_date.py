from datetime import datetime
from ai.post_processing import reconcile_financial_layout
from exports.common import EXPORT_COLUMNS, invoice_export_row


def _box(x1,y1,x2,y2): return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
def _line(text,x1,y1,x2,y2): return {"text":text,"confidence":.99,"bbox":_box(x1,y1,x2,y2),"engine":"paddleocr"}

def test_withholding_is_separate_from_discount():
    data={"subtotal":85963.99,"tax_amount":10315.68,"discount":None,"zero_rated_sales":None,"vat_exempt_sales":None,"total_amount":94560.39}
    lines=[_line("VATABLE SALES",10,10,180,30),_line("85,963.99",300,10,390,30),_line("VAT Amount",10,40,180,60),_line("10,315.68",300,40,390,60),_line("Zero Rated Sales",10,70,180,90),_line("VAT-Exempt Sales",10,100,180,120),_line("Less Withholding Tax",10,160,180,180),_line("1,719.28",300,160,390,180),_line("TOTAL AMOUNT DUE",10,190,180,210),_line("94,560.39",300,190,390,210)]
    out,_=reconcile_financial_layout(data,lines)
    assert out["withholding_tax"] == 1719.28
    assert out["discount"] == 0.0

def test_export_renames_vatable_hides_discount_and_adds_upload_date_wht():
    assert "Vatable Sales" in EXPORT_COLUMNS
    assert "Net Amount" not in EXPORT_COLUMNS
    assert "Discount" not in EXPORT_COLUMNS
    assert "Withholding Tax" in EXPORT_COLUMNS
    assert "Date Uploaded" in EXPORT_COLUMNS
    row=invoice_export_row({"subtotal":100,"withholding_tax":5,"discount":7,"created_at":"2026-09-17T08:00:00","invoice_date":"2026-08-01"})
    assert row["Vatable Sales"] == 100
    assert row["Withholding Tax"] == 5
    assert row["Date Uploaded"] == "2026-09-17"
    assert "Discount" not in row
