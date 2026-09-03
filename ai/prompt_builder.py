"""Builds prompts sent to the local LLM (via Ollama) for invoice extraction."""
import json
from config.constants import EXTRACTION_SCHEMA
from ai.invoice_templates import get_template

SYSTEM_PROMPT = """You are an expert invoice-data-extraction assistant.
You will be given raw OCR text from a scanned invoice. Extract the requested
fields as accurately as possible. Follow these rules strictly:

1. Respond with ONLY a single valid JSON object — no markdown, no commentary.
2. If a field is not present in the text, use null (or an empty list for line_items).
3. Numbers must be plain numeric values (no currency symbols, no commas).
4. Dates must be formatted as YYYY-MM-DD.
5. currency must be a 3-letter ISO 4217 code (e.g. USD, EUR, PHP). Infer it
   from currency symbols/context if not explicitly stated.
6. line_items is a list of objects: description, quantity, unit_price, amount.
7. Do not hallucinate values that are not supported by the text.
7a. plate_number is ONLY for a vehicle plate explicitly printed on the document.
    Prefer an explicitly labeled "Plate #", "Plate No.", "Plate Number",
    "Plate:", or "Vehicle Plate" value. Never confuse it with a ticket number,
    transaction number, invoice/receipt number, serial number, MIN, terminal ID,
    or other identifier. If no plate is printed, return null.
8. On receipts, several money amounts often appear close together (TOTAL,
   CASH/TENDERED, CHANGE, AMOUNT PAID). total_amount must ALWAYS be the
   TOTAL/AMOUNT DUE/AMOUNT PAYABLE — the actual price of the transaction.
   Never use CASH or TENDERED (what the customer handed over) or CHANGE
   (money handed back) as total_amount, even if that number is larger or
   appears more prominently on the receipt.
9. Line items are frequently OCR'd as SEPARATE lines per item rather than
   one row — a barcode/product-code line, then the item name, then the
   price, e.g.:
       2092500130408
       ATC FISH OIL SOFTGEL
       P225.00
   Associate each code/name/price group correctly: the item's
   `description` is the descriptive TEXT ("ATC FISH OIL SOFTGEL"), never
   a bare product code or price. If you cannot confidently match a real
   description to a price, leave that line item out entirely rather than
   inventing one with a numeric "description" like "2092500130408" or
   "225.00". A line item's `amount` must be that single item's price —
   never the receipt's overall subtotal or total figure.
10. Philippine BIR-compliant receipts print PERMIT/ACCREDITATION METADATA
    near the bottom — things like "PTU:", "ACC:", "Accreditation No.",
    "BIR Authority to Print No.", "DATE ISSUED" (for the permit, not the
    sale), and a company name/TIN attached to that block. This is almost
    always the POS-TERMINAL or RECEIPT-PRINTING SOFTWARE PROVIDER, not the
    actual merchant — e.g. a parking receipt whose real merchant is
    "SM DEVELOPMENT CORPORATION" (labeled "Name:" near the top) may ALSO
    show "CHASE TECHNOLOGIES CORPORATION" further down next to a "PTU:"
    number — that second company is not the vendor. Prefer the business
    name/address block near the TOP of the receipt (or explicitly labeled
    "Name:"/"Business Name:") for vendor_name, not one sitting next to
    permit/accreditation text. Apply the same logic to invoice_date: prefer
    a date near "Date:", "Trans. Date", or a transaction timestamp over a
    "DATE ISSUED" near permit/accreditation text (that's when the permit
    was issued, not when the sale happened) — they are frequently
    different dates on the same receipt.
11. When several ID-like numbers appear (a "Transaction#", a "Terminal#"
    or series code, and an actual "Invoice No."/"Sales Invoice No."/"OR#"/
    "Official Receipt #"), invoice_number must come from the one
    explicitly labeled as the invoice/receipt/OR number — never a
    "Transaction#" or terminal/series code, even if it's printed more
    prominently or closer to the top.
12. Never turn the invoice's own SUBTOTAL, NET AMOUNT, TOTAL, TAX/VAT,
    DISCOUNT, CASH/TENDERED, or CHANGE line into a fake line_item. Those
    values belong ONLY in their own top-level fields (subtotal,
    tax_amount, total_amount, discount) — a summary/payment line is not a
    purchased item, even if OCR text placed it near the item list.
13. If OCR text clearly shows only some of quantity/unit_price/amount for
    a given item, leave the missing one(s) null rather than guessing —
    e.g. don't invent quantity: 1 just to fill the field.
14. Philippine BIR-formatted invoices commonly print a sales breakdown
    with several columns: VATABLE SALES, VAT, ZERO-RATED SALES, and
    VAT-EXEMPT SALES, followed by a separate TOTAL SALES / AMOUNT DUE
    section. Match subtotal to VATABLE SALES specifically (not the
    ZERO-RATED or VAT-EXEMPT figures), and put those two into their own
    zero_rated_sales / vat_exempt_sales fields — never fold them into
    subtotal or leave them un-placed. Note this breakdown table's own
    columns are sometimes misprinted/misaligned by the source document
    itself (labels and values off by one row) — if the numbers don't line
    up with their labels in a way that makes arithmetic sense (e.g.
    VATABLE SALES + VAT should roughly equal the printed subtotal/total
    for that line), prefer the reading that IS arithmetically consistent
    over reading the columns literally left-to-right.
"""


def build_extraction_prompt(ocr_text: str, template_name: str | None = None) -> str:
    schema_str = json.dumps(EXTRACTION_SCHEMA, indent=2)
    template = get_template(template_name)
    template_instructions = template["prompt"] if template else ""
    return f"""{SYSTEM_PROMPT}

{template_instructions}

JSON schema to fill:
{schema_str}

OCR TEXT:
\"\"\"
{ocr_text}
\"\"\"

Return only the JSON object matching the schema above.
"""


def build_correction_prompt(ocr_text: str, previous_json: dict, issues: list[str], template_name: str | None = None) -> str:
    """Used when validation finds problems and we want the LLM to self-correct."""
    template = get_template(template_name)
    template_instructions = template["prompt"] if template else ""
    return f"""{SYSTEM_PROMPT}

{template_instructions}

Your previous extraction had these issues:
{chr(10).join(f"- {i}" for i in issues)}

Previous JSON:
{json.dumps(previous_json, indent=2)}

Original OCR TEXT:
\"\"\"
{ocr_text}
\"\"\"

Fix the issues and return only the corrected JSON object.
"""


def build_vision_verification_prompt(extracted_fields: dict, template_name: str | None = None) -> str:
    """Used by ai.vision_verifier — sent to a vision-capable model ALONGSIDE
    the invoice image itself, unlike build_extraction_prompt/
    build_correction_prompt above which only ever see OCR text.

    This is what lets the pipeline catch OCR misreads: the text-only
    extraction/correction steps can only be internally self-consistent with
    whatever the OCR already produced, they have no independent way to
    notice the OCR itself misread a character. A model that can actually
    see the document does.
    """
    fields_str = json.dumps(extracted_fields, indent=2)
    template = get_template(template_name)
    template_instructions = template.get("vision_prompt", "") if template else ""
    return f"""You are double-checking a handful of values that were already
extracted from this document image by a separate OCR + text-extraction
process. Look ONLY at what is actually printed/written in the IMAGE —
the values below are just a reference point to check against, not
necessarily correct.

Extracted values to verify:
{fields_str}

Respond with ONLY a single valid JSON object — no markdown, no commentary —
in exactly this shape:
{{
  "mismatches": [
    {{"field": "<field name from the list above>", "image_shows": "<what the image actually shows for this field>", "note": "<optional short reason, e.g. digit misread>"}}
  ]
}}

Rules:
1. Only include a field in "mismatches" if you are reasonably confident the
   image shows something DIFFERENT from the extracted value.
2. If a field looks correct, or the image is too unclear/cropped to tell,
   leave it out entirely — do not guess.
3. If every field matches, return {{"mismatches": []}}.
4. Never invent a field that is not in the list above.
"""
