"""SCommerce / Shopee "Invoice (Sale of Goods)" fixes, free of app imports.

* The invoice number is ISSI + 12 digits, and OCR reads the capital I as the digit 1 ("ISS1000010134539").
* The "Date Issued:" box of a REPRINT is often printed BLANK. LLM drafts then invent a date (the Order ID
  "250115GJHVA17C" read as 2025-01-15) and the vision pass "corrects" it to another one (2023-04-02, from the
  Acknowledgement Certificate number "AC-044-02-2023-..."). A printed blank is a blank: with no date text anywhere on
  the page, an extracted invoice_date has no source and is cleared so the record is reviewed instead.
Both rules act only when the seller is SCommerce, so no other document is touched."""
import re
from datetime import date

_DATE_LABEL = re.compile(r"(?i)^(?:invoice\s+)?date\s*issued\s*:?$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def _compact(text) -> str:
    return re.sub(r"[\s,.]+", "", str(text or "").lower())


def _date_in_text(iso: str, text: str) -> bool:
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        date(y, m, d)
    except (ValueError, TypeError):
        return False
    forms = {f"{y}-{m:02d}-{d:02d}", f"{m}/{d}/{y}", f"{m:02d}/{d:02d}/{y}", f"{d}/{m}/{y}", f"{d:02d}/{m:02d}/{y}",
             f"{m:02d}-{d:02d}-{y}", f"{d:02d}-{m:02d}-{y}", f"{_MONTHS[m-1]} {d}, {y}", f"{_MONTHS[m-1][:3]} {d}, {y}",
             f"{d} {_MONTHS[m-1]} {y}", f"{d} {_MONTHS[m-1][:3]} {y}", f"{_MONTHS[m-1]} {d:02d}, {y}"}
    hay = _compact(text)
    return any(_compact(f) in hay for f in forms)


def _all_date_labels_blank(ocr_lines) -> bool:
    """True when at least one 'Date Issued' label exists and none has a value cell on its row."""
    found = False
    for lab in ocr_lines or []:
        if not _DATE_LABEL.match(str(lab.get("text") or "").strip()):
            continue
        lb = lab.get("bbox") or []
        if not lb:
            continue
        found = True
        lx2 = max(p[0] for p in lb)
        lcy = (min(p[1] for p in lb) + max(p[1] for p in lb)) / 2
        for l in ocr_lines:
            b = l.get("bbox") or []
            if not b or l is lab:
                continue
            cy = (min(p[1] for p in b) + max(p[1] for p in b)) / 2
            if min(p[0] for p in b) >= lx2 - 2 and abs(cy - lcy) <= 10 and re.search(r"\d", str(l.get("text") or "")):
                return False
    return found


def normalize_ecommerce_invoice(data: dict, ocr_text: str, ocr_lines: list[dict] | None = None):
    if not re.search(r"(?i)s\s*commerce", str(data.get("vendor_name") or "")):
        return data, []
    out, notes = dict(data), []
    number = str(out.get("invoice_number") or "").strip()
    m = re.fullmatch(r"(?i)ISS[1lI|](\d{12})", number)
    if m and number[:4] != "ISSI":
        out["invoice_number"] = "ISSI" + m.group(1)
        notes.append(f"E-commerce invoice: invoice_number {number!r} -> {out['invoice_number']!r} (the SCommerce prefix is "
                     "ISSI; OCR read the letter I as the digit 1).")
    inv_date = out.get("invoice_date")
    if inv_date and _all_date_labels_blank(ocr_lines) and not _date_in_text(inv_date, ocr_text or ""):
        out["invoice_date"] = None
        notes.append(f"E-commerce invoice: invoice_date {inv_date!r} -> None (the printed 'Date Issued' is blank and no "
                     "date appears anywhere on the page; the value came from an Order ID / certificate number or a guess).")
    return out, notes
