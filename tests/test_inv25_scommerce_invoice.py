"""INV-25 (SCommerce/Shopee 'Invoice (Sale of Goods)'). Before: the free item (P0.00, cells not read by OCR) borrowed the
next product's qty/price/total (4,650) and then the Qty cell / 'Black 25W Adapter' text as its amount (1.00), so the line
items summed to 4,651 vs 4,650; seller TIN read 010-691-151 (printed 010-091-151); invoice number 'ISS1...' (printed
'ISSI...'). Fixture: tests/fixtures/inv25_scommerce_trace_min.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
FIX = json.loads((Path(__file__).parent / "fixtures" / "inv25_scommerce_trace_min.json").read_text(encoding="utf-8"))
LINES, TEXT, DRAFT, WRONG = FIX["ocr"]["lines"], FIX["ocr"]["text"], FIX["post_process"], FIX["wrong_final"]


def test_free_item_does_not_borrow_the_next_rows_amounts():
    from ai.post_processing import reconcile_line_item_geometry
    out, notes = reconcile_line_item_geometry(dict(DRAFT), LINES)
    got = [(i["unit_price"], i["amount"]) for i in out["line_items"]]
    assert got == [(0.0, 0.0), (4650.0, 4650.0)] and notes == []
    assert sum(a for _u, a in got) == 4650.0


def test_qty_cell_and_text_cells_are_not_row_amounts():
    from ai.post_processing import reconcile_line_item_geometry
    box = lambda x1, y1, x2, y2: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    lines = [{"text": "Free Gift Pack", "bbox": box(10, 10, 200, 30), "confidence": 1},
             {"text": "Black 25W Adapter", "bbox": box(300, 10, 450, 30), "confidence": 1},
             {"text": "1", "bbox": box(500, 10, 515, 30), "confidence": 1}]
    data = {"line_items": [{"description": "Free Gift Pack", "quantity": 1.0, "unit_price": 0.0, "amount": 0.0}]}
    out, notes = reconcile_line_item_geometry(data, lines)
    assert out["line_items"][0]["amount"] == 0.0 and notes == []


def test_a_real_price_cell_is_still_picked_up():
    from ai.post_processing import reconcile_line_item_geometry
    box = lambda x1, y1, x2, y2: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    lines = [{"text": "Widget", "bbox": box(10, 10, 100, 30), "confidence": 1},
             {"text": "1", "bbox": box(300, 10, 315, 30), "confidence": 1},
             {"text": "P250.00", "bbox": box(400, 10, 470, 30), "confidence": 1}]
    data = {"line_items": [{"description": "Widget", "quantity": 1.0, "unit_price": 1.0, "amount": 1.0}]}
    out, _ = reconcile_line_item_geometry(data, lines)
    assert out["line_items"][0]["amount"] == 250.0


def test_seller_tin_is_snapped_to_the_master_and_invoice_number_prefix_fixed():
    from ai.party_master import repair_vendor_tin_from_master
    from parser.ecommerce_invoice import normalize_ecommerce_invoice
    out, notes = repair_vendor_tin_from_master(dict(WRONG))
    assert out["vendor_tax_id"] == "010-091-151-00000" and notes
    fixed, n2 = normalize_ecommerce_invoice(dict(WRONG), TEXT)
    assert fixed["invoice_number"] == "ISSI000010134539" and n2
    assert normalize_ecommerce_invoice(dict(fixed), TEXT) == (fixed, [])               # idempotent
    other = {"vendor_name": "ACME", "invoice_number": "ISS1000010134539"}
    assert normalize_ecommerce_invoice(dict(other), TEXT) == (other, [])               # vendor-gated
