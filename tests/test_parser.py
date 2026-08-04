import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.currency_parser import to_float, normalize_currency
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
