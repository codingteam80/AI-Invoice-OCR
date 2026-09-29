from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_log_details_filter_is_removed_and_month_filter_is_multiselect():
    text = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    assert 'Filter Details' not in text
    assert 'details_filter' not in text
    assert 'Filter Date & Time' not in text
    assert 'multiselect("Filter by Month"' in text
    assert 'month_label_map' in text
    assert 'chosen_month_keys' in text


def test_log_month_filter_uses_activity_date_time_month():
    text = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    assert 'raw = row.get("date_time")' in text
    assert 'return raw.strftime("%Y-%m")' in text
    assert 'date(int(year), int(month), 1).strftime("%B %Y")' in text
