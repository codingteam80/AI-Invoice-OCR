from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_history_status_choices_only_processed_and_needs_review():
    text = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    assert 'STATUS_OPTIONS = ["processed", "needs_review"]' in text
    assert '"status": "processed"' in text


def test_add_category_requests_scroll_back_to_detail():
    text = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    marker = 'st.session_state[category_pending_key] = added'
    pos = text.index(marker)
    assert 'st.session_state["scroll_to_detail"] = True' in text[pos:pos + 300]


def test_lock_sets_processed_in_repository():
    text = (ROOT / "database/repository.py").read_text(encoding="utf-8")
    pos = text.index("def set_locked")
    block = text[pos:pos + 800]
    assert 'if locked:' in block
    assert 'obj.status = "processed"' in block


def test_log_page_and_audit_model_exist():
    assert (ROOT / "ui/pages/Log.py").exists()
    models = (ROOT / "database/models.py").read_text(encoding="utf-8")
    nav = (ROOT / "ui/components/nav.py").read_text(encoding="utf-8")
    assert "class AuditLogORM" in models
    assert '("pages/Log.py", "Log", "📋")' in nav


def test_export_download_is_audited():
    text = (ROOT / "ui/pages/Reports.py").read_text(encoding="utf-8")
    assert "DOWNLOAD EXPORT" in text
    assert "log_action" in text
