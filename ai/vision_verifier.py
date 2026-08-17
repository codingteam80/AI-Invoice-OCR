"""
Independent cross-check of extracted invoice data against the source IMAGE
itself, using a vision-capable local Ollama model (see settings.VISION_MODEL,
default qwen2.5vl:7b).

Why this exists: ai/extractor.py's validation/correction loop can only check
whether the LLM's output is internally self-consistent (does the math add
up, are required fields present) — it never has access to the original
image, only the OCR engine's (possibly wrong) text transcription of it. If
the OCR misreads a digit, the text-only pipeline has no way to notice. A
model that can actually see the document does.

Behavior: when a mismatch is found and the vision model's reading can be
parsed into the field's proper type, that value is AUTO-APPLIED to the
invoice — see verify_against_image's `corrections` return value and its use
in parser/invoice_parser.py. This is not a silent, unreviewed change: every
correction also produces a note (in the second return value), which flows
into invoice.vision_notes AND into the same `issues` list that drives
needs_review — so an auto-corrected invoice always still lands in
needs_review for a human to confirm before it can be locked (see
ai/confidence.py:score_extraction, ui/pages/History.py lock workflow).

This module is a best-effort BONUS signal, not a hard dependency:
  - Off by default (settings.VISION_VERIFICATION_ENABLED).
  - Fails open — any error (model not pulled, Ollama unreachable, bad JSON
    back, timeout) is logged and treated as "no issues found, no
    corrections", never raised. Vision verification going down must never
    block invoice processing.
"""
import base64
from pathlib import Path

import requests

from config.logging import get_logger
from config.settings import settings
from ai.prompt_builder import build_vision_verification_prompt
from parser.currency_parser import to_float
from utils.helpers import safe_json_loads
from utils.pdf_utils import pdf_to_images, is_pdf

logger = get_logger("ai.vision_verifier")

# Deliberately narrow: only the fields where a misread is actually costly
# enough to justify a second, slower model call (see scoping discussion).
# Money fields drive real financial impact; invoice_number/vendor_name feed
# the duplicate-detection and vendor-matching logic elsewhere in the app.
VISION_CHECK_FIELDS = ["invoice_number", "vendor_name", "subtotal", "tax_amount", "total_amount"]

# Fields that need numeric parsing (image_shows comes back as text either
# way, e.g. "1,200.00") before they can be written back onto the invoice.
_MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}


def _image_to_base64(file_path: str) -> str | None:
    """Returns base64-encoded image bytes for the given file, rendering the
    first page if it's a PDF. Returns None if it can't be read — caller
    treats that as "skip verification", not an error."""
    try:
        path = file_path
        if is_pdf(file_path):
            pages = pdf_to_images(file_path)
            if not pages:
                return None
            path = pages[0]
        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
    except Exception as e:
        logger.warning(f"Could not read {file_path} for vision verification: {e}")
        return None


def verify_against_image(file_path: str, extracted: dict) -> tuple[dict, list[str]]:
    """
    Sends the source image + the already-extracted VISION_CHECK_FIELDS
    values to settings.VISION_MODEL, and asks it to flag any it believes
    are wrong based on what's actually shown in the image.

    Returns (corrections, notes):
      - corrections: {field: new_value} for every mismatch whose
        image_shows value could be parsed into the field's proper type.
        The caller (parser/invoice_parser.py) applies these directly to the
        invoice — this is an auto-replace, not just a flag.
      - notes: human-readable strings, one per mismatch (whether or not it
        was auto-applied), in the same style as ai.validator.validate_extraction.
        These flow into both invoice.vision_notes and the shared `issues`
        list that drives needs_review, so an auto-corrected invoice is never
        silently treated as trustworthy — see module docstring.

    Returns ({}, []) on any failure or when verification is disabled — never raises.
    """
    if not settings.VISION_VERIFICATION_ENABLED:
        return {}, []

    image_b64 = _image_to_base64(file_path)
    if not image_b64:
        return {}, []

    fields_to_check = {f: extracted.get(f) for f in VISION_CHECK_FIELDS}
    prompt = build_vision_verification_prompt(fields_to_check)

    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": settings.LLM_TEMPERATURE},
    }
    try:
        resp = requests.post(url, json=payload, timeout=settings.VISION_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw = resp.json().get("response", "")
    except requests.RequestException as e:
        logger.warning(f"Vision verification call failed, skipping: {e}")
        return {}, []

    parsed = safe_json_loads(raw)
    if not isinstance(parsed, dict):
        logger.warning("Vision model did not return valid JSON, skipping verification.")
        return {}, []

    mismatches = parsed.get("mismatches", [])
    if not isinstance(mismatches, list):
        return {}, []

    corrections: dict = {}
    notes: list[str] = []
    for m in mismatches:
        if not isinstance(m, dict):
            continue
        field = m.get("field")
        if field not in VISION_CHECK_FIELDS:
            continue  # ignore hallucinated field names, per the prompt's rule 4
        seen = m.get("image_shows", "?")
        note = m.get("note", "")
        old_value = fields_to_check.get(field)

        new_value = to_float(seen) if field in _MONEY_FIELDS else (str(seen).strip() or None)
        if new_value is not None:
            corrections[field] = new_value
            notes.append(
                f"Vision check: auto-corrected '{field}' from '{old_value}' to '{new_value}' "
                f"— still needs review." + (f" ({note})" if note else "")
            )
        else:
            # Couldn't parse the vision model's reading into a usable value
            # (e.g. it returned something like "unclear" or empty) — leave
            # the original value in place, just flag it like before.
            notes.append(
                f"Vision check: '{field}' extracted as '{old_value}' but the image "
                f"appears to show '{seen}' (could not auto-apply — please verify manually)."
                + (f" ({note})" if note else "")
            )
    return corrections, notes
