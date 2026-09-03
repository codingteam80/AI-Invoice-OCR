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


def validate_extraction(data: dict, template_name: str | None = None) -> list[str]:
    """Returns a list of human-readable issues; empty list means it's valid."""
    issues = []

    if not isinstance(data, dict):
        return ["Response is not a JSON object."]

    for field in REQUIRED_FIELDS:
        if field not in data or data[field] in (None, "", []):
            issues.append(f"Missing required field: '{field}'")

    total = data.get("total_amount")
    if total is not None:
        try:
            total_f = float(total)
            if total_f <= 0:
                issues.append("total_amount must be greater than 0")
        except (TypeError, ValueError):
            issues.append("total_amount is not numeric")

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
    if subtotal is not None and total is not None:
        try:
            computed = float(subtotal) + float(tax) + float(zero_rated) + float(vat_exempt) - float(discount)
            if total and abs(computed - float(total)) > 0.05 * float(total):
                issues.append(
                    f"subtotal ({subtotal}) + tax ({tax}) + zero-rated ({zero_rated}) "
                    f"+ vat-exempt ({vat_exempt}) - discount ({discount}) = {computed:.2f}, "
                    f"which does not match total_amount ({total})"
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

    vendor_name = data.get("vendor_name")
    if vendor_name and isinstance(vendor_name, str):
        run = _longest_consonant_run(vendor_name)
        if run >= _GIBBERISH_CONSONANT_RUN_THRESHOLD:
            issues.append(
                f"vendor_name '{vendor_name}' looks like garbled OCR text, not a real "
                f"business name (a {run}-letter unbroken consonant run) — likely a "
                f"stylized logo/heading the OCR engine couldn't read; check the source "
                f"image manually."
            )

    line_items = data.get("line_items")
    if line_items is not None and not isinstance(line_items, list):
        issues.append("line_items must be a list")
    elif isinstance(line_items, list):
        for li in line_items:
            desc = str((li or {}).get("description") or "").strip()
            if desc and _BARE_NUMBER_DESCRIPTION_RE.match(desc):
                issues.append(
                    f"line item description '{desc}' is just a number/code, not a product "
                    f"name — likely a barcode or price that got used as the description "
                    f"instead of the actual item name (see prompt_builder rule 9)"
                )

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
                    items_sum = sum(float(li.get("amount", 0) or 0) for li in line_items)
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
