"""Evidence-grounded reconstruction of simple printed invoice tables.

Use only when OCR has an explicit Description / Qty / Unit Price / Amount
header. Such scanned tables are often returned by OCR as a *vertical* sequence
of cells, which broad XY proximity heuristics confuse with the next row.
No supplier names, invoice numbers, or example-specific numeric constants.
"""
from __future__ import annotations

import re


def _money(s: str) -> float | None:
    raw = str(s).strip().replace('₱', '').replace('$', '').replace('PHP', '').strip()
    # A thousands comma is occasionally recognized as a period, e.g. 3.495.00.
    raw = re.sub(r'(?<=\d)\.(?=\d{3}\.\d{2}$)', ',', raw)
    if not re.fullmatch(r'-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}', raw):
        return None
    try:
        return float(raw.replace(',', ''))
    except ValueError:
        return None


def _is_header(s: str) -> bool:
    return bool(re.match(r'(?i)^\s*(?:item\s*(?:no\.?|#)?|description|qty|quantity|unit\s*price|amount)\s*$', s))


def _table_rows(text: str) -> list[dict]:
    lines = [re.sub(r'\s+', ' ', ln).strip() for ln in (text or '').splitlines()]
    lines = [ln for ln in lines if ln]
    # Prevent accidentally parsing summary / service-account lines as items.
    for h in range(len(lines) - 3):
        headers = [re.sub(r'\W+', '', x).lower() for x in lines[h:h+6]]
        if ('description' in headers and ('qty' in headers or 'quantity' in headers)
                and 'unitprice' in headers and 'amount' in headers):
            start = h + next(i for i,x in enumerate(headers) if x == 'amount') + 1
            break
    else:
        return []
    end = next((i for i in range(start, len(lines)) if re.match(r'(?i)^\s*(?:subtotal|sub-total|vat\s*\(|total\s+(?:amount|sales|due))\b', lines[i])), len(lines))
    rows=[]
    for i in range(start+1, end-2):
        if not re.fullmatch(r'\d{1,3}(?:\.0+)?', lines[i]):
            continue
        qty = float(lines[i]);unit = _money(lines[i+1]);amount = _money(lines[i+2])
        if unit is None or amount is None or unit < 0 or amount < 0:
            continue
        desc = lines[i-1]
        if not re.search('[A-Za-z]',desc) or _is_header(desc) or re.match(r'(?i)^item\s*no',desc):
            continue
        # The description sits directly before the quantity; optional item
        # code (e.g. PG-001) is intentionally not included as its name.
        desc = re.sub(r'(?i)(?<!\w)Skg\b', '5kg', desc)
        expected = round(qty * unit,2)
        rows.append({'description':desc, 'quantity':qty,'unit_price':unit,
                     'amount': amount,'calculated':expected})
    # No confident structured rows -> leave model extraction alone.
    return rows


def reconcile_explicit_item_table(data: dict, ocr_text: str) -> tuple[dict,list[str]]:
    rows = _table_rows(ocr_text)
    if not rows:
        return data, []
    out = dict(data)
    notes=[]
    # Whole-table cross-check can recover an OCR-damaged leading digit in a
    # total cell: e.g. '$30.00' instead of '530.00', where Qty*Unit=530.00
    # AND the sum of the correctly computed row values matches the printed
    # subtotal. Never force arbitrary values to make a table balance.
    target = out.get('subtotal')
    try:
        target = float(target) if target is not None else None
    except (ValueError,TypeError):
        target = None
    sum_expected = round(sum(x['calculated'] for x in rows),2)
    sums_correspond = target is not None and abs(sum_expected-target) <= 0.05
    normalized=[]
    for row in rows:
        amount = row['amount']; expected=row['calculated']
        if abs(amount-expected) > 0.05:
            if sums_correspond or abs(amount-expected) <= 1.01:
                notes.append(f"Structured table: {row['description']} amount OCR read {amount:.2f}, but Qty × Unit Price gives {expected:.2f}; corrected with table arithmetic corroboration.")
                amount=expected
            else:
                notes.append(f"Structured table: {row['description']} printed amount {amount:.2f} disagrees with Qty × Unit Price {expected:.2f}; verify source image.")
        normalized.append({'description':row['description'], 'quantity':row['quantity'],
                           'unit_price':row['unit_price'],'amount':amount})
    # Do not replace incomplete OCR rows with a subset of a longer model list.
    # When the OCR identifies all rows, it owns their columns and recovers any
    # omitted model items (this is especially helpful on retail invoices).
    existing = out.get('line_items') or []
    if len(normalized) < len(existing):
        return data, notes
    if normalized != existing:
        out['line_items']=normalized
        notes.append('Reconstructed row-by-row Qty, Unit Price and Total Unit Price from the explicit printed table; prevented amounts being borrowed from adjacent rows.')
    return out,notes


def reconcile_explicit_zero_rows(data: dict, ocr_text: str) -> tuple[dict,list[str]]:
    """A literally printed 0.0 total must not become the adjacent Qty value."""
    lines = [s.strip() for s in (ocr_text or '').splitlines() if s.strip()]
    items = [dict(x) for x in data.get('line_items') or []]
    changed = []
    for item in items:
        description = str(item.get('description') or '').strip().casefold()
        if not description:
            continue
        for i, line in enumerate(lines[:-3]):
            if line.casefold() != description:
                continue
            qty = lines[i+1]
            rate = lines[i+2]
            total = lines[i+3]
            if (re.fullmatch(r'\d{1,3}',qty) and
                re.fullmatch(r'(?i)(?:\$|₱)?0+(?:\.0{1,2})?',rate) and
                re.fullmatch(r'(?i)(?:\$|₱)?0+(?:\.0{1,2})?',total)):
                if item.get('unit_price') != 0.0 or item.get('amount') != 0.0 or item.get('quantity') != float(qty):
                    item.update(quantity=float(qty),unit_price=0.0, amount=0.0)
                    changed.append(f"Explicit zero-total row {item['description']!r} preserved (Qty {qty}, Rate 0, Total 0).")
            break
    if not changed:
        return data,[]
    out = dict(data);out['line_items']=items
    return out,changed
