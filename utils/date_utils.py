"""Date parsing helpers — normalizes many invoice date formats to ISO."""
import re
from datetime import datetime, date

_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y/%m/%d", "%d.%m.%Y",
]


def parse_date(value: str) -> date | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # last resort: pull YYYY-MM-DD substring if present
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        try:
            return datetime.strptime(match.group(), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def to_iso(value: str) -> str | None:
    d = parse_date(value)
    return d.isoformat() if d else None
