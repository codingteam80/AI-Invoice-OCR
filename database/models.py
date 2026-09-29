"""SQLAlchemy ORM models (distinct from the Pydantic models in /models)."""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Date, DateTime, ForeignKey, Text
)
from sqlalchemy.orm import relationship
from database.database import Base


class VendorORM(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    normalized_name = Column(String, index=True)
    address = Column(String, nullable=True)
    tax_id = Column(String, nullable=True)

    invoices = relationship("InvoiceORM", back_populates="vendor")


class CustomCategoryORM(Base):
    """User-defined invoice category. The eight built-in categories live in config/constants.py and are immutable."""
    __tablename__ = "custom_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class InvoiceORM(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    invoice_number = Column(String, unique=True, index=True, nullable=False)
    invoice_date = Column(Date, nullable=True)
    date_uploaded = Column(Date, default=date.today, nullable=True)
    due_date = Column(Date, nullable=True)

    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor = relationship("VendorORM", back_populates="invoices")
    vendor_name = Column(String, nullable=False)
    # Denormalized copies (same pattern as vendor_name above) so History/API
    # can read these straight off the invoice row without a join — these
    # were previously only written to the vendors dimension table (see
    # InvoiceRepository.get_or_create_vendor) and never made it back out to
    # the user anywhere, even though extraction was already capturing them.
    vendor_address = Column(String, nullable=True)
    vendor_tax_id = Column(String, nullable=True)

    customer_name = Column(String, nullable=True)
    customer_contact = Column(String, nullable=True)
    customer_address = Column(String, nullable=True)
    customer_tax_id = Column(String, nullable=True)
    plate_number = Column(String, nullable=True)

    subtotal = Column(Float, default=0.0)
    tax_amount = Column(Float, nullable=True)
    tax_rate = Column(Float, nullable=True)
    discount = Column(Float, nullable=True)
    withholding_tax = Column(Float, nullable=True)
    # Philippine BIR sales-breakdown columns, sibling to `subtotal`
    # (VATABLE SALES) — see models/invoice.py and config/constants.py's
    # EXTRACTION_SCHEMA for what these represent.
    zero_rated_sales = Column(Float, nullable=True)
    vat_exempt_sales = Column(Float, nullable=True)
    total_amount = Column(Float, default=0.0)
    current_charges_total = Column(Float, nullable=True)
    previous_balance = Column(Float, nullable=True)
    currency = Column(String, default="USD")

    payment_terms = Column(String, nullable=True)
    category = Column(String, default="Others", nullable=False)
    source_file = Column(String, nullable=True)
    # See models/invoice.py::Invoice.enhanced_image_path — mirrors that
    # field so History can render a "cleaned-up" view alongside the
    # original. Nullable: PDFs and photos where no document edge was
    # confidently detected have no enhanced version.
    enhanced_image_path = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    ocr_engine_used = Column(String, nullable=True)
    confidence_score = Column(Float, nullable=True)
    status = Column(String, default="pending")
    raw_text = Column(Text, nullable=True)
    # Notes from ai/vision_verifier.py's independent image cross-check —
    # populated only when a vision-capable model flags a mismatch between
    # what was extracted and what it sees in the source image. Null when
    # verification is disabled, found nothing, or wasn't run for this
    # invoice (e.g. it predates the feature).
    vision_notes = Column(Text, nullable=True)
    locked = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    line_items = relationship("LineItemORM", back_populates="invoice", cascade="all, delete-orphan")


class LineItemORM(Base):
    __tablename__ = "line_items"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    invoice = relationship("InvoiceORM", back_populates="line_items")

    description = Column(String, nullable=False)
    quantity = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    amount = Column(Float, default=0.0)


class AuditLogORM(Base):
    """Append-only user activity log shown on the Log page."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False)
    action = Column(String, nullable=False, index=True)
    entity_type = Column(String, nullable=True)
    entity_id = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class UserORM(Base):
    """Local app account. Passwords are stored only as salted PBKDF2 hashes."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)  # stored lowercase
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user", nullable=False)  # "admin" | "user"
    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)


class BrowserLoginSessionORM(Base):
    """Local revocable browser login; random tokens are stored only as hashes."""
    __tablename__ = "browser_login_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, unique=True, nullable=False, index=True)
    password_fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
