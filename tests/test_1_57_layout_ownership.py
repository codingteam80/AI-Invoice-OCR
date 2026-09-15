from ai.post_processing import reconcile_customer_address_layout, reconcile_vendor_name
from ai.vision_verifier import _is_true_invoice_date_label


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_sgv_bill_to_block_becomes_customer_address_and_clears_contact():
    lines=[
        {'text':'Bill To:','bbox':box(190,458,303,498)},
        {'text':'tsukiden global solutions inc.','bbox':box(183,498,775,554)},
        {'text':'U 2102 21/F One Corporate Ctr Condo','bbox':box(183,546,775,602)},
        {'text':'J Vargas cor Meralco Ave','bbox':box(190,601,577,646)},
        {'text':'Brgy San Antonio Ortigas Center','bbox':box(186,650,687,698)},
        {'text':'Pasig City 1605','bbox':box(190,701,435,738)},
        {'text':'Client No.:','bbox':box(2692,698,2860,738)},
        {'text':'0011670080','bbox':box(3072,701,3277,742)},
    ]
    data={'customer_name':'tsukiden global solutions inc.','customer_address':None,
          'customer_contact':'U 2102 21/F One Corporate Ctr Condo, J Vargas cor Meralco Ave, Brgy San Antonio Ortigas Center, Pasig City 1605'}
    out,notes=reconcile_customer_address_layout(data,lines)
    assert out['customer_address']=='U 2102 21/F One Corporate Ctr Condo, J Vargas cor Meralco Ave, Brgy San Antonio Ortigas Center, Pasig City 1605'
    assert out['customer_contact'] is None
    assert notes


def test_globe_customer_address_preserves_2101_and_excludes_account_number():
    lines=[
        {'text':'Tsukiden Global Solutions Inc','bbox':box(252,573,927,651)},
        {'text':'ERIC C FRANCISCO','bbox':box(341,646,650,713)},
        {'text':'2101 One Corporate Center Julia','bbox':box(333,697,853,760)},
        {'text':'Account Number','bbox':box(1817,635,2027,688)},
        {'text':'876569970','bbox':box(1817,679,2001,728)},
        {'text':'Vargas Cor Meralco Ave Ortigas Center','bbox':box(333,741,938,808)},
        {'text':'Ortigas,Pasig','bbox':box(337,789,547,837)},
        {'text':'Metro Manila,1605','bbox':box(333,832,628,885)},
        {'text':'Invoice Date','bbox':box(1814,825,1968,878)},
    ]
    data={'customer_name':'Tsukiden Global Solutions Inc','customer_address':'One Corporate Center Julia Vargas Cor Meralco Ave Ortigas Center, Ortigas, Pasig, Metro Manila, 1605'}
    out,_=reconcile_customer_address_layout(data,lines)
    assert out['customer_address'].startswith('2101 One Corporate Center Julia')
    assert '876569970' not in out['customer_address']


def test_printer_footer_vendor_replaced_by_split_header_issuer():
    ocr='''Emerald Mansion\nCondominium Association Ine.\nG/F Emerald Mansion Emerald Ave Ortigas Ctr.\nSALES\nINVOICE\n7512\nBIR Authority to Prnt No.:0CN:043AU20250000013398\nAdelfa F. Ortega - Prop.\nNONVAT Reg.TIN:465-709-231-00000\nDate of ATP:November 262025\nPrinter's Accreditation No. 032MP2021000000039\nADEL PRINTING SERVICES,Stall B1,Cartimar Bldg\nAccreditation Date:12-10-2021 Expiration Date:12-09-2026'''
    out,notes=reconcile_vendor_name({'vendor_name':'ADEL PRINTING SERVICES'},ocr)
    assert out['vendor_name']=='Emerald Mansion Condominium Association Inc.'
    assert notes


def test_invoice_date_label_ownership_excludes_printer_and_admin_dates():
    assert _is_true_invoice_date_label('Date:')
    assert _is_true_invoice_date_label('Invoice Date')
    assert not _is_true_invoice_date_label('Date of ATP: November 26 2025')
    assert not _is_true_invoice_date_label('Accreditation Date: 12-10-2021')
    assert not _is_true_invoice_date_label('Expiration Date: 12-09-2026')
    assert not _is_true_invoice_date_label('Issue Date: August 06, 2026')
