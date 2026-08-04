"""Validates the JSON returned by the LLM before it becomes an Invoice model."""
from config.constants import REQUIRED_FIELDS


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

    return issues
