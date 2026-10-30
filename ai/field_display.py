"""Display evidence-based verification indicators beside extracted values.

Never describe support indicators as statistical probabilities of correctness.
"""
from __future__ import annotations

from ai.field_evidence import build_field_evidence
from datetime import date
import re
from utils.data_quality import is_missing


def evidence_for_invoice(detail: dict) -> tuple[dict[str, dict], bool]:
    """Saved evidence, or an explicitly marked historic OCR-text-only estimate."""
    saved = detail.get("field_evidence") or {}
    if saved:
        # Existing records may contain stale, supported-looking evidence even
        # though the stored invoice date is now in the future. Overlay only
        # current time-sensitive/missing-required review indicators; never
        # rewrite the original OCR evidence or silently mutate the database.
        evidence = {key: dict(value) if isinstance(value, dict) else value
                    for key, value in saved.items()}
        for key in ("invoice_number", "vendor_name", "invoice_date", "total_amount"):
            if is_missing(detail.get(key)):
                evidence[key] = {"score": None, "status": "missing", "source": "required_field_check",
                                 "evidence": "No extracted value", "warning": "Required field missing; verify against the invoice."}
        # Saved invoices from older QA builds already contain the raw invalid
        # date as a validation issue, but the field indicator can show only a
        # generic missing-value warning. Surface that source-specific reason.
        if is_missing(detail.get("invoice_date")):
            issues = detail.get("validation_issues") or []
            if isinstance(issues, str):
                issues = [issues]
            for issue in issues:
                note = str(issue)
                raw = re.search(r"(?i)invoice_date has an invalid date format:\s*([^;\s]+)", note)
                if not raw:
                    raw = re.search(r"(?i)printed Bill Date ['\"]?([\d/.-]+)", note)
                if raw:
                    evidence["invoice_date"] = {
                        "score": None, "status": "review", "source": "printed_date_validation",
                        "evidence": f"Printed Bill Date: {raw.group(1)}",
                        "warning": (
                            f"Printed Bill Date {raw.group(1)} is invalid; verify against "
                            "the invoice. Do not substitute Billing Period or Due Date."
                        ),
                    }
                    break
        date_value = detail.get("invoice_date")
        if not is_missing(date_value):
            try:
                if date.fromisoformat(str(date_value)[:10]) > date.today():
                    old = evidence.get("invoice_date") or {}
                    evidence["invoice_date"] = {**old, "status": "review",
                        "warning": "Invoice date is in the future; verify against the source invoice."}
            except (ValueError, TypeError):
                pass
        return evidence, any(meta.get("historic_ocr_only") for meta in evidence.values() if isinstance(meta, dict))
    if detail.get("raw_text"):
        return build_field_evidence(detail, detail.get("raw_text") or "", validation_issues=detail.get("validation_issues") or [], ocr_engine=detail.get("ocr_engine_used")), True
    return {}, True


def indicator(meta: dict | None) -> str:
    if not meta or meta.get("status") in {"not_provided", "derived"}:
        return ""
    status = meta.get("status")
    if status == "missing":
        return "⚠️ Missing"
    if status == "manual_edit":
        return "✏️ Manual"
    score = meta.get("score")
    if score is None:
        return "⚠️ Unverified"
    pct = f"{round(100 * score)}% evidence"
    return f"⚠️ {pct}" if meta.get("warning") else f"✓ {pct}"


def field_review_summary(evidence: dict[str, dict], limit: int = 16) -> list[str]:
    problems = []
    for path, meta in evidence.items():
        warning = meta.get("warning") if isinstance(meta, dict) else None
        if warning:
            problems.append(f"{path.replace('_', ' ')}: {warning}")
    return problems[:limit]


