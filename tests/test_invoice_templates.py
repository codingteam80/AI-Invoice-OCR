import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.invoice_templates import get_template
from ai.validator import validate_extraction
from utils.invoice_template_detector import detect_invoice_template


def test_detect_watsons_template():
    text = """
    WATSONS PERSONAL CARE STORES
    Sales invoice No.: SI12345
    VAT REG TIN# 123-456-789
    TOTAL DISCOUNTS 50.00
    Amount to Pay 1,070.00
    """
    assert detect_invoice_template(text) == "watsons"


def test_detect_generic_template():
    assert detect_invoice_template("ACME OFFICE SUPPLIES Invoice 123") == "generic"


def test_watsons_prompt_is_injected():
    from ai.prompt_builder import build_extraction_prompt
    prompt = build_extraction_prompt("WATSONS PERSONAL CARE STORES", "watsons")
    assert "WATSONS-SPECIFIC INVOICE FORMAT" in prompt
    assert 'TOTAL DISCOUNTS' in prompt
    assert '185TH POUCH BAG WITH SHOPPING BAG' in prompt


def test_watsons_special_line_item_normalization():
    template = get_template("watsons")
    data = {
        "vendor_name": "WATSONS PERSONAL CARE STORES",
        "line_items": [
            {"description": "185TH POUCH BAG WITH PO.01V SHOPPING BAG", "quantity": 1, "unit_price": 0.01, "amount": 0.01},
            {"description": "SHAMPOO", "quantity": 1, "unit_price": 100, "amount": 100},
        ],
    }
    result, notes = template["post_process"](data, "WATSONS PERSONAL CARE STORES")
    assert result["vendor_name"] == "watsons"
    assert result["category"] == "Medical & Health Supplies"
    assert result["line_items"][0]["description"] == "185TH POUCH BAG WITH SHOPPING BAG"
    assert result["line_items"][0]["unit_price"] == 0.01
    assert result["line_items"][0]["amount"] == 0.01


def test_watsons_separate_shopping_bag_is_merged():
    template = get_template("watsons")
    data = {
        "line_items": [
            {"description": "185TH POUCH BAG WITH", "quantity": 1, "unit_price": 0.01, "amount": 0.01},
            {"description": "SHOPPING BAG", "quantity": 1, "unit_price": 0.01, "amount": 0.01},
        ],
    }
    result, _ = template["post_process"](data, "WATSONS PERSONAL CARE STORES")
    assert len(result["line_items"]) == 1
    assert result["line_items"][0]["description"] == "185TH POUCH BAG WITH SHOPPING BAG"


def test_watsons_labeled_financial_fields():
    template = get_template("watsons")
    ocr = """
    WATSONS PERSONAL CARE STORES
    VAT SALE 1,339.29 160.71
    ZERO RATED SALE 0.00
    VAT EXEMPT SALE 0.00
    TOTAL DISCOUNTS 50.00
    Amount to Pay 1,450.00
    """
    result, _ = template["post_process"]({"line_items": []}, ocr)
    assert result["vendor_name"] == "watsons"
    assert result["category"] == "Medical & Health Supplies"
    assert result["subtotal"] == 1339.29
    assert result["tax_amount"] == 160.71
    assert result["discount"] == 50.00
    assert result["total_amount"] == 1450.00
    assert result["zero_rated_sales"] == 0.00
    assert result["vat_exempt_sales"] == 0.00



def test_watsons_financial_values_use_labeled_tax_table_and_amount_to_pay():
    template = get_template("watsons")
    ocr = """
    WATSONS PERSONAL CARE STORES
    SUBTOTAL P342.50
    AMOUNT TO PAY P342.50
    CASH P1000.00
    CHANGE P657.50
    TOTAL DISCOUNTS P0.01
    TAX CODE AMOUNT VAT AMT
    VAT SALE
    VAT EXEMPT SALE
    ZERO RATED SALE
    TOTAL
    305.80
    0.00
    0.00
    305.80
    36.70
    0.00
    0.00
    36.70
    CASHIER NAME: ACASTO
    """
    data = {
        "subtotal": 342.50,
        "tax_amount": 36.70,
        "discount": 0.01,
        "total_amount": 1000.00,
        "line_items": [
            {"description": "SQUALENE ISSHO SOFTG", "quantity": 10, "unit_price": 11.75, "amount": 117.50},
            {"description": "ATC FISH OIL SOFTGEL", "quantity": 1, "unit_price": 225.00, "amount": 225.00},
            {"description": "185TH POUCH BAG WITH", "quantity": 1, "unit_price": 0.01, "amount": 0.01},
        ],
    }
    result, _ = template["post_process"](data, ocr)
    assert result["subtotal"] == 305.80
    assert result["tax_amount"] == 36.70
    assert result["discount"] == 0.01
    assert result["total_amount"] == 342.50


def test_watsons_financial_validation_passes_for_realistic_receipt():
    from ai.validator import validate_extraction
    data = {
        "invoice_number": "0000489793",
        "invoice_date": "2026-08-16",
        "vendor_name": "watsons",
        "total_amount": 342.50,
        "currency": "PHP",
        "subtotal": 305.80,
        "tax_amount": 36.70,
        "discount": 0.01,
        "line_items": [
            {"description": "SQUALENE ISSHO SOFTG", "amount": 117.50},
            {"description": "ATC FISH OIL SOFTGEL", "amount": 225.00},
            {"description": "185TH POUCH BAG WITH SHOPPING BAG", "amount": 0.01},
        ],
    }
    issues = validate_extraction(data, template_name="watsons")
    assert not any("Watsons validation" in issue for issue in issues)
    assert not any("line_items sum to" in issue for issue in issues)


def test_watsons_financial_validation_flags_inconsistent_values():
    from ai.validator import validate_extraction
    data = {
        "invoice_number": "0000489793",
        "invoice_date": "2026-08-16",
        "vendor_name": "watsons",
        "total_amount": 1000.00,
        "currency": "PHP",
        "subtotal": 342.50,
        "tax_amount": 36.70,
        "discount": 0.01,
        "line_items": [
            {"description": "A", "amount": 117.50},
            {"description": "B", "amount": 225.00},
            {"description": "185TH POUCH BAG WITH SHOPPING BAG", "amount": 0.01},
        ],
    }
    issues = validate_extraction(data, template_name="watsons")
    assert any("Net Amount (Vatable Sales)" in issue for issue in issues)
    assert any("Total Amount Due" in issue for issue in issues)
    assert any("line-item Total Unit Price" in issue for issue in issues)


def test_watsons_financial_fields_ignore_cash_and_discount_column_misalignment():
    template = get_template("watsons")
    ocr = """WATSONS PERSONAL CARE STORES\nAMOUNT TO PAY\n342.50\nCASH\nCHANGE\nP1000.00\nP657.50\nYOU SAVED:\nTOTAL DISCOUNTS\nP0.01\nTAX CODE AMOUNT VAT AMT\nVAT SALE\nVAT EXEMPT SALE\nZERO RATED SALE\nTOTAL\n305.80\n0.00\n0.00\n305.80\n36.70\n0.00\n0.00\n36.70\n"""
    data = {"total_amount": 1000.0, "discount": 305.8, "subtotal": 342.5, "tax_amount": 36.7,
            "line_items": [{"description":"A","quantity":10,"unit_price":11.75,"amount":117.5},
                            {"description":"B","quantity":1,"unit_price":225,"amount":225},
                            {"description":"185TH POUCH BAG WITH","quantity":1,"unit_price":0.01,"amount":0.01}]}
    result, _ = template["post_process"](data, ocr)
    assert result["discount"] == 0.01
    assert result["subtotal"] == 305.80
    assert result["tax_amount"] == 36.70
    assert result["total_amount"] == 342.50


def test_plate_label_is_extracted_only_from_explicit_label():
    from ai.post_processing import reconcile_plate_number
    result, _ = reconcile_plate_number({}, "Ticket No: 123456789\nPlate #: ABC 1234\nTransaction: 999999")
    assert result["plate_number"] == "ABC 1234"


def test_plate_not_inferred_from_unlabelled_identifier():
    from ai.post_processing import reconcile_plate_number
    result, _ = reconcile_plate_number({}, "Ticket No: ABC1234\nTransaction: 999999")
    assert result.get("plate_number") is None


def test_invoice_model_has_optional_plate_number():
    from models.invoice import Invoice
    inv = Invoice(invoice_number="T-1", vendor_name="Parking", plate_number="ABC 1234")
    assert inv.plate_number == "ABC 1234"


def test_export_contains_plate_number_column():
    from exports.common import EXPORT_COLUMNS, invoice_export_row
    assert "Plate #" in EXPORT_COLUMNS
    row = invoice_export_row({"id": 1, "invoice_number": "T-1", "vendor_name": "Parking", "plate_number": "ABC 1234"})
    assert row["Plate #"] == "ABC 1234"


def test_watsons_real_sample_values_are_fixed():
    template = get_template("watsons")
    ocr = """WATSONS PERSONAL CARE STORES
VAT REG TIN# 214-706-591-029
SQUALENE ISSHO SOFTG 10 P11.75 P117.50
ATC FISH OTL SOFTGEL P225.00 V
185TH POUCH BAG WITH PO.01 V
SHOPPING BAG -PO.01
SUBTOTAL. P342.50
AMOUNT TO PAY P342.50
CASH P1000..00
CHANGE P657 50
YOU SAVED:
OTAL DISCOUNTS PO.01
TAX CODE AMOUNT VAT AMT
VAT SALE 305,80 36.70
VAT EXEMPT SALE 0.00 0.00
ZERO RATED SALE 0.00 0.00
TOTAL 305.80 36.70
Sales Invoice No,; 0000489793
"""
    data = {
        "invoice_number": "0000489793", "invoice_date": "2026-08-16",
        "vendor_name": "watsons", "currency": "PHP",
        "subtotal": 342.50, "tax_amount": 36.70, "discount": 305.80,
        "total_amount": 1000.00,
        "line_items": [
            {"description": "SQUALENE ISSHO SOFTG", "quantity": 10, "unit_price": 11.75, "amount": 117.50},
            {"description": "ATC FISH OTL SOFTGEL", "quantity": 1, "unit_price": 225.00, "amount": 225.00},
            {"description": "185TH POUCH BAG WITH PO.01 V", "quantity": 1, "unit_price": 0.01, "amount": 0.01},
        ],
    }
    result, _ = template["post_process"](data, ocr)
    assert result["subtotal"] == 305.80
    assert result["tax_amount"] == 36.70
    assert result["discount"] == 0.01
    assert result["total_amount"] == 342.50
    assert result["line_items"][-1]["description"] == "185TH POUCH BAG WITH SHOPPING BAG"
    assert result["line_items"][-1]["amount"] == 0.01
    assert validate_extraction(result, template_name="watsons") == []
