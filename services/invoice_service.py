"""Business logic layer: process an uploaded invoice end-to-end and persist it."""
import json
from pathlib import Path
from config.settings import settings
from config.logging import get_logger
from database.database import SessionLocal
from database.repository import InvoiceRepository, DuplicateInvoiceError, InvoiceLockedError
from parser.invoice_parser import parse_invoice, InvoiceParsingError
from utils.file_utils import move_to_processed, sanitize_filename_component
from utils.categorizer import auto_categorize

logger = get_logger("services.invoice")


def process_invoice_file(
    file_path: str,
    force_handwritten: bool | None = None,
    original_filename: str | None = None,
) -> dict:
    """
    Runs the full parsing pipeline on a saved file, persists the result to
    the database, archives a JSON copy, and moves the source file to
    'processed'. Returns a plain dict summary (JSON-serializable).

    force_handwritten: None to auto-detect the OCR engine (default), or
    True/False to override the handwriting detector.

    original_filename: the name the user actually uploaded (file_path itself
    is the on-disk, UUID-renamed copy) — stored so History can show it.
    """
    try:
        invoice = parse_invoice(file_path, force_handwritten=force_handwritten)
    except InvoiceParsingError as e:
        logger.error(f"Parsing failed for {file_path}: {e}")
        return {"success": False, "error": str(e), "file": file_path}

    invoice.original_filename = original_filename or Path(file_path).name
    invoice.category = auto_categorize(invoice.vendor_name, invoice.line_items)

    session = SessionLocal()
    try:
        repo = InvoiceRepository(session)
        try:
            orm_invoice = repo.save(invoice)
        except DuplicateInvoiceError as e:
            logger.warning(f"Duplicate invoice number skipped for {file_path}: {e}")
            # Duplicate invoice numbers are never saved to the DB, so they
            # never show up in History. Still move the source file out of
            # the uploads folder so it isn't left sitting there / reprocessed.
            try:
                move_to_processed(file_path)
            except Exception as move_err:
                logger.warning(f"Could not move duplicate file to processed dir: {move_err}")
            return {
                "success": False,
                "duplicate": True,
                "invoice_number": invoice.invoice_number,
                "existing_invoice_id": e.existing_id,
                "error": (
                    f"Duplicate invoice number '{invoice.invoice_number}' — "
                    f"already exists as invoice #{e.existing_id}. Skipped; "
                    "not added to History."
                ),
                "file": file_path,
            }
        invoice.id = orm_invoice.id
    finally:
        session.close()

    _archive_json(invoice)

    try:
        processed_path = move_to_processed(file_path)
    except Exception as e:
        logger.warning(f"Could not move file to processed dir: {e}")
        processed_path = file_path
    else:
        # move_to_processed() runs AFTER repo.save(), so the source_file
        # persisted above still points at the (now-empty) upload path.
        # Update it to the final processed path so History's image
        # preview can actually find the file on disk.
        session = SessionLocal()
        try:
            InvoiceRepository(session).update(invoice.id, {"source_file": processed_path})
        except Exception as e:
            logger.warning(f"Could not update source_file after move: {e}")
        finally:
            session.close()

    return {
        "success": True,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "vendor_name": invoice.vendor_name,
        "original_filename": invoice.original_filename,
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "total_amount": invoice.total_amount,
        "currency": invoice.currency,
        "confidence_score": invoice.confidence_score,
        "status": invoice.status,
        "processed_file": processed_path,
        "ocr_engine_used": invoice.ocr_engine_used,
        "vision_notes": invoice.vision_notes,
    }


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
        "customer_name": row.customer_name,
        "original_filename": row.original_filename,
        "source_file": row.source_file,
        "category": row.category,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "subtotal": row.subtotal,
        "tax_amount": row.tax_amount,
        "discount": row.discount,
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
