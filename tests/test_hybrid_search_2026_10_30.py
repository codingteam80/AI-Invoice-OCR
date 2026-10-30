"""Local-first and SQLite-assisted search regression tests."""
from datetime import date, datetime
import pytest
from ai.local_search_parser import parse_local_search
from ai import search_interpreter as si
from services.search_service import apply_hybrid_search, apply_search_plan

NOW = date(2026, 10, 30)
CONTEXT = {"vendors": ["MERALCO", "PLDT INC.", "WATSONS"], "categories": ["Transportation", "Utilities"],
           "currencies": ["PHP", "USD"], "users": ["Juan"], "actions": ["EDIT", "UPLOAD"]}


def test_local_vendor_month_amount_currency_and_db_filter_structure():
    plan = parse_local_search("Show MERALCO invoices from January to March 2026 above ₱5,000",
                              page="history", context=CONTEXT, today=NOW)
    assert plan["dates"] == [{"field": "invoice_date", "operator": "between", "value": "2026-01-01", "value2": "2026-03-31"}]
    assert plan["numbers"] == [{"field": "total_amount", "operator": ">", "value": 5000.0}]
    assert plan["choices"][0]["values"] == ["PHP"]
    assert plan["text"][0]["value"] == "MERALCO"


def test_amount_range_and_relative_uploaded_dates():
    plan = parse_local_search("invoices between ₱1,000 and ₱10,000 uploaded last month", page="reports", context=CONTEXT, today=NOW)
    assert plan["numbers"][0] == {"field": "total_amount", "operator": "between", "value": 1000.0, "value2": 10000.0}
    assert plan["dates"][0] == {"field": "date_uploaded", "operator": "between", "value": "2026-09-01", "value2": "2026-09-30"}


def test_log_local_actions_and_user():
    plan = parse_local_search("Show EDIT actions last month by user Juan", page="log", context=CONTEXT, today=NOW)
    assert {i["field"] for i in plan["text"]} == {"user", "action"}
    assert plan["dates"][0]["field"] == "date_time"


def test_unknown_words_not_silently_dropped():
    assert parse_local_search("Find expensive electricity bills that might be incorrect", page="history", context=CONTEXT,today=NOW) is None
    assert parse_local_search("Show invoices with suspicious customer details", page="history", context=CONTEXT,today=NOW) is None
    assert parse_local_search("Find Globe and Meralco invoices", page="history", context=CONTEXT,today=NOW) is None


def test_missing_invoice_number_without_llm_call(monkeypatch):
    monkeypatch.setattr(si, "interpret_natural_language_search", lambda *a,**kw: pytest.fail("LLM called"))
    plan, warnings, source = si.interpret_hybrid_search("show invoices missing invoice number", page="history",context=CONTEXT)
    assert warnings == []
    assert source.startswith("Local")
    assert plan["presence"] == [{"field": "invoice_number", "operator": "missing"}]


def test_complex_search_delegates_to_llm(monkeypatch):
    seen=[]
    def mock_model(q, *, page, context):
        seen.append(context)
        return {"text": [{"field": "vendor_name", "operator": "contains", "value": "MERALCO"}],
                "dates": [], "numbers": [], "choices": [], "booleans": [], "presence": []}, []
    monkeypatch.setattr(si, "interpret_natural_language_search", mock_model)
    plan,warnings,source=si.interpret_hybrid_search("find suspicious MERALCO invoice totals",page="history",context=CONTEXT)
    assert source.startswith("Groq")
    assert len(seen)==1 and "vendors" not in seen[0]
    assert plan["text"][0]["value"]=="MERALCO"


def test_reject_multi_currency_amount_search_without_currency():
    with pytest.raises(si.SearchInterpretationError,match="multiple currencies"):
        si.interpret_hybrid_search("show invoices over 5000",page="history",context=CONTEXT)


def test_does_not_treat_garbled_llm_output_as_show_all(monkeypatch):
    monkeypatch.setattr(si,"interpret_natural_language_search",lambda *a,**kw: ({"text":[],"dates":[],"numbers":[],"choices":[],"booleans":[],"presence":[]},[]))
    with pytest.raises(si.SearchInterpretationError,match="No supported filters"):
        si.interpret_hybrid_search("find the strange invoices",page="history",context=CONTEXT)


def test_local_sqlite_filter_matches_preexisting_row_filter(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database.models import InvoiceORM
    import database.database as db
    eng=create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Factory=sessionmaker(bind=eng)
    with Factory() as s:
        s.add_all([
            InvoiceORM(id=1,invoice_number="A1",vendor_name="MERALCO",total_amount=6000,currency="PHP",invoice_date=date(2026,2,15),locked=False),
            InvoiceORM(id=2,invoice_number="A2",vendor_name="MERALCO",total_amount=7000,currency="USD",invoice_date=date(2026,2,15),locked=False),
            InvoiceORM(id=3,invoice_number="A3",vendor_name="PLDT",total_amount=10000,currency="PHP",invoice_date=date(2026,2,15),locked=False),
            InvoiceORM(id=4,invoice_number="A4",vendor_name="MERALCO",total_amount=8000,currency="PHP",invoice_date=date(2026,7,15),locked=False)
        ]);s.commit()
    monkeypatch.setattr(db,"SessionLocal",Factory)
    rows=[{"id":r.id,"invoice_number":r.invoice_number,"vendor_name":r.vendor_name,"invoice_date":r.invoice_date,
           "total_amount":r.total_amount,"currency":r.currency,"locked":r.locked} for r in Factory().query(InvoiceORM).all()]
    plan=parse_local_search("MERALCO invoices from January to March 2026 above ₱5000",page="history",context=CONTEXT,today=NOW)
    assert apply_hybrid_search(rows,plan,"history")==apply_search_plan(rows,plan,"history")
    assert [r["id"] for r in apply_hybrid_search(rows,plan,"history")]==[1]


def test_local_date_and_sql_like_wildcards_safe(monkeypatch):
    # Literal '%' must not turn into SQL wildcard and broaden results.
    from services.search_service import normalize_search_plan
    rows=[{"id":1,"vendor_name":"A%B","invoice_date":"2026-01-01"},{"id":2,"vendor_name":"AXB","invoice_date":"2026-01-01"}]
    plan,_=normalize_search_plan({"text":[{"field":"vendor_name","operator":"contains","value":"A%B"}]},"history")
    assert [r["id"] for r in apply_search_plan(rows,plan,"history")]==[1]


def test_two_vendor_or_conditions_are_not_anded():
    from services.search_service import summarize_search_plan
    ctx={**CONTEXT,"vendors":["MERALCO","PLDT INC."]}
    plan=parse_local_search("Show MERALCO and PLDT INC. invoices above ₱5000",page="reports",context=ctx,today=NOW)
    assert plan["text"][0]["operator"]=="contains_any"
    rows=[{"id":1,"vendor_name":"MERALCO","total_amount":6000,"currency":"PHP"},
          {"id":2,"vendor_name":"PLDT INC.","total_amount":7000,"currency":"PHP"},
          {"id":3,"vendor_name":"GLOBE","total_amount":8000,"currency":"PHP"}]
    assert [r["id"] for r in apply_search_plan(rows,plan,"reports")]==[1,2]
    assert "contains any" in summarize_search_plan(plan)


def test_sql_candidate_ids_parameterized_or_vendor_names(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database.models import InvoiceORM
    import database.database as db
    from services.search_service import _sql_candidate_ids
    engine=create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Factory=sessionmaker(bind=engine)
    with Factory() as session:
        for i,name in enumerate(("MERALCO", "PLDT INC.", "GLOBE"),1):
            session.add(InvoiceORM(id=i, invoice_number=f"INV-{i}", vendor_name=name,
                                   total_amount=7000, currency="PHP"))
        session.commit()
    monkeypatch.setattr(db,"SessionLocal",Factory)
    ctx={**CONTEXT,"vendors":["MERALCO","PLDT INC."]}
    plan=parse_local_search("Show MERALCO and PLDT INC. invoices above ₱5000",page="history",context=ctx,today=NOW)
    assert _sql_candidate_ids([{"id":1},{"id":2},{"id":3}],plan,"history")=={1,2}
