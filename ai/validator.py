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


def validate_extraction(data: dict) -> list[str]:
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
    if subtotal is not None and total is not None:
        try:
            computed = float(subtotal) + float(tax) - float(discount)
            if total and abs(computed - float(total)) > 0.05 * float(total):
                issues.append(
                    f"subtotal ({subtotal}) + tax ({tax}) - discount ({discount}) "
                    f"= {computed:.2f}, which does not match total_amount ({total})"
                )
        except (TypeError, ValueError):
            issues.append("subtotal/tax/discount are not numeric")

    currency = data.get("currency")
    if currency and (not isinstance(currency, str) or len(currency) != 3):
        issues.append("currency must be a 3-letter ISO code")

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
            # Cross-check: do the line items actually add up to the claimed
            # subtotal? This catches the common failure mode where the LLM
            # (usually because OCR reading order scrambled the layout) mistakes
            # a single line item's amount for the invoice-level subtotal.
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

    return issues
