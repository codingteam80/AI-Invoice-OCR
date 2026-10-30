"""Regression tests for a clean History view with targeted review highlights."""

# Streamlit is an optional runtime dependency for source-only CI. Loading this
# pure rendering helper does not need a running Streamlit server.
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "compact_field_review", Path(__file__).resolve().parents[1] / "ui" / "components" / "field_evidence.py"
)
component = importlib.util.module_from_spec(spec)
spec.loader.exec_module(component)

# Name bindings for simple test cases.
from unittest.mock import Mock

# helper is directly loaded to avoid importing Streamlit's UI package.
line_items_review_table = component.line_items_review_table
needs_verification = component.needs_verification
render_field = component.render_field


class FakeColumn:
    def __init__(self):
        self.written = []
        self.markdowns = []

    def write(self, txt):
        self.written.append(txt)

    def markdown(self, txt, **kwargs):
        self.markdowns.append((txt, kwargs))


def test_only_problematic_field_is_highlighted_and_confidence_is_hidden():
    evidence = {
        "vendor_name": {"score": .89, "status": "supported", "warning": ""},
        "vendor_address": {"score": .44, "status": "review", "warning": "No OCR match"},
    }
    col = FakeColumn()
    render_field(col, "Vendor", "vendor_name", "STORE", evidence)
    render_field(col, "Vendor Address", "vendor_address", "Mall of Asia", evidence)
    assert col.written == ["**Vendor:** STORE"]
    assert len(col.markdowns) == 1
    html, options = col.markdowns[0]
    assert options["unsafe_allow_html"]
    assert "Mall of Asia" in html and "⚠ Verify" in html
    assert "89%" not in html and "44%" not in html
    assert "No OCR match" in html  # hover description


def test_untrusted_invoice_value_and_warning_cannot_insert_html():
    col = FakeColumn()
    render_field(col, "Address", "customer_address", '<script>alert("oops")</script>', {
        "customer_address": {"status": "review", "warning": '<img src=x onerror="alert(1)">'}
    })
    html = col.markdowns[0][0]
    assert "<script" not in html and "<img" not in html
    assert '&lt;script&gt;' in html and "&lt;img" in html


def test_manually_corrected_fields_do_not_show_as_ocr_problem():
    meta = {"status": "manual_edit", "score": None,
            "warning": "Manual correction: OCR evidence no longer applies."}
    assert not needs_verification(meta)
    col = FakeColumn()
    render_field(col, "Customer", "customer_name", "Corrected Buyer", {"customer_name": meta})
    assert not col.markdowns


def test_line_items_highlight_only_incorrect_cell_without_confidence_columns():
    items = [
        {"description": "Sneakers", "quantity": 1, "unit_price": 1999, "amount": 1999},
        {"description": "Backpack", "quantity": 1, "unit_price": 1499, "amount": 1499},
    ]
    ev = {"line_items.1.unit_price": {"status": "review", "score": .44, "warning": "No OCR row match"},
          "line_items.0.amount": {"status": "supported", "score": .89, "warning": ""}}
    styled, reasons = line_items_review_table(items, ev)
    assert list(styled.data.columns) == ["Item", "Qty", "Unit Price", "Total Unit Price"]
    assert styled.data.iloc[1]["Unit Price"] == "1,499.00"
    assert len(reasons) == 1 and "Backpack" in reasons[0]
    ctx = styled._compute().ctx
    assert any("background-color" in str(p) for p in ctx[(1, 2)])
    assert not ctx.get((0, 2))
    assert "evidence" not in styled.to_html().lower()


def test_no_unneeded_highlights_if_all_cells_supported():
    styled, reasons = line_items_review_table([
        {"description": "Notebook", "quantity": 1, "unit_price": 30, "amount": 30}
    ], {})
    assert reasons == []
    assert styled._compute().ctx == {}
