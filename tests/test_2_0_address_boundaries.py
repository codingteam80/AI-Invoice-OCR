from ai.address_ownership import seller_header_address, enforce_seller_address_boundary
from ai.vision_verifier import _address_from_header_ocr, _vendor_header_field_needs_recovery


HEADER = '''Sycip, gorres, velayo & CO.
Invoice
VAT Reg. TIN
000-502-547-00000
6760 Ayala Avenue, San Lorenzo
NCR,Fourth District
SGV
1226 City of Makati
Philippines
Building a better
Tel.No.:+63288910307
working world
Bill To:
Tsukiden global solutions inc.
U 2102 21/F One Corporate Ctr Condo
J Vargas cor Meralco Ave
Brgy San Antonio Ortigas Center'''
EXPECTED = '6760 Ayala Avenue, San Lorenzo, NCR,Fourth District, 1226 City of Makati, Philippines'


def test_sgv_fallback_stops_at_country_before_slogan_and_buyer():
    assert _address_from_header_ocr(HEADER) == EXPECTED
    polluted = EXPECTED + ', Building a better, J Vargas cor Meralco Ave, Brgy San Antonio Ortigas Center'
    fixed, notes = enforce_seller_address_boundary({'vendor_address': polluted}, HEADER)
    assert fixed['vendor_address'] == EXPECTED and notes


def test_buyer_boundary_without_country_or_contact():
    for label in ['Bill To:', 'Billed To:', 'Sold To:', 'Customer Name:', 'Client Address:']:
        text = 'Example Ltd\n12 Sample Street\nExample City\n' + label + '\n99 Buyer Avenue'
        assert seller_header_address(text) == '12 Sample Street, Example City'


def test_first_country_with_postcode_and_company_name():
    text = 'Example Philippines Inc\n32 Example Street\nTaguig, Philippines 1634\n9/F Other Tower\nCebu City 6000'
    assert seller_header_address(text) == '32 Example Street, Taguig, Philippines 1634'


def test_preserve_richer_or_different_address_without_exact_evidence():
    for address in [EXPECTED, '55 Different Avenue, Makati, Philippines, Annex 2',
                    '6760 Ayala Avenue, San Lorenzo, NCR, Fourth District, 1226 City of Makati, Philippines']:
        assert enforce_seller_address_boundary({'vendor_address': address}, HEADER)[0]['vendor_address'] == address
    assert enforce_seller_address_boundary({'vendor_address': EXPECTED + ', Annex 2'}, 'Ayala Avenue')[1] == []


def test_company_name_in_garbled_address_triggers_existing_recovery():
    data = {'vendor_name': 'Globe Business: Innove Communications, Inc.',
            'vendor_address': 'The Gbs Towr Cabu SamarL Innove Communications, Inc',
            'vendor_tax_id': '000-360-916-00000'}
    assert _vendor_header_field_needs_recovery(data)[0]
    data['vendor_address'] = '32nd Street corner 7th Avenue, Bonifacio Global City, Taguig, Philippines 1634'
    assert not _vendor_header_field_needs_recovery(data)[0]


def test_calls_do_not_share_address_state():
    for _ in range(3):
        assert seller_header_address(HEADER) == EXPECTED
        assert seller_header_address('12 Other Street\nOther City\nBill To:\n99 Wrong Avenue') == '12 Other Street, Other City'
        assert seller_header_address('Bill To:\n99 Wrong Avenue') is None
