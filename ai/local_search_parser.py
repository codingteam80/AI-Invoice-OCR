"""Conservative, zero-network natural-language parser for common invoice searches.

Only returns a plan when *all* meaningful words in the request have been
understood. Unrecognized terms are intentionally delegated to the LLM, not
silently ignored (which could produce misleading/wider results).
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from services.search_service import empty_search_plan, normalize_search_plan

_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_AMOUNT = r"(?:[₱$]|\b(?:PHP|USD)\s*|\bP\s*)?\s*\d[\d,]*(?:\.\d+)?\s*[kK]?"
_FILLERS = re.compile(r"\b(?:show|find|list|display|give|me|all|the|uploaded|upload|user|status|category|currency|invoices?|bills?|receipts?|records?|entries|transactions?|where|whose|with|that|are|is|were|was|from|for|in|on|of|dated|date|and|please|only|matching|having|amounts?|totals?|due|worth|costing|expenses?|spending|during|period|by|filtered|filter|search|activity|logs?|audit|actions?|performed)\b", re.I)


def _span_clear(s: str, m: re.Match) -> str:
    return s[:m.start()] + " " * (m.end() - m.start()) + s[m.end():]


def _month(n: str) -> int:
    n = n.lower()
    return _MONTHS.get(n, _MONTHS.get(n[:3]))


def _month_range(y: int, m: int) -> tuple[str, str]:
    return date(y, m, 1).isoformat(), date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def _add_date(plan: dict, field: str, start: date | str, end: date | str) -> None:
    plan["dates"].append({"field": field, "operator": "between", "value": str(start), "value2": str(end)})


def _money(raw: str) -> float:
    s = re.sub(r"(?i)\b(?:php|usd)\b|[₱$,\s]", "", raw).strip()
    s = re.sub(r"(?i)^p(?=\d)", "", s)
    multiplier = 1000 if s.lower().endswith("k") else 1
    return float(s.rstrip("kK")) * multiplier


def _entity_match(remaining: str, entries: list[str]) -> tuple[re.Match | None, str | None]:
    for entity in sorted((str(e).strip() for e in entries if str(e).strip()), key=len, reverse=True):
        # Search literal known values only; never guess a new vendor/customer.
        m = re.search(r"(?<!\w)" + re.escape(entity) + r"(?!\w)", remaining, re.I)
        if m:
            return m, entity
    return None, None


def parse_local_search(query: str, *, page: str, context: dict | None = None,
                       today: date | None = None) -> dict | None:
    """Return a normalized plan for a *complete* familiar request, else None.

    None means the caller should try LLM interpretation. The empty plan is only
    returned when the user explicitly requests all records.
    """
    if page.lower() not in {"history", "reports", "log", "logs"}:
        return None
    context = context or {}
    today = today or date.today()  # user's PC local date, not UTC server time
    remaining = " ".join(query.strip().split())
    if not remaining:
        return None
    if re.fullmatch(r"(?i)\s*(?:show|list|display|find)?\s*(?:me\s+)?(?:all|every)\s+(?:the\s+)?(?:invoices?|records?|logs?|activity|entries)\s*", remaining):
        return empty_search_plan()
    plan = empty_search_plan()
    is_log = page.lower() in {"log", "logs"}
    date_field = "date_time" if is_log else ("date_uploaded" if re.search(r"\b(?:upload(?:ed)?|date uploaded|upload date)\b", remaining, re.I) else "invoice_date")

    # Full month ranges; optional year per endpoint. The last explicit year
    # applies to both months unless each supplies a year.
    rng = re.compile(rf"\b(?:from\s+|between\s+)?({_MONTH})\s*(\d{{4}})?\s+(?:to|through|until|and|-)\s+({_MONTH})\s*(\d{{4}})?\b", re.I)
    m = rng.search(remaining)
    if m:
        y1 = int(m.group(2) or m.group(4) or today.year)
        y2 = int(m.group(4) or m.group(2) or today.year)
        start, _ = _month_range(y1, _month(m.group(1)))
        _, end = _month_range(y2, _month(m.group(3)))
        if start > end:
            return None  # ambiguous wraparound, ask model instead
        _add_date(plan, date_field, start, end)
        remaining = _span_clear(remaining, m)

    # Explicit quarters.
    qpat = re.compile(r"\b(?:q([1-4])|(?:first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter)\s*(?:of\s*)?(\d{4})?\b", re.I)
    m = qpat.search(remaining)
    if m:
        match_quarter = m.group(1) or {"first":1,"1st":1,"second":2,"2nd":2,"third":3,"3rd":3,"fourth":4,"4th":4}[m.group(0).split()[0].lower()]
        q=int(match_quarter); yr=int(m.group(2) or today.year)
        a,_=_month_range(yr,(q-1)*3+1); _,b=_month_range(yr,q*3)
        _add_date(plan,date_field,a,b)
        remaining=_span_clear(remaining,m)

    # Relative dates (the 'last N days' case takes priority).
    rel = re.compile(r"\b(?:today|yesterday|this\s+month|last\s+month|this\s+year|last\s+year|this\s+week|last\s+week|last\s+\d+\s+days?)\b", re.I)
    m = rel.search(remaining)
    if m:
        expr=m.group().lower()
        if expr=="today": a=b=today
        elif expr=="yesterday": a=b=today-timedelta(days=1)
        elif expr in {"this month","last month"}:
            y,mon=today.year,today.month-(1 if expr.startswith("last") else 0)
            if mon==0: y,mon=y-1,12
            a,b=_month_range(y,mon)
        elif expr in {"this year","last year"}:
            y=today.year-(1 if expr.startswith("last") else 0)
            a,b=date(y,1,1),date(y,12,31)
        elif expr in {"this week","last week"}:
            a=today-timedelta(days=today.weekday()+(7 if expr.startswith("last") else 0))
            b=a+timedelta(days=6)
        else:
            count=int(re.search(r"\d+",expr).group()); a=today-timedelta(days=count-1);b=today
        _add_date(plan,date_field,a,b)
        remaining=_span_clear(remaining,m)

    # A single named month (not already consumed by a range).
    if not plan["dates"]:
        m=re.search(rf"\b({_MONTH})\s*(\d{{4}})?\b",remaining,re.I)
        if m:
            a,b=_month_range(int(m.group(2) or today.year),_month(m.group(1)))
            _add_date(plan,date_field,a,b);remaining=_span_clear(remaining,m)

    # ISO date/ranges, supported only when explicitly tied to date wording.
    if not plan["dates"]:
        m=re.search(r"\b(?:between\s+|from\s+)(\d{4}-\d{2}-\d{2})\s+(?:and|to|through)\s+(\d{4}-\d{2}-\d{2})\b",remaining,re.I)
        if m:
            try: a=date.fromisoformat(m.group(1));b=date.fromisoformat(m.group(2))
            except ValueError: return None
            if a>b: return None
            _add_date(plan,date_field,a,b);remaining=_span_clear(remaining,m)

    # Presence searches: no invoice number, missing VAT, with plate number.
    if not is_log:
        alias_map={"invoice_number":r"invoice\s*(?:number|#)","vendor_name":r"vendor\s*name",
                   "customer_name":r"customer\s*name","vendor_tax_id":r"vendor\s*(?:tin|tax\s*id)",
                   "customer_tax_id":r"customer\s*(?:tin|tax\s*id)",
                   "plate_number":r"(?:plate\s*(?:number|no\.?|#)|vehicle\s*plate)",
                   "invoice_date":r"invoice\s*date","total_amount":r"total\s*amount\s*due"}
        for field,alias in alias_map.items():
            match=re.search(r"\b(missing|without|no|with|has)\s+(?:a\s+|an\s+)?("+alias+r")\b",remaining,re.I)
            if match:
                plan["presence"].append({"field":field,"operator":"missing" if match.group(1).lower() in {"missing","without","no"} else "present"})
                remaining=_span_clear(remaining,match)

    # Money range: match before single comparisons. Currency is extracted
    # separately for safety; don't consume unrelated date years as amounts.
    amount_re = re.compile(r"\b(?:amounts?\s+)?(?:between|from)\s+([₱$]?(?:PHP|USD|P)?\s*\d[\d,]*(?:\.\d+)?\s*[kK]?)\s+(?:and|to)\s+([₱$]?(?:PHP|USD|P)?\s*\d[\d,]*(?:\.\d+)?\s*[kK]?)\b",re.I)
    if not is_log:
        m=amount_re.search(remaining)
        if m:
            try: a,b=_money(m.group(1)),_money(m.group(2))
            except ValueError: return None
            if a>b: return None
            plan["numbers"].append({"field":"total_amount","operator":"between","value":a,"value2":b})
            raw_currency=m.group(1)+" "+m.group(2)
            remaining=_span_clear(remaining,m)
        else:
            raw_currency=""
        cmp_pat=re.compile(r"(?:(?:total\s*(?:amount\s*)?(?:due\s*)?|amounts?\s*)?(?:above|over|greater\s+than|more\s+than|at\s+least|minimum|below|under|less\s+than|at\s+most|maximum|equal\s+to|exactly|of)\s+)([₱$]?(?:PHP|USD|P)?\s*\d[\d,]*(?:\.\d+)?\s*[kK]?)\b",re.I)
        m=cmp_pat.search(remaining)
        if m:
            before=m.group(0).lower()
            operator=">=" if re.search(r"at\s+least|minimum",before) else "<=" if re.search(r"at\s+most|maximum",before) else ">" if re.search(r"above|over|greater|more",before) else "<" if re.search(r"below|under|less",before) else "="
            try: v=_money(m.group(1))
            except ValueError: return None
            plan["numbers"].append({"field":"total_amount","operator":operator,"value":v})
            raw_currency+=" "+m.group(1)
            remaining=_span_clear(remaining,m)
        if re.search(r"\b(?:PHP|pesos?|philippine\s+pesos?)\b|₱|(?<!\w)P\s*\d",raw_currency+" "+remaining,re.I):
            plan["choices"].append({"field":"currency","operator":"in","values":["PHP"]})
            remaining=re.sub(r"\b(?:PHP|pesos?|philippine\s+pesos?)\b|₱", " ",remaining,flags=re.I)
        if re.search(r"\b(?:USD|US\s+dollars?|dollars?)\b|\$",raw_currency+" "+remaining,re.I):
            if plan["choices"]:return None  # mixed currencies: explicit clarification needed
            plan["choices"].append({"field":"currency","operator":"in","values":["USD"]})
            remaining=re.sub(r"\b(?:USD|US\s+dollars?|dollars?)\b|\$", " ",remaining,flags=re.I)
    # Status, lock state (explicit only).
    if not is_log:
        status_pattern=re.compile(r"\b(?:needs?[_\s-]?review|unpaid|paid|processed|failed|pending)\b",re.I)
        m=status_pattern.search(remaining)
        if m:
            plan["choices"].append({"field":"status","operator":"in","values":[m.group().lower().replace(" ","_").replace("-","_")]})
            remaining=_span_clear(remaining,m)
        m=re.search(r"\b(unlocked|locked)\b",remaining,re.I)
        if m:
            plan["booleans"].append({"field":"locked","value":m.group(1).lower()=="locked"});remaining=_span_clear(remaining,m)
        vendor_matches=[]
        while True:
            m,name=_entity_match(remaining,context.get("vendors",[]))
            if not m: break
            vendor_matches.append(name);remaining=_span_clear(remaining,m)
        if len(vendor_matches)==1:
            plan["text"].append({"field":"vendor_name","operator":"contains","value":vendor_matches[0]})
        elif len(vendor_matches)>1:
            plan["text"].append({"field":"vendor_name","operator":"contains_any","values":vendor_matches})
        m,name=_entity_match(remaining,context.get("categories",[]))
        if m:
            plan["choices"].append({"field":"category","operator":"in","values":[name]})
            remaining=_span_clear(remaining,m)
    else:
        for field,entities in (("user",context.get("users",[])),("action",context.get("actions",[])),("entity_type",context.get("types",[]))):
            m,name=_entity_match(remaining,entities)
            if m:
                plan["text"].append({"field":field,"operator":"equals","value":name})
                remaining=_span_clear(remaining,m)

    # Residual non-filler text means unsupported intent. An LLM or a user
    # clarification is safer than applying an incomplete local interpretation.
    leftover=_FILLERS.sub(" ",remaining)
    leftover=re.sub(r"[\s,.!?;:()\[\]{}\-–—/]+","",leftover)
    if leftover:
        return None
    if not any(plan.values()):
        return None
    normalized,warnings=normalize_search_plan(plan,page)
    if warnings or normalized!=plan:
        # The normalization may coerce ints to floats, which is fine;
        # only warnings indicate dropped unsupported conditions.
        if warnings:return None
    return normalized
