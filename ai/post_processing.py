"""Cleans and normalizes raw LLM JSON before it's turned into an Invoice model."""
import re
import uuid
from utils.date_utils import to_iso, parse_date
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


# Labels reconcile_subtotal() scans for to find the TRUE net-of-VAT amount.
# Deliberately does NOT include "subtotal" itself — on many PH BIR receipts
# the printed "SUBTOTAL" line is VAT-INCLUSIVE (effectively a second total),
# not the net-of-VAT figure our `subtotal` field is meant to represent.
# That mix-up is exactly the bug this function exists to catch, so
# searching for "subtotal" would just find the same wrong number again.
NET_LABELS = ("net of vat", "amount net of vat", "vatable sales", "vat sale", "net sales")


def _amount_appears_in_text(ocr_text: str, value: float) -> bool:
    """Whether `value` shows up verbatim anywhere in the OCR text (comma-
    formatted or not) — used to corroborate a mathematically-derived
    candidate against something the receipt actually printed, rather than
    trusting pure arithmetic on its own."""
    return f"{value:.2f}" in ocr_text or f"{value:,.2f}" in ocr_text


def _plausible_subtotal(candidate: float, total_amount) -> bool:
    """A net-of-VAT subtotal should be a substantial, positive fraction of
    the total. Filters out two confirmed failure modes found by testing
    against real receipts:
      - Proximity search matching an unrelated zero-valued row that
        happens to sit next to the label (e.g. "VATable Sales" immediately
        followed by a DIFFERENT row's "0.00", not its own value).
      - A derived (total - tax) candidate that's nonsense because total or
        tax themselves aren't reliable for this invoice's structure (e.g. a
        courier invoice with itemized fees instead of a conventional
        subtotal/tax/total breakdown).
    PH VAT is 12%, so a genuine net amount should be roughly 85-90% of a
    VAT-inclusive total; the 50% floor here is deliberately generous rather
    than tuned tightly to that, so it only rejects clearly-implausible
    candidates, not just unusual tax rates.
    """
    if candidate <= 0:
        return False
    if total_amount is None:
        return True
    try:
        total_f = float(total_amount)
    except (TypeError, ValueError):
        return True
    if total_f <= 0:
        return True
    return total_f * 0.5 <= candidate <= total_f * 1.01


def _repeated_plausible_values(ocr_text: str, total_amount) -> set:
    """
    Signal 3 for reconcile_subtotal(): values that appear 2+ times verbatim
    in the OCR text. On BIR-compliant receipts, the true net-of-VAT
    subtotal is very often printed TWICE — once inline near the visible
    running total, and again in a separate tax-breakdown summary further
    down the receipt (confirmed on a real Watsons receipt: 305.80 appeared
    at two widely-separated points in the OCR text, 6 lines apart in one
    spot). Unlike signal 1 (_amounts_near_label), this doesn't require the
    label and its value to be anywhere near each other — it only needs the
    number to occur more than once, which survives the same column-major
    OCR reordering that breaks proximity search on these receipts.

    Filtered through _plausible_subtotal() same as the other signals (this
    is what keeps a repeated individual line-item price, e.g. two menu
    items that happen to cost the same, from being mistaken for the
    subtotal — such a price is well under half of the total and gets
    rejected there). Also explicitly excludes anything equal to
    total_amount, since the total itself commonly repeats too (e.g. once
    near the items, again on an "AMOUNT DUE" line) and a repeated TOTAL is
    not evidence of a *different*, smaller net-of-VAT figure.
    """
    counts: dict[float, int] = {}
    for match in _AMOUNT_RE.findall(ocr_text):
        try:
            val = round(float(match.replace(",", "")), 2)
        except ValueError:
            continue
        counts[val] = counts.get(val, 0) + 1

    total_f = None
    if total_amount is not None:
        try:
            total_f = float(total_amount)
        except (TypeError, ValueError):
            pass

    return {
        val for val, count in counts.items()
        if count >= 2
        and _plausible_subtotal(val, total_amount)
        and (total_f is None or abs(val - total_f) >= 0.01)
    }


def reconcile_subtotal(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for `subtotal` getting set to a VAT-INCLUSIVE figure (the
    receipt's own printed "SUBTOTAL" / "Total Sales (VAT Inclusive)" line)
    instead of the true net-of-VAT amount. Confirmed recurring failure mode
    across multiple different receipt formats/vendors — e.g. a Watsons
    receipt where "SUBTOTAL: 342.50" (VAT-inclusive) got extracted as
    subtotal, when the true net figure (305.80) was sitting in a separate
    "VAT SALE" tax-breakdown line further down. Notably, the vision
    cross-check step made the exact same mistake on that receipt — this
    isn't just a text-extraction gap the vision step can be relied on to
    catch, hence a dedicated regex-based reconciliation here, matching the
    same pattern as reconcile_total_amount()/reconcile_tax() above.

    Uses THREE independent signals, since testing against real receipts
    showed proximity search alone isn't enough here:
      1. A net-of-VAT-labeled line near the label itself (_amounts_near_label,
         same technique as reconcile_total_amount/reconcile_tax). Works when
         OCR happened to keep the label and value close together.
      2. A value MATHEMATICALLY DERIVED as total_amount - tax_amount +
         discount (the same identity ai.validator checks), but only trusted
         if that exact number also appears verbatim somewhere else in the
         OCR text. This exists because signal 1 alone reliably fails on BIR
         tax-breakdown tables: PaddleOCR frequently emits that table in
         column-major order (every row LABEL first, then every VALUE tens
         of lines later) — confirmed on a real Watsons receipt where "VAT
         SALE" and its value "305.80" were 23 lines apart, far further than
         any reasonably-safe proximity window could bridge without risking
         false matches on other receipts.
      3. A value that appears 2+ times verbatim anywhere in the OCR text
         (_repeated_plausible_values) — added after testing showed signals
         1 and 2 can BOTH fail together on the same invoice: if total_amount
         or tax_amount are themselves already wrong at this point in the
         pipeline (their own reconciliation runs before this), signal 2's
         "derived" math is poisoned by that upstream error, and signal 1
         still can't bridge the same column-major OCR gap described above.
         Repetition doesn't depend on either of those — it only needs the
         true subtotal to have been printed more than once, which is common
         on BIR receipts (once inline, again in the tax-breakdown summary).

    Both of the first two signals' candidates are filtered through
    _plausible_subtotal() before being trusted — also found necessary by
    testing against real receipts: signal 1 can match an unrelated zero
    sitting next to the label, and signal 2 can produce nonsense on invoice
    types (e.g. courier/freight) where total/tax don't follow the usual
    retail-receipt subtotal+tax=total shape. Signal 3 uses the same filter
    for the same reason (see _repeated_plausible_values docstring).

    Unlike reconcile_tax() (fill gaps only), this DOES overwrite an
    already-present subtotal — the bug is that subtotal always looks
    "present", just wrong, so a fill-only-if-missing check would never
    trigger. Still only auto-corrects when exactly one distinct plausible
    candidate is found and it disagrees with the extracted subtotal — same
    "unambiguous or don't touch it" precedent as reconcile_total_amount().
    Otherwise just notes the disagreement.
    """
    notes = []
    if not ocr_text:
        return data, notes

    subtotal_was_missing = data.get("subtotal") is None
    if subtotal_was_missing:
        subtotal_f = None
    else:
        try:
            subtotal_f = float(data["subtotal"])
        except (TypeError, ValueError):
            return data, notes

    total_amount = data.get("total_amount")
    tax_amount, discount = data.get("tax_amount") or 0, data.get("discount") or 0

    net_amounts = _amounts_near_label(ocr_text, NET_LABELS)
    candidates = {round(n, 2) for n in net_amounts if _plausible_subtotal(n, total_amount)}

    if total_amount is not None:
        try:
            derived = round(float(total_amount) - float(tax_amount) + float(discount), 2)
            if _plausible_subtotal(derived, total_amount) and _amount_appears_in_text(ocr_text, derived):
                candidates.add(derived)
        except (TypeError, ValueError):
            pass

    candidates |= _repeated_plausible_values(ocr_text, total_amount)

    if not candidates:
        return data, notes

    distinct_candidates = sorted(candidates)

    # subtotal was present and already matches a candidate -> nothing to do.
    if not subtotal_was_missing and any(abs(subtotal_f - c) < 0.01 for c in distinct_candidates):
        return data, notes

    if len(distinct_candidates) == 1:
        corrected = distinct_candidates[0]
        if subtotal_was_missing:
            # FILL, not correct — the field was null going in, not wrong.
            # Confirmed on a real CGD Medical Depot receipt: subtotal was
            # entirely missing (LLM left it null), total_amount=1,500.00
            # and tax_amount=160.71 were both correctly extracted, and
            # derived = 1,500.00 - 160.71 = 1,339.29 — a figure that also
            # appears verbatim elsewhere in the OCR text (PaddleOCR's
            # column-major read of the tax-breakdown table put it right
            # after the CASH line, positionally separated from its own
            # "Non-Taxable Sales" label but still present in the text).
            # Before this, a null subtotal short-circuited this whole
            # function at the top (see the old `data.get("subtotal") is
            # None: return` guard) — so a MISSING value never even got a
            # chance at signals 2/3 below, only a WRONG one did. The
            # ai/validator.py check added earlier correctly flags a null
            # subtotal for needs_review, but flagging isn't fixing; this
            # fills it in when the evidence is unambiguous, same
            # unambiguous-or-don't-touch-it bar as the correction path.
            notes.append(
                f"subtotal filled in as {corrected}: it was missing from the extraction, but "
                f"{corrected} was either found on a net-of-VAT-labeled line (e.g. 'VATable "
                f"Sales' / 'Net of VAT') or derived as total - tax + discount and confirmed to "
                f"appear elsewhere in the OCR text."
            )
        else:
            notes.append(
                f"subtotal auto-corrected from {subtotal_f} to {corrected}: the original value "
                f"didn't match a net-of-VAT-labeled line, but {corrected} was either found on one "
                f"(e.g. 'VATable Sales' / 'Net of VAT') or derived as total - tax + discount and "
                f"confirmed to appear elsewhere in the OCR text."
            )
        data = dict(data)
        data["subtotal"] = corrected
    else:
        subject = "is missing" if subtotal_was_missing else f"({subtotal_f})"
        notes.append(
            f"subtotal {subject} doesn't match any net-of-VAT-labeled line or the "
            f"total-minus-tax figure in the OCR text, but multiple candidates were found "
            f"({distinct_candidates}) — needs manual review."
        )

    return data, notes


_LETTERS_RE = re.compile(r"[A-Za-z]")
_DIGITS_RE = re.compile(r"[0-9]")
_SERIAL_PREFIX_RE = re.compile(r"^\s*S\s*/?\s*N\s*[:#.]?\s*", re.IGNORECASE)

# A real product description is mostly letters, with digits limited to a
# model number here and there (e.g. 'UGEE S1060W10 WIRELESS PEN' is 26%
# digits, 'ACER NITRO VG271U M3BMIIPX2' is 21%). A serial number or
# barcode — even one with several letters mixed in, like
# 'SNUG8P10116122402436' (5 letters) or 'W2M0KC00478544L' (5 letters) —
# runs 65-100% digits. Confirmed on real Datablitz/PCworth receipts where
# the letter-count-only check below (< 3 letters) let these slip through
# and get kept as a second, bogus line item.
_SERIAL_DIGIT_RATIO_THRESHOLD = 0.5


def _looks_like_bare_number_or_code(description: str) -> bool:
    """True for descriptions like '5250.00', '2092500130408',
    '37,390.00-VA', or a serial/barcode string like
    'SNUG8P10116122402436' — a real product description always contains
    actual words with a normal letter-to-digit ratio; a barcode, price, or
    serial number doesn't.

    Three independent signals, any one of which is disqualifying:
      1. Explicit 'S/N:' (or 'SN:', 'Serial No.') prefix — unambiguous.
      2. Fewer than 3 letters total (catches pure numbers/prices like
         '5250.00' — the '-VA'/'V' VATable-sale suffix OCR sometimes tacks
         onto a price is why the threshold is 3, not 1).
      3. Digits make up >=50% of the alphanumeric characters — catches
         serial numbers that happen to contain a handful of letters (see
         module comment above) without misflagging real model numbers
         like 'VG271U', which stay embedded in a mostly-letter description.
    """
    text = description or ""
    if _SERIAL_PREFIX_RE.match(text):
        return True

    letters = len(_LETTERS_RE.findall(text))
    if letters < 3:
        return True

    digits = len(_DIGITS_RE.findall(text))
    alnum = letters + digits
    if alnum > 0 and (digits / alnum) >= _SERIAL_DIGIT_RATIO_THRESHOLD:
        return True

    return False


def clean_line_items(data: dict) -> tuple[dict, list[str]]:
    """
    Backstop for the recurring "line item description is actually a bare
    price or product code" failure mode (a prompt-level instruction
    already discourages this — see ai/prompt_builder.py rule 9 — this
    catches it when that still gets missed). Confirmed on multiple real
    receipts across different vendors/layouts (Watson1, Datablitz1,
    SMstore1, PCworth1), where a line item's "description" was literally a
    bare price or barcode (e.g. "5250.00", "2092500130408",
    "37,390.00-VA") instead of the actual product name — usually because
    the item's code/name/price were OCR'd as three separate lines (per
    rule 9) and the LLM lost track of which group they belonged to.

    Drops any such line item rather than keeping it with a bogus
    description — a fabricated placeholder would be worse than no line
    item at all, and dropping items changes the line-items sum, so
    ai.validator's existing sum-vs-subtotal check still flags the invoice
    for review either way; this doesn't create a false sense of confidence.
    """
    notes = []
    items = data.get("line_items") or []
    if not items:
        return data, notes

    kept, dropped_descriptions = [], []
    for item in items:
        description = item.get("description") or ""
        if _looks_like_bare_number_or_code(description):
            dropped_descriptions.append(description)
        else:
            kept.append(item)

    if dropped_descriptions:
        data = dict(data)
        data["line_items"] = kept
        notes.append(
            f"Dropped {len(dropped_descriptions)} line item(s) whose description was just a "
            f"price or product code rather than an actual item name (e.g. "
            f"{dropped_descriptions[:3]!r}) — the OCR/LLM likely lost track of which "
            f"code/name/price group they belonged to. Add the real item(s) back manually if needed."
        )
    return data, notes


_VENDOR_BOILERPLATE_MARKERS = (
    "ptu", "acc:", "accr#", "accreditation", "valid until", "bir permit",
    "permit no", "pos provider", "machine identification",
)

# Label words that appear in a block ABOVE their actual values on some
# receipts (same column-major OCR ordering issue as the BIR tax table —
# see reconcile_subtotal). Used to skip past the label cluster itself when
# hunting for the value that follows an explicit "Name" label.
_VENDOR_LABEL_WORDS = {"name", "address", "business style", "tin", "pos provider", "vat reg tin"}


def _is_label_like_line(line: str) -> bool:
    stripped = line.strip().rstrip(":.").lower()
    return stripped in _VENDOR_LABEL_WORDS or len(stripped) < 2


_VENDOR_CASHIER_MARKERS = ("cashier", "served by", "serviced by", "staff", "teller", "processed by")
_VENDOR_BUSINESS_SUFFIXES = (
    "inc", "corp", "corporation", "co.", "ltd", "llc", "enterprises",
    "trading", "depot", "restaurant", "pharmacy", "supermarket", "solutions",
    "group", "company",
)


def _first_business_suffix_line(lines: list[str], limit: int = 15) -> str | None:
    """First line within the first `limit` lines that contains a business-
    entity keyword (see _VENDOR_BUSINESS_SUFFIXES) — used to find the real
    merchant near the top of the receipt (business headers are almost
    always in the first handful of lines), without depending on an exact
    "operated by" phrase surviving OCR garbling intact (it often doesn't —
    e.g. a real receipt had "OPERATED BY" OCR'd as "OPERAIED BYCGI", which
    a literal "operated\\s+by" regex would never match)."""
    for line in lines[:limit]:
        lower = line.lower()
        if any(suf in lower for suf in _VENDOR_BUSINESS_SUFFIXES):
            return line.strip()
    return None


_DATE_CANDIDATE_RE = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"        # 11/18/2025, 06-08-2022
    r"|\b\d{1,2}[A-Za-z]{3,9}\d{4}\b"              # 16AUG2026 (no separators)
    r"|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b"   # Nov 5 2025, Sep 09, 2012
)

# "sued" is a deliberate fuzzy variant of "issued" — confirmed on a real
# receipt where OCR dropped the leading "IS" ("ISSUED:06-08-2022" ->
# "SUED:06-08-2022"), which a literal "issued" substring check would miss.
_DATE_BOILERPLATE_MARKERS = (
    "issued", "sued", "valid until", "ptu", "permit", "expiry", "expires", "accreditation",
)


def reconcile_invoice_date(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for invoice_date being wrong in either of two confirmed ways:
      1. A permit/accreditation "Date Issued" got extracted instead of the
         actual transaction date (confirmed on two different real
         receipts, from two different vendors — a medical-supplies receipt
         where the extracted date was a BIR permit's issue date instead of
         the visit date printed at the top, and a 7-Eleven receipt where it
         was the store's BIR accreditation issue date instead of the
         07/22/2026 transaction timestamp also printed on the same
         receipt).
      2. A plain digit transcription error by the LLM on an otherwise
         correctly-OCR'd date — confirmed on a Watsons receipt where the
         OCR text clearly read "16AUG2026" but the LLM's own output was
         one day off ("...-17"). Since the OCR text itself was right here,
         this needed independently re-parsing dates straight from the OCR
         text rather than trusting the LLM's transcription of it — the
         same reasoning as reconcile_subtotal's signal 3.

    Scans every line of the OCR text for date-like substrings, parses each
    with utils.date_utils.parse_date, and drops any whose line sits within
    one line of a permit/accreditation marker (_DATE_BOILERPLATE_MARKERS)
    — same technique as reconcile_vendor_name's boilerplate check. Only
    auto-corrects when exactly one distinct candidate date survives that
    filter and it disagrees with the extracted date; if zero or multiple
    candidates survive, leaves invoice_date alone rather than guess among
    several plausible dates on the same receipt (permit dates, delivery
    dates, etc. are all valid-looking dates, just not the right one).
    """
    notes = []
    if not ocr_text:
        return data, notes

    current = data.get("invoice_date")  # already an ISO string by this point (see post_process)
    lines = ocr_text.splitlines()

    survivors: set[str] = set()
    for i, line in enumerate(lines):
        for m in _DATE_CANDIDATE_RE.finditer(line):
            parsed = parse_date(m.group())
            if not parsed:
                continue
            window = " ".join(lines[max(0, i - 1): i + 2]).lower()
            if not any(marker in window for marker in _DATE_BOILERPLATE_MARKERS):
                survivors.add(parsed.isoformat())

    if len(survivors) != 1:
        return data, notes  # none found, or several equally-plausible candidates — don't guess

    candidate = next(iter(survivors))
    if candidate == current:
        return data, notes  # already correct

    data = dict(data)
    data["invoice_date"] = candidate
    notes.append(
        f"Invoice date auto-corrected from '{current}' to '{candidate}': found as the only "
        f"date in the OCR text not sitting next to permit/accreditation-issuance wording "
        f"(e.g. 'Date Issued', 'PTU', 'Valid Until')."
    )
    return data, notes


_INVOICE_NUMBER_LABEL_RE = re.compile(
    r"(?:\bOR\s*#|sales\s+invoice\s+no\.?|invoice\s+no\.?|invoice\s*#)\s*:?\s*(\d{3,})",
    re.IGNORECASE,
)
# "transact" (not "transaction#") deliberately survives OCR garbling that
# inserts a space mid-word — confirmed on a real receipt OCR'd as
# "Transact ion#" rather than "Transaction#".
_INVOICE_NUMBER_EXCLUDED_LABELS = ("transact", "trans#", "tr#", "tr no", "terminal", "reset_cnt", "min#")


def reconcile_invoice_number(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for invoice_number getting set to a Transaction#/terminal ID
    instead of the real Official-Receipt/Sales-Invoice number. Confirmed
    on a real medical-supplies receipt: extracted invoice_number was
    "ALC081-221/2832-0071-241" — sitting right after a "Transaction#"
    label — while the real number, "21909", was printed elsewhere as a
    clean, self-contained "OR#021909" (label and value on the SAME OCR
    line).

    Deliberately narrow, and INTENTIONALLY does not try to solve the
    general "which of several separately-positioned numbers is the real
    one" problem: only acts when (a) the current value sits near a known
    "not the real invoice number" label, and (b) an explicit
    "OR#<digits>"/"Sales Invoice No. <digits>"-style pattern — label AND
    value on the same line — is found elsewhere with a different number.
    Label-and-value-on-the-same-line is a much more reliable signal than
    label-on-one-line-value-on-another, since OCR reading order frequently
    separates those two (see reconcile_vendor_name / reconcile_invoice_date
    for the same lesson on other fields).

    A second confirmed real case (a Watsons receipt) does NOT fit this
    pattern at all — its correct invoice number appears completely
    unlabeled elsewhere in the text, with no consistent structural signal
    (length, position, or nearby label) that reliably distinguishes it
    from the wrong value without risking false corrections on other
    receipts (testing found the two confirmed cases actively disagree on
    the two most obvious heuristics — e.g. "prefer the longer number" gets
    one right and the other wrong). That case is intentionally left
    unhandled here — the existing vision cross-check already catches it in
    practice, and guessing without a reliable signal risks doing more harm
    than leaving it alone.
    """
    notes = []
    current = str(data.get("invoice_number") or "").strip()
    if not current or not ocr_text:
        return data, notes

    lines = ocr_text.splitlines()
    lower_lines = [l.lower() for l in lines]
    idx = next((i for i, l in enumerate(lines) if current.lstrip("$") in l), None)
    if idx is None:
        return data, notes

    window = lower_lines[max(0, idx - 2): idx + 3]
    if not any(marker in w for w in window for marker in _INVOICE_NUMBER_EXCLUDED_LABELS):
        return data, notes  # current value isn't sitting next to a suspicious label

    match = _INVOICE_NUMBER_LABEL_RE.search(ocr_text)
    if not match:
        return data, notes

    candidate = match.group(1)
    if candidate == current or candidate.lstrip("0") == current.lstrip("$0"):
        return data, notes  # same number, just formatted differently — nothing to fix

    data = dict(data)
    data["invoice_number"] = candidate
    notes.append(
        f"Invoice number auto-corrected from '{current}' to '{candidate}': the original value sat "
        f"next to a Transaction#/terminal-ID label, while '{candidate}' was found as an explicit "
        f"'OR#'/'Sales Invoice No.' label with its number on the same line elsewhere in the receipt."
    )
    return data, notes


def reconcile_vendor_name(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for vendor_name getting set to something other than the real
    merchant name. Uses two independent, narrowly-scoped signals — tried in
    order, each requiring both a specific trigger AND a specific kind of
    replacement candidate, so a value that doesn't match either exact
    pattern is left alone rather than guessed at:

      1. POS-terminal-provider / accreditation-footer boilerplate.
         Confirmed on real parking-ticket receipts (SM mall parking): the
         receipt prints the real merchant ("SM DEVELOPMENT CORPORATION")
         once, clearly following a "Name:" label near the top — but ALSO
         prints a completely unrelated company name ("CHASE TECHNOLOGIES
         CORPORATION") much further down, next to "PTU"/"ACC:"
         permit-accreditation numbers, which is the printer/POS-system's
         own accreditation info, not the merchant.

      2. The cashier's name. Confirmed on a real medical-supplies receipt:
         vendor_name was set to "VERONICA PANGIL INAN" — a person's name,
         sitting within a few lines of "CASHIER"/"SERVICED BY" — while the
         actual merchant ("CGI MEDICAL DEPOT INC") was clearly printed in
         the business header near the top of the receipt.

    Both failure modes are confirmed despite an explicit prompt instruction
    covering case 1 (ai/prompt_builder.py rule 10) — prompt wording alone
    wasn't enough, same lesson as clean_line_items() and
    reconcile_subtotal(). Case 2 isn't covered by that rule at all, which
    is exactly why it needs its own separate signal here rather than being
    folded into signal 1's boilerplate-marker check — a cashier's name
    isn't POS/permit boilerplate, so signal 1's trigger correctly doesn't
    fire for it (and testing confirmed why it shouldn't: broadening signal
    1's candidate search, e.g. via an "Operated by X" match, to also catch
    this case would have wrongly overwritten legitimate short brand names
    like "DATA BLITZ" with their unrelated legal-registrant name found
    elsewhere on the same receipt, e.g. "Orion Cepheid, Inc." — testing
    against the wider batch is what caught that before it shipped).
    """
    notes = []
    vendor = (data.get("vendor_name") or "").strip()
    if not vendor:
        return data, notes

    lines = [l.strip() for l in ocr_text.splitlines()]
    lower_lines = [l.lower() for l in lines]

    vendor_idx = next((i for i, l in enumerate(lower_lines) if vendor.lower() in l), None)
    if vendor_idx is None:
        return data, notes  # can't locate the current value in the OCR text at all

    # Signal 1: POS-provider / accreditation boilerplate.
    window = lower_lines[max(0, vendor_idx - 2): vendor_idx + 5]
    if any(marker in w for w in window for marker in _VENDOR_BOILERPLATE_MARKERS):
        for i, l in enumerate(lower_lines):
            if l.rstrip(":") != "name":
                continue
            j = i + 1
            while j < len(lines) and _is_label_like_line(lines[j]):
                j += 1  # skip past the label cluster (Address/Business Style/TIN/...) to its values
            if j >= len(lines):
                break
            candidate = lines[j]
            if candidate and candidate.lower() != vendor.lower() and any(c.isalpha() for c in candidate):
                data = dict(data)
                data["vendor_name"] = candidate
                notes.append(
                    f"Vendor auto-corrected from '{vendor}' to '{candidate}': the original value sat next "
                    f"to POS-provider/permit-accreditation boilerplate (e.g. 'PTU', 'ACC:'), while "
                    f"'{candidate}' followed an explicit 'Name' label earlier in the receipt."
                )
            return data, notes
        return data, notes

    # Signal 2: the cashier's name, sitting near a cashier/staff marker.
    cashier_window = lower_lines[max(0, vendor_idx - 3): vendor_idx + 16]
    if any(marker in w for w in cashier_window for marker in _VENDOR_CASHIER_MARKERS):
        candidate = _first_business_suffix_line(lines)
        if candidate and candidate.lower() != vendor.lower():
            data = dict(data)
            data["vendor_name"] = candidate
            notes.append(
                f"Vendor auto-corrected from '{vendor}' to '{candidate}': the original value sat next to "
                f"a cashier/staff marker (e.g. 'CASHIER', 'SERVICED BY') and looks like a person's name, "
                f"while '{candidate}' is a business-named line near the top of the receipt."
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


def reconcile_swapped_subtotal_total(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for subtotal and total_amount getting SWAPPED with each other.
    Confirmed on a real Ace Hardware receipt: extracted subtotal=799.50,
    total_amount=713.84 — backwards, since total must be >= subtotal for
    any non-negative tax. The receipt's own printed CASH(1000.00)/
    CHANGE(200.50) confirms the true total was 799.50, and 713.84 was
    labeled "Net" a few lines below its own value; tax_amount had also
    been dropped to 0 in the same pass, rather than the true 85.66.
    Notably, this invoice had NO vision_notes at all and the HIGHEST
    confidence score in an entire test batch (0.791) — nothing else in the
    pipeline catches this shape of error, hence a dedicated check.

    Trigger: total_amount < subtotal — but this is NOT always a swap bug.
    Testing against real receipts found two confirmed false-positive
    classes that both had to be excluded before this was safe to ship:

      1. Senior-citizen/PWD discount receipts. These are VAT-exempt by
         law, so total legitimately ends up LESS than subtotal once the
         VAT-exemption/discount is deducted — that's correct math, not a
         swap. Confirmed on two real receipts (a supermarket OSCA-ID
         transaction and a Jollibee senior-citizen order) where the
         "swap" would have overwritten an ALREADY-CORRECT, explicitly
         labeled subtotal ("Subtota1 PHP 199.00" printed right on the
         Jollibee receipt) with the wrong value. Any senior/PWD marker
         anywhere in the OCR text disables this whole check.
      2. The implied "tax" is actually a discount. On the OSCA receipt
         above, the gap between subtotal and total was a "Discount" line
         (printed with a trailing "40.15-", the minus sign marking it as a
         deduction) that happened to also look like a plausible tax rate
         and appear verbatim in the text — this function's original
         corroboration check wasn't specific enough to tell the two apart.
         Now also rejects if the implied amount appears near "discount"/
         "less"/a trailing minus sign in the OCR text.

    Even with both exclusions, this stays conservative in the same way as
    the other reconcile_* functions: it still requires the implied amount
    to look like a plausible tax rate (0-25%) AND be corroborated verbatim
    in the OCR text before doing anything.

    MUST run before reconcile_subtotal(): that function's own "derive from
    total - tax + discount" signal would otherwise quietly resolve subtotal
    to equal the (still-wrong) total_amount, making them equal — at which
    point this function's trigger (total < subtotal) can no longer fire at
    all, permanently hiding the swap instead of fixing it. Confirmed by
    testing: feeding the real wrong values through the existing pipeline
    order left subtotal == total_amount == 713.84, with total_amount and
    tax_amount both still wrong and no further signal available to catch it.
    """
    notes = []
    subtotal, total_amount = data.get("subtotal"), data.get("total_amount")
    if subtotal is None or total_amount is None:
        return data, notes
    try:
        subtotal_f, total_f = float(subtotal), float(total_amount)
    except (TypeError, ValueError):
        return data, notes

    if total_f >= subtotal_f or total_f <= 0:
        return data, notes  # not backwards — nothing to check here

    lower_ocr = (ocr_text or "").lower()
    if any(marker in lower_ocr for marker in ("senior", "pwd", "osca")):
        return data, notes  # VAT-exempt discount receipt — total < subtotal can be legitimate here

    implied_tax = round(subtotal_f - total_f, 2)
    implied_rate = implied_tax / total_f
    if not (0 < implied_rate <= 0.25):
        return data, notes  # the gap doesn't look like a plausible tax rate

    # Reject if the implied amount looks like a discount rather than a tax:
    # a trailing minus sign (e.g. "40.15-"), or sitting near "discount"/
    # "less" wording, both mark a deduction, not an added tax.
    amount_str = f"{implied_tax:,.2f}"
    lines = (ocr_text or "").splitlines()
    for i, line in enumerate(lines):
        if amount_str not in line and f"{implied_tax:.2f}" not in line:
            continue
        if f"{amount_str}-" in line or f"{implied_tax:.2f}-" in line:
            return data, notes
        nearby = " ".join(lines[max(0, i - 2): i + 1]).lower()
        if "discount" in nearby or "less " in nearby or "less12" in nearby.replace(" ", ""):
            return data, notes

    if not _amount_appears_in_text(ocr_text, implied_tax):
        return data, notes  # can't corroborate the implied tax amount in the OCR text

    data = dict(data)
    data["subtotal"], data["total_amount"] = total_f, subtotal_f
    notes.append(
        f"Subtotal and Total Amount Due auto-corrected (swapped): they were backwards "
        f"(total {total_f} < subtotal {subtotal_f}, which isn't possible), and swapping them "
        f"back implies a {implied_rate:.0%} tax of {implied_tax}, which does appear elsewhere "
        f"in the OCR text."
    )
    current_tax = data.get("tax_amount")
    if current_tax is None or abs(float(current_tax)) < 0.01:
        data["tax_amount"] = implied_tax
        notes.append(f"Tax amount filled in as {implied_tax} (derived from the subtotal/total swap above).")

    return data, notes


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
    elif total_lines and not matches_total_line and not matches_cash and not matches_change:
        # Broader, non-CASH/CHANGE case: the OCR text DOES contain a
        # clearly TOTAL/AMOUNT-DUE-labeled figure, but it's neither what
        # the LLM extracted nor a CASH/CHANGE mixup — total_amount just
        # doesn't match anything recognizable. Confirmed on a real LBC
        # Express courier receipt: total_amount came out as 298.01 (a
        # Freight+VAT-only figure — the LLM silently dropped the
        # "VATable(Valuation)" fee component from the total), while the
        # OCR text separately captured "Amount Due: 374.00" verbatim.
        # Deliberately NOT auto-corrected the way the CASH/CHANGE case is:
        # with courier/utility-style multi-fee invoices there's a real
        # chance the labeled figure itself is a sub-component rather than
        # the true grand total, so guessing wrong here could silently
        # replace one wrong number with another. Surfacing it and forcing
        # needs_review is the safe move; a human with the image in front
        # of them resolves it in seconds.
        distinct_totals = sorted(set(round(t, 2) for t in total_lines))
        notes.append(
            f"total_amount ({total_f}) doesn't match any TOTAL/AMOUNT DUE line found in the "
            f"OCR text ({distinct_totals}) — needs manual review; the extraction may have "
            f"missed a fee/charge component."
        )

    return data, notes
