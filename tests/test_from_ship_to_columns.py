"""INV-02 (commercial invoice): FROM | SHIP TO address blocks are side by side."""
from ocr.paddleocr_engine import _reading_order_key, _median_line_height
from ai.party_columns import reconcile_from_ship_to_columns, normalize_letter_o_invoice_number


def box(x1, y, text, w=None):
    w = w or int(len(text) * 8.2)
    h = 14
    return {"text": text, "confidence": 0.98,
            "bbox": [[x1, y - h / 2], [x1 + w, y - h / 2], [x1 + w, y + h / 2], [x1, y + h / 2]]}


def _sorted_like_ocr(lines):
    bh = _median_line_height(lines)
    return sorted(lines, key=lambda l: _reading_order_key(l, bh))


# Coordinates read off the 917x770 INV-02.png
INV02 = _sorted_like_ocr([
    box(380, 43, "COMMERCIAL INVOICE"),
    box(119, 77, "DATE: May 9, 2018"), box(351, 77, "INVOICE NUMBER: O0001"), box(583, 77, "ITEM:"),
    box(119, 119, "FROM:"), box(351, 119, "SHIP TO:"),
    box(583, 139, "1 Piece House Key"),
    box(119, 158, "MS. SASA C. ARANETA"), box(351, 158, "MR. IRAWADI JOHN OUD"),
    box(119, 179, "1575 PRINCETON ST."), box(351, 179, "IA KARAMU STREET"),
    box(119, 199, "WACK-WACK VILLAGE"), box(351, 199, "TE ATATU PENINSULA"),
    box(119, 218, "MANDALUYONG"), box(351, 218, "AUCKLAND 0610"),
    box(119, 238, "METRO MANILA"), box(351, 238, "NEW ZEALAND"),
    box(119, 258, "PHILIPPINES 1555"),
    box(119, 358, "MOBILE NO.:"), box(351, 358, "MOBILE NO:"), box(583, 338, "Total Gross Weight:"),
    box(119, 379, "+63 917 5928989"), box(351, 379, "+64 210 284 6841"), box(583, 379, "2 grams (approximate)"),
    box(119, 479, "DESCRIPTION"), box(292, 479, "QUANTITY"), box(467, 479, "UNIT PRICE"),
    box(119, 521, "HOUSE KEY"), box(292, 521, "1 PIECE"),
])

WRONG = {  # what the app produced before the fix
    "vendor_name": "SASA C.ARANETA",
    "vendor_address": "1575 PRINCETON ST, IA KARAMU STREET, METRO MANILA",
    "customer_name": "MR.IRAWADI JOHN OUD",
    "customer_address": "AUCKLAND 0610, METRO MANILA, NEW ZEALAND, PHILIPPINES1555",
}


def test_ocr_reading_order_really_is_interleaved():
    texts = [l["text"] for l in INV02]
    assert texts.index("1575 PRINCETON ST.") + 1 == texts.index("IA KARAMU STREET")


def test_inv02_addresses_are_split_by_column():
    out, notes = reconcile_from_ship_to_columns(dict(WRONG), INV02)
    assert out["vendor_address"] == "1575 PRINCETON ST., WACK-WACK VILLAGE, MANDALUYONG, METRO MANILA, PHILIPPINES 1555"
    assert out["customer_address"] == "IA KARAMU STREET, TE ATATU PENINSULA, AUCKLAND 0610, NEW ZEALAND"
    assert len(notes) == 2


def test_names_mobile_numbers_and_item_cell_stay_out_of_addresses():
    out, _ = reconcile_from_ship_to_columns(dict(WRONG), INV02)
    for addr in (out["vendor_address"], out["customer_address"]):
        for junk in ("ARANETA", "IRAWADI", "MOBILE", "+63", "+64", "House Key", "grams"):
            assert junk.lower() not in addr.lower()


def test_idempotent_and_no_note_when_already_correct():
    once, _ = reconcile_from_ship_to_columns(dict(WRONG), INV02)
    twice, notes = reconcile_from_ship_to_columns(once, INV02)
    assert twice == once and notes == []


def test_no_op_without_both_side_by_side_labels():
    single = _sorted_like_ocr([
        box(40, 40, "Bill To:"), box(40, 60, "ACME CORP"), box(40, 80, "12 Sample Street"), box(40, 100, "Makati City"),
    ])
    assert reconcile_from_ship_to_columns(dict(WRONG), single) == (dict(WRONG), [])
    assert reconcile_from_ship_to_columns(dict(WRONG), None) == (dict(WRONG), [])
    # labels stacked vertically (not side by side) are not a two-column block
    stacked = _sorted_like_ocr([
        box(40, 40, "FROM:"), box(40, 60, "12 Sample Street"), box(40, 80, "Makati City"),
        box(40, 200, "SHIP TO:"), box(40, 220, "9 Other Road"), box(40, 240, "Cebu City"),
    ])
    assert reconcile_from_ship_to_columns(dict(WRONG), stacked) == (dict(WRONG), [])


def test_letter_o_invoice_number():
    out, notes = normalize_letter_o_invoice_number({"invoice_number": "O0001"})
    assert out["invoice_number"] == "00001" and notes
    for ok in ("00001", "INV-0001", "OR12345", "O12", "O-0001", None):
        assert normalize_letter_o_invoice_number({"invoice_number": ok}) == ({"invoice_number": ok}, [])
