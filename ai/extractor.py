"""Calls local Ollama text LLM to turn OCR text into structured invoice JSON.

This version also records a structured trace of every generation/correction
attempt when a mutable ``trace`` dict is supplied by the caller.
"""
from __future__ import annotations

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


def _usage_from_ollama(body: dict) -> dict:
    return {
        "prompt_eval_count": body.get("prompt_eval_count"),
        "eval_count": body.get("eval_count"),
        "total_duration": body.get("total_duration"),
        "load_duration": body.get("load_duration"),
        "prompt_eval_duration": body.get("prompt_eval_duration"),
        "eval_duration": body.get("eval_duration"),
    }


def _call_ollama(prompt: str, trace_attempt: dict | None = None) -> str:
    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",  # strongly reduces prose/fenced-code invalid-JSON failures
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": settings.LLM_TEMPERATURE},
    }

    if trace_attempt is not None:
        trace_attempt.update({
            "endpoint": "/api/generate",
            "model": settings.OLLAMA_MODEL,
            "prompt": prompt,
            "request_format": "json",
        })

    attempts = settings.OLLAMA_TIMEOUT_RETRIES + 1
    last_error = None
    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=settings.OLLAMA_TIMEOUT_SECONDS)
            elapsed = time.perf_counter() - start
            if trace_attempt is not None:
                trace_attempt["http_status"] = resp.status_code
                trace_attempt["elapsed_seconds"] = round(elapsed, 3)
            resp.raise_for_status()
            body = resp.json()
            raw = body.get("response", "")
            if trace_attempt is not None:
                trace_attempt["raw_response"] = raw
                trace_attempt["usage"] = _usage_from_ollama(body)
            return raw
        except requests.exceptions.Timeout as e:
            elapsed = time.perf_counter() - start
            last_error = e
            if trace_attempt is not None:
                trace_attempt.setdefault("timeout_attempts", []).append({"attempt": attempt, "elapsed_seconds": round(elapsed, 3)})
            logger.warning(
                f"Ollama call timed out after {elapsed:.1f}s "
                f"(attempt {attempt}/{attempts}, limit={settings.OLLAMA_TIMEOUT_SECONDS}s)"
            )
            continue
        except requests.RequestException as e:
            if trace_attempt is not None:
                trace_attempt["error"] = str(e)
                response = getattr(e, "response", None)
                if response is not None:
                    trace_attempt["response_body"] = response.text[:4000]
            raise ExtractionError(f"Ollama request failed: {e}") from e

    raise ExtractionError(
        f"Ollama request timed out after {attempts} attempt(s), "
        f"{settings.OLLAMA_TIMEOUT_SECONDS}s each."
    ) from last_error


def extract_invoice_data(ocr_text: str, template_name: str | None = None, trace: dict | None = None) -> dict:
    """Run extraction + validation/correction loop.

    ``trace`` is optional and backward-compatible. When provided it is
    populated in-place, including raw LLM responses, parsed JSON, validation
    issues and Ollama token counts for every attempt.
    """
    if trace is None:
        trace = {}
    trace.setdefault("model", settings.OLLAMA_MODEL)
    trace.setdefault("attempts", [])

    if not ocr_text or not ocr_text.strip():
        trace["error"] = "Empty OCR text"
        raise ExtractionError("Empty OCR text — nothing to extract.")

    if template_name is None:
        template_name = detect_invoice_template(ocr_text)
    trace["template_name"] = template_name
    logger.info(f"Invoice template selected: {template_name}")

    prompt = build_extraction_prompt(ocr_text, template_name=template_name)
    initial = {"kind": "initial"}
    trace["attempts"].append(initial)
    raw_response = _call_ollama(prompt, initial)
    parsed = safe_json_loads(raw_response)
    initial["parsed_json"] = parsed

    if parsed is None:
        trace["error"] = "LLM did not return valid JSON"
        raise ExtractionError("LLM did not return valid JSON.")

    for attempt in range(settings.LLM_MAX_RETRIES):
        issues = validate_extraction(parsed, template_name=template_name, ocr_text=ocr_text)
        trace.setdefault("validation_history", []).append({"attempt": attempt, "issues": list(issues)})
        if not issues:
            break
        logger.info(f"Validation issues (attempt {attempt+1}): {issues}")
        correction_prompt = build_correction_prompt(ocr_text, parsed, issues, template_name=template_name)
        corr_trace = {"kind": "correction", "correction_number": attempt + 1, "issues_given_to_model": list(issues)}
        trace["attempts"].append(corr_trace)
        raw_response = _call_ollama(correction_prompt, corr_trace)
        corrected = safe_json_loads(raw_response)
        corr_trace["parsed_json"] = corrected
        if corrected:
            parsed = corrected

    trace["final_parsed_json"] = parsed
    return parsed
