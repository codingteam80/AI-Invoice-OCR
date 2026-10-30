"""Batch isolation and field recovery from the supplied 2.0.2 traces."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from ai.source_evidence import reconcile_source_layout
from ai.field_ownership import reconcile_owned_fields

F=json.loads((Path(__file__).parent/'fixtures/qa/batch_round3.json').read_text(encoding='utf8'))
ADDRESS='Unit 2102, 21st Floor, One Corporate Centre, Dona Julia Vargas Avenue corner Meralco Ave. Ortigas'

def recover(f):
    return reconcile_source_layout(deepcopy(f['saved']),f['ocr']['text'],f['detail_lines'],f['detail_regions'])[0]

def assert_fields(name,d):
    if name=='Sample Invoice_Page_2.pdf':
        assert d['invoice_number']=='44072'
        assert d['customer_address']==ADDRESS
    elif name=='Addl Invoice_Page_2.pdf':
        assert d['customer_name'].upper()=='TSUKIDEN GLOBAL SOLUTIONS INC'
        assert d['customer_tax_id']=='007-848-122-00000'
        assert '2101 One Corporate Center Julia' in d['customer_address']
        assert d['customer_address'].endswith('Metro Manila,1605')
        assert 'Plan' not in d['customer_address']
    elif name=='Addl Invoice_Page_5.pdf':
        assert d['line_items']==[{'description':'SERVICE FEE (TICKET)','quantity':1,'unit_price':2468,'amount':2468}]
        assert d['currency']=='PHP'
    elif name=='INV-38-2.png':assert d['currency']=='PHP'
    elif name=='INV-34.jpg':
        assert d['vendor_name']=='SMART COMMUNICATIONS INC'
        assert d['currency']=='PHP'

@pytest.mark.parametrize('f',F,ids=[f['name']+' '+f['created'] for f in F])
def test_all_reported_batch_and_single_outputs(f):
    assert_fields(f['name'],recover(f))

@pytest.mark.parametrize('f',F,ids=[f['name']+' '+f['created'] for f in F])
def test_complete_pipeline_keeps_corrections(monkeypatch,f):
    from parser import invoice_parser as p
    from config.settings import settings
    for key in ['VISION_VERIFICATION_ENABLED','VENDOR_HEADER_RECOVERY_ENABLED','DIAGNOSTIC_TRACE_ENABLED']:
        monkeypatch.setattr(settings,key,False)
    monkeypatch.setattr(p,'extract_invoice_data',lambda *a,**k:deepcopy(f['initial']))
    o=deepcopy(f['ocr']);o.update(details_recovered=True,detail_lines=f['detail_lines'],detail_regions=f['detail_regions'])
    d=p._build_invoice_from_ocr(f['name'],'no-image.png',o['text'],.8,'paddleocr',o['lines'],o,1).model_dump(mode='json')
    assert_fields(f['name'],d)

@pytest.mark.parametrize('currency',['USD','EUR','GBP'])
def test_explicit_foreign_currency_not_overridden_by_country(currency):
    d,_=reconcile_owned_fields({'currency':currency},f'Customer Philippines\nInvoice total in {currency}\n100.00',[])
    assert d['currency']==currency

def test_smart_hardware_not_changed_into_mobile_operator():
    d,_=reconcile_owned_fields({'vendor_name':'SMART HARDWARE','currency':'USD'},'Smart Hardware Philippines invoice',[])
    assert d['vendor_name']=='SMART HARDWARE'
    assert d['currency']=='PHP'

def test_batch_order_does_not_change_source_fields():
    forward={f['name']+f['created']:recover(f) for f in F}
    reverse={f['name']+f['created']:recover(f) for f in reversed(F)}
    assert forward==reverse

def test_service_resets_vision_after_previous_file_timeout(monkeypatch):
    import requests
    from ai import vision_budget as b
    from services import invoice_service as s
    calls=[];states=[]
    def post(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:raise requests.Timeout('slow first file')
        return object()
    monkeypatch.setattr(b.requests,'post',post)
    def parse(path,**kwargs):
        states.append(b._CURRENT.get())
        try:b.vision_post('http://model',{}, {})
        except requests.Timeout:pass
        # Terminate parser deliberately; this check exercises the real service
        # boundary without modifying a database.
        raise s.InvoiceParsingError('test file finished')
    monkeypatch.setattr(s,'parse_invoice_pages',parse)
    with b.vision_batch() as outer:
        s.process_invoice_file('first.png')
        s.process_invoice_file('second.png')
        assert outer.reason is None
    assert len(calls)==2
    assert states[0].reason is not None and states[1].reason is None
    assert states[0] is not states[1]

def test_vision_retries_still_bounded_inside_one_file(monkeypatch):
    import requests
    from ai import vision_budget as b
    calls=[]
    def post(*a,**k):calls.append(1);raise requests.Timeout('down')
    monkeypatch.setattr(b.requests,'post',post)
    @b.vision_file_scope
    def file():
        for _ in range(3):
            with pytest.raises(requests.RequestException):b.vision_post('http://model',{}, {})
    file()
    assert len(calls)==1
