import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.validator import validate_extraction


def test_missing_required_field():
    data = {"invoice_number": "INV-1", "vendor_name": "Acme"}
    issues = validate_extraction(data)
    assert any("total_amount" in i for i in issues)


def test_totals_mismatch():
    data = {
        "invoice_number": "INV-1", "vendor_name": "Acme", "currency": "USD",
        "subtotal": 100, "tax_amount": 10, "discount": 0, "total_amount": 200,
    }
    issues = validate_extraction(data)
    assert any("does not match total_amount" in i for i in issues)


def test_valid_extraction():
    data = {
        "invoice_number": "INV-1", "vendor_name": "Acme", "currency": "USD",
        "subtotal": 100, "tax_amount": 10, "discount": 0, "total_amount": 110,
    }
    assert validate_extraction(data) == []
