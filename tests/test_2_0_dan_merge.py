from copy import deepcopy
from ai.party_master import apply_final_master_pass
from ai.address_ownership import enforce_seller_address_boundary


def test_existing_discount_is_not_deducted_twice():
    data = dict(subtotal=2598., tax_amount=0., discount=400., total_amount=2198.,
                line_items=[dict(description='Monthly plan', quantity=1, unit_price=2499., amount=2499.),
                            dict(description='Add-ons', quantity=1, unit_price=99., amount=99.)])
    original = deepcopy(data)
    out, notes = apply_final_master_pass(data, 'Discounts\n(400.00)')
    assert out == original
    assert data == original
    assert out['subtotal'] - out['discount'] == out['total_amount']


def test_customer_repair_precedes_boundary_cleanup():
    data = dict(vendor_tax_id='000-423-215-00000', customer_tax_id='007-848-122-000',
                vendor_address='19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air, 1209 City of Makati NCR, Fourth District Philippines, Unt 2102, 2ist Floor, Ore Corporate Cenire, Dona Julla Vargas Averue comer, Meraico Ave. Ortigas',
                customer_address='210 t e Ae coer, Meraico Ave. Ortigas')
    fixed, _ = apply_final_master_pass(data, '')
    fixed, _ = enforce_seller_address_boundary(fixed, '19/F Trident Tower 312 Sen. Gil Puyat Avenue Bel-Air\n1209 City of Makati NCR, Fourth District Philippines')
    assert fixed['customer_address'] == 'Unit 2102, 21st Floor, One Corporate Centre, Dona Julia Vargas Avenue corner Meralco Ave. Ortigas'
    assert 'Unt 2102' not in fixed['vendor_address']


def test_parser_calls_master_before_boundary_and_validates_final_data(monkeypatch):
    import sys, types
    ocr = types.ModuleType('ocr.ocr_engine')
    ocr.extract_from_file = lambda *a, **k: None
    ocr.extract_pages_from_file = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, 'ocr.ocr_engine', ocr)
    from parser import invoice_parser as parser
    data = dict(invoice_number='INV-1', invoice_date='2026-08-06', vendor_name='Example',
                subtotal=100., tax_amount=12., total_amount=112., currency='PHP',
                line_items=[dict(description='Monthly software subscription', quantity=1, unit_price=100., amount=100.)])
    monkeypatch.setattr(parser, 'extract_invoice_data', lambda *a, **k: deepcopy(data))
    monkeypatch.setattr(parser, 'recover_vendor_header_fields', lambda *a, **k: ({}, [], set()))
    monkeypatch.setattr(parser, 'verify_against_image', lambda *a, **k: ({}, []))
    for name in ['verify_handwritten_invoice_date','verify_statement_summary_crop','verify_customer_address_crop','verify_vendor_address_crop','verify_plate_number_crop']:
        monkeypatch.setattr(parser, name, lambda *a, **k: (None, []))
    order = []
    def master(d, text):
        order.append('master')
        return dict(d, vendor_address='12 Test Avenue'), ['master applied']
    def boundary(d, text):
        order.append('boundary')
        assert d['vendor_address'] == '12 Test Avenue'
        return d, []
    def validate(d, **kw):
        assert d['vendor_address'] == '12 Test Avenue'
        order.append('validation')
        return []
    monkeypatch.setattr(parser, 'apply_final_master_pass', master)
    monkeypatch.setattr(parser, 'enforce_seller_address_boundary', boundary)
    monkeypatch.setattr(parser, 'validate_extraction', validate)
    monkeypatch.setattr(parser, 'save_diagnostic_trace', lambda **kw: None)
    invoice = parser._build_invoice_from_ocr('test.png', 'test.png', 'Example\nInvoice INV-1\nVATable Sales 100.00\nVAT 12.00\nTotal 112.00', .99, 'paddleocr')
    assert order == ['master', 'boundary', 'validation']
    assert invoice.vendor_address == '12 Test Avenue'
