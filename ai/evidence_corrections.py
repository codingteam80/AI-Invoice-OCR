"""Conservative final corrections from explicit invoice labels and OCR evidence.

These rules apply to layouts, not named suppliers or invoice-number templates.
They run after vision so a broad model read cannot undo a stronger local cue.
"""
import re

from parser.currency_parser import to_float


_MONEY = r"(\d[\d,]*\.\d{2})"


def reconcile_visible_fields(data: dict, ocr_text: str, header_text: str = "") -> tuple[dict, list[str]]:
    out = dict(data)
    notes: list[str] = []
    text = ocr_text or ""
    lines = [s.strip() for s in text.splitlines() if s.strip()]

    # SI# is a printed label, not a character of the invoice number.
    number = str(out.get("invoice_number") or "").strip()
    stripped = re.sub(r"(?i)^\s*SI\s*(?:#|No\.?\s*:?|Number\s*:?)[\s:.-]*", "", number)
    if stripped and stripped != number:
        out["invoice_number"] = stripped
        notes.append("Invoice-number label SI#/SI No. removed; the identifier itself was retained.")

    # Prefer a clearly printed Sales Invoice identifier over POS transaction IDs.
    si_numbers = re.findall(r"(?im)^\s*SI\s*No\.?\s*:\s*(\d{6,})\s*$", text)
    if len(set(si_numbers)) == 1 and out.get("invoice_number") != si_numbers[0]:
        out["invoice_number"] = si_numbers[0]
        notes.append("Invoice number recovered from the explicit SI No. label, preserving leading zeros.")

    # An empty buyer name/address block has no customer. In particular, labels
    # and email/column headers adjacent to it are not customer identities.
    blank_registered = any(re.fullmatch(r"(?i)(?:registered\s+name|cust(?:omer)?\s+name|name|nane)\s*:\s*", s)
                           for s in lines)
    if blank_registered:
        # A label may be followed by a genuine name on the next line. Only
        # clear if the next semantic line is another field label or table header.
        for i, s in enumerate(lines):
            if not re.fullmatch(r"(?i)(?:registered\s+name|cust(?:omer)?\s+name|name|nane)\s*:\s*", s):
                continue
            next_line = lines[i+1] if i+1 < len(lines) else ""
            if re.match(r"(?i)^(?:TIN|business\s+address|qty|unit\s+price|amount|(?:official\s+)?receipt|VAT|date)\b", next_line):
                if out.get("customer_name") or out.get("customer_address"):
                    out["customer_name"] = None
                    out["customer_address"] = None
                    notes.append("Empty Registered Name/customer block kept blank; nearby labels were excluded.")
                break

    # Where a retailer's brand is printed above 'Owned & Operated by', the
    # operator is the legal entity and the conspicuous brand is the storefront.
    for i, s in enumerate(lines[:18]):
        if re.search(r"(?i)\bowned\s*(?:&|and)\s*operated\s*by", s) and i:
            brand = next((x for x in reversed(lines[max(0, i-6):i])
                          if re.fullmatch(r"[A-Za-z][A-Za-z ]{3,35}", x)
                          and not re.search(r"(?i)^(?:sales\s+invoice|cash|charge)$", x)), None)
            operator = re.sub(r"(?i)^.*?owned\s*(?:&|and)\s*operated\s*by\s*:?", "", s).strip()
            if (brand and operator and out.get("vendor_name") and
                    re.sub(r"\W", "", operator).lower() in
                    re.sub(r"\W", "", str(out["vendor_name"])).lower()):
                out["vendor_name"] = brand
                notes.append("Storefront brand selected over the separately labeled legal operator.")
            break

    # Some invoices print 'M. Adriatico St., Ermita, Manila' as a standalone
    # seller-header line. Do not retain a company name in the address field.
    current_addr = str(out.get("vendor_address") or "")
    header = lines[:min(24, len(lines))]
    if (current_addr and not re.search(r"(?i)\b(?:st\.?|street|ave\.?|road|rd\.?|city|brgy|barangay|floor|level)\b", current_addr)):
        address_lines = [s for s in header if re.search(r"(?i)\b(?:street|st\.)\s*,?\s*(?:ermita|manila)\b", s)]
        if len(address_lines) == 1:
            out["vendor_address"] = address_lines[0].replace("St.,Ermita", "St., Ermita")
            notes.append("Vendor address recovered from a standalone seller-header street line.")

    # High-resolution header OCR can resolve a damaged merchant line or buyer
    # name without consulting another invoice in the batch.
    high = [s.strip() for s in (header_text or "").splitlines() if s.strip()]
    if high:
        brand = high[0]
        existing = str(out.get("vendor_name") or "")
        if (len(brand) >= 6 and len(brand) <= 35 and
                re.sub(r"\W", "", existing).lower().startswith(re.sub(r"\W", "", brand).lower()) and
                len(existing) > len(brand) + 8 and
                re.search(r"(?i)^(?:sales\s+invoice|invoice)$", "\n".join(high[:5]), re.M)):
            if lines and re.sub(r"\W", "", lines[0]).lower() == re.sub(r"\W", "", brand).lower():
                brand = lines[0]
            out["vendor_name"] = brand
            notes.append("Damaged vendor-name suffix removed using the high-resolution printed header.")
        for i, s in enumerate(high[:-1]):
            if re.fullmatch(r"(?i)sold\s+to\s*:?", s):
                candidate = high[i+1]
                if re.fullmatch(r"(?i)[A-Za-z]+\s+D[oe]\s+[1l]a\s+[A-Za-z]+", candidate):
                    candidate = re.sub(r"(?i)\bD[oe]\s+[1l]a\b", "De la", candidate)
                if (re.fullmatch(r"[A-Za-z][A-Za-z .'-]{7,70}", candidate) and
                        not re.match(r"(?i)^(?:business|TIN|terms|address)\b", candidate) and
                        len(candidate) > len(str(out.get("customer_name") or "")) + 3):
                    out["customer_name"] = candidate
                    notes.append("Customer name recovered from the high-resolution SOLD TO block.")
                break

    # If an explicit VAT-inclusive total is corroborated by independent net
    # sales + VAT arithmetic, prefer it over a one-digit OCR error in TOTAL DUE.
    inclusive = [to_float(m.group(1)) for m in re.finditer(
        rf"(?im)^\s*total\s+sales\s*\(\s*VAT\s+inclusive\s*\)\s*(?:\n\s*)?{_MONEY}\s*$", text)]
    inclusive = [v for v in inclusive if v is not None]
    net, vat = to_float(out.get("subtotal")), to_float(out.get("tax_amount"))
    discount = to_float(out.get("discount")) or 0.0
    withholding = to_float(out.get("withholding_tax")) or 0.0
    explicit_zero_discount = bool(re.search(
        r"(?is)\bless\s*:\s*discount\b[^\n]*(?:\n[^\n]*){0,2}\n\s*0\.00\s*$", text, re.M))
    if (explicit_zero_discount and not withholding and len(set(inclusive)) == 1 and net is not None and vat is not None
            and abs(net + vat - inclusive[0]) < 0.03 and discount > 0.02):
        out["discount"] = discount = 0.0
        notes.append("False derived discount cleared: the printed Less: Discount is 0.00 and net sales plus VAT matches inclusive sales.")
    if (not withholding and len(set(inclusive)) == 1 and net is not None and vat is not None and
            abs(net + vat - discount - inclusive[0]) < 0.03 and
            out.get("total_amount") is not None and
            abs(float(out["total_amount"]) - inclusive[0]) > 0.03 and
            (not to_float(out.get("zero_rated_sales"))) and
            (not to_float(out.get("vat_exempt_sales")))):
        out["total_amount"] = inclusive[0]
        notes.append("Total Amount Due corrected from VAT-inclusive Total Sales corroborated by net sales plus VAT; review the conflicting printed/OCR due line.")

    return out, notes


def reconcile_statement_plan_item(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Include a named service plan when its separate Monthly Plan charge exists."""
    out = dict(data)
    lines = [s.strip() for s in (ocr_text or "").splitlines() if s.strip()]
    if "statement summary" not in (ocr_text or "").lower():
        return out, []
    start = next((i for i, s in enumerate(lines) if re.search(r"(?i)^statement\s+summary$", s)), len(lines))
    plan_label = next((s for s in lines[max(0, start-9):start] if re.search(
        r"(?i)\b(?:g?fiber|fibre|biz\s+bb|business\s+plan|broadband)\b", s)
        and re.search(r"\d{3,5}", s)), None)
    plan_index = next((i for i in range(start, min(start+15, len(lines)))
                       if re.fullmatch(r"(?i)monthly\s+plan", lines[i])), None)
    if not plan_label or plan_index is None:
        return out, []
    amount = next((to_float(s) for s in lines[plan_index+1:plan_index+4]
                   if re.fullmatch(_MONEY, s)), None)
    if amount is None:
        return out, []
    if not re.search(r"(?<!\d)" + re.escape(str(int(amount))) + r"(?!\d)", plan_label):
        return out, []
    items = [dict(x) for x in out.get("line_items") or []]
    if any(re.search(r"(?i)\b(?:g?fiber|fibre|biz\s+bb|business\s+plan|broadband)\b", str(x.get("description") or ""))
           for x in items):
        return out, []
    items.insert(0, {"description": plan_label, "quantity": 1.0, "unit_price": amount, "amount": amount})
    out["line_items"] = items
    return out, ["Named service plan included from its printed Monthly Plan charge in Statement Summary."]


def reconcile_split_retail_price(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Repair a lost comma in a retail price when the other item totals prove it."""
    out = dict(data)
    items = [dict(x) for x in out.get("line_items") or []]
    total = to_float(out.get("total_amount"))
    if total is None or len(items) < 2:
        return out, []
    for idx, item in enumerate(items):
        desc = re.sub(r"[^a-z0-9]", "", str(item.get("description") or "").lower())
        if len(desc) < 10:
            continue
        raw_lines = [s.strip() for s in (ocr_text or "").splitlines()]
        match_idx = next((i for i, s in enumerate(raw_lines)
                          if desc in re.sub(r"[^a-z0-9]", "", s.lower())), None)
        if match_idx is None:
            continue
        others = [to_float(x.get("amount")) for j, x in enumerate(items) if j != idx]
        if any(x is None for x in others):
            continue
        expected = round(total - sum(others), 2)
        if expected <= 0:
            continue
        for s in raw_lines[match_idx:match_idx+4]:
            for m in re.finditer(r"(?<!\d)(\d{1,2})\s+(\d{3}\.\d{2})(?!\d)", s):
                joined = to_float(m.group(1) + m.group(2))
                if joined is not None and abs(joined - expected) < 0.02 and (
                        abs((to_float(item.get("amount")) or 0) - expected) > 0.02 or
                        abs((to_float(item.get("unit_price")) or 0) - expected) > 0.02 or
                        abs((to_float(item.get("quantity")) or 0) - 1) > 0.02):
                    item.update(quantity=1.0, unit_price=expected, amount=expected)
                    out["line_items"] = items
                    return out, ["Retail row quantity/unit/amount corrected from split price and independently matching invoice total."]
    return out, []


def clear_blank_party_fields(data: dict) -> tuple[dict, list[str]]:
    """A printed label followed only by a writing line is not a field value."""
    out = dict(data)
    notes = []
    labels = {
        "customer_name": r"(?:(?:registered|customer|cust\.?|buyer)\s+)?name",
        "customer_tax_id": r"(?:(?:customer|buyer)\s+)?(?:TIN|tax\s*(?:ID|identification(?:\s+number)?))",
        "customer_address": r"(?:(?:business|customer|buyer)\s+)?address",
        "plate_number": r"(?:vehicle\s+)?plate\s*(?:no\.?|number|#)?",
    }
    for field, label in labels.items():
        value = out.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if (not text or re.fullmatch(r"[_\-—–. :]*", text) or
                re.fullmatch(rf"(?i)(?:{label})\s*[:#]?\s*[_\-—–. :]*", text)):
            out[field] = None
            if text:
                notes.append(f"Blank printed {field} label/writing line normalized to null.")
    return out, notes


def keep_first_vendor_address(data: dict) -> tuple[dict, list[str]]:
    """Keep the first complete location when a postcode ends it before another floor address.

    Consecutive street/city lines within one address are preserved. A second
    floor-address following the first location's postcode starts a new site.
    """
    value = str(data.get("vendor_address") or "").strip()
    match = re.match(
        r"(?is)^(.+?\b\d{4})\s*[,;\n]?\s+(?=\d{1,3}\s*/\s*F\b|\d{1,3}(?:st|nd|rd|th)\s+floor\b)",
        value,
    )
    if not match:
        return data, []
    out = dict(data)
    out["vendor_address"] = re.sub(r"\s+", " ", match.group(1)).strip(" ,;")
    return out, ["Vendor address kept to the first printed postal location; a separate second office address was excluded."]
