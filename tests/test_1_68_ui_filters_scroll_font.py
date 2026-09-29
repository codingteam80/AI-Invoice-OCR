from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_upload_saved_invoice_button_contains_processing_summary_and_no_separate_expander():
    text = (ROOT / "ui/pages/Upload.py").read_text(encoding="utf-8")
    assert 'OCR: {ocr_text} · Processing time: {time_text}' in text
    assert 'help="\\n".join(help_lines)' in text
    assert 'with st.expander("Processing details"' not in text
    assert 'st.switch_page("pages/History.py")' in text


def test_history_tables_scroll_and_invoice_month_filter_exists_for_shared_filter_function():
    text = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    assert 'with st.container(height=460, border=True):' in text
    assert 'Filter by month(Invoice Date)' in text
    assert 'key=f"invoice_month_filter_{scope_key}"' in text
    assert 'chosen_invoice_months' in text
    assert 'def _invoice_month_key' in text


def test_reports_has_uploaded_and_invoice_date_month_filters_and_scrollable_table():
    text = (ROOT / "ui/pages/Reports.py").read_text(encoding="utf-8")
    assert 'Filter by month(Date Uploaded)' in text
    assert 'Filter by Month(Invoice Date)' in text
    assert 'invoice_month_choice' in text
    assert 'chosen_invoice_month_keys' in text
    assert 'max-height: 460px; overflow: auto;' in text
    assert 'position: sticky; top: 0;' in text


def test_log_has_month_user_action_type_and_id_filters_and_scrollable_table():
    text = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    for label in [
        'Filter by Month',
        'Filter User',
        'Filter Action',
        'Filter Type',
        'Filter ID',
    ]:
        assert label in text
    assert 'Filter Date & Time' not in text
    assert 'Filter Details' not in text
    assert 'def _log_month_key' in text
    assert 'chosen_month_keys' in text
    assert 'max-height: 460px; overflow: auto;' in text
    assert 'Showing {len(filtered_logs)} of {len(logs)} log record(s).' in text


def test_global_font_is_increased_but_page_title_is_fixed_to_original_size():
    text = (ROOT / "ui/components/nav.py").read_text(encoding="utf-8")
    assert 'html {\n    font-size: 18px;' in text
    assert '[data-testid="stAppViewContainer"] h1 {' in text
    assert 'font-size: 44px !important;' in text
    assert '.stDownloadButton button' in text
    assert '.stNumberInput label' in text
