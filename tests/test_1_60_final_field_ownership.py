import json
from pathlib import Path

import cv2
import numpy as np

from ai.post_processing import reconcile_customer_name, reconcile_customer_tax_id_context
from ai import vision_verifier


def test_unfilled_buyers_name_placeholder_is_cleared():
    ocr = """BUYERS NAME:\nTIN #:\nADDRESS :\nBUS STYLE\n"""
    out, notes = reconcile_customer_name({'customer_name': 'BUYERS NAME'}, ocr)
    assert out['customer_name'] is None
    assert any('cleared' in n for n in notes)


def test_globe_customer_tin_geometry_survives_interleaved_columns():
    ocr = """Customer TIN\nInvoice Date\nPoblacion 2,Cabadbaran\n006-116-186-00000\n01/15/25\n"""
    lines = [
        {'text': 'Customer TIN', 'bbox': [[500,100],[650,100],[650,120],[500,120]]},
        {'text': 'Invoice Date', 'bbox': [[700,100],[820,100],[820,120],[700,120]]},
        {'text': 'Poblacion 2,Cabadbaran', 'bbox': [[100,130],[350,130],[350,150],[100,150]]},
        {'text': '006-116-186-00000', 'bbox': [[500,130],[675,130],[675,150],[500,150]]},
        {'text': '01/15/25', 'bbox': [[700,130],[790,130],[790,150],[700,150]]},
    ]
    out, notes = reconcile_customer_tax_id_context(
        {'customer_tax_id': '006-116-186-00000'}, ocr, lines
    )
    assert out['customer_tax_id'] == '006-116-186-00000'
    assert any('explicit Customer TIN geometry' in n for n in notes)


def test_generic_customer_id_is_still_not_a_tin_with_geometry():
    ocr = """ID:\nCUSTOMER NAME:RONELO BUNDA\n133908001111\nCustomer Signature\n"""
    lines = [
        {'text': 'ID:', 'bbox': [[50,100],[90,100],[90,120],[50,120]]},
        {'text': 'CUSTOMER NAME:RONELO BUNDA', 'bbox': [[50,125],[350,125],[350,150],[50,150]]},
        {'text': '133908001111', 'bbox': [[190,100],[360,100],[360,125],[190,125]]},
    ]
    out, _ = reconcile_customer_tax_id_context({'customer_tax_id': '133-908-001-111'}, ocr, lines)
    assert out['customer_tax_id'] is None


def _write_test_image(tmp_path: Path) -> str:
    path = tmp_path / 'test.png'
    img = np.full((500, 900, 3), 255, dtype=np.uint8)
    cv2.imwrite(str(path), img)
    return str(path)


def test_focused_vendor_address_crop_can_correct_character_level_read(monkeypatch, tmp_path):
    image = _write_test_image(tmp_path)
    monkeypatch.setattr(vision_verifier.settings, 'VISION_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(vision_verifier.settings, 'VENDOR_ADDRESS_VERIFY_ENABLED', True)
    monkeypatch.setattr(
        vision_verifier,
        '_post_generate',
        lambda prompt, b64, trace: json.dumps({
            'vendor_address': '32nd Street corner 7th Avenue, Bonifacio Global City, Taguig, Philippines 1634'
        }),
    )
    current = '22nd Street corner 7th Avenue, Banilad Global City, Taguig, Philippines 1634'
    value, notes = vision_verifier.verify_vendor_address_crop(image, current, [], trace={})
    assert value.startswith('32nd Street')
    assert 'Bonifacio Global City' in value
    assert notes


def test_plate_crop_can_correct_d_vs_o(monkeypatch, tmp_path):
    image = _write_test_image(tmp_path)
    monkeypatch.setattr(vision_verifier.settings, 'VISION_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(vision_verifier.settings, 'PLATE_NUMBER_VERIFY_ENABLED', True)
    monkeypatch.setattr(
        vision_verifier,
        '_post_generate',
        lambda prompt, b64, trace: json.dumps({'plate_number': 'NDG9821'}),
    )
    lines = [
        {'text': 'PLATE NO.:NOG9821', 'bbox': [[100,150],[260,150],[260,180],[100,180]]},
    ]
    value, notes = vision_verifier.verify_plate_number_crop(image, lines, trace={})
    assert value == 'NDG9821'
    assert notes
