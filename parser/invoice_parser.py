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
    clean_line_items, reconcile_vendor_name, reconcile_missing_vendor_name, reconcile_invoice_date, reconcile_swapped_subtotal_total,
    reconcile_tax_ids,
    reconcile_invoice_number, reconcile_zero_rated_exempt, reconcile_plate_number, reconcile_discount,
    reconcile_customer_name, reconcile_financial_layout, apply_vision_corrections_with_financial_gate,
    apply_identity_corrections_with_gate, reconcile_single_unreliable_financial_field, collect_financial_evidence,
    reconcile_statement_summary, reconcile_billing_statement_balances, reconcile_billing_current_period_financials, add_remaining_balance_line_item, extract_statement_summary_evidence, reconcile_line_item_geometry, reconcile_customer_address_layout,
    reconcile_vendor_address_block, reconcile_customer_tax_id_context, reconcile_blank_customer_address,
    reconcile_retail_line_items, reconcile_retail_tax_summary, reconcile_parking_tax_summary,
    reconcile_loyalty_balance_context, reconcile_issuer_printer_ownership,
)
from ai.vision_verifier import (verify_against_image, recover_vendor_header_fields, verify_handwritten_invoice_date,
    verify_statement_summary_crop, verify_customer_address_crop, verify_plate_number_crop, verify_vendor_address_crop)
from parser.table_parser import parse_line_items_from_text
from models.invoice import Invoice
from config.constants import INVOICE_STATUS_PROCESSED, INVOICE_STATUS_REVIEW, INVOICE_STATUS_FAILED
from utils.invoice_template_detector import detect_invoice_template
from ai.invoice_templates import get_template
from ai.diagnostics import save_diagnostic_trace

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
        ocr_lines=ocr_result.get("lines", []),
        ocr_result=ocr_result,
        page_number=1,
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
                ocr_lines=page.get("lines", []),
                ocr_result=page,
                page_number=page_number,
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
    ocr_lines: list[dict] | None = None,
    ocr_result: dict | None = None,
    page_number: int | None = None,
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
    text_llm_trace: dict = {}
    vision_trace: dict = {}
    stages: dict = {"ocr_text": ocr_text}
    try:
        extracted = extract_invoice_data(ocr_text, template_name=template_name, trace=text_llm_trace)
        stages["text_llm_extracted"] = dict(extracted)
    except ExtractionError as e:
        logger.error(f"LLM extraction failed for {file_path}: {e}")
        save_diagnostic_trace(
            source_file=file_path, page_number=page_number, image_path=image_path,
            processed_image_path=(ocr_result or {}).get("processed_image_path"),
            ocr_result=ocr_result or {"text": ocr_text, "lines": ocr_lines or [], "avg_confidence": ocr_confidence, "engine": ocr_engine},
            text_llm_trace=text_llm_trace, vision_trace=vision_trace, stages=stages,
            validation_issues=[], final_data=None, error=str(e),
        )
        raise InvoiceParsingError(str(e)) from e

    # 3. Fallback line items if the LLM returned none
    if not extracted.get("line_items"):
        heuristic_items = parse_line_items_from_text(ocr_text)
        if heuristic_items:
            extracted["line_items"] = heuristic_items

    # 4. Post-process / normalize values
    cleaned = post_process(extracted)
    stages["post_process"] = dict(cleaned)

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

    # 4c-2. Heuristic backstop: catch subtotal getting set to a
    # VAT-INCLUSIVE figure (the receipt's printed "SUBTOTAL" line) instead
    # of the true net-of-VAT amount — a confirmed recurring failure mode,
    # and one the vision cross-check below doesn't reliably catch either
    # (see ai/post_processing.reconcile_subtotal). MUST run BEFORE
    # reconcile_zero_rated_exempt below — see that step's comment for why.
    cleaned, subtotal_notes = reconcile_subtotal(cleaned, ocr_text)

    # 4c-1. Heuristic backstop: fill zero_rated_sales/vat_exempt_sales from
    # a regex scan of the raw OCR text when the LLM left them null, same
    # fill-only pattern as reconcile_tax above (see
    # ai/post_processing.reconcile_zero_rated_exempt). MUST run AFTER
    # reconcile_subtotal just above — its cross-check guard compares
    # zero_rated_sales/vat_exempt_sales against `subtotal` to catch
    # column-contamination, and needs subtotal's FINAL, corrected value to
    # do that reliably. Confirmed on a real North Star Travel invoice: this
    # step used to run first, so it compared against the still-raw,
    # not-yet-reconciled subtotal and missed a contamination case that the
    # exact same guard, run in this corrected order, catches immediately.
    cleaned, zero_exempt_notes = reconcile_zero_rated_exempt(cleaned, ocr_text)

    # 4c-1b. Heuristic backstop: fill `discount` from a regex scan of the
    # OCR text when missing, and flag (without overwriting) a sign/amount
    # disagreement with a clearly-labeled discount line — same fill-only-
    # or-flag pattern as reconcile_tax/reconcile_zero_rated_exempt above
    # (see ai/post_processing.reconcile_discount). MUST run after
    # post_process()'s normalize_discount() has already forced `discount`
    # to a positive magnitude, so the comparison here is apples-to-apples.
    cleaned, discount_notes = reconcile_discount(cleaned, ocr_text)

    # 4c-3a. Heuristic backstop: catch vendor_name being completely empty
    # (LLM dropped the field outright) — a different failure from 4c-3
    # below, which only corrects an already-present-but-wrong value. Runs
    # first so 4c-3's correction logic then has a value to work with.
    cleaned, missing_vendor_notes = reconcile_missing_vendor_name(cleaned, ocr_text)

    # 4c-3. Heuristic backstop: catch vendor_name getting set to
    # POS-provider/permit-accreditation footer boilerplate instead of the
    # actual merchant name (see ai/post_processing.reconcile_vendor_name —
    # confirmed on real parking-ticket receipts where an unrelated
    # "PTU"/"ACC:"-adjacent company name got picked over the one explicitly
    # labeled "Name:" higher up).
    cleaned, vendor_notes = reconcile_vendor_name(cleaned, ocr_text)

    # 4c-3b. Heuristic backstop: catch customer_name getting set to a
    # printer/booklet footer's own proprietor, an unrelated field's value
    # (e.g. a "Nature of Services:" entry), or left blank when an explicit
    # "Billed To"/"Sold To"/"Registered Name" label names the real customer
    # elsewhere in the text (see ai/post_processing.reconcile_customer_name
    # — confirmed on real North Star International Travel, SGV, and
    # Emerald Mansion Condominium Association invoices).
    cleaned, customer_notes = reconcile_customer_name(cleaned, ocr_text)

    # 1.54: rebuild customer address from the explicit ADDRESS block using OCR geometry.
    # This prevents neighboring PO Ref No./Terms fields from entering the address.
    cleaned, customer_address_notes = reconcile_customer_address_layout(cleaned, ocr_lines or [])

    # 4c-3c. Heuristic backstop: catch vendor_tax_id/customer_tax_id being
    # swapped with each other or filled with a printer-booklet's own
    # registration instead of either party's real TIN (see
    # ai/post_processing.reconcile_tax_ids — confirmed on a real North
    # Star International Travel invoice). Runs after customer_name/
    # vendor_name above so its "which party is which" comparisons are
    # working from already-corrected names.
    cleaned, tax_id_notes = reconcile_tax_ids(cleaned, ocr_text)

    # 1.52: if the whole-page pass missed/suspiciously populated seller
    # address/TIN, perform a generic high-resolution header-only second pass.
    # Use the upright/preprocessed OCR image when available so a sideways PDF
    # does not get sent back into the header reader in its original orientation.
    header_trace: dict = {}
    header_image_path = (
        (ocr_result or {}).get("processed_image_path")
        or (ocr_result or {}).get("oriented_image_path")
        or image_path
    )
    header_fields, header_notes, header_locked_fields = recover_vendor_header_fields(
        header_image_path, cleaned, trace=header_trace
    )
    if header_fields:
        cleaned.update(header_fields)
        cleaned = post_process(cleaned)
    vision_trace["vendor_header_recovery"] = header_trace

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
        template_notes + line_item_notes + swap_notes + total_notes + tax_notes + zero_exempt_notes
        + discount_notes + subtotal_notes + missing_vendor_notes + vendor_notes + customer_notes + customer_address_notes + tax_id_notes + header_notes + invnum_notes + date_notes
        + plate_notes
    )

    # 1.54: geometry-aware statement-summary and item-table passes.
    cleaned, statement_summary_notes = reconcile_statement_summary(cleaned, ocr_lines or [])
    cleaned, billing_balance_notes = reconcile_billing_statement_balances(cleaned, ocr_lines or [])
    cleaned, billing_current_notes = reconcile_billing_current_period_financials(cleaned, ocr_lines or [])
    billing_balance_notes.extend(billing_current_notes)
    statement_evidence = extract_statement_summary_evidence(ocr_lines or [])

    # 1.55: a dedicated crop provides an independent, row-focused second read
    # of Statement Summary. Geometry owns the fields when available; crop
    # evidence only fills a missing summary or confirms it.
    summary_crop_trace: dict = {}
    summary_crop, summary_crop_notes = verify_statement_summary_crop(image_path, ocr_lines or [], trace=summary_crop_trace)
    vision_trace["statement_summary_verification"] = summary_crop_trace
    if summary_crop_notes:
        reconciliation_notes.extend(summary_crop_notes)
    if summary_crop and not statement_evidence:
        mp=summary_crop.get("monthly_plan"); ao=summary_crop.get("add_ons",0.0); ct=summary_crop.get("current_charges_total")
        if mp is not None and ct is not None:
            disc=summary_crop.get("discounts")
            if disc is None: disc=max(0.0, round(float(mp)+float(ao or 0)-float(ct),2))
            cleaned["subtotal"]=round(float(mp)+float(ao or 0),2)
            cleaned["discount"]=round(abs(float(disc)),2)
            cleaned["current_charges_total"]=round(float(ct),2)
            cleaned["total_amount"]=round(float(ct),2)
            cleaned, extra_bill_notes = reconcile_billing_statement_balances(cleaned, ocr_lines or [])
            cleaned, extra_current_notes = reconcile_billing_current_period_financials(cleaned, ocr_lines or [])
            extra_bill_notes.extend(extra_current_notes)
            billing_balance_notes.extend(extra_bill_notes)
            statement_evidence={
                "monthly_plan":round(float(mp),2), "add_ons":round(float(ao or 0),2),
                "discounts":round(abs(float(disc)),2),
                "subtotal_before_discount":cleaned["subtotal"],
                "current_charges_total":cleaned["current_charges_total"],
                "strong_fields":["subtotal","discount","current_charges_total"],
                "source":"vision_statement_crop",
            }
    cleaned, line_geometry_notes = reconcile_line_item_geometry(cleaned, ocr_lines or [])
    # Re-run item cleanup so summary deductions such as "Discounts" can never
    # remain in Items Purchased, while legitimate charges such as Add-ons stay.
    cleaned, line_item_notes_154 = clean_line_items(cleaned)
    reconciliation_notes.extend(statement_summary_notes + billing_balance_notes + line_geometry_notes + line_item_notes_154)

    # Preserve OCR geometry for financial labels/values. This prevents a
    # flattened two-column BIR table from assigning a neighboring column's
    # figure to Zero-Rated/Discount/etc.
    cleaned, layout_financial_notes = reconcile_financial_layout(cleaned, ocr_lines or [])
    reconciliation_notes.extend(layout_financial_notes)
    vision_trace["financial_evidence_provenance"] = collect_financial_evidence(ocr_lines or [])
    stages["pre_vision_reconciled"] = dict(cleaned)

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
    vision_corrections, vision_issues = verify_against_image(image_path, cleaned, template_name=template_name, trace=vision_trace)

    # 1.50: vision financial values do not get a blind ``dict.update``. Treat
    # the image-read financial set as evidence, gate it through the accounting
    # identity, and lock only the coherent fields. This prevents a vision
    # misread such as North Star ``Discount = 296.16`` (actually the Less: VAT
    # row) and prevents weaker post-processing from destroying a correct vision
    # subtotal/tax/total set on handwritten invoices.
    # 1.52 identity gate runs first. Explicit labelled OCR/header evidence
    # (especially seller/customer TINs) removes conflicting vision corrections
    # before the financial gate sees the rest.
    # 1.55: full-page vision is weaker than a geometrically owned Statement
    # Summary / Previous Bill Activity block. Remove conflicting financial
    # corrections before the general gates so row-level evidence cannot be
    # destroyed by a broad image guess.
    statement_locked_fields: set[str] = set()
    if statement_evidence:
        statement_locked_fields.update({"subtotal", "discount"})
        # total_amount is owned either by the summary Total (no carried balance)
        # or by the explicit Amount to Pay parsed from Previous Bill Activity.
        statement_locked_fields.add("total_amount")
        for f in list(statement_locked_fields):
            if f in vision_corrections:
                vision_trace.setdefault("statement_summary_rejected_fullpage_vision", {})[f] = vision_corrections.pop(f)
                reconciliation_notes.append(
                    f"1.55 evidence priority: ignored full-page vision correction for {f}; explicit Statement Summary/billing rows own this field."
                )

    cleaned, filtered_vision_corrections, identity_locked_fields, identity_gate_notes, identity_accepted, identity_rejected = (
        apply_identity_corrections_with_gate(
            cleaned, vision_corrections, ocr_text, extra_locked_fields=header_locked_fields
        )
    )
    if identity_gate_notes:
        reconciliation_notes.extend(identity_gate_notes)
    vision_trace["identity_gate"] = {
        "accepted": identity_accepted,
        "rejected": identity_rejected,
        "locked_fields": sorted(identity_locked_fields),
    }

    cleaned, vision_locked_fields, vision_gate_notes, vision_accepted, vision_rejected = (
        apply_vision_corrections_with_financial_gate(
            cleaned, filtered_vision_corrections, vision_trace.get("mismatches", []), ocr_lines=ocr_lines or []
        )
    )
    if vision_gate_notes:
        reconciliation_notes.extend(vision_gate_notes)
    vision_trace["accepted_after_financial_gate"] = vision_accepted
    vision_trace["rejected_after_financial_gate"] = vision_rejected
    vision_trace["locked_financial_fields"] = sorted(vision_locked_fields)

    # Normalize raw date/money strings returned by vision, then re-apply OCR
    # layout evidence WITHOUT allowing it to overwrite stronger locked fields.
    cleaned = post_process(cleaned)
    cleaned, post_vision_financial_notes = reconcile_financial_layout(
        cleaned, ocr_lines or [], locked_fields=(set(vision_locked_fields) | statement_locked_fields)
    )
    if post_vision_financial_notes:
        reconciliation_notes.extend(post_vision_financial_notes)

    # 1.52: vision can return a raw ambiguous date such as 08/05/26. Re-run
    # labelled/context-aware date reconciliation AFTER post_process so an
    # explicit printed mm/dd/yy hint wins over the generic parser.
    cleaned, post_vision_date_notes = reconcile_invoice_date(cleaned, ocr_text)
    if post_vision_date_notes:
        reconciliation_notes.extend(post_vision_date_notes)

    # 1.54: when the OCR's explicit Date row looks handwritten/garbled,
    # run a tiny date-only crop through the vision model. The crop is easier to
    # read than the full page and is not told the existing month value.
    date_crop_trace: dict = {}
    date_crop_value, date_crop_notes = verify_handwritten_invoice_date(
        image_path, ocr_lines or [], trace=date_crop_trace
    )
    vision_trace["handwritten_date_verification"] = date_crop_trace
    if date_crop_value and date_crop_value != cleaned.get("invoice_date"):
        old_date = cleaned.get("invoice_date")
        cleaned["invoice_date"] = date_crop_value
        reconciliation_notes.append(
            f"Handwritten date crop corrected invoice_date from {old_date!r} to {date_crop_value!r}."
        )
    if date_crop_notes:
        reconciliation_notes.extend(date_crop_notes)
    # Preserve OCR date-format provenance even after the dedicated crop. If
    # the crop returns an ambiguous numeric value (e.g. 08/05/26), the explicit
    # Invoice Date/Due Date context gets the final say.
    cleaned, post_crop_date_notes = reconcile_invoice_date(cleaned, ocr_text)
    if post_crop_date_notes:
        reconciliation_notes.extend(post_crop_date_notes)

    # 1.52: if exactly one financial bucket is uniquely weak, derive its
    # exact amount from the other five stronger fields. The vision model may
    # still be useful for identifying WHICH bucket is non-zero even when its
    # handwritten amount is a few digits off.
    cleaned, single_field_notes, vision_locked_fields = reconcile_single_unreliable_financial_field(
        cleaned, ocr_lines or [], vision_accepted=vision_accepted, locked_fields=vision_locked_fields
    )
    if single_field_notes:
        reconciliation_notes.extend(single_field_notes)

    # 1.55 final ownership pass: row-level Statement Summary and carried-balance
    # evidence are deterministic and re-applied after every whole-page vision
    # and arithmetic heuristic.
    cleaned, final_summary_notes = reconcile_statement_summary(cleaned, ocr_lines or [])
    cleaned, final_billing_notes = reconcile_billing_statement_balances(cleaned, ocr_lines or [])
    cleaned, final_current_notes = reconcile_billing_current_period_financials(cleaned, ocr_lines or [])
    final_billing_notes.extend(final_current_notes)
    reconciliation_notes.extend(final_summary_notes + final_billing_notes)
    stages["post_vision_reconciled"] = dict(cleaned)

    # Vision is an independent signal, but for a recognized Watsons invoice
    # the printed Watsons labels are the deterministic source of truth for
    # the fields covered by the template. Re-apply the template after vision
    # so a vision model cannot reintroduce the exact CASH/SUBTOTAL mix-ups
    # that the Watsons rules already corrected.
    if template and template.get("post_process"):
        cleaned, post_vision_template_notes = template["post_process"](cleaned, ocr_text)
        if post_vision_template_notes:
            reconciliation_notes.extend(post_vision_template_notes)

    # 1.57: re-apply vendor-vs-printer ownership after full-page vision.
    # A printer/footer company must never reclaim vendor_name after the header
    # reconciliation has identified the actual invoice issuer.
    cleaned, post_vision_vendor_notes = reconcile_vendor_name(cleaned, ocr_text)
    if post_vision_vendor_notes:
        reconciliation_notes.extend(post_vision_vendor_notes)

    # Re-apply the explicit Plate label after vision verification so a generic
    # vision reading cannot replace a plate number with another identifier.
    cleaned, post_vision_plate_notes = reconcile_plate_number(cleaned, ocr_text)
    if post_vision_plate_notes:
        reconciliation_notes.extend(post_vision_plate_notes)

    # Re-apply the customer-label lookup after vision verification too, for
    # the same reason: an independent vision reading should not be able to
    # reintroduce a printer-footer/unrelated-field value over an explicit
    # "Billed To"/"Sold To" label match already established above.
    cleaned, post_vision_customer_notes = reconcile_customer_name(cleaned, ocr_text)
    if post_vision_customer_notes:
        reconciliation_notes.extend(post_vision_customer_notes)

    cleaned, post_vision_address_notes = reconcile_customer_address_layout(cleaned, ocr_lines or [])
    if post_vision_address_notes:
        reconciliation_notes.extend(post_vision_address_notes)
    address_crop_trace: dict = {}
    address_crop_value, address_crop_notes = verify_customer_address_crop(image_path, ocr_lines or [], trace=address_crop_trace)
    vision_trace["customer_address_verification"] = address_crop_trace
    if address_crop_value:
        old_addr=str(cleaned.get("customer_address") or "").strip()
        alpha=lambda x: len(__import__('re').findall(r'[A-Za-z]', x or ''))
        if alpha(address_crop_value) > alpha(old_addr) + 8:
            cleaned["customer_address"] = address_crop_value
            reconciliation_notes.append("1.55 customer-address crop replaced an incomplete whole-page address with a fuller explicit ADDRESS-block read.")
    if address_crop_notes:
        reconciliation_notes.extend(address_crop_notes)
    cleaned, post_vision_item_geom_notes = reconcile_line_item_geometry(cleaned, ocr_lines or [])
    cleaned, post_vision_item_clean_notes = clean_line_items(cleaned)
    reconciliation_notes.extend(post_vision_item_geom_notes + post_vision_item_clean_notes)

    # 1.59: final retail/layout ownership pass. These rules use explicit OCR
    # labels and arithmetic and therefore run after whole-page/template vision
    # so weaker guesses cannot reintroduce the known field swaps.
    cleaned, vendor_address_159 = reconcile_vendor_address_block(cleaned, ocr_text)
    cleaned, buyer_tin_159 = reconcile_customer_tax_id_context(cleaned, ocr_text, ocr_lines or [])
    cleaned, buyer_address_159 = reconcile_blank_customer_address(cleaned, ocr_text)
    cleaned, retail_items_159 = reconcile_retail_line_items(cleaned, ocr_text)
    cleaned, retail_tax_159 = reconcile_retail_tax_summary(cleaned, ocr_text)
    cleaned, parking_tax_159 = reconcile_parking_tax_summary(cleaned, ocr_text)
    reconciliation_notes.extend(
        vendor_address_159 + buyer_tin_159 + buyer_address_159 +
        retail_items_159 + retail_tax_159 + parking_tax_159
    )

    # 1.60: final focused visual ownership for two character-sensitive fields.
    # These crops are deliberately narrow so the vision model cannot borrow
    # unrelated identifiers/addresses from elsewhere on the page.
    vendor_addr_trace: dict = {}
    vendor_addr_value, vendor_addr_notes = verify_vendor_address_crop(
        image_path, cleaned.get("vendor_address"), ocr_lines or [], trace=vendor_addr_trace
    )
    vision_trace["vendor_address_verification"] = vendor_addr_trace
    if vendor_addr_value:
        cleaned["vendor_address"] = vendor_addr_value
    reconciliation_notes.extend(vendor_addr_notes)

    plate_trace: dict = {}
    plate_value, plate_notes = verify_plate_number_crop(image_path, ocr_lines or [], trace=plate_trace)
    vision_trace["plate_number_verification"] = plate_trace
    if plate_value:
        old_plate=str(cleaned.get("plate_number") or "").strip().upper()
        if plate_value.upper() != old_plate:
            cleaned["plate_number"] = plate_value.upper()
            reconciliation_notes.append(f"1.60 plate-number crop corrected {old_plate or 'missing'} to {plate_value.upper()} from the explicit PLATE NO. region.")
    reconciliation_notes.extend(plate_notes)

    # 1.61: final ownership hardening. Retail loyalty counters cannot become
    # billing balances, and a BIR printer footer cannot own the invoice issuer.
    cleaned, loyalty_balance_notes = reconcile_loyalty_balance_context(cleaned, ocr_text)
    cleaned, issuer_owner_notes = reconcile_issuer_printer_ownership(cleaned, ocr_text)
    reconciliation_notes.extend(loyalty_balance_notes + issuer_owner_notes)

    # 1.58: add a non-zero carried balance only after ownership cleanup.
    cleaned, balance_item_notes = add_remaining_balance_line_item(cleaned)
    reconciliation_notes.extend(balance_item_notes)

    # 5. Validate + score FINAL information confidence. Successful
    # reconciliation notes are provenance, not unresolved correctness defects.
    unresolved_validation_issues = validate_extraction(cleaned, template_name=template_name, ocr_text=ocr_text)
    prediction = score_extraction(cleaned, ocr_confidence, unresolved_validation_issues, ocr_engine=ocr_engine)
    issues = unresolved_validation_issues + reconciliation_notes + vision_issues

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

    stages["final_cleaned"] = dict(cleaned)
    trace_path = save_diagnostic_trace(
        source_file=file_path, page_number=page_number, image_path=image_path,
        processed_image_path=(ocr_result or {}).get("processed_image_path"),
        ocr_result=ocr_result or {"text": ocr_text, "lines": ocr_lines or [], "avg_confidence": ocr_confidence, "engine": ocr_engine},
        text_llm_trace=text_llm_trace, vision_trace=vision_trace, stages=stages,
        validation_issues=issues, final_data=invoice, error=None,
    )
    if trace_path:
        logger.info(f"Diagnostic trace saved: {trace_path}")

    logger.info(
        f"Parsed invoice {invoice.invoice_number!r} "
        f"(confidence={invoice.confidence_score}, status={invoice.status})"
    )
    return invoice
