"""Business logic layer: process an uploaded invoice end-to-end and persist it."""
import json
from pathlib import Path
from config.constants import SUPPORTED_IMAGE_EXTS
from config.settings import settings
from config.logging import get_logger
from database.database import SessionLocal
from database.repository import InvoiceRepository, DuplicateInvoiceError, InvoiceLockedError
from parser.invoice_parser import parse_invoice_pages, InvoiceParsingError
from utils.file_utils import move_to_processed, sanitize_filename_component
from utils.categorizer import auto_categorize

logger = get_logger("services.invoice")


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
                "currency": invoice.currency,
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


def update_invoice(invoice_id: int, updates: dict) -> dict:
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
        row = repo.update(invoice_id, updates)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        return _orm_to_dict(row)
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
        row = repo.set_locked(invoice_id, True)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        return _orm_to_dict(row)
    finally:
        session.close()


def unlock_invoice(invoice_id: int) -> dict:
    """Unlock an invoice so it can be edited again."""
    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        row = repo.set_locked(invoice_id, False)
        if row is None:
            raise ValueError(f"Invoice {invoice_id} not found")
        return _orm_to_dict(row)
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
        deleted = repo.delete(invoice_id)
        if not deleted:
            raise ValueError(f"Invoice {invoice_id} not found")
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
        "subtotal": row.subtotal,
        "tax_amount": row.tax_amount,
        "discount": row.discount,
        "zero_rated_sales": row.zero_rated_sales,
        "vat_exempt_sales": row.vat_exempt_sales,
        "total_amount": row.total_amount,
        "currency": row.currency,
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
