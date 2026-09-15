"""Miscellaneous shared helpers."""
import json
import re


def safe_json_loads(text: str) -> dict | None:
    """Extract and parse the first JSON object found in a block of text."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown code fences if the LLM wrapped its output.
    text = re.sub(r"^```(json)?", "", text.strip(), flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find the outermost {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# Philippine TIN: groups of 3 digits separated by dashes, either 3 groups
# (9 digits, e.g. "123-456-789") or 4 groups (12 digits, the 4th group is
# the branch code, e.g. "007-848-122-000"). A bare trailing 5-digit branch
# code ("...00000") shows up on some OCR reads (see sample invoices) — the
# regex allows 3-5 digits in the last group to tolerate that without
# rejecting an otherwise-valid TIN outright.
TIN_RE = re.compile(r"^\d{3}-\d{3}-\d{3}(-\d{3,5})?$")


def normalize_tin(value: str | None) -> str | None:
    """
    Normalize a raw TIN string into the standard PH BIR dash-grouped format
    ("000-423-215-000"). Handles the two most common OCR failure modes:

      1. Stray characters glued onto the digits (e.g. a trailing "V"/"-VA"
         VATable-sale suffix, or leading/trailing whitespace) — stripped.
      2. Digits with NO dashes at all (e.g. "00042321500", common when OCR
         drops thin separator glyphs) — re-grouped into 3-digit chunks.

    Returns the normalized string if it now matches the expected TIN shape
    (TIN_RE), otherwise returns the original value UNCHANGED (never
    fabricates a value or silently drops a TIN that just looks unusual —
    e.g. a genuinely non-Philippine tax ID should pass through untouched
    rather than get mangled by PH-specific regrouping).
    """
    if not value or not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value

    # Already valid — nothing to do.
    if TIN_RE.match(raw):
        return raw

    digits_only = re.sub(r"\D", "", raw)
    if len(digits_only) not in (9, 12):
        return value  # doesn't look like a PH TIN at all — leave untouched

    groups = [digits_only[i:i + 3] for i in range(0, len(digits_only), 3)]
    candidate = "-".join(groups)
    return candidate if TIN_RE.match(candidate) else value


def is_valid_tin(value: str | None) -> bool:
    """Whether `value` matches the standard PH BIR TIN shape."""
    return bool(value) and bool(TIN_RE.match(str(value).strip()))
