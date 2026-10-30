"""LLM transport helpers: local Ollama or OpenRouter (e.g. ``openrouter/free``).

The rest of the pipeline (prompts, validation, post-processing) is unchanged;
only the HTTP layer differs. Provider selection lives in ``config.settings``:

    LLM_PROVIDER=openrouter   -> OpenAI-compatible /chat/completions
    LLM_PROVIDER=ollama       -> local /api/generate (original behaviour)

If LLM_PROVIDER is not set, the hosted API is used whenever LLM_API_KEY is
present, otherwise Ollama.
"""
from __future__ import annotations

import random
import re
import time

from ai.processing_budget import remaining_timeout, budget_sleep, ModelBudgetExceeded
import requests

from config.logging import get_logger
from config.settings import settings
from utils.helpers import safe_json_loads

logger = get_logger("ai.llm_client")


_cooldown_until = 0.0  # monotonic time before which OpenRouter is skipped


def label() -> str:
    """Human-readable name of the configured API, e.g. ``LLM API (api.groq.com)``."""
    from urllib.parse import urlparse
    host = urlparse(settings.OPENROUTER_BASE_URL).netloc or settings.OPENROUTER_BASE_URL
    return f"LLM API ({host})"


def provider_name():
    """Actual API host identity; 'openrouter' remains the transport selector."""
    from urllib.parse import urlparse
    host = urlparse(settings.OPENROUTER_BASE_URL).hostname or 'unknown-api'
    return {'openrouter.ai': 'openrouter', 'api.groq.com': 'groq'}.get(host, host)


# --- Engine mode (switchable at runtime from the UI sidebar) ------------------
#   api_only    -> hosted API only, no Ollama fallback
#   ollama_only -> local Ollama only, the API is never called
#   both        -> API first, local Ollama as automatic fallback
MODES = ("api_only", "ollama_only", "both")
MODE_LABELS = {
    "api_only": "API only",
    "ollama_only": "Ollama only (local)",
    "both": "Both (API + Ollama fallback)",
}
_mode_override: str | None = None


def default_mode() -> str:
    """Mode implied by .env (LLM_PROVIDER / LLM_FALLBACK_TO_OLLAMA)."""
    if settings.LLM_PROVIDER != "openrouter":
        return "ollama_only"
    return "both" if settings.LLM_FALLBACK_TO_OLLAMA else "api_only"


def _mode_file():
    return settings.JSON_DIR / "llm_mode.json"


def get_mode() -> str:
    return _mode_override if _mode_override in MODES else default_mode()


def set_mode(mode: str, *, persist: bool = True) -> None:
    """Switch engine mode for the whole process (and remember it across restarts)."""
    global _mode_override
    if mode not in MODES:
        raise ValueError(f"Unknown LLM mode {mode!r}; expected one of {MODES}")
    if mode != get_mode():
        reset_cooldown()
    _mode_override = mode
    if persist:
        try:
            import json
            _mode_file().write_text(json.dumps({"mode": mode}), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Could not save LLM mode: {exc}")


def load_saved_mode() -> None:
    """Restore the mode chosen in the UI before the last restart (no-op if none)."""
    global _mode_override
    if _mode_override is not None:
        return
    try:
        import json
        saved = json.loads(_mode_file().read_text(encoding="utf-8")).get("mode")
        if saved in MODES:
            _mode_override = saved
    except (OSError, ValueError):
        pass


def clear_mode_override() -> None:
    global _mode_override
    _mode_override = None


def api_configured() -> bool:
    return bool(settings.OPENROUTER_API_KEY)


def using_openrouter() -> bool:
    """True when the hosted API may be used (mode api_only or both)."""
    return get_mode() != "ollama_only"


def fallback_enabled() -> bool:
    return get_mode() == "both"


def _status_of(exc) -> int | None:
    seen = 0
    while exc is not None and seen < 4:  # follow __cause__ (ExtractionError etc.)
        resp = getattr(exc, "response", None)
        if resp is not None and getattr(resp, "status_code", None):
            return int(resp.status_code)
        exc = getattr(exc, "__cause__", None)
        seen += 1
    return None


def note_openrouter_failure(exc_or_status=None) -> None:
    """Start a cooldown so following calls go straight to the fallback."""
    global _cooldown_until
    status = exc_or_status if isinstance(exc_or_status, int) else _status_of(exc_or_status)
    long_lived = status in (401, 402, 403) or "API_KEY is not set" in str(exc_or_status)
    seconds = settings.OPENROUTER_COOLDOWN_SECONDS if long_lived else settings.OPENROUTER_SHORT_COOLDOWN_SECONDS
    _cooldown_until = time.monotonic() + max(0, seconds)
    logger.warning(
        f"{label()} failed (status={status}); using local Ollama for the next {seconds}s"
        if fallback_enabled() else f"{label()} failed (status={status})"
    )


def reset_cooldown() -> None:
    global _cooldown_until
    _cooldown_until = 0.0


def pick_provider() -> str:
    """Provider to try FIRST for the next request: "openrouter" or "ollama"."""
    if not using_openrouter():
        return "ollama"
    if fallback_enabled() and time.monotonic() < _cooldown_until:
        return "ollama"
    return "openrouter"


def active_text_model() -> str:
    return settings.OPENROUTER_MODEL if pick_provider() == "openrouter" else settings.OLLAMA_MODEL


def active_search_model() -> str:
    return settings.OPENROUTER_SEARCH_MODEL if pick_provider() == "openrouter" else settings.SEARCH_LLM_MODEL


def active_vision_model() -> str:
    return settings.OPENROUTER_VISION_MODEL if pick_provider() == "openrouter" else settings.VISION_MODEL


def openrouter_url() -> str:
    return settings.OPENROUTER_BASE_URL.rstrip("/") + "/chat/completions"


def openrouter_headers() -> dict:
    key = settings.OPENROUTER_API_KEY
    if not key:
        raise requests.RequestException(
            "LLM_API_KEY is not set. Add LLM_API_KEY, LLM_BASE_URL and LLM_MODEL to your .env file."
        )
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if settings.OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
    if settings.OPENROUTER_APP_TITLE:
        headers["X-Title"] = settings.OPENROUTER_APP_TITLE
    return headers


def _image_mime(image_b64: str) -> str:
    if image_b64.startswith("/9j/"):
        return "image/jpeg"
    if image_b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def chat_payload(
    prompt: str,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool = True,
    image_b64: str | None = None,
) -> dict:
    """Build an OpenAI-style chat request (text-only or text + one image)."""
    if image_b64:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{_image_mime(image_b64)};base64,{image_b64}"}},
        ]
    else:
        content = prompt
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if settings.LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = settings.LLM_REASONING_EFFORT
    return payload


def parse_chat_response(body: dict) -> tuple[str, dict]:
    """Return (text, usage) from an OpenRouter chat completion body.

    OpenRouter can answer HTTP 200 with an ``error`` object and no choices
    (e.g. an upstream free provider failing), so that case is raised as a
    RequestException instead of silently returning an empty string.
    """
    choices = body.get("choices") or []
    if not choices:
        raise requests.RequestException(f"{label()} returned no choices: {body.get('error') or body}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if "</think>" in content:  # thinking models may inline their reasoning
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
        content = content.split("</think>")[-1].strip()
    usage = body.get("usage") or {}
    return content, {
        "provider": "openrouter",
        "routed_model": body.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "prompt_eval_count": usage.get("prompt_tokens"),
        "eval_count": usage.get("completion_tokens"),
    }


# Transient: worth retrying (free models are often busy or briefly down).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class OpenRouterError(requests.RequestException):
    """All OpenRouter attempts failed; ``.attempts`` lists why each one did."""

    def __init__(self, message, response=None, attempts=None):
        super().__init__(message, response=response)
        self.attempts = attempts or []


def _backoff(attempt: int, retry_after) -> float:
    try:
        wait = float(retry_after)
    except (TypeError, ValueError):
        wait = settings.OPENROUTER_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
    return min(max(wait, 0.5), 30.0)


def request_openrouter(
    payload: dict,
    timeout: float,
    *,
    expect_json: bool = False,
    strict: bool = True,
    log: list | None = None,
):
    """POST to OpenRouter, retrying transient failures. Returns (text, usage, resp).

    Retried (up to OPENROUTER_MAX_RETRIES times, exponential backoff, within
    OPENROUTER_RETRY_BUDGET_SECONDS): HTTP 408/425/429/5xx, connection errors,
    HTTP 200 with an error body / no choices, an empty answer, and (when
    expect_json) an answer that is not a JSON object. Because ``openrouter/free``
    picks a random free model per request, a retry usually reaches a different
    model, so bad output from one model is often fixed by the next try.
    Timeouts get OPENROUTER_TIMEOUT_RETRIES extra tries.

    Not retried: 400/401/402/403/404 - those will not fix themselves. A 400
    that complains about ``response_format`` is repaired once by dropping JSON
    mode.

    strict=True: raise OpenRouterError when output is still empty/not JSON after
    all tries (so the caller can fail over). strict=False: return the last text
    and let the caller deal with it.
    """
    url, headers = openrouter_url(), openrouter_headers()
    max_retries = max(0, settings.OPENROUTER_MAX_RETRIES)
    budget = settings.OPENROUTER_RETRY_BUDGET_SECONDS
    started = time.monotonic()
    history: list[str] = log if log is not None else []
    attempt = timeouts = 0
    json_mode_dropped = False
    last_exc: Exception | None = None
    last_resp = None
    last_text, last_usage = "", {}

    while True:
        attempt += 1
        reason = None
        retry_after = None
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=remaining_timeout(timeout))
            last_resp = resp
            if (resp.status_code == 400 and "response_format" in payload and not json_mode_dropped
                    and "response_format" in resp.text.lower()):
                logger.warning("Routed model rejected response_format; retrying without JSON mode")
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                json_mode_dropped = True
                attempt -= 1  # a protocol fix, not a failed attempt
                continue
            if (resp.status_code == 400 and "reasoning_effort" in payload
                    and "reasoning" in resp.text.lower()):
                logger.warning("Model rejected reasoning_effort; retrying without it")
                payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
                attempt -= 1
                continue
            if resp.status_code in RETRYABLE_STATUS:
                reason = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                last_exc = requests.HTTPError(f"{reason}: {resp.text[:200]}", response=resp)
            else:
                resp.raise_for_status()  # 400/401/402/403/404...: permanent, propagate now
                try:
                    text, usage = parse_chat_response(resp.json())
                except ValueError as exc:
                    reason, last_exc = "unreadable response body", exc
                except requests.RequestException as exc:  # 200 + error body / no choices
                    reason, last_exc = "no choices in response", exc
                else:
                    last_text, last_usage = text, usage
                    if not text.strip():
                        reason = "empty response"
                    elif expect_json and not isinstance(safe_json_loads(text), dict):
                        reason = "output was not valid JSON"
                    else:
                        if history:
                            usage = dict(usage, retries=list(history))
                        return text, usage, resp
        except ModelBudgetExceeded:
            raise  # A local limit is not a retryable provider outage.
        except requests.exceptions.Timeout as exc:
            timeouts += 1
            last_exc = exc
            if timeouts > settings.OPENROUTER_TIMEOUT_RETRIES:
                history.append(f"attempt {attempt}: timeout")
                raise
            reason = "timeout"
        except requests.exceptions.ConnectionError as exc:
            reason, last_exc = "connection error", exc

        history.append(f"attempt {attempt}: {reason}")
        elapsed = time.monotonic() - started
        if attempt > max_retries or elapsed >= budget:
            break
        wait = _backoff(attempt, retry_after)
        logger.warning(f"{label()} {reason}; retry {attempt}/{max_retries} in {wait:.1f}s")
        budget_sleep(wait)

    # Out of retries.
    bad_output = reason in ("empty response", "output was not valid JSON")
    if bad_output and not strict:
        return last_text, dict(last_usage, retries=list(history)), last_resp
    raise OpenRouterError(
        f"{label()} failed after {attempt} attempt(s): {'; '.join(history[-3:])}",
        response=last_resp, attempts=history,
    ) from last_exc


def complete_text(prompt: str, *, model: str, temperature: float, timeout: float, max_tokens: int) -> str:
    """One-shot JSON completion through OpenRouter (with retries); returns the text."""
    payload = chat_payload(prompt, model=model, temperature=temperature, max_tokens=max_tokens)
    text, _usage, _resp = request_openrouter(payload, timeout, expect_json=True)
    return text
