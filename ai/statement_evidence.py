"""Final, label-bound reconciliation for account/energy statements.

Read printed values, never stored QA answers. Do not derive a missing total or tax.
"""
import re
from parser.currency_parser import to_float
from utils.date_utils import parse_date

_AMOUNT = r"\(?-?\d[\d,]*\.\d{2}\)?"


def _next_amount(lines, pattern):
    values = []
    for i, line in enumerate(lines):
        match = re.fullmatch(pattern, line, re.I)
        if match:
            for candidate in lines[i+1:i+5]:
                if re.fullmatch(r"[:^₱P\s]*" + _AMOUNT, candidate):
                    values.append(to_float(candidate))
                    break
                if candidate and not re.fullmatch(r"[\d\s-]+", candidate):
                    break
    unique = set(v for v in values if v is not None)
    return next(iter(unique)) if len(unique) == 1 else None


def reconcile_statement_evidence(data, text):
    is_energy = bool(re.search(r"\bEB Invoice\s+(?:No\.?|Number)\b", text, re.I))
    is_statement = bool(re.search(r"Statement of Account No\.", text, re.I))
    if not (is_energy or is_statement):
        return data, []
    result = dict(data)
    notes = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    def set_value(field, value):
        if value is not None:
            if result.get(field) != value:
                notes.append(f"Statement evidence: {field} recovered from its printed label.")
            result[field] = value
    id_pattern = (r"EB Invoice\s+(?:No\.?|Number)\s*:?\s*([\w/-]+)" if is_energy
                  else r"Statement of Account No\.\s*([\w/-]+)")
    ids = set(re.findall(id_pattern, text, re.I))
    if len(ids) == 1:
        set_value("invoice_number", ids.pop())
    for i, line in enumerate(lines):
        match = re.match(r"^(?:Bill Date|Statement Date)\s*:?\s*(.*)$", line, re.I)
        if match:
            raw = match[1] or (lines[i+1] if i+1 < len(lines) else "")
            parsed = parse_date(raw.lstrip(": "))
            if parsed:
                set_value("invoice_date", parsed.isoformat())
                break
    set_value("total_amount", _next_amount(lines, r"TOTAL AMOUNT DUE"))
    if is_energy:
        set_value("current_charges_total", _next_amount(lines, r"TOTAL CURRENT BILL AMOUNT"))
        match = re.search(r"(?im)^VAT Sales\s+(" + _AMOUNT + r")\s+(" + _AMOUNT + r")\s*$", text)
        if match:
            set_value("subtotal", to_float(match[1]))
            set_value("tax_amount", to_float(match[2]))
        for field, label in [("vat_exempt_sales", "Non-VAT"), ("zero_rated_sales", "VAT Zero Rated")]:
            match = re.search(r"(?im)^" + label + r"\s+(" + _AMOUNT + r")\s*$", text)
            if match:
                set_value(field, to_float(match[1]))
        # The account-summary grid places several headings before their values.
        # The previous-bill amount is immediately before 'Please pay immediately'.
        for i, line in enumerate(lines):
            if line.casefold() == "please pay immediately" and i:
                candidate = lines[i-1]
                if re.fullmatch(r"[\^₱P\s]*" + _AMOUNT, candidate):
                    set_value("previous_balance", to_float(candidate))
        rows = []
        inside = False
        for i, line in enumerate(lines):
            if line == "BREAKDOWN OF ELECTRICITY CHARGES":
                inside = True
            if inside and i+2 < len(lines) and re.fullmatch(_AMOUNT, lines[i+1]) and re.fullmatch(r"\d+(?:\.\d+)?\s*%", lines[i+2]):
                if re.search(r"[A-Za-z]", line):
                    amount = to_float(lines[i+1])
                    rows.append(dict(description=line, quantity=1, unit_price=amount, amount=amount))
            if inside and "monthly electricity consumption" in line.lower():
                break
        current = result.get("current_charges_total")
        if rows and current is not None and abs(sum(row["amount"] for row in rows)-current) < 0.02:
            result["line_items"] = rows
            result["_normalization_issues"] = [issue for issue in result.get("_normalization_issues", []) if not issue.startswith("line_items row ")]
            result["_line_items_basis"] = "current_charges_total"
            notes.append("Statement evidence: retained the complete printed electricity charge breakdown, including taxes.")
    else:
        set_value("subtotal", _next_amount(lines, r"Monthly Service Fee \(VAT Exclusive\)"))
        set_value("tax_amount", _next_amount(lines, r"Value Added Tax \(VAT\)\s*-\s*\d+%"))
        set_value("current_charges_total", _next_amount(lines, r"Total Monthly Service Fee \(VAT Inclusive\)"))
        set_value("previous_balance", _next_amount(lines, r"Remaining Balance from Previous Bill(?: \(VAT Inclusive\))?"))
        rows = []
        for i, line in enumerate(lines[:-1]):
            if re.search(r"\bplan\b", line, re.I) and re.fullmatch(_AMOUNT, lines[i+1]):
                amount = to_float(lines[i+1])
                rows.append(dict(description=line, quantity=1, unit_price=amount, amount=amount))
        subtotal = result.get("subtotal")
        if rows and subtotal is not None and abs(sum(row["amount"] for row in rows)-subtotal) < 0.02:
            result["line_items"] = rows
            result["_normalization_issues"] = [issue for issue in result.get("_normalization_issues", []) if not issue.startswith("line_items row ")]
            notes.append("Statement evidence: retained current service-plan rows; previous payments are not purchased items.")
    return result, notes
