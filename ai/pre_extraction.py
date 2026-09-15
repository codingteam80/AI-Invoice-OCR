"""
Deterministic, regex-only pre-extraction pass that runs BEFORE the LLM is
even called.

Produces a small set of high-confidence "candidate" values for
well-structured fields (vendor/customer TIN, total, subtotal, tax) straight
from known label patterns in the OCR text — reusing the EXACT SAME finder
functions and shared label library (config/field_aliases.py) already proven
out by the reconcile_* backstops in ai/post_processing.py. Nothing here is a
second, parallel implementation of that matching logic; it's the same code,
called earlier.

These candidates are NOT used to skip the LLM, and are NEVER written
straight into the final result. They're injected into the extraction
prompt as cross-checkable anchors (see ai/prompt_builder.py), because
prompt wording ALONE has repeatedly proven insufficient to stop the LLM
hallucinating a value it never actually read off the page — see the many
reconcile_* docstrings in ai/post_processing.py, each describing a REAL
invoice where an explicit prompt rule already existed and the LLM still
got it wrong. Handing the LLM a specific, page-linked candidate to verify
("does the OCR text actually show 007-848-122-000 as the customer's TIN?")
is a fundamentally different, stronger ask than a general rule it has to
remember to apply correctly on its own to whatever this particular page
looks like. The post-processing reconcile_* backstops still run
afterwards regardless — this is an additional, earlier line of defense,
not a replacement for them.

Scope and honest limits:
- Helps most on printed, legible invoices where OCR read the label text
  correctly but its own column/row layout is scrambled (confirmed
  real cases: TRI-Q, North Star, SGV) — exactly the kind of thing a
  label-proximity regex is good at cutting through.
- Does NOT help when the underlying OCR text is itself garbled/illegible
  for the relevant section (confirmed real cases: Emerald Mansion,
  Gliptic Art's handwritten portions) — there is no pattern to find in
  text that was never read correctly in the first place. That failure
  mode needs fixing further upstream (better image preprocessing / a
  stronger handwriting-capable OCR engine), not here.
- Only ever proposes a value for fields with a reliable, narrow label
  pattern (TINs, total, subtotal, tax, an unlabeled business-suffix
  vendor name). Deliberately does NOT attempt line_items, dates, or
  customer_name here — those depend on document layout / context in
  ways a handful of regexes can't reliably anchor to across arbitrary
  invoice templates, and a wrong hint for something the LLM would
  otherwise have gotten right is worse than no hint at all.
"""
from ai.post_processing import (
    _find_vendor_tin_candidate,
    _find_customer_tin_candidate,
    _amounts_near_label,
    _first_business_suffix_line,
)
from config.field_aliases import (
    TOTAL_LABELS,
    TOTAL_EXCLUDE,
    SUBTOTAL_LABELS,
    TAX_AMOUNT_LABELS,
    TAX_AMOUNT_EXCLUDE,
)


def pre_extract_hints(ocr_text: str) -> dict:
    """
    Returns a dict of {field_name: candidate_value} for whichever of the
    narrow, high-confidence fields above a label-proximity match was found
    for. Missing keys simply mean no confident candidate was found — that
    is the common, expected case for most fields on most invoices, not an
    error. Never raises: a malformed/empty ocr_text just yields {}.
    """
    if not ocr_text or not ocr_text.strip():
        return {}

    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    hints: dict = {}

    vendor_tin = _find_vendor_tin_candidate(ocr_text)
    if vendor_tin:
        hints["vendor_tax_id"] = vendor_tin

    customer_tin = _find_customer_tin_candidate(lines)
    if customer_tin:
        hints["customer_tax_id"] = customer_tin

    totals = _amounts_near_label(ocr_text, TOTAL_LABELS, exclude=TOTAL_EXCLUDE)
    if totals:
        hints["total_amount"] = totals[0]

    subtotals = _amounts_near_label(ocr_text, SUBTOTAL_LABELS, allow_bare_integers=True)
    if subtotals:
        hints["subtotal"] = subtotals[0]

    taxes = _amounts_near_label(ocr_text, TAX_AMOUNT_LABELS, exclude=TAX_AMOUNT_EXCLUDE, allow_bare_integers=True)
    if taxes:
        hints["tax_amount"] = taxes[0]

    vendor_name = _first_business_suffix_line(lines)
    if vendor_name:
        hints["vendor_name"] = vendor_name

    return hints
