"""Business logic layer: process an uploaded invoice end-to-end and persist it."""
import json
from ai.vision_budget import vision_scope
from pathlib import Path
from config.constants import SUPPORTED_IMAGE_EXTS
from config.settings import settings
from config.logging import get_logger
from database.database import SessionLocal
from database.repository import InvoiceRepository, DuplicateInvoiceError, InvoiceLockedError
from database.models import InvoiceORM
from parser.invoice_parser import parse_invoice_pages, InvoiceParsingError
from utils.file_utils import move_to_processed, sanitize_filename_component
from utils.categorizer import auto_categorize
from services.audit_service import log_action

logger = get_logger("services.invoice")


_AUDIT_FIELD_LABELS = {
    "invoice_number": "Invoice #",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "vendor_name": "Vendor",
    "vendor_address": "Vendor Address",
    "vendor_tax_id": "Vendor TIN",
    "customer_name": "Customer",
    "customer_contact": "Customer Contact",
    "customer_address": "Customer Address",
    "customer_tax_id": "Customer TIN",
    "plate_number": "Plate #",
    "subtotal": "Vatable Sales",
    "tax_amount": "VAT",
    "discount": "Discount (internal)",
    "withholding_tax": "Withholding Tax",
    "zero_rated_sales": "Zero-Rated Sales",
    "vat_exempt_sales": "VAT-Exempt Sales",
    "total_amount": "Total Amount Due",
    "current_charges_total": "Current Charges Total",
    "previous_balance": "Previous Balance",
    "currency": "Currency",
    "payment_terms": "Payment Terms",
    "status": "Status",
    "category": "Category",
    "line_items": "Items Purchased / Line Items",
}


def _audit_display_value(value) -> str:
    """Compact, readable representation used in the Activity Log Details column."""
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, list):
        if not value:
            return "(none)"
        parts = []
        for item in value:
            if isinstance(item, dict):
                desc = item.get("description") or "(unnamed item)"
                qty = item.get("quantity") or 0
                unit = item.get("unit_price") or 0
                amount = item.get("amount") or 0
                parts.append(f"{desc} [Qty {qty:g}, Unit {unit:,.2f}, Total {amount:,.2f}]")
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(value)


def _describe_invoice_changes(before: dict, after: dict, submitted_fields) -> str:
    """Return only fields whose persisted values actually changed, with old/new values."""
    changes = []
    for field in submitted_fields:
        if field not in _AUDIT_FIELD_LABELS:
            continue
        old_value = before.get(field)
        new_value = after.get(field)
        if old_value == new_value:
            continue
        label = _AUDIT_FIELD_LABELS[field]
        changes.append(
            f"{label}: {_audit_display_value(old_value)} → {_audit_display_value(new_value)}"
        )
    return "\n".join(changes) if changes else "No field values changed."


def _generate_enhanced_image(file_path: str, enabled: bool | None = None) -> str | None:
    """Best-effort CamScanner-style enhanced copy for History to display.
    Returns the saved path, or None on any failure/skip — this must never
    raise, since a cosmetic enhancement failing is not a reason to fail
    processing the actual invoice.

    enabled: None (default) follows settings.ENHANCE_IMAGE_ENABLED;
    True/False overrides it for this call — lets the Upload page offer a
    per-batch checkbox without flipping the global default for everyone
    else, same pattern as force_handwritten below.
    """
    if enabled is None:
        enabled = settings.ENHANCE_IMAGE_ENABLED
    if not enabled:
        return None
    if Path(file_path).suffix.lower() not in SUPPORTED_IMAGE_EXTS:
        return None  # camscan() works on photos; PDFs are handled separately for OCR
    try:
        from ocr.preprocessing import camscan
        dest = Path(settings.ENHANCED_DIR) / f"{Path(file_path).stem}_scanned.jpg"
        camscan(file_path, save_path=str(dest))
        return str(dest)
    except Exception as e:
        logger.warning(f"Image enhancement failed for {file_path}, continuing without it: {e}")
        return None


@vision_scope
def process_invoice_file(
    file_path: str,
    force_handwritten: bool | None = None,
    original_filename: str | None = None,
    enhance_image: bool | None = None,
) -> list[dict]:
    """
    Runs the full parsing pipeline on a saved file, persists EVERY invoice
    found in it, archives a JSON copy of each, and moves the source file to
    'processed'. Returns a list of plain dict summaries (JSON-serializable)
    — one per invoice.

    A file is not always exactly one invoice: a multi-page PDF may contain
    several separate invoices, one per page (see
    parser.invoice_parser.parse_invoice_pages). A single-page file/image
    still returns a one-element list — callers must always iterate the
    result rather than assume a single dict.

    force_handwritten: None to auto-detect the OCR engine (default), or
    True/False to override the handwriting detector.

    original_filename: the name the user actually uploaded (file_path itself
    is the on-disk, UUID-renamed copy) — stored so History can show it. For
    a multi-invoice file, each invoice's stored original_filename gets a
    "(page N/M)" suffix so they're distinguishable in History.

    enhance_image: None (default) follows settings.ENHANCE_IMAGE_ENABLED,
    or True/False to override per-call (see Upload.py's checkbox).
    """
    try:
        invoices = parse_invoice_pages(file_path, force_handwritten=force_handwritten)
    except InvoiceParsingError as e:
        logger.error(f"Parsing failed for {file_path}: {e}")
        return [{"success": False, "error": str(e), "file": file_path}]

    base_filename = original_filename or Path(file_path).name
    multi_invoice = len(invoices) > 1

    # Enhance BEFORE move_to_processed() relocates file_path further down —
    # camscan() needs to read the file from its current location. Only
    # meaningful for a standalone photo (see _generate_enhanced_image);
    # for a multi-page PDF this is just None for every invoice, same as
    # before.
    enhanced_image_path = _generate_enhanced_image(file_path, enabled=enhance_image)

    results = []
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        for i, invoice in enumerate(invoices, start=1):
            invoice.original_filename = (
                f"{base_filename} (page {i}/{len(invoices)})" if multi_invoice else base_filename
            )
            invoice.vendor_name = (invoice.vendor_name or "Unknown Vendor").strip().upper()
            invoice.customer_name = (invoice.customer_name or "").strip().upper() or None
            invoice.category = auto_categorize(invoice.vendor_name, invoice.line_items)
            invoice.enhanced_image_path = enhanced_image_path

            try:
                orm_invoice = repo.save(invoice)
            except DuplicateInvoiceError as e:
                logger.warning(
                    f"Duplicate invoice number skipped for {file_path} "
                    f"(invoice {i}/{len(invoices)}): {e}"
                )
                # Duplicate invoice numbers are never saved to the DB, so
                # they never show up in History.
                results.append({
                    "success": False,
                    "duplicate": True,
                    "invoice_number": invoice.invoice_number,
                    "existing_invoice_id": e.existing_id,
                    "error": (
                        f"Duplicate invoice number '{invoice.invoice_number}' — "
                        f"already exists as invoice #{e.existing_id}. Skipped; "
                        "not added to History."
                    ),
                    "file": invoice.original_filename,
                })
                continue

            invoice.id = orm_invoice.id
            _archive_json(invoice)
            log_action("UPLOAD", "invoice", invoice.id, f"Uploaded and processed {invoice.original_filename}; invoice #{invoice.invoice_number}")
            results.append({
                "success": True,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
                "vendor_address": invoice.vendor_address,
                "vendor_tax_id": invoice.vendor_tax_id,
                "customer_name": invoice.customer_name,
                "customer_address": invoice.customer_address,
                "customer_tax_id": invoice.customer_tax_id,
                "plate_number": invoice.plate_number,
                "original_filename": invoice.original_filename,
                "subtotal": invoice.subtotal,
                "tax_amount": invoice.tax_amount,
                "zero_rated_sales": invoice.zero_rated_sales,
                "vat_exempt_sales": invoice.vat_exempt_sales,
                "total_amount": invoice.total_amount,
                "current_charges_total": invoice.current_charges_total,
                "previous_balance": invoice.previous_balance,
                "currency": invoice.currency,
                "category": invoice.category,
                "confidence_score": invoice.confidence_score,
                "status": invoice.status,
                "ocr_engine_used": invoice.ocr_engine_used,
                "vision_notes": invoice.vision_notes,
            })
    finally:
        session.close()

    # Move the source file out of uploads/ ONCE (it's one physical file on
    # disk regardless of how many invoices were extracted from it), then
    # point every successfully-saved invoice's source_file at the final
    # processed path — repo.save() above persisted them pointing at the
    # (now-empty) upload path, since the move happens after saving.
    try:
        processed_path = move_to_processed(file_path)
    except Exception as e:
        logger.warning(f"Could not move file to processed dir: {e}")
        processed_path = file_path

    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        for r in results:
            if r.get("success") and r.get("invoice_id"):
                try:
                    repo.update(r["invoice_id"], {"source_file": processed_path})
                except Exception as e:
                    logger.warning(f"Could not update source_file for invoice {r['invoice_id']}: {e}")
    finally:
        session.close()

    for r in results:
        r["processed_file"] = processed_path

    return results


def _archive_json(invoice) -> str:
    # invoice_number comes straight from OCR/LLM extraction and isn't
    # guaranteed to be filesystem-safe (e.g. a stray '/' or '\\' turns one
    # path segment into two and blows up write_text with a
    # FileNotFoundError, since that nested "directory" was never created —
    # see utils.file_utils.sanitize_filename_component).
    safe_invoice_number = sanitize_filename_component(invoice.invoice_number or "unknown")
    out_path = Path(settings.JSON_DIR) / f"invoice_{safe_invoice_number}_{invoice.id}.json"
    # raw_text is included (previously excluded) so this dump is enough on
    # its own to diagnose a bad extraction: the raw OCR output next to what
    # the LLM did with it shows whether a wrong value came from the OCR
    # engine misreading the page or from the LLM misinterpreting text that
    # was actually correct. Without it, storage/json/ + data/processed/
    # only show the "after" picture, not the "why".
    out_path.write_text(invoice.model_dump_json(indent=2), encoding="utf-8")
    return str(out_path)


def list_invoices(limit: int = 100, status: str | None = None) -> list[dict]:
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        rows = repo.list_all(limit=limit, status=status)
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def get_invoice(invoice_id: int) -> dict | None:
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        row = repo.get_by_id(invoice_id)
        return _orm_to_dict(row) if row else None
    finally:
        session.close()


def update_invoice(invoice_id: int, updates: dict, audit: bool = True) -> dict:
    """Apply manual corrections to an invoice's header fields.

    Used by the History page's "Edit" action — this is how a user fixes
    OCR misreads on invoices where the label/value layout tripped up
    extraction (common on some Philippine invoice layouts) or the scan was
    blurred. Raises DuplicateInvoiceError (see database.repository) if the
    edit renames invoice_number to one already used by a different invoice.
    """
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        before_row = repo.get_by_id(invoice_id)
        if before_row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        before = _orm_to_dict(before_row)
        submitted_fields = list(updates.keys())

        row = repo.update(invoice_id, updates)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        result = _orm_to_dict(row)
        if audit:
            changes = _describe_invoice_changes(before, result, submitted_fields)
            log_action(
                "EDIT",
                "invoice",
                invoice_id,
                f"Invoice #{result.get('invoice_number')}:\n{changes}",
            )
        return result
    finally:
        session.close()


def lock_invoice(invoice_id: int) -> dict:
    """Mark an invoice as locked — its data has been reviewed and confirmed
    correct. Locked invoices can't be edited until unlocked again, and are
    the gate for exporting on the Reports page.
    """
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        before = repo.get_by_id(invoice_id)
        if before is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        old_locked = bool(before.locked)
        old_status = before.status

        row = repo.set_locked(invoice_id, True)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        result = _orm_to_dict(row)
        changes = [f"Locked: {'Yes' if old_locked else 'No'} → Yes"]
        if old_status != result.get("status"):
            changes.append(f"Status: {old_status or '—'} → {result.get('status') or '—'}")
        log_action(
            "LOCK", "invoice", invoice_id,
            f"Invoice #{result.get('invoice_number')}:\n" + "\n".join(changes),
        )
        return result
    finally:
        session.close()


def unlock_invoice(invoice_id: int) -> dict:
    """Unlock an invoice so it can be edited again."""
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        before = repo.get_by_id(invoice_id)
        if before is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        old_locked = bool(before.locked)
        old_status = before.status

        row = repo.set_locked(invoice_id, False)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        result = _orm_to_dict(row)
        changes = [f"Locked: {'Yes' if old_locked else 'No'} → No"]
        if old_status != result.get("status"):
            changes.append(f"Status: {old_status or '—'} → {result.get('status') or '—'}")
        log_action(
            "UNLOCK", "invoice", invoice_id,
            f"Invoice #{result.get('invoice_number')}:\n" + "\n".join(changes),
        )
        return result
    finally:
        session.close()


def delete_invoice(invoice_id: int) -> None:
    """Permanently delete an invoice — e.g. to clear out a bad duplicate
    upload. Raises InvoiceLockedError if the invoice is locked (unlock it
    on the History page first); raises ValueError if it doesn't exist.
    """
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        before = repo.get_by_id(invoice_id)
        invoice_number = before.invoice_number if before else None
        deleted = repo.delete(invoice_id)
        if not deleted:
            raise ValueError(f"Invoice {invoice_id} not found")
        log_action("DELETE", "invoice", invoice_id, f"Deleted invoice #{invoice_number or '-'}")
    finally:
        session.close()


def list_custom_categories() -> list[str]:
    session = SessionLocal()
    try:
        return InvoiceRepository(session).list_custom_categories()
    finally:
        session.close()


def add_custom_category(name: str) -> str:
    session = SessionLocal()
    try:
        added = InvoiceRepository(session).add_custom_category(name)
        log_action("ADD CATEGORY", "category", None, f"Added custom category: {added}")
        return added
    finally:
        session.close()


def delete_custom_category(name: str) -> int:
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        affected_before = {
            row.id: {"invoice_number": row.invoice_number, "category": row.category}
            for row in session.query(InvoiceORM).filter(InvoiceORM.category == name).all()
        }
        count = repo.delete_custom_category(name)
        log_action("DELETE CATEGORY", "category", None, f"Deleted custom category: {name}; re-analysed {count} invoice(s)")

        # Record the actual category field transition for every invoice that
        # was automatically reclassified after the custom category was deleted.
        for invoice_id, previous in affected_before.items():
            row = repo.get_by_id(invoice_id)
            if row is None:
                continue
            new_category = row.category or "Others"
            if previous["category"] != new_category:
                log_action(
                    "REASSIGN CATEGORY",
                    "invoice",
                    invoice_id,
                    f"Invoice #{previous['invoice_number']}: Category: {previous['category'] or '—'} → {new_category}",
                )
        return count
    finally:
        session.close()


def search_invoices(keyword: str) -> list[dict]:
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        rows = repo.search(keyword)
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def get_dashboard_stats() -> dict:
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        return repo.stats()
    finally:
        session.close()


def _orm_to_dict(row) -> dict:
    return {
        "id": row.id,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "vendor_name": row.vendor_name,
        "vendor_address": row.vendor_address,
        "vendor_tax_id": row.vendor_tax_id,
        "customer_name": row.customer_name,
        "customer_contact": row.customer_contact,
        "customer_address": row.customer_address,
        "customer_tax_id": row.customer_tax_id,
        "plate_number": row.plate_number,
        "original_filename": row.original_filename,
        "source_file": row.source_file,
        "enhanced_image_path": row.enhanced_image_path,
        "category": row.category,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "date_uploaded": row.date_uploaded.isoformat() if row.date_uploaded else (row.created_at.date().isoformat() if row.created_at else None),
        "subtotal": row.subtotal,
        "tax_amount": row.tax_amount,
        "tax_rate": row.tax_rate,
        "discount": row.discount,
        "withholding_tax": row.withholding_tax,
        "zero_rated_sales": row.zero_rated_sales,
        "vat_exempt_sales": row.vat_exempt_sales,
        "total_amount": row.total_amount,
        "current_charges_total": row.current_charges_total,
        "previous_balance": row.previous_balance,
        "currency": row.currency,
        "payment_terms": row.payment_terms,
        "status": row.status,
        "locked": row.locked,
        "confidence_score": row.confidence_score,
        "ocr_engine_used": row.ocr_engine_used,
        "raw_text": row.raw_text,
        "vision_notes": row.vision_notes,
        "line_items": [
            {"description": li.description, "quantity": li.quantity, "unit_price": li.unit_price, "amount": li.amount}
            for li in row.line_items
        ],
    }
