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

This module is a best-effort BONUS signal, not a hard dependency:
  - Off by default (settings.VISION_VERIFICATION_ENABLED).
  - Fails open — any error (model not pulled, Ollama unreachable, bad JSON
    back, timeout) is logged and treated as "no issues found", never raised.
    Vision verification going down must never block invoice processing.
"""
import base64
from pathlib import Path

import requests

from config.logging import get_logger
from config.settings import settings
from ai.prompt_builder import build_vision_verification_prompt
from utils.helpers import safe_json_loads
from utils.pdf_utils import pdf_to_images, is_pdf

logger = get_logger("ai.vision_verifier")

# Deliberately narrow: only the fields where a misread is actually costly
# enough to justify a second, slower model call (see scoping discussion).
# Money fields drive real financial impact; invoice_number/vendor_name feed
# the duplicate-detection and vendor-matching logic elsewhere in the app.
VISION_CHECK_FIELDS = ["invoice_number", "vendor_name", "subtotal", "tax_amount", "total_amount"]


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


def verify_against_image(file_path: str, extracted: dict) -> list[str]:
    """
    Sends the source image + the already-extracted VISION_CHECK_FIELDS
    values to settings.VISION_MODEL, and asks it to flag any it believes
    are wrong based on what's actually shown in the image.

    Returns a list of human-readable issue strings in the same style as
    ai.validator.validate_extraction, so they plug directly into the
    existing issues list -> confidence-scoring / needs_review pipeline
    (see parser/invoice_parser.py). Returns [] on any failure or when
    verification is disabled — never raises.
    """
    if not settings.VISION_VERIFICATION_ENABLED:
        return []

    image_b64 = _image_to_base64(file_path)
    if not image_b64:
        return []

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
        return []

    parsed = safe_json_loads(raw)
    if not isinstance(parsed, dict):
        logger.warning("Vision model did not return valid JSON, skipping verification.")
        return []

    mismatches = parsed.get("mismatches", [])
    if not isinstance(mismatches, list):
        return []

    issues = []
    for m in mismatches:
        if not isinstance(m, dict):
            continue
        field = m.get("field")
        if field not in VISION_CHECK_FIELDS:
            continue  # ignore hallucinated field names, per the prompt's rule 4
        seen = m.get("image_shows", "?")
        note = m.get("note", "")
        issues.append(
            f"Vision check: '{field}' extracted as '{fields_to_check.get(field)}' but the image "
            f"appears to show '{seen}'." + (f" ({note})" if note else "")
        )
    return issues
