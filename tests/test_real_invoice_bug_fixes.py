"""
Regression tests for the remaining bugs found in a real batch of 7 invoices
(Emerald Mansion, Globe Business, SGV, TRI-Q, North Star x2, Gliptic Art) —
everything NOT already covered by tests/test_field_aliases_and_discount.py
(discount sign + field-alias library).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.post_processing import (
    clean_line_items, reconcile_zero_rated_exempt, reconcile_invoice_number, reconcile_subtotal,
    reconcile_customer_name, reconcile_vendor_name, reconcile_missing_vendor_name, reconcile_tax_ids,
)
from ai.prompt_builder import SYSTEM_PROMPT
from ai.validator import validate_extraction
from ai.confidence import score_extraction
from utils.helpers import normalize_tin, is_valid_tin
from utils.date_utils import to_iso
from config.constants import REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# Image 2 (Globe Business) & Image 5/7 (North Star): a VAT/summary row
# getting counted as its own line item, doubling the line-items sum.
# ---------------------------------------------------------------------------
def test_globe_monthly_recurring_fee_not_double_counted():
    data = {
        "line_items": [
            {"description": "Biz BB Plan 2499 50Mbps Unli", "quantity": 1, "unit_price": 2499.0, "amount": 2499.0},
            {"description": "Monthly Recurring Fee (MRF)", "quantity": 1, "unit_price": 2198.0, "amount": 2198.0},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"] == "Biz BB Plan 2499 50Mbps Unli"
    assert any("Monthly Recurring Fee" in n for n in notes)


def test_north_star_vat_line_not_counted_as_item():
    data = {
        "line_items": [
            {"description": "SERVICE FEE (TICKET)", "quantity": 1, "unit_price": 2468.0, "amount": 2468.0},
            {"description": "VAT (12% of SERVICE FEE (TICKET))", "quantity": 1, "unit_price": 296.16, "amount": 296.16},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"] == "SERVICE FEE (TICKET)"


# ---------------------------------------------------------------------------
# Image 4 (TRI-Q): a blank "Zero-Rated Sales" column got filled with the
# Total Sales (VAT Inclusive) figure instead of staying null.
# ---------------------------------------------------------------------------
def test_tri_q_blank_zero_rated_column_not_filled_with_total():
    """NOTE: previously this asserted zero_rated_sales got silently CLEARED
    to None. That was changed after it was confirmed to also misfire on a
    legitimate wholly-zero-rated (e.g. export) invoice, where
    zero_rated_sales == total_amount is genuinely correct — silently
    deleting a correct value is its own regression. The guard now leaves
    the value in place and only raises a note asking a human to confirm."""
    data = {"zero_rated_sales": 96279.67, "vat_exempt_sales": None, "total_amount": 96279.67}
    ocr = "Total Sales (VAT Inclusive) 96,279.67\nVATable Sales 85,963.99"
    result, notes = reconcile_zero_rated_exempt(data, ocr)
    assert result["zero_rated_sales"] == 96279.67
    assert any("exactly matches total_amount" in n for n in notes)


def test_wholly_zero_rated_invoice_is_not_cleared():
    """New case: an export/wholly-zero-rated invoice can legitimately have
    zero_rated_sales == total_amount. The guard must flag this for review
    but must NOT delete a correct value."""
    data = {"zero_rated_sales": 50000.00, "vat_exempt_sales": None, "total_amount": 50000.00}
    result, notes = reconcile_zero_rated_exempt(data, "Total Amount Due 50,000.00")
    assert result["zero_rated_sales"] == 50000.00
    assert any("exactly matches total_amount" in n for n in notes)


# ---------------------------------------------------------------------------
# TIN normalization/validation.
# ---------------------------------------------------------------------------
def test_tin_normalization_samples():
    assert normalize_tin("000-423-215-00000") == "000-423-215-00000"  # already valid shape
    assert normalize_tin("000423215000") == "000-423-215-000"          # no dashes -> regrouped
    assert normalize_tin("214-706-591-029") == "214-706-591-029"       # unchanged
    assert normalize_tin(None) is None
    assert normalize_tin("") == ""

    assert is_valid_tin("007-848-122-000") is True
    assert is_valid_tin("00042321500000") is False  # 14 digits, not a PH TIN shape


def test_tin_gets_normalized_through_post_process():
    from ai.post_processing import post_process
    result = post_process({"vendor_tax_id": "000423215000", "customer_tax_id": "007-848-122-000"})
    assert result["vendor_tax_id"] == "000-423-215-000"
    assert result["customer_tax_id"] == "007-848-122-000"


# ---------------------------------------------------------------------------
# Image 4 (TRI-Q) & Image 6/7 (Gliptic Art / North Star): date formats not
# previously recognized.
# ---------------------------------------------------------------------------
def test_new_date_formats_recognized():
    assert to_iso("13-Aug-26") == "2026-08-13"     # TRI-Q / North Star style
    assert to_iso("22-July-2026") == "2026-07-22"  # North Star style
    assert to_iso("MAY 28, 2026") == "2026-05-28"  # Gliptic Art style


# ---------------------------------------------------------------------------
# Image 6 (Gliptic Art): a fully handwritten invoice with garbled OCR text
# and a ~10x-wrong total that still had NO validation issues (internally
# self-consistent, just wrong) — previously scored "processed" at 82%.
# ---------------------------------------------------------------------------
def test_handwriting_forces_needs_review_even_with_no_validation_issues():
    extracted = {f: "x" for f in REQUIRED_FIELDS}

    printed_only = score_extraction(extracted, ocr_avg_confidence=0.82, validation_issues=[], ocr_engine="paddleocr")
    assert printed_only.needs_review is False

    handwritten = score_extraction(extracted, ocr_avg_confidence=0.82, validation_issues=[], ocr_engine="paddleocr+trocr")
    assert handwritten.needs_review is True
    assert handwritten.overall_confidence < printed_only.overall_confidence


# ---------------------------------------------------------------------------
# Image 3 (Emerald Mansion): the invoice pad's "CHARGE SALES" checkbox
# label got extracted as the description of the invoice's only line item.
# ---------------------------------------------------------------------------
def test_emerald_mansion_checkbox_label_not_kept_as_line_item():
    data = {
        "line_items": [
            {"description": "CHARGE SALES", "quantity": 1, "unit_price": 0.0, "amount": 2247.40},
        ]
    }
    result, notes = clean_line_items(data)
    assert result["line_items"] == []
    assert any("boilerplate" in n or "checkbox" in n.lower() or "CHARGE SALES" in n for n in notes)


# ---------------------------------------------------------------------------
# Image 8 (Gliptic Art): a printed "*** NOTHING FOLLOWS ***" marker below
# the genuine hand-written line items got extracted as an extra row.
# ---------------------------------------------------------------------------
def test_gliptic_art_nothing_follows_not_kept_as_line_item():
    data = {
        "line_items": [
            {"description": "ETCHED GLASS PLAQUE - DOME SHAPE", "quantity": 9, "unit_price": 1500.0, "amount": 13500.0},
            {"description": "*** NOTHING FOLLOWS ***", "quantity": None, "unit_price": None, "amount": None},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"].startswith("ETCHED")


# ---------------------------------------------------------------------------
# Image 3 (Adel Printing Services booklet footer): a printer's own
# accreditation number, not the invoice's number, sitting next to a "No.:"
# style label must not be mistaken for a valid invoice_number by having it
# escape the exclusion window reconcile_invoice_number relies on.
# ---------------------------------------------------------------------------
def test_printer_accreditation_number_excluded_from_invoice_number_window():
    data = {"invoice_number": "032MP20210000000039"}
    ocr = (
        "Sales Invoice No. 7512\n"
        "BIR Authority to Print No. OCN: 043AU20250000013398\n"
        "ADEL PRINTING SERVICES, Stall B1, Cartimar Bldg\n"
        "Printer's Accreditation No.: 032MP20210000000039\n"
    )
    result, notes = reconcile_invoice_number(data, ocr)
    assert result["invoice_number"] == "7512"
    assert notes


# ---------------------------------------------------------------------------
# Image 2 (TRI-Q): strengthen the zero-rated/exempt contamination guard so
# it also catches a match against `subtotal`, not just `total_amount`.
# ---------------------------------------------------------------------------
def test_zero_rated_matching_subtotal_also_cleared():
    """NOTE: name kept for continuity, but see note on
    test_tri_q_blank_zero_rated_column_not_filled_with_total above — the
    value is now left in place and only flagged, never deleted."""
    data = {"zero_rated_sales": 10500.0, "vat_exempt_sales": None, "subtotal": 10500.0, "total_amount": 11760.0}
    result, notes = reconcile_zero_rated_exempt(data, ocr_text="Subtotal 10,500.00")
    assert result["zero_rated_sales"] == 10500.0
    assert any("subtotal" in n for n in notes)


# ---------------------------------------------------------------------------
# Image 4/5 (SGV): two separate extraction runs each invented a different
# total_amount (13,520.00 / 13,020.00) that was internally consistent with
# that run's own (also-wrong) subtotal/tax — so no arithmetic cross-check
# ever fired — but the real printed total (11,760.00) never matched either,
# and neither invented figure appears anywhere in the OCR text at all.
# ---------------------------------------------------------------------------
def test_sgv_hallucinated_total_not_in_ocr_text_is_flagged():
    ocr = (
        "Subtotal: 10,500.00\nTax@12%VAT: 1,260.00\nTotal Amount: 11,760.00\n"
        "Please include this number with payment. PH91MKP0135619\n"
    )
    data = {
        **{f: "x" for f in REQUIRED_FIELDS},
        "subtotal": 12260.0,
        "tax_amount": 1260.0,
        "discount": None,
        "total_amount": 13520.0,
        "line_items": [],
    }
    issues = validate_extraction(data, ocr_text=ocr)
    assert any("does not appear verbatim" in i for i in issues)

    # The genuine printed total (11,760.00) DOES appear verbatim, and must
    # not be flagged just because it's a "large" number.
    data["total_amount"] = 11760.0
    issues = validate_extraction(data, ocr_text=ocr)
    assert not any("does not appear verbatim" in i for i in issues)


# ---------------------------------------------------------------------------
# Round 2 (re-run on the same batch): new failure modes surfaced once the
# round-1 fixes above were live.
# ---------------------------------------------------------------------------

# Image 2 (TRI-Q, re-run): the printed "VATABLE SALES" column header itself
# got extracted as a second, phantom line item sitting alongside the one
# genuine "UTILITY SERVICES" row, with the Total Sales figure as its amount.
def test_triq_vatable_sales_column_header_not_kept_as_line_item():
    data = {
        "line_items": [
            {"description": "UTILITY SERVICES", "quantity": 1, "unit_price": 85963.99, "amount": 85963.99},
            {"description": "VATABLE SALES", "quantity": 1, "unit_price": 96279.67, "amount": 96279.67},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"] == "UTILITY SERVICES"


# Image 3 (Emerald Mansion, re-run): subtotal still got contaminated with
# the Total Sales figure via the next-line fallback in _amounts_near_label,
# because that fallback wasn't checking `exclude` — only the label's own
# line was. reconcile_subtotal now passes the same total/amount-due exclude
# list used for zero-rated/exempt, and the fallback itself now honors it.
def test_emerald_mansion_subtotal_not_contaminated_by_total_sales_fallback():
    ocr = (
        "VATable Sales\n"
        "250\n"
        "Total Sales (VAT Inclusive) 2,247.40\n"
        "VAT-Exempt Sales 1,967.40\n"
    )
    data = {"subtotal": None, "total_amount": 2247.40, "tax_amount": 30.0, "discount": None}
    result, notes = reconcile_subtotal(data, ocr)
    # Before the exclude-on-fallback fix, the "VATable Sales" label's own
    # (amount-less) line fell through to the very next line, which was the
    # Total Sales row — filling subtotal with 2,247.40. That must no longer
    # happen; either it's left alone (None) or correctly filled with the
    # genuine 250, but never silently set to the Total Sales figure.
    assert result.get("subtotal") != 2247.40


def test_amounts_near_label_fallback_respects_exclude():
    from ai.post_processing import _amounts_near_label
    ocr = "VATable Sales\nTotal Sales (VAT Inclusive) 2,247.40\n"
    amounts = _amounts_near_label(
        ocr, ("vatable sales",), exclude=("total sales",), allow_bare_integers=True,
    )
    assert amounts == []


# Images 5 & 6 (North Star Travel, re-run): a booking/ticket reference
# number ("SERVICE FEE OF BS #B0280034") kept getting extracted as its own
# line item, with the ROE/foreign-unit-cost figure (not the item's real PHP
# total) attached as its amount.
def test_north_star_service_fee_reference_line_dropped():
    data = {
        "line_items": [
            {"description": "SERVICE FEE OF BS #B0280034", "quantity": 1, "unit_price": 61.70, "amount": 61.70},
            {"description": "SERVICE FEE (TICKET)", "quantity": 1, "unit_price": 40.0, "amount": 40.0},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"] == "SERVICE FEE (TICKET)"
    assert any("reference number" in n for n in notes)


# ---------------------------------------------------------------------------
# Round 3 (re-run again): customer_name had NO backstop at all, unlike
# vendor_name/invoice_number/invoice_date — it was just as susceptible to
# the same "footer boilerplate" and "unrelated field" mix-ups, confirmed on
# three separate real invoices.
# ---------------------------------------------------------------------------

# Image 1 (SGV): customer_name got set to the value of an unrelated
# "Nature of Services:" field instead of the real "Bill To:" customer.
def test_sgv_customer_name_not_confused_with_nature_of_services():
    ocr = (
        "SYCIP, GORRES, VELAYO & CO.\n"
        "Bill To:\n"
        "TSUKIDEN GLOBAL SOLUTIONS INC.\n"
        "U 2102 21/F One Corporate Ctr\n"
        "Nature of Services: Business Tax Services\n"
        "Client VAT / TIN: 007-848-122-000\n"
    )
    data = {"customer_name": "Business Tax Services"}
    result, notes = reconcile_customer_name(data, ocr)
    assert result["customer_name"] == "TSUKIDEN GLOBAL SOLUTIONS INC."
    assert notes


# Image 5 (North Star Travel): customer_name got set to the printing
# press's own proprietress name/TIN, sitting in the booklet footer next to
# "Printer's Accreditation" and "BIR Permit No.OCN", instead of the real
# "BILLED TO:" customer named near the top of the same page.
def test_north_star_customer_name_not_confused_with_printer_footer():
    ocr = (
        "NORTH STAR INTERNATIONAL TRAVEL INC.\n"
        "BILLED TO: Tsukiden Global Solutions Inc.\n"
        "TIN NO 007-848-122-000\n"
        "250 Pads (50x4) 50001-62500 BIR Permit No.OCN: 04SU20260000002180\n"
        "Macan PrintShop VAT REG. TIN # 908-260-302-00000\n"
        "Mylene Canlas - Proprietress\n"
        "Printer's Accreditation # 050MR20230000090805\n"
    )
    data = {"customer_name": "Mylene Canlas-Proprietres"}
    result, notes = reconcile_customer_name(data, ocr)
    assert result["customer_name"] == "Tsukiden Global Solutions Inc."
    assert notes


# Image 4 (Emerald Mansion): customer_name was left blank even though a
# handwritten "Registered Name:" clearly named the customer — and a less
# reliable "SOLD TO:" code sitting ABOVE it must not be preferred instead.
def test_emerald_mansion_customer_name_filled_from_registered_name_not_sold_to_code():
    ocr = (
        "Emerald Mansion Condominium Association Inc.\n"
        "SOLD TO: EM1107T8\n"
        "Registered Name: TSUKIDEN GLOBAL SOLUTIONS INC\n"
        "TIN:\n"
        "Business Address:\n"
    )
    data = {"customer_name": None}
    result, notes = reconcile_customer_name(data, ocr)
    assert result["customer_name"] == "TSUKIDEN GLOBAL SOLUTIONS INC"
    assert notes


# Image 4 (Emerald Mansion, vendor side): vendor_name got set to the
# printer's own booklet-footer name ("ADEL PRINTING SERVICES") because no
# explicit "Name:" label exists anywhere on this invoice for the real
# merchant, which is printed plainly as a header/logo line instead.
def test_emerald_mansion_vendor_name_falls_back_to_header_when_no_name_label():
    ocr = (
        "Emerald Mansion Condominium Association Inc.\n"
        "G/F Emerald Mansion Emerald Ave Ortigas Ctr\n"
        "Tel. No.: 8638-6834 * VAT Reg. TIN: 005-578-990-00000\n"
        "SALES INVOICE\n"
        "Registered Name: TSUKIDEN GLOBAL SOLUTIONS INC\n"
        "BIR Authority to Print No. OCN: 043AU20250000013398\n"
        "ADEL PRINTING SERVICES, Stall B1, Cartimar Bldg.\n"
        "Adelfa F. Ortega - Prop.\n"
        "Printer's Accreditation No.: 032MP20210000000039\n"
    )
    data = {"vendor_name": "ADEL PRINTING SERVICES"}
    result, notes = reconcile_vendor_name(data, ocr)
    assert result["vendor_name"] == "Emerald Mansion Condominium Association Inc."
    assert notes


# Image 3 (TRI-Q / Responsible Services Inc.): a single "UTILITY SERVICES"
# row got extracted TWICE — once with the net/VATable amount, once with
# the VAT-inclusive Total Sales amount for the same row — instead of once,
# roughly doubling the line-items sum.
def test_triq_duplicate_utility_services_amount_deduped():
    data = {
        "line_items": [
            {"description": "UTILITY SERVICES", "quantity": 1, "unit_price": 85963.99, "amount": 85963.99},
            {"description": "UTILITY SERVICES", "quantity": 1, "unit_price": 96279.67, "amount": 96279.67},
        ],
        "subtotal": 85963.99,
        "tax_amount": 10315.68,
        "total_amount": 94560.39,
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert abs(kept[0]["amount"] - 96279.67) < 0.01  # net + tax = VAT-inclusive figure
    assert any("duplicate" in n.lower() for n in notes)


# Image 1 (SGV): a wide, multi-column professional-fee breakdown table
# (Description | Fee | Expense | Professional Services | Unit Cost |
# Quantity | Net | Tax | Rate | Tax Amount | Total) got its own column
# HEADER labels extracted as three phantom line items, each paired with
# an unrelated number, alongside the one genuine "retainer fee" item.
def test_sgv_table_header_labels_not_kept_as_line_items():
    data = {
        "line_items": [
            {
                "description": "Our retainer fee for the month of August 2026",
                "quantity": 1, "unit_price": 11760.0, "amount": 11760.0,
            },
            {"description": "Professional Services", "quantity": 1, "unit_price": 560.0, "amount": 560.0},
            {"description": "Expense", "quantity": 1, "unit_price": 11200.0, "amount": 11200.0},
            {"description": "Fee", "quantity": 1, "unit_price": 500.0, "amount": 500.0},
        ]
    }
    result, notes = clean_line_items(data)
    kept = result["line_items"]
    assert len(kept) == 1
    assert kept[0]["description"].lower().startswith("our retainer fee")
    assert any("column-header" in n or "column header" in n for n in notes)


# A genuine item whose name merely CONTAINS one of the header words as a
# substring ("Convenience Fee") must survive — only a BARE, standalone
# header word is dropped, never a substring match (unlike
# _looks_like_tax_or_summary_line, which is deliberately substring-based
# for its own, more specific phrases).
def test_genuine_item_containing_header_word_as_substring_survives():
    data = {"line_items": [{"description": "Convenience Fee", "quantity": 1, "unit_price": 50.0, "amount": 50.0}]}
    result, notes = clean_line_items(data)
    assert len(result["line_items"]) == 1
    assert result["line_items"][0]["description"] == "Convenience Fee"


def test_prompt_builder_covers_roe_and_reference_line_guidance():
    # Guards against this prompt-level guidance silently regressing/being
    # deleted in a future edit — the underlying wrong-amount extraction
    # bug (ROE picked instead of the PHP total) can't be fixed in
    # post-processing since the correct value isn't recoverable there, so
    # this instruction is the only real defense against it recurring.
    assert "ROE" in SYSTEM_PROMPT
    assert "SERVICE FEE OF BS" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Round 3: TRI-Q's zero_rated_sales contamination guard was still slipping
# through on a re-run, because the wrong value (96,279.67) equals "Total
# Sales (VAT Inclusive)" — a THIRD figure, distinct from both subtotal
# (85,963.99) and total_amount (94,560.39, which is Total Sales MINUS a
# Withholding Tax of 1,719.28 on this invoice). Comparing only against
# total_amount/subtotal individually can never catch this whenever a
# withholding-tax deduction makes total_amount != Total Sales.
# ---------------------------------------------------------------------------
def test_triq_zero_rated_matches_total_sales_via_subtotal_plus_tax():
    """NOTE: name kept for continuity, but see note on
    test_tri_q_blank_zero_rated_column_not_filled_with_total above — the
    value is now left in place and only flagged, never deleted."""
    data = {
        "zero_rated_sales": 96279.67,
        "vat_exempt_sales": None,
        "subtotal": 85963.99,
        "tax_amount": 10315.68,
        "total_amount": 94560.39,  # Total Sales (96,279.67) - Withholding Tax (1,719.28)
    }
    result, notes = reconcile_zero_rated_exempt(data, ocr_text="")
    assert result["zero_rated_sales"] == 96279.67
    assert any("subtotal + tax_amount" in n for n in notes)


# ---------------------------------------------------------------------------
# Round 3: on a bad extraction run, vendor_name comes back completely empty
# (not merely wrong) and previously fell straight through to the
# "Unknown Vendor" placeholder even though the real business name is
# plainly printed near the top of the OCR text. Uses the real, exact
# (single-line, no reliable newlines) raw OCR text confirmed from an actual
# TRI-Q Responsible Services invoice.
# ---------------------------------------------------------------------------
_TRIQ_RAW_OCR = (
    "# 79 P. Cruz Street,Brgy. Old Zaniga, TRI- 0 1550 City of Mandaluyong NCR, Second District "
    "Philippines Tel. No.: 533-9571 * TeleFax: 534-1269 RESPONSIBLE SERVICES INC. VAT Reg. TIN: "
    "002-176-366-00000 No. 3219 SERVICE INVOICE cashcharge Date: 13-Aug-26 TSUKIDEN GLOBAL "
    "SOLUTIONS, INC. CUSTOMER: Registered Name: TSUKIDEN GLOBAL SOLUTIONS, INC #007-848-122-000 "
    "TIN: Business Address: 21st Floor One Corporate Center, Julia Vargas Corner Meralco Ave. "
    "Ortigas Center,Pasig City NATURE OF SERVICE QUANTITY UUOT UIST AMOUNT Please pay the "
    "following services for the PERiOD of JULY 16-31, 2026 UTILITY SERVICES 96,279.67 96,279.67 "
    "85,963.99 96,279.67 VATable Sales Total Sales (VAT Inclusive) VAT-Exempt Sales Less:VAT "
    "10,315.68 Amount: Net of VAT 85,963.99 Zero Rated Sales 96,279.67 VAT Amount 10,315.68 "
    "Amount Due Less: Withholding Tax 1,719.28 Received th ount of TOTAL AMOUNT DUE 94,560.39"
)


def test_missing_vendor_name_filled_from_real_triq_ocr_text():
    data = {"vendor_name": None}
    result, notes = reconcile_missing_vendor_name(data, _TRIQ_RAW_OCR)
    assert result["vendor_name"] and "RESPONSIBLE SERVICES" in result["vendor_name"].upper()
    assert notes


def test_missing_vendor_name_noop_when_already_present():
    data = {"vendor_name": "Something Already Here"}
    result, notes = reconcile_missing_vendor_name(data, _TRIQ_RAW_OCR)
    assert result["vendor_name"] == "Something Already Here"
    assert notes == []


def test_missing_vendor_name_noop_when_nothing_findable():
    data = {"vendor_name": None}
    result, notes = reconcile_missing_vendor_name(data, "no business suffix words here at all")
    assert result.get("vendor_name") is None
    assert notes == []


# ---------------------------------------------------------------------------
# Round 4: customer_name grabbing the SIGNATURE-BLOCK caption at the bottom
# of the invoice ("Customer Authorized Representative" / "Customer
# Signature Over Printed Name") instead of the real name printed near the
# top under "BILLED TO"/"Registered Name". Confirmed on real North Star
# International Travel and Gliptic Art Enterprise invoices.
# ---------------------------------------------------------------------------
def test_customer_name_signature_block_caption_corrected():
    ocr = (
        "NORTH STAR INTERNATIONAL TRAVEL INC.\n"
        "BILLED TO: Tsukiden Global Solutions Inc.\n"
        "ADDRESS: Unit 2102, 21st Floor\n"
        "TIN NO 007-848-122-000\n"
        "...\n"
        "Customer Authorized Representative\n"
    )
    data = {"customer_name": "Authorized Representative"}
    result, notes = reconcile_customer_name(data, ocr)
    assert result["customer_name"] == "Tsukiden Global Solutions Inc."
    assert notes


def test_customer_name_gliptic_signature_caption_corrected():
    ocr = (
        "SOLD TO:\n"
        "Registered Name: TSUKIDEN GLOBAL SOLUTIONS, INC.\n"
        "TIN: 007-848-122-000\n"
        "...\n"
        "Customer Signature Over Printed Name\n"
    )
    data = {"customer_name": "Signature Over Printed Name"}
    result, notes = reconcile_customer_name(data, ocr)
    assert result["customer_name"] == "TSUKIDEN GLOBAL SOLUTIONS, INC."
    assert notes


# ---------------------------------------------------------------------------
# Round 4: a "Customer TIN" table cell (value on a separate line/cell, not
# the same line) got matched as the bare "Customer:" name label, capturing
# the single leftover word "TIN" as if it were the customer's name.
# Confirmed on a real Globe Business invoice.
# ---------------------------------------------------------------------------
def test_customer_name_not_confused_with_customer_tin_label():
    ocr = (
        "Tsukiden Global Solutions Inc\n"
        "ERIC C FRANCISCO\n"
        "Customer TIN\n"
        "007-848-122-00000\n"
        "Invoice Date\n"
        "08/05/26\n"
    )
    data = {"customer_name": "TIN"}
    result, notes = reconcile_customer_name(data, ocr)
    # "TIN" isn't grounded as a real customer-name label match anymore, and
    # no better candidate exists on this invoice (Globe Business has no
    # explicit "Billed To"/"Registered Name" block) — the wrong value
    # should be cleared away rather than kept, even if nothing replaces it.
    assert result.get("customer_name") != "TIN"


# ---------------------------------------------------------------------------
# Round 4: vendor_tax_id / customer_tax_id reconciliation.
# ---------------------------------------------------------------------------
_NORTH_STAR_TIN_OCR = (
    "NORTH STAR INTERNATIONAL TRAVEL INC.\n"
    "VAT Reg. TIN: 000-423-215-00000\n"
    "BILLED TO: Tsukiden Global Solutions Inc.\n"
    "ADDRESS: Unit 2102, 21st Floor, One Corporate Centre\n"
    "TIN NO 007-848-122-000\n"
    "OSCA/ PWD/SP ID NO\n"
    "...\n"
    "Macan PrintShop VAT REG. TIN # 908-260-302-00000\n"
    "Mylene Canlas - Proprietress\n"
)


def test_customer_tax_id_printer_footer_tin_corrected():
    data = {"vendor_tax_id": None, "customer_tax_id": "908-260-302-00000"}
    result, notes = reconcile_tax_ids(data, _NORTH_STAR_TIN_OCR)
    assert result["customer_tax_id"] == "007-848-122-000"
    assert any("customer_tax_id" in n for n in notes)


def test_vendor_tax_id_filled_from_vat_reg_tin():
    data = {"vendor_tax_id": None, "customer_tax_id": "007-848-122-000"}
    result, notes = reconcile_tax_ids(data, _NORTH_STAR_TIN_OCR)
    assert result["vendor_tax_id"] == "000-423-215-00000"


def test_tax_ids_swapped_with_each_other_get_corrected():
    # vendor_tax_id and customer_tax_id swapped with each other.
    data = {"vendor_tax_id": "007-848-122-000", "customer_tax_id": "000-423-215-00000"}
    result, notes = reconcile_tax_ids(data, _NORTH_STAR_TIN_OCR)
    assert result["vendor_tax_id"] == "000-423-215-00000"
    assert result["customer_tax_id"] == "007-848-122-000"


def test_tax_ids_noop_when_already_correct():
    data = {"vendor_tax_id": "000-423-215-00000", "customer_tax_id": "007-848-122-000"}
    result, notes = reconcile_tax_ids(data, _NORTH_STAR_TIN_OCR)
    assert result["vendor_tax_id"] == "000-423-215-00000"
    assert result["customer_tax_id"] == "007-848-122-000"
    assert notes == []


# ---------------------------------------------------------------------------
# Round 5: vendor_name grounding check failed to fire AT ALL because the
# LLM's vendor_name ("ADELPRINTING SERVICES", words merged) didn't
# exact-substring-match the OCR text's own spelling ("ADEL PRINTING
# SERVICES", with a space) — so the untrusted-printer-boilerplate
# correction never even ran. Confirmed via the actual raw pre-post-
# processing LLM JSON output for a real Emerald Mansion Condominium
# Association invoice.
# ---------------------------------------------------------------------------
_EMERALD_MANSION_OCR = (
    "Emerald Mansion Condominium Association Inc.\n"
    "G/F Emerald Mansion Emerald Ave Ortigas Ctr.\n"
    "San Antonio Pasig City 1603\n"
    "Tel. No.: 8638-6834 * VAT Reg. TIN: 005-578-990-00000\n"
    "CASH SALES\n"
    "CHARGE SALES\n"
    "No 7512\n"
    "SOLD TO:\n"
    "Registered Name: TSUKIDEN GLOBAL SOLUTIONS INC\n"
    "TIN:\n"
    "Business Address:\n"
    "Nature of Service Amount\n"
    "utilities for Aug 2026 2,247.40\n"
    "100 Bklts. (50x3) 5001-10000\n"
    "BIR Authority to Print No.: OCN: 043AU20250000013398\n"
    "ADEL PRINTING SERVICES, Stall B1, Cartimar Bldg.\n"
    "C.M. Recto Ave. Brgy. 391 Zone 040, Quaipo, Manila 1001, Philippines\n"
    "NON VAT Reg. TIN: 465-709-231-00000\n"
    "Printer's Accreditation No.: 032MP20210000000039\n"
)


def test_vendor_name_grounding_survives_merged_vs_spaced_word_mismatch():
    data = {"vendor_name": "ADELPRINTING SERVICES"}
    result, notes = reconcile_vendor_name(data, _EMERALD_MANSION_OCR)
    assert result["vendor_name"] == "Emerald Mansion Condominium Association Inc."
    assert notes


def test_emerald_mansion_checkbox_items_dropped_from_real_raw_llm_output():
    # The exact shape of the raw (pre-post-processing) LLM JSON output
    # confirmed for this invoice: two phantom line items, one of which
    # (CHARGE SALES) carries the invoice number itself (7512) misread as
    # a peso amount.
    data = {
        "line_items": [
            {"description": "CHARGE SALES", "quantity": 1.0, "unit_price": 0.0, "amount": 7512.0},
            {"description": "CASH SALES", "quantity": 1.0, "unit_price": 0.0, "amount": 0.0},
        ]
    }
    result, notes = clean_line_items(data)
    assert result["line_items"] == []
