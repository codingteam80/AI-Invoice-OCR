import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.currency_parser import to_float, normalize_currency, normalize_discount
from utils.date_utils import parse_date


def test_to_float_plain():
    assert to_float("1234.56") == 1234.56


def test_to_float_with_thousands_comma():
    assert to_float("1,234.56") == 1234.56


def test_to_float_european_style():
    assert to_float("1.234,56") == 1234.56


def test_normalize_currency_symbol():
    assert normalize_currency("$") == "USD"


def test_normalize_currency_code():
    assert normalize_currency("eur") == "EUR"


def test_parse_date_iso():
    d = parse_date("2024-05-01")
    assert d.isoformat() == "2024-05-01"


def test_parse_date_slash():
    d = parse_date("05/01/2024")
    assert d is not None


# --- Negative-amount formats (discount/deduction lines) -------------------
# Real invoices print a deduction two common ways: wrapped in parentheses
# ("(400.00)") or with a trailing minus ("40.15-"). Both must resolve to a
# NEGATIVE float from to_float(), and to normalize_discount()'s single
# positive-magnitude convention used everywhere else in the app.

def test_to_float_parentheses_is_negative():
    assert to_float("(400.00)") == -400.00


def test_to_float_trailing_minus_is_negative():
    assert to_float("40.15-") == -40.15


def test_to_float_leading_minus_still_works():
    assert to_float("-400.00") == -400.00


def test_to_float_plain_positive_unaffected():
    assert to_float("400.00") == 400.00


def test_normalize_discount_from_parentheses():
    assert normalize_discount("(400.00)") == 400.00


def test_normalize_discount_from_trailing_minus():
    assert normalize_discount("40.15-") == 40.15


def test_normalize_discount_from_bare_negative_number():
    # LLM occasionally returns the JSON number -400 directly rather than a
    # string — normalize_discount must handle non-string input too.
    assert normalize_discount(-400) == 400.00


def test_normalize_discount_already_positive_unchanged():
    assert normalize_discount(400.00) == 400.00


def test_normalize_discount_none_stays_none():
    assert normalize_discount(None) is None
