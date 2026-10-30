"""Side-by-side party blocks (``FROM:`` | ``SHIP TO:``) read by column.

OCR reading order is top-to-bottom, then left-to-right, so two address blocks
printed next to each other come out interleaved line by line:

    1575 PRINCETON ST.      <- FROM column
    IA KARAMU STREET        <- SHIP TO column
    WACK-WACK VILLAGE       <- FROM column
    ...

Flat-text heuristics then mix both addresses. Here ownership is decided by
geometry: each address line belongs to the label whose left edge it is aligned
with, and only the lines directly under that label are collected.

Only applies when BOTH labels are present on the same row, so ordinary
single-block invoices are untouched.
"""
from __future__ import annotations

import re

_FROM_LABEL = re.compile(r"(?i)^\s*(?:from|sender|shipper|exporter)\s*:?\s*$")
_TO_LABEL = re.compile(
    r"(?i)^\s*(?:ship(?:ped)?\s*to|consignee|deliver(?:ed)?\s*to|bill(?:ed)?\s*to|sold\s*to)\s*:?\s*$"
)
# A contact/identifier label ends an address block.
_BLOCK_END = re.compile(
    r"(?i)\b(?:mobile|phone|tel(?:ephone)?|fax|e-?mail|contact|cell|tin|attn|attention)\b"
)
_TITLE = re.compile(r"(?i)^\s*(?:mr|mrs|ms|miss|mx|dr|atty|engr|sir|madam)\b\.?\s")


def _bounds(box):
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return min(xs), min(ys), max(xs), max(ys)
    except Exception:
        return None


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" ,;|")


def _tokens(text) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(t) >= 3}


def _is_party_name(line: str, party_name) -> bool:
    if _TITLE.match(line + " "):
        return True
    nt, lt = _tokens(party_name), _tokens(line)
    return bool(nt and lt and len(nt & lt) / len(lt) >= 0.6)


def _column_block(label: dict, items: list[dict], page_w: float, line_h: float, party_name) -> list[str]:
    lx1, _, _, ly2 = label["b"]
    tol = max(25.0, page_w * 0.04)
    col = [
        it for it in items
        if it is not label
        and it["cy"] > ly2 - 1
        and abs(it["b"][0] - lx1) <= tol
    ]
    col.sort(key=lambda it: it["cy"])

    lines: list[str] = []
    last_cy = label["cy"]
    for it in col:
        if it["cy"] - last_cy > line_h * 3.0:     # blank gap: block is over
            break
        if _BLOCK_END.search(it["text"]):         # MOBILE NO. / TEL / TIN ...
            break
        if not lines and _is_party_name(it["text"], party_name):
            last_cy = it["cy"]
            continue                              # the addressee's name, not address
        lines.append(it["text"])
        last_cy = it["cy"]
    return lines


def reconcile_from_ship_to_columns(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    """Set vendor_address / customer_address from the FROM and SHIP TO columns."""
    items = []
    for ln in ocr_lines or []:
        b = _bounds(ln.get("bbox"))
        text = _clean(ln.get("text"))
        if b and text:
            items.append({"text": text, "b": b, "cy": (b[1] + b[3]) / 2.0})
    if len(items) < 4:
        return data, []

    froms = [it for it in items if _FROM_LABEL.match(it["text"])]
    tos = [it for it in items if _TO_LABEL.match(it["text"])]
    heights = sorted(it["b"][3] - it["b"][1] for it in items if it["b"][3] > it["b"][1])
    line_h = heights[len(heights) // 2] if heights else 14.0

    pair = None
    for f in froms:
        for t in tos:
            same_row = abs(f["cy"] - t["cy"]) <= max(10.0, line_h)
            apart = abs(f["b"][0] - t["b"][0]) >= 60.0
            if same_row and apart:
                pair = (f, t)
                break
        if pair:
            break
    if not pair:
        return data, []

    page_w = max(it["b"][2] for it in items) or 1.0
    frm, to = pair
    vendor_lines = _column_block(frm, items, page_w, line_h, data.get("vendor_name"))
    buyer_lines = _column_block(to, items, page_w, line_h, data.get("customer_name"))
    # Require a real multi-line block on both sides, else leave the data alone.
    if len(vendor_lines) < 2 or len(buyer_lines) < 2:
        return data, []

    out = dict(data)
    notes: list[str] = []
    for field, lines in (("vendor_address", vendor_lines), ("customer_address", buyer_lines)):
        value = ", ".join(l.rstrip(" ,;") for l in lines)
        if _clean(out.get(field)) != value:
            notes.append(
                f"Side-by-side FROM/SHIP TO layout: {field} rebuilt from its own column "
                f"(was {out.get(field)!r})."
            )
            out[field] = value
    return out, notes


_LEADING_O_NUMBER = re.compile(r"^[Oo](\d{4,})$")


def normalize_letter_o_invoice_number(data: dict) -> tuple[dict, list[str]]:
    """``O0001`` -> ``00001``: a lone leading letter O followed only by digits.

    OCR confuses the zero-padded number's first 0 with the letter O. An invoice
    number made of one 'O' plus 4+ digits is far more likely a zero-padded
    number than a real 'O' prefix, but it is still flagged for review.
    """
    value = str(data.get("invoice_number") or "").strip()
    m = _LEADING_O_NUMBER.match(value)
    if not m:
        return data, []
    out = dict(data, invoice_number="0" + m.group(1))
    return out, [
        f"Invoice number {value!r} starts with the letter O followed only by digits; "
        f"read as zero-padded {out['invoice_number']!r}. Verify against the original."
    ]
