"""Regression coverage from all 59 supplied saved records, plus negative controls."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from ai.source_evidence import reconcile_source_layout
from ai.document_evidence import reconcile_document_evidence
from utils.data_quality import document_content_key

F = json.loads((Path(__file__).parent/'fixtures/qa/audit_round4.json').read_text(encoding='utf8'))


def recover(f):
    return reconcile_source_layout(deepcopy(f['saved']), f['ocr']['text'], f['detail_lines'], f['detail_regions'])[0]


EXPECTED = {
    5: {'subtotal':85963.99, 'total_amount':94560.39, 'withholding_tax':1719.28, 'current_charges_total':None},
    11: {'invoice_number':'1000044843719', 'subtotal':2133.76, 'discount':99},
    31: {'subtotal':376.32, 'tax_amount':45.16, 'currency':'PHP'},
    38: {'subtotal':4650, 'tax_amount':558, 'total_amount':5208},
    48: {'invoice_number':'3219', 'subtotal':85963.99, 'customer_name':'TSUKIDENGLOBAL SOLUTIONS, INC.'},
    4: {'subtotal':10500, 'tax_amount':1260, 'total_amount':11760, 'current_charges_total':None},
    9: {'vendor_name':'PLDT INC.', 'total_amount':1289},
    14: {'subtotal':13.39, 'tax_amount':1.61, 'total_amount':15, 'current_charges_total':None},
    15: {'subtotal':26.79, 'tax_amount':3.21, 'total_amount':30, 'current_charges_total':None},
    18: {'subtotal':1323, 'tax_amount':158.76, 'total_amount':1481.76, 'currency':'PHP'},
    20: {'vendor_name':'MS.SASA C.ARANETA', 'customer_name':'MR.IRAWADI JOHN OUD', 'total_amount':110},
    24: {'subtotal':8000, 'tax_amount':960, 'current_charges_total':8960, 'previous_balance':44800, 'total_amount':53760},
    25: {'invoice_number':'0298050594', 'subtotal':1279.83, 'tax_amount':153.58, 'total_amount':1433.26, 'previous_balance':-.15},
    26: {'total_amount':2595.89, 'subtotal':1248.21, 'tax_amount':149.79, 'discount':99, 'current_charges_total':1299},
    29: {'subtotal':2763.39, 'tax_amount':331.61, 'previous_balance':-5.7, 'total_amount':3089.30, 'currency':'PHP'},
    30: {'invoice_number':'0434287913', 'subtotal':1159.82, 'tax_amount':139.18, 'total_amount':1289},
    32: {'vendor_name':'MERALCO', 'invoice_number':'3353055189919', 'total_amount':9349.73, 'subtotal':None, 'tax_amount':None},
    39: {'vendor_name':'MERALCO', 'total_amount':1277.60, 'subtotal':None},
    40: {'vendor_name':'MERALCO', 'total_amount':1502.42, 'invoice_date':None},
    41: {'vendor_name':'MERALCO', 'total_amount':17972.99, 'current_charges_total':9274.86},
    42: {'invoice_number':'NOTARIZE185857', 'total_amount':400, 'currency':'USD'},
    43: {'tax_amount':3250, 'total_amount':2999, 'currency':'PHP'},
    44: {'subtotal':2347.23, 'tax_amount':281.67, 'previous_balance':-2114.13, 'total_amount':-1147.90},
    46: {'total_amount':1250, 'subtotal':1116.08, 'tax_amount':133.92},
    47: {'invoice_number':'PH91MKP0135619', 'subtotal':10500, 'tax_amount':1260, 'total_amount':11760, 'invoice_date':'2026-08-06'},
    50: {'subtotal':4888, 'tax_amount':586.56, 'total_amount':5474.56},
    51: {'invoice_number':'ISINVWA000039616110', 'total_amount':15},
    52: {'invoice_number':'ISORWA000001492046', 'subtotal':26.79, 'total_amount':30},
    53: {'invoice_number':'NOTARIZE185857', 'total_amount':400},
    54: {'total_amount':1481.76, 'tax_amount':158.76, 'currency':'PHP'},
    55: {'total_amount':110, 'vendor_name':'MS.SASA C.ARANETA', 'customer_name':'MR.IRAWADI JOHN OUD'},
    56: {'invoice_number':'4132095502586', 'vendor_name':'MERALCO', 'total_amount':5.64, 'subtotal':None, 'tax_amount':None},
    57: {'invoice_number':'0424128343', 'subtotal':8000, 'tax_amount':960, 'invoice_date':'2019-10-17'},
    58: {'invoice_number':'ISORPF000000318183', 'total_amount':68, 'tax_amount':7.29, 'subtotal':60.71},
    59: {'subtotal':2763.39, 'previous_balance':-5.7, 'currency':'PHP', 'invoice_date':'2018-09-12'},
}


@pytest.mark.parametrize('f', F, ids=[str(f['id'])+' '+f['name'] for f in F])
def test_record_recovery_is_stable_and_preserves_explicit_evidence(f):
    d = recover(f)
    again = reconcile_source_layout(deepcopy(d), f['ocr']['text'], f['detail_lines'], f['detail_regions'])[0]
    assert {k:v for k,v in d.items() if not k.startswith('_')} == {k:v for k,v in again.items() if not k.startswith('_')}
    for k, expected in EXPECTED.get(f['id'], {}).items():
        assert d.get(k) == expected, k


@pytest.mark.parametrize('f', [f for f in F if f['id'] in EXPECTED], ids=lambda f:str(f['id']))
def test_full_parser_keeps_source_corrections(monkeypatch, f):
    from parser import invoice_parser as p
    from config.settings import settings
    for key in ['VISION_VERIFICATION_ENABLED','VENDOR_HEADER_RECOVERY_ENABLED','DIAGNOSTIC_TRACE_ENABLED']:
        monkeypatch.setattr(settings,key,False)
    monkeypatch.setattr(p,'extract_invoice_data',lambda *a,**k:deepcopy(f['initial']))
    o = deepcopy(f['ocr'])
    o.update(details_recovered=True,detail_lines=f['detail_lines'],detail_regions=f['detail_regions'])
    d = p._build_invoice_from_ocr(f['name'],'not-used.png',o['text'],.8,o.get('engine','paddleocr'),o['lines'],o,1).model_dump(mode='json')
    for k, expected in EXPECTED[f['id']].items():
        if expected is None and k in {'subtotal','tax_amount','vat_exempt_sales','zero_rated_sales','withholding_tax'}:
            expected = 0.0  # v5 requested storage defaults; raw recovery remains nullable.
        assert d.get(k) == expected, k


def test_professional_services_and_retail_complete_rows():
    d = recover(F[3])
    assert [(x['quantity'],x['unit_price'],x['amount']) for x in d['line_items']] == [(1,10000,10000),(1,500,500)]
    d = recover(F[17])
    assert d['line_items'][0]['amount'] == 530
    assert sum(x['amount'] for x in d['line_items']) == 1323


def test_printed_inconsistencies_remain_reviewable():
    from ai.validator import validate_extraction
    for idx in (42,43):
        d = recover(F[idx])
        assert validate_extraction(d, ocr_text=F[idx]['ocr']['text'])
    # Never repair an arbitrary row just because its arithmetic is wrong.
    d = {'subtotal':100,'total_amount':112,'line_items':[{'description':'Item','quantity':1,'unit_price':100,'amount':10}]}
    out,_ = reconcile_document_evidence(d,'Unit Price\nSubtotal\n100.00\nVAT (12%)\n12.00\nTotal Amount\n112.00',[])
    assert out['line_items'][0]['amount'] == 10


def test_currency_and_identifier_negative_controls():
    d,_ = reconcile_document_evidence({'currency':'USD','invoice_number':'INV123'},'Invoice total in USD\n100.00\nCustomer Philippines\nSeries Number\n000001-999999',[])
    assert d['currency']=='USD' and d['invoice_number']=='INV123'
    d,_ = reconcile_document_evidence({'currency':'USD'},'Amount Due (PHP)\n100.00\nJP Morgan for USD',[])
    assert d['currency']=='PHP'


def test_content_identity_recognizes_supplied_duplicates_without_guessing():
    groups = {}
    for f in F:
        groups.setdefault(document_content_key(f['ocr']['text']), []).append(f['id'])
    assert [16,42,53] in groups.values()
    assert len([v for k,v in groups.items() if k and len(v)>1]) == 13
    original = F[15]['ocr']['text']
    assert document_content_key(original) != document_content_key(original.replace('$400.00','$401.00'))
    assert document_content_key(original) != document_content_key(original.replace('02/22/2026','02/23/2026'))
    assert document_content_key('INVOICE 1') is None


def test_null_amounts_survive_database_and_export_and_duplicate_migration(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database.database import Base
    from database.models import InvoiceORM
    from database.repository import InvoiceRepository, DuplicateInvoiceError
    from database.content_migration import migrate_content_identity
    from models.invoice import Invoice
    from ai.post_processing import sanitize_for_model
    from exports.common import invoice_export_row
    engine=create_engine('sqlite:///'+str(tmp_path/'audit.db'))
    Base.metadata.create_all(engine)
    data=sanitize_for_model({'invoice_number':'A','vendor_name':'Vendor','subtotal':None,'total_amount':None,'currency':None})
    inv=Invoice(**data,raw_text=F[15]['ocr']['text'])
    with Session(engine) as s:
        repo=InvoiceRepository(s)
        row=repo.save(inv)
        assert row.subtotal == 0.0 and row.total_amount is None and row.currency is None
        with pytest.raises(DuplicateInvoiceError):
            repo.save(Invoice(invoice_number='UNVERIFIED-x',vendor_name='O Proof',raw_text=F[41]['ocr']['text']))
        s.add(InvoiceORM(invoice_number='legacy',vendor_name='Other',raw_text=F[41]['ocr']['text']))
        s.commit()
    migrate_content_identity(engine)
    migrate_content_identity(engine)
    with Session(engine) as s:
        rows=s.query(InvoiceORM).order_by(InvoiceORM.id).all()
        assert len(rows)==2 and rows[1].status=='needs_review'
        assert len([x for x in json.loads(rows[1].validation_issues) if x.startswith('Duplicate source')])==1
    row=invoice_export_row(data)
    assert row['Total Amount Due']=='' and row['Vatable Sales']==0
    assert invoice_export_row(dict(data,total_amount=0))['Total Amount Due']==0


def test_missing_money_display_and_word_boundaries():
    from utils.money_display import display_money
    from ai.validator import _longest_consonant_run
    assert display_money(None,'PHP')=='Not available'
    assert display_money(0,'PHP')=='0.00 PHP'
    assert _longest_consonant_run('Monthly Plan') < 7
    assert _longest_consonant_run('XRTKLMNPQ') >= 7


def test_actual_edit_form_keeps_blank_distinct_from_entered_zero(tmp_path):
    import ast
    from streamlit.testing.v1 import AppTest
    path = Path(__file__).parents[1]/'ui/pages/History.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    definitions = '\n'.join(ast.unparse(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'_preserve_optional_numeric','render_edit_form'})
    code = '''
import streamlit as st
import pandas as pd
STATUS_OPTIONS=['pending','needs_review','processed']
CATEGORY_OPTIONS=['Others']
def list_custom_categories(): return []
def _render_invoice_image(inv): pass
def _parse_date(value): return None
def update_invoice(invoice_id,updates): st.session_state['saved_fields']=updates
''' + definitions + '''
render_edit_form({'id':1,'invoice_number':'A','vendor_name':'Vendor','status':'needs_review','subtotal':None,'total_amount':None,'currency':None})
'''
    script=tmp_path/'edit_form.py'
    script.write_text(code,encoding='utf8')
    app=AppTest.from_file(str(script)).run()
    assert not app.exception
    amounts={x.label:x for x in app.number_input}
    assert amounts['Vatable Sales'].value is None and amounts['Total Amount Due'].value is None
    amounts['Total Amount Due'].set_value(0.0).run()
    next(b for b in app.button if b.label=='💾 Save changes').click().run()
    assert not app.exception
    saved=app.session_state['saved_fields']
    assert saved['subtotal'] is None and saved['total_amount']==0 and saved['currency'] is None
