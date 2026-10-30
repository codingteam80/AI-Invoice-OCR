"""Meralco "ELECTRIC BILL" extraction rules and deterministic normalization.

The Meralco statement layout is stable enough for a vendor template (same
approach as ``watsons.py``).  Everything here is evidence-gated: a value is only
overwritten when it is read from its own printed label/row, and the financial
block is only applied when it satisfies the accounting identity

    VAT Sales + VAT + VAT Zero Rated + Non-VAT == TOTAL ENERGY AMOUNT

so a mis-read never replaces a good value with a worse one.  The rules run after
the LLM (hosted API *or* local Ollama) and after vision verification, so the
result does not depend on which model produced the first draft.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

MERALCO_TEMPLATE_NAME = "meralco"

# Fields this template pins from printed evidence. Used by the pipeline to drop
# vision "corrections" that this template has proven wrong.
MERALCO_PINNED_FIELDS = (
    "invoice_date", "due_date", "customer_tax_id", "subtotal", "tax_amount",
    "vat_exempt_sales", "zero_rated_sales", "current_charges_total", "vendor_tax_id", "total_amount",
)

MERALCO_PROMPT = """MERALCO ELECTRIC-BILL FORMAT

This document is a Meralco "ELECTRIC BILL". Apply these rules; they take
precedence over generic guesses. Never invent a value that is not printed.

- invoice_number: the value after "EB Invoice No." (13 digits).
- invoice_date: the "Bill Date" in the Billing Info box. NOT the Due Date.
- due_date: the "Due Date" in the Billing Info box.
- vendor_name: MERALCO. vendor_address / vendor_tax_id: the business-center block
  in the page header (address lines, Tel. No., "TIN ...-VAT"), NOT the
  addressee block at the top left.
- customer_name / customer_address: the addressee block at the top left.
- customer_tax_id: only a TIN printed after "CUSTOMER TIN:"; null when that label
  is empty. Never use the meter number, route code, or part of the invoice number.
- line_items: one per row of "BREAKDOWN OF ELECTRICITY CHARGES", amount = the
  SUBTOTAL column (not the PERCENTAGE column, and never the numbers on the
  consumption chart axis). Skip rows that are 0.00.
- subtotal: the Base amount of "VAT Sales" in "Energy Bill Amount" (page 2).
- tax_amount: the VAT amount on that same "VAT Sales" row.
- vat_exempt_sales: "Non-VAT". zero_rated_sales: "VAT Zero Rated".
- current_charges_total: "TOTAL ENERGY AMOUNT" / "TOTAL CURRENT BILL AMOUNT".
- previous_balance: "Balance From Previous Billing".
- total_amount: "TOTAL CURRENT BILL AMOUNT" (the current charges, NOT including the previous balance).
- withholding_tax: null (the CWT remark is conditional, not an applied deduction).
- Line items are VAT-inclusive charge groups, so they sum to current_charges_total,
  NOT to the VAT Sales subtotal. Do not change subtotal to make them match.
"""

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")
_MONEY = re.compile(r"^\s*(?:₱|PHP|P)?\s*(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})\s*$", re.I)
_CARRIED = {"remaining balance", "previous balance", "carried balance", "balance forward"}


# ----------------------------------------------------------------- helpers
def _rect(line: dict):
    try:
        pts = line.get("bbox") or []
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return min(xs), min(ys), max(xs), max(ys)
    except Exception:
        return None


def _money(text: str):
    m = _MONEY.match(str(text or ""))
    if not m:
        return None
    return round(float(m.group(1).replace(",", "") + "." + m.group(2)), 2)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _letters(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text or ""))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _geo(lines):
    out = []
    for ln in lines or []:
        r = _rect(ln)
        if r and _clean(ln.get("text")):
            out.append((ln, r, _clean(ln.get("text"))))
    return out


def _find(geo, pattern):
    rx = re.compile(pattern, re.I)
    return next((g for g in geo if rx.match(g[2])), None)


def _row_cells(geo, label, min_gap=0.0):
    """Money cells on the same row, to the right of ``label``, ordered by x."""
    _, (x1, y1, x2, y2), _ = label
    cy, band = (y1 + y2) / 2, max(0.6 * (y2 - y1), 10.0)
    cells = []
    for ln, (cx1, cy1, cx2, cy2), txt in geo:
        if cx1 <= x2 + min_gap or abs((cy1 + cy2) / 2 - cy) > band:
            continue
        v = _money(txt)
        if v is not None:
            cells.append((cx1, v))
    return [v for _, v in sorted(cells)]


def _parse_dmy(text: str):
    m = _DMY.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"


def _labelled_date(ocr_text: str, label: str):
    """Date printed as ``<label>`` followed (same or next line) by ``D Mon YYYY``."""
    lines = [_clean(x) for x in str(ocr_text or "").splitlines()]
    rx = re.compile(rf"(?i)^{label}\s*:?\s*(.*)$")
    for i, line in enumerate(lines):
        m = rx.match(line)
        if not m:
            continue
        for cand in [m.group(1)] + lines[i + 1:i + 3]:
            d = _parse_dmy(cand)
            if d:
                return d
            if cand and not cand.startswith(":") and _letters(cand) > 3:
                break
    return None


# --------------------------------------------------------------- extractors
def _energy_bill_amounts(geo, ocr_text: str) -> dict:
    """VAT Sales (base, VAT), VAT Zero Rated, Non-VAT and TOTAL ENERGY AMOUNT."""
    res: dict[str, Any] = {}
    if geo:
        spec = {
            "vat_sales": r"^VAT\s*Sales$",
            "zero": r"^VAT\s*Zero[\s-]*Rated$",
            "non_vat": r"^Non[\s-]*VAT$",
            "total": r"^TOTAL\s+ENERGY\s+AMOUNT$",
        }
        for key, rx in spec.items():
            label = _find(geo, rx)
            if label:
                cells = _row_cells(geo, label)
                if cells:
                    res[key] = cells
    if "vat_sales" not in res:  # flattened-text fallback
        text = str(ocr_text or "")
        num = r"([\d,]+\.\d{2})"
        m = re.search(rf"(?im)^\s*VAT\s*Sales\s*\n\s*{num}\s*\n\s*{num}", text)
        if m:
            res["vat_sales"] = [float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))]
        for key, label in (("zero", r"VAT\s*Zero[\s-]*Rated"), ("non_vat", r"Non[\s-]*VAT"),
                           ("total", r"TOTAL\s+ENERGY\s+AMOUNT")):
            m = re.search(rf"(?im)^\s*{label}\s*\n\s*{num}", text)
            if m:
                res[key] = [float(m.group(1).replace(",", ""))]
    return res


def _breakdown_items(geo) -> list[dict]:
    """Rows of 'BREAKDOWN OF ELECTRICITY CHARGES' using the SUBTOTAL column."""
    head = _find(geo, r"^BILL\s*SUBGROUP$")
    sub_h = _find(geo, r"^SUBTOTAL$")
    if not head or not sub_h:
        return []
    pct_h = _find(geo, r"^PERCENTAGE$")
    top = head[1][3]
    stop = _find(geo, r"^(?:Please be informed|For authorized collecting agents)")
    bottom = stop[1][1] if stop and stop[1][1] > top else top + 900
    sub_x1 = sub_h[1][0]
    right_limit = pct_h[1][0] if pct_h else sub_x1 + 450
    items = []
    for ln, (x1, y1, x2, y2), txt in geo:
        cy = (y1 + y2) / 2
        if not (top < cy < bottom) or x2 > sub_x1 + 60 or _letters(txt) < 3:
            continue
        if _money(txt) is not None or "%" in txt:
            continue
        band = max(0.7 * (y2 - y1), 12.0)
        amounts = [
            _money(t) for l2, (a1, b1, a2, b2), t in geo
            if a1 > x2 and a2 < right_limit and abs((b1 + b2) / 2 - cy) <= band and _money(t) is not None
        ]
        if amounts and amounts[0] > 0:
            items.append((cy, txt, amounts[0]))
    items.sort()
    return [{"description": d, "quantity": 1.0, "unit_price": a, "amount": a} for _, d, a in items]


def _seller_and_buyer_blocks(geo, customer_name: str):
    """Split the page header into the seller (business-center) block and the
    addressee block using column alignment. Returns (seller_lines, buyer_lines,
    seller_x1) as lists of (text, rect)."""
    bar = _find(geo, r"^ELECTRIC\s*BILL$")
    tel = next((g for g in geo if re.match(r"(?i)^Tel\.?\s*No", g[2])
                and (not bar or g[1][3] < bar[1][1])), None)
    name_key = _norm(customer_name)
    name = next((g for g in geo if name_key and _norm(g[2]) == name_key
                 and (not bar or g[1][3] < bar[1][1])), None)
    if not bar or not tel or not name:
        return [], [], None
    col_x = tel[1][0]
    tol = max(20.0, 0.012 * max(g[1][2] for g in geo))
    seller = [(t, r) for _, r, t in geo
              if abs(r[0] - col_x) <= tol and r[3] <= tel[1][1] + 2 and r[1] >= name[1][1] - 5
              and _letters(t) >= 3 and not re.search(r"(?i)\b(?:tin|tel)\b", t)]
    buyer = [(t, r) for _, r, t in geo
             if abs(r[0] - name[1][0]) <= 60 and r[1] >= name[1][1] - 5 and r[1] < bar[1][1] - 2
             and _norm(t) != name_key and _letters(t) >= 4]
    seller.sort(key=lambda x: x[1][1])
    buyer.sort(key=lambda x: x[1][1])
    return seller, buyer, col_x


def _strip_overlap(text: str, seller_segments: list[str]) -> str:
    """OCR sometimes fuses the end of a seller line into a neighbouring buyer line
    ("...AYALA-ALABANG" + "PRIME ST." -> "...AYALA-ALABANGRIME ST."). Remove a
    trailing run that is the (possibly first-letter-clipped) tail of a seller line."""
    for seg in seller_segments:
        s = _clean(seg)
        for k in (0, 1):
            tail = s[k:]
            if len(_norm(tail)) >= 5 and _clean(text).lower().endswith(tail.lower()) and len(text) > len(tail) + 5:
                return _clean(text)[: len(_clean(text)) - len(tail)].rstrip(" ,;")
    return text


def _snap_spelling(text: str, reference_words: list[str]) -> str:
    """Replace a near-miss word (OCR typo) with the same word read cleanly elsewhere
    in the header of the same bill (e.g. MONTINLOPA -> MUNTINLUPA)."""
    words = text.split(" ")
    for i, w in enumerate(words):
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) < 8:
            continue
        for ref in reference_words:
            rc = re.sub(r"[^A-Za-z]", "", ref)
            if rc.lower() != core.lower() and len(rc) == len(core) \
                    and SequenceMatcher(None, core.lower(), rc.lower()).ratio() >= 0.8:
                words[i] = w.replace(core, rc.upper() if core.isupper() else rc)
                break
    return " ".join(words)


# ---------------------------------------------------------------- the rule
def normalize_meralco_extraction(
    data: dict, ocr_text: str, ocr_lines: list[dict] | None = None, hints: dict | None = None,
) -> tuple[dict, list[str]]:
    out = dict(data)
    notes: list[str] = []
    geo = _geo(ocr_lines)
    hints = hints or {}

    def setf(field, value, why):
        old = out.get(field)
        same = (abs(float(old) - float(value)) < 0.005) if isinstance(value, float) and old is not None \
            and not isinstance(old, str) else old == value
        if not same:
            out[field] = value
            notes.append(f"Meralco template: {field} {old!r} -> {value!r} ({why}).")

    text = str(ocr_text or "")

    m = re.search(r"(?i)EB\s*Invoice\s*(?:No\.?|Number)\s*:?\s*(\d{10,})", text)
    if m:
        setf("invoice_number", m.group(1), "printed EB Invoice No.")

    bill = _labelled_date(text, r"Bill\s*Date")
    if bill:
        setf("invoice_date", bill, "printed Bill Date, not the Due Date")
    due = _labelled_date(text, r"Due\s*Date")
    if due:
        setf("due_date", due, "printed Due Date")

    # ---- financial block (identity-gated)
    amt = _energy_bill_amounts(geo, text)
    vs = amt.get("vat_sales") or []
    if len(vs) >= 2 and amt.get("total"):
        base, vat = vs[0], vs[1]
        zero = (amt.get("zero") or [0.0])[0]
        non_vat = (amt.get("non_vat") or [0.0])[0]
        total = amt["total"][-1]
        if abs(base + vat + zero + non_vat - total) <= 0.05:
            setf("subtotal", float(base), "VAT Sales base")
            setf("tax_amount", float(vat), "VAT on VAT Sales")
            setf("zero_rated_sales", float(zero), "VAT Zero Rated")
            setf("vat_exempt_sales", float(non_vat), "Non-VAT")
            setf("current_charges_total", float(total), "TOTAL ENERGY AMOUNT")
            # QA ground truth: Total_Amount is the current bill (TOTAL CURRENT BILL AMOUNT).
            # The carried balance stays in previous_balance and is not a line item.
            setf("total_amount", float(total), "current bill amount; carried balance kept in previous_balance")
            kept = [li for li in out.get("line_items") or []
                    if _clean((li or {}).get("description")).lower() not in _CARRIED]
            if len(kept) != len(out.get("line_items") or []):
                out["line_items"] = kept
                notes.append("Meralco template: carried-balance display row removed from line items (kept in previous_balance).")
        else:
            notes.append("Meralco template: Energy Bill Amount block did not reconcile to TOTAL ENERGY AMOUNT; financial fields left unchanged.")

    # ---- line items from the charges table (must reconcile to current charges)
    cct = out.get("current_charges_total")
    rows = _breakdown_items(geo)
    if rows and cct is not None and abs(sum(r["amount"] for r in rows) - float(cct)) <= 0.05:
        current = [li for li in out.get("line_items") or []
                   if _clean((li or {}).get("description")).lower() not in _CARRIED]
        carried = [li for li in out.get("line_items") or []
                   if _clean((li or {}).get("description")).lower() in _CARRIED]
        key = lambda items: sorted((_norm(i.get("description")), round(float(i.get("amount") or 0), 2)) for i in items)
        if key(current) != key(rows):
            out["line_items"] = rows + carried
            notes.append("Meralco template: line items rebuilt from the SUBTOTAL column of the charges table (sum equals current charges).")

    # ---- customer TIN: only what is printed after the label
    if _find(geo, r"^CUSTOMER\s*TIN\s*:?$") or re.search(r"(?i)CUSTOMER\s*TIN", text):
        label = _find(geo, r"^CUSTOMER\s*TIN\s*:?$")
        tin = None
        tin_rx = re.compile(r"\b\d{3}[- ]?\d{3}[- ]?\d{3}(?:[- ]?\d{3,5})?\b")
        if label:
            _, (x1, y1, x2, y2), _t = label
            cy, band = (y1 + y2) / 2, max(0.6 * (y2 - y1), 10.0)
            for _l, (a1, b1, a2, b2), t in sorted(geo, key=lambda g: g[1][0]):
                if a1 > x2 and abs((b1 + b2) / 2 - cy) <= band and a1 < x2 + 500 and tin_rx.fullmatch(t.strip()):
                    tin = t.strip()
                    break
        else:
            mm = re.search(r"(?i)CUSTOMER\s*TIN\s*:?[ \t]*(\d{3}[- ]?\d{3}[- ]?\d{3}(?:[- ]?\d{3,5})?)", text)
            tin = mm.group(1) if mm else None
        if tin is None and out.get("customer_tax_id"):
            setf("customer_tax_id", None, "CUSTOMER TIN label is empty on the bill")
        elif tin and not out.get("customer_tax_id"):
            setf("customer_tax_id", tin, "TIN printed after CUSTOMER TIN")

    # ---- seller vs addressee blocks
    seller, buyer, col_x = _seller_and_buyer_blocks(geo, out.get("customer_name") or "")
    if seller and buyer:
        vendor_segments = [t for t, _ in seller]
        hint = hints.get("vendor_header_address")
        if hint:
            segs = [s.strip(" ,;") for s in re.split(r"[\r\n]+", str(hint)) if s.strip(" ,;")]
            if segs and not re.search(r"(?i)\b(?:tel|tin)\b", " ".join(segs)):
                vendor_segments = segs if len(segs) > 1 else [str(hint).strip()]
        setf("vendor_address", ", ".join(vendor_segments), "seller business-center block")

        ref_words = [w for t, _ in seller for w in t.split()]
        parts = []
        for t, r in buyer:
            if col_x is not None and r[2] > col_x - 10:  # line fused with the seller column
                t = _strip_overlap(t, vendor_segments)
            parts.append(_snap_spelling(_clean(t), ref_words))
        if len(parts) >= 2:
            setf("customer_address", ", ".join(parts), "addressee block (seller column excluded)")

    return out, notes


# =====================================================================================================
# "Your electric bill" layout (2022+): no "ELECTRIC BILL" bar, no "Tel. No.", no Energy Bill Amount block.
#
#   ELPIDIO NAPOLES ALMA JR            ROOSEVELT BUSINESS CENTER   <- seller (Meralco business center)
#   0011 (UP) KAURAYA ST               QUEZON
#   TATALON                            Q. C.-KAMUNING
#   Q. C.-NEW MANILA METRO MANILA      TIN 000-101-528-000-VAT     <- seller TIN, NOT the customer's
#   ICP MTR- @ NR COR MAZARAGA         Billing Statement No.: 3353055189919
#   Your electric bill
#   Billing Period    Bill Date ...    (values sit UNDER their labels; OCR text order interleaves them)
#
# The two header blocks share rows, so flat OCR text mixes them and a model typically gives the seller's
# block and TIN to the customer. Ownership here comes from column geometry only.
# =====================================================================================================
_NEW_HEADING = r"^Your\s+electric\s+bill$"
_SELLER_TIN_LINE = re.compile(r"(?i)^TIN\s*[:.]?\s*(\d{3})[- ]?(\d{3})[- ]?(\d{3})[- ]?(\d{3,5})(?:[- ]?VAT)?$")
_METER_BLOCK = re.compile(r"(?i)^(?:ICP\b|meter\s*no|route\s*seq|print\s*seq|billing\s+st)")


def is_new_layout(ocr_lines) -> bool:
    geo = _geo(ocr_lines)
    heading = _find(geo, _NEW_HEADING)
    return bool(heading and any(_SELLER_TIN_LINE.match(g[2]) and g[1][3] < heading[1][1] for g in geo))


def _date_under_label(geo, label_pattern: str):
    """The ``D Mon YYYY`` printed directly UNDER its label (OCR text order puts the other column's
    value first, e.g. the billing-period start). A ``X to Y`` period is never a single date."""
    label = _find(geo, label_pattern)
    if not label:
        return None
    _, (lx1, ly1, lx2, ly2), _t = label
    best = None
    for _l, (x1, y1, x2, y2), t in geo:
        if " to " in t.lower() or not _parse_dmy(t) or y1 < ly2 - 3 or y1 > ly2 + 55:
            continue
        if x2 < lx1 - 5 or x1 > lx2 + 40:          # must sit in the label's column
            continue
        d = abs(x1 - lx1) + (y1 - ly2)
        if best is None or d < best[0]:
            best = (d, _parse_dmy(t))
    return best[1] if best else None


_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def _impossible_numeric_date_under_label(geo, label_pattern: str):
    """The raw ``a/b/YYYY`` printed under a label when it is not a real calendar date either as D/M or as M/D
    ("31/11/2021", "31/02/2021"), else None. Such a value cannot be read, only flagged: a model that "repairs"
    it (31 Nov -> 30 Nov) is inventing a date."""
    import calendar
    label = _find(geo, label_pattern)
    if not label:
        return None
    _, (lx1, ly1, lx2, ly2), _t = label
    for _l, (x1, y1, x2, y2), t in geo:
        m = _NUMERIC_DATE.match(t)
        if not m or y1 < ly2 - 3 or y1 > ly2 + 55 or x2 < lx1 - 5 or x1 > lx2 + 40:
            continue
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        def real(day, month):
            return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(y, month)[1]
        if not (real(a, b) or real(b, a)):
            return m.group(0).strip()
    return None


def normalize_meralco_new_layout(data: dict, ocr_text: str, ocr_lines: list[dict] | None):
    """Parties, TINs and dates of the 'Your electric bill' layout, from geometry. No-op elsewhere."""
    geo = _geo(ocr_lines)
    heading = _find(geo, _NEW_HEADING)
    tin_line = next((g for g in geo if heading and _SELLER_TIN_LINE.match(g[2]) and g[1][3] < heading[1][1]), None)
    if not heading or not tin_line:
        return data, []
    out, notes = dict(data), []

    def setf(field, value, why):
        old = out.get(field)
        if old != value:
            out[field] = value
            notes.append(f"Meralco 'Your electric bill' layout: {field} {old!r} -> {value!r} ({why}).")

    page_w = max(g[1][2] for g in geo)
    col_x = tin_line[1][0]
    tol = max(20.0, 0.012 * page_w)
    name_key = _norm(out.get("customer_name"))
    name = next((g for g in geo if name_key and _norm(g[2]) == name_key and g[1][3] < heading[1][1]), None)
    if not name:
        return data, []
    top = name[1][1] - 5

    # seller = the column of the TIN line, above it
    seller = sorted(((t, r) for _, r, t in geo
                     if abs(r[0] - col_x) <= tol and top <= r[1] and r[3] <= tin_line[1][1] + 2 and r[3] < heading[1][1]
                     and _letters(t) >= 3 and not _SELLER_TIN_LINE.match(t)), key=lambda x: x[1][1])
    # addressee = the column of the name, below it, until the meter/route block or a blank gap
    pitch_rows = sorted(r[1] for _, r, t in geo if abs(r[0] - name[1][0]) <= 60 and name[1][1] <= r[1] < heading[1][1])
    buyer, last_y2 = [], name[1][3]
    line_h = max(8.0, name[1][3] - name[1][1])
    for t, r in sorted(((t, r) for _, r, t in geo
                        if abs(r[0] - name[1][0]) <= 60 and r[1] > name[1][1] + 2 and r[3] < heading[1][1] - 2
                        and _norm(t) != name_key and _letters(t) >= 4), key=lambda x: x[1][1]):
        if _METER_BLOCK.match(t) or r[1] - last_y2 > 1.5 * line_h:
            break
        buyer.append(t); last_y2 = r[3]

    if len(seller) >= 2:
        setf("vendor_address", ", ".join(_clean(t) for t, _ in seller), "business-center block in the TIN column")
    m = _SELLER_TIN_LINE.match(tin_line[2])
    vendor_tin = "-".join(m.groups()) if m else None
    if vendor_tin:
        setf("vendor_tax_id", vendor_tin, "TIN printed in the seller header column")
        cust_digits = re.sub(r"\D", "", str(out.get("customer_tax_id") or ""))
        if cust_digits and cust_digits[:9] == re.sub(r"\D", "", vendor_tin)[:9] and not re.search(r"(?i)CUSTOMER\s*TIN", ocr_text or ""):
            setf("customer_tax_id", None, "that TIN is the seller's; this layout prints no customer TIN")
    if len(buyer) >= 2:
        setf("customer_address", ", ".join(_clean(t) for t in buyer), "addressee column under the name (seller column excluded)")

    bill = _date_under_label(geo, r"^Bill\s*Date$")
    invalid_bill = _impossible_numeric_date_under_label(geo, r"^Bill\s*Date$")
    if invalid_bill:
        # Even when earlier processing already cleared invoice_date, retain
        # the precise reason (not merely "required field missing"). Neither
        # a billing period nor a due date can substitute for this Bill Date.
        setf("invoice_date", None, f"printed Bill Date {invalid_bill!r} is not a real calendar date")
        notes.append(
            f"Invoice date verification required: the printed Bill Date {invalid_bill!r} "
            "is not a valid calendar date; verify it against the original invoice. "
            "Do not use Billing Period or Due Date as Invoice Date."
        )
    elif bill:
        setf("invoice_date", bill, "Bill Date printed under its label, not the billing-period start")
    due = _date_under_label(geo, r"^Due\s*Date$")
    if due:
        setf("due_date", due, "Due Date printed under its label")
    elif out.get("due_date"):
        bad = _impossible_numeric_date_under_label(geo, r"^Due\s*Date$")
        if bad:
            setf("due_date", None, f"the printed Due Date {bad!r} is not a real calendar date; left blank for review")
    return out, notes


# =====================================================================================================
# "Billing Invoice" layout (photo / short bill): a "Billing Invoice No. ..." header, a "VAT REG. TIN"
# line, a boxed "Please Pay" amount and a "Bill Computation Summary" whose rows are
#   <charge label> ........ <amount>      (Generation, Transmission, ..., Other Charges)
# The summary has no VAT breakdown and no invoice date (only a Due Date), and it is usually a small,
# slightly tilted photo, so OCR commonly (a) drops the decimal point ("6592" for 65.92), (b) misreads a
# whole amount, and (c) shifts rows by a few pixels. Amounts are therefore bound to labels by row
# geometry and checked against the printed "Charges for this billing period" total.
# =====================================================================================================
_BI_HEADING = r"^Billing\s*Invoice$"
_BI_SUMMARY = r"^Bill\s*Computation\s*Summary$"
_BI_CHARGES = r"^Charges\s+for\s+this\s+billing\s+period$"
_BI_TOTAL = r"^Total\s+Amount\s+Due$"
_BI_AMOUNT = re.compile(r"^\s*(?:₱|PHP|P)?\s*(-)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?\s*$", re.I)
_BI_LABELS = (
    "Generation", "Transmission", "System Loss", "Distribution (Meralco)", "Subsidies", "Senior Citizen",
    "Government Taxes", "Universal Charges", "FIT-All (Renewable)", "GEA-All (Renewable)", "Lifeline",
    "Other Charges", "Applied Credits",
)
_PARTY_LABEL = re.compile(r"(?i)account\s*number|^\W*CAN\W*$|\(CAN\)")
_BI_VENDOR_ADDRESS = "LOPEZ BLDG., ORTIGAS AVENUE, PASIG CITY"


def is_billing_invoice_layout(ocr_lines) -> bool:
    """True for the 'Billing Invoice' / 'Bill Computation Summary' layout (not the 'Your electric bill' one)."""
    geo = _geo(ocr_lines)
    return bool(_find(geo, _BI_HEADING) and _find(geo, _BI_SUMMARY) and _find(geo, _BI_CHARGES)
                and not _find(geo, _NEW_HEADING) and any("meralco" in g[2].lower() for g in geo))


def _bi_amount(text: str):
    """(value, had_decimal) for an amount cell, or None."""
    m = _BI_AMOUNT.match(str(text or ""))
    if not m:
        return None
    whole = m.group(2).replace(",", "")
    if m.group(3) is None and len(whole) < 2:
        return None
    val = float(f"{whole}.{m.group(3)}") if m.group(3) is not None else float(whole)
    return (-val if m.group(1) else val), m.group(3) is not None


def _bi_canonical(label: str) -> str:
    key = _norm(label)
    best, score = None, 0.0
    for cand in _BI_LABELS:
        r = SequenceMatcher(None, key, _norm(cand)).ratio()
        if r > score:
            best, score = cand, r
    return best if best and score >= 0.8 else _clean(label)


def _bi_vendor_tin(geo):
    """TIN from the 'VAT REG. TIN' line. OCR letters that stand for digits (D/O/Q -> 0, I/l -> 1) are mapped."""
    for _l, _r, txt in geo:
        m = re.match(r"(?i)^VAT\s*RE[CG][.\s:]*(?:TIN[.\s:]*)?(.*)$", txt)
        if not m:
            continue
        rest = re.sub(r"^[^0-9DOQIl]+", "", m.group(1).strip())
        digits = re.sub(r"\D", "", rest.translate(str.maketrans("DOQIl", "00011")))
        if len(digits) == 14:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]}-{digits[9:]}"
        if len(digits) == 12:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]}-{digits[9:]}"
    return None


def _bi_rows(geo):
    """[(label_text, amount_text, amount_value, had_decimal, label_conf_y)] between the header and total rows."""
    charges, total = _find(geo, _BI_CHARGES), _find(geo, _BI_TOTAL)
    if not charges or not total:
        return None, None, []
    top, bottom = charges[1][3] - 2, total[1][1] + 2
    band = [g for g in geo if top < (g[1][1] + g[1][3]) / 2 < bottom and g is not charges]
    cells = [(g, _bi_amount(g[2])) for g in band]
    cells = [(g, a) for g, a in cells if a is not None]
    if not cells:
        return None, None, []
    col_x = sorted(g[1][0] for g, _ in cells)[len(cells) // 2]
    labels = [g for g in band if _bi_amount(g[2]) is None and _letters(g[2]) >= 3 and g[1][2] <= col_x + 2]
    pairs = []
    for li, lab in enumerate(labels):
        lcy, lh = (lab[1][1] + lab[1][3]) / 2, max(lab[1][3] - lab[1][1], 8.0)
        for ci, (cell, _a) in enumerate(cells):
            if cell[1][0] < lab[1][2]:
                continue
            dy = abs((cell[1][1] + cell[1][3]) / 2 - lcy)
            if dy <= 0.9 * lh + 4:
                pairs.append((dy, li, ci))
    used_l, used_c, rows = set(), set(), []
    for dy, li, ci in sorted(pairs):
        if li in used_l or ci in used_c:
            continue
        used_l.add(li); used_c.add(ci)
        cell, (val, dec) = cells[ci]
        rows.append((labels[li][1][1], labels[li][2], cell[2], val, dec, cell[0].get("confidence") or 0.0))
    rows.sort()
    header_amt = None
    for ln, (x1, y1, x2, y2), txt in geo:
        a = _bi_amount(txt)
        if a and a[1] and abs((y1 + y2) / 2 - (charges[1][1] + charges[1][3]) / 2) <= 8 and x1 > charges[1][2]:
            header_amt = a[0]
    if header_amt is None:
        cells_t = _row_cells(geo, total)
        header_amt = cells_t[0] if cells_t else None
    return header_amt, total, [(lab, val, dec, conf) for _y, lab, _t, val, dec, conf in rows]


def _bi_solve(rows, target):
    """Repair decimal-loss / misread amounts so the rows sum to the printed charges total.

    An amount printed with two decimals that arrives without any ("6592") lost its decimal point, so it is
    value/100. If the rows still do not add up, exactly one remaining suspect (the lowest-confidence one) is
    taken as the arithmetic residual. Anything else returns None (caller leaves the data untouched)."""
    vals = [v for _l, v, _d, _c in rows]
    suspects = [i for i, (_l, _v, dec, _c) in enumerate(rows) if not dec]
    fixed, notes = list(vals), {}
    for i in suspects:
        fixed[i] = round(vals[i] / 100.0, 2)
        notes[i] = "decimal point restored"
    if target is None:
        return fixed, notes
    if abs(sum(fixed) - target) <= 0.011:
        return fixed, notes
    for i in sorted(suspects, key=lambda k: rows[k][3]):          # lowest OCR confidence first
        rest = sum(fixed) - fixed[i]
        resid = round(target - rest, 2)
        if resid > 0 and abs(resid - fixed[i]) > 0.011:
            trial = list(fixed)
            trial[i] = resid
            if abs(sum(trial) - target) <= 0.011:
                notes[i] = f"OCR read {rows[i][1]:g}; value recovered from the printed charges total"
                return trial, notes
    return None, {}


def normalize_meralco_billing_invoice(data: dict, ocr_text: str, ocr_lines: list[dict] | None):
    """Fields of the Meralco 'Billing Invoice' layout from row geometry + the printed charges total. No-op elsewhere."""
    if not ocr_lines or not is_billing_invoice_layout(ocr_lines):
        return data, []
    geo = _geo(ocr_lines)
    out, notes = dict(data), []

    def setf(field, value, why):
        old = out.get(field)
        if old != value:
            out[field] = value
            notes.append(f"Meralco 'Billing Invoice' layout: {field} {old!r} -> {value!r} ({why}).")

    # Vendor TIN: the LLM guesses digit order from a garbled OCR line; the line itself is the evidence.
    tin = _bi_vendor_tin(geo)
    if tin and re.sub(r"\D", "", str(out.get("vendor_tax_id") or ""))[:9] != re.sub(r"\D", "", tin)[:9]:
        setf("vendor_tax_id", tin, "read from the 'VAT REG. TIN' line with OCR letter/digit confusions mapped")

    # Vendor address: only repaired when the OCR text is clearly a garbling of the printed header address.
    cur = str(out.get("vendor_address") or "")
    if cur and cur != _BI_VENDOR_ADDRESS and SequenceMatcher(None, _norm(cur), _norm(_BI_VENDOR_ADDRESS)).ratio() >= 0.7:
        setf("vendor_address", _BI_VENDOR_ADDRESS, "OCR text is a garbled form of the printed header address")

    # The account-number label is not a customer; the charge labels are not an address.
    if _PARTY_LABEL.search(str(out.get("customer_name") or "")):
        setf("customer_name", None, "the value is the 'Customer Account Number (CAN)' label; the name is not printed")
    addr = str(out.get("customer_address") or "")
    if addr and sum(1 for lab in _BI_LABELS if _norm(lab)[:8] in _norm(addr)) >= 2:
        setf("customer_address", None, "the value is a list of charge labels from the Bill Computation Summary")

    # Charges table.
    target, _total_label, rows = _bi_rows(geo)
    if rows:
        solved = _bi_solve(rows, target)
        values, why = solved if solved[0] is not None else (None, {})
        if values is not None:
            items = [{"description": _bi_canonical(lab), "quantity": 1.0, "unit_price": v, "amount": v}
                     for (lab, _v, _d, _c), v in zip(rows, values)]
            if out.get("line_items") != items:
                out["line_items"] = items
                detail = "; ".join(f"{_bi_canonical(rows[i][0])}: {w}" for i, w in sorted(why.items()))
                notes.append("Meralco 'Billing Invoice' layout: line_items rebuilt from row geometry and checked "
                             f"against the printed charges total {target}" + (f" ({detail})." if detail else "."))
        else:
            notes.append("Meralco 'Billing Invoice' layout: charge rows do not add up to the printed total even after "
                         "decimal repair; line_items left unchanged - review against the source.")
    if target:
        if not out.get("current_charges_total"):
            setf("current_charges_total", target, "printed 'Charges for this billing period'")
        if not out.get("total_amount"):
            setf("total_amount", target, "printed 'Charges for this billing period'")
    if not out.get("due_date"):
        d = _labelled_date(ocr_text, r"Due\s*Date")
        if d:
            setf("due_date", d, "printed Due Date")
    return out, notes


# A Meralco bill's first page often prints NO VAT breakdown (only "Government Taxes" inside the charge groups); the
# VAT Sales / VAT lines live on the details page. The LLM then copies the printed total into `subtotal`, which the UI
# shows as "Vatable Sales" next to "VAT 0.00": a VAT base that the document never states.
_VAT_LABEL = re.compile(r"(?i)vat\s*(?:sales|amount|zero)|vatable|value[\s-]*added|total\s+energy\s+amount")


def has_vat_breakdown(ocr_text: str) -> bool:
    return bool(_VAT_LABEL.search(str(ocr_text or "")))


def clear_unprinted_vat_breakdown(data: dict, ocr_text: str):
    """subtotal -> None when it only repeats the printed total/current charges and no VAT line exists on the page."""
    if not re.search(r"(?i)meralco", str(ocr_text or "")) or has_vat_breakdown(ocr_text):
        return data, []
    sub = data.get("subtotal")
    if sub is None or data.get("tax_amount") not in (None, 0, 0.0):
        return data, []
    try:
        refs = [float(x) for x in (data.get("total_amount"), data.get("current_charges_total")) if x is not None]
        if not any(abs(float(sub) - r) <= 0.05 for r in refs):
            return data, []
    except (TypeError, ValueError):
        return data, []
    out = dict(data, subtotal=None)
    return out, [f"Meralco bill: subtotal {sub!r} -> None (it only repeats the printed total; this page prints no VAT "
                 "Sales / VAT line, so a vatable base and VAT amount are not stated)."]


def meralco_page_without_vat(data: dict, ocr_text: str | None = None) -> bool:
    """True for a Meralco document whose text prints no VAT Sales / VAT line. Meralco is recognised from the printed text
    OR from vendor_name, so it still works when a stored record is re-validated without its OCR text."""
    if not re.search(r"(?i)meralco|manila\s+electric", f"{data.get('vendor_name') or ''} {ocr_text or ''}"):
        return False
    return not (ocr_text and has_vat_breakdown(ocr_text))


def subtotal_is_unprinted_default(data: dict, ocr_text: str | None = None) -> bool:
    """A stored subtotal of exactly 0.0 on such a page is the model's placeholder for 'not printed' (the Invoice model
    cannot hold None, so sanitize_for_model writes 0.0), not a read value. Re-validation must not treat it as one."""
    sub = data.get("subtotal")
    try:
        if sub is None or abs(float(sub)) > 0.005:
            return False
        ref = data.get("current_charges_total") if data.get("current_charges_total") is not None else data.get("total_amount")
        if ref is None or float(ref) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return data.get("tax_amount") in (None, 0, 0.0) and meralco_page_without_vat(data, ocr_text)
