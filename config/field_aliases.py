"""
Central library of label ALIASES used to recognize an invoice field
regardless of which wording a particular vendor's template happens to
print.

Why this exists: the same canonical field shows up under many different
printed labels depending on the vendor/format. The clearest example is
`subtotal` (our "net amount before tax"), which is confirmed to appear on
real invoices as any of:

    Subtotal | Net Amount | Net of VAT | Amount Net of VAT |
    VATable Sales | VAT Sale | Net Sales | Net Amount (Vatable Sales)

Before this module, these label lists were hard-coded and DUPLICATED
across three different files (ai/post_processing.py, parser/tax_parser.py,
and various template files under ai/invoice_templates/), each with a
slightly different/incomplete list. That meant teaching the system a new
label variant (e.g. "Vatable Sales" vs "VAT Sale") required hunting down
every copy, and it was easy for one file to recognize a label the others
didn't.

This module is the SINGLE source of truth. Every label list below is a
tuple of lowercase substrings matched against a lowercased OCR line (see
ai/post_processing.py::_amounts_near_label / _line_matches_field). Add a
newly-observed vendor wording here ONCE and every consumer (subtotal
reconciliation, tax reconciliation, discount reconciliation, vision
verifier prompts, etc.) picks it up automatically.

Ordering within a tuple doesn't matter for matching (it's just "any of
these substrings"), but longer/more-specific phrases are listed first
for readability.
"""

# ---------------------------------------------------------------------------
# subtotal / net-of-VAT amount ("Net Amount", "VATable Sales", etc.)
#
# Deliberately does NOT include the bare word "subtotal" — on many PH BIR
# receipts the printed "SUBTOTAL" line is VAT-INCLUSIVE (effectively a
# second total), not the net-of-VAT figure this field is meant to hold.
# See ai/post_processing.py::reconcile_subtotal for the full story.
# ---------------------------------------------------------------------------
SUBTOTAL_LABELS = (
    "net amount (vatable sales)",
    "net amount(vatable sales)",
    "amount net of vat",
    "net of vat",
    "vatable sales",
    "vat sale",
    "net sales",
    "net amount",
)

# ---------------------------------------------------------------------------
# total amount due
# ---------------------------------------------------------------------------
TOTAL_LABELS = (
    "grand total",
    "amount due",
    "amount to pay",
    "amount payable",
    "total amount due",
    "total due",
    "total sales (vat inclusive)",
    "total sales",
    "total",
)
# Lines containing any of these must NEVER be treated as a TOTAL line, even
# though they contain the substring "total" (e.g. "Sub Total", "Total
# Discounts") — see ai/post_processing.py::_amounts_near_label `exclude`.
TOTAL_EXCLUDE = ("subtotal", "sub total", "sub-total", "total discount")

# ---------------------------------------------------------------------------
# tax / VAT amount
# ---------------------------------------------------------------------------
TAX_AMOUNT_LABELS = (
    "vat amoun",  # tolerates OCR dropping the trailing "t" before "(12%)"
    "tax amoun",
    "gst amoun",
    "vat:",
    "tax:",
    "gst:",
)
TAX_AMOUNT_EXCLUDE = (
    "vatable", "vat-exempt", "vat exempt", "zero rated", "non-vat",
)

# ---------------------------------------------------------------------------
# zero-rated / VAT-exempt sales columns (PH BIR three-way sales split)
# ---------------------------------------------------------------------------
ZERO_RATED_LABELS = ("zero rated", "zero-rated", "zero rated sales")
VAT_EXEMPT_LABELS = ("vat exempt", "vat-exempt", "exempt sales")

# ---------------------------------------------------------------------------
# discount
#
# A discount is virtually always printed as a DEDUCTION — often shown with
# a trailing minus ("40.15-") or wrapped in parentheses ("(400.00)"), both
# accounting conventions for "subtract this". See
# parser/currency_parser.py::to_float / normalize_discount for how those
# markers get turned into a sign, and ai/post_processing.py::
# reconcile_discount for how these labels are used to fill/verify the
# `discount` field. Regardless of how a vendor prints it, our own
# `discount` field is ALWAYS stored as a positive magnitude — the amount
# to subtract — never negative (see models/invoice.py::validate_totals and
# ai/validator.py, both of which compute `... - discount`). Deliberately
# does NOT include "add-ons"/similar addition-type charges — those go the
# OTHER direction (they increase the total) and must never be confused
# with a discount just because a vendor also prints them in parentheses.
# ---------------------------------------------------------------------------
DISCOUNT_LABELS = (
    "less: discount",
    "less discount",
    "total discounts",
    "discounts",
    "discount",
)

# Split for reconcile_discount()'s two-tier matching (see ai/post_processing.py):
# the phrase-level labels below are specific enough to trust with the loose
# match (next-line fallback + bare-integer amounts). The bare word
# "discount" on its own is too generic for that — it also matches column
# headers with no value, "No Discount Applied", "Senior Citizen/PWD
# Discount ID No. 123456", etc. — so it's kept separate and matched more
# strictly (see DISCOUNT_LABEL_GENERIC below).
DISCOUNT_LABELS_SPECIFIC = (
    "less: discount",
    "less discount",
    "total discounts",
    "discounts",
)
DISCOUNT_LABEL_GENERIC = ("discount",)

# ---------------------------------------------------------------------------
# cash / change (used to catch total_amount getting confused with these)
# ---------------------------------------------------------------------------
CASH_LABELS = ("cash", "tendered", "amount tendered")
CHANGE_LABELS = ("change",)

# ---------------------------------------------------------------------------
# Sales-invoice-pad checkbox labels ("[ ] CASH SALES  [ ] CHARGE SALES") that
# sit right above the actual item table on many PH BIR-registered invoice
# booklets. Confirmed on a real Emerald Mansion Condominium Association
# invoice: the checked box's own label text ("CHARGE SALES") got OCR'd and
# extracted as if it were the description of the ONE line item on the
# invoice, with the invoice's total amount attached to it — i.e. the real
# "utilities"/service description was lost and replaced by the checkbox
# label. See ai/post_processing.py::_looks_like_form_boilerplate_line.
# ---------------------------------------------------------------------------
CHECKBOX_FORM_LABELS = ("cash sales", "charge sales")

# ---------------------------------------------------------------------------
# "*** NOTHING FOLLOWS ***" (and minor OCR variants) is printed by many PH
# invoice/receipt templates to mark the end of the itemized rows — never an
# actual purchased item. Confirmed on a real Gliptic Art Enterprise invoice
# where this printed marker sat right after the genuine line items and got
# extracted as an additional bogus row. See ai/post_processing.py::
# _looks_like_form_boilerplate_line.
# ---------------------------------------------------------------------------
NOTHING_FOLLOWS_LABELS = ("nothing follows", "nothing follow")

# ---------------------------------------------------------------------------
# Printer/booklet-accreditation boilerplate ("Printer's Accreditation No.",
# BIR "Authority To Print"/ATP, "OCN") that sits in the fine print at the
# bottom of PH invoice booklets, right next to a long digit string that is
# NOT the invoice's own number. Confirmed on a real Adel Printing Services-
# printed invoice booklet footer ("Printer's Accreditation No.:
# 032MP20210000000039"), which risks being picked up as invoice_number the
# same way a Transaction#/terminal ID already was (see
# ai/post_processing.py::reconcile_invoice_number).
# ---------------------------------------------------------------------------
PRINTER_ACCREDITATION_LABELS = (
    "printer's accreditation", "printers accreditation", "accreditation no",
    "authority to print", "atp", "ocn",
)

# ---------------------------------------------------------------------------
# Canonical field -> its label tuple, for callers that want to iterate
# every known field rather than importing each tuple by name.
# ---------------------------------------------------------------------------
FIELD_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "subtotal": SUBTOTAL_LABELS,
    "total_amount": TOTAL_LABELS,
    "tax_amount": TAX_AMOUNT_LABELS,
    "zero_rated_sales": ZERO_RATED_LABELS,
    "vat_exempt_sales": VAT_EXEMPT_LABELS,
    "discount": DISCOUNT_LABELS,
}

# ---------------------------------------------------------------------------
# customer / bill-to block
#
# Unlike vendor_name (often just a header/logo with no label at all),
# every real PH invoice explicitly labels its customer block — "BILLED
# TO", "SOLD TO", "CUSTOMER:", "Registered Name:" — which makes an
# explicit-label lookup a reliable, high-confidence signal for this field
# specifically. See ai/post_processing.py::reconcile_customer_name.
# ---------------------------------------------------------------------------
CUSTOMER_LABELS = (
    "billed to", "bill to", "sold to", "customer", "registered name", "client",
)

# Labels that sit WITHIN the customer block itself (address/TIN/etc.) —
# used to skip past a label cluster to reach the actual value line, same
# technique as _VENDOR_LABEL_WORDS.
CUSTOMER_BLOCK_LABEL_WORDS = (
    "tin", "tin no", "business address", "address", "attention",
    "osca/ pwd/sp id no", "terms", "po ref no", "tc:", "a/e:",
)

# ---------------------------------------------------------------------------
# Signals that the value currently sitting in customer_name (or
# vendor_name, via _VENDOR_BOILERPLATE_MARKERS) is actually something
# else entirely mis-picked from elsewhere on the page — a printer/booklet
# footer's own proprietor name, or an unrelated "Nature of Service(s)"
# field value from an engagement-details block — rather than the real
# billed customer. Confirmed on real North Star International Travel
# (customer_name became the PRINTING PRESS's proprietress, "Mylene
# Canlas-Proprietres", off the booklet footer) and SGV (customer_name
# became "Business Tax Services", the value of an unrelated "Nature of
# Services:" field) invoices.
# ---------------------------------------------------------------------------
CUSTOMER_UNTRUSTED_CONTEXT_MARKERS = (
    "proprietress", "proprietor", "prop.", "nature of service",
    # Signature-block boilerplate at the BOTTOM of a PH invoice/receipt —
    # confirmed picked up as customer_name outright on real North Star
    # International Travel invoices ("Customer Authorized Representative")
    # and a real Gliptic Art Enterprise invoice ("Customer Signature Over
    # Printed Name"). Both phrases sit right next to (often on the very
    # same printed line as) the word "Customer", which is exactly why a
    # naive "look for a value near a Customer-ish label" pass can mistake
    # the signature-block CAPTION itself for the customer's name — the
    # real name ("Tsukiden Global Solutions Inc.") sits several lines
    # ABOVE, next to "BILLED TO"/"SOLD TO"/"Registered Name" instead.
    "authorized representative", "signature over printed name",
    "conforme and received by",
) + PRINTER_ACCREDITATION_LABELS

# ---------------------------------------------------------------------------
# TIN (Tax Identification Number) labels — separated into vendor vs
# customer variants because they anchor to DIFFERENT parts of a PH invoice
# and getting that anchor wrong is exactly how vendor_tax_id and
# customer_tax_id end up swapped (or either one ends up holding the
# invoice-BOOKLET PRINTER's own registration instead of either party's).
#
# "VAT Reg. TIN" (and close variants) is confirmed, across every single
# real invoice sample seen so far regardless of vendor, to ALWAYS sit in
# the vendor's own header block near the top of the page — never the
# customer's. "TIN NO" / "Customer TIN" / bare "TIN:" sitting within the
# BILLED TO/SOLD TO/Registered Name block (see CUSTOMER_LABELS above) is
# the customer's. A bare "TIN:" or "VAT REG. TIN" sitting instead next to
# PRINTER_ACCREDITATION_LABELS boilerplate (e.g. "Macan PrintShop VAT REG.
# TIN # 908-260-302-00000") belongs to neither party — confirmed on a real
# North Star International Travel invoice where that exact printer's-
# booklet TIN was picked up as customer_tax_id instead of the real
# customer's "007-848-122-000" printed a few lines above under "TIN NO".
# ---------------------------------------------------------------------------
VENDOR_TIN_LABELS = ("vat reg. tin", "vat reg tin", "vat registration tin", "vendor tin", "vendor vat/tin")
CUSTOMER_TIN_LABELS = ("customer tin", "customer vat/tin", "client tin", "client vat/tin", "buyer tin", "tin no", "tin")


def labels_for(field: str) -> tuple[str, ...]:
    """Return the known label aliases for a canonical field name, or an
    empty tuple if the field has no registered aliases."""
    return FIELD_LABEL_ALIASES.get(field, ())
