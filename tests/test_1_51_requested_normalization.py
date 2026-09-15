from ai.post_processing import (
    post_process,
    normalize_vendor_display_name,
    clean_customer_address,
    apply_vision_corrections_with_financial_gate,
)


def test_requested_vendor_display_normalizations():
    assert normalize_vendor_display_name('Innove Communications, Inc.') == 'Globe Business: Innove Communications, Inc.'
    assert normalize_vendor_display_name('Sycip, gorres, velayo & co') == 'SGV: Sycip, Gorres, Velayo & CO.'
    assert normalize_vendor_display_name('RESPONSIBLE SERVICES INC.') == 'TRI-Q: RESPONSIBLE SERVICES INC.'


def test_customer_address_removes_po_ref_label_but_keeps_address_after_it():
    raw = 'Unt 2102, 21st Floor, One Corporate Cenire, Dona Jula Vargas Averue coner PO Ref No.: Meraico Ave. Ortigas'
    cleaned = clean_customer_address(raw)
    assert 'PO Ref' not in cleaned
    assert 'Meraico Ave. Ortigas' in cleaned
    assert cleaned.startswith('Unt 2102')


def test_post_process_applies_vendor_and_address_normalization():
    out = post_process({
        'vendor_name': 'Innove Communications, Inc.',
        'customer_address': '21/F One Corporate Center, PO Reference Number: Meralco Ave., Ortigas',
        'line_items': [],
    })
    assert out['vendor_name'] == 'Globe Business: Innove Communications, Inc.'
    assert out['customer_address'] == '21/F One Corporate Center, Meralco Ave., Ortigas'


def test_gate_rejects_north_star_less_vat_as_discount():
    current = {
        'subtotal': 2468.00,
        'tax_amount': 296.16,
        'discount': 2468.00,
        'zero_rated_sales': None,
        'vat_exempt_sales': 0.0,
        'total_amount': 2764.16,
    }
    corrections = {'discount': 296.16}
    mismatches = [{'field': 'discount', 'image_shows': '296.16'}]
    out, locked, notes, accepted, rejected = apply_vision_corrections_with_financial_gate(
        current, corrections, mismatches
    )
    assert out['discount'] == 0.0
    assert 'discount' in locked
    assert accepted['discount'] == 0.0
    assert rejected['discount'] == 296.16
