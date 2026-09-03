"""Data-access layer — keeps SQLAlchemy specifics out of the services layer."""
from sqlalchemy.orm import Session
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from database.models import InvoiceORM, LineItemORM, VendorORM
from models.invoice import Invoice
from models.vendor import Vendor


class DuplicateInvoiceError(Exception):
    """Raised when an invoice with the same invoice_number already exists.

    Duplicate invoice numbers are rejected outright — they are never saved
    to the database, so they never show up in History.
    """

    def __init__(self, invoice_number: str, existing_id: int | None = None):
        self.invoice_number = invoice_number
        self.existing_id = existing_id
        super().__init__(
            f"Invoice number '{invoice_number}' already exists"
            + (f" (invoice ID {existing_id})" if existing_id is not None else "")
        )


class InvoiceLockedError(Exception):
    """Raised when trying to edit an invoice that has been locked.

    Locking an invoice on the History page means its data has been reviewed
    and confirmed correct — it must be unlocked again before it can be
    edited further.
    """

    def __init__(self, invoice_id: int):
        self.invoice_id = invoice_id
        super().__init__(f"Invoice {invoice_id} is locked and can't be edited — unlock it first.")


class InvoiceRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_or_create_vendor(self, name: str, address: str | None = None, tax_id: str | None = None) -> VendorORM:
        normalized = Vendor.normalize(name)
        vendor = (
            self.session.query(VendorORM)
            .filter(VendorORM.normalized_name == normalized)
            .first()
        )
        if vendor:
            return vendor
        vendor = VendorORM(name=name, normalized_name=normalized, address=address, tax_id=tax_id)
        self.session.add(vendor)
        self.session.flush()
        return vendor

    def get_by_invoice_number(self, invoice_number: str) -> InvoiceORM | None:
        if not invoice_number:
            return None
        return (
            self.session.query(InvoiceORM)
            .filter(InvoiceORM.invoice_number == invoice_number)
            .first()
        )

    def save(self, invoice: Invoice) -> InvoiceORM:
        # Reject duplicate invoice numbers up front — a duplicate must never
        # be persisted (and therefore never appears in History).
        existing = self.get_by_invoice_number(invoice.invoice_number)
        if existing:
            raise DuplicateInvoiceError(invoice.invoice_number, existing.id)

        vendor = self.get_or_create_vendor(
            invoice.vendor_name, invoice.vendor_address, invoice.vendor_tax_id
        )

        orm_invoice = InvoiceORM(
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            vendor_id=vendor.id,
            vendor_name=invoice.vendor_name,
            vendor_address=invoice.vendor_address,
            vendor_tax_id=invoice.vendor_tax_id,
            customer_name=invoice.customer_name,
            customer_contact=invoice.customer_contact,
            customer_address=invoice.customer_address,
            customer_tax_id=invoice.customer_tax_id,
            plate_number=invoice.plate_number,
            subtotal=invoice.subtotal,
            tax_amount=invoice.tax_amount,
            tax_rate=invoice.tax_rate,
            discount=invoice.discount,
            zero_rated_sales=invoice.zero_rated_sales,
            vat_exempt_sales=invoice.vat_exempt_sales,
            total_amount=invoice.total_amount,
            currency=invoice.currency,
            payment_terms=invoice.payment_terms,
            source_file=invoice.source_file,
            enhanced_image_path=invoice.enhanced_image_path,
            original_filename=invoice.original_filename,
            ocr_engine_used=invoice.ocr_engine_used,
            confidence_score=invoice.confidence_score,
            status=invoice.status,
            raw_text=invoice.raw_text,
            vision_notes=invoice.vision_notes,
        )
        orm_invoice.line_items = [
            LineItemORM(
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                amount=li.amount,
            )
            for li in invoice.line_items
        ]

        self.session.add(orm_invoice)
        try:
            self.session.commit()
        except IntegrityError:
            # Fallback in case two uploads with the same invoice number raced
            # past the check above — the unique constraint on invoice_number
            # is the last line of defense.
            self.session.rollback()
            existing = self.get_by_invoice_number(invoice.invoice_number)
            raise DuplicateInvoiceError(invoice.invoice_number, existing.id if existing else None)
        self.session.refresh(orm_invoice)
        return orm_invoice

    def update(self, invoice_id: int, data: dict) -> InvoiceORM | None:
        """Apply manual corrections to an existing invoice's header fields.

        Used by the History page's "Edit" action to fix OCR misreads — e.g.
        invoices where labels/values are misaligned, or the scan is blurred.

        `data` may include any of: invoice_number, invoice_date, due_date,
        vendor_name, vendor_address, vendor_tax_id, customer_name,
        customer_contact, customer_address, customer_tax_id, plate_number, subtotal,
        tax_amount, tax_rate, discount, zero_rated_sales, vat_exempt_sales,
        total_amount, currency, payment_terms, status, line_items (a
        full-replace list of {description, quantity, unit_price, amount}
        dicts — see below). Unrecognized keys are ignored.

        Raises DuplicateInvoiceError if the edit would rename invoice_number
        to one that already belongs to a *different* invoice.
        """
        obj = self.get_by_id(invoice_id)
        if not obj:
            return None

        if obj.locked:
            raise InvoiceLockedError(invoice_id)

        new_number = data.get("invoice_number")
        if new_number and new_number != obj.invoice_number:
            existing = self.get_by_invoice_number(new_number)
            if existing and existing.id != invoice_id:
                raise DuplicateInvoiceError(new_number, existing.id)

        if data.get("vendor_name"):
            vendor = self.get_or_create_vendor(
                data["vendor_name"], data.get("vendor_address"), data.get("vendor_tax_id")
            )
            obj.vendor_id = vendor.id
            obj.vendor_name = data["vendor_name"]

        editable_fields = [
            "invoice_number", "invoice_date", "due_date", "customer_name",
            "customer_contact", "customer_address", "customer_tax_id", "plate_number",
            "vendor_address", "vendor_tax_id",
            "subtotal", "tax_amount", "tax_rate", "discount",
            "zero_rated_sales", "vat_exempt_sales", "total_amount",
            "currency", "payment_terms", "status", "category",
        ]
        for field in editable_fields:
            if field in data:
                setattr(obj, field, data[field])

        if "line_items" in data:
            # Full replace, not a merge — matches how the History page's
            # line-items editor works (it always submits the complete,
            # current set of rows). The relationship's cascade="all,
            # delete-orphan" (see database/models.py) means reassigning
            # this list is enough; SQLAlchemy deletes the rows that are no
            # longer referenced and inserts the new ones on commit.
            obj.line_items = [
                LineItemORM(
                    description=li.get("description") or "",
                    quantity=li.get("quantity") or 0.0,
                    unit_price=li.get("unit_price") or 0.0,
                    amount=li.get("amount") or 0.0,
                )
                for li in (data["line_items"] or [])
            ]

        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            number = data.get("invoice_number") or obj.invoice_number
            existing = self.get_by_invoice_number(number)
            raise DuplicateInvoiceError(number, existing.id if existing else None)

        self.session.refresh(obj)
        return obj

    def get_by_id(self, invoice_id: int) -> InvoiceORM | None:
        return self.session.query(InvoiceORM).filter(InvoiceORM.id == invoice_id).first()

    def set_locked(self, invoice_id: int, locked: bool) -> InvoiceORM | None:
        """Lock (or unlock) an invoice.

        A locked invoice is one whose extracted data has been reviewed and
        confirmed correct — `update()` refuses to edit it until it's
        unlocked again. Reports/export gating checks this flag too.
        """
        obj = self.get_by_id(invoice_id)
        if not obj:
            return None
        obj.locked = locked
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def list_all(self, limit: int = 100, offset: int = 0, status: str | None = None) -> list[InvoiceORM]:
        q = self.session.query(InvoiceORM)
        if status:
            q = q.filter(InvoiceORM.status == status)
        return q.order_by(desc(InvoiceORM.created_at)).offset(offset).limit(limit).all()

    def search(self, keyword: str) -> list[InvoiceORM]:
        like = f"%{keyword}%"
        return (
            self.session.query(InvoiceORM)
            .filter(
                (InvoiceORM.invoice_number.ilike(like))
                | (InvoiceORM.vendor_name.ilike(like))
                | (InvoiceORM.customer_name.ilike(like))
            )
            .order_by(desc(InvoiceORM.created_at))
            .all()
        )

    def delete(self, invoice_id: int) -> bool:
        obj = self.get_by_id(invoice_id)
        if not obj:
            return False
        if obj.locked:
            raise InvoiceLockedError(invoice_id)
        self.session.delete(obj)
        self.session.commit()
        return True

    def stats(self) -> dict:
        from sqlalchemy import func
        total_invoices = self.session.query(func.count(InvoiceORM.id)).scalar() or 0
        total_amount = self.session.query(func.sum(InvoiceORM.total_amount)).scalar() or 0.0
        # Distinct vendors among invoices that currently exist — not a count
        # of the `vendors` table itself. That table is a permanent dimension
        # table (kept around for name-normalization/dedup lookups even after
        # an invoice is deleted), so counting its rows directly used to keep
        # "Vendors" inflated after the last invoice for a vendor was removed.
        # vendor_id is set on every invoice at creation (see
        # get_or_create_vendor above), and COUNT(DISTINCT ...) ignores NULLs
        # on its own, so this stays accurate without extra filtering.
        vendor_count = (
            self.session.query(func.count(func.distinct(InvoiceORM.vendor_id))).scalar() or 0
        )
        needs_review = (
            self.session.query(func.count(InvoiceORM.id))
            .filter(InvoiceORM.status == "needs_review")
            .scalar()
            or 0
        )
        return {
            "total_invoices": total_invoices,
            "total_amount": round(total_amount, 2),
            "vendor_count": vendor_count,
            "needs_review": needs_review,
        }
