from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.audit_service import audit_utc_to_local
from services.search_service import apply_search_plan, normalize_search_plan, summarize_search_plan

ROOT = Path(__file__).resolve().parents[1]


def test_plate_number_presence_treats_null_dash_and_zero_as_missing():
    rows = [
        {"id": 1, "plate_number": None},
        {"id": 2, "plate_number": "-"},
        {"id": 3, "plate_number": "0"},
        {"id": 4, "plate_number": "ABC-1234"},
    ]
    present, warnings = normalize_search_plan(
        {"presence": [{"field": "plate_number", "operator": "present"}]}, "history"
    )
    assert warnings == []
    assert [r["id"] for r in apply_search_plan(rows, present, "history")] == [4]

    missing, warnings = normalize_search_plan(
        {"presence": [{"field": "plate_number", "operator": "missing"}]}, "reports"
    )
    assert warnings == []
    assert [r["id"] for r in apply_search_plan(rows, missing, "reports")] == [1, 2, 3]
    assert summarize_search_plan(present) == "Plate # is present"
    assert summarize_search_plan(missing) == "Plate # is missing"


def test_presence_search_works_for_other_invoice_fields_and_preserves_numeric_zero():
    rows = [
        {
            "id": 1,
            "customer_tax_id": None,
            "vendor_address": "123 MAIN ST",
            "due_date": None,
            "tax_amount": 0.0,
            "line_items": [],
        },
        {
            "id": 2,
            "customer_tax_id": "007-848-122-000",
            "vendor_address": "-",
            "due_date": "2026-10-01",
            "tax_amount": None,
            "line_items": [{"description": "Parking", "quantity": 1, "unit_price": 100, "amount": 100}],
        },
    ]
    plan, warnings = normalize_search_plan(
        {
            "presence": [
                {"field": "customer_tax_id", "operator": "present"},
                {"field": "due_date", "operator": "present"},
                {"field": "line_items", "operator": "present"},
            ]
        },
        "history",
    )
    assert warnings == []
    assert [r["id"] for r in apply_search_plan(rows, plan, "history")] == [2]

    # Financial 0.00 is meaningful and must count as a present numeric value.
    zero_vat_plan, warnings = normalize_search_plan(
        {"presence": [{"field": "tax_amount", "operator": "present"}]}, "history"
    )
    assert warnings == []
    assert [r["id"] for r in apply_search_plan(rows, zero_vat_plan, "history")] == [1]


def test_search_supports_additional_saved_invoice_fields_and_line_item_text():
    rows = [
        {
            "id": 1,
            "vendor_address": "MAKATI CITY",
            "customer_tax_id": "123-456-789-000",
            "due_date": "2026-10-31",
            "tax_rate": 12,
            "payment_terms": "30 DAYS",
            "line_items": [{"description": "UTILITY SERVICES", "quantity": 1, "unit_price": 100, "amount": 100}],
        }
    ]
    raw = {
        "text": [
            {"field": "vendor_address", "operator": "contains", "value": "Makati"},
            {"field": "payment_terms", "operator": "contains", "value": "30 days"},
            {"field": "line_items", "operator": "contains", "value": "utility services"},
        ],
        "dates": [{"field": "due_date", "operator": "on", "value": "2026-10-31"}],
        "numbers": [{"field": "tax_rate", "operator": "=", "value": 12}],
    }
    plan, warnings = normalize_search_plan(raw, "reports")
    assert warnings == []
    assert [r["id"] for r in apply_search_plan(rows, plan, "reports")] == [1]


def test_explicit_with_without_presence_has_deterministic_fallback(monkeypatch):
    import ai.search_interpreter as si

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            # Simulate the old failure mode where Qwen recognizes no filters.
            return {"response": '{"text":[],"dates":[],"numbers":[],"choices":[],"booleans":[],"presence":[]}'}

    monkeypatch.setattr(si.requests, "post", lambda *args, **kwargs: FakeResponse())

    with_plate, warnings = si.interpret_natural_language_search(
        "search for invoice with plate number", page="history"
    )
    assert warnings == []
    assert with_plate["presence"] == [{"field": "plate_number", "operator": "present"}]

    without_tin, warnings = si.interpret_natural_language_search(
        "show invoices without customer TIN", page="reports"
    )
    assert warnings == []
    assert without_tin["presence"] == [{"field": "customer_tax_id", "operator": "missing"}]


def test_audit_utc_timestamp_converts_to_pc_local_timezone():
    # A historical 06:00 UTC audit row is 14:00 / 2:00 PM in UTC+8.
    stored_utc_naive = datetime(2026, 9, 22, 6, 0, 0)
    local = audit_utc_to_local(stored_utc_naive, timezone(timedelta(hours=8)))
    assert local.strftime("%Y-%m-%d %I:%M:%S %p") == "2026-09-22 02:00:00 PM"
    assert local.utcoffset() == timedelta(hours=8)


def test_log_page_displays_pc_local_time_with_am_pm():
    text = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    audit = (ROOT / "services/audit_service.py").read_text(encoding="utf-8")
    assert "Times shown in this PC's local time" in text
    assert '%Y-%m-%d %I:%M:%S %p' in text
    assert "audit_utc_to_local(r.created_at)" in audit


def test_explicit_presence_overrides_wrong_model_value_condition(monkeypatch):
    import ai.search_interpreter as si

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": '{"text":[{"field":"plate_number","operator":"equals","value":"null"}],"dates":[],"numbers":[],"choices":[],"booleans":[],"presence":[]}'
            }

    monkeypatch.setattr(si.requests, "post", lambda *args, **kwargs: FakeResponse())
    plan, warnings = si.interpret_natural_language_search(
        "show invoices without plate number", page="history"
    )
    assert warnings == []
    assert plan["text"] == []
    assert plan["presence"] == [{"field": "plate_number", "operator": "missing"}]
