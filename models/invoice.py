"""Pydantic models representing an invoice and its line items."""
from __future__ import annotations
from datetime import date
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class LineItem(BaseModel):
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    amount: float = 0.0

    @field_validator("amount")
    @classmethod
    def check_amount(cls, v, info):
        # If amount is missing/zero but qty & unit_price exist, derive it.
        qty = info.data.get("quantity", 1.0)
        price = info.data.get("unit_price", 0.0)
        if v == 0.0 and qty and price:
            return round(qty * price, 2)
        return v


class Invoice(BaseModel):
    id: Optional[int] = None
    invoice_number: str
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None

    vendor_name: str
    vendor_address: Optional[str] = None
    vendor_tax_id: Optional[str] = None

    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_address: Optional[str] = None
    customer_tax_id: Optional[str] = None
    plate_number: Optional[str] = None

    line_items: list[LineItem] = Field(default_factory=list)

    subtotal: float = 0.0
    tax_amount: Optional[float] = None
    tax_rate: Optional[float] = None
    discount: Optional[float] = None
    zero_rated_sales: Optional[float] = None
    vat_exempt_sales: Optional[float] = None
    total_amount: float = 0.0
    currency: str = "USD"

    payment_terms: Optional[str] = None
    category: str = "Others"

    source_file: Optional[str] = None
    # Path to the auto-cropped/perspective-corrected/contrast-enhanced
    # version of source_file (see ocr/preprocessing.py::camscan()). None
    # when the source is a PDF (camscan works on photos, not paginated
    # documents) or when no confident document-edge crop was found for
    # this particular photo. Never used for OCR itself — OCR runs against
    # its own preprocessing pass — this is purely so History can show the
    # person a cleaned-up "scanned" view of what they uploaded.
    enhanced_image_path: Optional[str] = None
    original_filename: Optional[str] = None
    ocr_engine_used: Optional[str] = None
    confidence_score: Optional[float] = None
    status: str = "pending"
    raw_text: Optional[str] = None
    vision_notes: Optional[str] = None
    locked: bool = False

    class Config:
        from_attributes = True

    def validate_totals(self, tolerance: float = 0.02) -> bool:
        """Sanity check: subtotal + tax - discount ≈ total."""
        computed = self.subtotal + (self.tax_amount or 0) - (self.discount or 0)
        if self.total_amount == 0:
            return False
        return abs(computed - self.total_amount) <= tolerance * self.total_amount
