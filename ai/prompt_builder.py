"""Builds prompts sent to the local LLM (via Ollama) for invoice extraction."""
import json
from config.constants import EXTRACTION_SCHEMA
from ai.invoice_templates import get_template
from ai.pre_extraction import pre_extract_hints

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
    over reading the columns literally left-to-right. Also never turn a
    VATABLE SALES / ZERO-RATED SALES / TOTAL SALES column label itself
    into a fake line_item (same rule as #12, for this table's labels
    specifically).
15. A line-item row that quotes a price in a FOREIGN currency will often
    print several money-like numbers side by side: a foreign-currency
    UNIT COST, a rate of exchange (ROE/exchange rate — a number like
    "61.70", NOT a price), and a TOTAL AMOUNT already converted to the
    invoice's own currency (matching its `currency` field). That
    TOTAL AMOUNT column — never the ROE, and never the foreign UNIT
    COST — is this item's `amount`. Also: a booking/ticket/transaction
    reference number printed near that row (e.g. "SERVICE FEE OF BS
    #B0280034", "EBC # 0000328802") is metadata about the item above it,
    never a second line item of its own — do not create a separate
    line_item for it, with or without a number attached.
16. A wide, multi-column BREAKDOWN table (e.g. a professional-fee invoice
    with columns like Description | Fee | Expense | Professional
    Services | Unit Cost | Quantity | Net | Tax | Rate | Tax Amount |
    Total) is frequently OCR'd with its rows and columns out of their
    true alignment — the header row's own column LABELS can end up
    sitting next to a completely unrelated number rather than their real
    data row. If a candidate line_item's `description` is just a bare
    column-header word by itself ("Fee", "Expense", "Professional
    Services", "Description", "Unit Cost", "Net", "Tax", "Total",
    "Amount") rather than an actual item/service name, do NOT create a
    line_item for it — that is the table's own header, not a purchased
    item, even if a number happens to sit next to it in the OCR text.
    The real item is whatever full description appears in the table's
    actual data row (e.g. "Our retainer fee for the month of August
    2026"). Apply the same caution to `customer_name`: on this kind of
    invoice, an unrelated "Nature of Services:"/"Engagement" field
    (describing what KIND of work was done, e.g. "Business Tax
    Services") is never the customer's name — the customer is whoever is
    named after "Bill To:"/"Billed To:", even if the Nature-of-Services
    value happens to be positioned closer to it in the OCR text.
"""


def _format_pre_extraction_hints(hints: dict) -> str:
    """
    Formats pre_extract_hints() output as a prompt section. Deliberately
    worded as candidates to VERIFY, not facts to copy — these come from
    plain label-proximity regex, which can and does pick the wrong
    occurrence of a label or a neighboring column's figure by mistake.
    The instruction to actively cross-check (not just "consider") matters:
    a passively-worded hint risks being copied uncritically the same way
    prompt rules 1-16 above have repeatedly not been enough on their own.
    """
    if not hints:
        return ""
    lines = "\n".join(f"- {field}: {value}" for field, value in hints.items())
    return f"""
Pattern-matched candidates (found automatically by scanning for known label
text near a value — NOT guaranteed correct, and NOT a substitute for
reading the OCR text yourself):
{lines}

For each candidate above, actively check it against the OCR text below
before using it — confirm the label really is followed by that exact
value, and that it isn't actually a different field's value, a neighboring
column's figure, or an unrelated boilerplate number (e.g. a printer's own
accreditation number rather than either party's TIN). Only put a candidate
into your JSON once you've verified it this way; if it looks wrong, use
what you read directly from the OCR text instead.
"""


def build_extraction_prompt(ocr_text: str, template_name: str | None = None) -> str:
    schema_str = json.dumps(EXTRACTION_SCHEMA, indent=2)
    template = get_template(template_name)
    template_instructions = template["prompt"] if template else ""
    hints_block = _format_pre_extraction_hints(pre_extract_hints(ocr_text))
    return f"""{SYSTEM_PROMPT}

{template_instructions}
{hints_block}
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
    """Compact vision cross-check prompt. Keep prompt tokens low because image
    tokens also consume the Ollama context window."""
    fields_str = json.dumps(extracted_fields, separators=(",", ":"), ensure_ascii=False)
    return f"""Read the invoice IMAGE independently and verify these extracted fields:
{fields_str}

Return ONLY JSON in this exact form:
{{"mismatches":[{{"field":"field_name","image_shows":"actual value","note":"short reason"}}]}}

Important financial-reading rules:
- Do NOT assume the supplied extracted values are correct and do NOT make the numbers balance by arithmetic.
- Read each amount from the value visibly attached to its own printed label/row/column.
- Distinguish TOTAL AMOUNT DUE / Amount to Pay from intermediate Amount Due, Total Sales, Less VAT, or Amount Net of VAT.
- A blank Discount, Zero-Rated, VAT-Exempt, or Withholding row is 0 only when that row is visibly blank; never borrow a nearby amount from another column.
- A percentage such as 12% is a tax rate, not a monetary tax_amount.
- For handwritten/poor OCR forms, zoom attention to the lower financial-summary box and read the printed/handwritten digits directly.

Include only fields that are clearly different. Omit correct/unclear fields.
Use only field names present in the input. If all match, return {{"mismatches":[]}}.
"""

