"""Validates the JSON returned by the LLM before it becomes an Invoice model."""
import re
from datetime import datetime
from config.constants import REQUIRED_FIELDS

_NET_TERMS_RE = re.compile(r"net\s*[:\-]?\s*(\d{1,3})\b", re.IGNORECASE)
# Matches a description that's ENTIRELY a number/price — e.g. "225.00",
# "2092500130408", "$2,445.00" — with nothing else. A real item name never
# looks like this; it's the strongest available signal that the extraction
# lost the item name in a multi-line layout (barcode/name/price on separate
# lines) and used the code or price as the description instead.
_BARE_NUMBER_DESCRIPTION_RE = re.compile(r"^[$₱P]?[\d,]+\.?\d*$")

# A common PH-receipt itemized-table footer marking the end of the list
# (variants: "*** NOTHING FOLLOWS ***", "NOTHING FOLLOWS", "X** NOTHING
# FOLLOWS **X" once OCR mangles the asterisks into letters) — never an
# actual purchased item. Confirmed on a real Gliptic Art Enterprise
# service invoice where this footer got extracted as a phantom line item
# with a fabricated price.
_NOTHING_FOLLOWS_RE = re.compile(r"nothing\s+follow", re.IGNORECASE)

# Checkbox/option-label boilerplate that sits right next to the item table
# on many PH BIR invoice forms ("☐ CASH SALES  ☐ CHARGE SALES") — never a
# real purchased item, but easy to grab by mistake since it's printed in
# the same visual area as the actual service/item description. Confirmed
# on a real Emerald Mansion Condominium Association invoice where the item
# description came back as "CHARGE SALES" instead of the handwritten
# "utilities for Aug 2026" actually printed on the line below it.
_CHECKBOX_BOILERPLATE_DESCRIPTIONS = {"cash sales", "charge sales", "cash", "charge"}

# A monetary amount formatted as a document number, e.g. invoice_number
# came back as "50,920.00" — always wrong; no real invoice number is
# written with a decimal point and exactly 2 fractional digits. Confirmed
# on a real PC Worth sales invoice where invoice_number was extracted as
# the printed TOTAL AMOUNT DUE figure instead of the actual "Nº 08758"
# printed at the top of the document.
_MONEY_LIKE_RE = re.compile(r"^[$₱P]?[\d,]+\.\d{2}$")

# Boilerplate compliance/registration numbers printed on nearly EVERY PH
# BIR-formatted invoice, near the bottom, and never the document's own
# identifier — but formatted just like one ("<Label> No. <alphanumeric
# code>"), so easy to mistake for it. Confirmed on two separate real North
# Star Travel invoices where invoice_number came back as
# "LL No.LLAR-049-06/2024-001213" (or, once the LLM dropped the label
# text, just the bare code "LLAR-049-06/2024-001213") — the printer's
# Loose-Leaf accreditation number printed in tiny text at the very bottom
# next to "Date Issued: June 20, 2024" — instead of the actual invoice
# number ("52091"/"44072") printed in red at the top of the document.
# "llar-" is included as a direct value-prefix check (not just a
# label-context check) specifically because the LLM sometimes returns
# only the bare code with the "LL No." label already stripped off.
_BOILERPLATE_ID_LABELS = (
    "ll no", "llar-", "loose-leaf", "looseleaf", "loose leaf",
    "printer's accreditation", "printers accreditation",
    "bir authority to print", "authority to print",
    "permit no", "ocn:", "ocn ", "date of atp", "atp:",
    "machine serial", "min:", "min #", "pos s/n", "s/n:",
)

# Form-section labels that occasionally get extracted as if they were the
# actual value of the field they introduce (e.g. customer_name coming back
# as "Received By:" — the printed line ABOVE the actual name, not a name
# itself). Confirmed on a real (very poor-quality) Gliptic Art Enterprise
# scan. Checked as an exact (case-insensitive, punctuation-stripped) match
# rather than substring, since a genuine name could legitimately contain
# one of these words elsewhere.
_FORM_LABEL_BOILERPLATE = {
    "received by", "sold to", "bill to", "billed to", "customer",
    "registered name", "business address", "business name",
    "customer signature over printed name", "attention",
}


def _looks_like_form_label(value: str) -> bool:
    return value.strip().lower().rstrip(":").strip() in _FORM_LABEL_BOILERPLATE


_VOWELS = set("AEIOU")
# Real business names — however abbreviation-heavy ("PC WORTH", "LBC
# EXPRESS", "NTT DoCoMo") — never run more than ~5 consonants in a row.
# A vendor_name that's actually garbled OCR noise (e.g. a stylized logo
# PaddleOCR couldn't read: a real DITO Telecom receipt came back as
# "DONRNTTNRN PPIATTRNNNNPPNNN") blows way past that — 12+ in a row in
# that real example. Checked against a batch of real vendor names
# (Philippine Seven Corporation, ACE HARDWARE, CGD MEDICAL DEPOT, DATA
# BLITZ, LBC EXPRESS, PC WORTH, DITO TELECOMMUNITY CORPORATION, SM
# DEVELOPMENT CORPORATION, BDO Unibank, PLDT Home, GCash) — max run was 5
# ("PLDT Home"), so 7 leaves comfortable margin without false-flagging
# legitimate abbreviation-style names.
_GIBBERISH_CONSONANT_RUN_THRESHOLD = 7


def _amount_appears_in_ocr_text(ocr_text: str, value: float) -> bool:
    """Whether `value` shows up verbatim anywhere in the OCR text, comma-
    formatted or not (same check as ai/post_processing.py::
    _amount_appears_in_text, duplicated locally rather than imported to
    keep this module's only dependency on config/constants — see the
    ai/post_processing.py note on why ZERO_RATED_LABELS/VAT_EXEMPT_LABELS
    must not be silently re-shadowed; the same "one accidental copy can
    drift from the other" risk doesn't apply to a two-line pure function
    like this one)."""
    return f"{value:.2f}" in ocr_text or f"{value:,.2f}" in ocr_text


def _longest_consonant_run(text: str) -> int:
    letters = re.sub(r"[^A-Za-z]", "", text or "").upper()
    best = current = 0
    for ch in letters:
        if ch in _VOWELS:
            current = 0
        else:
            current += 1
            best = max(best, current)
    return best


def validate_extraction(
    data: dict, template_name: str | None = None, ocr_text: str | None = None,
) -> list[str]:
    """Returns a list of human-readable issues; empty list means it's valid."""
    issues = []

    if not isinstance(data, dict):
        return ["Response is not a JSON object."]

    for field in REQUIRED_FIELDS:
        if field not in data or data[field] in (None, "", []):
            issues.append(f"Missing required field: '{field}'")

    total = data.get("total_amount")
    total_f = None
    if total is not None:
        try:
            total_f = float(total)
            if total_f <= 0:
                issues.append("total_amount must be greater than 0")
        except (TypeError, ValueError):
            issues.append("total_amount is not numeric")

    # Anti-hallucination check: the printed total is a real number sitting
    # somewhere on the page, so a correctly-read total_amount should appear
    # verbatim in the OCR text. This is DELIBERATELY independent of every
    # arithmetic cross-check elsewhere in this function — those only catch
    # the extraction disagreeing with ITSELF (subtotal+tax != total), which
    # does nothing when the LLM invents a total_amount that's internally
    # consistent with an ALSO-wrong subtotal/tax it derived from
    # double-counted line items. Confirmed on a real SGV (SyCip Gorres
    # Velayo & Co.) invoice: the printed Total Amount was PHP 11,760.00
    # (also printed a second time in the remittance box), but two separate
    # extraction runs each invented a different total (13,520.00 and
    # 13,020.00) that agreed perfectly with THAT run's own subtotal+tax —
    # neither figure ever appears anywhere in the OCR text at all, yet
    # both runs scored "processed" with no arithmetic issues raised. Only
    # checked when ocr_text is actually supplied (existing callers that
    # don't pass it keep their previous behavior unchanged), and only for a
    # total_amount that parsed as a valid positive number above.
    if total_f is not None and total_f > 0 and ocr_text:
        if not _amount_appears_in_ocr_text(ocr_text, total_f):
            issues.append(
                f"total_amount ({total_f:.2f}) does not appear verbatim anywhere in the OCR "
                f"text — it may have been invented/derived rather than read directly off the "
                f"document (even if it's internally consistent with subtotal/tax); check the "
                f"source image manually."
            )

    subtotal = data.get("subtotal")
    tax = data.get("tax_amount") or 0
    discount = data.get("discount") or 0
    # zero_rated_sales/vat_exempt_sales are separate BIR sales categories
    # that still count toward total_amount alongside the VATable subtotal
    # (see config/constants.py EXTRACTION_SCHEMA) — omitting them here
    # would make a perfectly correct invoice that HAS one of these columns
    # populated look like its numbers "don't add up".
    zero_rated = data.get("zero_rated_sales") or 0
    vat_exempt = data.get("vat_exempt_sales") or 0
    # 1.55: billing statements may carry a previous balance. Validate current-period
    # accounting against current_charges_total, then separately check that the
    # payable total includes the carried balance. Ordinary invoices still use
    # total_amount exactly as before.
    current_charges_total = data.get("current_charges_total")
    previous_balance = data.get("previous_balance")
    accounting_target = current_charges_total if current_charges_total is not None else total
    if subtotal is not None and accounting_target is not None:
        try:
            computed = float(subtotal) + float(tax) + float(zero_rated) + float(vat_exempt) - float(discount)
            target_f = float(accounting_target)
            if target_f and abs(computed - target_f) > 0.05 * abs(target_f):
                target_name = "current_charges_total" if current_charges_total is not None else "total_amount"
                issues.append(
                    f"subtotal ({subtotal}) + tax ({tax}) + zero-rated ({zero_rated}) "
                    f"+ vat-exempt ({vat_exempt}) - discount ({discount}) = {computed:.2f}, "
                    f"which does not match {target_name} ({accounting_target})"
                )
            if current_charges_total is not None and previous_balance is not None and total is not None:
                payable = round(float(current_charges_total) + float(previous_balance), 2)
                if abs(payable - float(total)) > 0.05 * max(abs(float(total)), 1.0):
                    issues.append(
                        f"current_charges_total ({current_charges_total}) + previous_balance ({previous_balance}) "
                        f"= {payable:.2f}, which does not match total_amount/Amount to Pay ({total})"
                    )
        except (TypeError, ValueError):
            issues.append("subtotal/tax/discount are not numeric")
    elif subtotal is None and total is not None:
        # subtotal isn't in REQUIRED_FIELDS (a handful of legitimate
        # documents genuinely have none, e.g. a plain payment/collection
        # receipt with no VAT breakdown), so a missing subtotal alone
        # doesn't fail extraction. But when it's null, the cross-check
        # above never runs at all — so a real invoice whose subtotal the
        # LLM simply failed to find sails through with zero issues raised,
        # and the History UI's `(subtotal or 0)` display then renders that
        # missing value as a confident-looking "0.00" (see a real CGD
        # Medical Depot receipt: subtotal null, tax_amount 160.71, total
        # 1,500.00 — displayed as Net Amount 0.00 PHP, status "processed",
        # never reviewed). Flag it explicitly instead so it forces
        # needs_review and the reviewer knows to check the source image,
        # rather than trusting a silently-defaulted zero.
        try:
            total_f = float(total)
            if total_f > 0:
                issues.append(
                    "subtotal/net amount is missing (null) — could not verify against "
                    f"total_amount ({total}); check the source document manually."
                )
        except (TypeError, ValueError):
            pass

    currency = data.get("currency")
    if currency and (not isinstance(currency, str) or len(currency) != 3):
        issues.append("currency must be a 3-letter ISO code")

    invoice_number = data.get("invoice_number")
    if invoice_number and isinstance(invoice_number, str):
        inv_num_stripped = invoice_number.strip()
        inv_num_lower = inv_num_stripped.lower()

        if any(label in inv_num_lower for label in _BOILERPLATE_ID_LABELS):
            issues.append(
                f"invoice_number '{invoice_number}' contains boilerplate compliance/"
                f"registration text (e.g. printer's accreditation, BIR authority-to-print, "
                f"or 'LL No.' loose-leaf permit number) — these are printed near the bottom "
                f"of nearly every PH BIR invoice and are never the document's own invoice "
                f"number; check the source image manually for the real one (usually near "
                f"the top, often in red/bold, labeled 'No.', 'Invoice #', 'SI No.', or "
                f"'OR #')."
            )
        elif _MONEY_LIKE_RE.match(inv_num_stripped):
            as_amount = None
            try:
                as_amount = float(inv_num_stripped.lstrip("$₱P").replace(",", ""))
            except (TypeError, ValueError):
                pass
            matched_field = None
            for money_field in ("total_amount", "subtotal", "tax_amount"):
                candidate = data.get(money_field)
                if as_amount is None or candidate is None:
                    continue
                try:
                    candidate_f = float(candidate)
                except (TypeError, ValueError):
                    continue
                if abs(as_amount - candidate_f) < 0.01:
                    matched_field = money_field
                    break
            if matched_field:
                issues.append(
                    f"invoice_number '{invoice_number}' is formatted as a monetary "
                    f"amount and matches {matched_field} ({data.get(matched_field)}) exactly "
                    f"— likely picked up a total/amount figure instead of the actual "
                    f"printed invoice/document number; check the source image manually."
                )
            else:
                issues.append(
                    f"invoice_number '{invoice_number}' is formatted like a monetary amount "
                    f"(e.g. '50,920.00') rather than a document number — likely misread; "
                    f"check the source image manually."
                )

    vendor_name = data.get("vendor_name")
    if vendor_name and isinstance(vendor_name, str):
        if _looks_like_form_label(vendor_name):
            issues.append(
                f"vendor_name '{vendor_name}' is a form-section label (e.g. 'Received By:', "
                f"'Registered Name'), not an actual business name — the real name is usually "
                f"printed on the line right after this label; check the source image manually."
            )
        else:
            run = _longest_consonant_run(vendor_name)
            if run >= _GIBBERISH_CONSONANT_RUN_THRESHOLD:
                issues.append(
                    f"vendor_name '{vendor_name}' looks like garbled OCR text, not a real "
                    f"business name (a {run}-letter unbroken consonant run) — likely a "
                    f"stylized logo/heading the OCR engine couldn't read; check the source "
                    f"image manually."
                )

    customer_name = data.get("customer_name")
    if customer_name and isinstance(customer_name, str) and _looks_like_form_label(customer_name):
        issues.append(
            f"customer_name '{customer_name}' is a form-section label (e.g. 'Received By:', "
            f"'Sold To:'), not an actual customer name — the real name is usually printed on "
            f"the line right after this label; check the source image manually."
        )

    line_items = data.get("line_items")
    if line_items is not None and not isinstance(line_items, list):
        issues.append("line_items must be a list")
    elif isinstance(line_items, list):
        for li in line_items:
            desc = str((li or {}).get("description") or "").strip()
            if not desc:
                continue
            if _BARE_NUMBER_DESCRIPTION_RE.match(desc):
                issues.append(
                    f"line item description '{desc}' is just a number/code, not a product "
                    f"name — likely a barcode or price that got used as the description "
                    f"instead of the actual item name (see prompt_builder rule 9)"
                )
            elif _NOTHING_FOLLOWS_RE.search(desc):
                issues.append(
                    f"line item description '{desc}' looks like a misread \"*** NOTHING "
                    f"FOLLOWS ***\" table-footer marker, not a real purchased item — this "
                    f"end-of-list marker (common on PH BIR-formatted invoices) should never "
                    f"be extracted as a line item; check the source image manually."
                )
            elif desc.lower() in _CHECKBOX_BOILERPLATE_DESCRIPTIONS:
                issues.append(
                    f"line item description '{desc}' looks like a 'CASH SALES'/'CHARGE "
                    f"SALES' checkbox option label from the invoice form itself, not a real "
                    f"item/service description — check the source image manually for the "
                    f"actual handwritten/printed description nearby."
                )
            else:
                run = _longest_consonant_run(desc)
                if run >= _GIBBERISH_CONSONANT_RUN_THRESHOLD:
                    issues.append(
                        f"line item description '{desc}' looks like garbled OCR text, not a "
                        f"real product name (a {run}-letter unbroken consonant run) — likely "
                        f"illegible handwriting/printing the OCR engine couldn't read; check "
                        f"the source image manually."
                    )

        # Cross-check: a line item whose amount equals the sum of the OTHER
        # line items is almost certainly an aggregate/subtotal row that got
        # included as if it were its own separate purchased item — not a
        # genuine additional charge. Confirmed on a real SGV invoice: a
        # table listing "Fee" (11,200.00) and "Expense" (560.00) rows was
        # followed by a bold "Professional Services" row printing their
        # SUM (11,760.00) as the category subtotal — that third row got
        # extracted as a third line item, double-counting the invoice and
        # corrupting the subtotal reconciliation below.
        if len(line_items) >= 2:
            try:
                amounts = [float(li.get("amount", 0) or 0) for li in line_items]
                for i, amt in enumerate(amounts):
                    if amt <= 0:
                        continue
                    others_sum = sum(a for j, a in enumerate(amounts) if j != i)
                    if others_sum > 0 and abs(amt - others_sum) < 0.01:
                        desc = (line_items[i].get("description") or "").strip()
                        issues.append(
                            f"line item '{desc}' ({amt:.2f}) equals the sum of every other "
                            f"line item combined — this is almost always a printed category "
                            f"subtotal/aggregate row (e.g. a bold summary row under separate "
                            f"'Fee'/'Expense' rows), not a genuine additional purchased item; "
                            f"including it double-counts the invoice. Check the source image "
                            f"manually."
                        )
            except (TypeError, ValueError, AttributeError):
                pass

        if line_items and subtotal is not None:
            # Cross-check: for generic invoices, line-item amounts are expected
            # to reconcile to subtotal. Watsons is different: its `subtotal`
            # field is specifically the VAT SALE / AMOUNT net-of-VAT figure,
            # while the line-item amounts are the transaction prices and the
            # printed discount bridges line-items to Amount To Pay. The
            # Watsons-specific validation below checks that exact identity.
            if template_name == "watsons":
                pass
            else:
                try:
                    current_items = [li for li in line_items if str((li or {}).get("description") or "").strip().lower() not in {"remaining balance", "previous balance", "carried balance", "balance forward"}]
                    items_sum = sum(float(li.get("amount", 0) or 0) for li in current_items)
                    subtotal_f = float(subtotal)
                    if subtotal_f and abs(items_sum - subtotal_f) > 0.05 * subtotal_f:
                        issues.append(
                            f"line_items sum to {items_sum:.2f}, which does not match "
                            f"subtotal ({subtotal_f}) â€” subtotal may have been misread "
                            f"as one of the individual line amounts"
                        )
                except (TypeError, ValueError, AttributeError):
                    pass

    # Cross-check: if payment_terms says "Net N", the gap between
    # invoice_date and due_date should be N days. Invoices frequently state
    # this explicitly, and it's a strong signal for catching a misread date
    # (e.g. OCR dropping a digit) even when we can't tell which of the two
    # dates is the wrong one.
    terms = data.get("payment_terms")
    invoice_date, due_date = data.get("invoice_date"), data.get("due_date")
    if terms and invoice_date and due_date:
        match = _NET_TERMS_RE.search(str(terms))
        if match:
            try:
                expected_days = int(match.group(1))
                d1 = datetime.fromisoformat(str(invoice_date)).date()
                d2 = datetime.fromisoformat(str(due_date)).date()
                actual_days = (d2 - d1).days
                if actual_days != expected_days:
                    issues.append(
                        f"due_date - invoice_date = {actual_days} days, but "
                        f"payment_terms says Net {expected_days} â€” one of the "
                        f"two dates was likely misread"
                    )
            except (ValueError, TypeError):
                pass

    # Watsons has a stable financial identity: the VATable/net amount is
    # explicitly the VAT SALE / AMOUNT figure, total is Amount To Pay, and
    # the line-item total after the printed discount must equal Amount To Pay.
    # These checks are intentionally template-specific; applying them to all
    # invoices would reject legitimate invoices with service charges or other
    # non-standard totals.
    if template_name == "watsons":
        try:
            net = float(data.get("subtotal")) if data.get("subtotal") is not None else None
            vat = float(data.get("tax_amount") or 0)
            total = float(data.get("total_amount")) if data.get("total_amount") is not None else None
            discount = float(data.get("discount") or 0)
        except (TypeError, ValueError):
            net = vat = total = discount = None

        if net is not None and total is not None and vat is not None:
            expected_net = round(total - vat, 2)
            if abs(net - expected_net) > 0.01:
                issues.append(
                    f"Watsons validation: Net Amount (Vatable Sales) ({net:.2f}) must equal "
                    f"Total Amount Due ({total:.2f}) - VAT ({vat:.2f}) = {expected_net:.2f}."
                )
            expected_total = round(net + vat, 2)
            if abs(total - expected_total) > 0.01:
                issues.append(
                    f"Watsons validation: Total Amount Due ({total:.2f}) must equal "
                    f"Net Amount (Vatable Sales) ({net:.2f}) + VAT ({vat:.2f}) = {expected_total:.2f}."
                )

        items = data.get("line_items")
        if isinstance(items, list) and total is not None:
            try:
                items_sum = round(sum(float(li.get("amount") or 0) for li in items), 2)
                expected_total = round(items_sum - discount, 2)
                if abs(expected_total - total) > 0.01:
                    issues.append(
                        f"Watsons validation: sum of line-item Total Unit Price ({items_sum:.2f}) "
                        f"- Discount ({discount:.2f}) = {expected_total:.2f}, but Total Amount Due "
                        f"is {total:.2f}."
                    )
            except (TypeError, ValueError, AttributeError):
                issues.append("Watsons validation: line-item amounts are not numeric; cannot verify line-item total minus discount.")

    return issues
