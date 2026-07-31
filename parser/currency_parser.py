"""Currency detection and numeric normalization."""
import re
from config.constants import CURRENCY_SYMBOLS

_ISO_CODES = {"USD", "EUR", "GBP", "JPY", "PHP", "INR", "KRW", "AUD", "CAD", "SGD", "CNY"}


def normalize_currency(value: str) -> str:
    if not value:
        return "USD"
    v = value.strip().upper()
    if v in _ISO_CODES:
        return v
    if value.strip() in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[value.strip()]
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in value:
            return code
    return "USD"


def to_float(value) -> float | None:
    """Convert '$1,234.56', '1.234,56', etc. into a plain float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s:
        return None
    # Handle European-style '1.234,56' -> '1234.56'
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Ambiguous: assume thousands separator unless exactly 2 decimals after comma
        parts = s.split(",")
        if len(parts[-1]) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None
