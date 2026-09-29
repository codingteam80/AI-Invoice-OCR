"""Deterministic, validated filtering for AI natural-language searches.

The LLM never receives database access and never emits executable SQL. It only
produces a small JSON search plan. This module validates that plan against an
allow-list, then applies it to the already-loaded invoice/log dictionaries.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


# Searchable invoice fields. These cover the business-facing fields saved with
# an invoice. Internal confidence is intentionally excluded from AI Search
# because management requested confidence not be surfaced in the UI.
INVOICE_TEXT_FIELDS = {
    "invoice_number",
    "vendor_name",
    "vendor_address",
    "vendor_tax_id",
    "customer_name",
    "customer_contact",
    "customer_address",
    "customer_tax_id",
    "plate_number",
    "original_filename",
    "source_file",
    "enhanced_image_path",
    "payment_terms",
    "ocr_engine_used",
    "raw_text",
    "vision_notes",
    "line_items",
}
INVOICE_DATE_FIELDS = {"invoice_date", "date_uploaded", "due_date"}
INVOICE_NUMBER_FIELDS = {
    "id",
    "subtotal",
    "tax_amount",
    "tax_rate",
    "withholding_tax",
    "zero_rated_sales",
    "vat_exempt_sales",
    "total_amount",
    "current_charges_total",
    "previous_balance",
}
INVOICE_CHOICE_FIELDS = {"category", "status", "currency"}
INVOICE_BOOLEAN_FIELDS = {"locked"}
# Presence/absence checks are allowed for every searchable invoice field. This
# powers requests such as "with plate number", "without customer TIN", or
# "with previous balance".
INVOICE_PRESENCE_FIELDS = (
    INVOICE_TEXT_FIELDS
    | INVOICE_DATE_FIELDS
    | INVOICE_NUMBER_FIELDS
    | INVOICE_CHOICE_FIELDS
    | INVOICE_BOOLEAN_FIELDS
)

LOG_TEXT_FIELDS = {"user", "action", "entity_type", "details"}
LOG_DATE_FIELDS = {"date_time"}
LOG_NUMBER_FIELDS = {"id", "entity_id"}
LOG_CHOICE_FIELDS: set[str] = set()
LOG_BOOLEAN_FIELDS: set[str] = set()
LOG_PRESENCE_FIELDS: set[str] = set()

TEXT_OPERATORS = {"contains", "equals", "not_contains", "not_equals", "starts_with", "ends_with"}
DATE_OPERATORS = {"on", "before", "on_or_before", "after", "on_or_after", "between"}
NUMBER_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "between"}
CHOICE_OPERATORS = {"in", "not_in"}
PRESENCE_OPERATORS = {"present", "missing"}

FIELD_LABELS = {
    "id": "ID",
    "invoice_number": "Invoice #",
    "invoice_date": "Invoice Date",
    "date_uploaded": "Date Uploaded",
    "due_date": "Due Date",
    "vendor_name": "Vendor",
    "vendor_address": "Vendor Address",
    "vendor_tax_id": "Vendor TIN",
    "customer_name": "Customer",
    "customer_contact": "Customer Contact",
    "customer_address": "Customer Address",
    "customer_tax_id": "Customer TIN",
    "plate_number": "Plate #",
    "original_filename": "Filename",
    "source_file": "Source File",
    "enhanced_image_path": "Enhanced Image",
    "payment_terms": "Payment Terms",
    "ocr_engine_used": "OCR Engine",
    "raw_text": "Raw OCR Text",
    "vision_notes": "Vision Notes",
    "line_items": "Items Purchased / Line Items",
    "subtotal": "Vatable Sales",
    "tax_amount": "VAT",
    "tax_rate": "Tax Rate",
    "withholding_tax": "Withholding Tax",
    "zero_rated_sales": "Zero-Rated Sales",
    "vat_exempt_sales": "VAT-Exempt Sales",
    "total_amount": "Total Amount Due",
    "current_charges_total": "Current Charges Total",
    "previous_balance": "Previous Balance",
    "category": "Category",
    "status": "Status",
    "currency": "Currency",
    "locked": "Locked",
    "date_time": "Date & Time",
    "user": "User",
    "action": "Action",
    "entity_type": "Type",
    "entity_id": "Entity ID",
    "details": "Details",
}


def search_schema_for(page: str) -> dict[str, set[str]]:
    normalized = (page or "").strip().lower()
    if normalized in {"history", "reports", "invoice", "invoices"}:
        return {
            "text": INVOICE_TEXT_FIELDS,
            "dates": INVOICE_DATE_FIELDS,
            "numbers": INVOICE_NUMBER_FIELDS,
            "choices": INVOICE_CHOICE_FIELDS,
            "booleans": INVOICE_BOOLEAN_FIELDS,
            "presence": INVOICE_PRESENCE_FIELDS,
        }
    if normalized in {"log", "logs"}:
        return {
            "text": LOG_TEXT_FIELDS,
            "dates": LOG_DATE_FIELDS,
            "numbers": LOG_NUMBER_FIELDS,
            "choices": LOG_CHOICE_FIELDS,
            "booleans": LOG_BOOLEAN_FIELDS,
            "presence": LOG_PRESENCE_FIELDS,
        }
    raise ValueError(f"Unsupported AI-search page: {page}")


def empty_search_plan() -> dict:
    return {"text": [], "dates": [], "numbers": [], "choices": [], "booleans": [], "presence": []}


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "locked"}:
            return True
        if normalized in {"false", "no", "0", "unlocked"}:
            return False
    return None


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def normalize_search_plan(raw: Any, page: str) -> tuple[dict, list[str]]:
    """Return an allow-listed search plan and non-fatal validation warnings."""
    schema = search_schema_for(page)
    plan = empty_search_plan()
    warnings: list[str] = []

    if not isinstance(raw, dict):
        return plan, ["The AI response was not a JSON object."]

    for item in raw.get("text", []) if isinstance(raw.get("text", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid text condition.")
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if field not in schema["text"] or operator not in TEXT_OPERATORS or not value:
            warnings.append(f"Ignored unsupported text condition: {field or '?'} {operator or '?'}.")
            continue
        plan["text"].append({"field": field, "operator": operator, "value": value})

    for item in raw.get("dates", []) if isinstance(raw.get("dates", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid date condition.")
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip().lower()
        value = _iso_date(item.get("value"))
        value2 = _iso_date(item.get("value2")) if item.get("value2") is not None else None
        if field not in schema["dates"] or operator not in DATE_OPERATORS or not value:
            warnings.append(f"Ignored unsupported date condition: {field or '?'} {operator or '?'}.")
            continue
        if operator == "between":
            if not value2:
                warnings.append(f"Ignored incomplete date range for {field}.")
                continue
            if value2 < value:
                value, value2 = value2, value
        normalized = {"field": field, "operator": operator, "value": value}
        if value2:
            normalized["value2"] = value2
        plan["dates"].append(normalized)

    for item in raw.get("numbers", []) if isinstance(raw.get("numbers", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid numeric condition.")
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip()
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            value = None
        try:
            value2 = float(item.get("value2")) if item.get("value2") is not None else None
        except (TypeError, ValueError):
            value2 = None
        if field not in schema["numbers"] or operator not in NUMBER_OPERATORS or value is None:
            warnings.append(f"Ignored unsupported numeric condition: {field or '?'} {operator or '?'}.")
            continue
        if operator == "between":
            if value2 is None:
                warnings.append(f"Ignored incomplete numeric range for {field}.")
                continue
            if value2 < value:
                value, value2 = value2, value
        normalized = {"field": field, "operator": operator, "value": value}
        if value2 is not None:
            normalized["value2"] = value2
        plan["numbers"].append(normalized)

    for item in raw.get("choices", []) if isinstance(raw.get("choices", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid choice condition.")
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip().lower()
        values_raw = item.get("values")
        if isinstance(values_raw, str):
            values_raw = [values_raw]
        values = [str(v).strip() for v in (values_raw or []) if str(v).strip()]
        if field not in schema["choices"] or operator not in CHOICE_OPERATORS or not values:
            warnings.append(f"Ignored unsupported choice condition: {field or '?'} {operator or '?'}.")
            continue
        plan["choices"].append({"field": field, "operator": operator, "values": values})

    for item in raw.get("booleans", []) if isinstance(raw.get("booleans", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid boolean condition.")
            continue
        field = str(item.get("field") or "").strip()
        value = _to_bool(item.get("value"))
        if field not in schema["booleans"] or value is None:
            warnings.append(f"Ignored unsupported boolean condition: {field or '?'}.")
            continue
        plan["booleans"].append({"field": field, "value": value})

    for item in raw.get("presence", []) if isinstance(raw.get("presence", []), list) else []:
        if not isinstance(item, dict):
            warnings.append("Ignored an invalid presence condition.")
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip().lower()
        if field not in schema["presence"] or operator not in PRESENCE_OPERATORS:
            warnings.append(f"Ignored unsupported presence condition: {field or '?'} {operator or '?'}.")
            continue
        plan["presence"].append({"field": field, "operator": operator})

    return plan, warnings


def _line_items_text(value: Any) -> str:
    if not isinstance(value, list):
        return "" if value is None else str(value)
    chunks: list[str] = []
    for item in value:
        if isinstance(item, dict):
            chunks.extend(str(item.get(key) or "") for key in ("description", "quantity", "unit_price", "amount"))
        else:
            chunks.append(str(item))
    return " ".join(chunk for chunk in chunks if chunk)


def _row_text(row: dict, field: str) -> str:
    value = row.get(field)
    if field == "line_items":
        return _line_items_text(value)
    return "" if value is None else str(value)


def _row_date(row: dict, field: str) -> date | None:
    value = row.get(field)
    if field == "date_uploaded" and not value:
        value = row.get("created_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _row_number(row: dict, field: str) -> float | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_missing_value(row: dict, field: str) -> bool:
    """Field-aware blank detection for natural-language "with/without" searches.

    Text/date placeholders such as NULL, blank, dash, and literal "0" count as
    missing, matching how absent invoice identifiers (for example plate number)
    are commonly stored. Numeric zero is *not* missing: VAT/zero-rated/etc. 0.00
    is a legitimate extracted financial value and must remain searchable as
    present.
    """
    value = row.get(field)

    if field == "line_items":
        return not bool(value)

    if field in INVOICE_NUMBER_FIELDS or field in LOG_NUMBER_FIELDS:
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().casefold() in {"", "-", "—", "–", "n/a", "na", "none", "null"}
        return False

    if field in INVOICE_BOOLEAN_FIELDS or field in LOG_BOOLEAN_FIELDS:
        return value is None

    if value is None:
        return True
    text = str(value).strip().casefold()
    return text in {"", "-", "—", "–", "0", "0.0", "0.00", "n/a", "na", "none", "null"}


def _matches_text(actual: str, operator: str, expected: str) -> bool:
    a = actual.casefold()
    e = expected.casefold()
    if operator == "contains":
        return e in a
    if operator == "equals":
        return a == e
    if operator == "not_contains":
        return e not in a
    if operator == "not_equals":
        return a != e
    if operator == "starts_with":
        return a.startswith(e)
    if operator == "ends_with":
        return a.endswith(e)
    return False


def _matches_date(actual: date | None, cond: dict) -> bool:
    if actual is None:
        return False
    first = date.fromisoformat(cond["value"])
    operator = cond["operator"]
    if operator == "on":
        return actual == first
    if operator == "before":
        return actual < first
    if operator == "on_or_before":
        return actual <= first
    if operator == "after":
        return actual > first
    if operator == "on_or_after":
        return actual >= first
    if operator == "between":
        second = date.fromisoformat(cond["value2"])
        return first <= actual <= second
    return False


def _matches_number(actual: float | None, cond: dict) -> bool:
    if actual is None:
        return False
    expected = float(cond["value"])
    operator = cond["operator"]
    if operator == "=":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    if operator == "between":
        return expected <= actual <= float(cond["value2"])
    return False


def apply_search_plan(rows: list[dict], plan: dict | None, page: str) -> list[dict]:
    """Apply an already-normalized plan in memory. Every condition is ANDed."""
    if not plan:
        return list(rows)

    result: list[dict] = []
    for row in rows:
        ok = True

        for cond in plan.get("text", []):
            if not _matches_text(_row_text(row, cond["field"]), cond["operator"], cond["value"]):
                ok = False
                break
        if not ok:
            continue

        for cond in plan.get("dates", []):
            if not _matches_date(_row_date(row, cond["field"]), cond):
                ok = False
                break
        if not ok:
            continue

        for cond in plan.get("numbers", []):
            if not _matches_number(_row_number(row, cond["field"]), cond):
                ok = False
                break
        if not ok:
            continue

        for cond in plan.get("choices", []):
            actual = _row_text(row, cond["field"]).casefold()
            choices = {str(v).casefold() for v in cond.get("values", [])}
            in_choices = actual in choices
            if (cond["operator"] == "in" and not in_choices) or (
                cond["operator"] == "not_in" and in_choices
            ):
                ok = False
                break
        if not ok:
            continue

        for cond in plan.get("booleans", []):
            if bool(row.get(cond["field"])) is not bool(cond["value"]):
                ok = False
                break
        if not ok:
            continue

        for cond in plan.get("presence", []):
            missing = _is_missing_value(row, cond["field"])
            if (cond["operator"] == "present" and missing) or (
                cond["operator"] == "missing" and not missing
            ):
                ok = False
                break

        if ok:
            result.append(row)

    return result


def _fmt_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%b %d, %Y")
    except ValueError:
        return value


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def summarize_search_plan(plan: dict | None) -> str:
    """Human-readable explanation of the exact deterministic filters applied."""
    if not plan:
        return "No AI filters are active."
    parts: list[str] = []
    for cond in plan.get("text", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        op = {
            "contains": "contains",
            "equals": "is",
            "not_contains": "does not contain",
            "not_equals": "is not",
            "starts_with": "starts with",
            "ends_with": "ends with",
        }[cond["operator"]]
        parts.append(f'{label} {op} “{cond["value"]}”')
    for cond in plan.get("dates", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        op = cond["operator"]
        if op == "between":
            parts.append(f'{label} {_fmt_date(cond["value"])} to {_fmt_date(cond["value2"])}')
        else:
            wording = {
                "on": "on",
                "before": "before",
                "on_or_before": "on or before",
                "after": "after",
                "on_or_after": "on or after",
            }[op]
            parts.append(f'{label} {wording} {_fmt_date(cond["value"])}')
    for cond in plan.get("numbers", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        if cond["operator"] == "between":
            parts.append(
                f'{label} {_fmt_number(cond["value"])} to {_fmt_number(cond["value2"])}'
            )
        else:
            parts.append(f'{label} {cond["operator"]} {_fmt_number(cond["value"])}')
    for cond in plan.get("choices", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        joined = ", ".join(cond.get("values", []))
        wording = "is one of" if cond["operator"] == "in" else "is not any of"
        parts.append(f"{label} {wording} {joined}")
    for cond in plan.get("booleans", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        if cond["field"] == "locked":
            parts.append("Locked invoices" if cond["value"] else "Unlocked invoices")
        else:
            parts.append(f"{label} = {'Yes' if cond['value'] else 'No'}")
    for cond in plan.get("presence", []):
        label = FIELD_LABELS.get(cond["field"], cond["field"])
        parts.append(f"{label} is present" if cond["operator"] == "present" else f"{label} is missing")
    return "; ".join(parts) if parts else "No filtering conditions were recognized."
