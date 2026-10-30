"""Conservative, auditable evidence indicators for extracted invoice fields.

These scores measure *support in the OCR text and validation*, not calibrated
probabilities of correctness. Do not infer an OCR match solely from generic
average OCR confidence, and never silently invent values for missing fields.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any

from utils.helpers import clamp

# Fields actually extracted from a document, not app-maintained fields (IDs,
# upload date, filename, processing status, source paths, etc.).
DOCUMENT_FIELDS = (
    "invoice_number", "invoice_date", "due_date", "vendor_name", "vendor_address",
    "vendor_tax_id", "customer_name", "customer_contact", "customer_address",
    "customer_tax_id", "plate_number", "subtotal", "tax_amount", "tax_rate",
    "discount", "withholding_tax", "zero_rated_sales", "vat_exempt_sales",
    "total_amount", "current_charges_total", "previous_balance", "currency",
    "payment_terms", "category",
)
ITEM_FIELDS = ("description", "quantity", "unit_price", "amount")
NUMERIC_FIELDS = {
    "subtotal", "tax_amount", "tax_rate", "discount", "withholding_tax",
    "zero_rated_sales", "vat_exempt_sales", "total_amount", "current_charges_total",
    "previous_balance", "quantity", "unit_price", "amount",
}
OPTIONAL_FIELDS = set(DOCUMENT_FIELDS) - {"invoice_number", "invoice_date", "vendor_name", "total_amount"}
# Category is assigned by application rules, rather than found verbatim in the invoice.
DERIVED_FIELDS = {"category"}
PLACEHOLDERS = {"", "-", "--", "—", "n/a", "na", "none", "null", "unknown", "unknown vendor", "not extracted", "not specified", "not available"}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def _is_present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and value.strip().lower() in PLACEHOLDERS)


def _numeric_match(value: Any, snippet: str) -> bool:
    """Find an OCR numeric token, without confusing 5 with 500 or 5.00 with 50."""
    try:
        val = float(value)
    except (ValueError, TypeError, OverflowError):
        return False
    if not (abs(val) < 1e15):
        return False
    pattern = r"(?<![\w.])\(?-?\s*\d[\d,]*(?:\.\d+)?\)?(?![\w.])"
    for raw in re.findall(pattern, snippet):
        try:
            found = float(raw.replace(",", "").replace("(", "-").replace(")", "").replace(" ", ""))
        except ValueError:
            continue
        if abs(val - found) <= (0.005 if abs(val) < 1 else 0.011):
            return True
    return False


def _text_match(value: Any, lines: list[str]) -> tuple[float, str]:
    """Return a text-evidence strength and a very short observed OCR fragment."""
    needle = _norm(value)
    if not needle:
        return 0.0, ""
    if len(needle) <= 2:  # '1', '0', 'D' or 'PH': ubiquitous, not unique evidence
        return 0.0, ""
    for line in lines:
        normalized = _norm(line)
        if needle in normalized:
            return 1.0, line[:120]
    all_text = _norm(" ".join(lines))
    if needle in all_text:
        return 0.85, "OCR text across multiple lines"
    words = [w for w in _tokens(value) if len(w) >= 3]
    if len(words) >= 2:
        joined = " ".join(lines).lower()
        matched = sum(1 for word in words if word in joined)
        overlap = matched / len(words)
        if overlap >= .85:
            return .62, "Most words found separately in OCR text"
    return 0.0, ""


def _numeric_evidence(value: Any, lines: list[str]) -> tuple[float, str]:
    for line in lines:
        if _numeric_match(value, line):
            return 0.82, line[:120]
    return 0.0, ""


def _line_item_evidence(item: dict, key: str, lines: list[str]) -> tuple[float, str]:
    desc = item.get("description")
    matches = []
    for i, line in enumerate(lines):
        strength, _ = _text_match(desc, [line])
        if strength >= .85:
            matches.append(i)
    if key == "description":
        return _text_match(desc, lines)
    # Price/quantity from another item must NOT count as supporting this row.
    if not matches:
        return (0.0, "Description could not be anchored to an OCR row")
    for i in matches:
        # First use the SAME description row. Never borrow a price from the
        # next named item, even when OCR lines were adjacent.
        if _numeric_match(item.get(key), lines[i]):
            return .90, lines[i][:120]
        # Some OCR engines split a row into its description followed by a
        # numeric-only line. Allow at most two such lines; stop at labels.
        for line in lines[i+1:min(len(lines), i+3)]:
            letters = set(re.findall(r"[a-zA-Z]+", line))
            if letters - {"PHP", "Php", "php", "P", "QTY", "Qty", "qty", "PCS", "pcs"}:
                break
            if _numeric_match(item.get(key), line):
                return .78, line[:120]
    return 0.0, "No matching amount near the item description"


def _issues_for_field(field: str, issues: list[str]) -> list[str]:
    aliases = {
        "subtotal": ("subtotal", "vatable sales", "net amount"),
        "tax_amount": ("vat", "tax amount", "tax inconsistent"),
        "total_amount": ("total amount", "total due", "total mismatch", "payable"),
        "line_items": ("line item", "items", "quantity", "unit price"),
    }
    phrases = aliases.get(field, (field.replace("_", " "),))
    return [str(issue) for issue in issues if any(
        p in str(issue).lower().replace("_", " ") for p in phrases
    )]


def build_field_evidence(
    extracted: dict,
    ocr_text: str = "",
    ocr_lines: list[dict] | None = None,
    validation_issues: list[str] | None = None,
    ocr_engine: str | None = None,
) -> dict[str, dict]:
    """Build per-field/per-cell evidence map used by persistence and UI.

    No document/word evidence => conservative score + review warning.
    Scores are HEURISTICS; OCR matches establish reading, not semantic ownership.
    """
    issues = validation_issues or []
    text_lines = [s.strip() for s in str(ocr_text or "").splitlines() if s.strip()]
    observed = [ln for ln in (ocr_lines or []) if isinstance(ln, dict) and ln.get("text")]
    if observed:
        # The OCR text can preserve more source material than geometrical lines;
        # retain both without resorting to made-up source references.
        lines = text_lines + [str(line["text"]).strip() for line in observed]
    else:
        lines = text_lines
    has_ocr = bool(lines)
    handwritten = "trocr" in (ocr_engine or "").lower()
    evidence: dict[str, dict] = {}

    def record(path: str, value: Any, field: str, item: dict | None = None):
        if not _is_present(value):
            missing_required = field in {"invoice_number", "invoice_date", "vendor_name", "total_amount"} and item is None
            evidence[path] = {
                "score": None, "status": "missing" if missing_required else "not_provided",
                "source": "unavailable", "evidence": "No extracted value",
                "warning": "Required field missing; check source." if missing_required else "",
            }
            return
        if field in DERIVED_FIELDS and item is None:
            evidence[path] = {"score": None, "status": "derived", "source": "application_rule",
                              "evidence": "Assigned by category rules; not an OCR field", "warning": ""}
            return
        problems = _issues_for_field(field if item is None else "line_items", issues)
        if item is not None:
            strength, excerpt = _line_item_evidence(item, field, lines)
        elif field in NUMERIC_FIELDS:
            strength, excerpt = _numeric_evidence(value, lines)
        elif field == "invoice_date" or field == "due_date":
            strength, excerpt = _text_match(value, lines)
            if not strength:
                # Many documents display DD/MM/YYYY while the saved value is ISO.
                try:
                    when = date.fromisoformat(str(value)[:10])
                    variants = (when.strftime("%d/%m/%Y"), when.strftime("%m/%d/%Y"),
                                when.strftime("%d-%b-%Y"), when.strftime("%d %b %Y"),
                                when.strftime("%d-%b-%y"), when.strftime("%d %B %Y"))
                    for candidate in variants:
                        strength, excerpt = _text_match(candidate, lines)
                        if strength:
                            break
                except (ValueError, TypeError):
                    pass
        else:
            strength, excerpt = _text_match(value, lines)

        if strength >= .8:
            score = .78 + .12 * strength
            source = "ocr_text_near_item" if item is not None else "ocr_text"
        elif strength > 0:
            score = .67 + .12 * strength
            source = "partial_ocr_text"
        else:
            score = .44 if has_ocr else .40
            source = "unverified"
        # OCR engines sometimes provide an individual line confidence. When
        # the evidence is present on a low-quality line, it cannot support a
        # high score even if its text happens to match the final value.
        for line_meta in observed:
            source_line = str(line_meta.get("text") or "")
            match = (_numeric_match(value, source_line) if field in NUMERIC_FIELDS
                     else _text_match(value, [source_line])[0] >= .85)
            if not match:
                continue
            if item is not None and _text_match(item.get("description"), [source_line])[0] < .85:
                continue
            try:
                line_quality = float(line_meta.get("confidence"))
            except (TypeError, ValueError):
                continue
            if 0 <= line_quality <= 1:
                score -= .32 * (1 - line_quality)
                break
        # Reconciliation helps confirm numbers, but not that the right row
        # belongs to the right buyer. A matching value elsewhere is not proof.
        if item is not None and field in {"quantity", "unit_price", "amount"}:
            try:
                q, p, a = (float(item[k]) for k in ("quantity", "unit_price", "amount"))
                consistent = q >= 0 and p >= 0 and abs(q*p-a) <= .05
                score += .05 if consistent else -.27
                if not consistent:
                    problems.append("Line item quantity × unit price does not match total unit price.")
            except (ValueError, KeyError, TypeError, OverflowError):
                problems.append("Line item arithmetic could not be checked.")
                score -= .12
        # Future-dated invoices need an explicit field warning even when the
        # date appears clearly in OCR. A clear reading can still be invalid.
        if field == "invoice_date" and item is None:
            try:
                if date.fromisoformat(str(value)[:10]) > date.today():
                    problems.append("Invoice date is in the future; verify against the source invoice.")
            except (ValueError, TypeError):
                pass
        if problems:
            score -= .28
        if handwritten:
            score -= .08
        score = round(clamp(score), 3)
        if problems:
            warning = "; ".join(dict.fromkeys(problems))[:320]
        elif score < .70:
            warning = "No reliable matching source evidence; verify this value against the invoice."
        elif score < .80:
            warning = "Only partial OCR evidence; verify this value against the invoice."
        else:
            warning = ""
        evidence[path] = {
            "score": score,
            "status": "review" if warning else "supported",
            "source": source,
            "evidence": excerpt if excerpt else "No matching source text located",
            "warning": warning,
        }

    for field in DOCUMENT_FIELDS:
        record(field, extracted.get(field), field)
    for idx, item in enumerate(extracted.get("line_items") or []):
        if not isinstance(item, dict):
            continue
        for key in ITEM_FIELDS:
            record(f"line_items.{idx}.{key}", item.get(key), key, item=item)
    return evidence


def mark_user_changes(before: dict, updates: dict, evidence: dict[str, dict]) -> dict[str, dict]:
    """Avoid showing pre-edit OCR evidence as proof of manually edited values."""
    out = {path: dict(meta) for path, meta in evidence.items()}
    for field in DOCUMENT_FIELDS:
        if field not in updates or before.get(field) == updates[field]:
            continue
        if field == "category":
            out[field] = {"score": None, "status": "derived", "source": "manual_category",
                          "evidence": "Manually assigned category", "warning": ""}
        else:
            out[field] = {"score": None, "status": "manual_edit", "source": "user_edit",
                          "evidence": "Changed by a user after extraction", "warning": "Manual correction: automatic OCR evidence score no longer applies."}
    if "line_items" in updates:
        old_items = before.get("line_items") or []
        new_items = updates.get("line_items") or []
        for idx, item in enumerate(new_items):
            for key in ITEM_FIELDS:
                path = f"line_items.{idx}.{key}"
                if (idx >= len(old_items) or
                    old_items[idx].get("description") != item.get("description") or
                    old_items[idx].get(key) != item.get(key)):
                    out[path] = {"score": None, "status": "manual_edit", "source": "user_edit",
                                 "evidence": "Changed by a user after extraction", "warning": "Manual correction: automatic OCR evidence score no longer applies."}
        for path in tuple(out):
            match = re.fullmatch(r"line_items\.(\d+)\.(\w+)", path)
            if match and int(match.group(1)) >= len(new_items):
                out.pop(path)
    return out
