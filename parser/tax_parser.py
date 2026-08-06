"""Detects tax rate/amount mentions in raw OCR text as a cross-check for the LLM output.

Line-based rather than a single regex over the whole blob, because a naive
"(?:tax|vat|gst)\\D{0,N}(amount)" pattern breaks on real receipts in two
ways: (1) a percentage in the label itself, e.g. "VAT Amount(12%): 23.57",
puts DIGITS between the word "vat" and the actual amount, so a \\D-only
gap regex simply fails to match; and (2) "VATable Sales" contains the
substring "vat" too, so the same loose regex can latch onto the pre-tax
sales base instead of the actual tax figure. Scanning line-by-line with
explicit include/exclude labels avoids both failure modes.
"""
import re

_RATE_ON_LINE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s?%")
_AMOUNT_ON_LINE_RE = re.compile(r"[\d,]+\.\d{2}")

# Lines that actually state the tax/VAT/GST charge itself.
_TAX_LABELS = ("vat amount", "tax amount", "gst amount", "vat:", "tax:", "gst:")
# Lines that mention tax terminology but are NOT the tax figure — the
# taxable/exempt sales base, not the tax charged on it.
_TAX_LABEL_EXCLUDE = ("vatable", "vat-exempt", "vat exempt", "zero rated", "non-vat")


def _tax_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if any(ex in lower for ex in _TAX_LABEL_EXCLUDE):
            continue
        if any(label in lower for label in _TAX_LABELS):
            lines.append(line)
    return lines


def find_tax_rate(text: str) -> float | None:
    for line in _tax_lines(text):
        match = _RATE_ON_LINE_RE.search(line)
        if match:
            return float(match.group(1))
    return None


def find_tax_amount(text: str) -> float | None:
    for line in _tax_lines(text):
        amounts = _AMOUNT_ON_LINE_RE.findall(line)
        if amounts:
            # Take the LAST amount on the line — labels like "VAT Amount(12%)"
            # put a number in the label itself before the real trailing value.
            return float(amounts[-1].replace(",", ""))
    return None
