"""Wraps an LLM extraction result together with confidence metadata."""
from __future__ import annotations
from typing import Optional, Any
from pydantic import BaseModel


class FieldConfidence(BaseModel):
    field: str
    value: Any
    confidence: float  # 0.0 - 1.0
    source: str = "llm"  # llm | ocr | derived


class Prediction(BaseModel):
    raw_json: dict
    field_confidences: list[FieldConfidence] = []
    overall_confidence: float = 0.0
    needs_review: bool = False
    warnings: list[str] = []

    def low_confidence_fields(self, threshold: float = 0.75) -> list[str]:
        return [fc.field for fc in self.field_confidences if fc.confidence < threshold]
