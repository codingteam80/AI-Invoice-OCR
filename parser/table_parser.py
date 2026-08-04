"""
Fallback heuristic table parser — used when the LLM fails to extract
line items so we can still recover something from raw OCR line data.
"""
import re
from parser.currency_parser import to_float

_ROW_RE = re.compile(
    r"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<price>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+\.\d{2})$"
)


def parse_line_items_from_text(text: str) -> list[dict]:
    """Attempts to find 'description qty price amount' rows in plain OCR text."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        match = _ROW_RE.match(line)
        if match:
            items.append({
                "description": match.group("desc").strip(),
                "quantity": to_float(match.group("qty")),
                "unit_price": to_float(match.group("price")),
                "amount": to_float(match.group("amount")),
            })
    return items
