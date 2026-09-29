"""1.71: regression tests built from the REAL wrong outputs of the 5 Tsukiden invoices."""
from ai.party_master import apply_final_master_pass

CUST_TIN = "007-848-122-000"

def _run(data, ocr=""):
    return apply_final_master_pass(data, ocr)[0]


def test_sgv_vendor_address_isolated_date_and_expense_recovered():
    data = dict(
        vendor_name="SGV: SYCIP, GORRES, VELAYO & CO.", vendor_tax_id="000-502-547-00000",
        vendor_address="6760 Ayala Avenue, San Lorenzo, NCR,Fourth District, 1226 City of Makati, Philippines, Building a better, J Vargas cor Meralco Ave, Brgy San Antonio Ortigas Center",
        customer_name="TSUKIDEN GLOBAL SOLUTIONS INC.", customer_tax_id=CUST_TIN,
        customer_address="U 2102 21/F One Corporate Ctr Condo, J Vargas cor Meralco Ave, Brgy San Antonio, Ortigas Center, Pasig City 1605",
        invoice_date=None, subtotal=10500.0, tax_amount=1260.0, total_amount=11760.0,
        line_items=[dict(description="Our retainer fee for the month of August 2026", quantity=1, unit_price=10000.0, amount=10000.0)],
    )
    ocr = "Document No.\nIssue Date:\nDue Date:\nAugust 06, 2026\nSeptember 05, 2026\nDate Issued: July 1, 2025\nFee\n10,000.00\nExpense\n500.00\n"
    out = _run(data, ocr)
    assert out["vendor_address"] == "6760 Ayala Avenue, San Lorenzo, NCR, Fourth District, 1226 City of Makati, Philippines"
    assert out["invoice_date"] == "2026-08-06"
    assert [round(i["amount"], 2) for i in out["line_items"]] == [10000.0, 500.0]
    assert out["line_items"][1]["description"] == "Expense"


def test_globe_vendor_address_and_discount_recovered():
    data = dict(
        vendor_tax_id="000-360-916-00000",
        vendor_address="32nd Street comr 7thAvn, 9/ The Gobe TowerCebu Samar Loop co Pana Road, Ortigas,Pasig, Vargas Cor Meralco Ave Ortigas Center",
        customer_name="Tsukiden Global Solutions Inc", customer_tax_id="007-848-122-00000",
        customer_address="2101 One Corporate Center Julia Vargas Cor Meralco Ave Ortigas Center, Ortigas,Pasig, Metro Manila,1605",
        subtotal=2598.0, tax_amount=0.0, total_amount=2198.0,
        line_items=[dict(description="Biz BB Plan 2499 50Mbps Unli", quantity=1, unit_price=2499.0, amount=2499.0),
                    dict(description="Add-ons", quantity=1, unit_price=99.0, amount=99.0)],
    )
    ocr = "Monthly Recurring Fee (MRF)\nMonthly Plan\nAdd-ons\nDiscounts\n2,198.00\n2,499.00\n99.00\n(400.00)\nTotal\nPhp 2,198.00\n"
    out = _run(data, ocr)
    assert out["vendor_address"] == "32nd Street corner 7th Avenue, Bonifacio Global City, Taguig, Philippines 1634"
    assert [round(i["amount"], 2) for i in out["line_items"]] == [2499.0, 99.0, -400.0]
    assert out["line_items"][2]["description"].lower().startswith("discount")
    assert out["subtotal"] == 2198.0


def test_triq_vendor_address():
    data = dict(vendor_tax_id="002-176-366-00000", customer_name="TSUKIDEN GLOBAL SOLUTIONS, INC.", customer_tax_id=CUST_TIN,
                vendor_address="# 79 P. Cruz Street,Brgy. Old Zaniga,., 1550 City of Mandaluyong NCR, Second District Philippines, 21st Floor One Corporate Center, Julia Vargas, Corner Meralco Ave..",
                customer_address="21st Floor One Corporate Center, Julia Vargas Corner Meralco Ave., Ortigas Center, Pasig City")
    out = _run(data)
    assert out["vendor_address"] == "# 79 P. Cruz Street, Brgy. Old Zaniga, 1550 City of Mandaluyong NCR, Second District Philippines"
    assert out["customer_address"].startswith("21st Floor One Corporate Center")


def test_northstar_52091_typos_and_unit_bleed():
    data = dict(vendor_tax_id="000-423-215-00000", customer_name="TSUKIDEN GLOBAL SOLUTIONS INC.", customer_tax_id=CUST_TIN,
                vendor_address="19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air, 1209 City of Makati NCR, Fourth District Philippines., Unit 2102",
                customer_address="Unit 2102, 21st Floor, One Corporate Centre, Dona Julla Vargas Avenue comer, Meralco Ave. Ortigas")
    out = _run(data)
    assert out["vendor_address"] == "19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air, 1209 City of Makati NCR, Fourth District Philippines"
    assert out["customer_address"] == "Unit 2102, 21st Floor, One Corporate Centre, Dona Julia Vargas Avenue corner Meralco Ave. Ortigas"


def test_northstar_44072_swapped_garbled_customer_address():
    data = dict(vendor_tax_id="000-423-215-00000", customer_name="TSUKIDEN GLOBAL SOLUTIONS INC.", customer_tax_id=CUST_TIN,
                vendor_address="19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air, 1209 City of Makati NCR, Fourth District Philippines, Unt 2102, 2ist Floor, Ore Corporate Cenire, Dona Julla Vargas Averue comer, Meraico Ave. Ortigas",
                customer_address="210 t e Ae coer, Meraico Ave. Ortigas")
    out = _run(data)
    assert out["vendor_address"] == "19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air, 1209 City of Makati NCR, Fourth District Philippines"
    assert out["customer_address"] == "Unit 2102, 21st Floor, One Corporate Centre, Dona Julia Vargas Avenue corner Meralco Ave. Ortigas"


def test_unknown_vendor_generic_isolation_keeps_own_address():
    data = dict(vendor_tax_id="111-222-333-00000", customer_name="TSUKIDEN GLOBAL SOLUTIONS INC.", customer_tax_id=CUST_TIN,
                vendor_address="5 Rizal Street, Quezon City, Metro Manila, J Vargas cor Meralco Ave, Brgy San Antonio Ortigas Center",
                customer_address="U 2102 21/F One Corporate Ctr Condo, J Vargas cor Meralco Ave, Brgy San Antonio, Ortigas Center, Pasig City 1605")
    assert _run(data)["vendor_address"] == "5 Rizal Street, Quezon City, Metro Manila"


def test_no_change_when_already_correct_and_no_false_recovery():
    data = dict(vendor_tax_id="002-176-366-00000", subtotal=85963.99, tax_amount=10315.68, total_amount=96279.67, withholding_tax=1719.28,
                vendor_address="# 79 P. Cruz Street, Brgy. Old Zaniga, 1550 City of Mandaluyong NCR, Second District Philippines",
                line_items=[dict(description="UTILITY SERVICES", quantity=1, unit_price=96279.67, amount=96279.67)])
    out = _run(data, "UTILITY SERVICES\n96,279.67\n")
    assert out["line_items"] == data["line_items"]
