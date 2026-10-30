"""Per-file model budgets. OCR and image preparation never spend model time.

API text, local text, API vision and local vision have independent allowances,
so an exhausted provider cannot prevent fallback or image verification.
"""
from contextvars import ContextVar
from functools import wraps
import time
import requests
from config.settings import settings

_BUDGETS = ContextVar('invoice_model_budgets', default=None)
_ACTIVE = ContextVar('active_model_budget', default=None)

class ModelBudgetExceeded(requests.exceptions.Timeout):
    """Local limit, not a server/network timeout."""


def processing_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _BUDGETS.get() is not None:
            return function(*args, **kwargs)
        token = _BUDGETS.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _BUDGETS.reset(token)
    return wrapped


def model_scope(kind):
    """Charge only time spent inside model calls (including retry backoff)."""
    def decorate(function):
        @wraps(function)
        @processing_scope
        def wrapped(*args, **kwargs):
            if _ACTIVE.get() is not None:
                return function(*args, **kwargs)
            budgets = _BUDGETS.get()
            allowance = budgets.setdefault(kind, max(1.0, float(getattr(settings, kind.upper() + '_BUDGET_SECONDS'))))
            start = time.monotonic()
            token = _ACTIVE.set((kind, start + allowance))
            try:
                return function(*args, **kwargs)
            finally:
                budgets[kind] = max(0.0, allowance - (time.monotonic() - start))
                _ACTIVE.reset(token)
        return wrapped
    return decorate


def remaining_timeout(configured):
    active = _ACTIVE.get()
    if active is None:
        return configured
    kind, deadline = active
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelBudgetExceeded(f'{kind.replace("_", " ")} time budget exhausted; no further request was sent.')
    return min(configured, remaining)


def budget_sleep(seconds):
    time.sleep(min(seconds, remaining_timeout(seconds)))
