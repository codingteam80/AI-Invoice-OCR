from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_upload_saved_invoice_rows_open_history_detail():
    text = (ROOT / "ui/pages/Upload.py").read_text(encoding="utf-8")
    assert 'st.session_state["detail_selected_id"] = invoice_id' in text
    assert 'st.session_state["scroll_to_detail"] = True' in text
    assert 'st.switch_page("pages/History.py")' in text
    assert 'key=f"open_saved_invoice_{invoice_id}"' in text


def test_edit_audit_lists_changed_fields_old_and_new_values():
    text = (ROOT / "services/invoice_service.py").read_text(encoding="utf-8")
    assert "def _describe_invoice_changes" in text
    assert "old_value = before.get(field)" in text
    assert "new_value = after.get(field)" in text
    assert "→" in text
    assert "Invoice #" in text and "{changes}" in text


def test_add_category_scroll_retry_keeps_detail_view_visible():
    text = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    assert 'st.session_state["scroll_to_detail"] = True' in text
    assert 'setTimeout(scrollBackToTarget, 250)' in text
    assert 'setTimeout(scrollBackToTarget, 600)' in text


def test_reports_export_actions_preserve_export_viewport():
    text = (ROOT / "ui/pages/Reports.py").read_text(encoding="utf-8")
    assert 'id="generate-export-anchor"' in text
    assert 'st.session_state["scroll_to_generate_export"] = True' in text
    assert "def _finish_confirmed_export_download" in text
    assert "def _record_export_download" in text
    assert "_scroll_to_export_if_requested()" in text


def test_lock_unlock_and_category_reassignment_audit_old_new_values():
    text = (ROOT / "services/invoice_service.py").read_text(encoding="utf-8")
    assert 'Locked: {' in text and '→ Yes' in text and '→ No' in text
    assert '"REASSIGN CATEGORY"' in text
    assert 'REASSIGN CATEGORY' in text
    assert '→ {new_category}' in text
