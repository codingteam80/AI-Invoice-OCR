"""Whole Qty rendering without changing stored quantities or fractional units."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("quantity_view", _ROOT / "ui/components/field_evidence.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
format_qty = _module.format_line_item_quantity
review_table = _module.line_items_review_table


@pytest.mark.parametrize("source, expected", [
    (1, "1"),
    (1.0, "1"),
    ("1.000000", "1"),
    (0.0, "0"),
    (-0.0, "0"),
    (2.50, "2.5"),
    ("0.1250", "0.125"),
    ("1,200.000", "1200"),
    (None, "—"),
    ("", "—"),
    (float("nan"), "—"),
])
def test_quantity_formatter_preserves_fractional_parts(source, expected):
    assert format_qty(source) == expected


def test_read_only_table_qty_is_clean_without_modifying_items_or_highlights():
    items = [
        {"description": "Ticket", "quantity": 1.000000, "unit_price": 1229.40, "amount": 1229.40},
        {"description": "Produce by weight", "quantity": 0.75, "unit_price": 100, "amount": 75},
    ]
    before = [dict(item) for item in items]
    evidence = {"line_items.1.quantity": {"status": "review", "warning": "Confirm weight"}}
    styled, warnings = review_table(items, evidence)
    assert styled.data["Qty"].tolist() == ["1", "0.75"]
    assert styled.data["Unit Price"].tolist() == ["1,229.40", "100.00"]
    assert items == before
    assert len(warnings) == 1
    assert styled._compute().ctx.get((1, 1))
    assert not styled._compute().ctx.get((0, 1))


def test_edit_form_quantity_format_has_no_fixed_decimals_and_does_not_cast_to_integer():
    src = (_ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert 'NumberColumn("Quantity", min_value=0.0, step=1.0, format="%.15g")' in src
    assert '"quantity": float(row["Quantity"])' in src
