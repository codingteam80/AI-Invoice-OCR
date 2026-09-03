"""Watsons-specific extraction rules and deterministic normalization.

The Watsons/PERSONAL CARE STORES layout is sufficiently repeatable that a
vendor-specific template can improve extraction without making the generic
invoice prompt larger or more ambiguous.
"""
from __future__ import annotations

import re
from typing import Any

WATSONS_TEMPLATE_NAME = "watsons"

WATSONS_PROMPT = """WATSONS-SPECIFIC INVOICE FORMAT

This document appears to be a WATSONS / WATSONS PERSONAL CARE STORES invoice.
Apply the following vendor-specific rules. These rules take precedence over
generic field-location guesses when the Watsons labels are visible in the OCR.
Do not invent a value if the relevant printed text is unreadable or absent.

1. invoice_number
   - Use the value stated next to "Sales invoice No.:".
   - Do NOT use a transaction number, terminal number, POS number, or other
     reference number when "Sales invoice No.:" is present.

2. vendor_name
   - The vendor is WATSONS.
   - The document may print "WATSONS PERSONAL CARE STORES". Normalize the
     extracted vendor_name to exactly "watsons" for this application.

3. vendor_address
   - Use the vendor address in the top/header area, normally directly below
     the vendor name.

4. vendor_tax_id
   - Use the value stated next to "VAT REG TIN#" in the top/header area.
   - This is the vendor TIN, not the customer's TIN.

5. customer_name
   - Use the value stated after "Customer Name:".
   - If the field is absent, blank, or unreadable, use null. The UI may display
     a dash for a missing value.

6. customer_address
   - Use the customer's address associated with the Customer Name section,
     normally below that section.
   - If absent or unreadable, use null.

7. customer_tax_id
   - Use the customer's TIN associated with the Customer Name section.
   - Do not use the vendor's "VAT REG TIN#".
   - If absent or unreadable, use null.

8. invoice_date
   - Use the transaction/invoice date in the lower area of the invoice.
   - On this layout it is commonly around two OCR lines below
     "Sales invoice No.:". Use the actual sale date, not a BIR permit or
     accreditation issuance date.

9. subtotal / Net Amount
   - Use the amount in the "VAT SALE" row under the "AMOUNT" column.
   - This maps to the application's `subtotal` / Net Amount field.
   - Do not substitute the total amount or a VAT-inclusive subtotal.

10. zero_rated_sales
    - Use the amount in the "ZERO RATED SALE" row under "AMOUNT".
    - If the row is printed but blank, use null.

11. vat_exempt_sales
    - Use the amount in the "VAT EXEMPT SALE" row under "AMOUNT".
    - If the row is printed but blank, use null.

12. tax_amount / VAT
    - Use the amount in the "VAT SALE" row under the "VAT AMT" column.

13. discount
    - Use the amount stated next to "TOTAL DISCOUNTS".
    - Do not confuse this with line-item prices, VAT, CASH, CHANGE, or
      AMOUNT TO PAY.

14. total_amount
    - Use the amount stated next to "Amount to Pay".
    - This is the final amount owed for the transaction.
    - Never use CASH/TENDERED or CHANGE as total_amount.

15. category
    - Always set category to exactly "Medical & Health Supplies" for this
      Watsons template. Do not return "watsons" as the category.

16. WATSONS LINE ITEM SPECIAL CASE
    - If an item description contains or begins with "185TH POUCH BAG WITH",
      the complete description must be exactly:
      "185TH POUCH BAG WITH SHOPPING BAG"
    - Do NOT use "185TH POUCH BAG WITH PO.01V SHOPPING BAG".
    - Do NOT split "185TH POUCH BAG WITH" and "SHOPPING BAG" into two items.
    - For this specific item, unit_price must be 0.01 and amount must be 0.01.
      Quantity should be 1 unless the invoice clearly prints another quantity.

17. FINANCIAL CONSISTENCY RULES
    - Net Amount (Vatable Sales) = VAT SALE / AMOUNT.
    - Total Amount Due = AMOUNT TO PAY.
    - For this Watsons layout, Net Amount + VAT must equal Total Amount Due.
    - Sum of line-item Total Unit Price - TOTAL DISCOUNTS must equal Amount To Pay.
    - If OCR ordering separates labels from values because the OCR engine read
      the receipt column-by-column, use the Watsons labels AND these arithmetic
      identities to select the correct value. Never use CASH as Total Amount Due.

18. If the OCR text contains both CASH and CHANGE, calculate CASH - CHANGE
    only as a corroborating check. It must not override a clearly printed
    AMOUNT TO PAY value.
"""

WATSONS_VISION_PROMPT = """WATSONS VISUAL VERIFICATION RULES

This image appears to be a WATSONS / WATSONS PERSONAL CARE STORES invoice.
When checking the extracted fields against the image, use the printed labels
and this layout guidance:
- invoice_number: value next to "Sales invoice No.:"
- vendor_name: the top/header business name; normalize to "watsons"
- vendor_tax_id: value next to "VAT REG TIN#"
- vendor_address: top/header address below the vendor
- customer_name/address/customer_tax_id: values in the "Customer Name:"
  section; leave missing values null
- invoice_date: transaction date associated with the sale, not permit metadata
- subtotal: "VAT SALE" row under "AMOUNT"
- zero_rated_sales: "ZERO RATED SALE" row under "AMOUNT"
- vat_exempt_sales: "VAT EXEMPT SALE" row under "AMOUNT"
- tax_amount: "VAT SALE" row under "VAT AMT"
- discount: value next to "TOTAL DISCOUNTS"
- total_amount: value next to "Amount to Pay"; never CASH or CHANGE
- The Watsons financial identity should read Net Amount + VAT = Total Amount Due.
- For a line item beginning "185TH POUCH BAG WITH", the complete printed
  item should be interpreted as "185TH POUCH BAG WITH SHOPPING BAG" and
  its unit price and amount should be 0.01.
"""

_AMOUNT_RE = re.compile(r"(?<!\d)(\d[\d,]*\.\d{2})(?!\d)")
_INVOICE_NO_RE = re.compile(
    r"sales\s*invoice\s*(?:no|number)\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]*)",
    re.IGNORECASE,
)
_VENDOR_TIN_RE = re.compile(
    r"vat\s*reg(?:\.|\s)*\s*tin\s*#?\s*[:.]?\s*([0-9A-Z\-]+)",
    re.IGNORECASE,
)


def _first_amount_near_label(text: str, labels: tuple[str, ...], window: int = 120) -> float | None:
    """Return the first monetary value near a known label.

    OCR can put a label and value on the same line or in adjacent detection
    boxes. This intentionally uses a small character window and never scans
    the entire document, so an unrelated total is not silently selected.
    """
    lower = text.lower()
    for label in labels:
        start = 0
        while True:
            idx = lower.find(label.lower(), start)
            if idx < 0:
                break
            segment = text[idx:idx + len(label) + window]
            match = _AMOUNT_RE.search(segment)
            if match:
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    pass
            start = idx + len(label)
    return None


def _normalize_special_line_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize the known Watsons pouch-bag OCR artifact.

    Also merges a separate `SHOPPING BAG` row when it immediately follows a
    `185TH POUCH BAG WITH` row, preventing the bag suffix from becoming a
    second purchased item.
    """
    normalized: list[dict[str, Any]] = []
    changed = False
    i = 0
    while i < len(items):
        item = dict(items[i] or {})
        desc = str(item.get("description") or "").strip()
        upper = re.sub(r"\s+", " ", desc).upper()

        if upper.startswith("185TH POUCH BAG WITH"):
            item["description"] = "185TH POUCH BAG WITH SHOPPING BAG"
            item["unit_price"] = 0.01
            item["amount"] = 0.01
            if item.get("quantity") in (None, 0, 0.0):
                item["quantity"] = 1.0
            changed = changed or desc != item["description"] or float(item.get("unit_price") or 0) != 0.01
            if i + 1 < len(items):
                next_desc = str((items[i + 1] or {}).get("description") or "").strip()
                if re.fullmatch(r"shopping\s+bag", next_desc, re.IGNORECASE):
                    i += 1
                    changed = True
            normalized.append(item)
        elif re.fullmatch(r"shopping\s+bag", desc, re.IGNORECASE) and normalized:
            # If the previous item is the special pouch item, merge this suffix
            # rather than keeping a separate line.
            prev = normalized[-1]
            if prev.get("description") == "185TH POUCH BAG WITH SHOPPING BAG":
                changed = True
            else:
                normalized.append(item)
        else:
            normalized.append(item)
        i += 1
    return normalized, changed



def _normalized_ocr(text: str) -> str:
    """Normalize common OCR separators without destroying line structure.

    Some OCR engines emit Philippine receipt decimals as ``305,80`` instead
    of ``305.80``. Convert only a comma followed by exactly two digits, so
    thousands separators such as ``1,000.00`` remain untouched.
    """
    text = text or ""
    text = text.replace("₱", "P").replace("＄", "$").replace("\t", " ")
    text = re.sub(r"(?<=\d),(?=\d{2}(?:\D|$))", ".", text)
    # Common receipt OCR mistake: `P0.01` becomes `PO.01`. Correct only the
    # O when it is immediately after a currency marker and before a decimal.
    text = re.sub(r"(?i)(?<=P)O(?=\.\d{2}(?:\D|$))", "0", text)
    return text


def _amounts_from_lines(lines: list[str]) -> list[float]:
    amounts: list[float] = []
    for line in lines:
        for match in _AMOUNT_RE.finditer(line):
            try:
                amounts.append(float(match.group(1).replace(",", "")))
            except ValueError:
                continue
    return amounts


def _watsons_amount_to_pay(text: str, expected_total: float | None = None) -> float | None:
    """Extract the Watsons `Amount To Pay` value without selecting CASH.

    OCR on Watsons receipts can be column-major.  When the literal value is
    separated from its label, the strongest independent check is the BIR tax
    table: VAT SALE amount + VAT equals Amount To Pay on this receipt format.
    CASH/CHANGE is only a fallback corroboration.
    """
    text = _normalized_ocr(text)
    lines = text.splitlines()
    label_re = re.compile(r"amount\s+to\s+pay", re.IGNORECASE)

    same_line: list[float] = []
    for line in lines:
        if label_re.search(line):
            same_line.extend(_amounts_from_lines([line]))

    # A value printed on the SAME OCR line as "Amount To Pay" is the strongest
    # direct evidence and must win over derived arithmetic. This preserves the
    # printed value on layouts where extra charges make net + VAT differ from
    # the final payable amount.
    if same_line:
        return same_line[0]

    # If the label/value pair was separated by column-major OCR, use the
    # Watsons net + VAT identity as the deterministic fallback. This prevents
    # CASH (e.g. 1000.00) from being mistaken for Amount To Pay.
    if expected_total is not None:
        return round(expected_total, 2)

    # Do not blindly take the next line after Amount To Pay: OCR can place CASH
    # immediately after the label while putting the actual amount elsewhere.
    for i, line in enumerate(lines):
        if not label_re.search(line):
            continue
        for j in range(i + 1, min(len(lines), i + 5)):
            lower = lines[j].lower()
            if "cash" in lower or "change" in lower:
                break
            amounts = _amounts_from_lines([lines[j]])
            if amounts:
                return amounts[0]

    cash = _first_amount_near_label(text, ("cash",), window=40)
    change = _first_amount_near_label(text, ("change",), window=40)
    if cash is not None and change is not None:
        derived = round(cash - change, 2)
        if derived > 0 and (expected_total is None or abs(derived - expected_total) <= 0.01):
            return derived
    return None

def _amount_appears(text: str, value: float) -> bool:
    return (
        f"{value:.2f}" in text
        or f"{value:,.2f}" in text
        or f"P{value:.2f}" in text
        or f"P{value:,.2f}" in text
    )


def _watsons_tax_breakdown(text: str, total_amount: float | None = None) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (VAT SALE amount, VAT, VAT-exempt sales, zero-rated sales).

    Watsons BIR tax tables are frequently returned by OCR in either row-major
    order (each row carries its two numbers) or column-major order (all AMOUNT
    values first, then all VAT AMT values).  Supporting both is important
    because proximity-only extraction can attach the first later value (for
    example the CASH amount) to an earlier label.
    """
    text = _normalized_ocr(text)
    lower = text.lower()
    start = lower.find("tax code")
    if start < 0:
        start = lower.find("vat sale")
    if start < 0:
        return None, None, None, None
    end_candidates = [lower.find("cashier name", start), lower.find("sales invoice no", start)]
    ends = [x for x in end_candidates if x >= 0]
    end = min(ends) if ends else len(text)
    section = text[start:end]
    section_lines = section.splitlines()

    rows: dict[str, list[float]] = {}
    row_labels = (
        ("vat exempt sale", "vat-exempt sale"),
        ("zero rated sale", "zero-rated sale"),
        ("vat sale",),
        ("total",),
    )
    for i, line in enumerate(section_lines):
        line_lower = line.lower()
        matched = None
        for labels in row_labels:
            if any(label in line_lower for label in labels):
                matched = labels[0]
                break
        if matched is None:
            continue
        amounts = _amounts_from_lines([line])
        if len(amounts) < 2:
            for j in range(i + 1, min(len(section_lines), i + 4)):
                amounts.extend(_amounts_from_lines([section_lines[j]]))
                if len(amounts) >= 2:
                    break
        if amounts:
            rows[matched] = amounts[:2]

    # Strongest case: VAT SALE itself carries both columns.
    vat_sale = rows.get("vat sale")
    if vat_sale and len(vat_sale) >= 2:
        net, vat = vat_sale[0], vat_sale[1]
    else:
        net = vat = None

    # Column-major fallback. The table order is VAT SALE, VAT EXEMPT SALE,
    # ZERO RATED SALE, TOTAL for each numeric column.
    amounts = _amounts_from_lines(section_lines)
    if (net is None or vat is None) and len(amounts) >= 8:
        first_col = amounts[:4]
        second_col = amounts[4:8]
        candidates = []
        for n in first_col:
            for v in second_col:
                candidates.append((n, v))
        if total_amount is not None:
            for n, v in candidates:
                if abs((n + v) - float(total_amount)) <= 0.01:
                    net, vat = n, v
                    break
        if net is None or vat is None:
            net, vat = first_col[0], second_col[0]

        exempt = first_col[1]
        zero = first_col[2]
    else:
        exempt = rows.get("vat exempt sale", [None])[0]
        zero = rows.get("zero rated sale", [None])[0]

    return net, vat, exempt, zero


def normalize_watsons_extraction(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Apply conservative, deterministic Watsons corrections after LLM extraction.

    The LLM remains the primary extractor. These rules only override values
    when a Watsons-specific label gives a sufficiently strong signal, and
    otherwise leave the LLM result untouched.
    """
    result = dict(data)
    notes: list[str] = []
    text = _normalized_ocr(ocr_text or "")

    # Brand normalization is deterministic once the Watsons template has been
    # selected by the detector.
    if result.get("vendor_name") and str(result["vendor_name"]).strip().lower() != "watsons":
        old = result["vendor_name"]
        result["vendor_name"] = "watsons"
        notes.append(f"Watsons template normalized vendor_name from '{old}' to 'watsons'.")
    elif not result.get("vendor_name"):
        result["vendor_name"] = "watsons"
        notes.append("Watsons template filled vendor_name as 'watsons'.")

    result["category"] = "Medical & Health Supplies"

    # Strongly labeled invoice number and vendor TIN. Only override when the
    # label and value are actually visible in OCR text.
    m = _INVOICE_NO_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if candidate and candidate != str(result.get("invoice_number") or "").strip():
            old = result.get("invoice_number")
            result["invoice_number"] = candidate
            notes.append(f"Watsons template corrected invoice_number from '{old}' to '{candidate}'.")

    m = _VENDOR_TIN_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if candidate and candidate != str(result.get("vendor_tax_id") or "").strip():
            old = result.get("vendor_tax_id")
            result["vendor_tax_id"] = candidate
            notes.append(f"Watsons template corrected vendor_tax_id from '{old}' to '{candidate}'.")

    # Normalize line items BEFORE deriving the discount.  The line-item sum is
    # an independent signal that lets us distinguish the printed TOTAL DISCOUNTS
    # (0.01 on the sample Watsons receipt) from unrelated column-major values
    # such as 305.80.
    items = result.get("line_items") or []
    normalized_items, changed = _normalize_special_line_items(items)
    if changed:
        result["line_items"] = normalized_items
        notes.append("Watsons template normalized the '185TH POUCH BAG WITH' line item to the required full description and price.")
    items = result.get("line_items") or []

    # First recover the Watsons BIR tax table because it gives us the strongest
    # independent source for Net Amount and VAT, and therefore the expected
    # Amount To Pay (= net + VAT for this receipt format).
    tax_table_net, tax_table_vat, tax_table_exempt, tax_table_zero = _watsons_tax_breakdown(
        text, result.get("total_amount")
    )
    expected_total = None
    if tax_table_net is not None and tax_table_vat is not None:
        expected_total = round(tax_table_net + tax_table_vat, 2)

    total_candidate = _watsons_amount_to_pay(text, expected_total=expected_total)
    if total_candidate is not None:
        old = result.get("total_amount")
        if old is None or abs(float(old) - total_candidate) > 0.005:
            result["total_amount"] = total_candidate
            notes.append(f"Watsons template set total_amount to {total_candidate} from Amount To Pay.")

    # The explicit TOTAL DISCOUNTS label is authoritative.  However, when OCR
    # separates labels from values, proximity can return an unrelated tax-table
    # number. If the line-item sum and Amount To Pay are available, their exact
    # difference is a stronger consistency check for the printed discount.
    discount_candidate = _first_amount_near_label(text, ("total discounts", "otal discounts"), window=60)
    line_items_sum = None
    try:
        if items:
            line_items_sum = round(sum(float(li.get("amount") or 0) for li in items), 2)
    except (TypeError, ValueError, AttributeError):
        line_items_sum = None

    total_for_discount = result.get("total_amount")
    derived_discount = None
    if line_items_sum is not None and total_for_discount is not None:
        try:
            candidate = round(line_items_sum - float(total_for_discount), 2)
            if candidate >= 0 and _amount_appears(text, candidate):
                derived_discount = candidate
        except (TypeError, ValueError):
            pass

    if derived_discount is not None:
        # Use the mathematically corroborated discount when it disagrees with a
        # proximity-only OCR candidate. This fixes the Watsons column-major case
        # where `TOTAL DISCOUNTS` can otherwise be paired with 305.80.
        if discount_candidate is None or abs(discount_candidate - derived_discount) > 0.005:
            discount_candidate = derived_discount
            notes.append(f"Watsons template resolved discount to {derived_discount} from line-item total minus Amount To Pay.")

    if discount_candidate is not None:
        old = result.get("discount")
        if old is None or abs(float(old) - discount_candidate) > 0.005:
            result["discount"] = discount_candidate
            notes.append(f"Watsons template set discount to {discount_candidate} from TOTAL DISCOUNTS.")

    if tax_table_net is not None:
        old = result.get("subtotal")
        if old is None or abs(float(old) - tax_table_net) > 0.005:
            result["subtotal"] = tax_table_net
            notes.append(f"Watsons template set subtotal/net amount to {tax_table_net} from VAT SALE / AMOUNT.")
    if tax_table_vat is not None:
        old = result.get("tax_amount")
        if old is None or abs(float(old) - tax_table_vat) > 0.005:
            result["tax_amount"] = tax_table_vat
            notes.append(f"Watsons template set tax_amount/VAT to {tax_table_vat} from VAT SALE / VAT AMT.")
    if tax_table_exempt is not None:
        result["vat_exempt_sales"] = tax_table_exempt
    if tax_table_zero is not None:
        result["zero_rated_sales"] = tax_table_zero

    return result, notes
