from datetime import datetime
from pathlib import Path

from services.search_service import (
    apply_search_plan,
    normalize_search_plan,
    summarize_search_plan,
)

ROOT = Path(__file__).resolve().parents[1]


def test_invoice_ai_search_supports_date_range_amount_vendor_and_lock_state():
    rows = [
        {
            "id": 1,
            "invoice_number": "A-1",
            "vendor_name": "WATSONS PERSONAL CARE STORES",
            "customer_name": "ACME",
            "invoice_date": "2026-07-10",
            "date_uploaded": "2026-09-01",
            "total_amount": 15000.0,
            "category": "Medical & Health Supplies",
            "status": "processed",
            "locked": False,
        },
        {
            "id": 2,
            "invoice_number": "A-2",
            "vendor_name": "WATSONS PERSONAL CARE STORES",
            "customer_name": "ACME",
            "invoice_date": "2026-11-01",
            "date_uploaded": "2026-09-02",
            "total_amount": 20000.0,
            "category": "Medical & Health Supplies",
            "status": "processed",
            "locked": False,
        },
        {
            "id": 3,
            "invoice_number": "A-3",
            "vendor_name": "OTHER VENDOR",
            "customer_name": "ACME",
            "invoice_date": "2026-08-01",
            "date_uploaded": "2026-09-03",
            "total_amount": 9999.0,
            "category": "Others",
            "status": "processed",
            "locked": True,
        },
    ]
    raw = {
        "text": [{"field": "vendor_name", "operator": "contains", "value": "Watsons"}],
        "dates": [{"field": "invoice_date", "operator": "between", "value": "2026-06-01", "value2": "2026-10-31"}],
        "numbers": [{"field": "total_amount", "operator": ">", "value": 10000}],
        "choices": [],
        "booleans": [{"field": "locked", "value": False}],
    }
    plan, warnings = normalize_search_plan(raw, "history")
    assert warnings == []
    result = apply_search_plan(rows, plan, "history")
    assert [r["id"] for r in result] == [1]


def test_invoice_ai_search_rejects_unknown_fields_and_sql_like_operators():
    raw = {
        "text": [{"field": "drop_table", "operator": "contains", "value": "x"}],
        "numbers": [{"field": "total_amount", "operator": "DELETE FROM", "value": 1}],
        "dates": [], "choices": [], "booleans": [],
    }
    plan, warnings = normalize_search_plan(raw, "reports")
    assert plan == {"text": [], "dates": [], "numbers": [], "choices": [], "booleans": [], "presence": []}
    assert warnings


def test_log_ai_search_supports_activity_date_action_user_and_entity_id():
    rows = [
        {
            "id": 1,
            "date_time": datetime(2026, 9, 18, 10, 0),
            "user": "Juan",
            "action": "EDIT",
            "entity_type": "invoice",
            "entity_id": 15,
            "details": "Category changed",
        },
        {
            "id": 2,
            "date_time": datetime(2026, 8, 18, 10, 0),
            "user": "Juan",
            "action": "EDIT",
            "entity_type": "invoice",
            "entity_id": 15,
            "details": "Category changed",
        },
        {
            "id": 3,
            "date_time": datetime(2026, 9, 19, 10, 0),
            "user": "Maria",
            "action": "LOCK",
            "entity_type": "invoice",
            "entity_id": 16,
            "details": "Locked",
        },
    ]
    raw = {
        "text": [
            {"field": "user", "operator": "equals", "value": "Juan"},
            {"field": "action", "operator": "equals", "value": "EDIT"},
        ],
        "dates": [{"field": "date_time", "operator": "between", "value": "2026-09-01", "value2": "2026-09-30"}],
        "numbers": [{"field": "entity_id", "operator": "=", "value": 15}],
        "choices": [], "booleans": [],
    }
    plan, warnings = normalize_search_plan(raw, "log")
    assert warnings == []
    result = apply_search_plan(rows, plan, "log")
    assert [r["id"] for r in result] == [1]


def test_search_summary_explains_applied_conditions():
    plan, _ = normalize_search_plan(
        {
            "text": [{"field": "vendor_name", "operator": "contains", "value": "Globe"}],
            "dates": [],
            "numbers": [{"field": "total_amount", "operator": ">", "value": 10000}],
            "choices": [],
            "booleans": [{"field": "locked", "value": True}],
        },
        "history",
    )
    summary = summarize_search_plan(plan)
    assert "Vendor contains" in summary
    assert "Total Amount Due > 10,000" in summary
    assert "Locked invoices" in summary


def test_pages_use_reusable_qwen_ai_search_and_keep_manual_filters():
    history = (ROOT / "ui/pages/History.py").read_text(encoding="utf-8")
    reports = (ROOT / "ui/pages/Reports.py").read_text(encoding="utf-8")
    log = (ROOT / "ui/pages/Log.py").read_text(encoding="utf-8")
    component = (ROOT / "ui/components/ai_search.py").read_text(encoding="utf-8")
    interpreter = (ROOT / "ai/search_interpreter.py").read_text(encoding="utf-8")

    assert 'page="history"' in history
    assert 'key=f"history_{scope_key}"' in history
    assert 'apply_search_plan(filtered, ai_plan, "history")' in history
    assert 'Filter by month(Invoice Date)' in history

    assert 'page="reports"' in reports
    assert 'apply_search_plan(invoices, ai_plan, "reports")' in reports
    assert 'Filter by month(Date Uploaded)' in reports
    assert 'Filter by Month(Invoice Date)' in reports

    assert 'page="log"' in log
    assert 'apply_search_plan(filtered_logs, ai_plan, "log")' in log
    assert 'Filter by Month' in log

    assert 'AI Search ({settings.SEARCH_LLM_MODEL})' in component
    assert 'Clear AI Search' in component
    assert 'AI interpreted:' in component
    assert '"format": "json"' in interpreter
    assert 'DO NOT write SQL' in interpreter
    assert 'settings.SEARCH_LLM_MODEL' in interpreter


def test_search_settings_default_to_qwen_2_5_7b():
    settings = (ROOT / "config/settings.py").read_text(encoding="utf-8")
    assert 'SEARCH_LLM_MODEL: str = os.getenv("SEARCH_LLM_MODEL", os.getenv("OLLAMA_MODEL", "qwen2.5:7b"))' in settings
    assert 'SEARCH_LLM_TIMEOUT_SECONDS' in settings


def test_interpreter_uses_local_ollama_json_and_validates(monkeypatch):
    import ai.search_interpreter as si

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": '{"text":[],"dates":[],"numbers":[{"field":"total_amount","operator":">","value":10000}],"choices":[],"booleans":[]}'
            }

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(si.requests, "post", fake_post)
    plan, warnings = si.interpret_natural_language_search(
        "show invoices above 10000",
        page="reports",
        context={"categories": ["Others"]},
    )
    assert warnings == []
    assert plan["numbers"][0] == {"field": "total_amount", "operator": ">", "value": 10000.0}
    assert captured["url"].endswith("/api/generate")
    assert captured["payload"]["model"] == si.settings.SEARCH_LLM_MODEL
    assert captured["payload"]["format"] == "json"
    assert "DO NOT write SQL" in captured["payload"]["prompt"]
