from pathlib import Path

from config.constants import CATEGORY_OPTIONS
from database.database import Base
from database.models import InvoiceORM, CustomCategoryORM
from database.repository import InvoiceRepository
from models.invoice import Invoice
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _repo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return InvoiceRepository(sessionmaker(bind=engine)())


def test_builtin_category_count_and_names_are_immutable_source_list():
    assert len(CATEGORY_OPTIONS) == 8
    assert "Others" in CATEGORY_OPTIONS


def test_custom_category_add_delete_reassigns_invoice():
    repo = _repo()
    assert repo.add_custom_category("Professional Services") == "Professional Services"
    assert "Professional Services" in repo.list_custom_categories()
    inv = Invoice(invoice_number="C164-1", vendor_name="Acme Inc", customer_name="Buyer Ltd", subtotal=100, total_amount=100, category="Professional Services")
    saved = repo.save(inv)
    saved.category = "Professional Services"; repo.session.commit()
    changed = repo.delete_custom_category("Professional Services")
    assert changed == 1
    assert repo.get_by_id(saved.id).category == "Others"


def test_builtin_category_cannot_be_deleted():
    repo = _repo()
    import pytest
    with pytest.raises(ValueError):
        repo.delete_custom_category("Transportation")


def test_vendor_and_customer_names_saved_uppercase():
    repo = _repo()
    inv = Invoice(invoice_number="C164-2", vendor_name="Globe Business: Innove Communications, Inc.", customer_name="Tsukiden Global Solutions Inc.", subtotal=1, total_amount=1)
    row = repo.save(inv)
    assert row.vendor_name == "GLOBE BUSINESS: INNOVE COMMUNICATIONS, INC."
    assert row.customer_name == "TSUKIDEN GLOBAL SOLUTIONS INC."


def test_history_has_month_separator_and_custom_category_manager():
    text = Path("ui/pages/History.py").read_text(encoding="utf-8")
    assert "st.divider()" in text
    assert "Manage custom categories" in text
    assert "Delete Custom Category" in text


def test_reports_uses_single_confirm_download_button():
    text = Path("ui/pages/Reports.py").read_text(encoding="utf-8")
    assert "Continue Export & Download" in text
    assert 'button("Continue Export"' not in text
