"""Conservative recovery from labeled source cells, independent of model success.

This layer never treats a payment address, an advertisement, or a previous
balance as the issuer, currency, or current sale. Ambiguous values stay flagged.
"""
import re
from utils.date_utils import parse_date


def reconcile_document_evidence(data, text, lines):
    from ai.source_evidence import geometry, number, _row_value
    out = dict(data)
    notes = []
    cells = geometry(lines)
    raw = [s.strip() for s in text.splitlines() if s.strip()]

    def put(key, value):
        if out.get(key) != value:
            out[key] = value
            notes.append('Document label evidence recovered ' + key + '.')

    def money(s):
        s = re.sub(r'(?i)\s*CR$', '', s).strip()
        v = number(s)
        return v

    def amount(pattern):
        values = []
        for c in cells:
            if re.fullmatch(pattern, c['text'], re.I):
                competing = [x['x'] for x in cells if x['x'] > c['r'] and abs(x['y']-c['y']) < max(x['h'],c['h'])*.6 and re.search(r'(?i)total sales|amount.*net|less:', x['text'])]
                v = _row_value(c, cells, x_max=min(competing) if competing else None)
                if v is None:
                    below = [x for x in cells if 0 < x['y']-c['y'] < c['h']*3.5
                             and (abs(x['x']-c['x']) < c['h']*2 or abs(x['r']-c['r']) < c['h']*2) and money(x['text']) is not None
                             and re.search(r'\.\d{1,2}', x['text'])]
                    if len(below) == 1:
                        v = money(below[0]['text'])
                if v is not None:
                    values.append(v)
        # Immediate next text row is safe only for an isolated amount, not a
        # search that crosses other headings (which caused the old VAT swaps).
        if not values:
            for i, s in enumerate(raw[:-1]):
                if re.fullmatch(pattern, s, re.I) and re.search(r'\.\d{1,2}', raw[i+1]):
                    v = money(raw[i+1])
                    if v is not None:
                        values.append(v)
        unique = set(values)
        return unique.pop() if len(unique) == 1 else None

    def recover(key, pattern):
        v = amount(pattern)
        if v is not None:
            put(key, v)
        return v

    # Explicit amount-due labels are distinct from amount paid / net sales.
    recover('total_amount', r'(?:total\s*am\s*ount(?:\s*due)?(?:\s*:?\s*\(?PHP\)?)?|total\s*amount\s*due\s*in\s*USD|total\s*due\s*\(overdue\s*\+\s*current\))\s*:?')

    if out.get('total_amount') is None and re.search(r'(?i)electric bill', text):
        recover('total_amount', r'Please Pay')

    # Identifier labels with no value on the same OCR line.
    for i, s in enumerate(raw[:-1]):
        if re.fullmatch(r'(?i)(?:series\s*number|document\s*no\.?|billing\s*invoice\s*no\.?)\s*:?', s):
            value = raw[i+1]
            if re.fullmatch(r'[A-Z0-9][A-Z0-9/]{3,}', value, re.I) and re.search(r'\d', value):
                put('invoice_number', value)
                break
    # Compact statement labels and number suffixes survived OCR even when the
    # ordinary phrase matcher failed. Do not select routing/account numbers.
    match = re.search(r'(?i)(?:statement\s*of\s*acc[0o]unt\s*(?:no\.?|number)|billing\s*st[a-z]*ment\s*no\.?)\s*:?\s*([\d][\d -]{5,}\d)', text)
    if match:
        put('invoice_number', re.sub(r'\s+', '', match[1]))
    match = re.search(r'(?im)^SOA\s*#\s*(\d{5,})\s*$', text)
    if match:
        put('invoice_number', match[1])

    # Dates must come from an invoice/statement date label, never a billing
    # period, due date, printer accreditation date or payment posting date.
    from utils.date_utils import parse_date_with_context
    for label_pattern in (r'(?:Invoice|Statement|Bill|Issue)\s*Date', r'Date'):
        if label_pattern == r'Date' and not re.search(r'(?i)invoice|receipt', text):
            continue
        dates = set()
        for c in cells:
            m = re.fullmatch(label_pattern+r'\s*:?\s*(.*)', c['text'], re.I)
            if not m:
                continue
            candidates = [m[1]] if m[1] else []
            if not candidates:
                candidates = [x['text'].lstrip(': ') for x in cells
                              if (x['x'] > c['r'] and abs(x['y']-c['y']) < max(x['h'],c['h'])*.6)
                              or (0 < x['y']-c['y'] < c['h']*3 and abs(x['x']-c['x']) < c['h']*2)]
            for candidate in candidates:
                candidate = re.sub(r'(?i)SEPT(?=\d|\s)', 'Sep ', candidate)
                candidate = re.sub(r'(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])', ' ', candidate)
                candidate = re.sub(r',\s*', ', ', candidate)
                # Keep contiguous day/year OCR out of generic date parsing.
                d = parse_date_with_context(candidate, text)
                if d:
                    dates.add(d.isoformat())
        if len(dates) == 1:
            put('invoice_date', dates.pop())
            break

    # PLDT service fee and tax are current charges, never the old balance.
    pldt = bool(re.search(r'(?im)^\s*(?:A?PLDT(?:\s+INC\.?)?|PHILIPPINE LONG DISTANCE TELEPHONE COMPANY)\s*$', text))
    if pldt:
        put('vendor_name', 'PLDT INC.')
        put('currency', 'PHP')
        vat_net = amount(r'VATABLE\s*SALE[S]?\s*:?')
        if vat_net is not None:
            put('subtotal', vat_net)
        else:
            fee = amount(r'Month[lty]+\s*Service Fee(?: and Other Basic Charges|\s*\(VAT Exclusive\))?')
            usage = amount(r'Usage Charges')
            if fee is not None:
                put('subtotal', round(fee + (usage or 0), 2))
        recover('tax_amount', r'(?:Value Added Ta(?:x|ll)(?:\s*\(VAT\)\s*-\s*\d+%)?|VAT\s*:)')
        current = recover('current_charges_total', r'Total Current Charges(?:\s*-.*)?|Total Monthly Service Fee\s*\(VAT Inclusive\)')
        prev = amount(r'Remaining Balance from Previous (?:Bill|Invoice)(?:\s*\(VAT Inclusive\))?')
        credit_labels = [c for c in cells if re.fullmatch(r'Remaining Balance from Previous (?:Bill|Invoice)(?:\s*\(VAT Inclusive\))?', c['text'], re.I)]
        for c in credit_labels:
            dx = c['b'][1][0]-c['b'][0][0]
            slope = (c['b'][1][1]-c['b'][0][1])/dx if dx else 0
            hits = [(abs(x['y']-c['y']-slope*(x['x']-c['x'])), x) for x in cells if x['x'] > c['r'] and re.fullmatch(r'\(?[\d,.]+\)?\s*(?:CR)?', x['text'], re.I) and money(x['text']) is not None]
            if hits:
                delta, x = min(hits, key=lambda z:z[0])
                if delta < max(c['h'],x['h'])*.8:
                    prev = money(x['text'])
                    if re.search(r'(?i)CR$', x['text']) or x['text'].startswith('('):prev = -abs(prev)
        # OCR often drops the parentheses on a credit. Only restore its sign
        # when the printed previous bill and payment independently corroborate.
        balance = amount(r'Balance from Previous Bill?')
        paid = amount(r'Less:\s*Payments Received\s*-\s*Thank You!?')
        if prev is not None and balance is not None and paid is not None:
            expected = round(balance-abs(paid), 2)
            if abs(abs(expected)-abs(prev)) < .02:
                prev = expected
        if prev is not None:
            put('previous_balance', prev)
        if current is None and prev is not None and out.get('subtotal') is not None and out.get('tax_amount') is not None:
            candidate = round(out['subtotal'] + out['tax_amount'], 2)
            total = out.get('total_amount')
            if total is not None and abs(candidate+prev-total) < .02 and (f'{candidate:,.2f}' in text or f'{candidate:.2f}' in text):
                put('current_charges_total', candidate)
        fee = amount(r'Month[lty]+\s*Service Fee(?: and Other Basic Charges|\s*\(VAT Exclusive\))?')
        usage = amount(r'Usage Charges')
        if fee is not None and out.get('subtotal') is not None and abs(fee+(usage or 0)-out['subtotal']) < .02:
            rows = [dict(description='Monthly Service Fee and Other Basic Charges', quantity=1, unit_price=fee, amount=fee)]
            if usage is not None:
                rows.append(dict(description='Usage Charges', quantity=1, unit_price=usage, amount=usage))
            # Preserve more detailed plan rows when their arithmetic is sound.
            items = out.get('line_items') or []
            valid = bool(items) and all(x.get('quantity') is not None and x.get('unit_price') is not None and x.get('amount') is not None and abs(x['quantity']*x['unit_price']-x['amount']) < .02 for x in items)
            if not valid or abs(sum(x.get('amount') or 0 for x in items)-out['subtotal']) >= .02:
                put('line_items', rows)
                out['_normalization_issues'] = [s for s in out.get('_normalization_issues', []) if not s.startswith('line_items row ')]

    if re.search(r'(?im)^\s*Globe\s*$', text) and re.search(r'(?i)Current Bill Charges', text):
        m = re.search(r'(?i)SOA\s*Number\s*:\s*(\d+)', text)
        if m:
            put('invoice_number', m[1])
        recover('tax_amount', r'VAT')
        current = recover('current_charges_total', r'Total Current Bill')
        recurring = amount(r'Monthly Recurring Fee\s*\(?MRF\)?')
        one_time = amount(r'One-time Charges')
        tax = out.get('tax_amount')
        if recurring is not None and current is not None and tax is not None:
            net = round(recurring+(one_time or 0), 2)
            adjustment = round(net+tax-current, 2)
            # Reconcile a deduction only if that exact number is printed.
            # Corrupt amount strings do not authorize inventing an adjustment.
            if abs(adjustment) < .02:
                put('subtotal', net)
            elif f'{abs(adjustment):.2f}' in text:
                put('subtotal', net)
                put('discount', adjustment)
            if out.get('subtotal') == net:
                rows = [dict(description='Monthly Recurring Fee', quantity=1, unit_price=recurring, amount=recurring)]
                if one_time is not None:
                    rows.append(dict(description='One-time Charges', quantity=1, unit_price=one_time, amount=one_time))
                put('line_items', rows)
                out['_normalization_issues'] = [s for s in out.get('_normalization_issues', []) if not s.startswith('line_items row ')]
            previous = amount(r'Previous Balance')
            if previous is not None and out.get('total_amount') is not None and abs(previous+current-out['total_amount']) < .02:
                put('previous_balance', previous)

    # A cellular statement's foreign call-rate advertisement is outside the
    # financial summary. Use the country fallback within that summary only.
    if re.search(r'(?i)MONTHLY STATEMENT OF ACCOUNT', text) and re.search(r'(?i)PHILIPPINES', '\n'.join(raw[:8])):
        summary = re.split(r'(?i)ACCOUNT SUMMARY', text)[-1].split('Total Amount Due')[0]
        if not re.search(r'\b(?:USD|EUR|GBP|JPY|AUD|CAD|SGD)\b', summary):
            put('currency', 'PHP')
        recover('subtotal', r'Sub[lto]*tal')
        recover('current_charges_total', r'Total Curr[e]?nt Charges')
        previous = amount(r'Previous Balance')
        current, total = out.get('current_charges_total'), out.get('total_amount')
        if previous is not None and current is not None and total is not None and abs(previous + current - total) < .02:
            put('previous_balance', previous)

    # Multi-rate invoice summaries have separate VAT rows. Sum only these
    # labeled summary amounts, and require the printed subtotal/total identity.
    if len(set(re.findall(r'(?i)VAT\s*\(\s*(\d+(?:\.\d+)?)%\s*\)', text))) > 1:
        all_cells = cells
        headings = [c for c in cells if re.fullmatch(r'Subtotal', c['text'], re.I)]
        if headings:
            summary_y = max(c['y'] for c in headings)
            cells = [c for c in cells if c['y'] >= summary_y - c['h']*.5]
        net = amount(r'Subtotal')
        gross = amount(r'Total')
        rates = set(re.findall(r'(?i)VAT\s*\(\s*(\d+(?:\.\d+)?)%\s*\)', text))
        taxes = [amount(r'VAT\s*\(\s*'+re.escape(rate)+r'%\s*\)') for rate in rates]
        if headings and net is not None and gross is not None and taxes and all(t is not None for t in taxes) and abs(net + sum(taxes) - gross) < .02:
            put('subtotal', net)
            put('tax_amount', round(sum(taxes), 2))
            put('total_amount', gross)
            from ai.source_evidence import _table_items
            table_cells = [dict(c) for c in all_cells if c['y'] < summary_y]
            aliases = {'item':'Description', 'hrs/qty':'Qty', 'subtotal':'Amount'}
            for c in table_cells:
                c['text'] = aliases.get(c['text'].lower(), c['text'])
            qty_header = next((c for c in table_cells if c['text']=='Qty'), None)
            if qty_header:
                prior = None
                for c in sorted(list(table_cells), key=lambda c:c['y']):
                    if c['y'] <= qty_header['y'] or c['x'] >= qty_header['x']:
                        continue
                    numeric_row = any(number(x['text']) is not None and x['x'] >= qty_header['x'] and abs(x['y']-c['y']) < max(x['h'],c['h']) for x in table_cells)
                    if not numeric_row and prior and c['y']-prior['y'] < max(c['h'],prior['h'])*2:
                        prior['text'] += ' ' + c['text']
                        table_cells.remove(c)
                    else:
                        prior = c
            rows = _table_items(table_cells)
            if rows and abs(sum(r['amount'] for r in rows)-net) < .02 and all(abs(r['quantity']*r['unit_price']-r['amount'])<.02 for r in rows):
                put('line_items', rows)
                out['_normalization_issues'] = [s for s in out.get('_normalization_issues', []) if not s.startswith('line_items row ')]
        cells = all_cells

    # Service forms with a separate withholding deduction are not account
    # statements: their gross amount must not masquerade as current balance.
    if re.search(r'(?i)SERVICE INVOICE', text) and re.search(r'(?i)Withholding Tax', text) and not re.search(r'(?i)Previous Balance|ROE', text):
        recover('subtotal', r'VATable Sales')
        recover('withholding_tax', r'Less:\s*Withholding Tax')
        put('current_charges_total', None)
        headings = [c for c in cells if re.fullmatch(r'SERVICE INVOICE', c['text'], re.I)]
        for h in headings:
            ids = [re.fullmatch(r'No\.\s*(\d+)', c['text'], re.I) for c in cells if abs(c['y']-h['y']) < h['h']*3]
            ids = [m[1] for m in ids if m]
            if len(ids) == 1:
                put('invoice_number', ids[0])
        header = re.split(r'(?i)SERVICE INVOICE', text)[0]
        companies = re.findall(r'(?im)^([A-Z][^\n]*\b(?:INC\.?|CORPORATION))\s*$', header)
        if len(companies) == 1 and re.match(r'[#\d]', str(out.get('vendor_name') or '')):
            put('vendor_name', companies[0])
        m = re.search(r'(?im)^CUSTOMER:\s*\n([^\n]+)', text)
        if m:
            put('customer_name', m[1].strip())

    if re.search(r'(?i)Seller Name:', text) and re.search(r'(?i)Invoice \(Sale Of Goods\)', text):
        m = re.search(r'(?im)^Seller Name:\s*(.+)$', text)
        if m:
            put('vendor_name', m[1])
        seller_address = re.search(r'(?im)^Seller Address:\s*([^\n]+)', text)
        if seller_address and not re.search(r'(?i)^Seller Name:', seller_address[1]):
            put('vendor_address', seller_address[1].strip())
        recover('subtotal', r'Subtotal')
        recover('tax_amount', r'VAT amount\s*12%')
        recover('total_amount', r'Total')
        put('currency', 'PHP') if re.search(r'(?i)Philippines', text) else None
        for key, label in [('zero_rated_sales',r'Subtotal.*Zero.rated.*'),('vat_exempt_sales',r'Subtotal.*Exempt.*')]:
            if any(re.fullmatch(label,c['text'],re.I) for c in cells):
                put(key, amount(label))

    if re.search(r'(?im)^TOTAL CURRENT CHARGES BEFORE TAX$', text):
        recover('subtotal', r'TOTAL CURRENT CHARGES BEFORE TAX')
        recover('tax_amount', r'\+VAT\s*\(Please see page \d+\)')
        recover('current_charges_total', r'TOTAL CURRENT CHARGES')
    if re.search(r'(?im)^TOTAL\s*inc$', text):
        recover('total_amount', r'TOTAL\s*inc')
        if re.search(r'(?im)^PH\.$', text) and re.search(r'P\s*\d[\d,]*\.\d{2}', text):
            put('currency', 'PHP')

    # A Meralco bill without its tax-breakdown page does not establish VATable
    # sales or VAT. Government Taxes includes other taxes, and is not VAT.
    energy = bool(re.search(r'(?i)\bMERALCO\b', text) and re.search(r'(?i)electric bill|EB Invoice|Billing\s*St[a-z]*ment|Billing Invoice', text))
    if energy:
        put('vendor_name', 'MERALCO')
        put('currency', 'PHP')
        recover('current_charges_total', r'Charges?\w* for this billing period|Total current amount|TOTAL CURRENT BILL AMOUNT')
        recover('previous_balance', r'Remaining Balance from previous bill')
        # The header separates the buyer (left) and business centre (right).
        business = next((c for c in cells if re.search(r'(?i)BUSINESS CENTER', c['text'])), None)
        if business and raw and not re.search(r'\d|(?i:for inquiries|meralco)', raw[0]):
            name = next((c for c in cells if c['text'] == raw[0] and c['x'] < business['x']), None)
            if name:
                put('customer_name', name['text'])
                end = min([c['y'] for c in cells if c['y'] > name['y'] and re.search(r'(?i)Billing\s*St[a-z]*ment', c['text'])] or [name['y']+name['h']*8])
                address = sorted([c for c in cells if name['y']+name['h']*.5 < c['y'] < end-name['h']*.5 and c['r'] < business['x']], key=lambda c:c['y'])
                if address:
                    put('customer_address', ' '.join(c['text'] for c in address))
        for label in cells:
            if re.fullmatch(r'Contract in the name of', label['text'], re.I):
                names = [c for c in cells if c['x'] > label['r'] and abs(c['y']-label['y']) < label['h']*.6 and re.search(r'[A-Za-z]', c['text'])]
                if names:
                    put('customer_name', min(names,key=lambda c:c['x'])['text'])
            if not out.get('customer_address') and re.fullmatch(r'Service Address', label['text'], re.I):
                addresses = [c for c in cells if label['r'] < c['x'] < label['r']+label['h']*8 and abs(c['y']-label['y']) < label['h']*.6]
                if len(addresses) == 1:
                    a = addresses[0]
                    next_lines = [c for c in cells if 0 < c['y']-a['y'] < a['h']*1.5 and abs(c['x']-a['x']) < a['h']]
                    put('customer_address', ' '.join([a['text']]+[c['text'] for c in next_lines]))
        if not re.search(r'(?i)VAT\s*Sales|VATABLE\s*SALES', text):
            for key in ('subtotal', 'tax_amount'):
                put(key, None)
            warning = 'VAT breakdown not present on supplied electricity-bill page; VATable sales and VAT require review.'
            out['_normalization_issues'] = list(dict.fromkeys(out.get('_normalization_issues', []) + [warning]))

    # Plain retail tax tables. Recover total before applying the two independent
    # constraints needed to repair a single OCR digit in the tax cell.
    retail = bool(re.search(r'(?i)Unit\s*Price', text) and re.search(r'(?im)^\s*Subtotal\s*$', text))
    if retail:
        recover('subtotal', r'Subtotal')
        tax = recover('tax_amount', r'VAT\s*\(12%?\)?')
        put('current_charges_total', None)
        sub, total = out.get('subtotal'), out.get('total_amount')
        if sub is not None and total is not None and re.search(r'VAT\s*\(12%\)?', text, re.I):
            expected = round(sub*.12, 2)
            if abs(sub+expected-total) < .02:
                put('tax_amount', expected)
        # An OCR '$' can be a printed 5 (e.g. 530.00). Repair only when the
        # printed quantity/rate AND independently printed subtotal agree.
        items = out.get('line_items') or []
        if items and sub is not None and not re.search(r'\bUSD\b', text):
            repaired = [dict(item) for item in items]
            changed = False
            for item in repaired:
                q, p, a = item.get('quantity'), item.get('unit_price'), item.get('amount')
                if q is None or p is None or a is None or abs(q*p-a) < .02:
                    continue
                evidence = [c['text'] for c in cells if c['text'].startswith('$') and number(c['text']) == a]
                if len(evidence) == 1 and number('5'+evidence[0][1:]) == round(q*p, 2):
                    item['amount'] = round(q*p, 2)
                    changed = True
            if changed and abs(sum(item.get('amount') or 0 for item in repaired)-sub) < .02:
                put('line_items', repaired)

    # E-wallet receipt totals include VAT; "Total Sales Amount" excludes VAT.
    if re.search(r'(?i)PH\s*VAT\s*STD', text):
        sub = recover('subtotal', r'PH\s*VAT\s*STD(?: sales)?')
        recover('tax_amount', r'VAT')
        recover('vat_exempt_sales', r'PH\s*VAT\s*EXEMPT(?: sales)?')
        recover('zero_rated_sales', r'PH\s*VAT\s*ZERO(?: sales)?')
        put('currency', 'PHP')
        put('current_charges_total', None)
        if sub is not None and out.get('total_amount') is not None and re.search(r'Receipt Amount in PHP', text, re.I):
            out['_line_items_basis'] = 'total_amount'
        for i, s in enumerate(raw[:-1]):
            if re.fullmatch(r'(?i)(?:Bill To|Received From)', s):
                put('customer_name', raw[i+1])
                break

    # Net/tax/total service table (e.g. professional services). A gross row is
    # not a unit price. Retain separate fee and expense rows, excluding summary.
    if re.search(r'(?im)^Tax Amount$', text) and re.search(r'(?im)^Net$', text):
        recover('subtotal', r'Subtotal\s*:?')
        recover('tax_amount', r'Tax\s*@\s*12%\s*VAT\s*:?')
        put('current_charges_total', None)
        heads = {k: next((c for c in cells if re.fullmatch(p, c['text'], re.I)), None)
                 for k, p in [('description', 'Description'), ('quantity', 'Quantity'), ('unit_price', 'Unit Cost'), ('amount', 'Net')]}
        if all(heads.values()):
            h = heads['description']
            rows = []
            for c in cells:
                if c['y'] <= h['y'] or c['x'] > h['r']+h['h']:
                    continue
                vals = {}
                for k in ('unit_price', 'quantity', 'amount'):
                    head = heads[k]
                    candidates = [x for x in cells if abs(x['y']-c['y']) < max(x['h'], c['h'])*.6
                                  and abs(x['r']-head['r']) < h['h']*2 and number(x['text']) is not None]
                    if len(candidates) == 1:
                        vals[k] = number(candidates[0]['text'])
                if len(vals) == 3 and abs(vals['quantity']*vals['unit_price']-vals['amount']) < .02:
                    rows.append(dict(description=c['text'], **vals))
            if rows and out.get('subtotal') is not None and abs(sum(r['amount'] for r in rows)-out['subtotal']) < .02:
                put('line_items', rows)
                out['_normalization_issues'] = [s for s in out.get('_normalization_issues', []) if not s.startswith('line_items row ')]

    # Source party ownership: only use the seller block, not the first OCR line.
    for pat in (r'(?im)^\s*(Innove Communications,?\s*Inc\.?)\s*$',
                r'(?im)^\s*(Notarize,?\s*Inc\.?\s*dba\s*Proof)\s*$',
                r'(?im)^\s*(Puregold Price Club Inc\.?)\s*$',
                r'(?im)^\s*(ACE HARDWARE PHIL\w* INC\.?)\s*$'):
        m = re.search(pat, text)
        if m:
            compact = re.sub(r'[^a-z]', '', str(out.get('vendor_name') or '').casefold())
            issuer = re.sub(r'[^a-z]', '', m[1].casefold())
            if not (issuer in compact or (issuer.startswith('acehardware') and compact.startswith('acehardware'))):
                value = m[1].replace('PHILPIPINES', 'PHILIPPINES')
                if issuer.startswith('innove'):value = 'GLOBE BUSINESS: INNOVE COMMUNICATIONS, INC.'
                put('vendor_name', value)
            break
    # Invoice number appears explicitly in the footer on wire-payment invoices.
    m = re.search(r'(?im)^Invoice number\s+([A-Z]+\d+)\s*$', text)
    if m:
        put('invoice_number', m[1])

    # FROM and SHIP TO are separate source columns. Do not choose the buyer as
    # issuer simply because the OCR text interleaves the two address blocks.
    frm = next((c for c in cells if re.fullmatch(r'FROM\s*:', c['text'], re.I)), None)
    ship = next((c for c in cells if re.fullmatch(r'SHIP TO\s*:', c['text'], re.I)), None)
    if frm and ship and frm['x'] < ship['x']:
        for label, field, right in ((frm, 'vendor', ship['x']), (ship, 'customer', ship['x']+(ship['x']-frm['x']))):
            block = sorted([c for c in cells if label['y'] < c['y'] < label['y']+label['h']*12
                            and label['x']-label['h'] <= c['x'] < right
                            and not re.search(r'(?i)mobile|gross weight', c['text'])], key=lambda c:c['y'])
            if block:
                put(field+'_name', block[0]['text'])
                put(field+'_address', ' '.join(c['text'] for c in block[1:]))
        explicit = re.findall(r'(\d[\d,]*\.\d{2})\s*Philippine Peso', text, re.I)
        if explicit and len(set(explicit)) == 1:
            put('total_amount', number(explicit[0]))
            put('subtotal', number(explicit[0]))
            put('currency', 'PHP')

    frm = next((c for c in cells if c['text'].upper()=='FROM'), None)
    to = next((c for c in cells if c['text'].upper()=='TO'), None)
    if frm and to and frm['x'] < to['x'] and abs(frm['y']-to['y']) < max(frm['h'],to['h']):
        for label, field, right in ((frm,'vendor',to['x']-to['h']), (to,'customer',float('inf'))):
            block = sorted([c for c in cells if label['y'] < c['y'] < label['y']+label['h']*8 and label['x']-label['h'] <= c['x'] < right], key=lambda c:c['y'])
            postal = []
            for c in block:
                if re.search(r'@|(?i:invoice|due date|TIN|^\+?\d[\d-]{6,})', c['text']):break
                postal.append(c['text'])
            if len(postal)>1:
                put(field+'_name', postal[0])
                put(field+'_address', ' '.join(postal[1:]))

    # Currency evidence must belong to an amount/total. A dollar-looking OCR
    # digit or USD bank-payment instructions cannot override an explicit PHP.
    local = bool(re.search(r'(?i)\bPHP\s*[\d(]|[\d.)]\s*Philippine Peso|(?:Currency|Amount(?: Due)?)\s*[:(]\s*PHP', text))
    foreign_total = re.search(r'(?i)(?:invoice\s*total|total\s*amount(?:\s*due)?)\s*(?:in\s*|[:(]\s*)(USD|EUR|GBP|JPY|AUD|CAD|SGD)', text)
    if foreign_total:
        put('currency', foreign_total[1].upper())
    elif local:
        put('currency', 'PHP')
    elif retail and re.search(r'(?i)Metro Manila|Philippines', text) and not re.search(r'\b(?:USD|EUR|GBP|JPY|AUD|CAD|SGD)\b', text):
        put('currency', 'PHP')
    if re.match(r'(?i)^(?:machine\s*S/?N\s*:|for (?:inquiries|further assistance)|commercial invoice$|invoice no\s*:)', str(out.get('vendor_name') or '')):
        put('vendor_name', None)
    if re.search(r'(?i)^Account Number|metering facilities|please contact', str(out.get('customer_name') or '')):
        put('customer_name', None)
    return out, notes
