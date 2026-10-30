"""Shared validation primitives; placeholders are never evidence of a value."""
import math
import unicodedata
import re

MISSING_TEXT = {"", "-", "--", "—", "–", "n/a", "na", "null", "none", "unknown", "unknown vendor", "not available"}


def is_missing(value):
    return value is None or value == [] or (
        isinstance(value, str) and value.strip().casefold() in MISSING_TEXT
    )


def finite_number(value):
    if isinstance(value, bool) or is_missing(value):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def identity_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def duplicate_key(vendor, number):
    # Keep punctuation and leading zeros: they can be significant identifiers.
    if is_missing(vendor) or is_missing(number) or str(number).startswith("UNVERIFIED-"):
        return None
    import hashlib
    import json
    return hashlib.sha256(json.dumps([re.sub(r"\s*([.,])\s*", r"\1", identity_text(vendor)), identity_text(number)]).encode()).hexdigest()


def document_content_key(text):
    """Exact normalized OCR content, independent of model-guessed identity.

    Retain decimal points and signs so financial differences cannot disappear.
    No fuzzy image matching: similar templates are not sufficient duplicates.
    """
    import hashlib
    compact = re.sub(r'[^a-z0-9.+-]', '', identity_text(text))
    if len(compact) < 120 or not re.search(r'\d', compact):
        return None
    return hashlib.sha256(compact.encode()).hexdigest()
