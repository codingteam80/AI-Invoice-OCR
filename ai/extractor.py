"""
Calls a local Ollama model (Qwen 2.5 / Llama 3) to turn raw OCR text into
structured invoice JSON.
"""
import requests
from config.settings import settings
from config.logging import get_logger
from ai.prompt_builder import build_extraction_prompt, build_correction_prompt
from ai.validator import validate_extraction
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
        "options": {"temperature": settings.LLM_TEMPERATURE},
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.RequestException as e:
        raise ExtractionError(f"Ollama request failed: {e}") from e


def extract_invoice_data(ocr_text: str) -> dict:
    """
    Runs the extraction prompt, validates the result, and retries with a
    correction prompt (up to LLM_MAX_RETRIES) if validation fails.
    """
    if not ocr_text or not ocr_text.strip():
        raise ExtractionError("Empty OCR text — nothing to extract.")

    prompt = build_extraction_prompt(ocr_text)
    raw_response = _call_ollama(prompt)
    parsed = safe_json_loads(raw_response)

    if parsed is None:
        raise ExtractionError("LLM did not return valid JSON.")

    for attempt in range(settings.LLM_MAX_RETRIES):
        issues = validate_extraction(parsed)
        if not issues:
            break
        logger.info(f"Validation issues (attempt {attempt+1}): {issues}")
        correction_prompt = build_correction_prompt(ocr_text, parsed, issues)
        raw_response = _call_ollama(correction_prompt)
        corrected = safe_json_loads(raw_response)
        if corrected:
            parsed = corrected

    return parsed
