"""
Top-level orchestrator: turns a raw invoice file into a validated Invoice
object by chaining OCR -> LLM extraction -> validation -> post-processing.
"""
from pathlib import Path

from config.logging import get_logger
from ocr.ocr_engine import extract_from_file, extract_pages_from_file
from ai.extractor import extract_invoice_data, ExtractionError
from ai.validator import validate_extraction
from ai.confidence import score_extraction
from ai.post_processing import (
    post_process, sanitize_for_model, reconcile_total_amount, reconcile_tax, reconcile_subtotal,
    clean_line_items, reconcile_vendor_name, reconcile_invoice_date, reconcile_swapped_subtotal_total,
    reconcile_invoice_number, reconcile_zero_rated_exempt, reconcile_plate_number,
)
from ai.vision_verifier import verify_against_image
from parser.table_parser import parse_line_items_from_text
from models.invoice import Invoice
from config.constants import INVOICE_STATUS_PROCESSED, INVOICE_STATUS_REVIEW, INVOICE_STATUS_FAILED
from utils.invoice_template_detector import detect_invoice_template
from ai.invoice_templates import get_template

logger = get_logger("parser.invoice")


class InvoiceParsingError(Exception):
    pass


def parse_invoice(file_path: str, force_handwritten: bool | None = None) -> Invoice:
    """
    Full pipeline for a single invoice file, treating the ENTIRE file
    (all pages, if any) as one invoice.

    Kept for backward compatibility (single-invoice callers, tests) — for
    a file that may contain several separate invoices across its pages
    (e.g. a multi-page PDF batch scan), use parse_invoice_pages() instead,
    which runs this same pipeline independently per page rather than
    concatenating every page's OCR text into one extraction call.

    Raises InvoiceParsingError only on unrecoverable failure (e.g. OCR crash).

    force_handwritten: None to auto-detect (default), or True/False to
    override the handwriting detector and pin the engine explicitly.
    """
    logger.info(f"Starting invoice parse: {file_path}")

    # OCR (auto-routes between PaddleOCR and TrOCR based on handwriting detection)
    ocr_result = extract_from_file(file_path, force_handwritten=force_handwritten)

    if not ocr_result["text"].strip():
        raise InvoiceParsingError("OCR produced no text — file may be blank or unreadable.")

    return _build_invoice_from_ocr(
        file_path=file_path,
        image_path=file_path,
        ocr_text=ocr_result["text"],
        ocr_confidence=ocr_result["avg_confidence"],
        ocr_engine=ocr_result.get("engine"),
    )


def parse_invoice_pages(file_path: str, force_handwritten: bool | None = None) -> list[Invoice]:
    """
    Full pipeline for a file that may contain MULTIPLE, separate invoices
    — one per page — rather than a single invoice spread across several
    pages. This is the entrypoint services/invoice_service.py should use
    for anything a user uploads.

    Why this exists: a multi-page PDF is frequently a batch of unrelated
    invoices stacked into one file (e.g. two different vendors' service
    invoices scanned together), not one invoice split across pages. The
    old parse_invoice() ran OCR once, concatenated every page's text into
    a single blob, and asked the LLM for ONE JSON invoice object back —
    which silently discarded every invoice on the file but whichever one
    the extraction happened to latch onto, with no error raised. This
    function instead runs OCR independently PER PAGE (see
    ocr/ocr_engine.py::extract_pages_from_file) and the complete
    extract -> reconcile -> validate -> score pipeline independently for
    EACH page's text, so every invoice in the file gets its own result.

    A single-page image still goes through this same path and simply
    returns a one-element list — callers should always treat the result
    as a list, never assume exactly one invoice per file.

    A page whose OCR text is blank (e.g. a genuine blank divider/cover
    page) is skipped rather than failing the whole batch. Raises
    InvoiceParsingError only if EVERY page in the file failed or produced
    no text — a partial batch (some pages good, some not) still returns
    whatever succeeded.
    """
    logger.info(f"Starting multi-page invoice parse: {file_path}")
    pages = extract_pages_from_file(file_path, force_handwritten=force_handwritten)

    invoices: list[Invoice] = []
    skip_reasons: list[str] = []
    for page in pages:
        page_number = page.get("page_number", 1)
        total_pages = page.get("total_pages", len(pages))
        ocr_text = page.get("text", "")

        if not ocr_text.strip():
            msg = f"page {page_number}/{total_pages}: OCR produced no text"
            logger.warning(f"{file_path} — {msg}, skipping.")
            skip_reasons.append(msg)
            continue

        try:
            invoice = _build_invoice_from_ocr(
                file_path=file_path,
                image_path=page.get("image_path", file_path),
                ocr_text=ocr_text,
                ocr_confidence=page.get("avg_confidence", 0.0),
                ocr_engine=page.get("engine"),
            )
        except InvoiceParsingError as e:
            msg = f"page {page_number}/{total_pages}: {e}"
            logger.warning(f"{file_path} — extraction failed for {msg}")
            skip_reasons.append(msg)
            continue

        invoices.append(invoice)

    if not invoices:
        reasons = "; ".join(skip_reasons) if skip_reasons else "no pages found"
        raise InvoiceParsingError(f"No invoice could be extracted from any page of {file_path} ({reasons}).")

    return invoices


def _build_invoice_from_ocr(
    file_path: str,
    image_path: str,
    ocr_text: str,
    ocr_confidence: float,
    ocr_engine: str | None,
) -> Invoice:
    """
    The shared per-invoice pipeline: takes OCR output already produced for
    ONE invoice's worth of text (whether that's a whole single-page file
    via parse_invoice(), or one page of a multi-invoice file via
    parse_invoice_pages()) and runs extraction -> reconciliation ->
    validation -> scoring -> model-building on it.

    image_path is passed separately from file_path so the vision
    cross-check step (ai/vision_verifier.py) always looks at the actual
    single, static image for THIS invoice — for a multi-page PDF that's
    the specific page's rendered PNG, not the original multi-page file
    (which vision_verifier would otherwise always resolve to page 1 of,
    regardless of which page is actually being verified).
    """
    # 2. Detect a stable vendor-specific invoice layout, then run the LLM
    # extraction with that template injected into the prompt.
    template_name = detect_invoice_template(ocr_text)
    try:
        extracted = extract_invoice_data(ocr_text, template_name=template_name)
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

    # 4a-v. Apply deterministic vendor-template corrections after generic
    # normalization. The LLM remains the primary extractor; these rules only
    # enforce high-confidence layout/business rules for a recognized template.
    template = get_template(template_name)
    template_notes: list[str] = []
    if template and template.get("post_process"):
        cleaned, template_notes = template["post_process"](cleaned, ocr_text)

    # 4a. Heuristic backstop: drop line items whose "description" is
    # actually a bare price or product code (see ai/post_processing.
    # clean_line_items — confirmed recurring across several real receipts
    # with multi-line item layouts: code line, then name, then price).
    cleaned, line_item_notes = clean_line_items(cleaned)

    # 4a-2. Heuristic backstop, MUST run before reconcile_subtotal/tax/
    # total below: catch subtotal and total_amount being SWAPPED with each
    # other outright (see ai/post_processing.reconcile_swapped_subtotal_total
    # — confirmed on a real Ace Hardware receipt that had no vision_notes
    # at all and the highest confidence score in an entire test batch,
    # despite being wrong). Testing confirmed running the other reconcile
    # steps first would let reconcile_subtotal quietly "fix" subtotal to
    # equal the still-wrong total_amount, permanently hiding the swap.
    cleaned, swap_notes = reconcile_swapped_subtotal_total(cleaned, ocr_text)

    # 4b. Heuristic backstop: catch cases where total_amount actually matches
    # a CASH/CHANGE line in the OCR text instead of the real TOTAL line
    # (see ai/post_processing.reconcile_total_amount). Auto-corrects when
    # confident, and always surfaces a note so it's still reviewable.
    cleaned, total_notes = reconcile_total_amount(cleaned, ocr_text)

    # 4c. Heuristic backstop: fill tax_rate/tax_amount from a regex scan of
    # the raw OCR text when the LLM left them null/zero, and flag (without
    # overwriting) cases where the LLM's tax_amount disagrees with a
    # clearly-labeled VAT/tax line in the OCR text (see
    # ai/post_processing.reconcile_tax).
    cleaned, tax_notes = reconcile_tax(cleaned, ocr_text)

    # 4c-1. Heuristic backstop: fill zero_rated_sales/vat_exempt_sales from
    # a regex scan of the raw OCR text when the LLM left them null, same
    # fill-only pattern as reconcile_tax above (see
    # ai/post_processing.reconcile_zero_rated_exempt).
    cleaned, zero_exempt_notes = reconcile_zero_rated_exempt(cleaned, ocr_text)

    # 4c-2. Heuristic backstop: catch subtotal getting set to a
    # VAT-INCLUSIVE figure (the receipt's printed "SUBTOTAL" line) instead
    # of the true net-of-VAT amount — a confirmed recurring failure mode,
    # and one the vision cross-check below doesn't reliably catch either
    # (see ai/post_processing.reconcile_subtotal).
    cleaned, subtotal_notes = reconcile_subtotal(cleaned, ocr_text)

    # 4c-3. Heuristic backstop: catch vendor_name getting set to
    # POS-provider/permit-accreditation footer boilerplate instead of the
    # actual merchant name (see ai/post_processing.reconcile_vendor_name —
    # confirmed on real parking-ticket receipts where an unrelated
    # "PTU"/"ACC:"-adjacent company name got picked over the one explicitly
    # labeled "Name:" higher up).
    cleaned, vendor_notes = reconcile_vendor_name(cleaned, ocr_text)

    # 4c-5. Heuristic backstop: catch invoice_number getting set to a
    # Transaction#/terminal-ID instead of the real Official-Receipt/Sales-
    # Invoice number (see ai/post_processing.reconcile_invoice_number).
    cleaned, invnum_notes = reconcile_invoice_number(cleaned, ocr_text)

    # 4c-4. Heuristic backstop: catch invoice_date being a permit/
    # accreditation issuance date instead of the actual transaction date,
    # or a plain LLM digit-transcription error on an otherwise-correct OCR
    # read (see ai/post_processing.reconcile_invoice_date).
    cleaned, date_notes = reconcile_invoice_date(cleaned, ocr_text)

    cleaned, plate_notes = reconcile_plate_number(cleaned, ocr_text)

    reconciliation_notes = (
        template_notes + line_item_notes + swap_notes + total_notes + tax_notes + zero_exempt_notes + subtotal_notes
        + vendor_notes + invnum_notes + date_notes + plate_notes
    )

    # 4d. Vision cross-check: an independent second look at the actual
    # IMAGE (not the OCR text) for the fields where a misread is costliest.
    # Complements 4b/4c above — those catch specific known LLM/OCR
    # confusions from the text alone, this catches OCR misreads the
    # text-only pipeline has no way to see at all. Off by default and
    # fails open (see ai/vision_verifier.py docstring).
    #
    # Any correction the vision model is confident enough about is applied
    # directly to `cleaned` here — this is an auto-replace, not just a flag.
    # It's still safe: vision_issues (below) always gets a note for every
    # correction, which feeds into `issues` and forces needs_review=True
    # (see ai/confidence.py), so an auto-corrected value still requires a
    # human to review and lock it before it's treated as final.
    vision_corrections, vision_issues = verify_against_image(image_path, cleaned, template_name=template_name)
    if vision_corrections:
        cleaned.update(vision_corrections)

    # Vision is an independent signal, but for a recognized Watsons invoice
    # the printed Watsons labels are the deterministic source of truth for
    # the fields covered by the template. Re-apply the template after vision
    # so a vision model cannot reintroduce the exact CASH/SUBTOTAL mix-ups
    # that the Watsons rules already corrected.
    if template and template.get("post_process"):
        cleaned, post_vision_template_notes = template["post_process"](cleaned, ocr_text)
        if post_vision_template_notes:
            reconciliation_notes.extend(post_vision_template_notes)

    # Re-apply the explicit Plate label after vision verification so a generic
    # vision reading cannot replace a plate number with another identifier.
    cleaned, post_vision_plate_notes = reconcile_plate_number(cleaned, ocr_text)
    if post_vision_plate_notes:
        reconciliation_notes.extend(post_vision_plate_notes)

    # 5. Validate + score confidence — MUST run before sanitizing, so a
    #    genuinely-missing field still counts against confidence/needs_review.
    issues = validate_extraction(cleaned, template_name=template_name) + reconciliation_notes + vision_issues
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
    invoice.ocr_engine_used = ocr_engine
    invoice.confidence_score = prediction.overall_confidence
    invoice.raw_text = ocr_text
    invoice.vision_notes = "\n".join(vision_issues) if vision_issues else None
    invoice.status = INVOICE_STATUS_REVIEW if prediction.needs_review else INVOICE_STATUS_PROCESSED

    logger.info(
        f"Parsed invoice {invoice.invoice_number!r} "
        f"(confidence={invoice.confidence_score}, status={invoice.status})"
    )
    return invoice
