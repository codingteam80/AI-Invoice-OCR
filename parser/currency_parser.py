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
    """
    Convert '$1,234.56', '1.234,56', '(400.00)', '40.15-', etc. into a
    plain float.

    Two accounting conventions for a NEGATIVE amount are common on real
    invoices/bills (see e.g. a Globe Business bill printing its discount
    line as "Discounts (400.00)", and PH BIR receipts printing a deduction
    with a trailing minus like "40.15-") and both must be recognized
    BEFORE the digit/comma/dot cleanup below strips the sign markers away:

      - Parentheses: "(400.00)" means -400.00. Stripped down to plain
        digits/dot before this fix, this silently became a POSITIVE
        400.00 — the sign information was thrown away entirely, not just
        misplaced.
      - Trailing minus: "40.15-" means -40.15. Python's float() only
        accepts a LEADING minus ("-40.15"), so passing the trailing-minus
        string through unchanged made float() raise ValueError and this
        function silently return None for a perfectly parseable amount.

    A leading minus ("-400.00") already worked correctly and is untouched.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None

    # 1.50: reject malformed OCR money tokens instead of partially salvaging
    # a plausible-looking prefix/suffix. A real observed example was
    # ``1.9603.40`` (corrupted ``1,967.40``), which downstream regex code
    # could otherwise turn into the phantom amount ``1.96``. Multiple dots
    # are valid only for European thousands notation when a comma decimal
    # separator is also present (e.g. ``1.234.567,89``).
    compact = re.sub(r"[^\d.,\-()]", "", s)
    if compact.count(".") > 1 and "," not in compact:
        return None
    if compact.count(",") > 1 and "." not in compact:
        # Multiple commas are fine as thousands separators only when every
        # group after the first is exactly three digits (1,234,567).
        unsigned = compact.strip("-()")
        parts = unsigned.split(",")
        if not (parts and all(part.isdigit() for part in parts) and all(len(part) == 3 for part in parts[1:])):
            return None

    negative = False
    # Parentheses-as-negative, e.g. "(400.00)" — accounting convention.
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    # Trailing minus, e.g. "40.15-" — common on PH BIR receipt deduction
    # lines. Checked after the parens case so "(400.00)" isn't double-counted.
    if s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    # Leading minus, e.g. "-400.00" — kept as-is; re.sub below preserves it,
    # and float() already understands a single leading minus natively.

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
        result = float(s)
    except ValueError:
        return None
    if negative and result > 0:
        result = -result
    return result


def normalize_discount(value) -> float | None:
    """
    Parse a discount value and return it as a POSITIVE magnitude — the
    app-wide convention for the `discount` field everywhere it's used
    (models/invoice.py::validate_totals, ai/validator.py, ui/pages/
    History.py, exports/*) is `subtotal + tax - discount = total`, which
    only works if `discount` itself is always a positive "amount to
    subtract", never a signed number.

    Real invoices/OCR text frequently print a discount AS a negative
    figure (parentheses, a trailing minus, or the LLM occasionally just
    returning a negative number directly, e.g. a Globe Business bill's
    "Discounts (400.00)" line). Feeding that straight into the
    `- discount` formula used everywhere else would silently ADD the
    discount back instead of subtracting it (double-negative), or display
    a confusing "Discount: -400.00" in the UI. This function is the single
    place that normalizes any of those input shapes to the one convention
    the rest of the app expects.
    """
    parsed = to_float(value)
    if parsed is None:
        return None
    return abs(parsed)
