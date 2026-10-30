import json
from pathlib import Path
from copy import deepcopy
import pytest
from ai.source_evidence import reconcile_source_layout, is_unused_form, review_fallback
from ai.validator import validate_extraction
from utils.data_quality import duplicate_key

FIXTURES=json.loads((Path(__file__).parent/'fixtures/qa/output_round2.json').read_text(encoding='utf8'))

def corrected(name):
    f=FIXTURES[name]
    return reconcile_source_layout(deepcopy(f['saved'] or {}),f['ocr']['text'],f['detail_lines'],f['detail_regions'])[0]

@pytest.mark.parametrize('name',['INV-38.png','INV-38-2.png'])
def test_hardware_table_and_tax(name):
    d=corrected(name)
    assert d['subtotal']==4888
    assert d['tax_amount']==586.56
    assert [(i['quantity'],i['unit_price'],i['amount']) for i in d['line_items']]==[(1,3495,3495),(1,695,695),(2,349,698)]
    assert not any('quantity * unit_price' in s for s in validate_extraction(d))
    if name=='INV-38.png':
        assert d['vendor_tax_id']=='200-531-071-0000'
        assert 'Item No' not in d['customer_address']

@pytest.mark.parametrize('name',['INV-29.jpg','INV-40.png'])
def test_explicit_zero_row(name):
    d=corrected(name)
    assert d['line_items'][-1]['amount']==0
    assert d['line_items'][-1]['unit_price']==0
    assert d['line_items'][-1]['quantity']==2
    assert sum(i['amount'] for i in d['line_items'])==400

def test_punctuation_spacing_duplicate_identity():
    assert duplicate_key('NOTARIZE,INC.DBA PROOF','NOTARIZE185857')==duplicate_key('NOTARIZE, INC. DBA PROOF','NOTARIZE185857')
    assert duplicate_key('ACE HARDWARE PHILIPPINES INC','1234567890')!=duplicate_key('SHOPEE PHILIPPINES INC.','1234567890')
    assert duplicate_key('Acme','001-A')!=duplicate_key('Acme','001A')

def test_unused_receipt_rejected_before_llm(monkeypatch):
    from parser import invoice_parser as p
    f=FIXTURES['INV-36.jpg']
    monkeypatch.setattr(p,'extract_invoice_data',lambda *a,**kw:pytest.fail('Blank receipt must not call the model'))
    with pytest.raises(p.InvoiceParsingError,match='Unused blank receipt template'):
        p._build_invoice_from_ocr('receipt.jpg','receipt.jpg',f['ocr']['text'],.9,'paddleocr',f['ocr']['lines'])

@pytest.mark.parametrize('name',['INV-20.jpg','INV-34.jpg','507f5eb6a61848cf998ace90fead58a2.pdf'])
def test_timeout_recoverable_documents(name):
    f=FIXTURES[name]
    assert not is_unused_form(f['ocr']['text'])
    d=review_fallback(f['ocr']['text'],f['ocr']['lines'])
    assert d is not None
    assert any('manual review' in x for x in d['_normalization_issues'])

def test_billing_addresses():
    d=corrected('INV-01.pdf')
    assert d['vendor_address']=='ALABANG BUS. CENTER PRIME ST. MUNTINLUPA'
    assert d['customer_address']=='AVIDA LAND CORP. 0001 LT(212) PRIME ST. ST MADRIGAL BUSINESS PARK BGY AYALA-ALABANG MUNTINLUPA METRO MANILA'
    d=corrected('INV-04.pdf')
    assert d['customer_address']=='UNIT 3415 FLORA VISTA 345 PEACOCK ST BGY COMMONWEALTH QUEZON CITY METRO MANILA 3QNNDC0157 QUEZON CITY'

@pytest.mark.parametrize('name',['ParkingTickets1.png','ParkingTickets2.png'])
def test_parking_source_fields(name):
    d=corrected(name)
    assert d['vendor_address']=='JAZZ RESIDENCES CORNER METROPOLITAN AVENUE AND NICANOR GARCIA ST. BEL-AIR C NCR FOURTH DISTRICT PHILIPPINES 1209'
    assert d['plate_number']=='NDG9821'
    assert d['vat_exempt_sales']==0
    assert d['tax_amount']==(23.57 if name.endswith('1.png') else 25.71)
    assert d['current_charges_total'] is None

def test_north_star_blank_tax_cells_and_address():
    d=corrected('Sample Invoice_Page_2.pdf')
    assert d['tax_amount']==147.53
    assert d['zero_rated_sales'] is None
    assert d['vat_exempt_sales'] is None
    assert d['customer_address']=='Unit 2102, 21st Floor, One Corporate Centre, Dona Julia Vargas Avenue corner Meralco Ave. Ortigas'

def test_globe_correct_addon_and_master_address():
    d=corrected('Addl Invoice_Page_2.pdf')
    assert d['subtotal']==2598
    assert d['discount']==400
    assert d['line_items'][1]=={'description':'Add-ons','quantity':1,'unit_price':99,'amount':99}
    assert corrected('GlobeInvoice1.jpg')['vendor_address']==d['vendor_address']

def test_row_arithmetic_mismatch_remains_visible():
    d=corrected('INV-38.png');d['line_items'][0]['quantity']=3
    assert any('quantity * unit_price' in s for s in validate_extraction(d))

def test_pdf_all_pages_and_selection():
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_string('''
import streamlit as st
from ui.components.document_pages import render_document_pages
render_document_pages(['first','second','third','fourth'],7,st.text)
''').run()
    assert not app.exception
    assert [x.value for x in app.text]==['first','second','third','fourth']
    app.selectbox[0].select('Page 3').run()
    assert [x.value for x in app.text]==['third']

def test_duplicate_migration_retains_legacy_rows_and_rejects_new_copy(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database.database import Base
    from database.models import InvoiceORM
    from database.repository import InvoiceRepository,DuplicateInvoiceError
    from database.qa_migration import migrate_duplicate_identity
    from models.invoice import Invoice
    engine=create_engine('sqlite:///'+str(tmp_path/'dup.db'))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a=InvoiceORM(invoice_number='NOTARIZE185857',vendor_name='NOTARIZE,INC.DBA PROOF',total_amount=400,currency='USD',status='processed',duplicate_key='old-a')
        b=InvoiceORM(invoice_number='NOTARIZE185857',vendor_name='NOTARIZE, INC. DBA PROOF',total_amount=400,currency='USD',status='processed',duplicate_key='old-b')
        s.add_all([a,b]);s.commit()
    migrate_duplicate_identity(engine);migrate_duplicate_identity(engine)
    with Session(engine) as s:
        rows=s.query(InvoiceORM).order_by(InvoiceORM.id).all()
        assert len(rows)==2
        assert rows[0].duplicate_key is not None
        assert rows[1].duplicate_key is None and rows[1].status=='needs_review'
        with pytest.raises(DuplicateInvoiceError):
            InvoiceRepository(s).save(Invoice(invoice_number='NOTARIZE185857',vendor_name='NOTARIZE, INC. DBA PROOF',total_amount=400,currency='USD'))

@pytest.mark.parametrize('name',['INV-20.jpg','INV-34.jpg','507f5eb6a61848cf998ace90fead58a2.pdf'])
def test_full_pipeline_after_timeout_keeps_review_status(monkeypatch,name):
    from parser import invoice_parser as p
    from config.settings import settings
    f=FIXTURES[name];o=deepcopy(f['ocr'])
    o.update(details_recovered=True,detail_lines=f['detail_lines'],detail_regions=f['detail_regions'])
    monkeypatch.setattr(settings,'VISION_VERIFICATION_ENABLED',False)
    monkeypatch.setattr(settings,'VENDOR_HEADER_RECOVERY_ENABLED',False)
    monkeypatch.setattr(settings,'DIAGNOSTIC_TRACE_ENABLED',False)
    def failed(*a,**kw):raise p.ExtractionError('Ollama request timed out')
    monkeypatch.setattr(p,'extract_invoice_data',failed)
    inv=p._build_invoice_from_ocr(name,'missing-preview.png',o['text'],.7,'paddleocr',o['lines'],o,1)
    assert inv.status=='needs_review'
    assert any('OCR-only recovery' in s for s in inv.validation_issues)
    if name=='INV-20.jpg':assert inv.invoice_number.startswith('UNVERIFIED-')

@pytest.mark.parametrize('name',['INV-38.png','INV-38-2.png','INV-29.jpg','ParkingTickets2.png','Sample Invoice_Page_2.pdf','Addl Invoice_Page_2.pdf'])
def test_pipeline_retains_final_source_corrections(monkeypatch,name):
    from parser import invoice_parser as p
    from config.settings import settings
    f=FIXTURES[name];o=deepcopy(f['ocr'])
    o.update(details_recovered=True,detail_lines=f['detail_lines'],detail_regions=f['detail_regions'])
    for setting in ('VISION_VERIFICATION_ENABLED','VENDOR_HEADER_RECOVERY_ENABLED','DIAGNOSTIC_TRACE_ENABLED'):
        monkeypatch.setattr(settings,setting,False)
    monkeypatch.setattr(p,'extract_invoice_data',lambda *a,**kw:deepcopy(f['initial']))
    inv=p._build_invoice_from_ocr(name,'missing-preview.png',o['text'],.9,'paddleocr',o['lines'],o,1)
    expected=corrected(name)
    keys=['subtotal','tax_amount','customer_address','plate_number','zero_rated_sales','vat_exempt_sales']
    if name=='INV-38.png':keys.append('vendor_tax_id')
    if name not in ('INV-38-2.png',):keys.append('vendor_address')
    for key in keys:
        if key in expected:
            value = expected[key]
            if value is None and key in {'subtotal','tax_amount','vat_exempt_sales','zero_rated_sales','withholding_tax'}:
                value = 0.0  # v5 storage policy; source evidence assertions stay nullable.
            assert getattr(inv,key)==value,key
    if name in ('INV-38.png','INV-38-2.png','INV-29.jpg','Addl Invoice_Page_2.pdf'):
        assert [i.model_dump() for i in inv.line_items]==expected['line_items']

def test_completed_receipt_is_not_rejected_as_blank():
    text=FIXTURES['INV-36.jpg']['ocr']['text']
    assert is_unused_form(text)
    assert not is_unused_form(text.replace('TOTAL AMOUNT DUE','TOTAL AMOUNT DUE 100.00'))

def test_partial_geometry_does_not_drop_existing_rows():
    from ai.source_evidence import _table_items,geometry
    f=deepcopy(FIXTURES['INV-38.png'])
    cells=geometry(f['detail_lines'])
    assert len(_table_items(cells))==3
    # Remove the only quantity in the final item row.
    numeric=[c for c in cells if c['text']=='2']
    assert numeric
    broken=[c for c in cells if c is not numeric[0]]
    assert _table_items(broken)==[]
