"""Request-local vision circuit breaker. Never shares state across upload batches."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import time
import requests
from config.settings import settings


@dataclass
class VisionBudget:
    reason: str | None = None
    requests: int = 0
    skipped: int = 0
    elapsed_seconds: float = 0.0


_CURRENT = ContextVar('invoice_vision_budget', default=None)


@contextmanager
def vision_batch():
    token = _CURRENT.set(VisionBudget())
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)


def vision_scope(function):
    """API/single-file calls get a scope too; nested UI calls reuse the batch."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _CURRENT.get() is not None:
            return function(*args, **kwargs)
        with vision_batch():
            return function(*args, **kwargs)
    return wrapped


def vision_post(url, payload, attempt):
    state = _CURRENT.get()
    if state and state.reason and getattr(settings, 'VISION_BATCH_GUARD_ENABLED', True):
        state.skipped += 1
        attempt.update(skipped=True, elapsed_seconds=0.0, skipped_reason=state.reason)
        raise requests.RequestException('Vision skipped for this batch after an earlier failure: ' + state.reason)
    start = time.perf_counter()
    if state:
        state.requests += 1
    try:
        return requests.post(url, json=payload, timeout=settings.VISION_TIMEOUT_SECONDS)
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
    incomplete = bool(errors or (state and state.reason))
    summary = {'incomplete': incomplete, 'errors': errors}
    if state:
        summary.update(batch_failure_reason=state.reason, batch_requests=state.requests,
                       batch_skipped_requests=state.skipped, batch_vision_elapsed_seconds=round(state.elapsed_seconds, 3))
    return summary


def verification_warning(summary):
    if not summary['incomplete']:
        return None
    reason = summary.get('batch_failure_reason') or 'one or more image verification checks failed'
    return 'VISION_VERIFICATION_INCOMPLETE: ' + reason + '. OCR/text results were saved; review the invoice against the original image.'
