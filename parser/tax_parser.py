"""Detects tax rate/amount mentions in raw OCR text as a cross-check for the LLM output.

Line-based rather than a single regex over the whole blob, because a naive
"(?:tax|vat|gst)\\D{0,N}(amount)" pattern breaks on real receipts in two
ways: (1) a percentage in the label itself, e.g. "VAT Amount(12%): 23.57",
puts DIGITS between the word "vat" and the actual amount, so a \\D-only
gap regex simply fails to match; and (2) "VATable Sales" contains the
substring "vat" too, so the same loose regex can latch onto the pre-tax
sales base instead of the actual tax figure. Scanning line-by-line with
explicit include/exclude labels avoids both failure modes.

Two more real-world OCR artifacts, both seen on actual receipts, are
handled here too:
  (3) PaddleOCR emits one detection box per line (see
      ocr/paddleocr_engine.py), and on receipts laid out in columns the
      label and its amount are frequently detected as two separate boxes
      — "VAT Amount(12%)" on one line, "25.71" on the next — rather than
      one merged line. ai/post_processing.py's `_amounts_near_label`
      already falls back to the following line for this exact reason on
      CASH/CHANGE/TOTAL labels; the tax label search below now does the
      same, instead of only ever checking the label's own line.
  (4) OCR frequently drops the "t" that sits right before the opening
      "(" of "(12%)", turning "VAT Amount(12%)" into "VAT Amoun12%". The
      label match below tolerates that specific truncation so the line
      isn't silently discarded.
"""
import re

from config.field_aliases import TAX_AMOUNT_LABELS as _TAX_LABELS, TAX_AMOUNT_EXCLUDE as _TAX_LABEL_EXCLUDE

_RATE_ON_LINE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s?%")
_AMOUNT_ON_LINE_RE = re.compile(r"[\d,]+\.\d{2}")

# _TAX_LABELS / _TAX_LABEL_EXCLUDE now come from config/field_aliases.py
# (the shared label-alias library — see that module's docstring) rather
# than being defined here separately. "amoun" (not "amount") deliberately
# matches both the correctly-OCR'd word and the common truncation that
# drops the trailing "t" before "(12%)". _TAX_LABEL_EXCLUDE keeps lines
# that mention tax terminology but are NOT the tax figure itself — the
# taxable/exempt sales base, not the tax charged on it — from matching.


def _tax_line_indices(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(ex in lower for ex in _TAX_LABEL_EXCLUDE):
            continue
        if any(label in lower for label in _TAX_LABELS):
            hits.append((i, line))
    return hits


def _tax_lines(text: str) -> list[str]:
    return [line for _, line in _tax_line_indices(text)]


def find_tax_rate(text: str) -> float | None:
    lines = text.splitlines()
    for i, line in _tax_line_indices(text):
        match = _RATE_ON_LINE_RE.search(line)
        if match:
            return float(match.group(1))
        # Same split-detection-box issue as find_tax_amount below: the "%"
        # can land on the following OCR line instead of the label's own line.
        if i + 1 < len(lines):
            match = _RATE_ON_LINE_RE.search(lines[i + 1])
            if match:
                return float(match.group(1))
    return None


def find_tax_amount(text: str) -> float | None:
    lines = text.splitlines()
    for i, line in _tax_line_indices(text):
        amounts = _AMOUNT_ON_LINE_RE.findall(line)
        if amounts:
            # Take the LAST amount on the line — labels like "VAT Amount(12%)"
            # put a number in the label itself before the real trailing value.
            return float(amounts[-1].replace(",", ""))
        # Label and amount were detected as separate OCR boxes (separate
        # lines) — check the next line before giving up on this label.
        if i + 1 < len(lines):
            amounts = _AMOUNT_ON_LINE_RE.findall(lines[i + 1])
            if amounts:
                return float(amounts[-1].replace(",", ""))
    return None
