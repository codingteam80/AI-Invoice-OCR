"""
Tests for ai/pre_extraction.py — the regex-only pass that runs BEFORE the
LLM is called, and for its wiring into ai/prompt_builder.py.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.pre_extraction import pre_extract_hints
from ai.prompt_builder import build_extraction_prompt

_NORTH_STAR_OCR = (
    "NORTH STAR INTERNATIONAL TRAVEL INC.\n"
    "VAT Reg. TIN: 000-423-215-00000\n"
    "BILLED TO: Tsukiden Global Solutions Inc.\n"
    "TIN NO 007-848-122-000\n"
    "Total Amount Due PHP 2,764.16\n"
)


def test_pre_extract_hints_finds_both_tins():
    hints = pre_extract_hints(_NORTH_STAR_OCR)
    assert hints["vendor_tax_id"] == "000-423-215-00000"
    assert hints["customer_tax_id"] == "007-848-122-000"


def test_pre_extract_hints_finds_total():
    hints = pre_extract_hints(_NORTH_STAR_OCR)
    assert hints["total_amount"] == 2764.16


def test_pre_extract_hints_finds_unlabeled_vendor_name():
    hints = pre_extract_hints(_NORTH_STAR_OCR)
    assert hints["vendor_name"] == "NORTH STAR INTERNATIONAL TRAVEL INC."


def test_pre_extract_hints_empty_for_blank_text():
    assert pre_extract_hints("") == {}
    assert pre_extract_hints(None) == {}


def test_pre_extract_hints_never_raises_on_garbage_text():
    # Confirmed real failure mode: garbled/illegible OCR text from a
    # handwritten invoice. No candidates should be found, but this must
    # not raise — see the module docstring's "honest limits" section.
    garbage = "aanaaaana aa aaee aane aaaaaaa aa ae aaa ap as"
    hints = pre_extract_hints(garbage)
    assert hints == {}


def test_pre_extract_hints_never_includes_line_items_or_dates():
    # Deliberately out of scope — see module docstring.
    hints = pre_extract_hints(_NORTH_STAR_OCR)
    assert "line_items" not in hints
    assert "invoice_date" not in hints
    assert "customer_name" not in hints


def test_build_extraction_prompt_includes_hints_section():
    prompt = build_extraction_prompt(_NORTH_STAR_OCR)
    assert "Pattern-matched candidates" in prompt
    assert "000-423-215-00000" in prompt
    assert "actively check it against the OCR text" in prompt


def test_build_extraction_prompt_omits_hints_section_when_nothing_found():
    prompt = build_extraction_prompt("no recognizable labels anywhere here")
    assert "Pattern-matched candidates" not in prompt


def test_build_extraction_prompt_still_includes_ocr_text_and_schema():
    # The hints section must be additive, never displacing the rest of
    # the prompt the LLM still needs.
    prompt = build_extraction_prompt(_NORTH_STAR_OCR)
    assert "OCR TEXT" in prompt
    assert "JSON schema to fill" in prompt
    assert _NORTH_STAR_OCR.strip() in prompt
