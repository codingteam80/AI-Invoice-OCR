"""Conditional local vision must be optional, cost-aware and fail-open."""
from types import SimpleNamespace
import requests

from ai import llm_client, vision_budget, vision_decision, vision_verifier


def _mode_local(monkeypatch):
    monkeypatch.setattr(llm_client, 'get_mode', lambda: 'ollama_only')
    monkeypatch.setattr(llm_client, 'pick_provider', lambda: 'ollama')
    monkeypatch.setattr(vision_budget.settings, 'VISION_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(vision_budget.settings, 'VISION_MODEL', 'qwen2.5vl:7b')


def _sample():
    data = dict(invoice_number='INV-100', invoice_date='2026-10-01',
                vendor_name='Acme Store', customer_name='Mary Cruz',
                subtotal=100., tax_amount=12., total_amount=112.,
                discount=0., withholding_tax=0., zero_rated_sales=0., vat_exempt_sales=0.,
                line_items=[dict(description='Service', quantity=1, unit_price=100, amount=100)])
    text = ('ACME STORE\nCustomer: Mary Cruz\nInvoice No. INV-100\nDate: 2026-10-01\n'
            'Service 1 100.00 100.00\nVatable Sales 100.00\nVAT 12.00\nTotal Amount 112.00')
    return data, text


def test_clean_printed_invoice_skips_full_image():
    data, text = _sample()
    needed, reasons = vision_decision.choose_full_image_vision(
        data, ocr_confidence=.98, ocr_engine='paddleocr', ocr_text=text)
    assert needed is False
    assert reasons == []


def test_handwritten_invoice_selects_vision():
    data, text = _sample()
    needed, reasons = vision_decision.choose_full_image_vision(
        data, ocr_confidence=.98, ocr_engine='paddleocr+trocr', ocr_text=text)
    assert needed and 'handwriting detected' in reasons


def test_low_ocr_or_missing_critical_fields_selects_vision():
    data, text = _sample()
    needed, reasons = vision_decision.choose_full_image_vision(
        data, ocr_confidence=.60, ocr_engine='paddleocr', ocr_text=text)
    assert needed and 'low OCR confidence' in reasons
    data['invoice_number'] = None
    needed, reasons = vision_decision.choose_full_image_vision(data, ocr_confidence=.99, ocr_text=text)
    assert needed and 'missing invoice_number' in reasons


def test_financial_or_line_item_error_selects_vision():
    data, text = _sample()
    data['total_amount'] = 215.0
    needed, reasons = vision_decision.choose_full_image_vision(data, ocr_confidence=.99, ocr_text=text)
    assert needed and 'financial amounts do not reconcile' in reasons
    data['total_amount'] = 112.0
    data['line_items'][0]['amount'] = 999.0
    needed, reasons = vision_decision.choose_full_image_vision(data, ocr_confidence=.99, ocr_text=text)
    assert needed and 'line-item arithmetic mismatch' in reasons


def test_installed_model_is_checked_only_once_per_batch(monkeypatch):
    _mode_local(monkeypatch)
    calls = []
    def get(url, timeout):
        calls.append((url, timeout))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {'models': [{'name': 'qwen2.5vl:7b'}]})
    monkeypatch.setattr(vision_budget.requests, 'get', get)
    with vision_budget.vision_batch():
        a = vision_budget.preflight_local_vision_model()
        b = vision_budget.preflight_local_vision_model()
        assert a == b == {'checked': True, 'available': True, 'reason': None}
    assert len(calls) == 1
    with vision_budget.vision_batch():
        vision_budget.preflight_local_vision_model()
    assert len(calls) == 2


def test_missing_model_skips_all_local_image_requests_without_upload_error(monkeypatch):
    _mode_local(monkeypatch)
    def get(url, timeout):
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {'models': [{'name': 'qwen2.5:7b'}]})
    monkeypatch.setattr(vision_budget.requests, 'get', get)
    def no_post(*args, **kwargs):
        raise AssertionError('Missing qwen2.5vl must not send requests to /api/generate or /api/chat')
    monkeypatch.setattr(vision_verifier.requests, 'post', no_post)
    with vision_budget.vision_batch():
        check = vision_budget.preflight_local_vision_model()
        assert check['checked'] and check['available'] is False
        assert 'ollama pull qwen2.5vl:7b' in check['reason']
        trace = {}
        assert vision_verifier.verify_against_image('nonexistent.png', {}, trace=trace) == ({}, [])
        assert trace['model_unavailable']
        assert vision_verifier._post_generate('prompt', 'image', {}) is None
        assert vision_verifier._post_chat('prompt', 'image', {}) is None
        summary = vision_budget.verification_summary(trace)
        assert summary['batch_unavailable_skips'] == 3
        assert summary['batch_requests'] == 0


def test_offline_ollama_preflight_does_not_throw(monkeypatch):
    _mode_local(monkeypatch)
    def get(*args, **kwargs):
        raise requests.ConnectionError('connection refused')
    monkeypatch.setattr(vision_budget.requests, 'get', get)
    with vision_budget.vision_batch():
        check = vision_budget.preflight_local_vision_model()
        assert check['available'] is False
        assert 'ConnectionError' in check['reason']


def test_api_only_mode_does_not_probe_local_models(monkeypatch):
    monkeypatch.setattr(llm_client, 'get_mode', lambda: 'api_only')
    monkeypatch.setattr(vision_budget.settings, 'VISION_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(vision_budget.requests, 'get', lambda *a, **k: 1 / 0)
    with vision_budget.vision_batch():
        assert vision_budget.preflight_local_vision_model()['checked'] is False
