"""Detects tax rate/amount mentions in raw OCR text as a cross-check for the LLM output."""
import re

_TAX_RATE_RE = re.compile(r"(?:tax|vat|gst)\D{0,10}(\d{1,2}(?:\.\d+)?)\s?%", re.IGNORECASE)
_TAX_AMOUNT_RE = re.compile(r"(?:tax|vat|gst)\D{0,15}([\d,]+\.\d{2})", re.IGNORECASE)


def find_tax_rate(text: str) -> float | None:
    match = _TAX_RATE_RE.search(text)
    return float(match.group(1)) if match else None


def find_tax_amount(text: str) -> float | None:
    match = _TAX_AMOUNT_RE.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))
