"""
Combines OCR-level confidence with extraction-validation results to produce
a per-field and overall confidence score for the extracted invoice.
"""
from config.constants import REQUIRED_FIELDS
from config.settings import settings
from models.prediction import Prediction, FieldConfidence
from utils.helpers import clamp


def score_extraction(extracted: dict, ocr_avg_confidence: float, validation_issues: list[str]) -> Prediction:
    field_confidences = []

    penalty_per_issue = 0.12
    base_penalty = clamp(len(validation_issues) * penalty_per_issue, 0.0, 0.6)

    for field in REQUIRED_FIELDS:
        value = extracted.get(field)
        present = value not in (None, "", [])
        field_issue_hit = any(field in issue for issue in validation_issues)

        conf = ocr_avg_confidence if present else 0.0
        if field_issue_hit:
            conf *= 0.5
        field_confidences.append(FieldConfidence(field=field, value=value, confidence=round(clamp(conf), 3)))

    overall = clamp(ocr_avg_confidence - base_penalty)
    needs_review = overall < settings.CONFIDENCE_THRESHOLD or len(validation_issues) > 0

    return Prediction(
        raw_json=extracted,
        field_confidences=field_confidences,
        overall_confidence=round(overall, 3),
        needs_review=needs_review,
        warnings=validation_issues,
    )
