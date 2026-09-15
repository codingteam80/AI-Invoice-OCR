from ai.post_processing import reconcile_invoice_date, add_remaining_balance_line_item
from ai.confidence import score_extraction
from utils.categorizer import auto_categorize


def test_triq_header_date_owns_invoice_date_over_printer_date_issued():
    ocr = """SERVICE INVOICE
No. 3219
Date:
13-Aug-26
Date Issued
04-04-2025
"""
    data, notes = reconcile_invoice_date({"invoice_date": "2025-04-04"}, ocr)
    assert data["invoice_date"] == "2026-08-13"
    assert any("1.58 invoice-date ownership" in n for n in notes)


def test_nonzero_remaining_balance_is_added_to_items_but_zero_is_not():
    base = {
        "previous_balance": 1749.51,
        "line_items": [
            {"description": "GFiber Biz Plus", "quantity": 1, "unit_price": 3499.0, "amount": 3499.0},
            {"description": "Add-ons", "quantity": 1, "unit_price": 758.0, "amount": 758.0},
        ],
    }
    out, _ = add_remaining_balance_line_item(base)
    rb = [x for x in out["line_items"] if x["description"] == "Remaining Balance"]
    assert len(rb) == 1
    assert rb[0]["quantity"] == 1.0
    assert rb[0]["unit_price"] == 1749.51
    assert rb[0]["amount"] == 1749.51

    zero, _ = add_remaining_balance_line_item({"previous_balance": 0.0, "line_items": []})
    assert zero["line_items"] == []


def test_parking_and_watsons_categories():
    assert auto_categorize(
        "SM DEVELOPMENT CORPORATION", [{"description": "PARKING FEE"}]
    ) == "Transportation"
    assert auto_categorize(
        "WATSONS PERSONAL CARE STORES PHILIPPINES INC", []
    ) == "Medical & Health Supplies"


def test_final_confidence_rewards_reconciled_consistency_not_only_ocr():
    data = {
        "invoice_number": "IN000971069973",
        "invoice_date": "2026-08-05",
        "vendor_name": "Globe Business: Innove Communications, Inc.",
        "vendor_tax_id": "000-360-916-00000",
        "customer_name": "TSUKIDEN GLOBAL SOLUTIONS INC.",
        "subtotal": 2598.0,
        "tax_amount": 0.0,
        "discount": 400.0,
        "zero_rated_sales": 0.0,
        "vat_exempt_sales": 0.0,
        "current_charges_total": 2198.0,
        "previous_balance": 0.0,
        "total_amount": 2198.0,
        "currency": "PHP",
        "line_items": [
            {"description": "Biz BB Plan", "quantity": 1, "unit_price": 2499.0, "amount": 2499.0},
            {"description": "Add-ons", "quantity": 1, "unit_price": 99.0, "amount": 99.0},
        ],
    }
    pred = score_extraction(data, 0.72, [], ocr_engine="paddleocr")
    assert pred.overall_confidence >= 0.85
    assert pred.needs_review is False


def test_handwriting_still_forces_review_under_new_confidence_semantics():
    data = {
        "invoice_number": "X1",
        "invoice_date": "2026-08-01",
        "vendor_name": "Vendor",
        "subtotal": 100.0,
        "tax_amount": 12.0,
        "discount": 0.0,
        "zero_rated_sales": 0.0,
        "vat_exempt_sales": 0.0,
        "total_amount": 112.0,
        "currency": "PHP",
        "line_items": [],
    }
    printed = score_extraction(data, 0.90, [], ocr_engine="paddleocr")
    pred = score_extraction(data, 0.90, [], ocr_engine="paddleocr+trocr")
    assert pred.needs_review is True
    assert pred.overall_confidence < printed.overall_confidence


def test_category_persists_through_repository_save_and_read():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database import models as _models  # register tables
    from database.repository import InvoiceRepository
    from models.invoice import Invoice, LineItem

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        inv = Invoice(
            invoice_number="PK-158-1",
            invoice_date="2026-09-14",
            vendor_name="SM DEVELOPMENT CORPORATION",
            line_items=[LineItem(description="PARKING FEE", quantity=1, unit_price=220, amount=220)],
            subtotal=196.43,
            tax_amount=23.57,
            total_amount=220.0,
            currency="PHP",
            category="Transportation",
        )
        saved = InvoiceRepository(session).save(inv)
        assert saved.category == "Transportation"
        fetched = InvoiceRepository(session).get_by_id(saved.id)
        assert fetched.category == "Transportation"
    finally:
        session.close()
