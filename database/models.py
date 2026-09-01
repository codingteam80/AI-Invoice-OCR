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


class InvoiceORM(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    invoice_number = Column(String, unique=True, index=True, nullable=False)
    invoice_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)

    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor = relationship("VendorORM", back_populates="invoices")
    vendor_name = Column(String, nullable=False)

    customer_name = Column(String, nullable=True)

    subtotal = Column(Float, default=0.0)
    tax_amount = Column(Float, nullable=True)
    tax_rate = Column(Float, nullable=True)
    discount = Column(Float, nullable=True)
    total_amount = Column(Float, default=0.0)
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
