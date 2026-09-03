"""
Calls a local Ollama model (Qwen 2.5 / Llama 3) to turn raw OCR text into
structured invoice JSON.
"""
import time
import requests
from config.settings import settings
from config.logging import get_logger
from ai.prompt_builder import build_extraction_prompt, build_correction_prompt
from ai.validator import validate_extraction
from utils.invoice_template_detector import detect_invoice_template
from utils.helpers import safe_json_loads

logger = get_logger("ai.extractor")


class ExtractionError(Exception):
    pass


def _call_ollama(prompt: str) -> str:
    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": settings.LLM_TEMPERATURE},
    }

    attempts = settings.OLLAMA_TIMEOUT_RETRIES + 1
    last_error = None
    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=settings.OLLAMA_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json().get("response", "")
        except requests.exceptions.Timeout as e:
            elapsed = time.perf_counter() - start
            last_error = e
            logger.warning(
                f"Ollama call timed out after {elapsed:.1f}s "
                f"(attempt {attempt}/{attempts}, limit={settings.OLLAMA_TIMEOUT_SECONDS}s)"
            )
            continue  # timeouts get a fresh retry; connection/HTTP errors below do not
        except requests.RequestException as e:
            raise ExtractionError(f"Ollama request failed: {e}") from e

    raise ExtractionError(
        f"Ollama request timed out after {attempts} attempt(s), "
        f"{settings.OLLAMA_TIMEOUT_SECONDS}s each. The model may be too slow for "
        f"this document size, or may be cold-starting on every call — check "
        f"`ollama ps` to confirm the model is actually staying loaded between "
        f"requests, and consider a smaller/quantized model or raising "
        f"OLLAMA_TIMEOUT_SECONDS further."
    ) from last_error


def extract_invoice_data(ocr_text: str, template_name: str | None = None) -> dict:
    """
    Runs the extraction prompt, validates the result, and retries with a
    correction prompt (up to LLM_MAX_RETRIES) if validation fails.
    """
    if not ocr_text or not ocr_text.strip():
        raise ExtractionError("Empty OCR text — nothing to extract.")

    if template_name is None:
        template_name = detect_invoice_template(ocr_text)
    logger.info(f"Invoice template selected: {template_name}")

    prompt = build_extraction_prompt(ocr_text, template_name=template_name)
    raw_response = _call_ollama(prompt)
    parsed = safe_json_loads(raw_response)

    if parsed is None:
        raise ExtractionError("LLM did not return valid JSON.")

    for attempt in range(settings.LLM_MAX_RETRIES):
        issues = validate_extraction(parsed, template_name=template_name)
        if not issues:
            break
        logger.info(f"Validation issues (attempt {attempt+1}): {issues}")
        correction_prompt = build_correction_prompt(ocr_text, parsed, issues, template_name=template_name)
        raw_response = _call_ollama(correction_prompt)
        corrected = safe_json_loads(raw_response)
        if corrected:
            parsed = corrected

    return parsed
