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
