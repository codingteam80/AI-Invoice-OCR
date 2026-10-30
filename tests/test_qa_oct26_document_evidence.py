"""Non-LLM checks: OCR source evidence, not filenames, controls corrections."""
import json
from copy import deepcopy
from pathlib import Path
from ai.qa_source_review import reconcile_qa_source_review

QA = Path(__file__).parent / 'fixtures' / 'qa'
F = {x['name']:x for name in ('audit_round4.json','audit_round5.json') for x in json.loads((QA/name).read_text())}

def run(name, data=None):
    f=F[name]
    out,notes=reconcile_qa_source_review(deepcopy(f['saved'] if data is None else data),f['ocr']['text'],f['ocr']['lines'])
    return out,notes

def test_globe_customer_tin_with_label():
    data=deepcopy(F['Addl Invoice_Page_2.pdf']['saved']);data['customer_tax_id']=None
    d,_=run('Addl Invoice_Page_2.pdf',data)
    assert d['customer_tax_id']=='007-848-122-00000'

def test_parking_addresses_not_corrupted():
    for n in ('ParkingTickets1.png','ParkingTickets2.png'):
        d,_=run(n)
        assert 'JAZZ RESIDENCES' in d['vendor_address']

def test_north_star_vat_unchanged():
    assert run('Sample Invoice_Page_2.pdf')[0]['tax_amount']==147.53

def test_meralco_seller_buyer_columns_and_invoice_dates():
    d,_=run('INV-05.jpg')
    assert d['vendor_address']=='BACOOR BUSINESS CENTER GEN. EMILIO AGUINALDO BACOOR'
    assert 'ACM WOODST' in d['customer_address'] and 'BACOOR BUSINESS' not in d['customer_address']
    assert d['invoice_date']=='2022-09-24'
    d,_=run('INV-17.jpg')
    assert d['vendor_address']=='ROOSEVELT BUSINESS CENTER QUEZON Q.C.-KAMUNING'
    assert d['invoice_date']=='2023-05-22'
    d,_=run('INV-28.png')
    assert d['vendor_address']=='BACOOR BUSINESS CTR. GEN.EMILIO AGUINALDO BACOOR'
    assert 'SUMMERVILLS' in d['customer_address']

def test_electricity_breakdown_only_when_all_rows_reconcile():
    for name, expected_count in [('INV-05.jpg',8),('INV-17.jpg',9),('INV-28.png',9)]:
        d,_=run(name)
        assert len(d['line_items'])==expected_count
        assert abs(sum(r['amount'] for r in d['line_items'])-d['current_charges_total']) < .02
        assert all(r['quantity']==1 and r['unit_price']==r['amount'] for r in d['line_items'])
    d,_=run('INV-28.png')
    assert any(r['description']=='Government Taxes' for r in d['line_items'])
    assert any(r['description']=='FIT-All (Renewable)' for r in d['line_items'])

def test_electricity_vat_never_invented():
    d,_=run('INV-28.png',dict(F['INV-28.png']['saved'],subtotal=7404.2,tax_amount=1870.18))
    assert d['subtotal'] is None and d['tax_amount'] is None
    assert any('VATable Sales and VAT are not explicitly' in s for s in d['_normalization_issues'])

def test_pldt_credit_identification_and_totals():
    d,_=run('INV-08.jpg')
    assert d['invoice_number']=='0298050594'
    assert d['subtotal']==1279.83 and d['tax_amount']==153.58
    assert d['total_amount']==1433.26 and d['previous_balance']==-.15
    d,_=run('INV-15.jpg')
    assert d['invoice_number']=='0434287913'
    assert d['subtotal']==1159.82 and d['tax_amount']==139.18
    d,_=run('INV-07.jpg')
    assert d['subtotal']==8000 and d['tax_amount']==960

def test_converge_printed_discrepancy_flagged():
    d,_=run('INV-33.jpg')
    assert any('Conflicting printed pre-tax values' in s for s in d['_normalization_issues'])

def test_statement_tax_summary_excluded_from_items():
    d,_=run('INV-13.png')
    assert not any(r['description'].upper()=='TAXES' for r in d['line_items'])

def test_sun_brand_requires_source_evidence():
    d,_=run('INV-16.png')
    assert d['vendor_name']=='SUN CELLULAR'
    d,_=reconcile_qa_source_review({'vendor_name':'DIGITELMOBILE PHILIPPINES, INC.'},'DIGITELMOBILE PHILIPPINES, INC.')
    assert d['vendor_name']=='DIGITELMOBILE PHILIPPINES, INC.'

def test_globe_negative_adjustment_carries_review():
    d,_=run('INV-09.jpg')
    r=[r for r in d['line_items'] if r['description']=='Adjustments - Current Bill']
    assert len(r)==1 and r[0]['unit_price']==r[0]['amount']==-99
    assert any('inferred' in x.lower() for x in d['_normalization_issues'])

def test_unprinted_meralco_buyer_and_date_remain_missing():
    d,_=run('INV-19.jpg')
    assert d['customer_name'] is None and d['customer_address'] is None and d['invoice_date'] is None

def test_no_global_vat_or_zero_item_inventions():
    d,notes=reconcile_qa_source_review({'vendor_name':'Independent Store','line_items':[{'description':'VAT','amount':12}]},'Small sales receipt')
    assert d['line_items']==[{'description':'VAT','amount':12}]

def test_sun_cellular_reconciled_charges_and_date_not_guessed():
    d,_=run('INV-16.png')
    assert d['vendor_name']=='SUN CELLULAR'
    assert d['subtotal']==376.32
    assert [(x['description'],x['amount']) for x in d['line_items']]==[
        ('Monthly Service Fee',312.50), ('Usage Charges',79.84),('Discounts and Credits',-16.02)]
    assert d['invoice_date'] is None
    assert any('Statement Date label is unreadable' in note for note in d['_normalization_issues'])

def test_globe_customer_postal_address_not_reduced_to_last_line():
    d,_=run('INV-18.jpg')
    assert d['customer_address']=='Makabuhay St.Purok 1.Putingkahoy Silang Cavite 4118 Philippines'
    assert d['invoice_date'] is None

def test_negative_adjustments_are_valid_items_instead_of_validation_errors():
    from ai.validator import validate_extraction
    sample=dict(F['INV-09.jpg']['saved'])
    sample.update({'line_items':[
        {'description':'Monthly Recurring Fee','quantity':1,'unit_price':1248.21,'amount':1248.21},
        {'description':'Adjustments - Current Bill','quantity':1,'unit_price':-99,'amount':-99}
    ], 'subtotal':1248.21,'tax_amount':149.79,'current_charges_total':1299.0})
    issues=validate_extraction(sample)
    assert not any('row 2' in s and 'negative' in s for s in issues)
    assert not any('line_items sum to' in s for s in issues)

def test_cooperative_short_name_needs_legal_name_evidence():
    d,_=reconcile_qa_source_review({'vendor_name':'Pampanga II Electric Cooperative Inc.','vendor_address':'Consumer address'},
        'PAMPANGA II ELECTRIC COOPERATIVE INC. (PELCO II)\\nInvoice amount')
    assert 'PELCO II' in d['vendor_name']
    assert d['vendor_address'] is None
    d,_=reconcile_qa_source_review({'vendor_name':'Other Co-op','vendor_address':'Office Address 1'},
        'Other Co-op\\nInvoice amount')
    assert d['vendor_name']=='Other Co-op'

def test_pldt_mailing_address_stops_before_financial_summary():
    d,_=run('INV-14.jpg')
    assert 'MATINA CROSSING' in d['customer_address']
    assert 'BILL SUMMARY' not in d['customer_address']
    assert d['vendor_address'] is None  # Header incorrectly borrowed from buyer
    assert any('Statement of Account number' in x for x in d['_normalization_issues'])
    d,_=run('INV-08.jpg')
    assert d['customer_address']=='002-BL-004 REPAR HOMES ST CAMARIN DISTRICT REPAR HOMES1 CALOOCAN CITY'
    assert d['vendor_address'] is None
    d,_=run('INV-07.jpg')
    assert 'LGU MAHARLIKA HWY' in d['customer_address']
