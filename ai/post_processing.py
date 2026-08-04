"""Cleans and normalizes raw LLM JSON before it's turned into an Invoice model."""
import re
import uuid
from utils.date_utils import to_iso
from parser.currency_parser import normalize_currency, to_float
from config.settings import settings
from config.constants import CATEGORY_OPTIONS, DEFAULT_CATEGORY

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


def normalize_category(value) -> str:
    """Snap whatever the LLM returned to one of CATEGORY_OPTIONS.

    Case/whitespace-insensitive exact match against the fixed list; any
    unmatched or missing value falls back to DEFAULT_CATEGORY ("Others")
    rather than letting a stray category name leak into the DB and split
    the History page's category tables.
    """
    if not value:
        return DEFAULT_CATEGORY
    value = str(value).strip()
    for option in CATEGORY_OPTIONS:
        if value.lower() == option.lower():
            return option
    return DEFAULT_CATEGORY


def post_process(data: dict) -> dict:
    data = dict(data)  # shallow copy

    data["category"] = normalize_category(data.get("category"))

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

    for text_field in ("vendor_name", "vendor_address", "customer_name", "invoice_number"):
        if data.get(text_field):
            data[text_field] = str(data[text_field]).strip()

    return data


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
        else:
            notes.append(
                f"total_amount ({total_f}) matches a CASH/TENDERED or CHANGE line rather than "
                f"a TOTAL line, but multiple candidate TOTAL values were found in the OCR text "
                f"({distinct_totals}) — needs manual review."
            )

    return data, notes
