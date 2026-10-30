"""Regression tests for saved per-field/per-line-item evidence indicators."""
from ai.field_evidence import DOCUMENT_FIELDS, ITEM_FIELDS, build_field_evidence, mark_user_changes
from ai.confidence import score_extraction
from ai.field_display import indicator, evidence_for_invoice


def sample():
    return {
        "invoice_number": "IV-43", "invoice_date": "2026-10-20",
        "vendor_name": "SM SUPERMALLS", "vendor_address": "Makati City",
        "customer_name": "JUAN CRUZ", "total_amount": 1680.00, "subtotal": 1500.0,
        "tax_amount": 180.0, "currency": "PHP", "category": "Shopping",
        "line_items": [
            {"description": "Backpack", "quantity": 1, "unit_price": 1500.0, "amount": 1500.0},
            {"description": "Sneakers", "quantity": 1, "unit_price": 0.0, "amount": 0.0},
        ],
    }


def ocr():
    return "SM SUPERMALLS\nIV-43\n20 Oct 2026\nMakati City\nJUAN CRUZ\nBackpack 1 1500.00 1500.00\nSneakers 1 0.00 0.00\nVAT 180.00\nTotal 1680.00"


def test_all_invoice_fields_and_all_line_item_cells_scored():
    evidence = build_field_evidence(sample(), ocr())
    assert set(DOCUMENT_FIELDS).issubset(evidence)
    for index in (0, 1):
        for field in ITEM_FIELDS:
            assert f"line_items.{index}.{field}" in evidence
    assert evidence["vendor_name"]["status"] == "supported"
    assert evidence["line_items.0.unit_price"]["score"] >= .85
    assert evidence["line_items.1.amount"]["score"] >= .85
    assert evidence["category"]["status"] == "derived"
    assert evidence["vendor_tax_id"]["status"] == "not_provided"
    assert evidence["vendor_tax_id"]["warning"] == ""


def test_absent_ocr_support_warns_and_is_not_confident_by_default():
    data = sample()
    data["customer_name"] = "NOT A NAME ON THE DOCUMENT"
    evidence = build_field_evidence(data, ocr())
    assert evidence["customer_name"]["score"] < .7
    assert evidence["customer_name"]["warning"]
    assert evidence["customer_name"]["source"] == "unverified"


def test_item_price_other_row_not_counted_as_evidence_for_wrong_row():
    data = sample()
    data["line_items"][0]["unit_price"] = 800.0
    data["line_items"][0]["amount"] = 800.0
    text = "Backpack 1 1500.00 1500.00\n" + "\n" * 5 + "Shoes 1 800.00 800.00"
    evidence = build_field_evidence(data, text)
    assert evidence["line_items.0.unit_price"]["score"] < .7
    assert evidence["line_items.0.unit_price"]["warning"]


def test_line_item_math_inconsistency_downgrades_both_cells():
    data = sample()
    data["line_items"][0]["amount"] = 1000.0
    evidence = build_field_evidence(data, ocr())
    assert "does not match" in evidence["line_items.0.amount"]["warning"]
    assert evidence["line_items.0.amount"]["score"] < .75


def test_validation_warning_attaches_to_affected_field():
    ev = build_field_evidence(sample(), ocr(), validation_issues=["VAT amount inconsistent with taxable sales"])
    assert ev["tax_amount"]["status"] == "review"
    assert ev["tax_amount"]["warning"]
    assert not ev["vendor_name"]["warning"]


def test_score_extraction_exposes_actual_item_values_and_support_map():
    d = sample()
    prediction = score_extraction(d, .9, [], ocr_engine="paddleocr", ocr_text=ocr())
    path = "line_items.0.unit_price"
    assert any(fc.field == path and fc.value == 1500.0 for fc in prediction.field_confidences)
    assert prediction.field_evidence[path]["status"] == "supported"
    assert 0 <= prediction.overall_confidence <= 1


def test_manual_edits_invalidate_changed_cells_and_preserve_untouched_values():
    d = sample()
    before = {**d, "field_evidence": build_field_evidence(d, ocr())}
    revised = [dict(li) for li in d["line_items"]]
    revised[0]["amount"] = 1499.0
    revised[1]["description"] = "Canvas Sneakers"
    updated = mark_user_changes(before, {"line_items": revised, "customer_name": "OTHER PERSON"}, before["field_evidence"])
    assert updated["customer_name"]["score"] is None
    assert updated["line_items.0.amount"]["source"] == "user_edit"
    assert updated["line_items.0.unit_price"]["source"] != "user_edit"
    # Description changed, so all cells on that row must be invalidated.
    for field in ITEM_FIELDS:
        assert updated[f"line_items.1.{field}"]["source"] == "user_edit"


def test_history_old_record_indicators_explicitly_identified():
    d = sample()
    d["raw_text"] = ocr()
    d["field_evidence"] = {}
    generated, historic = evidence_for_invoice(d)
    assert historic and generated["vendor_name"]["score"] is not None
    assert indicator(generated["line_items.0.unit_price"])
    d["field_evidence"] = generated
    saved, historic = evidence_for_invoice(d)
    assert not historic and saved == generated


def test_evidence_json_roundtrip_repository(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database import models as _models
    from database.repository import InvoiceRepository
    from models.invoice import Invoice, LineItem
    from services.invoice_service import _orm_to_dict
    engine = create_engine(f"sqlite:///{tmp_path / 'confidence.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        original = Invoice(vendor_name="EXAMPLE", invoice_number="TEST-CONF-21", currency="PHP",
                           total_amount=25.0, line_items=[LineItem(description="Pen", quantity=1, unit_price=25, amount=25)],
                           raw_text="EXAMPLE\nTEST-CONF-21\nPen 1 25 25\n25")
        input_data = original.model_dump(mode="json")
        original.field_evidence = build_field_evidence(input_data, original.raw_text)
        orm = InvoiceRepository(session).save(original)
        detail = _orm_to_dict(orm)
        assert detail["field_evidence"]["line_items.0.amount"]["score"] is not None
        assert detail["field_evidence"]["vendor_name"]["source"] == "ocr_text"
        saved_id = orm.id
        rows = [dict(i) for i in detail["line_items"]]
        rows[0]["amount"] = 24.0
        updates = {"line_items": rows}
        changed = mark_user_changes(detail, updates, detail["field_evidence"])
        updated = InvoiceRepository(session).update(saved_id, {**updates, "field_evidence": changed})
        assert _orm_to_dict(updated)["field_evidence"]["line_items.0.amount"]["status"] == "manual_edit"
    finally:
        session.close()
        engine.dispose()
