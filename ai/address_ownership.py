"""Keep seller-header OCR within the first printed postal location."""
import re

_CUE = re.compile(
    r"\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|bldg|building|tower|"
    r"floor|city|philippines|barangay|brgy\.?|corner|cor\.?|loop|park|district|ncr|"
    r"taguig|cebu|makati|pasig|mandaluyong|quezon)\b", re.I)
_BUYER = re.compile(
    r"\b(?:bill(?:ed)?\s*to|sold\s*to|ship(?:ped)?\s*to|customer|"
    r"buyer|client\s*(?:name|no\.?|address)|account\s*(?:name|number))\b", re.I)
_CONTACT = re.compile(r"\b(?:tel(?:ephone)?\.?|fax|email|www\.)", re.I)
_COUNTRY_END = re.compile(r"\bPhilippines\b(?:\s*[, ]\s*\d{4})?[.,]*\s*$", re.I)


def seller_header_address(text: str) -> str | None:
    """Stop at buyer labels/contact metadata, not merely skip those labels.

    Once an address reaches its printed country/postcode, later slogans and
    addresses cannot extend it. No vendor names or sample addresses are used.
    """
    parts, seen = [], set()
    for raw in (text or "").splitlines()[:40]:
        line = re.sub(r"\s+", " ", raw).strip(" ,;|")
        if _BUYER.search(line):
            break
        if _CONTACT.search(line):
            if parts:
                break
            continue
        if re.search(r"\b(?:invoice|vat\s*reg|tin)\b", line, re.I):
            continue
        # A country in the company name is not a postal-address line.
        if re.search(r"\b(?:incorporated|inc\.?|corporation|corp\.?)\s*$", line, re.I):
            continue
        if len(line) < 8 or not _CUE.search(line):
            continue
        if line.lower() not in seen:
            parts.append(line)
            seen.add(line.lower())
        if _COUNTRY_END.search(line):
            break
    return ", ".join(parts) or None


def enforce_seller_address_boundary(data: dict, header_text: str) -> tuple[dict, list[str]]:
    """Trim a contaminated suffix only when the same prefix is in the header.

    Preserve richer vision reads if OCR is incomplete. The country endpoint
    must exist in both sources, preceded by a meaningful matching address.
    """
    current = str(data.get("vendor_address") or "").strip()
    evidence = seller_header_address(header_text)
    end = re.search(r"\bPhilippines\b(?:\s*[, ]\s*\d{4})?", current, re.I)
    if not end or not evidence or not _COUNTRY_END.search(evidence):
        return data, []
    prefix = current[:end.end()].strip(" ,;")
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    if len(norm(prefix)) < 25 or norm(prefix) != norm(evidence):
        return data, []
    if current == prefix:
        return data, []
    result = dict(data, vendor_address=prefix)
    return result, ["Vendor address ended at the postal location confirmed in seller-header OCR; unrelated trailing text was excluded."]
