"""QA regression coverage independent of LLM nondeterminism and model downloads."""
import json
from datetime import date, timedelta
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai.validator import validate_extraction
from ai.post_processing import post_process
from database.database import Base
from database.repository import InvoiceRepository, DuplicateInvoiceError
from database.qa_migration import migrate_duplicate_identity
from models.invoice import Invoice, LineItem
from parser.page_groups import group_invoice_pages
from parser.currency_parser import normalize_currency
from services.search_service import filter_invoice_keyword
from services.invoice_service import _orm_to_dict
from ocr.preprocessing import deskew, normalize_lighting


def valid(**changes):
    data = dict(invoice_number="00001", invoice_date="2020-01-10", vendor_name="ACME",
                currency="PHP", subtotal=100, tax_amount=12, total_amount=112,
                line_items=[dict(description="Item", quantity=2, unit_price=50, amount=100)])
    data.update(changes)
    return data


@pytest.mark.parametrize("placeholder", [None, "", " ", "-", "—", "N/A", "Unknown Vendor"])
@pytest.mark.parametrize("field", ["invoice_number", "vendor_name", "invoice_date", "total_amount"])
def test_required_placeholders(placeholder, field):
    assert any("Missing required field" in s and field in s for s in validate_extraction(valid(**{field: placeholder})))


def test_bad_date_future_date_currency():
    for value in ["2020-02-31", "not-a-date", (date.today() + timedelta(days=1)).isoformat()]:
        assert any("invoice_date" in s for s in validate_extraction(valid(invoice_date=value)))
    assert normalize_currency("XYZ") == "XYZ"
    assert any("currency" in s for s in validate_extraction(valid(currency="XYZ")))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", True, -1])
def test_invalid_numeric_values(value):
    issues = validate_extraction(valid(tax_amount=value))
    assert any("tax_amount" in s for s in issues)


def test_tax_and_small_arithmetic_errors():
    assert any("exceeds" in s for s in validate_extraction(valid(tax_amount=200)))
    assert any("does not match" in s for s in validate_extraction(valid(total_amount=113)))
    assert any("quantity * unit_price" in s for s in validate_extraction(valid(
        line_items=[dict(description="Item", quantity=2, unit_price=50, amount=101)])))


def test_zero_quantity_and_amount_preserved():
    row = dict(description="Free item", quantity=0, unit_price=50, amount=0)
    assert post_process(valid(line_items=[row]))["line_items"][0]["quantity"] == 0
    assert LineItem(description="Discounted item", quantity=1, unit_price=50, amount=0).amount == 0


def test_valid_identifier_and_bad_date_survive_cleaning():
    result = post_process(valid(invoice_number="INV-00IS", invoice_date="2020-02-31"))
    assert result["invoice_number"] == "INV-00IS"
    assert any("invalid date" in s for s in validate_extraction(result))


@pytest.mark.parametrize("rows", [[None, 4, "bad"], "not-a-list", {}])
def test_malformed_rows_do_not_crash(rows):
    assert validate_extraction(valid(line_items=rows))


def test_page_groups_keep_continuations_and_separate_new_invoices():
    pages = [dict(page_number=1, text="Account Number: 123\nPage 1 of 2"),
             dict(page_number=2, text="Account Number: 123\nPage 2 of 2\nSecond item"),
             dict(page_number=3, text="Invoice No: 999\nPage 1 of 1")]
    result = group_invoice_pages(pages)
    assert len(result) == 2
    assert "Second item" in result[0]["text"]
    assert result[0]["source_page_numbers"] == [1, 2]
    pages[1]["text"] = "Account Number: 999\nPage 2 of 2"
    assert len(group_invoice_pages(pages)) == 3


@pytest.fixture
def repo():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield InvoiceRepository(session)
    session.close()
    engine.dispose()


def test_vendor_scoped_duplicates_and_edit(repo):
    a = repo.save(Invoice(**valid(vendor_name="Alpha")))
    b = repo.save(Invoice(**valid(vendor_name="Beta")))
    assert a.id != b.id
    with pytest.raises(DuplicateInvoiceError):
        repo.save(Invoice(**valid(vendor_name=" ALPHA ")))
    with pytest.raises(DuplicateInvoiceError):
        repo.update(b.id, {"vendor_name": "alpha"})
    assert repo.get_by_id(b.id).vendor_name == "BETA"


def test_persistence_quality_items_and_literal_search(repo):
    invoice = Invoice(**valid(invoice_number="A_001", validation_issues=["Check date"],
                              field_confidences=[{"field": "invoice_date", "confidence": 0.2}]))
    saved = repo.save(invoice)
    result = _orm_to_dict(saved)
    assert result["validation_issues"] == ["Check date"]
    assert result["field_confidences"][0]["confidence"] == 0.2
    assert result["line_items"] == [x.model_dump() for x in invoice.line_items]
    assert [r.id for r in repo.search(" A_001 ")] == [saved.id]
    assert not repo.search("A%001")
    assert filter_invoice_keyword([result], "acme") == [result]
    assert not filter_invoice_keyword([result], "%")


@pytest.mark.parametrize("inline", [True, False])
def test_legacy_unique_migration_preserves_rows_and_items(tmp_path, inline):
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    with engine.begin() as conn:
        constraint = " UNIQUE" if inline else ""
        conn.exec_driver_sql(f"CREATE TABLE invoices(id INTEGER PRIMARY KEY, invoice_number TEXT NOT NULL{constraint}, vendor_name TEXT, duplicate_key TEXT, status TEXT)")
        if not inline:
            conn.exec_driver_sql("CREATE UNIQUE INDEX ix_invoices_invoice_number ON invoices(invoice_number)")
        conn.exec_driver_sql("CREATE TABLE line_items(id INTEGER PRIMARY KEY, invoice_id INTEGER REFERENCES invoices(id), amount REAL)")
        conn.exec_driver_sql("INSERT INTO invoices VALUES (1,'00001','ALPHA',NULL,'processed')")
        conn.exec_driver_sql("INSERT INTO line_items VALUES (1,1,112)")
    migrate_duplicate_identity(engine)
    migrate_duplicate_identity(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO invoices VALUES (2,'00001','BETA',NULL,'processed')")
        assert conn.exec_driver_sql("SELECT amount FROM line_items WHERE invoice_id=1").scalar() == 112
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def _text_angle(image):
    mask = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    return (cv2.minAreaRect(cv2.findNonZero(mask))[-1] + 45) % 90 - 45


@pytest.mark.parametrize("angle", [-7, -3, 3, 7])
def test_deskew_corrects_both_directions(angle):
    image = np.full((500, 700), 255, dtype=np.uint8)
    for y in range(100, 400, 50):
        cv2.putText(image, "Invoice ABC 123456", (80, y), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)
    rotated = cv2.warpAffine(image, cv2.getRotationMatrix2D((350, 250), angle, 1), (700, 500), borderValue=255)
    assert abs(_text_angle(deskew(rotated))) < 0.5


def test_shadow_normalization_preserves_text():
    image = np.tile(np.linspace(100, 250, 500, dtype=np.uint8), (300, 1))
    cv2.putText(image, "INVOICE 001", (40, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)
    result = normalize_lighting(image)
    assert result[20, :].std() < image[20, :].std() / 3
    assert np.min(result[130:165, 40:250]) < 50


def test_correction_timeout_retains_usable_first_result(monkeypatch):
    from ai import extractor
    from config.settings import settings
    responses = iter([json.dumps(valid(total_amount=113)), extractor.ExtractionError("timeout")])
    def call(*args):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(extractor, "_call_ollama", call)
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 1)
    trace = {}
    assert extractor.extract_invoice_data("Total 113.00", trace=trace)["total_amount"] == 113
    assert trace["stopped_on_failed_correction"] == "timeout"


def test_deadline_prevents_retries(monkeypatch):
    import requests
    from ai import processing_budget as budget
    from config.settings import settings
    clock = [100.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "LOCAL_TEXT_BUDGET_SECONDS", 30)
    @budget.processing_scope
    @budget.model_scope("local_text")
    def request():
        assert budget.remaining_timeout(300) == 30
        clock[0] += 31
        with pytest.raises(requests.exceptions.Timeout):
            budget.remaining_timeout(300)
    request()
    assert budget.remaining_timeout(300) == 300


@pytest.mark.parametrize("name,number,subtotal,tax,current,balance,total,count", [
    ("INV-04", "0434287913", 1159.82, 139.18, 1299, -10, 1289, 1),
    ("INV-01", "4230011264445", 884.27, 98.63, 1034.55, 1508.52, 2543.07, 9),
])
def test_actual_statement_text(name, number, subtotal, tax, current, balance, total, count):
    from pathlib import Path
    from ai.statement_evidence import reconcile_statement_evidence
    text = (Path(__file__).parent / "fixtures" / "qa" / (name + ".txt")).read_text(encoding="utf8")
    result, notes = reconcile_statement_evidence(valid(), text)
    assert result["invoice_number"] == number
    assert result["subtotal"] == subtotal
    assert result["tax_amount"] == tax
    assert result["current_charges_total"] == current
    assert result["previous_balance"] == balance
    assert result["total_amount"] == total
    assert len(result["line_items"]) == count
    assert not validate_extraction(result)
    assert notes


def test_missing_numbered_page_is_flagged():
    result = group_invoice_pages([dict(text="Invoice 123\nPage 1 of 2", page_number=1)])
    assert "Incomplete numbered document" in result[0]["warnings"][0]


def test_manual_edit_revalidates_and_drops_stale_confidence(repo):
    row = repo.save(Invoice(**valid(confidence_score=0.95)))
    updated = repo.update(row.id, {"total_amount": -112})
    assert updated.status == "needs_review"
    assert "negative" in updated.validation_issues
    assert updated.confidence_score is None
    with pytest.raises(ValueError):
        repo.update(row.id, {"total_amount": float("nan")})


def test_upload_runs_processing_off_event_loop(monkeypatch):
    import asyncio
    import threading
    from io import BytesIO
    from starlette.datastructures import UploadFile
    from api import upload
    caller = threading.get_ident()
    threads = []
    monkeypatch.setattr(upload, "handle_upload", lambda *args: "invoice.png")
    def process(*args, **kwargs):
        threads.append(threading.get_ident())
        return [{"success": True}]
    monkeypatch.setattr(upload, "process_invoice_file", process)
    result = asyncio.run(upload.upload_invoice(UploadFile(filename="invoice.png", file=BytesIO(b"test"))))
    assert result["invoices"][0]["success"]
    assert threads[0] != caller


def test_export_api_uses_single_file_path(monkeypatch, tmp_path):
    from api import invoices
    path = tmp_path / "invoices.csv"
    path.write_text("Invoice #,Vendor\n001,ACME\n")
    monkeypatch.setattr(invoices, "list_invoices", lambda **kwargs: [valid()])
    monkeypatch.setattr(invoices, "export_invoices", lambda *args, **kwargs: [str(path)])
    result = invoices.export("csv")
    assert result.path == str(path)
    assert result.filename == "invoices.csv"


def test_orientation_probe_does_not_auto_flip_crops(monkeypatch, tmp_path):
    from ocr import ocr_engine, paddleocr_engine
    path = str(tmp_path / "page.png")
    cv2.imwrite(path, np.full((80, 160, 3), 255, dtype=np.uint8))
    monkeypatch.setattr(paddleocr_engine, "detect_boxes", lambda _: [[[0,0],[150,0],[150,30],[0,30]]])
    calls = []
    def recognize(crop, classify_angle=True):
        calls.append(classify_angle)
        return {"text": "Invoice 001", "confidence": 0.99}
    monkeypatch.setattr(paddleocr_engine, "recognize_crop", recognize)
    assert ocr_engine._orientation_score(path)[0] > 0
    assert calls == [False]


def test_archive_path_update_preserves_quality(repo):
    row = repo.save(Invoice(**valid(confidence_score=0.91, validation_issues=["Check source"])))
    updated = repo.update(row.id, {"source_file": "processed/invoice.pdf"})
    assert updated.source_file == "processed/invoice.pdf"
    assert updated.confidence_score == 0.91
    assert json.loads(updated.validation_issues) == ["Check source"]


def test_partial_document_failure_reaches_saved_review(monkeypatch):
    from parser import invoice_parser
    monkeypatch.setattr(invoice_parser, "extract_pages_from_file", lambda *args, **kwargs: [
        {"text": "Invoice 001", "page_number": 1, "total_pages": 2},
        {"text": "", "page_number": 2, "total_pages": 2, "error": "OCR failed"},
    ])
    monkeypatch.setattr(invoice_parser, "_build_invoice_from_ocr", lambda **kwargs: Invoice(**valid()))
    result = invoice_parser.parse_invoice_pages("source.pdf")
    assert result[0].status == "needs_review"
    assert "page 2/2" in result[0].validation_issues[-1]


def test_quality_component_displays_saved_warnings():
    from streamlit.testing.v1 import AppTest
    test = AppTest.from_string('''
from ui.components.quality_details import render_quality_details
render_quality_details({"status":"needs_review", "validation_issues":["Missing invoice_date"],
"confidence_score":0.4, "field_confidences":[{"field":"invoice_date","value":None,"confidence":0.0,"source":"missing"}]})
''').run(timeout=10)
    assert not test.exception
    assert test.warning[0].value == "Missing invoice_date"
    assert len(test.dataframe) == 1
