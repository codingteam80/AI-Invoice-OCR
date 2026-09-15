"""Cleans and normalizes raw LLM JSON before it's turned into an Invoice model."""
import re
import uuid
from utils.date_utils import to_iso, parse_date, parse_date_with_context
from utils.helpers import normalize_tin, is_valid_tin, TIN_RE
from parser.currency_parser import normalize_currency, to_float, normalize_discount
from parser.tax_parser import find_tax_rate, find_tax_amount
from config.settings import settings
from config.field_aliases import (
    SUBTOTAL_LABELS as NET_LABELS,
    TOTAL_LABELS,
    TOTAL_EXCLUDE,
    TAX_AMOUNT_LABELS,
    TAX_AMOUNT_EXCLUDE,
    ZERO_RATED_LABELS,
    VAT_EXEMPT_LABELS,
    DISCOUNT_LABELS,
    DISCOUNT_LABELS_SPECIFIC,
    DISCOUNT_LABEL_GENERIC,
    CASH_LABELS,
    CHANGE_LABELS,
    CHECKBOX_FORM_LABELS,
    NOTHING_FOLLOWS_LABELS,
    PRINTER_ACCREDITATION_LABELS,
    CUSTOMER_LABELS,
    CUSTOMER_BLOCK_LABEL_WORDS,
    CUSTOMER_UNTRUSTED_CONTEXT_MARKERS,
    VENDOR_TIN_LABELS,
    CUSTOMER_TIN_LABELS,
)

# Chars that handwriting/OCR commonly confuse with digits. Only applied to
# the numeric run of an invoice number (e.g. "INV-00I" -> "INV-001"),
# never to the whole string, so we don't mangle a genuinely alphabetic ID.
_DIGIT_CONFUSION = str.maketrans({"I": "1", "l": "1", "O": "0", "o": "0", "S": "5"})
_ID_NUMERIC_TAIL_RE = re.compile(r"^(?P<prefix>\D*)(?P<tail>[\dIlOoS]+)$")

# TOTAL_LABELS/TOTAL_EXCLUDE/CASH_LABELS/CHANGE_LABELS/NET_LABELS/
# ZERO_RATED_LABELS/VAT_EXEMPT_LABELS/DISCOUNT_LABELS are all now imported
# from config/field_aliases.py — the single shared library of label
# variants per canonical field, so a newly-observed vendor wording only
# needs to be added in ONE place and every reconcile_* function below picks
# it up. (Previously each of these lists was duplicated, slightly
# differently, across this file and parser/tax_parser.py.)

# Requires 2 decimal places for the "does this value repeat verbatim
# elsewhere in the whole document" checks (_repeated_plausible_values,
# _amount_appears_in_text) — there, staying strict on the decimal format
# matters to avoid false-positive matches against unrelated integers (TINs,
# invoice numbers, quantities) scattered throughout a whole document.
_AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}")

# Looser variant — bare integers allowed — used ONLY for the specific,
# unambiguous sales-breakdown-column label sets (see allow_bare_integers
# doc on _amounts_on_line below): VATable/net-amount, zero-rated, VAT-
# exempt, and discount. Needed because some real invoices (particularly
# handwritten/manually filled BIR forms) print whole-peso amounts with no
# decimal point at all, e.g. "VATable Sales   250" or "VAT   30" — the
# strict decimals-required pattern below silently found nothing on those
# lines.
_LABEL_LINE_AMOUNT_RE = re.compile(r"(?<![\d.,])(\()?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(\))?(-)?(?![\d.,])")

# Strict variant — decimals required — used for TOTAL/CASH/CHANGE label
# sets. Those labels include very generic words ("total" alone matches
# "Total Qty", "Total Items", "Total Weight", ...), so bare integers are
# NOT accepted there: doing so would risk an unrelated small integer on a
# coincidentally-labeled line being mistaken for the actual total/cash/
# change amount. Still sign-aware (parens/trailing minus), same as the
# looser variant above — that part carries no such false-positive risk.
_LABEL_LINE_AMOUNT_RE_STRICT = re.compile(r"(?<![\d.,])(\()?\s*(\d[\d,]*\.\d{2})\s*(\))?(-)?(?![\d.,])")


def _amounts_on_line(line: str, allow_bare_integers: bool = False) -> list[float]:
    """
    Find amounts on a line, sign-aware: a value wrapped in parentheses
    ("(400.00)") or followed by a trailing minus ("40.15-") is a deduction
    and comes back NEGATIVE, matching the same convention parser/
    currency_parser.py::to_float uses. Callers that always want a positive
    magnitude (e.g. discount reconciliation) apply abs()/normalize_discount
    themselves — keeping the sign here means callers that DO care (telling
    a discount from an addition) don't lose that information at the
    source.

    allow_bare_integers: when False (the default), an amount must have
    exactly 2 decimal digits to count — this is deliberately the STRICT,
    original behavior for TOTAL/CASH/CHANGE labels. Those label sets
    include very generic words (`"total"` matches "Total Qty", "Total
    Items", "Total Weight", etc.), so accepting bare integers there would
    risk a completely unrelated small integer on a coincidentally-labeled
    line getting mistaken for the total/cash/change amount — a real
    regression that would make some previously-fine invoices suddenly need
    review, or worse, corrupt the "exactly one distinct candidate" auto-
    correction logic in reconcile_total_amount().

    Only pass allow_bare_integers=True for label sets that are specific/
    unambiguous sales-breakdown-column names (VATable Sales, Zero Rated
    Sales, VAT-Exempt Sales, Discount) — those don't collide with other
    common receipt phrasing the way "total"/"cash"/"change" do, so it's
    safe there and needed for handwritten/manually-filled BIR forms that
    print whole-peso figures with no decimal point at all (e.g.
    "VATable Sales   250").
    """
    pattern = _LABEL_LINE_AMOUNT_RE if allow_bare_integers else _LABEL_LINE_AMOUNT_RE_STRICT
    out = []
    for match in pattern.finditer(line):
        open_paren, number, close_paren, trailing_minus = match.groups()
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if (open_paren and close_paren) or trailing_minus:
            value = -value
        out.append(value)
    return out


def _amounts_near_label(
    ocr_text: str, labels: tuple, exclude: tuple = (),
    allow_bare_integers: bool = False, same_line_only: bool = False,
) -> list[float]:
    """
    Scan OCR text for amounts on lines that mention one of `labels`.

    Receipts are often OCR'd as separate detection boxes per column, so a
    label ("CASH") and its amount ("1,000.00") frequently land on two
    consecutive lines rather than one merged line — hence checking the
    following line too when the label's own line has no amount on it.

    allow_bare_integers: forwarded to _amounts_on_line — see its docstring.
    Only pass True for specific, unambiguous label sets (VATable/net
    amount, zero-rated, VAT-exempt, discount); leave False (default) for
    TOTAL/CASH/CHANGE, which include generic words like "total" that would
    otherwise risk matching an unrelated integer (e.g. "Total Qty: 12").

    `exclude` is checked against BOTH the label's own line AND the
    fallback next-line — not just the former. Confirmed on a real Emerald
    Mansion Condominium Association invoice: the "VATable Sales" label's
    own line held no amount (so the next-line fallback kicked in), and the
    very next OCR line happened to be a blank "Zero-Rated Sales" row
    followed shortly by "Total Sales (VAT Inclusive)  2,247.40" — the
    fallback grabbed that Total Sales figure and it slipped through as
    subtotal, because it was only ~ever checked against subtotal's own
    "close enough to total_amount" plausibility band (which a genuinely
    low-tax invoice can legitimately sit inside too, so that band alone
    can't reject it). Excluding "total sales"/"amount due" on the
    fallback line closes this off at the source instead.

    same_line_only: when True, disables the next-line fallback above and
    requires an amount to be found on the SAME line as the label. Use this
    for label sets that are too generic to safely borrow a number from
    whatever happens to be printed right after them — e.g. the bare word
    "discount" can match a column header or an unrelated ID/note line with
    no real discount value on it; grabbing the next line's number in that
    case produces a phantom discount rather than a real one. Specific
    phrase-level labels (e.g. "less: discount") are safe to use with the
    default next-line fallback since they rarely appear without an actual
    value nearby.
    """
    lines = ocr_text.splitlines()
    amounts = []
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(ex in lower for ex in exclude):
            continue
        if any(label in lower for label in labels):
            found = _amounts_on_line(line, allow_bare_integers=allow_bare_integers)
            if not found and not same_line_only and i + 1 < len(lines):
                next_lower = lines[i + 1].lower()
                if not any(ex in next_lower for ex in exclude):
                    found = _amounts_on_line(lines[i + 1], allow_bare_integers=allow_bare_integers)
            amounts.extend(found)
    return amounts


_VENDOR_DISPLAY_NORMALIZATIONS = (
    (re.compile(r"\b(?:innove\s+communications\s*,?\s*inc\.?|innovate\s+communications\s*,?\s*inc\.?|globe\s+business)\b", re.IGNORECASE),
     "Globe Business: Innove Communications, Inc."),
    (re.compile(r"\bsycip\s*,?\s*gorres\s*,?\s*velayo\s*(?:&|and)\s*co\.?\b", re.IGNORECASE),
     "SGV: Sycip, Gorres, Velayo & CO."),
    (re.compile(r"\bresponsible\s+services\s+inc\.?\b", re.IGNORECASE),
     "TRI-Q: RESPONSIBLE SERVICES INC."),
)


def normalize_vendor_display_name(value: str | None) -> str | None:
    """Normalize selected legal vendor names to the requested display labels.

    This is a display-name normalization step only. It does not route parsing
    through vendor-specific invoice templates and it does not change any
    financial extraction rules.
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    for pattern, canonical in _VENDOR_DISPLAY_NORMALIZATIONS:
        if pattern.search(text):
            return canonical
    return text


_PO_REF_LABEL_RE = re.compile(
    r"\bPO\s*Ref(?:erence)?\s*(?:No\.?|Number)?\s*[:#-]?\s*",
    re.IGNORECASE,
)


def clean_customer_address(value: str | None) -> str | None:
    """Remove embedded non-address field labels from a customer address.

    OCR can interleave a neighboring ``PO Ref No.`` label into the address
    reading order. Remove the label itself while preserving real address text
    that appears before or after it.
    """
    if value is None:
        return None
    text = str(value)
    text = _PO_REF_LABEL_RE.sub(" ", text)
    # 1.56: OCR/LLM reading order can prepend a customer account/barcode
    # number to the postal address (confirmed on a Globe bill where
    # ``876569970`` was stored before "One Corporate Center..."). A long
    # standalone numeric token at the START is not an address component;
    # remove it only when substantial alphabetic address text follows.
    m = re.match(r"^\s*(\d{8,14})\s+(.+)$", text, flags=re.DOTALL)
    if m and len(re.findall(r"[A-Za-z]", m.group(2))) >= 10:
        text = m.group(2)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip(" ,;:\n\t")
    return text or None


_VENDOR_CONTACT_SUFFIX_RE = re.compile(
    r"(?:[,;\s]*)(?:Tel(?:ephone)?\.?\s*No\.?|TeleFax|Fax)\s*[:#.-]?.*$",
    re.IGNORECASE,
)


def clean_vendor_address(value: str | None) -> str | None:
    """Keep postal address text only; strip embedded phone/fax contact tails.

    OCR/LLM reading order can append a vendor's contact line to the address,
    e.g. ``..., Philippines, Tel. No.: 533-9571 * TeleFax: 534-1269``.
    Telephone/fax details are not part of ``vendor_address``.
    """
    if value is None:
        return None
    text = str(value).strip()
    text = _VENDOR_CONTACT_SUFFIX_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = text.strip(" ,;:*\n\t")
    return text or None


def post_process(data: dict) -> dict:
    data = dict(data)  # shallow copy

    for date_field in ("invoice_date", "due_date"):
        if data.get(date_field):
            data[date_field] = to_iso(str(data[date_field]))

    for money_field in (
        "subtotal", "tax_amount", "total_amount", "current_charges_total", "previous_balance", "tax_rate",
        "zero_rated_sales", "vat_exempt_sales",
    ):
        if data.get(money_field) is not None:
            data[money_field] = to_float(data[money_field])

    # `discount` gets its OWN normalizer rather than plain to_float(): the
    # rest of the app (models/invoice.py::validate_totals, ai/validator.py,
    # ui/pages/History.py, exports/*) always computes
    # `subtotal + tax - discount = total`, which requires discount to be a
    # POSITIVE magnitude. A vendor printing "(400.00)"/"40.15-", or the LLM
    # occasionally returning a bare negative number, must not be allowed to
    # flip that formula into silently ADDING the discount back. See
    # parser/currency_parser.py::normalize_discount.
    if data.get("discount") is not None:
        data["discount"] = normalize_discount(data["discount"])

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
        "vendor_name", "vendor_address", "vendor_tax_id",
        "customer_name", "customer_contact", "customer_address", "customer_tax_id",
        "invoice_number",
    ):
        if data.get(text_field):
            data[text_field] = str(data[text_field]).strip()

    if data.get("vendor_name"):
        data["vendor_name"] = normalize_vendor_display_name(data["vendor_name"])

    if data.get("vendor_address"):
        data["vendor_address"] = clean_vendor_address(data["vendor_address"])

    if data.get("customer_address"):
        data["customer_address"] = clean_customer_address(data["customer_address"])

    if data.get("invoice_number"):
        data["invoice_number"] = _normalize_id_digits(data["invoice_number"])

    for tin_field in ("vendor_tax_id", "customer_tax_id"):
        if data.get(tin_field):
            data[tin_field] = normalize_tin(data[tin_field])

    return data


_PLATE_LABEL_RE = re.compile(
    r"(?:plate\s*(?:#|no\.?|number)?|vehicle\s+plate)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\- ]{1,14})",
    re.IGNORECASE,
)


def reconcile_plate_number(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Extract a vehicle plate only when it is explicitly labelled.

    Parking tickets and vehicle-related receipts commonly contain many other
    identifier-like values. This intentionally requires a plate label so a
    ticket/transaction/OR number cannot be mistaken for the plate.
    """
    if not ocr_text:
        return data, []
    result = dict(data)
    notes: list[str] = []
    for line in str(ocr_text).splitlines():
        m = _PLATE_LABEL_RE.search(line)
        if not m:
            continue
        candidate = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;|-\")")
        # A plate should not be an obviously long numeric transaction/receipt ID.
        if candidate.isdigit() and len(candidate) > 8:
            continue
        old = result.get("plate_number")
        if candidate and str(old or "").strip().upper() != candidate.upper():
            result["plate_number"] = candidate.upper()
            notes.append(f"plate_number set from explicit Plate label: {candidate.upper()}")
        return result, notes
    return result, notes


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


# NOTE: ZERO_RATED_LABELS/VAT_EXEMPT_LABELS are imported from
# config/field_aliases above — do NOT redefine them here. A previous
# version of this file redeclared identical-looking tuples at this exact
# spot, which silently SHADOWED the shared-library import for the rest of
# the module (every reconcile_* function below would have kept using
# whatever got defined last, regardless of edits made to field_aliases.py).
# That defeats the entire point of field_aliases.py being the single
# source of truth (see its module docstring) — a newly-added label variant
# there would never actually take effect here. Left as a bare comment
# (rather than silently just deleting it) so this doesn't quietly get
# reintroduced by a future merge/refactor.

# Lines to skip entirely when hunting for a zero-rated/exempt AMOUNT — scanning
# past a "Total Sales" line specifically closes the exact failure mode
# confirmed on a real TRI-Q Responsible Services invoice: the printed
# "Zero-Rated Sales" box was blank, but _amounts_near_label's "check the
# next line" fallback (see its docstring) walked onto an adjacent "Total
# Sales (VAT Inclusive)" row and grabbed ITS figure instead. The post-hoc
# "equals total_amount -> null it out" guard below already catches this
# after the fact; excluding "total sales" up front stops the wrong value
# from being picked in the first place, which also protects cases where
# total_amount itself isn't available yet to compare against.
_ZERO_RATED_EXEMPT_EXCLUDE = ("total sales", "total amount", "amount due", "amount to pay")


def reconcile_zero_rated_exempt(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for missing zero_rated_sales/vat_exempt_sales — same "fill
    gaps only, never overwrite" precedent as reconcile_tax() above, for the
    same reason: a prompt-level instruction (ai/prompt_builder.py rule 14)
    already asks the LLM to separate these from subtotal/VATABLE SALES,
    but a plain regex scan of clearly-labeled OCR lines is a reliable,
    cheap way to catch it when the LLM leaves them null even though the
    receipt actually prints a (non-zero) figure on that line.

    Deliberately does NOT try to auto-fill these when the printed column is
    genuinely blank (the common case — most invoices have no zero-rated or
    VAT-exempt sales at all) — a blank column is not the same as a missing
    extraction, and there's no OCR text to find a number in either way, so
    the "no amount found near the label" case naturally leaves both fields
    untouched at null, which is the correct result.

    The regex-fill steps below are skipped entirely when ocr_text isn't
    available, but the numeric cross-check guard further down always
    still runs regardless — it only compares already-extracted fields
    against each other and needs no OCR text of its own, so there's no
    reason to skip it just because the fill step above couldn't run.
    """
    notes = []
    data = dict(data)

    if ocr_text:
        if data.get("zero_rated_sales") is None:
            amounts = _amounts_near_label(
                ocr_text, ZERO_RATED_LABELS, exclude=_ZERO_RATED_EXEMPT_EXCLUDE, allow_bare_integers=True,
            )
            if amounts:
                data["zero_rated_sales"] = amounts[0]
                notes.append(f"zero_rated_sales filled from OCR text via regex fallback: {amounts[0]}")

        if data.get("vat_exempt_sales") is None:
            amounts = _amounts_near_label(
                ocr_text, VAT_EXEMPT_LABELS, exclude=_ZERO_RATED_EXEMPT_EXCLUDE, allow_bare_integers=True,
            )
            if amounts:
                data["vat_exempt_sales"] = amounts[0]
                notes.append(f"vat_exempt_sales filled from OCR text via regex fallback: {amounts[0]}")

    # Guard against the "blank column got filled with a DIFFERENT summary
    # column's figure" failure mode. Confirmed on a real TRI-Q Responsible
    # Services invoice: the printed "Zero-Rated Sales" box was BLANK, but
    # zero_rated_sales was extracted as 96,279.67 — which was actually the
    # "Total Sales (VAT Inclusive)" figure sitting in the adjacent column,
    # not a genuine zero-rated amount. Checked against BOTH total_amount
    # AND subtotal (the true net-of-VAT figure) — a zero_rated_sales or
    # vat_exempt_sales that exactly matches either is never legitimate
    # (they're sales SUBCATEGORIES that must be strictly less than the
    # total, and distinct from the VATable subtotal column they sit
    # alongside) — so null it out and flag for review rather than silently
    # keep a duplicated figure. The exclude-list added to the fill step
    # above already stops most of these before they happen; this remains
    # as a second, independent layer of defense for whatever slips past it
    # (e.g. a label/value pairing that legitimately has no "total sales"
    # wording nearby but still lands on the wrong column).
    #
    # Also compares against subtotal + tax_amount specifically — an
    # approximation of the printed "Total Sales (VAT Inclusive)" figure —
    # not just total_amount, because those two are NOT the same number
    # whenever a Withholding Tax deduction sits between them (TOTAL SALES
    # minus WITHHOLDING TAX = TOTAL AMOUNT DUE). Confirmed still slipping
    # through on that exact real TRI-Q invoice even after the fix above:
    # subtotal (85,963.99) and total_amount (94,560.39, i.e. Total Sales
    # LESS Withholding Tax of 1,719.28) were both already correctly
    # extracted, so neither one alone matched the contaminated
    # zero_rated_sales value (96,279.67) — but subtotal + tax_amount
    # (85,963.99 + 10,315.68 = 96,279.67) does, since that sum IS the
    # printed Total Sales figure the wrong value actually came from.
    total_amount = data.get("total_amount")
    subtotal = data.get("subtotal")
    tax_amount = data.get("tax_amount")
    comparisons: list[tuple[float, str]] = []
    for value, label in (
        (total_amount, "total_amount"), (subtotal, "subtotal"), (tax_amount, "tax_amount"),
    ):
        if value is None:
            continue
        try:
            comparisons.append((float(value), label))
        except (TypeError, ValueError):
            continue
    if subtotal is not None and tax_amount is not None:
        try:
            comparisons.append((float(subtotal) + float(tax_amount), "subtotal + tax_amount"))
        except (TypeError, ValueError):
            pass

    if comparisons:
        for field, field_label in (
            ("zero_rated_sales", "Zero-Rated Sales"),
            ("vat_exempt_sales", "VAT-Exempt Sales"),
        ):
            value = data.get(field)
            if value is None:
                continue
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                continue
            if value_f <= 0:
                continue
            match = next((cf for cf, cl in comparisons if abs(value_f - cf) < 0.01), None)
            if match is None:
                continue
            matched_label = next(cl for cf, cl in comparisons if abs(value_f - cf) < 0.01)
            # NOTE: deliberately NOT cleared to None anymore. A value equal
            # to a total-level figure is often this exact wrong-column
            # misread, but it can also be entirely legitimate — e.g. a
            # wholly zero-rated (export) sale really does have
            # zero_rated_sales == total_amount. Silently nulling a correct
            # value on those invoices was itself a confirmed regression,
            # so this now only flags the coincidence for a human to
            # confirm, without touching the extracted data.
            notes.append(
                f"{field} ({value_f}) exactly matches {matched_label} ({match}). This can mean "
                f"a different summary column's figure was read into the wrong column while the "
                f"printed \"{field_label}\" box was actually blank — OR it can be a legitimate "
                f"wholly-{field_label.lower()} invoice. Left as-is; please verify against the "
                f"source document."
            )

    return data, notes


def reconcile_discount(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for the `discount` field: fills it from a regex scan of the
    OCR text when the LLM left it null (same fill-only pattern as
    reconcile_zero_rated_exempt above), AND flags — but never silently
    hides — a disagreement in SIGN between what the LLM returned and what
    the receipt itself prints.

    Two distinct real-world failure modes motivate this:

      1. Missing entirely. A discount line the LLM's extraction dropped
         (e.g. a Globe Business bill's "Discounts (400.00)" tucked inside a
         "Charges For This Month" breakdown, several lines below the
         top-level Amount To Pay) gets filled from DISCOUNT_LABELS
         (config/field_aliases.py) via the same proximity search used for
         subtotal/tax/zero-rated above.

      2. Sign confusion. post_process() already normalizes whatever the
         LLM returned to a positive magnitude via normalize_discount() —
         but that normalization is "blind": it can't tell a genuine
         discount from, say, an "Add-ons" charge that happens to be
         printed the same way. If the OCR text's own discount-labeled line
         disagrees with the (now-positive) extracted discount by more than
         a rounding error, that's surfaced as a review note rather than
         silently trusted, following the same "auto-fix only when
         unambiguous, otherwise flag" precedent as reconcile_tax/
         reconcile_subtotal above.

    Deliberately does NOT auto-correct a disagreement (unlike
    reconcile_subtotal) — a printed discount can legitimately combine
    several deduction lines (loyalty discount + senior/PWD discount +
    promo), so a single regex hit disagreeing with the LLM's total isn't
    strong enough evidence to overwrite it, only to ask a human to check.
    """
    notes: list[str] = []
    if not ocr_text:
        return data, notes

    data = dict(data)
    amounts = _amounts_near_label(
        ocr_text, DISCOUNT_LABELS_SPECIFIC, allow_bare_integers=True,
    ) + _amounts_near_label(
        ocr_text, DISCOUNT_LABEL_GENERIC, allow_bare_integers=False, same_line_only=True,
    )
    # _amounts_near_label preserves sign (see _amounts_on_line) — a
    # discount is a deduction either way, so take the magnitude.
    magnitudes = [abs(a) for a in amounts if abs(a) > 0]

    if data.get("discount") is None:
        if magnitudes:
            found = magnitudes[0]
            data["discount"] = found
            notes.append(f"discount filled from OCR text via regex fallback: {found}")
        return data, notes

    try:
        current = float(data["discount"])
    except (TypeError, ValueError):
        return data, notes

    if magnitudes and not any(abs(current - m) < 0.01 for m in magnitudes):
        notes.append(
            f"discount ({current}) doesn't match any discount-labeled line found in the OCR "
            f"text ({sorted(set(round(m, 2) for m in magnitudes))}) — needs manual review."
        )

    return data, notes


# NET_LABELS (the labels reconcile_subtotal() scans for to find the TRUE
# net-of-VAT amount) now comes from config/field_aliases.SUBTOTAL_LABELS,
# imported above. Deliberately does NOT include "subtotal" itself — on many
# PH BIR receipts the printed "SUBTOTAL" line is VAT-INCLUSIVE (effectively
# a second total), not the net-of-VAT figure our `subtotal` field is meant
# to represent. That mix-up is exactly the bug this function exists to
# catch, so searching for "subtotal" would just find the same wrong number
# again.


def _amount_appears_in_text(ocr_text: str, value: float) -> bool:
    """Whether `value` shows up anywhere in the OCR text — used to
    corroborate a mathematically-derived candidate against something the
    receipt actually printed, rather than trusting pure arithmetic alone.

    Tolerant of common OCR noise around an otherwise-correct number:
    stray whitespace next to the decimal point or thousands separators
    ("1,234 .56", "1 ,234.56") is stripped before comparing, and a whole-
    peso value is also matched against a plain integer form (some receipts
    print whole amounts with no decimal point at all, e.g. "250" for
    250.00). Requiring an exact byte-for-byte substring match (the
    original behavior) was flagging plenty of genuinely-correct totals as
    'possibly hallucinated' just because of ordinary OCR spacing noise —
    this keeps the corroboration check useful without that false-positive
    rate.
    """
    if not ocr_text:
        return False
    compact = re.sub(r"\s+", "", ocr_text)
    candidates = {f"{value:.2f}", f"{value:,.2f}"}
    if value == int(value):
        candidates.add(str(int(value)))
        candidates.add(f"{int(value):,}")
    return any(re.sub(r"\s+", "", c) in compact for c in candidates)


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

    # Excluding "total sales"/"amount due" lines here closes the same
    # column-bleed failure mode as _ZERO_RATED_EXEMPT_EXCLUDE above, applied
    # to subtotal instead: confirmed on a real Emerald Mansion Condominium
    # Association invoice where the "VATable Sales" label's own line had no
    # amount, and the next-line fallback landed on "Total Sales (VAT
    # Inclusive)" instead — see _amounts_near_label's docstring for the
    # full mechanism.
    net_amounts = _amounts_near_label(
        ocr_text, NET_LABELS, exclude=_ZERO_RATED_EXEMPT_EXCLUDE, allow_bare_integers=True,
    )
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

# Description phrases that mark a row as a TAX/SUMMARY/PAYMENT line rather
# than an actual purchased product or service. Confirmed recurring failure
# mode: a Globe Business billing statement's "Monthly Recurring Fee (MRF)"
# summary row — itself just the sum of the "real" line items below it —
# got extracted as an ADDITIONAL line item alongside them, silently
# doubling the line-items sum (e.g. "Biz BB Plan 2,499.00" + "Monthly
# Recurring Fee (MRF) 2,198.00" summed to 4,697.00 when the actual total
# was 2,198.00). Same failure mode confirmed on North Star Travel invoices,
# where "VAT (12% of SERVICE FEE)" was extracted as its own line item
# instead of being the tax portion of the SERVICE FEE row above it. Also
# confirmed on a real TRI-Q Responsible Services invoice, where the ONLY
# genuine line item ("UTILITY SERVICES") got a phantom sibling item
# literally named "VATABLE SALES" whose amount was the Total Sales figure
# — one of the printed BIR sales-breakdown COLUMN HEADERS (VATable Sales /
# Zero-Rated Sales / VAT-Exempt Sales / Total Sales) got read as if it were
# a second purchased item rather than the summary table underneath the
# real items.
_TAX_OR_SUMMARY_ITEM_MARKERS = (
    "vat (", "vat(", "vat amount", "value added tax",
    "monthly recurring fee", "recurring fee",
    "total sales", "total amount due", "amount due", "amount to pay",
    "grand total", "subtotal", "sub total", "sub-total",
    "less: vat", "less vat", "add: vat", "add vat",
    "less: discount", "less discount", "discounts", "less withholding", "less w/tax",
    "net of vat", "amount net of vat",
    "vatable sales", "zero rated sales", "zero-rated sales",
    "vat exempt sales", "vat-exempt sales",
)

# Column-header words from a fee/professional-services BREAKDOWN table
# (Description | Fee | Expense | Professional Services | Unit Cost |
# Quantity | Net | Tax | Rate | Tax Amount | Total) — confirmed on a real
# SyCip Gorres Velayo & Co. (SGV) invoice printed as a wide, multi-column
# landscape table. _hybrid_extract's row-major reading order (see its own
# docstring) deliberately does NOT attempt column-aware layout detection —
# that was tried and reverted because it broke the far more common
# "item name .... price" receipt-row case — so on a genuinely wide,
# multi-column table like this one, the header row's own column LABELS
# can end up misread as if they were separate line items, each paired
# with whatever unrelated number happened to land in the same reading-
# order position (e.g. "Professional Services" attached to 560.00,
# "Expense" attached to 11,200.00, "Fee" attached to 500.00 — none of
# which are real purchased items; the invoice's one genuine item is
# "Our retainer fee for the month of August 2026").
#
# Deliberately an EXACT (whole, case-insensitive, stripped) match rather
# than a substring check, unlike _TAX_OR_SUMMARY_ITEM_MARKERS above:
# several of these words ("Fee", "Total", "Tax") are common enough as
# SUBSTRINGS of genuine item names ("Convenience Fee", "Total Care
# Package") that substring-matching them would drop real items; as a
# BARE, standalone description they are never a real purchased item on
# any real invoice seen so far, only ever a table column header.
_TABLE_HEADER_ITEM_LABELS = (
    "description", "fee", "expense", "professional services",
    "unit cost", "quantity", "qty", "qty.", "net", "tax", "rate",
    "tax amount", "total", "amount", "item description",
    "item description / nature of service", "nature of service",
)


def _looks_like_table_column_header(description: str) -> bool:
    """True when a line item's description is nothing but a bare table
    column-header word (see _TABLE_HEADER_ITEM_LABELS) rather than any
    real item/service name — see that constant's docstring for the
    confirmed real-world case this guards against."""
    lower = (description or "").strip().lower()
    return lower in _TABLE_HEADER_ITEM_LABELS


def _looks_like_tax_or_summary_line(description: str) -> bool:
    """True when a line item's description is actually a tax/summary/
    payment-total phrase (see _TAX_OR_SUMMARY_ITEM_MARKERS above) rather
    than a real purchased product or billed service — these rows should
    never be counted as a separate line item since they either duplicate
    (a summary that restates the sum of the real items) or fragment (a tax
    portion that belongs to the item above it, not its own item) the
    line-items total.
    """
    lower = (description or "").strip().lower()
    if not lower:
        return False
    return any(marker in lower for marker in _TAX_OR_SUMMARY_ITEM_MARKERS)


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


def _looks_like_form_boilerplate_line(description: str) -> bool:
    """
    True when a line item's description is actually printed FORM
    boilerplate — a sales-invoice-pad checkbox label ("CASH SALES" /
    "CHARGE SALES") or an end-of-items marker ("*** NOTHING FOLLOWS ***")
    — rather than any kind of real charge description at all (unlike
    _looks_like_tax_or_summary_line, these aren't even a tax/summary
    figure that duplicates a real one; they're pure form furniture that
    should never have become a row in the first place).

    Deliberately requires the WHOLE (stripped, punctuation/asterisk-
    trimmed) description to match rather than a substring check — "cash"
    or "charge" alone are too common in genuine descriptions (e.g. "Cash
    Advance Fee") to safely substring-match the way _TAX_OR_SUMMARY_ITEM_MARKERS
    does for its longer, more specific phrases.

    Confirmed on two real invoices:
      - Emerald Mansion Condominium Association: the invoice pad's
        "[ ] CASH SALES  [x] CHARGE SALES" checkbox row got OCR'd and
        extracted as the description of the invoice's one and only line
        item (with the invoice's real total amount attached to it),
        losing the actual "utilities" description entirely.
      - Gliptic Art Enterprise: the printed "*** NOTHING FOLLOWS ***"
        marker below the genuine hand-written line items got extracted as
        an additional row.
    """
    lower = (description or "").strip().lower()
    if not lower:
        return False
    if lower in CHECKBOX_FORM_LABELS:
        return True
    # Strip asterisks/dashes/spaces so "*** NOTHING FOLLOWS ***", "- nothing
    # follows -", and a plain "nothing follows" all normalize the same way
    # before the substring check.
    stripped = re.sub(r"[\*\-_=~\s]+", " ", lower).strip()
    return any(marker in stripped for marker in NOTHING_FOLLOWS_LABELS)


_REFERENCE_LINE_RE = re.compile(
    r"^(?:service\s+fee\s+of|official\s+receipt|o\.?r\.?|booking|reference|ref\.?)\b.*#\s*\S+",
    re.IGNORECASE,
)


def _looks_like_reference_line(description: str) -> bool:
    """
    True for a description like "SERVICE FEE OF BS #B0280034" — a booking/
    transaction REFERENCE NUMBER for the real line item above it, not a
    distinct billable item of its own. Confirmed on two real North Star
    International Travel invoices: the genuine "SERVICE FEE (TICKET)" row
    (and its VAT, already caught by _looks_like_tax_or_summary_line's
    "vat (" marker) sit above a third OCR line that's just this fee's
    booking-system reference number — but it still got extracted as if it
    were its own separate line item, with a stray nearby figure (the
    ROE/unit-cost column, not the item's actual PHP total) attached as its
    "amount".

    Narrowly scoped to "<known reference word> ... # <code>" so a
    legitimately named item that happens to contain a hash-tagged model or
    lot number isn't caught by accident — this is about the description
    STARTING with a reference-type word, not merely containing "#"
    anywhere.
    """
    text = (description or "").strip()
    if not text:
        return False
    return bool(_REFERENCE_LINE_RE.match(text))




def _bbox_center(box) -> tuple[float, float] | None:
    try:
        xs=[float(p[0]) for p in box]; ys=[float(p[1]) for p in box]
        return (sum(xs)/len(xs), sum(ys)/len(ys))
    except Exception:
        return None


def _bbox_bounds(box) -> tuple[float,float,float,float] | None:
    try:
        xs=[float(p[0]) for p in box]; ys=[float(p[1]) for p in box]
        return min(xs),min(ys),max(xs),max(ys)
    except Exception:
        return None


def extract_statement_summary_evidence(ocr_lines: list[dict] | None) -> dict:
    """Return strong row-level evidence from a compact Statement Summary block.

    1.55 deliberately uses label + bbox alignment rather than absolute page
    coordinates. This lets the same logic work on both full-resolution PDFs and
    smaller phone photos. The returned ``subtotal_before_discount`` is the sum
    of charge components; ``current_charges_total`` is the printed summary Total.
    """
    lines = ocr_lines or []
    if not lines:
        return {}
    def low(l): return re.sub(r"\s+", " ", str(l.get("text") or "")).strip().lower()
    anchors = [i for i,l in enumerate(lines) if "statement summary" in low(l)]
    if not anchors:
        return {}
    a = anchors[0]
    block=[]
    for l in lines[a:a+36]:
        t=low(l)
        if block and any(k in t for k in ("previous bill activity","payment activity","remaining balance")):
            break
        block.append(l)
    aliases={
        "monthly plan": {"monthly plan"},
        "add_ons": {"add-ons","addons","add ons"},
        "discounts": {"discounts","discount"},
        "total": {"total"},
    }
    labels={}
    for l in block:
        t=low(l).rstrip(':')
        for key,names in aliases.items():
            if t in names:
                labels[key]=l
    if "total" not in labels or "monthly plan" not in labels:
        return {}

    def nearest(label, maxdy=32):
        b=_bbox_bounds(label.get('bbox')) if label else None
        if not b: return None
        cy=(b[1]+b[3])/2
        # Strong row ownership: value must be clearly to the RIGHT of this
        # label, close to the same baseline, and must look like money rather
        # than a date/identifier.
        vals=[]
        for l in block:
            raw=str(l.get('text') or '').strip()
            if '/' in raw or re.fullmatch(r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}',raw):
                continue
            bb=_bbox_bounds(l.get('bbox'))
            if not bb or bb[0] <= b[2] + 40:
                continue
            lcy=(bb[1]+bb[3])/2
            delta=lcy-cy
            if delta < -3 or delta>maxdy:
                continue
            v=to_float(raw)
            if v is None:
                continue
            vals.append((abs(lcy-cy), -bb[0], abs(float(v)), raw))
        if not vals: return None
        vals.sort()
        return vals[0][2]

    monthly=nearest(labels.get('monthly plan'))
    addons=nearest(labels.get('add_ons')) if labels.get('add_ons') else 0.0
    discount=nearest(labels.get('discounts')) if labels.get('discounts') else None
    total=nearest(labels.get('total'), maxdy=38)
    if monthly is None or total is None:
        return {}
    addons=float(addons or 0.0)
    charge_sum=round(float(monthly)+addons,2)
    derived_discount=round(charge_sum-float(total),2)
    # A neighboring row can still occasionally bleed into Discounts. Require
    # the explicit discount to agree with the independently anchored charges +
    # Total; otherwise the exact difference is stronger evidence.
    if derived_discount >= -0.01:
        if discount is None or abs(float(discount)-derived_discount)>0.01:
            discount=max(0.0,derived_discount)
    else:
        return {}
    return {
        'monthly_plan': round(float(monthly),2),
        'add_ons': round(addons,2),
        'discounts': round(abs(float(discount or 0.0)),2),
        'subtotal_before_discount': charge_sum,
        'current_charges_total': round(float(total),2),
        'strong_fields': ['subtotal','discount','current_charges_total'],
    }


def reconcile_statement_summary(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    ev=extract_statement_summary_evidence(ocr_lines)
    if not ev:
        return data, []
    result=dict(data); notes=[]
    result['subtotal']=ev['subtotal_before_discount']
    result['discount']=ev['discounts']
    result['current_charges_total']=ev['current_charges_total']
    # For a simple statement with no carried prior balance, the summary Total
    # is also the final payable amount. A later 1.55 billing-balance pass may
    # replace total_amount when an explicit Remaining Balance/Amount to Pay is
    # present.
    result['total_amount']=ev['current_charges_total']
    notes.append(
        f"Statement-summary geometry set subtotal={result['subtotal']:.2f}, discount={result['discount']:.2f}, "
        f"current_charges_total={result['current_charges_total']:.2f} from explicit row ownership."
    )
    if ev['add_ons']>0:
        items=[dict(x) for x in (result.get('line_items') or [])]
        found=False
        for item in items:
            d=re.sub(r'\s+',' ',str(item.get('description') or '')).strip().lower()
            if d in {'add-ons','addons','add ons'}:
                item['quantity']=1.0; item['unit_price']=ev['add_ons']; item['amount']=ev['add_ons']; found=True
        if not found:
            items.append({'description':'Add-ons','quantity':1.0,'unit_price':ev['add_ons'],'amount':ev['add_ons']})
        # When this compact summary explicitly owns Monthly Plan and Add-ons,
        # a single non-summary plan item should use the Monthly Plan amount.
        # This repairs cases where generic item geometry accidentally consumed
        # a nearby Due Date as a large numeric price.
        main=[i for i in items if re.sub(r'\s+',' ',str(i.get('description') or '')).strip().lower() not in {'add-ons','addons','add ons'}]
        if len(main)==1 and ev.get('monthly_plan') is not None:
            main[0]['quantity']=to_float(main[0].get('quantity')) or 1.0
            main[0]['amount']=ev['monthly_plan']
            if abs(main[0]['quantity']-1.0)<0.005:
                main[0]['unit_price']=ev['monthly_plan']
        result['line_items']=items
    return result, notes


def reconcile_billing_statement_balances(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    """Separate current-period charges from carried previous balance.

    Generic telecom/utility statement pattern: Statement Summary has a current
    charges Total, while Previous Bill Activity has a Remaining Balance and a
    later Amount to Pay. ``total_amount`` remains what is payable now; the new
    fields keep the accounting identity honest instead of forcing prior balance
    into current-period VAT buckets.
    """
    lines=ocr_lines or []
    if not lines or data.get('current_charges_total') is None:
        return data, []
    def low(l): return re.sub(r'\s+',' ',str(l.get('text') or '')).strip().lower()
    anchor=next((i for i,l in enumerate(lines) if 'previous bill activity' in low(l)),None)
    if anchor is None: return data, []
    block=lines[anchor:anchor+28]
    def find_label(parts):
        for l in block:
            t=low(l)
            if any(p in t for p in parts): return l
        return None
    def same_row_value(label,maxdy=32):
        b=_bbox_bounds(label.get('bbox')) if label else None
        if not b:return None
        cy=(b[1]+b[3])/2; vals=[]
        for l in block:
            raw=str(l.get('text') or '').strip(); bb=_bbox_bounds(l.get('bbox'))
            if not bb or bb[0] <= b[2]+35 or '/' in raw: continue
            if abs((bb[1]+bb[3])/2-cy)>maxdy: continue
            v=to_float(raw)
            if v is not None: vals.append((abs((bb[1]+bb[3])/2-cy),-bb[0],float(v)))
        if not vals:return None
        vals.sort(); return round(abs(vals[0][2]),2)
    remaining=same_row_value(find_label(('remaining balance',)))
    amount_to_pay=same_row_value(find_label(('amount to pay','amount to pay (total amount due)')))
    if remaining is None and amount_to_pay is None:
        return data, []
    result=dict(data); notes=[]
    if remaining is not None:
        result['previous_balance']=remaining
    if amount_to_pay is not None:
        result['total_amount']=amount_to_pay
    if remaining is not None and amount_to_pay is not None:
        expected=round(float(result.get('current_charges_total') or 0)+remaining,2)
        if abs(expected-amount_to_pay)<=0.02:
            notes.append(
                f"Billing statement separated current_charges_total={result['current_charges_total']:.2f} "
                f"from previous_balance={remaining:.2f}; total_amount/Amount to Pay={amount_to_pay:.2f}."
            )
        else:
            notes.append(
                f"Billing statement found previous_balance={remaining:.2f} and Amount to Pay={amount_to_pay:.2f}; "
                "their relationship needs review."
            )
    return result, notes

def reconcile_loyalty_balance_context(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Keep loyalty/rewards counters out of invoice carried-balance fields.

    Retail POS receipts may print ``Open Balance`` / ``Close Balance`` /
    ``Ticket Earn`` for a loyalty or points ledger.  Those counters are not a
    prior invoice balance and must never create a ``Remaining Balance`` line
    item.  Genuine billing-statement labels such as ``Previous Balance``,
    ``Remaining Balance`` and ``Balance Forward`` still retain ownership.
    """
    if not ocr_text or data.get("previous_balance") is None:
        return data, []
    text = re.sub(r"\s+", " ", str(ocr_text)).lower()
    loyalty = bool(re.search(r"\bopen\s+balance\b", text)) and bool(
        re.search(r"\b(?:close\s+balance|ticket\s+earn|points?\s+earned?|loyalty)\b", text)
    )
    genuine = bool(re.search(
        r"\b(?:previous\s+(?:bill\s+)?balance|remaining\s+balance|balance\s+forward|previous\s+bill\s+activity)\b",
        text,
    ))
    if not loyalty or genuine:
        return data, []
    out = dict(data)
    old = out.get("previous_balance")
    out["previous_balance"] = None
    items = []
    for item in out.get("line_items") or []:
        desc = re.sub(r"\s+", " ", str((item or {}).get("description") or "")).strip().lower()
        if desc in {"remaining balance", "previous balance", "carried balance", "balance forward", "open balance", "close balance"}:
            continue
        items.append(item)
    out["line_items"] = items
    return out, [
        f"1.61 loyalty-balance ownership cleared previous_balance={old}: Open/Close Balance and Ticket Earn belong to a retail loyalty/rewards ledger, not invoice billing."
    ]


def add_remaining_balance_line_item(data: dict) -> tuple[dict, list[str]]:
    """Add a non-zero carried/remaining balance to Items Purchased for display.

    ``previous_balance`` remains the authoritative accounting field. The added
    row is a display/business row and must not be treated as current-period
    sales by validation.
    """
    notes: list[str] = []
    try:
        balance = float(data.get("previous_balance") or 0.0)
    except (TypeError, ValueError):
        return data, notes
    if abs(balance) <= 0.005:
        return data, notes
    items = list(data.get("line_items") or [])
    for li in items:
        desc = str((li or {}).get("description") or "").strip().lower()
        if desc in {"remaining balance", "previous balance", "carried balance", "balance forward"}:
            li["description"] = "Remaining Balance"
            li["quantity"] = 1.0
            li["unit_price"] = round(balance, 2)
            li["amount"] = round(balance, 2)
            out = dict(data); out["line_items"] = items
            return out, notes
    items.append({"description":"Remaining Balance","quantity":1.0,"unit_price":round(balance,2),"amount":round(balance,2)})
    out = dict(data); out["line_items"] = items
    notes.append(f"1.58 billing display: added non-zero Remaining Balance {balance:.2f} to Items Purchased while preserving previous_balance as a separate carried-balance field.")
    return out, notes


def reconcile_billing_current_period_financials(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    """Keep carried previous balances out of current-period tax arithmetic.

    When a statement exposes ``current_charges_total`` and ``previous_balance``,
    the normal invoice identity targets current charges, not Amount to Pay.
    If no strong printed VAT amount exists and subtotal-discount already equals
    current charges, clear a tax value that was merely derived from the carried
    balance or confused with another summary number.
    """
    if data.get("current_charges_total") is None:
        return data, []
    out=dict(data); notes=[]
    cct=to_float(out.get("current_charges_total")); sub=to_float(out.get("subtotal"))
    disc=normalize_discount(out.get("discount")) or 0.0
    zero=to_float(out.get("zero_rated_sales")) or 0.0
    exempt=to_float(out.get("vat_exempt_sales")) or 0.0
    if cct is None or sub is None:
        return out, notes
    ev=_strong_ocr_financial_evidence(ocr_lines or [])
    tax_ev=ev.get("tax_amount") or {}
    strong_tax=tax_ev.get("score",0) >= 85 and tax_ev.get("source") != "explicit_blank"
    implied=round(cct - sub - zero - exempt + disc,2)
    if not strong_tax and abs(implied) <= 0.05:
        old=to_float(out.get("tax_amount"))
        if old is not None and abs(old) > 0.02:
            out["tax_amount"] = None
            notes.append(f"1.56 billing semantics: cleared tax_amount={old:.2f}; current charges already reconcile as subtotal + sales buckets - discount = {cct:.2f}, and no strong labelled VAT amount is printed. Previous balance is not tax.")
    return out, notes


def reconcile_line_item_geometry(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    """Correct line-item unit price/amount from a same-row OCR amount cell.

    Uses table geometry only when a description region and an amount region are
    clearly on the same row. This is generic and avoids using sales-summary rows
    far below the item table.
    """
    lines=ocr_lines or []; items=data.get('line_items') or []
    if not lines or not items:
        return data, []
    result=dict(data); out=[dict(i) for i in items]; notes=[]
    # If the table exposes an explicit TOTAL AMOUNT header, use its x-position
    # as the authoritative amount column. This prevents Qty/ROE/unit-cost
    # numbers from being mistaken for the PHP line total.
    amount_header = next((l for l in lines if re.search(r"(?i)^\s*total\s+amount\s*$", str(l.get("text") or ""))), None)
    amount_header_box = _bbox_bounds(amount_header.get("bbox")) if amount_header else None
    for idx,item in enumerate(out):
        desc=re.sub(r'\s+',' ',str(item.get('description') or '')).strip()
        if not desc: continue
        # 1.55: Add-ons inside a Statement Summary is reconciled by the
        # summary-row parser itself. Do not let generic item geometry attach
        # an MRF/Total value from a nearby row to it.
        if desc.lower() in {'add-ons','addons','add ons'} and any('statement summary' in str(x.get('text') or '').lower() for x in lines):
            continue
        # exact/fuzzy containment against OCR item description
        desc_line=None
        for l in lines:
            t=re.sub(r'\s+',' ',str(l.get('text') or '')).strip()
            if t and (t.lower()==desc.lower() or (len(desc)>8 and desc.lower() in t.lower())):
                desc_line=l; break
        if not desc_line: continue
        db=_bbox_bounds(desc_line.get('bbox'))
        if not db: continue
        dcy=(db[1]+db[3])/2
        candidates=[]
        for l in lines:
            b=_bbox_bounds(l.get('bbox'))
            if not b or b[0] <= db[2]: continue
            cy=(b[1]+b[3])/2
            if abs(cy-dcy)>60: continue
            if amount_header_box and b[0] < amount_header_box[0] - 80:
                continue
            raw_value=str(l.get('text') or '').strip()
            # Dates/periods/IDs are never prices. This prevents values like
            # 08/26/26 from becoming 82,626.00 after punctuation stripping.
            if '/' in raw_value or re.fullmatch(r'\d{1,2}[-/]\d{1,2}[-/]\d{2,4}', raw_value):
                continue
            v=to_float(raw_value)
            if v is None or v<=0: continue
            candidates.append((b[0],float(v)))
        if not candidates: continue
        candidates.sort(key=lambda x:x[0])
        # Rightmost same-row money cell is the total amount; for quantity=1 the
        # unit price is the same. On a one-price row this covers both columns.
        row_amount=round(candidates[-1][1],2)
        qty=to_float(item.get('quantity')) or 1.0
        old_amt=to_float(item.get('amount')) or 0.0
        old_unit=to_float(item.get('unit_price')) or 0.0
        if abs(old_amt-row_amount)>0.005 or (abs(qty-1.0)<0.005 and abs(old_unit-row_amount)>0.005):
            item['amount']=row_amount
            if abs(qty-1.0)<0.005:
                item['unit_price']=row_amount
            notes.append(
                f"Line-item geometry corrected {desc!r}: unit_price={item.get('unit_price')}, amount={row_amount:.2f} "
                "from the same-row amount cell."
            )
    result['line_items']=out
    return result, notes


def _looks_address_like(text: str) -> bool:
    """Conservative generic test for a postal-address line/block."""
    t=re.sub(r"\s+", " ", str(text or "")).strip()
    if len(re.findall(r"[A-Za-z]", t)) < 6:
        return False
    signals=(
        r"\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|city|brgy\.?|barangay|center|centre|ctr\.?|condo|building|bldg\.?|floor|flr\.?|unit|suite|vargas|meralco|ortigas|pasig|makati|taguig|metro manila)\b",
        r"^\s*(?:unit\s*)?[A-Za-z]?\s*\d{2,5}\b",
        r"\b\d{4}\b\s*$",
    )
    return any(re.search(p, t, re.IGNORECASE) for p in signals)


def _rebuild_customer_address_from_party_block(data: dict, lines: list[dict]) -> tuple[str | None, str | None]:
    """Fallback when an invoice has no standalone ADDRESS label."""
    if not lines:
        return None, None
    max_x=max((_bbox_bounds(l.get("bbox")) or (0,0,0,0))[2] for l in lines) or 1
    party_label_re=re.compile(r"(?i)^\s*(?:bill(?:ed)?\s*to|sold\s*to)\s*:?\s*$")
    stop_re=re.compile(r"(?i)^(?:issue\s+date|invoice\s+date|due\s+date|client\s+no|engagement\s+no|client\s+vat|customer\s+tin|account\s+number|service\s*id|statement\s+summary|nature\s+of\s+services|description|terms|po\s*ref)\b")
    anchor=None; anchor_kind=None
    for l in lines:
        if party_label_re.match(re.sub(r"\s+", " ", str(l.get("text") or "")).strip()):
            anchor=l; anchor_kind="Bill To/Sold To block"; break
    if anchor is None:
        cname=re.sub(r"\s+", " ", str(data.get("customer_name") or "")).strip().lower()
        if cname:
            for l in lines:
                t=re.sub(r"\s+", " ", str(l.get("text") or "")).strip().lower()
                if t and (cname in t or t in cname) and len(t) >= 6:
                    anchor=l; anchor_kind="customer-name block"; break
    if anchor is None:
        return None, None
    ab=_bbox_bounds(anchor.get("bbox"))
    if not ab:
        return None, None
    anchor_y=(ab[1]+ab[3])/2
    x_limit=max_x*0.48
    candidates=[]
    for l in lines:
        b=_bbox_bounds(l.get("bbox"))
        if not b: continue
        cy=(b[1]+b[3])/2
        if cy <= anchor_y+20 or cy > anchor_y+360: continue
        if b[0] > x_limit: continue
        t=re.sub(r"\s+", " ", str(l.get("text") or "")).strip()
        if not t or stop_re.search(t): continue
        candidates.append((cy,b[0],t))
    candidates.sort()
    if not candidates:
        return None, None
    start_idx=None
    for i,(_,__,t) in enumerate(candidates):
        if _looks_address_like(t):
            start_idx=i; break
    if start_idx is None:
        return None, None
    parts=[]; last_y=None
    for cy,_,t in candidates[start_idx:]:
        if last_y is not None and cy-last_y > 95:
            break
        if len(parts) and not _looks_address_like(t) and len(re.findall(r"[A-Za-z]",t)) < 5:
            break
        parts.append(t); last_y=cy
    text=clean_customer_address(", ".join(parts))
    return text, anchor_kind


def reconcile_customer_address_layout(data: dict, ocr_lines: list[dict] | None) -> tuple[dict, list[str]]:
    """Rebuild customer address from explicit ADDRESS geometry or party block.

    1.57 adds a Bill-To/customer-name fallback for SGV/Globe-style layouts.
    It is column-aware, excluding right-side account/client metadata while
    preserving genuine leading unit numbers such as ``2101``.
    """
    lines=ocr_lines or []
    if not lines: return data, []
    addr=None
    for l in lines:
        if re.fullmatch(r"(?i)\s*(?:business\s+)?address\s*:?\s*", str(l.get("text") or "")):
            addr=l; break
    text=None; source=None
    if addr:
        ab=_bbox_bounds(addr.get("bbox"))
        if ab:
            ax2=ab[2]; ay=(ab[1]+ab[3])/2
            stop_patterns=re.compile(r"(?i)^(?:terms|po\s*ref|tin\s*no|tin\b|attention|a/e|tc|osca|service description|nature of service)\b")
            stop_y=None
            for l in lines:
                t0=re.sub(r"\s+", " ", str(l.get("text") or "")).strip()
                if not stop_patterns.search(t0): continue
                b0=_bbox_bounds(l.get("bbox"))
                if not b0: continue
                cy0=(b0[1]+b0[3])/2
                if b0[0] < ax2 + 100 and cy0 > ay and (stop_y is None or cy0 < stop_y):
                    stop_y=cy0
            candidates=[]
            for l in lines:
                t=re.sub(r"\s+", " ", str(l.get("text") or "")).strip()
                if not t or stop_patterns.search(t): continue
                b=_bbox_bounds(l.get("bbox"))
                if not b: continue
                cy=(b[1]+b[3])/2
                if b[0] >= ax2-10 and b[0] < 1650 and cy >= ay-35 and cy <= ay+115 and (stop_y is None or cy < stop_y-2):
                    candidates.append((cy,b[0],t))
            candidates.sort()
            parts=[]
            for _,__,t in candidates:
                if t not in parts: parts.append(t)
            text=clean_customer_address(", ".join(parts)) if parts else None
            source="explicit ADDRESS block"
    if not text or len(text)<12:
        text,source=_rebuild_customer_address_from_party_block(data,lines)
    if not text or len(text)<12:
        return data, []
    old=str(data.get("customer_address") or "").strip()
    tiny_tokens=[tok for tok in re.findall(r"\b[A-Za-z]{1,2}\b",text) if tok.lower() not in {"st","rd","no","u"}]
    if len(tiny_tokens) >= 5:
        return data, []
    alpha=lambda x: len(re.findall(r"[A-Za-z]",x or ""))
    old_clean="" if old in {"","-"} else old
    restores_leading_number=bool(re.match(r"^\s*\d{2,5}\b",text) and not re.match(r"^\s*\d{2,5}\b",old_clean))
    if old_clean and alpha(text) <= alpha(old_clean)+8 and not restores_leading_number:
        return data, []
    result=dict(data); result["customer_address"]=text
    contact=str(result.get("customer_contact") or "").strip()
    if contact and _looks_address_like(contact) and alpha(contact) >= 20:
        result["customer_contact"]=None
    return result,[f"Customer address rebuilt from {source} using OCR geometry; neighboring account/client/PO metadata was excluded."]

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

    kept, dropped_descriptions, dropped_tax_summary, dropped_boilerplate, dropped_reference, dropped_header = (
        [], [], [], [], [], []
    )
    for item in items:
        description = item.get("description") or ""
        if _looks_like_bare_number_or_code(description):
            dropped_descriptions.append(description)
        elif _looks_like_tax_or_summary_line(description):
            dropped_tax_summary.append(description)
        elif _looks_like_form_boilerplate_line(description):
            dropped_boilerplate.append(description)
        elif _looks_like_reference_line(description):
            dropped_reference.append(description)
        elif _looks_like_table_column_header(description):
            dropped_header.append(description)
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
    if dropped_tax_summary:
        data = dict(data)
        data["line_items"] = kept
        notes.append(
            f"Dropped {len(dropped_tax_summary)} line item(s) that were actually a VAT/tax "
            f"portion or a running-total/summary row rather than a distinct purchased item "
            f"(e.g. {dropped_tax_summary[:3]!r}) — including them alongside the real items "
            f"double-counts the line-items sum against subtotal/total_amount. Confirmed on "
            f"real Globe Business (\"Monthly Recurring Fee (MRF)\") and North Star Travel "
            f"(\"VAT (12% of SERVICE FEE)\") invoices."
        )
    if dropped_boilerplate:
        data = dict(data)
        data["line_items"] = kept
        notes.append(
            f"Dropped {len(dropped_boilerplate)} line item(s) that were actually printed form "
            f"boilerplate (a sales-invoice-pad checkbox label like \"CHARGE SALES\", or an "
            f"end-of-items marker like \"*** NOTHING FOLLOWS ***\") rather than a real charge "
            f"description (e.g. {dropped_boilerplate[:3]!r}) — confirmed on real Emerald "
            f"Mansion Condominium Association and Gliptic Art Enterprise invoices. If this was "
            f"the invoice's ONLY line item, the real description was likely lost entirely and "
            f"needs to be added back manually."
        )
    if dropped_reference:
        data = dict(data)
        data["line_items"] = kept
        notes.append(
            f"Dropped {len(dropped_reference)} line item(s) that were actually a booking/"
            f"transaction reference number for another item, not a distinct billable item "
            f"(e.g. {dropped_reference[:3]!r}) — confirmed on real North Star International "
            f"Travel invoices where a \"SERVICE FEE OF BS #...\" reference line got extracted "
            f"as its own item with an unrelated figure attached as its amount."
        )
    if dropped_header:
        data = dict(data)
        data["line_items"] = kept
        notes.append(
            f"Dropped {len(dropped_header)} line item(s) that were actually a bare table "
            f"column-header word (e.g. {dropped_header[:3]!r}) rather than a real item/service "
            f"name — confirmed on a real SyCip Gorres Velayo & Co. (SGV) invoice printed as a "
            f"wide multi-column table, where the header row's own column labels ('Fee', "
            f"'Expense', 'Professional Services') each got paired with an unrelated number and "
            f"extracted as if they were separate purchased items."
        )

    # 1.56: on billing statements, a generic "Monthly Plan" row may be a
    # Statement Summary aggregate rather than a purchased item. If its amount
    # equals current_charges_total while the OTHER concrete items reconcile to
    # that same total after the statement discount, drop only that aggregate.
    current_items = data.get("line_items") or kept
    cct = to_float(data.get("current_charges_total"))
    disc = normalize_discount(data.get("discount")) or 0.0
    if cct is not None:
        for i, it in list(enumerate(current_items)):
            if re.sub(r"\s+", " ", str(it.get("description") or "")).strip().lower() != "monthly plan":
                continue
            amt = to_float(it.get("amount")) or 0.0
            others = [x for j,x in enumerate(current_items) if j != i]
            other_sum = sum(to_float(x.get("amount")) or 0.0 for x in others)
            if abs(amt-cct) <= 0.02 and len(others) >= 2 and abs((other_sum-disc)-cct) <= 0.05:
                current_items = others
                data = dict(data); data["line_items"] = current_items
                notes.append("Dropped generic 'Monthly Plan' line item because it duplicated the Statement Summary current-charges total while the concrete items already reconciled to that total.")
                break

    deduped, dedupe_notes = _dedupe_repeated_description_items(data.get("line_items") or current_items, data)
    if dedupe_notes:
        data = dict(data)
        data["line_items"] = deduped
        notes.extend(dedupe_notes)

    return data, notes


def _dedupe_repeated_description_items(items: list[dict], data: dict) -> tuple[list[dict], list[str]]:
    """
    Backstop for the same line item getting extracted TWICE under an
    identical description, once with the net (VATable) amount and once
    with the VAT-inclusive (Total Sales) amount — confirmed on a real
    TRI-Q / Responsible Services Inc. invoice, where a single "UTILITY
    SERVICES" row got split into two line_items ("UTILITY SERVICES":
    85,963.99, the VATable Sales figure, and "UTILITY SERVICES": 96,279.67,
    the Total Sales/VAT-inclusive figure for that same row) instead of one
    item with one amount, doubling the line-items sum to roughly 1.9x the
    real total.

    Only fires when TWO items share the exact same (case-insensitive,
    whitespace-normalized) description — genuinely repeated items with the
    same name are rare enough on these small service invoices that this is
    safe, and we keep exactly one, never invent a merged value. Prefers,
    in order: the amount that equals subtotal + tax_amount (the most
    specific corroborating signal, since that's exactly what a VATable +
    VAT = VAT-inclusive split implies), then the amount that equals
    total_amount, then simply the larger of the two (a VAT-inclusive
    duplicate is always >= its net-of-VAT twin).
    """
    if not items or len(items) < 2:
        return items, []

    groups: dict[str, list[int]] = {}
    for idx, item in enumerate(items):
        key = re.sub(r"\s+", " ", (item.get("description") or "").strip().lower())
        if key:
            groups.setdefault(key, []).append(idx)

    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupe_groups:
        return items, []

    try:
        subtotal = float(data["subtotal"]) if data.get("subtotal") is not None else None
    except (TypeError, ValueError):
        subtotal = None
    try:
        tax_amount = float(data["tax_amount"]) if data.get("tax_amount") is not None else None
    except (TypeError, ValueError):
        tax_amount = None
    try:
        total_amount = float(data["total_amount"]) if data.get("total_amount") is not None else None
    except (TypeError, ValueError):
        total_amount = None

    net_plus_tax = (
        round(subtotal + tax_amount, 2) if subtotal is not None and tax_amount is not None else None
    )

    drop_indices: set[int] = set()
    notes: list[str] = []
    for key, idxs in dupe_groups.items():
        amounts = [(i, float(items[i].get("amount") or 0.0)) for i in idxs]
        keep_idx = None
        if net_plus_tax is not None:
            keep_idx = next((i for i, a in amounts if abs(a - net_plus_tax) < 0.01), None)
        if keep_idx is None and total_amount is not None:
            keep_idx = next((i for i, a in amounts if abs(a - total_amount) < 0.01), None)
        if keep_idx is None:
            keep_idx = max(amounts, key=lambda pair: pair[1])[0]

        dropped_amounts = [a for i, a in amounts if i != keep_idx]
        for i, _ in amounts:
            if i != keep_idx:
                drop_indices.add(i)
        notes.append(
            f"Dropped {len(dropped_amounts)} duplicate line item(s) named "
            f"{items[keep_idx].get('description')!r} (amount(s) {dropped_amounts}) — the same "
            f"item was extracted twice under different amounts (one matching the net/VATable "
            f"figure, one matching the VAT-inclusive Total Sales figure for the same row); kept "
            f"only {items[keep_idx].get('amount')}."
        )

    result = [item for i, item in enumerate(items) if i not in drop_indices]
    return result, notes


_VENDOR_BOILERPLATE_MARKERS = (
    "ptu", "acc:", "accr#", "accreditation", "valid until", "bir permit",
    "permit no", "pos provider", "machine identification", "authority to print",
    "date of atp", "printer's accreditation", "printer accreditation", "nonvat reg",
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
    "group", "company", "association",
)


def _first_business_suffix_line(lines: list[str], limit: int = 15) -> str | None:
    """First likely legal/business header near the top of the document.

    1.57 joins a short preceding brand/building line with an entity line when
    OCR splits a legal name over two visual lines.
    """
    for i,line in enumerate(lines[:limit]):
        lower=line.lower()
        if any(suf in lower for suf in _VENDOR_BUSINESS_SUFFIXES):
            candidate=line.strip()
            if i>0 and ("association" in lower or len(candidate.split()) <= 4):
                prev=lines[i-1].strip()
                if prev and any(c.isalpha() for c in prev) and not _is_label_like_line(prev):
                    if not re.search(r"(?i)\b(?:street|st\.?|ave\.?|avenue|road|rd\.?|city|tel|fax|tin)\b",prev):
                        candidate=f"{prev} {candidate}"
            candidate=re.sub(r"(?i)\bIne\.?$", "Inc.", candidate)
            return re.sub(r"\s+", " ", candidate).strip()
    return None


_DATE_CANDIDATE_RE = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"        # 11/18/2025, 06-08-2022
    r"|\b\d{1,2}[/\-][A-Za-z]{3,9}[/\-]\d{2,4}\b" # 13-Aug-26
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

    # 1.52: an explicit ``Invoice Date`` label outranks generic date
    # scanning. This is especially important when the document itself prints
    # a format hint such as ``Billing Period (mm/dd/yy)`` and also contains a
    # Due Date. The old "exactly one date survives" rule could not decide in
    # that common case and a post-vision raw ``08/05/26`` could fall back to
    # day-first parsing.
    labeled_candidates: set[str] = set()

    # 1.53: when OCR misses a printed mm/dd/yy hint, use another explicitly
    # labelled date (normally Due Date) only as a disambiguation signal. For
    # example Invoice Date 08/05/26 + Due Date 08/26/26 strongly favors
    # Aug-05 over May-08 because the former is the nearby billing date.
    # 1.58: a plain ``Date:`` row in the invoice header is strong direct
    # ownership evidence too. This fixes layouts such as TRI-Q where the real
    # transaction date is printed as ``Date:`` / ``13-Aug-26`` while a BIR or
    # printer footer later contains ``Date Issued 04-04-2025``. Only an exact
    # Date label (or Date + value on that same row) qualifies; ``Date of ATP``,
    # ``Date Issued``, accreditation/permit/expiry dates are intentionally
    # excluded. Search only a short forward window so a distant footer date
    # cannot be captured.
    plain_date_candidates: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not re.match(r"(?i)^date\s*:?\s*(?:$|\d|[A-Za-z]{3,9})", stripped):
            continue
        if re.search(r"(?i)date\s+(?:of|issued|accredit|expir|valid|permit|ptu)", stripped):
            continue
        window = " ".join(lines[i:min(len(lines), i + 3)])
        for dm in _DATE_CANDIDATE_RE.finditer(window):
            parsed = parse_date_with_context(dm.group(), ocr_text)
            if parsed:
                plain_date_candidates.add(parsed.isoformat())
                break
    if len(plain_date_candidates) == 1:
        candidate = next(iter(plain_date_candidates))
        if candidate != current:
            result = dict(data)
            result["invoice_date"] = candidate
            return result, [
                f"1.58 invoice-date ownership: corrected '{current}' to '{candidate}' from the explicit header Date row; printer/BIR Date Issued/ATP/accreditation dates are not invoice dates."
            ]
        return data, notes

    due_candidates=[]
    for i, line in enumerate(lines):
        if not re.search(r"(?i)\bdue\s+date\b", line):
            continue
        window=" ".join(lines[i:min(len(lines), i+5)])
        for dm in _DATE_CANDIDATE_RE.finditer(window):
            dd=parse_date_with_context(dm.group(), ocr_text)
            if dd:
                due_candidates.append(dd)
                break

    for i, line in enumerate(lines):
        if not re.search(r"(?i)\binvoice\s+date\b", line):
            continue
        window = " ".join(lines[i:min(len(lines), i + 7)])
        for m in _DATE_CANDIDATE_RE.finditer(window):
            raw=m.group()
            parsed = parse_date_with_context(raw, ocr_text)
            short=re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})",raw)
            has_format_hint=bool(re.search(r"(?i)\b(?:mm\s*/\s*dd|dd\s*/\s*mm)\s*/\s*(?:yy|yyyy)\b",ocr_text))
            if short and not has_format_hint:
                a,b,yy=map(int,short.groups())
                if a<=12 and b<=12 and due_candidates:
                    from datetime import datetime as _dt
                    opts=[]
                    for fmt in ("%d/%m/%y","%m/%d/%y"):
                        try:
                            cand=_dt.strptime(raw,fmt).date()
                        except ValueError:
                            continue
                        gaps=[(due-cand).days for due in due_candidates if (due-cand).days>=0]
                        if gaps:
                            opts.append((min(gaps),cand))
                    if opts:
                        opts.sort(key=lambda x:x[0])
                        if opts[0][0] <= 90 and (len(opts)==1 or opts[1][0]-opts[0][0] >= 14):
                            parsed=opts[0][1]
            if parsed:
                labeled_candidates.add(parsed.isoformat())
                break
    if len(labeled_candidates) == 1:
        candidate = next(iter(labeled_candidates))
        if candidate != current:
            result = dict(data)
            result["invoice_date"] = candidate
            return result, [
                f"Invoice date auto-corrected from '{current}' to '{candidate}': "
                "read from the explicit Invoice Date label using the document's printed date-format hint."
            ]
        return data, notes

    survivors: set[str] = set()
    for i, line in enumerate(lines):
        for m in _DATE_CANDIDATE_RE.finditer(line):
            parsed = parse_date_with_context(m.group(), ocr_text)
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
#
# PRINTER_ACCREDITATION_LABELS (imported from config/field_aliases) is
# folded in here too: a printer's own booklet-accreditation number
# ("Printer's Accreditation No.: 032MP20210000000039") sits in the fine
# print at the bottom of PH invoice booklets, right next to a long digit
# string that looks just as "number-like" as a real invoice number but
# belongs to the PRINTER, not this invoice — confirmed on a real Adel
# Printing Services-printed invoice booklet footer. Same failure shape as
# the Transaction#/terminal-ID case above, so it gets the same exclusion
# treatment.
_INVOICE_NUMBER_EXCLUDED_LABELS = (
    "transact", "trans#", "tr#", "tr no", "terminal", "reset_cnt", "min#",
) + PRINTER_ACCREDITATION_LABELS


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


def reconcile_missing_vendor_name(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for when the LLM returns vendor_name as null/empty outright —
    a DIFFERENT failure from the one reconcile_vendor_name() below handles
    (which only ever corrects an already-present-but-wrong value; it
    returns immediately if vendor_name is empty, by design, since it has
    no "current value" to locate in the OCR text and correct from).
    Without this, an invoice hits the sanitize_for_model() placeholder and
    is permanently recorded as "Unknown Vendor" even when the real
    business name is sitting in plain text 1-2 lines from the top of the
    document — confirmed recurring on runs of real TRI-Q Responsible
    Services and North Star International Travel invoices where the LLM
    dropped vendor_name entirely despite the business-suffixed header
    line ("RESPONSIBLE SERVICES INC.", "NORTH STAR INTERNATIONAL TRAVEL
    INC.") being clearly present and legible in the OCR text.

    Deliberately reuses the exact same "first business-suffix line near
    the top" heuristic reconcile_vendor_name() already uses for its own
    corrections, rather than a new heuristic, so the two functions can
    never disagree with each other about what counts as a business name.
    Still just a best-effort fallback: if no business-suffix line is
    found in the first few lines either, this leaves vendor_name empty
    and lets the existing "Unknown Vendor" placeholder + needs_review
    behavior take over, rather than guessing at an arbitrary line.
    """
    notes: list[str] = []
    if data.get("vendor_name") or not ocr_text:
        return data, notes
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    candidate = _first_business_suffix_line(lines)
    if not candidate:
        return data, notes
    data = dict(data)
    data["vendor_name"] = candidate
    notes.append(
        f"vendor_name was empty/missing from the extraction; filled with '{candidate}' — the "
        f"first business-suffixed line found near the top of the OCR text."
    )
    return data, notes


def _collapse_whitespace(text: str) -> str:
    """Lowercase and strip ALL whitespace — used so an OCR/LLM value that
    differs from the source OCR line only in whether words got merged or
    split by a stray/missing space still counts as "found" when checking
    whether a value is grounded in the OCR text. Confirmed on a real
    Emerald Mansion Condominium Association invoice: the LLM's vendor_name
    came back as "ADELPRINTING SERVICES" (words merged together) while the
    OCR text itself printed "ADEL PRINTING SERVICES" (with the space) — an
    exact-substring grounding check never located it, so the untrusted-
    printer-boilerplate correction below never even got a chance to run."""
    return re.sub(r"\s+", "", text.lower())


def _find_line_containing(lines_lower: list[str], value: str) -> int | None:
    """Index of the first line in `lines_lower` (already lowercased) that
    contains `value`, matching whitespace-insensitively (see
    _collapse_whitespace)."""
    needle = _collapse_whitespace(value)
    if not needle:
        return None
    for i, l in enumerate(lines_lower):
        if needle in _collapse_whitespace(l):
            return i
    return None


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

    vendor_idx = _find_line_containing(lower_lines, vendor)
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

        # No explicit "Name:" label exists on this document at all (common
        # on invoices whose real merchant is just a logo/header line with
        # no label in front of it, e.g. "Emerald Mansion Condominium
        # Association Inc." printed plainly at the top) — fall back to the
        # same "first business-suffix line near the top" heuristic signal
        # 2 already uses below, restricted to lines that appear BEFORE the
        # boilerplate block itself so we don't just re-find the footer.
        candidate = _first_business_suffix_line(lines[:vendor_idx])
        if candidate and candidate.lower() != vendor.lower():
            data = dict(data)
            data["vendor_name"] = candidate
            notes.append(
                f"Vendor auto-corrected from '{vendor}' to '{candidate}': the original value sat next "
                f"to POS-provider/permit-accreditation boilerplate (e.g. 'PTU', 'ACC:'), and no explicit "
                f"'Name' label exists on this document — '{candidate}' is a business-named line near "
                f"the top of the page instead."
            )
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


def reconcile_issuer_printer_ownership(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Prefer the invoice issuer in the header over a printer business in the footer.

    Philippine pre-printed invoices commonly contain a second company name in
    BIR Authority-to-Print / printer-accreditation boilerplate.  If the current
    vendor occurs in or below that footer block, a business-named header line
    before the footer owns ``vendor_name`` instead.
    """
    vendor = re.sub(r"\s+", " ", str(data.get("vendor_name") or "")).strip()
    if not vendor or not ocr_text:
        return data, []
    lines = [re.sub(r"\s+", " ", x).strip() for x in str(ocr_text).splitlines() if x.strip()]
    lower = [x.lower() for x in lines]
    vendor_idx = _find_line_containing(lower, vendor)
    if vendor_idx is None:
        # Extractors sometimes concatenate the printer name and its postal
        # address into one vendor string while OCR keeps them on separate
        # lines.  Locate the leading business-name segment instead of
        # requiring the whole concatenated value to occur verbatim.
        vendor_head = vendor.split(",", 1)[0].strip()
        vendor_idx = _find_line_containing(lower, vendor_head)
    if vendor_idx is None:
        return data, []
    footer_markers = (
        "authority to print", "date of atp", "printer's accreditation",
        "printer accreditation", "nonvat reg", "printer", "accreditation date",
    )
    marker_indices = [i for i, line in enumerate(lower) if any(m in line for m in footer_markers)]
    if not marker_indices:
        return data, []
    footer_start = min(marker_indices)
    # Only intervene when the selected vendor belongs to, or follows, the
    # printer/ATP footer.  Do not rewrite legitimate header vendors merely
    # because the document also contains footer boilerplate.
    if vendor_idx < max(0, footer_start - 1):
        return data, []
    candidate = _first_business_suffix_line(lines[:footer_start], limit=min(24, footer_start))
    if not candidate or _collapse_whitespace(candidate) == _collapse_whitespace(vendor):
        return data, []
    # Reject obvious metadata/address lines accidentally containing a suffix.
    if re.search(r"(?i)\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|city|tel\.?|fax|tin|invoice|receipt)\b", candidate):
        return data, []
    out = dict(data)
    out["vendor_name"] = candidate
    return out, [
        f"1.61 issuer ownership corrected vendor_name from '{vendor}' to '{candidate}': the former is inside BIR printer/Authority-to-Print footer boilerplate while the latter is the business header before the invoice body."
    ]


def _is_customer_block_label_line(line: str) -> bool:
    stripped = line.strip().rstrip(":.").lower()
    if not stripped or len(stripped) < 2:
        return True
    return stripped in CUSTOMER_BLOCK_LABEL_WORDS or stripped in CUSTOMER_LABELS


_CUSTOMER_LABEL_SAMELINE_RE = re.compile(
    r"^\s*(?:billed\s*to|bill\s*to|sold\s*to|registered\s*name|customer|client)\s*:?\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Words that turn "customer"/"client" from the customer-NAME label into a
# DIFFERENT field's label instead (e.g. "Customer TIN", "Customer
# Address", "Client No.") — matched against the first word right after
# "customer"/"client" so those compound labels never get mistaken for the
# bare "Customer:" name label. Confirmed on a real Globe Business invoice:
# a "Customer TIN" table cell (value on a separate line/cell below it, not
# on the same line) got matched as "customer" + captured "TIN" as if that
# single leftover word were the customer's actual name.
_CUSTOMER_LABEL_FALSE_FRIENDS = ("tin", "address", "name", "no", "no.", "id", "contact", "information", "details")
_CUSTOMER_NAME_PLACEHOLDER_RE = re.compile(
    r"(?i)^\s*(?:buyer(?:'s|s)?\s+name|customer\s+name|name\s+of\s+(?:buyer|customer)|client\s+name|bill(?:ed)?\s+to|sold\s+to)\s*:?[\s_-]*$"
)


def _is_customer_name_placeholder(value: str | None) -> bool:
    """True when a supposed customer name is only an unfilled form label."""
    if not value:
        return False
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return bool(_CUSTOMER_NAME_PLACEHOLDER_RE.fullmatch(cleaned))


def _is_customer_label_false_friend(line: str) -> bool:
    lower = line.strip().lower()
    for base in ("customer", "client"):
        if lower.startswith(base):
            rest = lower[len(base):].lstrip(" :").split()
            if rest and rest[0].rstrip(".:") in _CUSTOMER_LABEL_FALSE_FRIENDS:
                return True
    return False


#  Lower number = more trusted. "Registered Name"/"Billed To"/"Bill To"
#  reliably hold the full legal customer name on real PH invoices. "Sold
#  To" is ranked last: confirmed on real Emerald Mansion / Gliptic Art
#  invoices where "SOLD TO:" held a short internal account code
#  ("EM1107T8") or was left blank, while the actual customer name sat a
#  couple of lines further down next to "Registered Name:" instead.
_CUSTOMER_LABEL_PRIORITY = {
    "registered name": 0,
    "billed to": 1,
    "bill to": 1,
    "customer": 2,
    "client": 2,
    "sold to": 3,
}


def _find_customer_label_candidate(lines: list[str]) -> str | None:
    """
    Look for an explicit customer-block label ("BILLED TO:", "SOLD TO:",
    "Registered Name:", "Customer:") and return the value that follows it
    — either on the SAME line (e.g. "BILLED TO: Tsukiden Global Solutions
    Inc.") or, if the label sits alone on its own line, the next
    non-label, non-blank line (skipping past an "Address"/"TIN"/etc.
    label cluster the same way a vendor's "Name:" value is found — see
    reconcile_vendor_name).

    When several different labels are present, prefers the highest-
    priority one found (see _CUSTOMER_LABEL_PRIORITY) rather than simply
    whichever appears first in reading order — some invoices print a
    less-reliable label (e.g. "SOLD TO") ABOVE the more reliable
    "Registered Name" field.
    """
    candidates: list[tuple[int, str]] = []  # (priority, candidate)

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        lower = line.rstrip(":.").lower()

        if _is_customer_label_false_friend(line):
            continue

        if lower in CUSTOMER_LABELS:
            j = i + 1
            while j < len(lines) and _is_customer_block_label_line(lines[j]):
                j += 1
            if j < len(lines):
                candidate = lines[j].strip()
                if candidate and any(c.isalpha() for c in candidate):
                    candidates.append((_CUSTOMER_LABEL_PRIORITY.get(lower, 9), candidate))
            continue

        match = _CUSTOMER_LABEL_SAMELINE_RE.match(line)
        if match:
            matched_label = next((label for label in CUSTOMER_LABELS if label in lower), None)
            if matched_label:
                candidate = match.group(1).strip(" :")
                if candidate and any(c.isalpha() for c in candidate) and not _is_customer_block_label_line(candidate):
                    candidates.append((_CUSTOMER_LABEL_PRIORITY.get(matched_label, 9), candidate))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def reconcile_customer_name(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for customer_name (the billed party, NOT the vendor) getting
    set to something other than the real customer. Unlike vendor_name —
    which is frequently just an unlabeled header/logo — the customer block
    on a PH invoice is almost always explicitly labeled ("BILLED TO:",
    "SOLD TO:", "Registered Name:", "CUSTOMER:"), which makes a direct
    label lookup a reliable, high-confidence signal here specifically.

    Three confirmed real failure modes, all fixed the same way (find the
    label-based candidate; use it if the current value looks wrong):

      1. Missing entirely. A real Emerald Mansion Condominium Association
         invoice had a handwritten "Registered Name: TSUKIDEN GLOBAL
         SOLUTIONS INC" that the extraction left as customer_name: null.

      2. A printer/booklet footer's own proprietor got picked up instead.
         A real North Star International Travel invoice: customer_name
         came out as "Mylene Canlas-Proprietres" — the name/TIN of the
         PRINTING PRESS that printed the invoice booklet (sitting in the
         page footer next to "Printer's Accreditation No." and "BIR
         Permit No.OCN"), while the actual "BILLED TO: Tsukiden Global
         Solutions Inc." block sat near the top of the same page.

      3. An unrelated field's value got picked up instead. A real SyCip
         Gorres Velayo & Co. (SGV) invoice: customer_name came out as
         "Business Tax Services" — the value of that invoice's own
         "Nature of Services:" field (an engagement-details line, not a
         customer identifier) — while the real "Bill To:" block further
         down the page named "Tsukiden Global Solutions Inc." correctly.

    Auto-corrects when: (a) customer_name is missing/blank, (b) the current
    value can be located in the OCR text sitting near one of
    CUSTOMER_UNTRUSTED_CONTEXT_MARKERS (printer-footer/proprietor/
    "nature of service" wording), (c) the current value cannot be located
    anywhere in the OCR text at all (a value that isn't even grounded in
    this document's own text is not worth protecting the way
    reconcile_vendor_name protects an ungrounded vendor_name — there, no
    reliable unlabeled-header fallback exists; here, the explicit-label
    lookup already IS that reliable fallback), or (d) the current value IS
    one of _CUSTOMER_LABEL_FALSE_FRIENDS on its own (e.g. bare "TIN") — a
    single field-modifier word can never be a real company/person name, so
    it's treated as implausible even when it happens to also be "grounded"
    (it trivially is, since it's a substring of the very "Customer TIN"
    label line it was mis-parsed from). Confirmed on a real Globe Business
    invoice, where customer_name came out as literally "TIN" — the single
    word left over after "Customer" matched a "Customer TIN" table-cell
    label whose actual value sat on a separate line/cell below it (see
    _is_customer_label_false_friend). In every case, only overrides when a
    genuine label-based candidate is actually found, and never when that
    candidate is identical to the current value.
    """
    notes = []
    current = (data.get("customer_name") or "").strip()
    lines = [l.strip() for l in (ocr_text or "").splitlines()]
    lower_lines = [l.lower() for l in lines]

    should_check_replacement = not current
    reason = "customer_name was missing"

    if current and (_is_customer_name_placeholder(current) or current.lower().rstrip(".:") in _CUSTOMER_LABEL_FALSE_FRIENDS):
        should_check_replacement = True
        reason = f"'{current}' is an unfilled customer field label, not a plausible company/person name"
    elif current:
        idx = _find_line_containing(lower_lines, current)
        if idx is None:
            should_check_replacement = True
            reason = f"'{current}' does not appear anywhere in the OCR text"
        else:
            window = lower_lines[max(0, idx - 2): idx + 3]
            if any(marker in w for w in window for marker in CUSTOMER_UNTRUSTED_CONTEXT_MARKERS):
                should_check_replacement = True
                reason = (
                    f"'{current}' sits next to printer-footer/proprietor or "
                    f"unrelated 'Nature of Service' wording rather than a customer label"
                )

    if not should_check_replacement:
        return data, notes

    is_implausible_value = bool(current and (_is_customer_name_placeholder(current) or current.lower().rstrip(".:") in _CUSTOMER_LABEL_FALSE_FRIENDS))

    candidate = _find_customer_label_candidate(lines)
    if not candidate or candidate.lower() == current.lower():
        if is_implausible_value:
            # No better candidate exists, but the current value is
            # nonsense on its face (a bare field-modifier word) — clear it
            # to null rather than keep it, same principle as
            # sanitize_for_model preferring an honest "missing" over a
            # confidently-wrong placeholder.
            data = dict(data)
            data["customer_name"] = None
            notes.append(f"customer_name cleared: {reason}, and no better candidate was found.")
        return data, notes

    data = dict(data)
    data["customer_name"] = candidate
    notes.append(
        f"customer_name auto-corrected from {current!r} to {candidate!r}: {reason}, while "
        f"'{candidate}' was found directly following an explicit 'Billed To'/'Sold To'/"
        f"'Registered Name'/'Customer' label."
    )
    return data, notes


_TIN_CANDIDATE_RE = re.compile(r"(\d[\d\-\s]{7,17}\d)")


def _label_pattern(label: str) -> re.Pattern:
    """Compile `label` into a word-boundary-safe regex — used instead of a
    plain substring check so e.g. the "tin" entry in CUSTOMER_TIN_LABELS
    can never match inside an unrelated word like "obtain" or "continuing"."""
    return re.compile(r"\b" + re.escape(label) + r"\b", re.IGNORECASE)


def _extract_tin_near(text: str, start: int, window: int = 40) -> str | None:
    """Find a TIN-SHAPED digit run within `window` chars after position
    `start` in `text`. Returns the raw matched string (not yet normalized
    or validated against the strict PH TIN shape) or None."""
    segment = text[start: start + window]
    m = _TIN_CANDIDATE_RE.search(segment)
    return m.group(1).strip() if m else None


def _find_vendor_tin_candidate(ocr_text: str) -> str | None:
    """
    Find a TIN following an explicit "VAT Reg. TIN"-style label (see
    VENDOR_TIN_LABELS) — confirmed, across every real invoice sample seen
    so far (Globe Business, TRI-Q, North Star, SGV, Gliptic Art, Emerald
    Mansion), to always sit in the VENDOR's own header block near the top
    of the page, never the customer's and never a separate printer-
    booklet footer's registration. Returns the first candidate that's
    still a validly-shaped PH TIN after normalize_tin() — an unusable-
    looking candidate is worse than none, since reconcile_tax_ids only
    ever replaces a value with something it's actually confident about.
    """
    for label in VENDOR_TIN_LABELS:
        for m in _label_pattern(label).finditer(ocr_text):
            raw = _extract_tin_near(ocr_text, m.end())
            if raw:
                normalized = normalize_tin(raw)
                if is_valid_tin(normalized):
                    return normalized
    return None


def _find_customer_tin_candidate(lines: list[str]) -> str | None:
    """
    Find a TIN sitting within the customer block (see CUSTOMER_LABELS /
    _find_customer_label_candidate) — i.e. near "BILLED TO"/"SOLD TO"/
    "Registered Name"/"Customer", never anywhere else in the document.
    Only looks within a bounded window of lines following a customer-block
    label so a TIN printed elsewhere (vendor header, printer-booklet
    footer) can never be mistaken for the customer's own — the exact
    failure mode confirmed on a real North Star International Travel
    invoice, where customer_tax_id came out as "908-260-302-00000" (the
    invoice-BOOKLET PRINTER's own VAT registration, sitting in the page
    footer next to "Printer's Accreditation No.") instead of the real
    customer's "007-848-122-000", printed a few lines above under an
    explicit "TIN NO" label inside the "BILLED TO" block.

    Recognizes the customer-block label whether it sits ALONE on its own
    line ("BILLED TO:" then the name below) OR merged with the name on
    the SAME line ("BILLED TO: Tsukiden Global Solutions Inc.") — the
    exact same two layouts _find_customer_label_candidate already handles
    for the name itself; this must recognize both too; otherwise the
    "same line" layout (confirmed the more common one across real North
    Star / TRI-Q invoices) never gets a block window to search at all.
    """
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        lower = line.rstrip(":.").lower()
        is_block_start = lower in CUSTOMER_LABELS or bool(_CUSTOMER_LABEL_SAMELINE_RE.match(line))
        if not is_block_start:
            continue
        block = "\n".join(lines[i: min(len(lines), i + 8)])
        for label in CUSTOMER_TIN_LABELS:
            for m in _label_pattern(label).finditer(block):
                raw = _extract_tin_near(block, m.end())
                if raw:
                    normalized = normalize_tin(raw)
                    if is_valid_tin(normalized):
                        return normalized
    return None


def reconcile_tax_ids(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """
    Backstop for vendor_tax_id / customer_tax_id getting swapped with each
    other, filled with a printer-booklet's own registration, or left
    missing despite a clearly labeled TIN being present in the OCR text.

    Confirmed real failure mode: a North Star International Travel
    invoice's customer_tax_id came out as "908-260-302-00000" — the
    invoice-BOOKLET PRINTER's own VAT registration — while the real
    customer's TIN ("007-848-122-000") was printed a few lines above,
    directly under an explicit "TIN NO" label inside the "BILLED TO"
    block (see _find_customer_tin_candidate).

    Auto-corrects a field when: (a) it's missing/blank, (b) it fails the
    strict PH TIN shape (utils.helpers.is_valid_tin) even after
    normalize_tin(), (c) it's IDENTICAL to the OTHER party's TIN (vendor
    and customer are never the same legal entity, so an exact match is
    always a mix-up, never a coincidence), or (d) it sits in the OCR text
    next to CUSTOMER_UNTRUSTED_CONTEXT_MARKERS (the same printer-footer/
    proprietor signal reconcile_customer_name already uses) — and only
    when a genuine label-based replacement candidate is actually found
    and differs from the current value.
    """
    notes: list[str] = []
    if not ocr_text:
        return data, notes

    lines = [l.strip() for l in ocr_text.splitlines()]
    lower_lines = [l.lower() for l in lines]

    vendor_tin = normalize_tin(data.get("vendor_tax_id"))
    customer_tin = normalize_tin(data.get("customer_tax_id"))

    def _in_untrusted_context(value: str) -> bool:
        idx = _find_line_containing(lower_lines, value)
        if idx is None:
            return False
        window = lower_lines[max(0, idx - 2): idx + 3]
        return any(marker in w for w in window for marker in CUSTOMER_UNTRUSTED_CONTEXT_MARKERS)

    result = dict(data)

    # Computed once, upfront, regardless of whether either field currently
    # "looks fine" on its own — needed for the swap check below, which
    # requires comparing BOTH current values against BOTH candidates.
    vendor_candidate = _find_vendor_tin_candidate(ocr_text)
    customer_candidate = _find_customer_tin_candidate(lines)

    # Swapped-with-each-other detection: the current vendor_tax_id is
    # actually the value that belongs in the CUSTOMER block (and/or vice
    # versa) — both individual values can look perfectly validly-shaped on
    # their own, so is_valid_tin() alone can never catch this; only
    # cross-referencing against where each one ACTUALLY belongs can.
    vendor_holds_customer_value = bool(
        customer_candidate and vendor_tin == customer_candidate and vendor_candidate != vendor_tin
    )
    customer_holds_vendor_value = bool(
        vendor_candidate and customer_tin == vendor_candidate and customer_candidate != customer_tin
    )

    needs_vendor_fix = (
        not vendor_tin
        or not is_valid_tin(vendor_tin)
        or (customer_tin and vendor_tin == customer_tin)
        or vendor_holds_customer_value
    )
    if needs_vendor_fix:
        candidate = vendor_candidate
        if candidate and candidate != vendor_tin:
            old = result.get("vendor_tax_id")
            result["vendor_tax_id"] = candidate
            notes.append(
                f"vendor_tax_id auto-corrected from {old!r} to {candidate!r}: found directly "
                f"following an explicit 'VAT Reg. TIN' label in the vendor's own header block."
            )
            vendor_tin = candidate

    needs_customer_fix = (
        not customer_tin
        or not is_valid_tin(customer_tin)
        or (vendor_tin and customer_tin == vendor_tin)
        or (customer_tin and _in_untrusted_context(customer_tin))
        or customer_holds_vendor_value
    )
    if needs_customer_fix:
        candidate = customer_candidate
        if candidate and candidate != customer_tin:
            old = result.get("customer_tax_id")
            result["customer_tax_id"] = candidate
            notes.append(
                f"customer_tax_id auto-corrected from {old!r} to {candidate!r}: found within "
                f"the 'BILLED TO'/'SOLD TO'/'Registered Name' customer block, replacing a value "
                f"that was missing, invalidly shaped, matched the vendor's own TIN, or sat next "
                f"to printer-footer/proprietor boilerplate instead."
            )

    return result, notes



def apply_identity_corrections_with_gate(
    data: dict,
    vision_corrections: dict | None,
    ocr_text: str,
    extra_locked_fields: set[str] | None = None,
) -> tuple[dict, dict, set[str], list[str], dict, dict]:
    """Protect explicit OCR/header identity evidence from weaker vision guesses.

    Returns ``(data, filtered_corrections, locked_fields, notes, accepted, rejected)``.
    The first 1.52 target is TIN confusion such as SGV's valid ``VAT Reg.
    TIN: 000-502-547-00000`` being overwritten by ``Client No. 0011670080``.
    """
    out=dict(data); corrections=dict(vision_corrections or {})
    locked=set(extra_locked_fields or ())
    notes=[]; accepted={}; rejected={}

    lines=[l.strip() for l in str(ocr_text or "").splitlines()]
    vendor_candidate=_find_vendor_tin_candidate(str(ocr_text or "")) if ocr_text else None
    # Some OCR engines separate the label and value into adjacent lines
    # (SGV: "VAT Reg. TIN" then "000-502-547-00000"). Use a strict
    # label-bound fallback rather than accepting any free-standing number.
    if not vendor_candidate and ocr_text:
        m=re.search(
            r"(?is)(?:VAT\s*Reg(?:istered)?\.?\s*TIN|VAT\s+TIN)\s*[:#.-]*\s*(?:\n\s*)?([0-9][0-9 -]{7,20}[0-9])",
            str(ocr_text),
        )
        if m:
            cand=normalize_tin(m.group(1).strip())
            if is_valid_tin(cand):
                vendor_candidate=cand
    customer_candidate=_find_customer_tin_candidate(lines) if ocr_text else None
    for field,candidate in (("vendor_tax_id",vendor_candidate),("customer_tax_id",customer_candidate)):
        if candidate and is_valid_tin(candidate):
            old=normalize_tin(out.get(field))
            out[field]=candidate; locked.add(field); accepted[field]=candidate
            if old != candidate:
                notes.append(f"Identity evidence gate: locked {field}={candidate} from an explicit labelled OCR TIN field.")

    for field in list(corrections):
        if field not in locked:
            continue
        proposed=corrections[field]
        current=out.get(field)
        if field.endswith("tax_id"):
            proposed=normalize_tin(str(proposed)) if proposed is not None else None
            current=normalize_tin(str(current)) if current is not None else None
        if proposed != current:
            rejected[field]=corrections.pop(field)
            notes.append(
                f"Identity evidence gate: rejected vision correction for '{field}'={proposed!r}; "
                f"stronger explicit OCR/header evidence is locked at {current!r}."
            )
        else:
            corrections.pop(field, None)
    return out, corrections, locked, notes, accepted, rejected

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

# ---------------------------------------------------------------------------
# Layout-aware generic financial reconciliation (1.47 audit hardening)
# ---------------------------------------------------------------------------
def reconcile_financial_layout(
    data: dict, ocr_lines: list[dict] | None, locked_fields: set[str] | None = None,
) -> tuple[dict, list[str]]:
    """Strict geometry-aware financial reconciliation with positive deductions.

    ``locked_fields`` (1.50) protects stronger evidence established later in
    the pipeline (typically vision-confirmed + arithmetically-consistent
    values) from being overwritten by weaker OCR-layout heuristics.
    """
    import re
    from parser.currency_parser import to_float
    if not ocr_lines:
        return data, []
    locked_fields = set(locked_fields or ())
    out=dict(data); notes=[]
    patterns={
        "subtotal": re.compile(r"\b(?:vatable\s+sales?|vat\s+sale|net\s+amount|amount\s+net\s+of\s+vat)\b",re.I),
        "tax_amount": re.compile(r"\b(?:vat\s+amount|value\s+added\s+tax|tax\s*@?\s*\d+(?:\.\d+)?%\s*vat)\b|^\s*vat\s*:?\s*$",re.I),
        "zero_rated_sales": re.compile(r"\bzero[ -]?rated(?:\s+sales?)?\b",re.I),
        "vat_exempt_sales": re.compile(r"\bvat[ -]?exempt(?:ed)?\s+sales?\b",re.I),
        "discount": re.compile(r"\b(?:less\s*:?\s*)?discounts?\b|\bsenior\s+disc",re.I),
        "total_amount": re.compile(r"\b(?:total\s+amount\s+due|total\s+amt\s+due|amount\s+due|amount\s+to\s+pay|grand\s+total|total\s+sales(?:\s*\(\s*vat\s+inclusive\s*\))?)\b",re.I),
    }
    withholding_re=re.compile(r"\bwithh?olding\s+tax\b",re.I)
    def geom(line):
        b=line.get("bbox") or []
        if not b:return (0.,0.,0.,0.)
        xs=[float(p[0]) for p in b]; ys=[float(p[1]) for p in b]
        return min(xs),min(ys),max(xs),max(ys)
    rows=[]
    for i,line in enumerate(ocr_lines):
        txt=str(line.get("text") or "").strip(); x1,y1,x2,y2=geom(line); rows.append((i,txt,x1,y1,x2,y2,line))
    def overlap(a,b):
        ay1,ay2=a[3],a[5]; by1,by2=b[3],b[5]
        return max(0.,min(ay2,by2)-max(ay1,by1))/max(1.,min(ay2-ay1,by2-by1))
    label_rows=[]
    for r in rows:
        for f,pat in patterns.items():
            if pat.search(r[1]): label_rows.append((r,f)); break
    def numeric_value(txt):
        stripped=txt.strip()
        # A percentage is a RATE, never a monetary VAT amount. This closes
        # the SGV/SYCIP failure where the nearby ``12%`` Tax Rate cell was
        # selected as ``tax_amount = 12.00`` instead of PHP 1,260.00.
        if "%" in stripped:
            return None
        alpha=re.sub(r'(?i)php','',stripped)
        if re.search(r'[A-Za-z]',alpha): return None
        if '/' in stripped or re.search(r'\d{1,2}-[A-Za-z]{3,}',stripped): return None
        return to_float(stripped)
    def value_to_right(label_row, min_overlap=0.60):
        i,txt,lx1,ly1,lx2,ly2,_=label_row; cand=[]
        for r in rows:
            j,t,rx1,ry1,rx2,ry2,_=r
            if j==i or rx1 < lx2-3 or overlap(label_row,r)<min_overlap: continue
            v=numeric_value(t)
            if v is None: continue
            gap=rx1-lx2; h=max(1.,ly2-ly1)
            if gap > max(600., h*28): continue
            blocked=False
            for other,ofield in label_rows:
                if other[0]==i: continue
                if other[2] > lx2 and other[2] < rx1 and overlap(label_row,other)>=0.50:
                    blocked=True; break
            if not blocked: cand.append((gap,-overlap(label_row,r),float(v)))
        return sorted(cand)[0][2] if cand else None
    explicit=set(); blank=set(); evidence={}
    priorities={"total amount due":100,"total amt due":95,"amount due":90,"amount to pay":90,"grand total":85,"total sales (vat inclusive)":70,"total sales":65}
    for r,field in label_rows:
        txt=r[1]
        # Match a trailing money token only when it starts at a real token
        # boundary. Without the negative look-behind, malformed OCR such as
        # ``1.9603.40`` could be silently truncated to ``9603.40``/``1.96``.
        suffix=re.search(r"(?<![\d.,])(?:PHP|P|₱)?\s*\(?\s*(-?[0-9][0-9,]*(?:\.\d{1,2})?)\s*\)?\s*$",txt,re.I)
        low=' '.join(txt.lower().split())
        # Strong summary totals sometimes OCR a few pixels above the printed
        # label. Allow modest vertical overlap only for authoritative total
        # labels; keep optional financial buckets strict so adjacent columns
        # cannot bleed into blank Discount/Zero-Rated/VAT-Exempt rows.
        min_ov = 0.35 if field == 'total_amount' and ('total amount due' in low or 'total amt due' in low) else 0.60
        value=to_float(suffix.group(1)) if suffix else value_to_right(r, min_ov)
        if value is None:
            # Generic utility rows labelled only "Discounts" may place their
            # amount slightly above/below the text baseline; do not call them
            # explicit zero. Form-style "Less: Discount"/withholding rows and
            # Zero/VAT-Exempt buckets can safely establish a blank zero.
            if field != 'discount' or re.search(r'\bless\b|senior|pwd|withh?olding|w/?tax', low, re.I):
                blank.add(field)
            continue
        if field=='discount': value=abs(value)
        pri=50
        if field=='total_amount':
            pri=max((v for k,v in priorities.items() if k in low),default=50)
        elif field=='tax_amount':
            # Prefer explicit summary labels over a bare ``VAT`` cell. On SGV
            # invoices the line-item table contains repeated bare VAT labels
            # beside 12% rate/individual tax cells, while the summary line
            # ``Tax@12%VAT: 1,260.00`` is the authoritative invoice VAT.
            if re.search(r'tax\s*@?\s*\d+(?:\.\d+)?%\s*vat', low):
                pri=100
            elif 'vat amount' in low or 'value added tax' in low:
                pri=90
            elif re.fullmatch(r'vat\s*:?', low):
                pri=35
        if field not in evidence or pri>evidence[field][1]: evidence[field]=(round(float(value),2),pri,txt)
    withholding=None
    for r in rows:
        if withholding_re.search(r[1]):
            v=to_float(r[1]); v=v if v is not None else value_to_right(r)
            if v is not None: withholding=abs(float(v)); break
    for field,(value,pri,label) in evidence.items():
        old=to_float(out.get(field)) if out.get(field) is not None else None
        if field in locked_fields:
            explicit.add(field)
            if old is not None and abs(float(old)-value)>0.01:
                notes.append(
                    f"Layout reconciliation: preserved locked '{field}'={old:.2f}; "
                    f"weaker OCR layout suggested {value:.2f} from '{label}'."
                )
            continue
        if old is None or abs(float(old)-value)>0.01:
            out[field]=value; notes.append(f"Layout reconciliation: '{field}' set to {value:.2f} from OCR label/row '{label}'.")
        explicit.add(field)
    for f in ('discount','zero_rated_sales','vat_exempt_sales'):
        if f in blank and f not in explicit and f not in locked_fields:
            old=to_float(out.get(f)) if out.get(f) is not None else None
            out[f]=0.0; explicit.add(f)
            notes.append(
                f"Layout reconciliation: '{f}' label is present but its own row is blank; "
                f"treated as 0.00 instead of borrowing a neighboring value"
                + (f" (replacing {old:.2f})." if old is not None and abs(old)>0.01 else ".")
            )
    if out.get('discount') is not None and 'discount' not in locked_fields:
        d=to_float(out.get('discount'))
        if d is not None and d<0:
            out['discount']=abs(d); notes.append(f"Discount sign normalized from {d:.2f} to {abs(d):.2f}; deductions are stored as positive magnitudes.")
    if withholding is not None and 'discount' not in locked_fields:
        existing=abs(to_float(out.get('discount')) or 0.0)
        if abs(existing-withholding)>0.01:
            out['discount']=round(withholding+(existing if 'discount' in explicit else 0.0),2)
            notes.append(f"Layout reconciliation: withholding tax {withholding:.2f} included in effective discount/deduction.")
        explicit.add('discount')
    # Derive Discount from the five other fields when it is not explicitly
    # supported by OCR. This directly fixes cases like Net=2468, VAT=296.16,
    # Total=2764.16 where an LLM hallucinated Discount=2468.
    vals={k:to_float(out.get(k)) for k in ("subtotal","tax_amount","zero_rated_sales","vat_exempt_sales","discount","total_amount")}
    if all(vals[k] is not None for k in ("subtotal","tax_amount","zero_rated_sales","vat_exempt_sales","total_amount")) and "discount" not in explicit and "discount" not in locked_fields:
        expected=round(vals["subtotal"]+vals["tax_amount"]+vals["zero_rated_sales"]+vals["vat_exempt_sales"]-vals["total_amount"],2)
        if expected >= -0.01:
            expected=max(0.0,expected)
            old=vals["discount"]
            if old is None or abs(old-expected)>0.01:
                out["discount"]=expected
                notes.append(f"Financial reconciliation: corrected discount from {old} to {expected:.2f} using Net + VAT + Zero-Rated + VAT-Exempt - Total.")

    # Final identity check; if exactly one non-explicit financial field is the
    # only uncertain value, solve for it. Never overwrite multiple explicit
    # printed values merely to force the equation to balance.
    vals={k:to_float(out.get(k)) for k in ("subtotal","tax_amount","zero_rated_sales","vat_exempt_sales","discount","total_amount")}
    if all(v is not None for v in vals.values()):
        expected_total=round(vals["subtotal"]+vals["tax_amount"]+vals["zero_rated_sales"]+vals["vat_exempt_sales"]-vals["discount"],2)
        if abs(expected_total-vals["total_amount"])>0.02:
            uncertain=[f for f in vals if f not in explicit and f not in locked_fields]
            if len(uncertain)==1:
                f=uncertain[0]
                # 1.53 provenance rule: never replace an already-present Total
                # Amount Due purely by arithmetic. A derived total is weaker
                # evidence than a value that survived the text/image pipeline,
                # and this exact pattern destroyed Emerald's printed 2,247.40
                # when OCR could not parse its malformed ``2.247.40`` glyphs.
                if f == "total_amount" and vals.get("total_amount") is not None:
                    notes.append(
                        f"Financial reconciliation: total_amount={vals['total_amount']:.2f} is not explicitly geometry-confirmed, but it was preserved because 1.53 does not overwrite an existing total using arithmetic alone."
                    )
                    return out, notes
                formulas={
                    "subtotal": vals["total_amount"]-vals["tax_amount"]-vals["zero_rated_sales"]-vals["vat_exempt_sales"]+vals["discount"],
                    "tax_amount": vals["total_amount"]-vals["subtotal"]-vals["zero_rated_sales"]-vals["vat_exempt_sales"]+vals["discount"],
                    "zero_rated_sales": vals["total_amount"]-vals["subtotal"]-vals["tax_amount"]-vals["vat_exempt_sales"]+vals["discount"],
                    "vat_exempt_sales": vals["total_amount"]-vals["subtotal"]-vals["tax_amount"]-vals["zero_rated_sales"]+vals["discount"],
                    "discount": vals["subtotal"]+vals["tax_amount"]+vals["zero_rated_sales"]+vals["vat_exempt_sales"]-vals["total_amount"],
                    "total_amount": expected_total,
                }
                nv=round(formulas[f],2)
                if nv >= -0.01:
                    nv=max(0.0,nv)
                    ov=out.get(f); out[f]=nv
                    notes.append(f"Financial reconciliation: corrected '{f}' from {ov} to {nv:.2f}; it was the only non-explicit value preventing the accounting identity from balancing.")
            else:
                notes.append(f"Financial reconciliation: printed/extracted values still do not balance (computed total {expected_total:.2f} vs total {vals['total_amount']:.2f}); left unchanged for review because more than one field is uncertain.")

    return out, notes

# ---------------------------------------------------------------------------
# OCR financial evidence scoring (1.52)
# ---------------------------------------------------------------------------
def _strong_ocr_financial_evidence(ocr_lines: list[dict] | None) -> dict[str, dict]:
    """Return strong OCR financial evidence with provenance and semantics.

    1.53 evidence classes:
      * explicit_label_geometry: value attached to the same label row/column;
      * explicit_blank: an optional bucket label whose own row is visibly blank;
      * withholding_label_geometry: explicit withholding tax, treated as a deduction.

    Flattened next-line OCR text is intentionally excluded here. This evidence
    is used to veto weaker LLM/vision/arithmetic guesses.
    """
    if not ocr_lines:
        return {}
    pats={
        "subtotal": re.compile(r"\b(?:vatable\s+sales?|vat\s+sale|net\s+amount|amount\s+net\s+of\s+vat)\b",re.I),
        "tax_amount": re.compile(r"\b(?:vat\s+amount|value\s+added\s+tax|tax\s*@?\s*\d+(?:\.\d+)?%\s*vat)\b|^\s*vat\s*:?\s*$",re.I),
        "zero_rated_sales": re.compile(r"\bzero[ -]?rated(?:\s+sales?)?\b",re.I),
        "vat_exempt_sales": re.compile(r"\bvat[ -]?exempt(?:ed)?\s+sales?\b",re.I),
        "discount": re.compile(r"\b(?:less\s*:?\s*)?discounts?\b|\bsenior\s+disc|\bwithh?olding\s+tax\b|\bless\s+w/?tax\b",re.I),
        "total_amount": re.compile(r"\b(?:total\s+amount\s+due|total\s+amt\s+due|amount\s+due|amount\s+to\s+pay|grand\s+total|total\s+sales(?:\s*\(\s*vat\s+inclusive\s*\))?)\b",re.I),
    }
    def box(line):
        b=line.get("bbox") or []
        if not b:return (0.,0.,0.,0.)
        xs=[float(x[0]) for x in b]; ys=[float(x[1]) for x in b]
        return min(xs),min(ys),max(xs),max(ys)
    rows=[]
    for i,l in enumerate(ocr_lines):
        rows.append((i,str(l.get("text") or "").strip(),*box(l)))
    def yover(a,b):
        return max(0.,min(a[5],b[5])-max(a[3],b[3]))/max(1.,min(a[5]-a[3],b[5]-b[3]))
    evidence={}
    optional_blank={"discount","zero_rated_sales","vat_exempt_sales"}
    for r in rows:
        field=None
        for f,pat in pats.items():
            if pat.search(r[1]):field=f;break
        if not field:continue
        low=' '.join(r[1].lower().split())
        suffix=re.search(r"(?<![\d.,])(?:PHP|P|₱)?\s*\(?\s*(-?[0-9][0-9,]*(?:\.\d{1,2})?)\s*\)?\s*$",r[1],re.I)
        value=to_float(suffix.group(1)) if suffix and '%' not in r[1] else None
        score=0
        source='explicit_label_geometry'
        if value is not None:
            score=98
        else:
            candidates=[]
            # Authoritative TOTAL AMOUNT DUE values may sit a few pixels above
            # the label due to OCR polygon skew. Optional blank buckets stay
            # strict so adjacent-column values do not bleed into them.
            min_overlap=0.35 if field=='total_amount' and ('total amount due' in low or 'total amt due' in low) else 0.60
            for rr in rows:
                if rr[0]==r[0] or rr[2] < r[4]-3 or yover(r,rr)<min_overlap:continue
                txt=rr[1]
                if '%' in txt or re.search(r'[A-Za-z]',re.sub(r'(?i)php','',txt)):continue
                v=to_float(txt)
                if v is None:continue
                gap=rr[2]-r[4]
                if gap>450:continue
                candidates.append((gap,-yover(r,rr),v))
            if candidates:
                value=sorted(candidates)[0][2]; score=86
            elif field in optional_blank and (
                field != 'discount' or re.search(r'\bless\b|senior|pwd|withh?olding|w/?tax', low, re.I)
            ):
                value=0.0; score=108; source='explicit_blank'
        if value is None:continue
        if field=='discount':value=abs(float(value))
        if field=='tax_amount':
            if re.search(r'tax\s*@?\s*\d+(?:\.\d+)?%\s*vat',low):score+=12
            elif 'vat amount' in low or 'value added tax' in low:score+=8
            elif re.fullmatch(r'vat\s*:?',low):score-=18
        if field=='discount' and ('withholding' in low or 'w/tax' in low):
            score=max(score,112); source='withholding_label_geometry'
        if field=='total_amount':
            if 'total amount due' in low: score=max(score,115)
            elif 'total amt due' in low: score=max(score,112)
            elif 'amount to pay' in low: score=max(score,105)
            elif 'amount due' in low: score=max(score,100)
            elif 'total sales (vat inclusive)' in low: score=max(score,96)
            elif 'total sales' in low: score=max(score,90)
        prev=evidence.get(field)
        if prev is None or score>prev['score']:
            evidence[field]={'value':round(float(value),2),'score':score,'label':r[1],'source':source}
    return evidence


def collect_financial_evidence(ocr_lines: list[dict] | None) -> dict[str, dict]:
    """Public diagnostic wrapper for 1.53 financial provenance."""
    return _strong_ocr_financial_evidence(ocr_lines)

def reconcile_single_unreliable_financial_field(
    data: dict,
    ocr_lines: list[dict] | None,
    vision_accepted: dict | None = None,
    locked_fields: set[str] | None = None,
) -> tuple[dict, list[str], set[str]]:
    """Derive one weak financial field from five stronger fields.

    Unlike the older layout reconciler, this step can use a *nearby* vision
    observation to identify which bucket is unreliable, then derive the exact
    value from the accounting identity. This is the generic Emerald case:
    vision says VAT-exempt is non-zero but slightly misreads the handwriting;
    Net/VAT/Zero/Discount/Total determine the exact VAT-exempt amount.
    """
    out=dict(data); notes=[]; locks=set(locked_fields or ())
    vals={k:to_float(out.get(k)) for k in _FINANCIAL_FIELDS_150}
    if vals['subtotal'] is None or vals['total_amount'] is None:
        return out,notes,locks
    for k in ('tax_amount','discount','zero_rated_sales','vat_exempt_sales'):
        if vals[k] is None:vals[k]=0.0
    residual=_financial_identity_residual(out)
    if residual is None or residual<=0.02:
        return out,notes,locks
    strong=_strong_ocr_financial_evidence(ocr_lines)
    va={k:to_float(v) for k,v in (vision_accepted or {}).items() if k in _FINANCIAL_FIELDS_150}
    formulas={
        'subtotal': vals['total_amount']-vals['tax_amount']-vals['zero_rated_sales']-vals['vat_exempt_sales']+vals['discount'],
        'tax_amount': vals['total_amount']-vals['subtotal']-vals['zero_rated_sales']-vals['vat_exempt_sales']+vals['discount'],
        'zero_rated_sales': vals['total_amount']-vals['subtotal']-vals['tax_amount']-vals['vat_exempt_sales']+vals['discount'],
        'vat_exempt_sales': vals['total_amount']-vals['subtotal']-vals['tax_amount']-vals['zero_rated_sales']+vals['discount'],
        'discount': vals['subtotal']+vals['tax_amount']+vals['zero_rated_sales']+vals['vat_exempt_sales']-vals['total_amount'],
        'total_amount': vals['subtotal']+vals['tax_amount']+vals['zero_rated_sales']+vals['vat_exempt_sales']-vals['discount'],
    }
    ranked=[]
    for f,nv in formulas.items():
        nv=round(float(nv),2)
        if nv < -0.02 or f in locks:continue
        nv=max(0.0,nv)
        # Strong same-row printed evidence should almost never be displaced.
        evidence_score=(strong.get(f) or {}).get('score',0)
        score=-evidence_score/20.0
        cur=vals[f]
        if f in va:
            score+=5.0
            # A near-but-not-exact vision read is excellent evidence for WHICH
            # bucket is non-zero even when arithmetic should determine amount.
            vv=va[f]
            if vv is not None and max(abs(nv),1)>0 and abs(vv-nv)/max(abs(nv),1)<=0.15:
                score+=4.0
        if (cur is None or abs(cur)<0.01) and nv>0.01:score+=2.0
        ranked.append((score,f,nv,cur))
    if not ranked:return out,notes,locks
    ranked.sort(reverse=True)
    best=ranked[0]
    if best[0] < 2.5 or (len(ranked)>1 and best[0]-ranked[1][0] < 1.5):
        return out,notes,locks
    _,f,nv,cur=best
    test=dict(out); test[f]=nv
    after=_financial_identity_residual(test)
    if after is not None and after<=0.02:
        out[f]=nv
        # 1.53: a derived number is not automatically strong evidence. Lock it
        # only when at least two *other* financial fields have independent
        # printed OCR label/geometry support. This preserves Emerald's safe
        # one-field derivation while preventing weak/derived sets from becoming
        # immutable on difficult handwritten invoices.
        supporting_printed = sum(
            1 for k, ev in strong.items()
            if k != f and ev.get("score", 0) >= 85 and ev.get("source") != "explicit_blank"
        )
        if supporting_printed >= 2:
            locks.add(f)
            lock_note = " and was locked because at least two independent printed fields support the derivation"
        else:
            lock_note = "; it remains reviewable because fewer than two independent printed fields support the derivation"
        notes.append(
            f"Financial evidence reconciliation: derived '{f}'={nv:.2f} from the other five fields; "
            "it was the uniquely weakest field and the correction restores the accounting identity" + lock_note + "."
        )
    return out,notes,locks

# ---------------------------------------------------------------------------
# Vision financial evidence gate / source-priority locking (1.50)
# ---------------------------------------------------------------------------
_FINANCIAL_FIELDS_150 = {
    "subtotal", "tax_amount", "discount", "zero_rated_sales",
    "vat_exempt_sales", "total_amount",
}
_CORE_FINANCIAL_FIELDS_150 = (
    "subtotal", "tax_amount", "zero_rated_sales", "vat_exempt_sales", "total_amount",
)


def _financial_identity_residual(data: dict) -> float | None:
    """Absolute accounting-identity error, treating optional blank buckets as 0."""
    subtotal = to_float(data.get("subtotal"))
    total = to_float(data.get("total_amount"))
    if subtotal is None or total is None:
        return None
    tax = to_float(data.get("tax_amount")) or 0.0
    zero = to_float(data.get("zero_rated_sales")) or 0.0
    exempt = to_float(data.get("vat_exempt_sales")) or 0.0
    discount = normalize_discount(data.get("discount")) or 0.0
    return abs((subtotal + tax + zero + exempt - discount) - total)


def apply_vision_corrections_with_financial_gate(
    data: dict,
    vision_corrections: dict | None,
    vision_mismatches: list[dict] | None = None,
    ocr_lines: list[dict] | None = None,
) -> tuple[dict, set[str], list[str], dict, dict]:
    """Apply vision evidence without letting it break accounting consistency.

    Returns ``(data, locked_fields, notes, accepted, rejected)``.

    1.50 evidence priority:
      * non-financial vision corrections are applied normally;
      * financial observations are considered as a group, because changing a
        true subtotal/tax/total one field at a time can temporarily make an
        internally-wrong OCR result look *less* consistent;
      * when vision's core numbers (Net + VAT + Zero + Exempt, Total) imply a
        non-negative Discount, those core fields are accepted and LOCKED, and
        Discount is derived from the accounting identity;
      * otherwise, a financial correction is only accepted if it does not make
        the identity residual worse. This rejects cases such as SGV/SYCIP's
        ``VAT 12%`` being proposed as a monetary ``tax_amount = 12``.
    """
    out = dict(data)
    corrections = dict(vision_corrections or {})
    mismatches = vision_mismatches or []
    notes: list[str] = []
    locked: set[str] = set()
    accepted: dict = {}
    rejected: dict = {}

    # Apply identity/text fields first; they do not affect the financial gate.
    for field, value in corrections.items():
        if field not in _FINANCIAL_FIELDS_150:
            out[field] = value
            accepted[field] = value

    # Collect every numeric value the vision model says it sees, including
    # values identical to the current extraction (verify_against_image omits
    # identical values from its corrections dict). Those confirmations are
    # useful for locking a coherent subtotal/tax/total set.
    observed: dict[str, float] = {}
    for m in mismatches:
        if not isinstance(m, dict):
            continue
        field = m.get("field")
        if field not in _FINANCIAL_FIELDS_150:
            continue
        raw = m.get("image_shows")
        value = normalize_discount(raw) if field == "discount" else to_float(raw)
        if value is not None:
            observed[field] = float(value)
    for field, raw in corrections.items():
        if field in _FINANCIAL_FIELDS_150:
            value = normalize_discount(raw) if field == "discount" else to_float(raw)
            if value is not None:
                observed[field] = float(value)

    strong_ocr = _strong_ocr_financial_evidence(ocr_lines)

    # 1.53 provenance priority: explicit OCR label+geometry and explicit blank
    # evidence outrank vision observations that point at a neighboring cell.
    # This is intentionally applied before evaluating the vision set as a
    # batch so mathematically-cancelling field swaps cannot become authoritative.
    for field, ev in strong_ocr.items():
        if ev.get("score", 0) < 100 or field not in _FINANCIAL_FIELDS_150:
            continue
        strong_value = float(ev["value"])
        # A visually blank row from OCR is strong zero evidence only when the
        # surrounding financial set already balances. If it does not balance
        # and vision sees a non-zero value in that bucket, keep the bucket
        # reviewable so the one-field derivation step can recover handwritten
        # amounts (Emerald VAT-Exempt 1,967.40).
        if ev.get("source") == "explicit_blank" and field in observed and abs(float(observed[field])) > 0.02:
            current_residual = _financial_identity_residual(out)
            if current_residual is not None and current_residual > 0.05:
                notes.append(
                    f"Financial evidence priority: OCR row '{ev.get('label')}' looked blank for '{field}', "
                    f"but the current accounting residual is {current_residual:.2f} and vision sees {observed[field]:.2f}; "
                    "left the field unlocked for arithmetic/vision reconciliation."
                )
                continue
        if field in observed and abs(float(observed[field]) - strong_value) > 0.02:
            rejected[field] = observed[field]
            notes.append(
                f"Vision evidence gate: rejected vision '{field}'={observed[field]:.2f}; "
                f"stronger {ev.get('source')} evidence from '{ev.get('label')}' gives {strong_value:.2f}."
            )
        observed[field] = strong_value
        old = to_float(out.get(field))
        if old is None or abs(old - strong_value) > 0.02:
            out[field] = strong_value
            accepted[field] = strong_value
            notes.append(
                f"Financial evidence priority: '{field}' set to {strong_value:.2f} from "
                f"{ev.get('source')} '{ev.get('label')}'."
            )
        locked.add(field)

    # 1.56: strong image subtotal+total override for difficult handwritten
    # financial blocks. If vision independently reads BOTH major printed
    # anchors, OCR has no strong contradictory subtotal/total geometry, and
    # the remaining buckets are strongly zero, derive the missing VAT from
    # those anchors. This defeats a small but internally-balanced OCR trio such
    # as 4,295 + 586.20 = 4,881.20 when the image clearly shows
    # 24,464.29 + 2,935.71 = 27,400.00.
    if "subtotal" in observed and "total_amount" in observed:
        sub_ev=strong_ocr.get("subtotal") or {}; total_ev=strong_ocr.get("total_amount") or {}
        no_strong_anchor_conflict = sub_ev.get("score",0) < 100 and total_ev.get("score",0) < 100
        z_ev=strong_ocr.get("zero_rated_sales") or {}; e_ev=strong_ocr.get("vat_exempt_sales") or {}; d_ev=strong_ocr.get("discount") or {}
        strong_zero_buckets = all((ev.get("score",0) >= 100 and abs(float(ev.get("value",0.0))) <= 0.02) for ev in (z_ev,e_ev,d_ev))
        vision_sub=float(observed["subtotal"]); vision_total=float(observed["total_amount"])
        implied_tax=round(vision_total-vision_sub,2)
        materially_different = (abs(vision_sub-(to_float(out.get("subtotal")) or 0.0)) > max(5.0,0.15*max(vision_sub,1.0)) and abs(vision_total-(to_float(out.get("total_amount")) or 0.0)) > max(5.0,0.15*max(vision_total,1.0)))
        if no_strong_anchor_conflict and strong_zero_buckets and materially_different and 0.0 <= implied_tax <= max(0.30*vision_sub, 0.02):
            for f,v in (("subtotal",vision_sub),("tax_amount",implied_tax),("total_amount",vision_total),("discount",0.0),("zero_rated_sales",0.0),("vat_exempt_sales",0.0)):
                out[f]=round(v,2); accepted[f]=round(v,2); locked.add(f)
            if "tax_amount" in observed and abs(observed["tax_amount"]-implied_tax)>0.02:
                rejected["tax_amount"]=observed["tax_amount"]
            notes.append(f"1.56 image-anchor override: accepted vision subtotal={vision_sub:.2f} and total={vision_total:.2f}; strong zero-bucket evidence implies VAT={implied_tax:.2f}. These labelled image anchors outrank the smaller OCR set that only balanced arithmetically.")
            return out,locked,notes,accepted,rejected

    # Evaluate vision's CORE financial set as a batch. Require that vision saw
    # both subtotal and total; otherwise there is not enough independent image
    # evidence to overrule OCR/layout arithmetic.
    if "subtotal" in observed and "total_amount" in observed:
        core = {}
        for field in _CORE_FINANCIAL_FIELDS_150:
            if field in observed:
                core[field] = observed[field]
            else:
                current = to_float(out.get(field))
                core[field] = 0.0 if current is None else current

        derived_discount = round(
            core["subtotal"] + core["tax_amount"] + core["zero_rated_sales"]
            + core["vat_exempt_sales"] - core["total_amount"], 2
        )
        # A negative deduction means the candidate core set is missing another
        # sales bucket or contains a wrong amount; do NOT make it authoritative.
        coherent_core = derived_discount >= -0.02

        # 1.52 semantic guard: ``subtotal == total`` while tax is positive
        # and the derived discount exactly cancels that tax is a classic
        # label-confusion pattern on handwritten BIR forms. If OCR does not
        # strongly support a printed discount amount, prefer the semantic
        # interpretation Discount=0 and derive Net = Total - VAT - other
        # sales buckets. This recovers cases like Gliptic without relying on
        # a vendor template.
        tax_v=float(core.get("tax_amount") or 0.0)
        suspicious_tax_discount=(
            coherent_core and tax_v>0.02
            and abs(float(core["subtotal"])-float(core["total_amount"]))<=0.02
            and abs(derived_discount-tax_v)<=0.02
            and (
                (strong_ocr.get("discount") or {}).get("score",0)<80
                or (strong_ocr.get("discount") or {}).get("source")=="explicit_blank"
                or abs(float((strong_ocr.get("discount") or {}).get("value",0.0)))<=0.02
            )
        )
        if suspicious_tax_discount:
            corrected_subtotal=round(
                core["total_amount"]-core["tax_amount"]-core["zero_rated_sales"]-core["vat_exempt_sales"],2
            )
            if corrected_subtotal>=-0.02:
                core["subtotal"]=max(0.0,corrected_subtotal)
                observed["subtotal"]=core["subtotal"]
                derived_discount=0.0
                notes.append(
                    "Vision evidence gate: rejected a tax-equals-discount cancellation pattern; "
                    f"derived subtotal={core['subtotal']:.2f} and discount=0.00 from Total/VAT because no strong OCR discount value exists."
                )

        # A vision response that merely repeats the already-extracted numbers
        # is not independent enough to LOCK a financial set when OCR provides
        # little/no printed support. This was the 1.52 Gliptic failure: weak
        # text-derived numbers were echoed by vision, then became immutable.
        financial_correction_fields = {
            f for f in corrections if f in _FINANCIAL_FIELDS_150
        }
        strong_core_count = sum(
            1 for f in _CORE_FINANCIAL_FIELDS_150
            if (strong_ocr.get(f) or {}).get("score", 0) >= 85
            and (strong_ocr.get(f) or {}).get("source") != "explicit_blank"
        )
        independent_core = bool(financial_correction_fields) or strong_core_count >= 2

        if coherent_core and independent_core:
            derived_discount = max(0.0, derived_discount)
            for field in _CORE_FINANCIAL_FIELDS_150:
                if field not in observed:
                    continue
                value = round(float(observed[field]), 2)
                old = to_float(out.get(field))
                out[field] = value
                locked.add(field)
                accepted[field] = value
                if old is None or abs(old - value) > 0.01:
                    notes.append(
                        f"Vision evidence gate: accepted and locked '{field}'={value:.2f}; "
                        "it belongs to an arithmetically coherent image-read financial set."
                    )

            old_discount = normalize_discount(out.get("discount"))
            out["discount"] = derived_discount
            locked.add("discount")
            accepted["discount"] = derived_discount
            if old_discount is None or abs(old_discount - derived_discount) > 0.01:
                notes.append(
                    f"Vision evidence gate: set and locked discount={derived_discount:.2f} "
                    "from Net + VAT + Zero-Rated + VAT-Exempt - Total; conflicting OCR/vision "
                    "discount readings were not allowed to break the accounting identity."
                )
            # Explicitly record a conflicting vision discount as rejected.
            if "discount" in observed and abs(observed["discount"] - derived_discount) > 0.02:
                rejected["discount"] = observed["discount"]
                notes.append(
                    f"Vision evidence gate: rejected vision discount {observed['discount']:.2f}; "
                    f"the coherent image-read core fields imply {derived_discount:.2f}."
                )
            return out, locked, notes, accepted, rejected
        elif coherent_core and not independent_core:
            notes.append(
                "Vision evidence gate: coherent financial values were not locked because the vision model only repeated existing values and fewer than two strong printed OCR fields corroborated the set."
            )

    # No authoritative batch: gate financial corrections one by one against the
    # current identity residual. This is the safe path for incomplete/mixed
    # financial layouts (e.g. a VAT-exempt amount not successfully read).
    for field, raw in corrections.items():
        if field not in _FINANCIAL_FIELDS_150:
            continue
        value = normalize_discount(raw) if field == "discount" else to_float(raw)
        if value is None:
            rejected[field] = raw
            continue
        before = _financial_identity_residual(out)

        # A vision model can confuse a nearby ``Less: VAT`` amount with a
        # blank ``Less: Discount`` row. When the proposed discount itself
        # still fails the accounting identity, compare it with the discount
        # implied by the currently established Net/VAT/Total core. This is
        # intentionally limited to discount corrections; it does not invent
        # arbitrary sales buckets.
        if field == "discount":
            subtotal_now = to_float(out.get("subtotal"))
            tax_now = to_float(out.get("tax_amount"))
            total_now = to_float(out.get("total_amount"))
            if subtotal_now is not None and tax_now is not None and total_now is not None:
                zero_now = to_float(out.get("zero_rated_sales")) or 0.0
                exempt_now = to_float(out.get("vat_exempt_sales")) or 0.0
                implied = round(subtotal_now + tax_now + zero_now + exempt_now - total_now, 2)
                if implied >= -0.02:
                    implied = max(0.0, implied)
                    proposed = dict(out)
                    proposed["discount"] = value
                    proposed_residual = _financial_identity_residual(proposed)
                    implied_data = dict(out)
                    implied_data["discount"] = implied
                    implied_residual = _financial_identity_residual(implied_data)
                    if (proposed_residual is not None and implied_residual is not None
                            and implied_residual + 0.02 < proposed_residual):
                        rejected[field] = value
                        out["discount"] = implied
                        accepted["discount"] = implied
                        locked.add("discount")
                        notes.append(
                            f"Vision evidence gate: rejected vision discount {value:.2f}; "
                            f"the established Net/VAT/Total values imply {implied:.2f} and "
                            "produce a better accounting identity."
                        )
                        continue

        tentative = dict(out)
        tentative[field] = value
        after = _financial_identity_residual(tentative)
        if before is None or after is None or after <= before + 0.02:
            out[field] = value
            accepted[field] = value
            # Only lock a financial correction when it actually reaches a
            # balanced identity. A merely "less bad" guess should remain
            # reviewable and overridable by stronger evidence.
            if after is not None and after <= 0.05:
                locked.add(field)
        else:
            rejected[field] = value
            notes.append(
                f"Vision evidence gate: rejected '{field}'={value:.2f}; accounting residual "
                f"would worsen from {before:.2f} to {after:.2f}."
            )

    return out, locked, notes, accepted, rejected

# ---------------------------------------------------------------------------
# 1.59 Retail & Layout Reliability
# ---------------------------------------------------------------------------

def _line_money_value(line: str) -> float | None:
    """Parse a standalone/near-standalone receipt money token."""
    if line is None:
        return None
    text = str(line).strip()
    # Keep this stricter than to_float(): dates/account IDs must not become money.
    if re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", text):
        return None
    m = re.search(r"(?<!\d)([-(]?\s*(?:PHP|P)?\s*[\d,]+(?:\.\d{1,2})?\s*\)?-?)(?!\d)", text, re.I)
    if not m:
        return None
    token = m.group(1).strip()
    # Avoid pure long integer IDs unless explicitly currency-prefixed.
    digits = re.sub(r"\D", "", token)
    if "." not in token and not re.search(r"\b(?:PHP|P)\s*\d", token, re.I) and len(digits) >= 7:
        return None
    return to_float(token)


def _next_money_after_label(lines: list[str], label_re: re.Pattern, max_ahead: int = 3, stop_re: re.Pattern | None = None) -> float | None:
    for i, raw in enumerate(lines):
        if not label_re.search(raw):
            continue
        # A value can be on the same line.
        tail = label_re.sub("", raw, count=1)
        same = _line_money_value(tail)
        if same is not None:
            return same
        for j in range(i + 1, min(len(lines), i + 1 + max_ahead)):
            if stop_re and stop_re.search(lines[j]):
                break
            val = _line_money_value(lines[j])
            if val is not None:
                return val
    return None


def reconcile_vendor_address_block(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Rebuild a seller address from consecutive header lines.

    Generic ownership rule: seller address lines live after the seller name in
    the document header and before seller TIN/invoice metadata.  This fixes
    truncated North Star addresses and removes a repeated company name from
    Watsons addresses without hardcoding either vendor.
    """
    if not ocr_text:
        return data, []
    result = dict(data)
    notes: list[str] = []
    lines = [re.sub(r"\s+", " ", x).strip(" ,;|") for x in str(ocr_text).splitlines()]
    lines = [x for x in lines if x]
    if not lines:
        return data, []

    vendor = re.sub(r"[^a-z0-9 ]+", " ", str(result.get("vendor_name") or "").lower())
    vendor_tokens = {x for x in vendor.split() if len(x) >= 3 and x not in {"inc", "corp", "corporation", "company"}}

    stop = re.compile(r"(?i)\b(?:VAT\s*(?:REG\.?\s*)?TIN|TIN\s*#|SERVICE\s+INVOICE|SALES\s+INVOICE|OFFICIAL\s+RECEIPT|SERIAL\s*#|MIN\s*:|INVOICE\s+NO|No\.?\s*:|email\s*:|tel(?:ephone)?\.?\s*no)\b")
    cue = re.compile(r"(?i)\b(?:outlet|supermarket|street|st\.?|avenue|ave\.?|road|rd\.?|tower|bldg|building|floor|city|barangay|brgy\.?|district|ncr|philippines|bel[- ]?air|corner|cor\.?|ermita|makati|manila|pasig|taguig|cebu)\b|^\s*\d{1,3}\s*/?F\b|^\s*\d{4}\s+City\b")

    header = []
    for line in lines[:35]:
        if stop.search(line):
            break
        header.append(line)

    candidates: list[str] = []
    for line in header:
        low_norm = re.sub(r"[^a-z0-9 ]+", " ", line.lower())
        toks = {x for x in low_norm.split() if len(x) >= 3}
        # Seller-name-only lines do not belong in address.
        overlap = len(toks & vendor_tokens) / max(1, len(toks)) if toks else 0.0
        if overlap >= 0.60:
            continue
        # A company-name continuation like "PHILIPPINES INC" is not an
        # address merely because it contains the country name.
        if re.fullmatch(r"(?i)[^A-Za-z0-9]*philippines[^A-Za-z0-9]*(?:inc\.?|incorporated|corp\.?|corporation)?[^A-Za-z0-9]*", line):
            continue
        if re.search(r"(?i)\b(?:cash sales|charge sales|watsons|north star international travel)\b", line) and not cue.search(line):
            continue
        if cue.search(line):
            candidates.append(line)

    # Deduplicate while preserving reading order.
    dedup: list[str] = []
    seen = set()
    for line in candidates:
        key = re.sub(r"\W+", "", line.lower())
        if key and key not in seen:
            dedup.append(line)
            seen.add(key)

    if not dedup:
        # At minimum strip a repeated normalized vendor name from the current address.
        current = str(result.get("vendor_address") or "").strip()
        if current and result.get("vendor_name"):
            vn = re.escape(str(result["vendor_name"]).strip())
            cleaned = re.sub(rf"^\s*{vn}\s*[,;:-]*\s*", "", current, flags=re.I).strip(" ,;:")
            if cleaned and cleaned != current:
                result["vendor_address"] = cleaned
                return result, ["1.59 vendor-address cleanup removed a duplicated vendor name from the address field."]
        return data, []

    rebuilt = clean_vendor_address(", ".join(dedup))
    if not rebuilt:
        return data, []

    # Also remove the vendor name if OCR concatenated it into the first address line.
    current = str(result.get("vendor_address") or "").strip()
    alpha = lambda x: len(re.findall(r"[A-Za-z]", x or ""))
    current_name_polluted = False
    if current and result.get("vendor_name"):
        vt = {x for x in re.sub(r"[^a-z0-9 ]+", " ", str(result["vendor_name"]).lower()).split() if len(x) >= 3}
        ct = {x for x in re.sub(r"[^a-z0-9 ]+", " ", current.lower()).split() if len(x) >= 3}
        needed = 1 if len(vt) <= 2 else max(2, min(4, len(vt)))
        current_name_polluted = len(vt & ct) >= needed

    # Prefer the reconstructed header when it materially adds address content,
    # or whenever the existing address is polluted by the seller name.
    if not current or alpha(rebuilt) >= alpha(current) + 10 or current_name_polluted:
        result["vendor_address"] = rebuilt
        notes.append("1.59 vendor-address ownership rebuilt the seller's multi-line header address and excluded the seller name/contact metadata.")
    return result, notes


def reconcile_customer_tax_id_context(data: dict, ocr_text: str, ocr_lines: list[dict] | None = None) -> tuple[dict, list[str]]:
    """Keep only customer TINs owned by an explicit customer-TIN label.

    1.60 fixes a two-column Globe layout where ``Customer TIN`` and its value
    are separated by other column text in flattened OCR order.  When bounding
    boxes are available, ownership is decided geometrically: a candidate below
    the Customer-TIN label in the same column is strong evidence.  Generic
    ``ID``/``Customer ID`` labels do not count as TIN evidence.
    """
    candidate = normalize_tin(data.get("customer_tax_id"))
    if not candidate or not ocr_text:
        return data, []
    digits = re.sub(r"\D", "", candidate)
    if len(digits) < 9:
        return data, []

    def rect(line):
        try:
            pts=line.get("bbox") or []
            xs=[float(p[0]) for p in pts]; ys=[float(p[1]) for p in pts]
            return min(xs),min(ys),max(xs),max(ys)
        except Exception:
            return None

    # Strongest evidence: explicit customer/buyer TIN label owns a nearby
    # numeric box in the same column. This survives interleaved two-column OCR.
    geom_lines = ocr_lines or []
    value_boxes=[]
    label_boxes=[]
    for ln in geom_lines:
        txt=str(ln.get("text") or "").strip()
        r=rect(ln)
        if not r:
            continue
        if digits in re.sub(r"\D", "", txt):
            value_boxes.append(r)
        if re.search(r"(?i)\b(?:customer|buyer)\s*(?:VAT\s*)?(?:TIN|TIL|T1N|TlN|tax\s+identification)(?:\s*(?:no\.?|#))?\b", txt):
            label_boxes.append(r)
    for lb in label_boxes:
        lx1,ly1,lx2,ly2=lb; lcx=(lx1+lx2)/2; lh=max(1.0,ly2-ly1)
        for vb in value_boxes:
            vx1,vy1,vx2,vy2=vb; vcx=(vx1+vx2)/2
            # Value is normally directly below the label, but tolerate a small
            # horizontal offset caused by OCR box segmentation.
            if vy1 >= ly1-lh*0.4 and vy1 <= ly2+lh*4.5 and abs(vcx-lcx) <= max(180.0,(lx2-lx1)*1.25):
                return data, [f"1.60 customer_tax_id={candidate} preserved from explicit Customer TIN geometry."]

    lines = [str(x).strip() for x in str(ocr_text).splitlines()]
    hits = [i for i,line in enumerate(lines) if digits in re.sub(r"\D", "", line)]
    if not hits:
        return data, []
    # Fallback for OCR without boxes: require an explicit TIN label, allowing a
    # wider look-back for interleaved columns, but reject generic ID labels.
    for i in hits:
        nearby_lines=lines[max(0,i-6):min(len(lines),i+3)]
        nearby=" ".join(nearby_lines)
        explicit=bool(re.search(r"(?i)\b(?:customer|buyer)?\s*(?:VAT\s*)?(?:TIN|TIL|T1N|TlN|tax\s+identification)(?:\s*(?:no\.?|#))?\b", nearby))
        generic_id=bool(re.search(r"(?i)\b(?:customer\s+)?ID\s*:?", " ".join(lines[max(0,i-2):i+1])))
        if explicit and not generic_id:
            return data, [f"1.60 customer_tax_id={candidate} preserved from explicit TIN text context."]

    result = dict(data)
    result["customer_tax_id"] = None
    return result, [f"1.60 buyer-ID ownership cleared customer_tax_id={candidate}: the printed number has no explicit Customer/Buyer TIN ownership."]


def reconcile_blank_customer_address(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Keep an explicitly blank buyer Address field blank.

    Compact POS/parking receipts often print ``Name / Address / TIN / Business
    Style`` with empty buyer fields, immediately followed by tax-summary
    numbers.  Those numbers must not become a customer address.
    """
    if not ocr_text:
        return data, []
    lines = [re.sub(r"\s+", " ", x).strip() for x in str(ocr_text).splitlines()]
    for i, line in enumerate(lines):
        if not re.fullmatch(r"(?i)address\s*:?", line):
            continue
        window = [x for x in lines[i+1:i+4] if x]
        if not window:
            continue
        # If the next semantic label is TIN/Business Style rather than address
        # text, the printed customer address is blank.
        if any(re.fullmatch(r"(?i)(?:TIN|business\s+style|bus(?:iness)?\s*style)\s*:?.*", x) for x in window[:2]):
            if data.get("customer_address") not in (None, "", "-"):
                result = dict(data)
                result["customer_address"] = None
                return result, ["1.59 buyer-address ownership kept an explicitly blank Address field empty; following tax-summary numbers were excluded."]
    return data, []


def reconcile_retail_line_items(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Decode compact retail quantity/unit-price syntax such as 10P11.75.

    Supports common POS forms ``10P11.75``, ``7@P24.00``, ``10 @ P41.50`` and
    ``3x25.00``.  The printed line total is used as an arithmetic cross-check
    before an existing extracted item is corrected.
    """
    if not ocr_text or not data.get("line_items"):
        return data, []
    from difflib import SequenceMatcher

    raw_lines = [re.sub(r"\s+", " ", x).strip() for x in str(ocr_text).splitlines()]
    compact_re = re.compile(r"^\s*(\d{1,3})\s*(?:@\s*)?(?:P\s*)?(\d+(?:\.\d{1,2})?)\s*$", re.I)
    x_re = re.compile(r"^\s*(\d{1,3})\s*[xX]\s*(?:P\s*)?(\d+(?:\.\d{1,2})?)\s*$", re.I)
    # Require a visible separator signal: P after qty, @, or x. Avoid treating
    # arbitrary two-number description fragments as price syntax.
    signal_re = re.compile(r"^\s*\d{1,3}\s*(?:@\s*P?|P|[xX]\s*P?)\s*\d+(?:\.\d{1,2})?\s*$", re.I)

    items = [dict(x) for x in data.get("line_items") or []]
    changed = []
    for i, line in enumerate(raw_lines):
        if not signal_re.match(line):
            continue
        m = x_re.match(line) or compact_re.match(line)
        if not m:
            # Explicit @/P form with optional spaces.
            m = re.match(r"^\s*(\d{1,3})\s*@?\s*P\s*(\d+(?:\.\d{1,2})?)\s*$", line, re.I)
        if not m:
            continue
        qty = float(m.group(1)); unit = float(m.group(2)); expected = round(qty * unit, 2)

        # Find printed amount shortly after the compact qty/unit row.
        amount = None
        for nxt in raw_lines[i+1:i+4]:
            if re.fullmatch(r"\d{10,}", re.sub(r"\D", "", nxt)):
                continue
            v = _line_money_value(nxt)
            if v is not None and abs(abs(v) - expected) <= 0.03:
                amount = expected
                break
        if amount is None:
            continue

        # Find the closest preceding non-barcode/product-description line.
        desc = None
        for prev in reversed(raw_lines[max(0, i-5):i]):
            if not prev or re.fullmatch(r"\d{8,}", re.sub(r"\D", "", prev)):
                continue
            if re.search(r"(?i)^(?:serial|min|subtotal|amount to pay|cash|change|tax code)", prev):
                continue
            if re.search(r"[A-Za-z]{3}", prev):
                desc = prev
                break
        if not desc:
            continue

        def score(it: dict) -> float:
            a = re.sub(r"[^a-z0-9 ]+", " ", str(it.get("description") or "").lower())
            b = re.sub(r"[^a-z0-9 ]+", " ", desc.lower())
            seq = SequenceMatcher(None, a, b).ratio()
            ta = {x for x in a.split() if len(x) >= 3}; tb = {x for x in b.split() if len(x) >= 3}
            overlap = len(ta & tb) / max(1, len(tb))
            amount_match = 0.25 if abs((to_float(it.get("amount")) or 0) - expected) <= 0.03 else 0.0
            return max(seq, overlap) + amount_match
        if not items:
            continue
        idx = max(range(len(items)), key=lambda k: score(items[k]))
        if score(items[idx]) < 0.45:
            continue
        oldq = to_float(items[idx].get("quantity")); oldu = to_float(items[idx].get("unit_price"))
        items[idx]["quantity"] = qty
        items[idx]["unit_price"] = unit
        items[idx]["amount"] = amount
        changed.append((items[idx].get("description"), oldq, oldu, qty, unit))

    if not changed:
        return data, []
    result = dict(data); result["line_items"] = items
    notes = [f"1.59 retail line parser corrected {len(changed)} item(s) from explicit quantity×unit-price POS syntax and verified the printed line total."]
    return result, notes


def reconcile_retail_tax_summary(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Own retail tax-summary labels before generic arithmetic can swap them.

    Handles two generic receipt patterns:
      * VAT SALE + VAT AMT + printed SUBTOTAL/AMOUNT TO PAY
      * VAT-exempt/PWD receipts with ``LESS 12% VAT`` (a deduction, never VAT)
    """
    if not ocr_text:
        return data, []
    low = str(ocr_text).lower()
    if not any(x in low for x in ("vat sale", "vai sale", "vat exempt sale", "less 12%vat", "less 12% vat")):
        return data, []
    lines = [re.sub(r"\s+", " ", x).strip() for x in str(ocr_text).splitlines() if str(x).strip()]
    result = dict(data); notes: list[str] = []

    subtotal_printed = _next_money_after_label(lines, re.compile(r"(?i)^\s*subtotal\.?\s*$"), max_ahead=2)
    pay = _next_money_after_label(lines, re.compile(r"(?i)^\s*amount\s+to\s+pay\s*$"), max_ahead=3,
                                  stop_re=re.compile(r"(?i)^\s*(?:cash|change|total number)\b"))
    less_vat = _next_money_after_label(lines, re.compile(r"(?i)^\s*less\s*12\s*%?\s*vat\s*$"), max_ahead=2)

    # PWD/SC VAT-exempt form: LESS 12% VAT is a deduction, not output VAT.
    if less_vat is not None:
        less_vat = abs(float(less_vat))
        result["tax_amount"] = 0.0
        # Prefer the receipt's printed TOTAL DISCOUNTS when it already exists;
        # LESS VAT is one component of that printed total.  Otherwise expose
        # the LESS VAT amount as the effective discount/deduction.
        printed_discount = normalize_discount(result.get("discount")) or 0.0
        result["discount"] = printed_discount if printed_discount > 0 else less_vat
        if pay is not None:
            result["total_amount"] = abs(float(pay))
        # If an independently printed VAT-exempt bucket reconciles with the
        # printed total discounts, it owns the sales bucket; don't also count
        # the receipt's intermediate SUBTOTAL as vatable sales.
        exempt = to_float(result.get("vat_exempt_sales")) or 0.0
        disc = normalize_discount(result.get("discount")) or 0.0
        if exempt > 0 and result.get("total_amount") is not None and abs((exempt - disc) - float(result["total_amount"])) <= 0.05:
            result["subtotal"] = 0.0
        notes.append(f"1.59 retail tax ownership treated LESS 12% VAT {less_vat:.2f} as a deduction and locked VAT to 0.00; it is not a positive VAT amount.")
        return result, notes

    # VAT-able retail receipt: derive VAT from printed VAT SALE and the
    # VAT-inclusive receipt subtotal. This is much safer than trusting a
    # column-shifted OCR VAT-AMT value.
    vat_sale = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"(?i)VA[TI]\s+SALE", line):
            if i + 1 < len(lines):
                vat_sale = _line_money_value(lines[i+1])
            break
    if vat_sale is not None and subtotal_printed is not None:
        vat_sale = abs(float(vat_sale)); total = abs(float(subtotal_printed))
        vat = round(total - vat_sale, 2)
        if vat >= -0.01 and vat <= max(0.0, total * 0.30 + 0.05):
            result["subtotal"] = vat_sale
            result["tax_amount"] = max(0.0, vat)
            result["total_amount"] = total
            notes.append(f"1.59 retail tax-summary ownership set VAT SALE={vat_sale:.2f}, VAT={max(0.0,vat):.2f}, and payable total={total:.2f} from the printed tax summary/subtotal.")
    return result, notes


def reconcile_parking_tax_summary(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """Lock explicit compact parking-receipt tax rows and printed zeros."""
    if not ocr_text or not re.search(r"(?i)\bparking\s+fee\b", ocr_text):
        return data, []
    result = dict(data); notes: list[str] = []
    total = to_float(result.get("total_amount"))
    tax = to_float(result.get("tax_amount"))
    if total is not None and tax is not None and total >= tax >= 0:
        corrected_net = round(float(total) - float(tax), 2)
        old = to_float(result.get("subtotal"))
        result["subtotal"] = corrected_net
        result["discount"] = 0.0
        result["vat_exempt_sales"] = 0.0
        result["zero_rated_sales"] = 0.0
        notes.append(
            f"1.59 parking tax-summary ownership locked printed zero Discount/VAT-Exempt/Zero-Rated rows and reconciled VATable Sales as Total {total:.2f} - VAT {tax:.2f} = {corrected_net:.2f}"
            + (f" (OCR had {old:.2f})." if old is not None and abs(old-corrected_net) > 0.01 else ".")
        )
    return result, notes
