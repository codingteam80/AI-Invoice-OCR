"""Layout-level regression tests; synthetic OCR avoids shipping private QA scans."""
from ai.structured_table_recovery import reconcile_explicit_item_table, reconcile_explicit_zero_rows
from ai.statement_qa_recovery import (reconcile_statement_evidence, reconcile_electricity_charge_table,
                                       reconcile_tax_total_identity, reconcile_credit_balance_sign)
from ai.validator import validate_extraction
from services import report_service


def _retail(rows, subtotal):
    text='Item No.\nDescription\nQty\nUnit Price\nAmount\n'+rows+'Subtotal\n'+subtotal+'\n'
    return reconcile_explicit_item_table({'line_items': [], 'subtotal':float(subtotal.replace(',',''))},text)


def test_missing_sneakers_and_backpack_price():
    result,notes=_retail('A\nSneakers\n1\n1,999.00\n1.998.00\nB\nBackpack\n1\n1,499.00\n1,499.00\n','3,498.00')
    assert [(r['description'],r['amount']) for r in result['line_items']]== [('Sneakers',1999),('Backpack',1499)]
    assert notes


def test_rice_qty_unit_total_and_toothpaste():
    result,notes=_retail('PG-001\nRice Skg\n2\n265.00\n$30.00\nPG-002\nCooking Oil 1L\n3\n125.00\n375.00\nPG-003\nInstant Noodles (Pack)\n5\n48.00\n240.00\nPG-004\nToothpaste\n2\n89.00\n178.00\n', '1,323.00')
    assert [(x['description'],x['quantity'],x['unit_price'],x['amount']) for x in result['line_items']] == [
        ('Rice 5kg',2,265,530),('Cooking Oil 1L',3,125,375),('Instant Noodles (Pack)',5,48,240),('Toothpaste',2,89,178)]


def test_robinsons_row_alignment():
    result,_=_retail('RS-100\nFoldable Chair\n2\n895.00\n1,790.00\nRS-200\nTable Lamp\n1\n1,295.00\n1,295.00\n','3,085.00')
    assert result['line_items'][0]['amount']==1790
    assert result['line_items'][1]['unit_price']==1295


def test_zero_line_total_cannot_be_taken_from_quantity():
    d={'line_items':[{'description':'IHN additional seal', 'quantity':2, 'unit_price':0,'amount':2}]}
    out,_=reconcile_explicit_zero_rows(d,'IHN additional seal\n2\n$0.0\n$0.0\n')
    assert out['line_items'][0]['amount']==0


def test_receipt_customer_address_excludes_name_and_amount_columns():
    d={'customer_name':'TEST BUYER','customer_address':'TEST BUYER, 7 SUNRISE STREET, TOWN, Receipt Amount in PHP, Functional Amount in PHP, 30.00'}
    out,_=reconcile_statement_evidence(d,'Address\n7 SUNRISE STREET\nTOWN CITY 4010\nDescription\nReceipt Amount in PHP\n')
    assert 'TEST BUYER' not in out['customer_address']
    assert 'Amount' not in out['customer_address']


def test_globe_soa_monthly_fee_adjustment_and_address():
    d={'invoice_number':'123','subtotal':0,'line_items':[],'customer_address':'Brgy. Hill, City','discount':0}
    txt='SOA Number:1000044843719\nStatement Summary\nMonthly Recurring Fee (MRF\n2,133.76\nAdjustments-Current Bill\n(99.00)\nVAT\n256.05\nTotal Current Bill\nP2.290.81\n'
    out,_=reconcile_statement_evidence(d,txt)
    assert out['invoice_number']=='1000044843719'
    assert out['subtotal']==2133.76
    assert out['line_items'][0]['amount']==2133.76
    assert out['line_items'][1]['amount']==-99.0
    assert out['discount']==99.0
    assert out['current_charges_total']==2290.81


def test_pldt_billing_identity_and_negative_total_review():
    d={'invoice_number':'incorrect','subtotal':2628.9,'tax_amount':281.67,
       'total_amount':-1147.9,'current_charges_total':2628.9,'previous_balance':2114.13,
       'discount':4058.47,'line_items':[]}
    txt='Statement of Account No.: 0448162-171\nBILL SUMMARY\nMonthly Service Fee and Other Basic Charges\n2,320.53\nUsage Charges\n26.70\nValue Added Tax\n281.67\nTotal Current Charges\n2,628.90\n'
    out,_=reconcile_statement_evidence(d,txt)
    assert out['invoice_number']=='0448162-171'
    assert out['subtotal']==2347.23
    assert not out['discount']
    issues=validate_extraction(out)
    assert any('calculation needs review' in x for x in issues)
    assert any('negative' in x for x in issues)


def test_smart_logo_and_missing_buyer_address():
    d={'vendor_name':'DSmart','customer_address':'Billing Period Charges, Total, Monthly Plan'}
    out,_=reconcile_statement_evidence(d,'DSmart\nJuan Dela Cruz\nInvoice Date\nBilling Period\nSmart Consnacaions Inc\n')
    assert out['vendor_name']=='Smart'
    assert out['customer_address'] is None


def test_meralco_vendor_office_address_with_interleaved_logo():
    d={'vendor_address':'buyer address'}
    out,_=reconcile_statement_evidence(d,'MAKATI BUSINESS CTR\nMERALCO\nKAMAGONG\nMAKATI CITY\n')
    assert out['vendor_address']=='MAKATI BUSINESS CTR, KAMAGONG, MAKATI CITY'


def test_electric_bill_ignores_chart_and_uses_eustotal():
    txt='BREAKDOWN OF ELECTRICITY CHARGES\nBILL SUBGROUP\nEUSTOTAL\nPERCENTAGE\nGeneration\n1.862.04*\n46.62%\nTransmission\n231.75\n7.53%\nSystem Lass\n160.58\n4.10%\nDistribution\n1,227.15\n27.86%\nSubeitties\n29.19\n0.65%\nGovemment Taxes\n449.97\n20.01%\nUniversal Charges\n83.32\n2.01%\nOther Charges\n0.88\n0.80%\nTotal Current Charges\n4,383.94\n'
    out,_=reconcile_electricity_charge_table({'line_items':[]},txt)
    assert [x['amount'] for x in out['line_items']][:3]==[1862.04,231.75,160.58]
    assert out['line_items'][-1]['amount']==0.88


def test_vat_digits_recovered_from_independent_total_and_rate():
    d={'subtotal':4888.0,'total_amount':5474.56,'tax_amount':566.56}
    out,_=reconcile_tax_total_identity(d,'VAT (12%)\n566.56\n')
    assert out['tax_amount']==586.56


def test_monthly_spend_use_invoice_dates_by_default(monkeypatch):
    rows=[{'invoice_date':'2025-12-01','date_uploaded':'2026-10-20','total_amount':20.0},
          {'invoice_date':'2026-02-18','date_uploaded':'2026-10-20','total_amount':30.0}]
    monkeypatch.setattr(report_service,'list_invoices',lambda limit:rows)
    assert report_service.spend_by_month()=={'2025-12':20.0,'2026-02':30.0}
    assert report_service.spend_by_month(date_basis='uploaded')=={'2026-10':50.0}


def test_legitimate_negative_adjustment_is_not_error():
    obj={'subtotal':2133.76,'tax_amount':256.05,'discount':99.0,'total_amount':2290.81,
         'line_items':[{'description':'Adjustments-Current Bill','quantity':1,'unit_price':-99.0,'amount':-99.0}]}
    errors=validate_extraction(obj)
    assert not any('negative amount' in x or 'negative unit_price' in x for x in errors)


def test_carry_forward_credit_not_debit_if_payable_reconciles():
    d={'current_charges_total':2290.81, 'previous_balance':1499.0, 'total_amount':791.81,
       'line_items':[{'description':'Remaining Balance','quantity':1,'unit_price':1499.0,'amount':1499.0}]}
    out,notes=reconcile_credit_balance_sign(d,'Remaining Balance from Previous Bill\nP1,499.00')
    assert out['previous_balance']==-1499
    assert out['line_items'][0]['amount']==-1499
    assert notes


def test_negative_credit_does_not_hide_inconsistent_printed_total():
    d={'current_charges_total':2628.9, 'previous_balance':2114.13, 'total_amount':-1147.90,
       'line_items':[{'description':'Remaining Balance','quantity':1,'unit_price':2114.13,'amount':2114.13}]}
    txt='BILL SUMMARY\nBalance from Previous Bil\n1,385.87\nLess:Payments Received-Thank You!\n3,500.00)\nRemaining Balance from Previous Bill\n2,114.13'
    out,_=reconcile_credit_balance_sign(d,txt)
    assert out['previous_balance']==-2114.13
    assert any('does not match total_amount/Amount to Pay' in x for x in validate_extraction({**out,'subtotal':2347.23,'tax_amount':281.67,'discount':0}))
