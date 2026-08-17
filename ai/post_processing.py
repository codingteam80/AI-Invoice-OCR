"""Cleans and normalizes raw LLM JSON before it's turned into an Invoice model."""
import re
import uuid
from utils.date_utils import to_iso
from parser.currency_parser import normalize_currency, to_float
from parser.tax_parser import find_tax_rate, find_tax_amount
from config.settings import settings

# Chars that handwriting/OCR commonly confuse with digits. Only applied to
# the numeric run of an invoice number (e.g. "INV-00I" -> "INV-001"),
# never to the whole string, so we don't mangle a genuinely alphabetic ID.
_DIGIT_CONFUSION = str.maketrans({"I": "1", "l": "1", "O": "0", "o": "0", "S": "5"})
_ID_NUMERIC_TAIL_RE = re.compile(r"^(?P<prefix>\D*)(?P<tail>[\dIlOoS]+)$")

# Labels used by reconcile_total_amount() to scan raw OCR text for the
# amounts sitting next to each kind of line. "total" deliberately excludes
# "subtotal" lines (see _amounts_near_label's `exclude` handling below).
TOTAL_LABELS = ("grand total", "amount due", "amount payable", "total due", "total")
TOTAL_EXCLUDE = ("subtotal", "sub total", "sub-total")
CASH_LABELS = ("cash", "tendered", "amount tendered")
CHANGE_LABELS = ("change",)

_AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}")


def _amounts_on_line(line: str) -> list[float]:
    out = []
    for match in _AMOUNT_RE.findall(line):
        try:
            out.append(float(match.replace(",", "")))
        except ValueError:
            continue
    return out


def _amounts_near_label(ocr_text: str, labels: tuple, exclude: tuple = ()) -> list[float]:
    """
    Scan OCR text for amounts on lines that mention one of `labels`.

    Receipts are often OCR'd as separate detection boxes per column, so a
    label ("CASH") and its amount ("1,000.00") frequently land on two
    consecutive lines rather than one merged line — hence checking the
    following line too when the label's own line has no amount on it.
    """
    lines = ocr_text.splitlines()
    amounts = []
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(ex in lower for ex in exclude):
            continue
        if any(label in lower for label in labels):
            found = _amounts_on_line(line)
            if not found and i + 1 < len(lines):
                found = _amounts_on_line(lines[i + 1])
            amounts.extend(found)
    return amounts


def post_process(data: dict) -> dict:
    data = dict(data)  # shallow copy

    for date_field in ("invoice_date", "due_date"):
        if data.get(date_field):
            data[date_field] = to_iso(str(data[date_field]))

    for money_field in ("subtotal", "tax_amount", "discount", "total_amount", "tax_rate"):
        if data.get(money_field) is not None:
            data[money_field] = to_float(data[money_field])

    if data.get("currency"):
        data["currency"] = normalize_currency(data["currency"])

    items = data.get("line_items") or []
    cleaned_items = []
    for item in items:
        cleaned_items.append({
            "description": (item.get("description") or "").strip(),
            "quantity": to_float(item.get("quantity", 1)) or 1.0,
            "unit_price": to_float(item.get("unit_price", 0)) or 0.0,
            "amount": to_float(item.get("amount", 0)) or 0.0,
        })
    data["line_items"] = cleaned_items

    for text_field in (
        "vendor_name", "vendor_address", "customer_name", "customer_contact", "invoice_number",
    ):
        if data.get(text_field):
            data[text_field] = str(data[text_field]).strip()

    if data.get("invoice_number"):
        data["invoice_number"] = _normalize_id_digits(data["invoice_number"])

    return data


def _normalize_id_digits(value: str) -> str:
    """
    Invoice numbers like 'INV-001' are almost always digits after the
    prefix, but OCR/handwriting frequently swaps look-alikes (I/1, O/0,
    l/1, S/5). If the tail is ALREADY all-numeric we leave it untouched;
    we only rewrite look-alike characters when the tail is a mix of digits
    and exactly those look-alikes, since a genuinely alphanumeric ID
    (e.g. 'INV-A01') should be left alone.
    """
    match = _ID_NUMERIC_TAIL_RE.match(value)
    if not match:
        return value
    tail = match.group("tail")
    if tail.isdigit() or not any(c.isdigit() for c in tail):
        return value
    fixed_tail = tail.translate(_DIGIT_CONFUSION)
    return match.group("prefix") + fixed_tail


def reconcile_tax(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for missing tax_amount/tax_rate. parser/tax_parser.py already
    implements regex-based tax detection but was never wired into the
    pipeline, so a null/zero tax_amount from the LLM was silently accepted
    even when the OCR text clearly contained tax figures (e.g. '2%', '5%',
    'Tax: 4.80'). Only FILLS gaps — never overwrites a value the LLM did
    provide, since the LLM has more context (e.g. per-line-item tax rates)
    than a flat regex scan can.

    It does, however, still flag disagreements: if the LLM already
    supplied a tax_amount but a clearly-labeled VAT/tax line in the raw
    OCR text scans to a different figure, that's the same "auto-correct
    only when unambiguous, otherwise note and let a human look"
    precedent reconcile_total_amount() follows above — the regex hit is a
    cruder signal than the LLM's, so it doesn't get to silently overwrite,
    but silently dropping the disagreement would hide a real error (e.g.
    the LLM quietly reconciling tax_amount against a misread subtotal so
    the arithmetic balances, even though the receipt's own printed VAT
    line said something else).
    """
    notes = []
    if not ocr_text:
        return data, notes

    data = dict(data)
    if data.get("tax_rate") in (None, 0, 0.0):
        rate = find_tax_rate(ocr_text)
        if rate is not None:
            data["tax_rate"] = rate
            notes.append(f"tax_rate filled from OCR text via regex fallback: {rate}%")

    regex_tax_amount = find_tax_amount(ocr_text)
    if data.get("tax_amount") in (None, 0, 0.0):
        if regex_tax_amount is not None:
            data["tax_amount"] = regex_tax_amount
            notes.append(f"tax_amount filled from OCR text via regex fallback: {regex_tax_amount}")
    elif regex_tax_amount is not None:
        try:
            llm_tax_amount = float(data["tax_amount"])
        except (TypeError, ValueError):
            llm_tax_amount = None
        if llm_tax_amount is not None and abs(llm_tax_amount - regex_tax_amount) > 0.01:
            notes.append(
                f"tax_amount extracted as {llm_tax_amount}, but a regex scan of the OCR "
                f"text found {regex_tax_amount} on a labeled VAT/tax line instead — needs "
                f"manual review."
            )

    return data, notes


def sanitize_for_model(data: dict) -> dict:
    """
    Fill in safe placeholders for fields the Invoice model requires as
    non-optional (invoice_number, vendor_name, subtotal, total_amount,
    currency), so a document the LLM genuinely couldn't fully read (e.g. a
    retail receipt with no labeled "Invoice Number") doesn't crash
    Invoice(**data) with a pydantic validation error.

    IMPORTANT: call this ONLY after validate_extraction()/score_extraction()
    have already run on the un-sanitized data. Those steps are what
    correctly flag the invoice as needs_review / low-confidence because a
    required field was truly missing — if you sanitize first, that signal
    is lost because the placeholder looks like a "present" value.
    """
    data = dict(data)

    if not data.get("invoice_number"):
        data["invoice_number"] = f"UNVERIFIED-{uuid.uuid4().hex[:8]}"
    if not data.get("vendor_name"):
        data["vendor_name"] = "Unknown Vendor"
    if data.get("subtotal") is None:
        data["subtotal"] = 0.0
    if data.get("total_amount") is None:
        data["total_amount"] = 0.0
    if not data.get("currency"):
        data["currency"] = settings.DEFAULT_CURRENCY

    return data


def reconcile_total_amount(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Heuristic backstop for the "LLM picked CASH/CHANGE instead of TOTAL"
    failure mode (a prompt-level instruction already discourages this, see
    ai/prompt_builder.py rule 8 — this catches it if that still gets
    missed). Cross-references the extracted total_amount against labeled
    lines in the raw OCR text:

      - If total_amount matches a CASH/TENDERED or CHANGE line, does NOT
        match any TOTAL line, and exactly one distinct TOTAL value is
        found in the text -> auto-correct total_amount to it.
      - Either way, returns a human-readable note. Callers should fold
        these into the validation issues list so the invoice still gets
        flagged needs_review with reduced confidence — an auto-correction
        is a good guess, not a certainty, and deserves a human glance.

    Returns: (possibly-updated data dict, list of note strings)
    """
    notes = []
    if not ocr_text or data.get("total_amount") is None:
        return data, notes

    try:
        total_f = float(data["total_amount"])
    except (TypeError, ValueError):
        return data, notes

    total_lines = _amounts_near_label(ocr_text, TOTAL_LABELS, exclude=TOTAL_EXCLUDE)
    cash_lines = _amounts_near_label(ocr_text, CASH_LABELS)
    change_lines = _amounts_near_label(ocr_text, CHANGE_LABELS)

    matches_cash = any(abs(total_f - c) < 0.01 for c in cash_lines)
    matches_change = any(abs(total_f - c) < 0.01 for c in change_lines)
    matches_total_line = any(abs(total_f - t) < 0.01 for t in total_lines)

    if (matches_cash or matches_change) and not matches_total_line and total_lines:
        distinct_totals = sorted(set(round(t, 2) for t in total_lines))
        if len(distinct_totals) == 1:
            corrected = distinct_totals[0]
            wrong_kind = "CASH/TENDERED" if matches_cash else "CHANGE"
            notes.append(
                f"total_amount auto-corrected from {total_f} to {corrected}: the original "
                f"value matched a {wrong_kind} line in the OCR text rather than the TOTAL line."
            )
            data = dict(data)
            data["total_amount"] = corrected

            # If subtotal was ALSO set to the same wrong CASH/CHANGE value
            # (common when the LLM anchored both fields on the same
            # misread number), it's now silently wrong even though
            # total_amount just got fixed. We can't safely guess a
            # replacement subtotal here — recomputing it from total - tax
            # is a bigger inferential leap than the total fix above,
            # which had a real matching label to fall back on — so just
            # surface it as a note and let the required-field/needs_review
            # path force a human look rather than pretend it's still fine.
            try:
                if data.get("subtotal") is not None and abs(float(data["subtotal"]) - total_f) < 0.01:
                    notes.append(
                        f"subtotal ({data['subtotal']}) also matched the same {wrong_kind} "
                        f"value as the corrected total_amount — likely wrong too, needs manual review."
                    )
            except (TypeError, ValueError):
                pass
        else:
            notes.append(
                f"total_amount ({total_f}) matches a CASH/TENDERED or CHANGE line rather than "
                f"a TOTAL line, but multiple candidate TOTAL values were found in the OCR text "
                f"({distinct_totals}) — needs manual review."
            )

    return data, notes
