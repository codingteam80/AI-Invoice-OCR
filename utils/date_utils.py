"""Date parsing helpers — normalizes many invoice date formats to ISO."""
import re
from datetime import datetime, date

_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y/%m/%d", "%d.%m.%Y",
    # Added after testing against real receipts: compact no-separator dates
    # ("16AUG2026") and month-name dates without a comma ("Nov 5 2025") —
    # both appear on real receipts and weren't matched by any format above.
    "%d%b%Y", "%b %d %Y", "%B %d %Y",
    # Added after testing against real invoices with 2-DIGIT years and a
    # dash-separated day-month(name)-year layout ("13-Aug-26" -> 2026-08-13,
    # confirmed on a real North Star International Travel service invoice;
    # "22-July-2026" confirmed on the same vendor with a 4-digit year).
    # Python's %y treats 00-68 as 2000-2068 and 69-99 as 1969-1999, which is
    # the right side of the century split for any invoice a currently-running
    # system will realistically see.
    "%d-%b-%y", "%d-%B-%y", "%d/%m/%y", "%d %b %y", "%d %B %y",
    "%d-%b-%Y", "%d-%B-%Y",
    # NOTE: "%m/%d/%y" (month-first, 2-digit year) is deliberately NOT
    # included here. Both "%d/%m/%y" and "%m/%d/%y" would successfully
    # parse the exact same string (e.g. "03/04/26"), just swapping day and
    # month — whichever format happened to sit earlier in this list would
    # silently win, with no way to tell from the code which reading was
    # actually correct for a given vendor. All confirmed real invoice
    # examples (North Star International Travel) use a day-first layout,
    # matching the PH convention already used for the 4-digit-year formats
    # above, so only the day-first 2-digit form is kept here.
]


def parse_date(value: str) -> date | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()

    # 1.50: safely support short month/day/two-digit-year dates such as
    # ``8/24/26`` without changing the long-standing day-first behavior for
    # ambiguous values such as ``03/04/26``. If one side is > 12, the order
    # is unambiguous; otherwise the existing format list below remains the
    # tie-breaker (day/month/year).
    short_slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})", value)
    if short_slash:
        first, second, yy = map(int, short_slash.groups())
        try:
            if first <= 12 < second:
                return datetime.strptime(f"{first:02d}/{second:02d}/{yy:02d}", "%m/%d/%y").date()
            if second <= 12 < first:
                return datetime.strptime(f"{first:02d}/{second:02d}/{yy:02d}", "%d/%m/%y").date()
        except ValueError:
            return None

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


def parse_date_with_context(value: str, context: str | None = None) -> date | None:
    """Parse a date while honoring an explicit printed format hint.

    Full-page invoice OCR sometimes contains an explicit hint such as
    ``Billing Period (mm/dd/yy)``. For ambiguous strings like ``08/05/26``
    that hint is stronger evidence than the generic day-first fallback.
    """
    if not value or not isinstance(value, str):
        return None
    raw=value.strip()
    ctx=str(context or "")
    m=re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", raw)
    if m:
        first,second,year=m.groups()
        month_first=bool(re.search(r"(?i)\bmm\s*/\s*dd\s*/\s*(?:yy|yyyy)\b|month\s*/\s*day\s*/\s*year",ctx))
        day_first=bool(re.search(r"(?i)\bdd\s*/\s*mm\s*/\s*(?:yy|yyyy)\b|day\s*/\s*month\s*/\s*year",ctx))
        if month_first and not day_first:
            fmt="%m/%d/%y" if len(year)==2 else "%m/%d/%Y"
            try:return datetime.strptime(raw,fmt).date()
            except ValueError:return None
        if day_first and not month_first:
            fmt="%d/%m/%y" if len(year)==2 else "%d/%m/%Y"
            try:return datetime.strptime(raw,fmt).date()
            except ValueError:return None
    return parse_date(raw)


def to_iso_with_context(value: str, context: str | None = None) -> str | None:
    d=parse_date_with_context(value,context)
    return d.isoformat() if d else None
