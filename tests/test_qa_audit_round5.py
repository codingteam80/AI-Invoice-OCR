"""Requested storage defaults and final carried-balance reconciliation."""
import json
from pathlib import Path
from copy import deepcopy
import pytest
from ai.post_processing import sanitize_for_model, add_remaining_balance_line_item
from ai.validator import validate_extraction
from ai.source_evidence import reconcile_source_layout

FIELDS = ('subtotal', 'tax_amount', 'vat_exempt_sales', 'zero_rated_sales', 'withholding_tax')
F = json.loads((Path(__file__).parent/'fixtures/qa/audit_round5.json').read_text(encoding='utf8'))

@pytest.mark.parametrize('missing', [None, '', 'N/A', '--'])
def test_requested_defaults_preserve_other_unknowns(missing):
    d = sanitize_for_model(dict.fromkeys(FIELDS, missing))
    assert all(d[k] == 0.0 for k in FIELDS)
    assert d['total_amount'] is None and d['currency'] is None

def test_known_amounts_not_overwritten():
    assert all(sanitize_for_model(dict.fromkeys(FIELDS, 12.34))[k] == 12.34 for k in FIELDS)

@pytest.mark.parametrize('balance', [1749.51, -10.0, 0, None])
def test_balance_sync_idempotent_and_nonmutating(balance):
    original = {'previous_balance':balance, 'line_items':[
        dict(description='Service', quantity=1, unit_price=100, amount=100),
        dict(description='Previous Balance', quantity=1, unit_price=999, amount=999),
        dict(description='Remaining Balance', quantity=1, unit_price=999, amount=999)]}
    before = deepcopy(original)
    d, _ = add_remaining_balance_line_item(original)
    assert original == before
    assert add_remaining_balance_line_item(d)[0] == d
    assert len(d['line_items']) == (2 if balance else 1)
    if balance: assert d['line_items'][-1] == dict(description='Remaining Balance',quantity=1,unit_price=balance,amount=balance)

def test_credit_row_allowed_without_allowing_negative_products():
    d = {'invoice_number':'A123', 'vendor_name':'Vendor', 'invoice_date':'2025-01-01', 'currency':'PHP',
         'subtotal':100, 'tax_amount':0, 'current_charges_total':100, 'previous_balance':-10, 'total_amount':90,
         'line_items':[dict(description='Service',quantity=1,unit_price=100,amount=100)]}
    d = add_remaining_balance_line_item(d)[0]
    assert not any('negative' in s or 'line_items sum' in s for s in validate_extraction(d))
    d['line_items'][-1]['description'] = 'Product'
    assert any('negative' in s for s in validate_extraction(d))

@pytest.mark.parametrize('f', F, ids=lambda f:f['name'])
def test_all_supplied_records_defaults_and_balance_rows(f):
    d = reconcile_source_layout(deepcopy(f['saved']), f['ocr']['text'], f['detail_lines'], f['detail_regions'])[0]
    d = sanitize_for_model(add_remaining_balance_line_item(d)[0])
    assert all(d[k] is not None for k in FIELDS)
    balance = d.get('previous_balance') or 0
    rows = [r for r in d.get('line_items',[]) if r['description']=='Remaining Balance']
    assert len(rows) == (1 if abs(balance)>.005 else 0)
    if rows: assert rows[0]['amount'] == round(balance,2)
    if f['name']=='INV-16.png': assert d['previous_balance']==862.50
    if f['name']=='INV-21.jpg':
        assert d['subtotal']==5800 and d['tax_amount']==570
        assert [r['amount'] for r in d['line_items']]==[2500,1000,1500,800]
        assert 'Palm Avenue' in d['vendor_address'] and 'Time Plaza' in d['customer_address']

def test_vat_summary_needs_matching_printed_total():
    from ai.document_evidence import reconcile_document_evidence
    f = deepcopy(next(f for f in F if f['name']=='INV-21.jpg'))
    lines = deepcopy(f['detail_lines'])
    for line in lines:
        line['text'] = line['text'].replace('6,370.00','9,370.00')
    d = reconcile_document_evidence({'subtotal':None,'tax_amount':None}, f['ocr']['text'].replace('6,370.00','9,370.00'), lines)[0]
    assert d.get('tax_amount') is None

@pytest.mark.parametrize('f', F, ids=lambda f:f['name'])
def test_full_parser_storage_policy(monkeypatch, f):
    from parser import invoice_parser as p
    from config.settings import settings
    for key in ['VISION_VERIFICATION_ENABLED','VENDOR_HEADER_RECOVERY_ENABLED','DIAGNOSTIC_TRACE_ENABLED']:
        monkeypatch.setattr(settings,key,False)
    monkeypatch.setattr(p,'extract_invoice_data',lambda *a,**k:deepcopy(f['initial']))
    o = deepcopy(f['ocr'])
    o.update(details_recovered=True,detail_lines=f['detail_lines'],detail_regions=f['detail_regions'])
    d = p._build_invoice_from_ocr(f['name'],'not-used.png',o['text'],.8,o.get('engine','paddleocr'),o['lines'],o,1).model_dump(mode='json')
    assert all(d[k] is not None for k in FIELDS)
    rows = [r for r in d['line_items'] if r['description']=='Remaining Balance']
    balance=d.get('previous_balance') or 0
    assert len(rows)==(1 if abs(balance)>.005 else 0)
    if rows: assert rows[0]['amount']==round(balance,2)
    if f['name']=='INV-21.jpg':
        assert d['subtotal']==5800 and d['tax_amount']==570
        assert [r['amount'] for r in d['line_items']]==[2500,1000,1500,800]
    if f['name']=='INV-16.png': assert balance==862.50
