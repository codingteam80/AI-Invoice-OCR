import json
from pathlib import Path

import cv2
import numpy as np

from ai.post_processing import (
    reconcile_loyalty_balance_context,
    reconcile_customer_tax_id_context,
    reconcile_issuer_printer_ownership,
    add_remaining_balance_line_item,
)
from ai import vision_verifier


def test_watsons_open_close_balance_is_not_previous_invoice_balance():
    ocr = """Open Balance\n15\nTicket Earn\n0\nClose Balance\n15\nAMOUNT TO PAY\n362.50\n"""
    data = {
        "previous_balance": 15.0,
        "line_items": [
            {"description": "SANGOBION CAPX100", "quantity": 1, "unit_price": 152.50, "amount": 152.50},
            {"description": "Remaining Balance", "quantity": 1, "unit_price": 15.0, "amount": 15.0},
        ],
    }
    out, notes = reconcile_loyalty_balance_context(data, ocr)
    assert out["previous_balance"] is None
    assert all(i["description"] != "Remaining Balance" for i in out["line_items"])
    out2, _ = add_remaining_balance_line_item(out)
    assert all(i["description"] != "Remaining Balance" for i in out2["line_items"])
    assert notes


def test_genuine_previous_balance_is_not_cleared_by_loyalty_guard():
    ocr = """Previous Bill Activity\nRemaining Balance\n1,749.51\nAmount to Pay\n5,947.51\n"""
    out, _ = reconcile_loyalty_balance_context({"previous_balance": 1749.51, "line_items": []}, ocr)
    assert out["previous_balance"] == 1749.51


def test_triq_til_ocr_variant_preserves_customer_tin():
    ocr = """CUSTOMER:\nTSUKIDEN GLOBAL SOLUTIONS, INC.\nRegistered Name:\nTSUKIDEN GLOBAL SOLUTIONS, INC.\nTIL\n#007-848-122-000\nBusiness Address:\n21st Floor One Corporate Center\n"""
    out, notes = reconcile_customer_tax_id_context({"customer_tax_id": "007-848-122-000"}, ocr, [])
    assert out["customer_tax_id"] == "007-848-122-000"
    assert notes


def test_generic_customer_id_still_does_not_become_tin_after_til_tolerance():
    ocr = """ID:\nCUSTOMER NAME:RONELO BUNDA\n133908001111\nCustomer Signature\n"""
    out, _ = reconcile_customer_tax_id_context({"customer_tax_id": "133-908-001-111"}, ocr, [])
    assert out["customer_tax_id"] is None


def test_printer_footer_cannot_own_emerald_vendor_name():
    ocr = """Emerald Mansion\nCondominium Association Ine.\nG/F Emerald Mansion Emerald Ave Ortigas Ctr.\nSan Antonio Pasig City 1603\nVAT Reg. TIN: 005-578-990-00000\nSALES INVOICE\nNo. 7512\nBIR Authority to Print No.: OCN:043AU20250000013398\nAdelfa F. Ortega - Prop.\nNONVAT Reg. TIN:465-709-231-00000\nDate of ATP: November 26 2025\nPrinter's Accreditation No. 032MP2021000000039\nADEL PRINTING SERVICES\nStall B1, Cartimar Bldg, C.M. Rect Ave.\n"""
    data = {"vendor_name": "ADEL PRINTING SERVICES, Stall B1, Cartimar Bldg, C.M. Rect Ave."}
    out, notes = reconcile_issuer_printer_ownership(data, ocr)
    assert out["vendor_name"] == "Emerald Mansion Condominium Association Inc."
    assert notes


def _blank_image(tmp_path: Path) -> str:
    p = tmp_path / "header.png"
    cv2.imwrite(str(p), np.full((1000, 1200, 3), 255, dtype=np.uint8))
    return str(p)


def test_vendor_address_crop_can_expand_incomplete_outlet_address(monkeypatch, tmp_path):
    image = _blank_image(tmp_path)
    monkeypatch.setattr(vision_verifier.settings, "VISION_VERIFICATION_ENABLED", True)
    monkeypatch.setattr(vision_verifier.settings, "VENDOR_ADDRESS_VERIFY_ENABLED", True)
    full = (
        "OUTLET 2 SM CITY MANILA CONCEPCION COR ARROCEROS & SAN MARCELINO ST "
        "BARANGAY 659 ERMITA NCR, CITY OF MANILA, FIRST DISTRICT"
    )
    monkeypatch.setattr(
        vision_verifier,
        "_post_generate",
        lambda prompt, b64, trace: json.dumps({"vendor_address": full}),
    )
    current = "BARANGAY 659 ERMITA, NCR CITY OF MANILA, FIRST DISTRICT"
    value, notes = vision_verifier.verify_vendor_address_crop(image, current, [], trace={})
    assert value == full
    assert notes
