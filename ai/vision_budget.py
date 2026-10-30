"""File-local vision circuit breaker; an earlier upload cannot disable later files."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import time
from ai.processing_budget import remaining_timeout, model_scope, ModelBudgetExceeded
import requests
from config.settings import settings


@dataclass
class VisionBudget:
    reason: str | None = None
    requests: int = 0
    skipped: int = 0
    elapsed_seconds: float = 0.0
    local_model_checked: bool = False
    local_model_available: bool | None = None
    local_model_reason: str | None = None
    unavailable_skips: int = 0


_CURRENT = ContextVar('invoice_vision_budget', default=None)
_PREFLIGHT = ContextVar('invoice_vision_preflight', default=None)


@contextmanager
def vision_batch():
    token = _CURRENT.set(VisionBudget())
    cache_token = None
    if _PREFLIGHT.get() is None:
        cache_token = _PREFLIGHT.set(VisionBudget())
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)
        if cache_token is not None:
            _PREFLIGHT.reset(cache_token)


def vision_scope(function):
    """API/single-file calls get a scope too; nested UI calls reuse the batch."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _CURRENT.get() is not None:
            return function(*args, **kwargs)
        with vision_batch():
            return function(*args, **kwargs)
    return wrapped


def vision_file_scope(function):
    """Each uploaded file owns its failure state, even inside a UI batch."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with vision_batch():
            return function(*args, **kwargs)
    return wrapped


def preflight_local_vision_model() -> dict:
    """Check installed Ollama vision model once per upload batch, without loading it.

    Only local-capable modes need /api/tags. Cloud-only calls are unaffected.
    Failed preflights are advisory: text OCR/extraction must still run.
    """
    from ai import llm_client
    state = _PREFLIGHT.get()
    if not settings.VISION_VERIFICATION_ENABLED or llm_client.get_mode() == "api_only":
        return {"checked": False, "available": None, "reason": None}
    if state is not None and state.local_model_checked:
        return {"checked": True, "available": state.local_model_available,
                "reason": state.local_model_reason}

    model = str(settings.VISION_MODEL).strip()
    reason = None
    available = False
    try:
        response = requests.get(f"{settings.OLLAMA_HOST.rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("Ollama returned an unexpected model list")
        models = {
            str(info.get(key, "")).strip().lower()
            for info in payload["models"] if isinstance(info, dict)
            for key in ("name", "model")
        }
        name = model.lower()
        if ":" not in name:
            name += ":latest"
        available = name in models
        if not available:
            reason = (f"Local vision model '{model}' is not installed. "
                      f"Run 'ollama pull {model}' to enable visual verification.")
    except (requests.RequestException, ValueError, TypeError) as exc:
        reason = f"Could not check Ollama vision model '{model}' ({type(exc).__name__})."
    if state is not None:
        state.local_model_checked = True
        state.local_model_available = available
        state.local_model_reason = reason
    return {"checked": True, "available": available, "reason": reason}


def local_vision_unavailable(trace: dict | None = None) -> bool:
    """Skip costly failed local calls only when a preflight established failure.

    Does not mutate the general circuit-breaker reason, which is reserved for
    real vision HTTP failures. A missing optional model is not an upload error.
    """
    state = _PREFLIGHT.get()
    if state is None or state.local_model_available is not False:
        return False
    state.unavailable_skips += 1
    if trace is not None:
        trace["skipped_reason"] = state.local_model_reason or "local vision model unavailable"
        trace["model_unavailable"] = True
    return True


@model_scope("local_vision")
def vision_post(url, payload, attempt):
    state = _CURRENT.get()
    if state and state.reason and getattr(settings, 'VISION_BATCH_GUARD_ENABLED', True):
        state.skipped += 1
        attempt.update(skipped=True, elapsed_seconds=0.0, skipped_reason=state.reason)
        raise requests.RequestException('Vision skipped for this file after an earlier failure: ' + state.reason)
    start = time.perf_counter()
    if state:
        state.requests += 1
    try:
        return requests.post(url, json=payload, timeout=remaining_timeout(settings.VISION_TIMEOUT_SECONDS))
    except ModelBudgetExceeded as exc:
        attempt.update(error=str(exc), error_kind='budget_exhausted', skipped=True)
        if state:
            state.requests -= 1
            state.skipped += 1
            state.reason = str(exc)
        raise
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        attempt['error'] = str(exc)
        if state:
            state.reason = 'vision request timed out' if isinstance(exc, requests.exceptions.Timeout) else 'vision server connection failed'
        raise
    finally:
        elapsed = time.perf_counter() - start
        attempt['elapsed_seconds'] = round(elapsed, 3)
        if state:
            state.elapsed_seconds += elapsed


def verification_summary(trace):
    errors = []
    def walk(value, path='vision'):
        if isinstance(value, dict):
            if value.get('recovered') is True:
                return
            if value.get('error'):
                errors.append(path + ': ' + str(value['error'])[:300])
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, path + '.' + key)
        elif isinstance(value, list):
            for child in value:
                walk(child, path)
    walk(trace)
    state = _CURRENT.get()
    preflight = _PREFLIGHT.get()
    missing_local_vision_used = bool(preflight and preflight.local_model_available is False
                                    and preflight.unavailable_skips)
    incomplete = bool(errors or (state and state.reason) or missing_local_vision_used)
    summary = {'incomplete': incomplete, 'errors': errors}
    if state:
        summary.update(batch_failure_reason=state.reason, batch_requests=state.requests,
                       batch_skipped_requests=state.skipped, batch_vision_elapsed_seconds=round(state.elapsed_seconds, 3))
    if missing_local_vision_used:
        summary["batch_failure_reason"] = preflight.local_model_reason
    if preflight is not None:
        summary.update(local_model_available=preflight.local_model_available,
                       local_model_reason=preflight.local_model_reason,
                       batch_unavailable_skips=preflight.unavailable_skips)
    return summary


def verification_warning(summary):
    if not summary['incomplete']:
        return None
    reason = summary.get('batch_failure_reason') or 'one or more image verification checks failed'
    return 'VISION_VERIFICATION_INCOMPLETE: ' + reason + '. OCR/text results were saved; review the invoice against the original image.'
