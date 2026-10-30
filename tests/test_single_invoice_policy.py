"""A multi-invoice upload must not partially persist invoices."""
from types import SimpleNamespace
from unittest.mock import patch
from services.invoice_service import process_invoice_file


def test_two_separate_invoices_rejected_before_persistence():
    invoices = [SimpleNamespace(invoice_number='A'), SimpleNamespace(invoice_number='B')]
    with patch('services.invoice_service.parse_invoice_pages', return_value=invoices), \
         patch('services.invoice_service.SessionLocal') as session:
        result = process_invoice_file('/tmp/batch.pdf', original_filename='batch.pdf')
    assert len(result) == 1
    assert result[0]['success'] is False
    assert 'Multiple invoices' in result[0]['error']
    session.assert_not_called()
