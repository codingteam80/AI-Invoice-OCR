import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.field_aliases import labels_for, FIELD_LABEL_ALIASES
from ai.post_processing import post_process, reconcile_discount, _amounts_near_label
from config.field_aliases import DISCOUNT_LABELS, SUBTOTAL_LABELS


# --- config/field_aliases.py: the shared "subtotal can also be called Net
# Amount / VATable Sales / ..." mapping library ---------------------------

def test_labels_for_returns_registered_aliases():
    assert "vatable sales" in labels_for("subtotal")
    assert "net amount" in labels_for("subtotal")


def test_labels_for_unknown_field_returns_empty():
    assert labels_for("not_a_real_field") == ()


def test_every_canonical_field_has_at_least_one_alias():
    for field, aliases in FIELD_LABEL_ALIASES.items():
        assert len(aliases) > 0, f"{field} has no registered label aliases"


def test_subtotal_recognizes_vatable_sales_labelled_line():
    ocr_text = "VATable Sales 85,963.99\nZero Rated Sales 96,279.67\n"
    amounts = _amounts_near_label(ocr_text, SUBTOTAL_LABELS)
    assert 85963.99 in amounts


def test_subtotal_recognizes_net_amount_parenthetical_label():
    # Image-1-style label: "Net Amount (Vatable Sales): 1,960.34 PHP"
    ocr_text = "Net Amount (Vatable Sales): 1,960.34 PHP"
    amounts = _amounts_near_label(ocr_text, SUBTOTAL_LABELS)
    assert 1960.34 in amounts


# --- discount: post_process() sign normalization --------------------------

def test_post_process_normalizes_negative_discount_to_positive():
    data = {"discount": -400.00}
    cleaned = post_process(data)
    assert cleaned["discount"] == 400.00


def test_post_process_normalizes_parenthetical_discount_string():
    data = {"discount": "(400.00)"}
    cleaned = post_process(data)
    assert cleaned["discount"] == 400.00


def test_post_process_leaves_missing_discount_as_none():
    data = {}
    cleaned = post_process(data)
    assert cleaned.get("discount") is None


# --- reconcile_discount(): fill-from-OCR and disagreement flagging --------

def test_reconcile_discount_fills_missing_from_labelled_line():
    ocr_text = "Charges For This Month\nDiscounts (400.00)\nTotal Php 2,198.00\n"
    data = {"discount": None}
    cleaned, notes = reconcile_discount(data, ocr_text)
    assert cleaned["discount"] == 400.00
    assert any("filled" in n for n in notes)


def test_reconcile_discount_flags_disagreement_without_overwriting():
    ocr_text = "Less: Discount 250.00\n"
    data = {"discount": 400.00}  # already normalized to positive upstream
    cleaned, notes = reconcile_discount(data, ocr_text)
    assert cleaned["discount"] == 400.00  # unchanged — flag, don't auto-fix
    assert any("needs manual review" in n for n in notes)


def test_reconcile_discount_no_op_when_it_matches():
    ocr_text = "Less: Discount 400.00\n"
    data = {"discount": 400.00}
    cleaned, notes = reconcile_discount(data, ocr_text)
    assert cleaned["discount"] == 400.00
    assert notes == []


def test_reconcile_discount_no_ocr_text_is_no_op():
    data = {"discount": None}
    cleaned, notes = reconcile_discount(data, "")
    assert cleaned == data
    assert notes == []
