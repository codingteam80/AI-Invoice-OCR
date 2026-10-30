"""Critical invariants when combining the Oct 26 evidence branch with Oct 23 UI/vision."""
from unittest.mock import patch
from types import SimpleNamespace


def test_oct26_source_evidence_is_preserved():
    from ai import source_evidence, statement_evidence, qa_source_review
    from ocr import detail_recovery
    from parser import page_groups
    assert callable(qa_source_review.reconcile_qa_source_review)
    assert hasattr(source_evidence, 'reconcile_source_layout')
    assert hasattr(statement_evidence, 'reconcile_statement_evidence')
    assert hasattr(detail_recovery, 'recover_detail_lines')
    assert hasattr(page_groups, 'group_invoice_pages')


def test_supported_ocr_field_and_cell_evidence_retained():
    from ai.confidence import score_extraction
    sample = {'vendor_name': 'Store ABC', 'invoice_number': 'INV-101',
              'total_amount': 112, 'subtotal': 100, 'tax_amount': 12,
              'line_items': [{'description': 'Service ABC', 'quantity': 1, 'unit_price': 100, 'amount': 100}]}
    predicted = score_extraction(sample, .98, [], ocr_engine='paddleocr',
                                 ocr_text='Store ABC\nINV-101\nService ABC 1 100.00 100.00\nVAT 12.00\nTOTAL 112.00')
    assert predicted.overall_confidence >= 0
    assert 'vendor_name' in predicted.field_evidence
    assert 'line_items.0.unit_price' in predicted.field_evidence
    assert any(f.field == 'line_items.0.amount' for f in predicted.field_confidences)


def test_approved_export_never_contains_hidden_fields():
    from exports.common import EXPORT_COLUMNS, invoice_export_row
    forbidden = {'Due Date', 'Tax Rate', 'Payment Terms', 'Customer Contact', 'Current Charges Total', 'Previous Balance'}
    assert not forbidden.intersection(EXPORT_COLUMNS)
    row = invoice_export_row({'due_date': '2099-01-01', 'payment_terms': 'Secret',
                              'customer_contact': 'Secret', 'current_charges_total': 999})
    assert not forbidden.intersection(row)


def test_preflight_scope_is_shared_across_file_scopes_but_not_batches():
    from ai import vision_budget
    from ai import llm_client
    checks = []
    def fake_get(url, timeout):
        checks.append(url)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {'models': [{'name': 'qwen2.5vl:7b'}]})
    with patch.object(vision_budget.requests, 'get', fake_get), \
         patch.object(vision_budget.settings, 'VISION_VERIFICATION_ENABLED', True), \
         patch.object(vision_budget.settings, 'VISION_MODEL', 'qwen2.5vl:7b'), \
         patch.object(llm_client, 'get_mode', return_value='ollama_only'):
        with vision_budget.vision_batch():
            vision_budget.preflight_local_vision_model()
            with vision_budget.vision_batch():
                assert vision_budget.preflight_local_vision_model()['available'] is True
        with vision_budget.vision_batch():
            vision_budget.preflight_local_vision_model()
    assert len(checks) == 2


def test_missing_values_display_as_zero_without_mutating_invoice():
    import runpy
    from pathlib import Path
    formatter = runpy.run_path(str(Path(__file__).parents[1] / 'ui/components/financial_display.py'))['format_history_amount']
    invoice = {'subtotal': None}
    assert formatter(invoice['subtotal']) == '0.00'
    assert invoice['subtotal'] is None


def test_negative_billing_discrepancy_gets_explicit_warning():
    from ai.validator import validate_extraction
    result = validate_extraction({'invoice_number': 'X1', 'vendor_name': 'Test Vendor',
                                  'total_amount': -1147.90, 'subtotal': 2347.23,
                                  'current_charges_total': 2628.90, 'previous_balance': 2114.13})
    assert any('Total Amount Due calculation needs review' in value for value in result)
