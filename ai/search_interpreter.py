"""Natural-language search interpreter backed by the local Ollama text model.

The model is deliberately used only as a parser. It has no database/tool
access and is never allowed to generate executable SQL. Its JSON output is
allow-listed and normalized by services.search_service before the UI applies
it to in-memory rows.
"""
from __future__ import annotations

import json
import re
from datetime import date

import requests

from config.settings import settings
from services.search_service import normalize_search_plan, search_schema_for
from utils.helpers import safe_json_loads


class SearchInterpretationError(Exception):
    pass


# Deterministic safety net for explicit "with/without <field>" wording. Qwen
# remains the primary interpreter, but these common presence requests should
# never degrade to an empty plan merely because a small local model chose the
# wrong JSON bucket. Longest/more-specific aliases are checked first.
_INVOICE_PRESENCE_ALIASES = {
    "invoice_number": ["invoice number", "invoice #"],
    "vendor_name": ["vendor name", "vendor"],
    "vendor_address": ["vendor address"],
    "vendor_tax_id": ["vendor tin", "vendor tax id", "vendor tax number"],
    "customer_name": ["customer name", "customer"],
    "customer_contact": ["customer contact", "contact person"],
    "customer_address": ["customer address"],
    "customer_tax_id": ["customer tin", "customer tax id", "customer tax number"],
    "plate_number": ["plate number", "plate no", "plate #", "vehicle plate"],
    "original_filename": ["filename", "file name"],
    "source_file": ["source file"],
    "enhanced_image_path": ["enhanced image", "cleaned image"],
    "payment_terms": ["payment terms", "payment term"],
    "ocr_engine_used": ["ocr engine"],
    "raw_text": ["raw ocr text", "ocr text"],
    "vision_notes": ["vision notes", "vision note"],
    "line_items": ["line items", "line item", "items purchased", "purchased items"],
    "invoice_date": ["invoice date"],
    "date_uploaded": ["date uploaded", "upload date", "uploaded date"],
    "due_date": ["due date"],
    "id": ["invoice id"],
    "subtotal": ["vatable sales", "net amount"],
    "tax_amount": ["vat amount", "vat"],
    "tax_rate": ["tax rate", "vat rate"],
    "withholding_tax": ["withholding tax", "wht"],
    "zero_rated_sales": ["zero-rated sales", "zero rated sales"],
    "vat_exempt_sales": ["vat-exempt sales", "vat exempt sales"],
    "total_amount": ["total amount due", "total amount", "total"],
    "current_charges_total": ["current charges total", "current charges"],
    "previous_balance": ["previous balance", "remaining balance"],
    "category": ["category"],
    "status": ["status"],
    "currency": ["currency"],
    "locked": ["locked status", "lock status"],
}


def _augment_explicit_presence(query: str, page: str, plan: dict) -> dict:
    if page.lower() not in {"history", "reports", "invoice", "invoices"}:
        return plan
    lowered = " ".join((query or "").casefold().split())
    if not lowered:
        return plan

    # Collect every explicit presence phrase, then keep the most-specific
    # (longest alias) match when phrases overlap. This prevents generic aliases
    # such as "customer" from also matching "without customer TIN".
    candidates: list[tuple[int, int, int, str, str]] = []
    for field, aliases in _INVOICE_PRESENCE_ALIASES.items():
        for alias in aliases:
            escaped = re.escape(alias.casefold())
            patterns = [
                ("missing", rf"\bwithout\s+(?:a\s+|an\s+|any\s+)?{escaped}\b"),
                ("missing", rf"\bmissing\s+(?:a\s+|an\s+|any\s+)?{escaped}\b"),
                ("missing", rf"\bno\s+{escaped}\b"),
                ("missing", rf"\b{escaped}\s+(?:is\s+)?(?:missing|blank|null|empty)\b"),
                ("present", rf"\bwith\s+(?:a\s+|an\s+|any\s+)?{escaped}\b"),
                ("present", rf"\bhas\s+(?:a\s+|an\s+|any\s+)?{escaped}\b"),
                ("present", rf"\bhaving\s+(?:a\s+|an\s+|any\s+)?{escaped}\b"),
                ("present", rf"\b{escaped}\s+(?:is\s+)?(?:present|filled|available)\b"),
            ]
            for operator, pattern in patterns:
                for match in re.finditer(pattern, lowered):
                    candidates.append((match.start(), match.end(), len(alias), field, operator))

    selected: list[tuple[int, int, int, str, str]] = []
    for candidate in sorted(candidates, key=lambda c: (-c[2], c[0])):
        start_pos, end_pos = candidate[0], candidate[1]
        if any(start_pos < chosen[1] and chosen[0] < end_pos for chosen in selected):
            continue
        selected.append(candidate)

    # Apply selected conditions in textual order. If the same field is stated
    # twice, the later explicit wording wins.
    for _, _, _, field, operator in sorted(selected, key=lambda c: c[0]):
        # Explicit presence wording is authoritative for that field. Remove any
        # model-generated value comparison for the same field so a mistaken
        # "plate_number equals null" condition cannot accidentally AND away
        # every row.
        for bucket in ("text", "dates", "numbers", "choices", "booleans", "presence"):
            plan[bucket] = [c for c in plan.get(bucket, []) if c.get("field") != field]
        plan["presence"].append({"field": field, "operator": operator})
    return plan


def _page_field_guide(page: str) -> str:
    page = page.lower()
    if page in {"history", "reports"}:
        return """
Text fields:
- invoice_number = Invoice #
- vendor_name = Vendor
- vendor_address = Vendor Address
- vendor_tax_id = Vendor TIN
- customer_name = Customer
- customer_contact = Customer Contact
- customer_address = Customer Address
- customer_tax_id = Customer TIN
- plate_number = Plate # / plate number
- original_filename = uploaded filename
- source_file = stored source-file path/name
- enhanced_image_path = enhanced/cropped invoice image
- payment_terms = payment terms
- ocr_engine_used = OCR engine
- raw_text = stored raw OCR text
- vision_notes = vision verification notes
- line_items = Items Purchased / Line Items (description, quantity, unit price, total unit price)
Date fields:
- invoice_date = date printed on the invoice
- date_uploaded = date the invoice was uploaded into this system
- due_date = invoice due date
Numeric fields:
- id = internal invoice ID
- subtotal = Vatable Sales
- tax_amount = VAT
- tax_rate = VAT/tax rate
- withholding_tax = Withholding Tax
- zero_rated_sales = Zero-Rated Sales
- vat_exempt_sales = VAT-Exempt Sales
- total_amount = Total Amount Due / total
- current_charges_total = Current Charges Total
- previous_balance = Previous Balance
Choice fields:
- category
- status
- currency
Boolean fields:
- locked
Presence/absence checks:
- Every field listed above supports present/missing, including plate_number, TINs, addresses, dates, amounts, line_items, category/status/currency, and locked.
""".strip()
    return """
Text fields:
- user = PC/Windows user
- action = activity action (for example UPLOAD, EDIT, DELETE, LOCK, UNLOCK, DOWNLOAD EXPORT)
- entity_type = Type
- details = audit Details text
Date fields:
- date_time = when the activity happened
Numeric fields:
- id = audit-log row ID
- entity_id = affected object/invoice ID
There are no choice, boolean, or presence fields on the Log page.
""".strip()


def _build_prompt(query: str, page: str, context: dict | None = None) -> str:
    today = date.today().isoformat()
    context_json = json.dumps(context or {}, ensure_ascii=False)
    default_date_rule = (
        "If the user says date/dates without saying uploaded, use invoice_date. "
        "Only use date_uploaded when the user explicitly mentions upload/uploaded date."
        if page.lower() in {"history", "reports"}
        else "Date/time/month references on the Log page use date_time."
    )
    return f"""You are a natural-language SEARCH FILTER INTERPRETER for a local invoice application.
Your only job is to convert the user's request into a small JSON filter plan.
DO NOT answer the user conversationally. DO NOT write SQL. DO NOT write Python. DO NOT invent database rows.
Return JSON only.

Today on this PC is {today}.
Page: {page}
{default_date_rule}

Allowed fields for this page:
{_page_field_guide(page)}

Return exactly this JSON shape (arrays may be empty):
{{
  "text": [{{"field": "vendor_name", "operator": "contains", "value": "WATSONS"}}],
  "dates": [{{"field": "invoice_date", "operator": "between", "value": "2026-06-01", "value2": "2026-10-31"}}],
  "numbers": [{{"field": "total_amount", "operator": ">", "value": 10000}}],
  "choices": [{{"field": "category", "operator": "in", "values": ["Transportation"]}}],
  "booleans": [{{"field": "locked", "value": false}}],
  "presence": [{{"field": "plate_number", "operator": "present"}}]
}}

Operators:
- text: contains, equals, not_contains, not_equals, starts_with, ends_with
- dates: on, before, on_or_before, after, on_or_after, between
- numbers: =, !=, >, >=, <, <=, between
- choices: in, not_in
- presence: present, missing

Interpretation rules:
- Multiple requested conditions are AND conditions.
- "above" / "more than" means >. "at least" means >=.
- "below" / "less than" means <. "at most" means <=.
- A numeric or date "between A and B" range is inclusive at both ends.
- Resolve relative dates (today, yesterday, this month, last month) using today's date above.
- If a month name has no year, use the current year.
- "June to October" means June 1 through October 31 of the current year.
- For a whole month, use a between condition covering the first through last calendar day.
- Do not use fields outside the allow-list above.
- Do not infer a category/vendor/customer that the user did not request.
- For History/Reports, requests that ask whether a field EXISTS or is ABSENT must use the presence array.
  Examples: "with plate number" / "has plate number" -> {{"field":"plate_number","operator":"present"}}.
  "without plate number" / "missing plate number" -> {{"field":"plate_number","operator":"missing"}}.
  Apply the same rule to every other invoice field: vendor/customer TIN, addresses, due date, payment terms, amounts, line items, etc.
- A missing text/date field includes null, blank, dash, or placeholder 0. Numeric 0.00 is a valid PRESENT financial value; do not treat numeric zero as missing.
- If the request means "show all" or contains no usable filter, return all six arrays empty.

Known values from the current application (use these only to match spelling/casing when relevant):
{context_json}

User request:
{query.strip()}
"""


def interpret_natural_language_search(
    query: str,
    *,
    page: str,
    context: dict | None = None,
) -> tuple[dict, list[str]]:
    """Ask the local text model for a search plan, then strictly validate it."""
    if not query or not query.strip():
        raise SearchInterpretationError("Enter a search request first.")

    # Validate the page up front so unsupported pages never reach Ollama.
    search_schema_for(page)

    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.SEARCH_LLM_MODEL,
        "prompt": _build_prompt(query, page, context),
        "stream": False,
        "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.0},
    }
    try:
        response = requests.post(url, json=payload, timeout=settings.SEARCH_LLM_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise SearchInterpretationError(
            f"AI Search timed out after {settings.SEARCH_LLM_TIMEOUT_SECONDS} seconds. "
            f"Make sure Ollama and {settings.SEARCH_LLM_MODEL} are running."
        ) from exc
    except requests.RequestException as exc:
        raise SearchInterpretationError(
            f"Could not reach local Ollama AI Search ({settings.SEARCH_LLM_MODEL}): {exc}"
        ) from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise SearchInterpretationError("Ollama returned an invalid response.") from exc

    parsed = safe_json_loads(body.get("response", ""))
    if not isinstance(parsed, dict):
        raise SearchInterpretationError("Qwen did not return a valid JSON search plan. Please rephrase the search.")

    plan, warnings = normalize_search_plan(parsed, page)
    plan = _augment_explicit_presence(query, page, plan)
    return plan, warnings
