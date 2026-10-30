"""Regression coverage for the October 15 expired-before-send failures.

Uses recorded OCR evidence and mocked transports; does not require model access.
"""
import json
from copy import deepcopy
from pathlib import Path
import numpy as np
import pytest
import requests
from config.settings import settings
from ai import processing_budget as budget, extractor, llm_client
from ai import focused_review as focus
from ai.field_ownership import reconcile_owned_fields
from ai.party_master import apply_final_master_pass

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/qa/emerald_oct15.json').read_text())


def test_slow_ocr_does_not_spend_model_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'PROCESSING_BUDGET_SECONDS', 30)
    monkeypatch.setattr(settings, 'LOCAL_TEXT_BUDGET_SECONDS', 300)
    timeouts = []
    def post(*a, **kw):
        timeouts.append(kw['timeout'])
        return type('Response', (), {'status_code':200, 'raise_for_status':lambda self:None, 'json':lambda self:{'response':'{}'}})()
    monkeypatch.setattr(requests, 'post', post)
    @budget.processing_scope
    def file():
        clock[0] += 53  # Recorded first OCR run exceeded the old 30 seconds.
        extractor._call_local_ollama('invoice')
    file()
    assert timeouts == [300]


def test_budgets_cumulative_but_ocr_gaps_free_and_next_file_fresh(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'LOCAL_TEXT_BUDGET_SECONDS', 30)
    seen = []
    @budget.model_scope('local_text')
    def call():
        seen.append(budget.remaining_timeout(300))
        clock[0] += 10
    @budget.processing_scope
    def file():
        call()
        clock[0] += 90  # Native OCR/image preparation, not model processing.
        call()
    file(); file()
    assert seen == [30, 20, 30, 20]


def test_api_budget_exhaustion_preserves_fallback_and_vision(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'API_TEXT_BUDGET_SECONDS', 2)
    monkeypatch.setattr(settings, 'LOCAL_TEXT_BUDGET_SECONDS', 300)
    monkeypatch.setattr(settings, 'LOCAL_VISION_BUDGET_SECONDS', 180)
    monkeypatch.setattr(settings, 'LLM_PROVIDER', 'openrouter')
    monkeypatch.setattr(settings, 'LLM_FALLBACK_TO_OLLAMA', True)
    monkeypatch.setattr(settings, 'OPENROUTER_API_KEY', 'test')
    def failed_api(*a, **kw):
        clock[0] += 3
        budget.remaining_timeout(120)
    monkeypatch.setattr(llm_client, 'request_openrouter', failed_api)
    calls = []
    def post(*a, **kw):
        calls.append(kw['timeout'])
        return type('Response', (), {'status_code':200, 'raise_for_status':lambda self:None, 'json':lambda self:{'response':'{}'}})()
    monkeypatch.setattr(requests, 'post', post)
    @budget.processing_scope
    def file():
        trace = {}
        assert extractor._call_ollama('invoice', trace) == '{}'
        assert trace['provider'] == 'ollama'
        assert trace['api_attempt']['error_kind'] == 'budget_exhausted'
        assert llm_client._cooldown_until == 0
        @budget.model_scope('local_vision')
        def vision():
            assert budget.remaining_timeout(180) == 180
        vision()
    file()
    assert calls == [300]


def test_exhausted_local_budget_stops_without_http_retry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'LOCAL_TEXT_BUDGET_SECONDS', 2)
    calls = []
    def post(*a, **kw):
        calls.append(1); clock[0] += 3
        raise requests.Timeout('real request timeout')
    monkeypatch.setattr(requests, 'post', post)
    trace = {}
    with pytest.raises(extractor.ExtractionError, match='budget exhausted'):
        extractor._call_local_ollama('invoice', trace)
    assert len(calls) == 1
    assert trace['error_kind'] == 'budget_exhausted'


def test_groq_trace_identity(monkeypatch):
    monkeypatch.setattr(settings, 'OPENROUTER_BASE_URL', 'https://api.groq.com/openai/v1')
    assert llm_client.provider_name() == 'groq'


def test_real_header_recovers_printed_number():
    h = FIXTURE['header'][0]
    out, _ = reconcile_owned_fields({}, h['text'], h['lines'])
    assert out['invoice_number'] == '7512'


def test_ambiguous_header_numbers_not_guessed():
    h = deepcopy(FIXTURE['header'][0])
    n = next(row for row in h['lines'] if row['text'] == '7512')
    h['lines'].append(dict(n, text='9999'))
    out, _ = reconcile_owned_fields({}, h['text'], h['lines'])
    assert not out.get('invoice_number')


def test_real_seller_city_preserved_despite_customer_master_overlap():
    data = dict(FIXTURE['final'], vendor_address='G/F Emerald Mansion Emerald Ave Ortigas Ctr., San Antonio Pasig City 1603')
    out, _ = apply_final_master_pass(data, FIXTURE['ocr']['text'])
    assert 'San Antonio Pasig City 1603' in out['vendor_address']
    assert not out.get('customer_address')


@pytest.fixture
def focused(monkeypatch):
    monkeypatch.setattr(settings, 'VISION_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(focus.cv2, 'imread', lambda _: np.zeros((2000, 1400, 3), dtype=np.uint8))
    data = deepcopy(FIXTURE['final'])
    read = {k: data[k] for k in focus.FIELDS}
    read['vat_exempt_sales'] = 1967.4
    return data, read


def test_independent_image_read_repairs_only_conflicting_amount(monkeypatch, focused):
    data, read = focused
    data['line_items'] = [{'description':'existing row'}]
    monkeypatch.setattr(focus, '_read_crop', lambda *a: read)
    out, notes = focus.review_unresolved_regions(data, 'image', FIXTURE['ocr']['lines'], {})
    assert out['vat_exempt_sales'] == 1967.4
    assert out['total_amount'] == 2247.4
    assert focus._balance(out)
    assert data['vat_exempt_sales'] == 19603.4
    assert notes


@pytest.mark.parametrize('change', [{'total_amount':19883.4}, {'vat_exempt_sales':None}, {'vat_exempt_sales':1960}, {'vat_exempt_sales':1957.4,'tax_amount':40}])
def test_ambiguous_or_multiple_changed_amounts_remain_for_review(monkeypatch, focused, change):
    data, read = focused
    data['line_items'] = [{'description':'existing'}]
    read.update(change)
    monkeypatch.setattr(focus, '_read_crop', lambda *a: read)
    out, notes = focus.review_unresolved_regions(data, 'image', FIXTURE['ocr']['lines'], {})
    assert out == data and not notes


def test_no_image_result_does_not_invent_financial_correction(monkeypatch, focused):
    data, _ = focused
    monkeypatch.setattr(focus, '_read_crop', lambda *a: None)
    out, _ = focus.review_unresolved_regions(data, 'image', FIXTURE['ocr']['lines'], {})
    assert out == data


def test_missing_service_rows_supported_without_schema_error(monkeypatch, focused):
    from models.invoice import Invoice
    data, read = focused
    responses = iter([read, {'line_items':[{'description':'Utilities for Aug 2026','amount':2247.4}]}])
    monkeypatch.setattr(focus, '_read_crop', lambda *a: next(responses))
    out, notes = focus.review_unresolved_regions(data, 'image', FIXTURE['ocr']['lines'], {})
    assert out['line_items'] == [{'description':'Utilities for Aug 2026','quantity':1.0,'unit_price':2247.4,'amount':2247.4}]
    assert any('display conventions' in n for n in notes)
    assert Invoice(**dict(out, vendor_name='Seller')).line_items[0].amount == 2247.4


def test_vision_disabled_never_calls_model(monkeypatch, focused):
    data, _ = focused
    monkeypatch.setattr(settings, 'VISION_VERIFICATION_ENABLED', False)
    monkeypatch.setattr(focus, '_read_crop', lambda *a: pytest.fail('vision disabled'))
    assert focus.review_unresolved_regions(data, 'image', FIXTURE['ocr']['lines'], {})[0] == data


def test_recorded_invoice_pipeline_replay(monkeypatch, focused):
    """Full pipeline with recorded OCR and explicit simulated image readings."""
    from parser import invoice_parser as p
    for key in ['DIAGNOSTIC_TRACE_ENABLED','STATEMENT_SUMMARY_VERIFY_ENABLED','CUSTOMER_ADDRESS_VERIFY_ENABLED','VENDOR_ADDRESS_VERIFY_ENABLED','PLATE_NUMBER_VERIFY_ENABLED']:
        monkeypatch.setattr(settings, key, False)
    def no_text(*a, **kw):
        raise extractor.ExtractionError('Simulated offline text model')
    monkeypatch.setattr(p, 'extract_invoice_data', no_text)
    def header(*a, **kw):
        kw['trace'].update(best_ocr_text=FIXTURE['header'][0]['text'], ocr_attempts=FIXTURE['header'])
        return {'vendor_address':'G/F Emerald Mansion Emerald Ave Ortigas Ctr., San Antonio Pasig City 1603', 'vendor_tax_id':'005-578-990-00000'}, [], {'vendor_address','vendor_tax_id'}
    monkeypatch.setattr(p, 'recover_vendor_header_fields', header)
    monkeypatch.setattr(p, 'verify_against_image', lambda *a, **kw: ({'customer_name':'TSUKIDEN GLOBAL SOLUTIONS INC'}, []))
    monkeypatch.setattr(p, 'verify_handwritten_invoice_date', lambda *a, **kw: ('2026-08-24', []))
    _, read = focused
    def crop(image, rect, prompt, trace):
        if 'financial summary' in prompt:
            return read
        return {'line_items':[{'description':'Utilities for Aug 2026', 'amount':2247.4}]}
    monkeypatch.setattr(focus, '_read_crop', crop)
    o = deepcopy(FIXTURE['ocr'])
    o.update(details_recovered=True, detail_lines=o['lines'], detail_regions={})
    out = p._build_invoice_from_ocr('recorded.pdf', 'mock-image', o['text'], .9, 'paddleocr', o['lines'], o, 1)
    assert out.invoice_number == '7512'
    assert str(out.invoice_date) == '2026-08-24'
    assert out.vat_exempt_sales == 1967.4
    assert out.total_amount == 2247.4
    assert 'San Antonio Pasig City 1603' in out.vendor_address
    assert out.customer_name == 'TSUKIDEN GLOBAL SOLUTIONS INC'
    assert out.line_items[0].amount == 2247.4
    assert any('AI extraction was unavailable' in s for s in out.validation_issues)
    assert not any('AI correction was incomplete' in s for s in out.validation_issues)
    assert out.status == 'needs_review'  # A recovered record is never silently certified.


def test_successful_vision_fallback_is_not_incomplete(monkeypatch):
    from ai import vision_verifier as v, vision_budget as vb
    monkeypatch.setattr(settings, 'LLM_PROVIDER', 'openrouter')
    monkeypatch.setattr(settings, 'LLM_FALLBACK_TO_OLLAMA', True)
    monkeypatch.setattr(settings, 'OPENROUTER_API_KEY', 'test')
    def api(prompt, image, trace):
        trace.setdefault('http_attempts', []).append({'provider':'groq','error':'temporary failure'})
        raise requests.ConnectionError('temporary failure')
    monkeypatch.setattr(v, '_post_openrouter_vision', api)
    monkeypatch.setattr(v, '_post_ollama_generate', lambda *a: '{"mismatches":[]}')
    trace = {}
    with vb.vision_batch():
        assert v._post_generate('prompt', 'image', trace) == '{"mismatches":[]}'
        assert not vb.verification_summary(trace)['incomplete']
    assert trace['fallback_from']['recovered']


def test_exhausted_vision_budget_reports_local_limit(monkeypatch):
    from ai import vision_budget as v
    clock = [0.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'LOCAL_VISION_BUDGET_SECONDS', 1)
    def post(*a, **kw):
        clock[0] += 2
        return object()
    monkeypatch.setattr(requests, 'post', post)
    @budget.processing_scope
    def file():
        with v.vision_batch() as state:
            v.vision_post('url', {}, {})
            attempt = {}
            with pytest.raises(budget.ModelBudgetExceeded):
                v.vision_post('url', {}, attempt)
            assert 'no further request was sent' in state.reason
            assert state.requests == 1 and state.skipped == 1
            assert attempt['error_kind'] == 'budget_exhausted'
    file()
