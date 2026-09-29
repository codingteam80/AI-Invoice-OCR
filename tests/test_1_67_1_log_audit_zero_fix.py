from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_log_table_gives_details_majority_width_and_multiline_display():
    text = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    assert 'col.col-datetime {{ width: 13%; }}' in text
    assert 'col.col-user {{ width: 8%; }}' in text
    assert 'col.col-action {{ width: 10%; }}' in text
    assert 'col.col-type {{ width: 7%; }}' in text
    assert 'col.col-id {{ width: 5%; }}' in text
    assert 'col.col-details {{ width: 57%; }}' in text
    assert 'text.replace(" | ", "\\n")' in text
    assert '.replace("\\n", "<br>")' in text


def test_new_audit_changes_are_stored_one_field_per_line():
    text = (ROOT / "services/invoice_service.py").read_text(encoding="utf-8")
    assert 'return "\\n".join(changes)' in text
    assert 'f"Invoice #{result.get(\'invoice_number\')}:\\n{changes}"' in text
    assert '"\\n".join(changes)' in text


def test_manual_edit_never_resubmits_hidden_discount_and_preserves_zero_values():
    text = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    start = text.index('updates = {')
    end = text.index('            try:', start)
    block = text[start:end]
    assert '"discount":' not in block
    assert '"withholding_tax": _preserve_optional_numeric' in block
    assert '"zero_rated_sales": _preserve_optional_numeric' in block
    assert '"vat_exempt_sales": _preserve_optional_numeric' in block
    assert 'def _preserve_optional_numeric' in text


def test_v167_zero_to_null_repair_is_audit_evidence_scoped():
    text = (ROOT / "database/database.py").read_text(encoding="utf-8")
    assert 'def _repair_v167_zero_to_null_edit_bug' in text
    assert 'Discount (internal): 0.00 → —' in text
    assert 'Zero-Rated Sales: 0.00 → —' in text
    assert 'VAT-Exempt Sales: 0.00 → —' in text
    assert "WHERE action = 'EDIT' AND entity_type = 'invoice'" in text
    assert 'AND {column} IS NULL' in text
