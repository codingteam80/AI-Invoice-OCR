"""Conservative, explainable selection of the expensive whole-image VLM pass.

Focused field/area readers retain their existing separate OCR-driven triggers.
This module does not use the VLM to decide whether the VLM is required.
"""
from __future__ import annotations
import math
from ai.confidence import _financial_consistency, _line_item_consistency
from ai.field_evidence import build_field_evidence


def choose_full_image_vision(data: dict, *, ocr_confidence: float | None = None,
                             ocr_engine: str | None = None, ocr_text: str = "",
                             ocr_lines: list[dict] | None = None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    engine = str(ocr_engine or "").lower()
    if "trocr" in engine or "handwrit" in engine:
        reasons.append("handwriting detected")
    try:
        quality = float(ocr_confidence) if ocr_confidence is not None else None
        if quality is not None and math.isfinite(quality):
            # OCR integrations report normalized confidence in [0, 1].
            if quality < 0.75:
                reasons.append("low OCR confidence")
    except (TypeError, ValueError, OverflowError):
        pass
    for field in ("invoice_number", "invoice_date", "vendor_name", "total_amount"):
        value = data.get(field)
        if value is None or str(value).strip().lower() in {"", "-", "unknown", "none", "n/a"}:
            reasons.append(f"missing {field}")
    financial_ok, billing_ok = _financial_consistency(data)
    if financial_ok is False or billing_ok is False:
        reasons.append("financial amounts do not reconcile")
    if _line_item_consistency(data) is False:
        reasons.append("line-item arithmetic mismatch")
    if not data.get("line_items"):
        reasons.append("no line items extracted")
    if ocr_text or ocr_lines:
        evidence = build_field_evidence(data, ocr_text, ocr_lines, [], ocr_engine)
        crucial = ("invoice_number", "vendor_name", "customer_name", "subtotal", "tax_amount", "total_amount")
        for field in crucial:
            record = evidence.get(field) or {}
            if record.get("status") == "review" or (
                record.get("score") is not None and record["score"] <= 0.5
            ):
                reasons.append(f"weak OCR evidence for {field}")
                break
        for key, record in evidence.items():
            if not key.startswith("line_items."):
                continue
            if (record or {}).get("score") is not None and record["score"] <= 0.5:
                reasons.append("weak OCR evidence for line items")
                break
    return bool(reasons), reasons
