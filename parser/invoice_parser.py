"""
Top-level orchestrator: turns a raw invoice file into a validated Invoice
object by chaining OCR -> LLM extraction -> validation -> post-processing.
"""
from config.logging import get_logger
from ocr.ocr_engine import extract_from_file
from ai.extractor import extract_invoice_data, ExtractionError
from ai.validator import validate_extraction
from ai.confidence import score_extraction
from ai.post_processing import post_process, sanitize_for_model, reconcile_total_amount, reconcile_tax
from parser.table_parser import parse_line_items_from_text
from models.invoice import Invoice
from config.constants import INVOICE_STATUS_PROCESSED, INVOICE_STATUS_REVIEW, INVOICE_STATUS_FAILED

logger = get_logger("parser.invoice")


class InvoiceParsingError(Exception):
    pass


def parse_invoice(file_path: str, force_handwritten: bool | None = None) -> Invoice:
    """
    Full pipeline for a single invoice file.
    Raises InvoiceParsingError only on unrecoverable failure (e.g. OCR crash).

    force_handwritten: None to auto-detect (default), or True/False to
    override the handwriting detector and pin the engine explicitly.
    """
    logger.info(f"Starting invoice parse: {file_path}")

    # 1. OCR (auto-routes between PaddleOCR and TrOCR based on handwriting detection)
    ocr_result = extract_from_file(file_path, force_handwritten=force_handwritten)
    ocr_text = ocr_result["text"]
    ocr_confidence = ocr_result["avg_confidence"]

    if not ocr_text.strip():
        raise InvoiceParsingError("OCR produced no text — file may be blank or unreadable.")

    # 2. LLM extraction
    try:
        extracted = extract_invoice_data(ocr_text)
    except ExtractionError as e:
        logger.error(f"LLM extraction failed for {file_path}: {e}")
        raise InvoiceParsingError(str(e)) from e

    # 3. Fallback line items if the LLM returned none
    if not extracted.get("line_items"):
        heuristic_items = parse_line_items_from_text(ocr_text)
        if heuristic_items:
            extracted["line_items"] = heuristic_items

    # 4. Post-process / normalize values
    cleaned = post_process(extracted)

    # 4b. Heuristic backstop: catch cases where total_amount actually matches
    # a CASH/CHANGE line in the OCR text instead of the real TOTAL line
    # (see ai/post_processing.reconcile_total_amount). Auto-corrects when
    # confident, and always surfaces a note so it's still reviewable.
    cleaned, total_notes = reconcile_total_amount(cleaned, ocr_text)

    # 4c. Heuristic backstop: fill tax_rate/tax_amount from a regex scan of
    # the raw OCR text when the LLM left them null/zero (see ai/post_processing.reconcile_tax).
    cleaned, tax_notes = reconcile_tax(cleaned, ocr_text)
    reconciliation_notes = total_notes + tax_notes

    # 5. Validate + score confidence — MUST run before sanitizing, so a
    #    genuinely-missing field still counts against confidence/needs_review.
    issues = validate_extraction(cleaned) + reconciliation_notes
    prediction = score_extraction(cleaned, ocr_confidence, issues)

    # 6. Build Invoice model. Sanitize placeholders now (not earlier) so
    #    documents the LLM couldn't fully read (e.g. a receipt with no
    #    labeled invoice number) get safe defaults instead of crashing —
    #    they're still correctly flagged needs_review by step 5 above.
    safe_data = sanitize_for_model(cleaned)
    try:
        invoice = Invoice(**safe_data)
    except Exception as e:
        logger.error(f"Failed to build Invoice model from extracted data: {e}")
        raise InvoiceParsingError(f"Extracted data failed schema validation: {e}") from e

    invoice.source_file = file_path
    invoice.ocr_engine_used = ocr_result.get("engine")
    invoice.confidence_score = prediction.overall_confidence
    invoice.raw_text = ocr_text
    invoice.status = INVOICE_STATUS_REVIEW if prediction.needs_review else INVOICE_STATUS_PROCESSED

    logger.info(
        f"Parsed invoice {invoice.invoice_number!r} "
        f"(confidence={invoice.confidence_score}, status={invoice.status})"
    )
    return invoice
