from pathlib import Path

from ai.post_processing import (
    reconcile_vendor_address_block,
    reconcile_customer_tax_id_context,
    reconcile_blank_customer_address,
    reconcile_retail_line_items,
    reconcile_retail_tax_summary,
    reconcile_parking_tax_summary,
)
from utils.categorizer import auto_categorize


def test_history_uses_one_table_per_month_and_keeps_category_column():
    source = Path('ui/pages/History.py').read_text(encoding='utf-8')
    assert 'def render_invoice_table(' in source
    assert 'render_category_tables(' not in source
    assert '"Category"' in source
    assert 'Filter by category' in source


def test_north_star_full_vendor_address_is_rebuilt_from_header():
    ocr = '''NORTH STAR INTERNATIONAL TRAVEL INC.\nCASH SALES\n19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air\nCHARGE SALES\n1209 City of Makati NCR, Fourth District Philippines\nTel. No.: (632) 8885-7212\nSERVICE INVOICE\nVAT Reg. TIN: 000-423-215-00000\n'''
    data = {
        'vendor_name': 'NORTH STAR INTERNATIONAL TRAVEL INC.',
        'vendor_address': '1209 City of Makati NCR, Fourth District Philippines',
    }
    out, _ = reconcile_vendor_address_block(data, ocr)
    assert '19/F Trident Tower' in out['vendor_address']
    assert '312 Sen. Gil Puyat Avenue' in out['vendor_address']
    assert '1209 City of Makati' in out['vendor_address']
    assert 'Tel.' not in out['vendor_address']


def test_watsons_vendor_name_is_not_duplicated_into_address():
    ocr = '''watsons\nWATSONS PERSONAL CARE STORES\nPHILIPPINES INC\nSUPERMARKET AREA SM CITY MANILA\nCONCEPCION COR ARROCEROS & SAN MARCELINO ST\nBARANGAY 659 NCR, CITY OF MANILA\nVAT REG TIN# 214-706-591-029\n'''
    data = {
        'vendor_name': 'watsons',
        'vendor_address': 'WATSONS PERSONAL CARE STORES PHILIPPINES INC SUPERMARKET AREA SM CITY MANILA CONCEPCION COR ARROCEROS & SAN MARCELINO ST BARANGAY 659 NCR, CITY OF MANILA',
    }
    out, _ = reconcile_vendor_address_block(data, ocr)
    assert not out['vendor_address'].upper().startswith('WATSONS PERSONAL CARE STORES')
    assert 'SUPERMARKET AREA SM CITY MANILA' in out['vendor_address']
    assert 'CONCEPCION COR ARROCEROS' in out['vendor_address']


def test_retail_compact_qty_unit_patterns_fix_watsons_items():
    ocr = '''2092100032386\nSQUALENE ISSHO SOFTG\n10P11.75\nP117.50V\n2092100005793\nRHILCET PLUS 10MG 5M\n7@P24.00\nP168.00 E\n'''
    data = {
        'line_items': [
            {'description': 'SQUALENE ISSHO SOFTG 10P', 'quantity': 1, 'unit_price': 117.50, 'amount': 117.50},
            {'description': 'RHILCET PLUS 10MG 5M', 'quantity': 20, 'unit_price': 24, 'amount': 168},
        ]
    }
    out, _ = reconcile_retail_line_items(data, ocr)
    assert out['line_items'][0]['quantity'] == 10
    assert out['line_items'][0]['unit_price'] == 11.75
    assert out['line_items'][0]['amount'] == 117.50
    assert out['line_items'][1]['quantity'] == 7
    assert out['line_items'][1]['unit_price'] == 24.0
    assert out['line_items'][1]['amount'] == 168.0


def test_watsons2_tax_summary_owns_vat_and_total():
    ocr = '''SUBTOTAL\nP362.50\nAMOUNT TO PAY\nP362.50\nCASH\nP400.00\nTAX CODE\nAMOUNT\nVAT AMT\nVAT SALE\n323.66\n0.00\n38.84\n0.00\nZERO RATED SALE\nVAT EXEMPT SALE\n0.00\n0.00\nTOTAL\n323.66\n38.84\n'''
    data = {'subtotal': 323.66, 'tax_amount': 0.0, 'discount': 0.01, 'zero_rated_sales': 0.0, 'vat_exempt_sales': 0.0, 'total_amount': 323.66}
    out, _ = reconcile_retail_tax_summary(data, ocr)
    assert out['subtotal'] == 323.66
    assert out['tax_amount'] == 38.84
    assert out['total_amount'] == 362.50


def test_less_12_percent_vat_is_deduction_not_positive_vat():
    ocr = '''SUBTOTAL\nP731.48\nLESS 12%VAT\n-P95.41\nAMOUNT TO PAY\nP636.07\nTOTAL DISCOUNTS\nP159.03\nVAT AMT\n0.00\nVAT EXEMPT SALE\n795.09\n'''
    data = {'subtotal': 731.48, 'tax_amount': 95.41, 'discount': 159.03, 'vat_exempt_sales': 795.09, 'zero_rated_sales': 0.0, 'total_amount': 636.07}
    out, _ = reconcile_retail_tax_summary(data, ocr)
    assert out['tax_amount'] == 0.0
    assert out['discount'] == 159.03
    assert out['vat_exempt_sales'] == 795.09
    assert out['subtotal'] == 0.0
    assert out['total_amount'] == 636.07


def test_unlabelled_customer_id_does_not_become_tin():
    ocr = '''ID:\nCUSTOMER NAME:RONELO BUNDA\n133908001111\nCustomer Signature\n'''
    out, _ = reconcile_customer_tax_id_context({'customer_tax_id': '133-908-001-111'}, ocr)
    assert out['customer_tax_id'] is None


def test_explicit_customer_tin_is_preserved():
    ocr = '''BILLED TO: Customer\nTIN NO\n007-848-122-000\n'''
    out, _ = reconcile_customer_tax_id_context({'customer_tax_id': '007-848-122-000'}, ocr)
    assert out['customer_tax_id'] == '007-848-122-000'


def test_blank_parking_buyer_address_stays_blank_and_tax_rows_are_locked():
    ocr = '''PARKING FEE\nAMOUNT DUE\nP220.00\nVATable\nSales\n195.43\nZero Rated Sales\n0.00\nVAT-Exempt Sales\n0.00\nVAT Amount12%\n23.57\nName\nAddress:\nTIN\nBusiness Style\n'''
    data = {
        'customer_address': '0.00, :, 23.57',
        'subtotal': 195.43,
        'tax_amount': 23.57,
        'discount': 194.43,
        'vat_exempt_sales': 195.43,
        'zero_rated_sales': 0.0,
        'total_amount': 220.0,
    }
    out, _ = reconcile_blank_customer_address(data, ocr)
    out, _ = reconcile_parking_tax_summary(out, ocr)
    assert out['customer_address'] is None
    assert out['subtotal'] == 196.43
    assert out['discount'] == 0.0
    assert out['vat_exempt_sales'] == 0.0
    assert out['zero_rated_sales'] == 0.0


def test_travel_ticket_expense_categorizes_as_transportation():
    assert auto_categorize(
        'NORTH STAR INTERNATIONAL TRAVEL INC.',
        [{'description': 'SERVICE FEE (TICKET)'}],
    ) == 'Transportation'
