"""
Regression tests for the fixes applied after the Sept 2026 update was found
to have broken previously-working extractions on invoices unrelated to the
original "discount sign" bug it targeted. See
AI-Invoice-OCR_Regression_Report.md for the full root-cause writeup.

Each test below corresponds 1:1 to one of the four fixes in that report.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types
if "torch" not in sys.modules:
    sys.modules["torch"] = types.ModuleType("torch")

import numpy as np
import cv2

from config.settings import settings
from ocr import ocr_engine, paddleocr_engine
from ai.post_processing import _amount_appears_in_text, reconcile_discount
from utils.date_utils import parse_date


def _make_test_image(tmp_path, name="test_invoice.png", shape=(300, 200, 3)):
    img = np.full(shape, 255, dtype="uint8")
    path = str(tmp_path / name)
    cv2.imwrite(path, img)
    return path


# ---------------------------------------------------------------------------
# Fix 1: auto page-rotation now requires a clear margin before rotating,
# instead of flipping on ANY difference in detected-box count.
# ---------------------------------------------------------------------------
def test_upright_page_with_slightly_noisy_alternate_rotation_is_not_flipped(tmp_path, monkeypatch):
    """The regression case: an already-upright invoice where some OTHER
    rotation coincidentally detects a FEW more boxes (table borders, a
    stamp, a logo) than the upright orientation — this must NOT be enough
    to trigger a rotation. Before the fix, any difference at all (even
    +1 box) would flip the page."""
    image_path = _make_test_image(tmp_path)

    def fake_detect_boxes(path):
        if path == image_path:
            return [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 20  # upright: 20 boxes
        # every rotated candidate detects only slightly more — NOT a clear win
        return [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 22

    monkeypatch.setattr(paddleocr_engine, "detect_boxes", fake_detect_boxes)
    monkeypatch.setattr(settings, "OCR_AUTO_ORIENT_ENABLED", True)

    result_path = ocr_engine.correct_page_orientation(image_path)
    assert result_path == image_path  # stays untouched


def test_genuinely_sideways_page_still_gets_rotated(tmp_path, monkeypatch):
    """The original SGV-style bug must still be fixed: a page that's truly
    sideways (upright orientation finds almost nothing, one rotation finds
    plenty) must still be corrected."""
    image_path = _make_test_image(tmp_path)

    def fake_detect_boxes(path):
        if path == image_path:
            return []  # sideways: upright finds nothing
        if "orient_probe_1_" in path:
            return [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 40  # clear winner
        return [[[0, 0], [10, 0], [10, 5], [0, 5]]] * 1

    monkeypatch.setattr(paddleocr_engine, "detect_boxes", fake_detect_boxes)
    monkeypatch.setattr(settings, "OCR_AUTO_ORIENT_ENABLED", True)

    result_path = ocr_engine.correct_page_orientation(image_path)
    assert result_path != image_path


# ---------------------------------------------------------------------------
# Fix 2: total-amount "appears in OCR text" check now tolerates ordinary
# OCR whitespace noise and whole-peso (no-decimal) printing instead of
# requiring an exact byte-for-byte substring match.
# ---------------------------------------------------------------------------
def test_amount_appears_in_text_tolerates_stray_whitespace():
    ocr_text = "Amount Due: 1 ,234 .56 PHP"
    assert _amount_appears_in_text(ocr_text, 1234.56) is True


def test_amount_appears_in_text_tolerates_whole_peso_no_decimal():
    ocr_text = "Total Amount Due   250"
    assert _amount_appears_in_text(ocr_text, 250.00) is True


def test_amount_appears_in_text_still_rejects_genuinely_absent_value():
    ocr_text = "Total Amount Due   999.00"
    assert _amount_appears_in_text(ocr_text, 1234.56) is False


# ---------------------------------------------------------------------------
# Fix 4: ambiguous 2-digit-year date formats ("%d/%m/%y" AND "%m/%d/%y"
# both being tried) removed — only the day-first form (matching the
# confirmed real PH invoice samples) remains.
# ---------------------------------------------------------------------------
def test_two_digit_year_slash_date_parses_day_first():
    # "13/08/26" must be read as 13 August 2026, not "invalid month 13"
    # under a month-first interpretation, and not silently swapped.
    result = parse_date("13/08/26")
    assert result is not None
    assert (result.day, result.month, result.year) == (13, 8, 2026)


# ---------------------------------------------------------------------------
# "Phantom discount" fix: reconcile_discount() no longer invents a discount
# from an unrelated number near the bare word "discount" (column header
# with no value, an ID/note line, or the next line's unrelated number).
# The specific phrase labels ("less: discount", etc.) still fill normally.
# ---------------------------------------------------------------------------
def test_generic_discount_header_with_no_value_does_not_invent_one(tmp_path=None):
    """Regression case: receipt has a 'Discount' column header with
    nothing on that line, followed by an unrelated line item price. Must
    NOT be picked up as a discount."""
    ocr_text = "Discount\nBottled Water          45.00"
    result, notes = reconcile_discount({"discount": None}, ocr_text)
    assert result["discount"] is None


def test_generic_discount_label_with_unrelated_id_number_is_ignored():
    """Regression case: 'Senior Citizen/PWD Discount ID No. 123456' must
    not be read as a 150000+ peso discount."""
    ocr_text = "Senior Citizen/PWD Discount ID No. 123456"
    result, notes = reconcile_discount({"discount": None}, ocr_text)
    assert result["discount"] is None


def test_generic_discount_label_with_real_inline_amount_still_fills():
    """The generic label must still work when a real, properly-formatted
    amount is printed on the SAME line."""
    ocr_text = "Discount          150.00"
    result, notes = reconcile_discount({"discount": None}, ocr_text)
    assert result["discount"] == 150.00


def test_specific_discount_phrase_still_fills_from_next_line():
    """Specific phrase labels keep the looser next-line fallback — this
    must keep working (e.g. the real Globe Business bill case)."""
    ocr_text = "Discounts\n(400.00)"
    result, notes = reconcile_discount({"discount": None}, ocr_text)
    assert result["discount"] == 400.00
