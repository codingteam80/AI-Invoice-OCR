from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.database import Base
from database.models import InvoiceORM, LineItemORM
from database.repository import InvoiceRepository
from utils.categorizer import categorize_with_custom_categories


def test_custom_reanalysis_prefers_builtin_when_evidence_exists():
    assert categorize_with_custom_categories("Watsons Personal Care", [{"description":"Vitamin softgel"}], {"Special":"Watsons vitamin"}) == "Medical & Health Supplies"


def test_custom_reanalysis_can_use_remaining_custom_profile():
    profiles = {"Hotel & Lodging": ["ACME HOTEL room accommodation deluxe room"]}
    got = categorize_with_custom_categories("ACME HOTEL", [{"description":"room accommodation"}], profiles)
    assert got == "Hotel & Lodging"


def test_delete_custom_category_reanalyses_affected_invoices():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session(); repo = InvoiceRepository(session)
    repo.add_custom_category("Old Travel")
    repo.add_custom_category("Hotel & Lodging")
    # Profile example for the remaining custom category.
    a = InvoiceORM(invoice_number="A", vendor_name="ACME HOTEL", category="Hotel & Lodging", locked=False)
    a.line_items = [LineItemORM(description="room accommodation", quantity=1, unit_price=1, amount=1)]
    # Affected invoice should be learned into Hotel & Lodging, even when locked.
    b = InvoiceORM(invoice_number="B", vendor_name="ACME HOTEL", category="Old Travel", locked=True)
    b.line_items = [LineItemORM(description="room accommodation deluxe", quantity=1, unit_price=1, amount=1)]
    # Built-in evidence should go to Transportation.
    c = InvoiceORM(invoice_number="C", vendor_name="Parking Corp", category="Old Travel", locked=False)
    c.line_items = [LineItemORM(description="parking fee", quantity=1, unit_price=1, amount=1)]
    session.add_all([a,b,c]); session.commit()
    changed = repo.delete_custom_category("Old Travel")
    assert changed == 2
    assert repo.get_by_invoice_number("B").category == "Hotel & Lodging"
    assert repo.get_by_invoice_number("C").category == "Transportation"
    assert repo.get_by_invoice_number("B").locked is True


def test_history_ui_has_add_category_below_others_and_bulk_lock():
    text = Path("ui/pages/History.py").read_text(encoding="utf-8")
    assert 'add_category_choice = "➕ Add new category..."' in text
    assert 'CATEGORY_OPTIONS + [add_category_choice]' in text
    assert 'with st.expander("🗑️ Manage custom categories")' in text
    assert 'Lock selected' in text
    # Add controls live under the Category selector, not in management expander.
    manage = text.split('with st.expander("🗑️ Manage custom categories")', 1)[1]
    assert 'st.button("Add Category"' not in manage.split('computed =',1)[0]
