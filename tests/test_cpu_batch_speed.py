import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import requests
from ai import vision_budget as budget
from ai import extractor, vision_verifier


def test_one_timeout_per_batch_and_next_batch_retries(monkeypatch):
    calls=[]
    def fail(*args, **kwargs):
        calls.append(1)
        raise requests.ReadTimeout('read timeout=180')
    monkeypatch.setattr(budget.requests,'post',fail)
    with budget.vision_batch() as state:
        for _ in range(23):
            trace={}
            with pytest.raises(requests.RequestException):vision_verifier._post_generate('prompt','image',trace)
        assert len(calls)==1 and state.skipped==22
        assert budget.verification_summary({})['incomplete']
        assert 'VISION_VERIFICATION_INCOMPLETE' in budget.verification_warning(budget.verification_summary({}))
    assert not budget.verification_summary({})['incomplete']
    with budget.vision_batch():
        with pytest.raises(requests.ReadTimeout):vision_verifier._post_generate('prompt','image',{})
    assert len(calls)==2


def test_success_and_http_endpoint_fallback_are_not_disabled(monkeypatch):
    class Response:
        def __init__(self, code):self.status_code=code;self.ok=code==200;self.text='rejected'
        def json(self):return {'message':{'content':'{}'},'response':'{}'}
    responses=iter([Response(400),Response(200),Response(200)])
    monkeypatch.setattr(budget.requests,'post',lambda *a,**k:next(responses))
    with budget.vision_batch() as state:
        trace={}
        assert vision_verifier._post_generate('p','image',trace) is None
        assert vision_verifier._post_chat('p','image',trace)=='{}'
        assert vision_verifier._post_generate('p','image',{})=='{}'
        assert state.requests==3 and not state.reason
        assert all('elapsed_seconds' in a for a in trace['http_attempts'])


def test_scope_cleanup_and_thread_isolation():
    def other():
        with budget.vision_batch() as state:return state.reason
    with budget.vision_batch() as state:
        state.reason='timeout'
        with ThreadPoolExecutor(1) as pool:assert pool.submit(other).result() is None
        assert budget.verification_summary({})['incomplete']
    @budget.vision_scope
    def fail():
        budget._CURRENT.get().reason='failure'
        raise ValueError('test')
    with pytest.raises(ValueError):fail()
    assert budget._CURRENT.get() is None


def test_early_cleanup_avoids_correction_for_header_item(monkeypatch):
    data={'invoice_number':'1','line_items':[{'description':'Fee','quantity':1,'unit_price':10,'amount':10},{'description':'Monthly software subscription','quantity':1,'unit_price':100,'amount':100}]}
    calls=[]
    monkeypatch.setattr(extractor,'_call_ollama',lambda *a:(calls.append(1) or json.dumps(data)))
    monkeypatch.setattr(extractor,'validate_extraction',lambda d,**k:['header item'] if any(i['description']=='Fee' for i in d['line_items']) else [])
    trace={};result=extractor.extract_invoice_data('invoice',template_name='generic',trace=trace)
    assert len(calls)==1 and len(result['line_items'])==1
    assert trace['attempts'][0]['parsed_json']==data
    assert trace['early_cleanup']


def test_repeated_correction_stops_but_changed_response_can_continue(monkeypatch):
    data={'invoice_number':'1','vendor_name':'Vendor','line_items':[]}
    calls=[]
    monkeypatch.setattr(extractor,'_call_ollama',lambda *a:(calls.append(1) or json.dumps(data)))
    monkeypatch.setattr(extractor,'validate_extraction',lambda *a,**k:['unresolved'])
    trace={};extractor.extract_invoice_data('invoice',template_name='generic',trace=trace)
    assert len(calls)==2 and 'stopped_on_repeated_result' in trace
    responses=iter([data,dict(data,invoice_number='2'),dict(data,invoice_number='3')])
    monkeypatch.setattr(extractor,'_call_ollama',lambda *a:json.dumps(next(responses)))
    monkeypatch.setattr(extractor,'validate_extraction',lambda d,**k:[] if d['invoice_number']=='3' else ['missing '+d['invoice_number']])
    trace={};out=extractor.extract_invoice_data('invoice',template_name='generic',trace=trace)
    assert out['invoice_number']=='3' and len(trace['attempts'])==3


def test_incomplete_verification_requires_review():
    from ai.confidence import score_extraction
    warning=budget.verification_warning({'incomplete':True,'batch_failure_reason':'vision request timed out'})
    pred=score_extraction({'invoice_number':'1','invoice_date':'2026-09-28','vendor_name':'Vendor','total_amount':112,'subtotal':100,'tax_amount':12,'currency':'PHP'},1,[warning],ocr_engine='paddleocr')
    assert pred.needs_review


def test_pipeline_saves_review_status_and_warning_for_failed_and_skipped_vision(monkeypatch):
    import types
    ocr=types.ModuleType('ocr.ocr_engine')
    ocr.extract_from_file=lambda *a,**k:None
    ocr.extract_pages_from_file=lambda *a,**k:[]
    monkeypatch.setitem(sys.modules,'ocr.ocr_engine',ocr)
    from parser import invoice_parser as parser
    data={'invoice_number':'INV-1','invoice_date':'2026-09-28','vendor_name':'Acme Services','subtotal':100,'tax_amount':12,'total_amount':112,'currency':'PHP','line_items':[{'description':'Monthly software subscription','quantity':1,'unit_price':100,'amount':100}]}
    monkeypatch.setattr(parser,'extract_invoice_data',lambda *a,**k:dict(data))
    monkeypatch.setattr(parser,'recover_vendor_header_fields',lambda *a,**k:({},[],set()))
    for name in ['verify_handwritten_invoice_date','verify_statement_summary_crop','verify_customer_address_crop','verify_vendor_address_crop','verify_plate_number_crop']:
        monkeypatch.setattr(parser,name,lambda *a,**k:(None,[]))
    calls=[]
    def fail(*a,**k):
        calls.append(1);raise requests.ReadTimeout('test timeout')
    monkeypatch.setattr(budget.requests,'post',fail)
    def verify(*a,trace,**k):
        try:vision_verifier._post_generate('p','image',trace)
        except requests.RequestException as exc:trace['error']=str(exc)
        return {},[]
    monkeypatch.setattr(parser,'verify_against_image',verify)
    traces=[]
    monkeypatch.setattr(parser,'save_diagnostic_trace',lambda **k:(traces.append(k) or None))
    with budget.vision_batch():
        for _ in range(2):
            invoice=parser._build_invoice_from_ocr('test.png','test.png','Acme Services\nInvoice INV-1\n2026-09-28\nVATable Sales 100.00\nVAT 12.00\nTotal 112.00',.99,'paddleocr')
            assert invoice.status=='needs_review'
            assert 'VISION_VERIFICATION_INCOMPLETE' in invoice.vision_notes
    assert len(calls)==1
    assert traces[-1]['vision_trace']['verification_availability']['incomplete']
    assert 'timing_seconds' in traces[-1]['stages']
