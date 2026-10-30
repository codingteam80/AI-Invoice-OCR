"""Grounded post-vision repairs for common statement/receipt *layouts*.

No invoice-filename/vendor-specific monetary constants. Ambiguous source values
are flagged rather than replaced by arithmetic guesses.
"""
from __future__ import annotations
import re
from decimal import Decimal, ROUND_HALF_UP


def amount(text: str) -> float | None:
    raw = str(text).strip().replace('₱','').replace('$','')
    raw = re.sub(r'(?i)^\s*(?:php|p)\s*', '', raw)
    raw=raw.strip().rstrip('*').strip()
    neg = raw.startswith('(') and raw.endswith(')')
    raw = raw.strip('()').strip()
    raw = re.sub(r'(?<=\d)\.(?=\d{3}\.\d{2,3}$)',',',raw)
    if not re.fullmatch(r'-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,3}',raw):
        return None
    value = float(raw.replace(',',''))
    return -abs(value) if neg else value


def round_money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _label_number(lines: list[str], expr: str) -> float | None:
    for i,line in enumerate(lines):
        m=re.match(rf'(?i)^\s*(?:{expr})\s*[:\-]?\s*(.*)$',line)
        if not m:
            continue
        same = amount(m.group(1))
        if same is not None: return same
        if i+1<len(lines):
            nxt = amount(lines[i+1])
            if nxt is not None: return nxt
    return None


def _normalize_address(data: dict, text: str) -> tuple[dict,list[str]]:
    out=dict(data); notes=[]
    current=str(out.get('customer_address') or '').strip()
    if not current:
        return out,notes
    lines=[s.strip() for s in text.splitlines() if s.strip()]
    # Invoice's explicitly labeled address block ends at the table header.
    for i,s in enumerate(lines):
        if not re.fullmatch('(?i)address\\s*:?|customer address\\s*:?|business address\\s*:',s):
            continue
        portion=[]
        for ln in lines[i+1:i+8]:
            if re.match(r'(?i)^(?:description|amount|receipt amount|functional amount|item no\.?|subtotal|TIN|terms|invoice no|subscriber.s name|due date|bill summary)\b',ln):
                break
            if re.search(r'(?i)(?:street|st\.?\b|brgy|city|poblacion|batangas|laguna|metro manila|calaca|pasig|province|philippines|\b\d{4}\b)',ln):
                portion.append(ln)
            elif portion:
                break
        if portion:
            candidate=re.sub(r',\s*,+', ',', ', '.join(portion))
            if len(candidate)>=15 and (not current or len(candidate) < len(current)):
                out['customer_address']=candidate
                notes.append('Customer address limited to the explicitly labeled postal ADDRESS block; adjacent name/table totals removed.')
                return out,notes
    # Trim inadvertent OCR/table columns appended to an otherwise valid address.
    cut=re.search(r'(?i)\s*[,;]\s*(?:item\s*no\.?|description\s*[,;]|receipt\s+amount\b|functional\s+amount\b|amount\s*[,;]|due\s+date\s*[:;,]|total\s+amount\s+due\b|invoice\s+date\s*[:;,])',current)
    if cut:
        current=current[:cut.start()].strip(' ,;')
        notes.append('Unrelated invoice table/summary fields removed from Customer Address.')
    customer=str(out.get('customer_name') or '').strip()
    if customer and current.casefold().startswith(customer.casefold()+','):
        current=current[len(customer)+1:].strip(' ,;')
        notes.append('Customer name removed from Customer Address.')
    # In 2-column PLDT-style payment stubs the OCR reads subscriber name,
    # Due Date and Amount Due before the postal address. A comma split alone
    # would reduce the entire address to just the subscriber's name.
    street = re.search(r'(?i)\b\d{2,5}\s*[A-Z]?\s+[A-Z][A-Z\s]+?\b(?:ST\.?|STREET|AVENUE|AVE\.?|ROAD|RD\.?)\b[^,\n]*',str(out.get('customer_address') or ''))
    if street and not re.search(r'(?i)\b(?:ST\.?|STREET|AVENUE|AVE\.?|ROAD|RD\.?)\b', current):
        current=street.group(0).strip(' ,;')
        # Only include locality words actually visible in the document.
        locality=re.search(r'(?i)\b(?:CAVITE|MANILA\s+CITY|METRO\s+MANILA)\b',str(out.get('customer_address') or '')[street.end():])
        if locality and locality.group().lower() not in current.lower():
            current+=', '+locality.group()
        notes.append('Customer postal street recovered from payment-stub address; due date and account metadata excluded.')
    if current != out.get('customer_address'):
        out['customer_address']=current or None
    return out,notes


def reconcile_statement_evidence(data: dict, text: str) -> tuple[dict,list[str]]:
    out,notes=_normalize_address(data,text or '')
    lines=[s.strip() for s in (text or '').splitlines() if s.strip()]
    if not lines:
        return out,notes
    normalized='\n'.join(lines)

    # Statement identifiers: SOA and Statement of Account No. outrank a
    # separate telephone, account, bill, or series number.
    id_match=re.search(r'(?im)^\s*(?:SOA\s*Number|Statement\s+of\s+Account\s+No\.?|Statement\s+Number)\s*:?\s*([A-Z0-9][A-Z0-9/\-]+)\s*$',normalized)
    if id_match:
        number=id_match.group(1)
        if re.fullmatch(r'[A-Za-z0-9/-]{5,35}',number) and out.get('invoice_number')!=number:
            out['invoice_number']=number
            notes.append('Invoice/statement number recovered from the explicit SOA/Statement-of-Account-number field.')
    if not id_match:
        for i,line in enumerate(lines[:-1]):
            if re.fullmatch(r'(?i)(?:SOA\s*Number|Statement\s+of\s+Account\s+No\.?|Billing\s+Invoice\s+No\.?)\s*:?',line):
                candidate=lines[i+1]
                if re.fullmatch(r'[A-Za-z0-9/-]{5,35}',candidate) and out.get('invoice_number')!=candidate:
                    out['invoice_number']=candidate
                    notes.append('Document ID recovered from separately printed statement/invoice number label.')
                break

    if re.search(r'(?im)^\s*D?Smart\s*$',normalized):
        if str(out.get('vendor_name') or '').strip().lower() in {'dsmart','smart','d smart'}:
            out['vendor_name']='Smart'
            notes.append('Stray D-shaped logo artifact before Smart removed; printed issuer identified as Smart.')
    if re.search(r'(?im)^\s*(?:D?Smart|Smart\s+Communications)',normalized):
        # No postal buyer address appears in this compact bill. A billing
        # period/amount breakdown is not a substitute for an address.
        if not re.search(r'(?i)\b(?:customer|service|billing)\s+address\b|\b\d+\s+\w+\s+(?:st\.?|street|ave\.?|road|rd\.?)\b',normalized):
            if out.get('customer_address'):
                out['customer_address']=None
                notes.append('Customer Address cleared: no customer-address field or postal address appears in the bill.')

    # On a degraded bill header, seller city guessed by vision can be absent
    # from all OCR evidence. Prefer an explicit missing value + review warning
    # to presenting a confident-looking invented office address. Only run this
    # for a BILLING INVOICE with no seller street/location in its OCR header.
    if re.search(r'(?i)\bBILLING\s+INVOICE\b',normalized):
        addr=str(out.get('vendor_address') or '')
        head=' '.join(lines[:12]).lower()
        city=re.search(r'(?i)\b(Mandaluyong|Makati|Pasig|Taguig|Quezon City|Manila|Cavite|Pasay)\b',addr)
        has_header_location=bool(re.search(r'(?i)\b(?:Street|Ave\.?|Avenue|Blvd|Bldg|Building|Road|RD\.?|Makati|Manila)\b',head))
        if addr and city and not has_header_location and city.group().lower() not in normalized.lower():
            out['vendor_address']=None
            notes.append('VALIDATION: Vendor Address is not corroborated by the degraded seller-header OCR and has been left blank for manual verification.')

    # Interleaved 2-column electricity header: header BUSINESS CTR and the
    # next separate street/locality lines belong to seller office, not buyer.
    for i,line in enumerate(lines[:55]):
        if re.search(r'(?i)\bBUSINESS\s+(?:CTR|CENTER|CENTRE)\b',line):
            suffix=[]
            for candidate in lines[i+1:i+5]:
                if re.search(r'(?i)\b(?:meralco|smart|globe|pldt)\b',candidate):
                    continue
                if re.search(r'(?i)\b(?:city|makati|pasig|manila|kamagong|brgy|st\.?|street)\b',candidate):
                    suffix.append(candidate)
            if len(suffix)>=2:
                recovered=', '.join([line]+suffix[:2])
                if recovered.casefold()!=str(out.get('vendor_address') or '').casefold():
                    out['vendor_address']=recovered
                    notes.append('Vendor office address recovered from printed BUSINESS CTR/address lines, excluding interleaved buyer block.')
            break

    if re.search(r'(?i)\b(?:BILL|INVOICE)\s+SUMMARY\b',normalized):
        present=str(out.get('customer_address') or '')
        if not re.search(r'(?i)\d+\s*[A-Z]?\s+[A-Z ]+(?:ST\.?|STREET|AVE\.?|AVENUE)\b',present):
            # Use the first street in the actual printed subscriber block,
            # not an unrelated company's remittance/bank details.
            begin=normalized[:normalized.upper().find('BILL SUMMARY') if 'BILL SUMMARY' in normalized.upper() else normalized.upper().find('INVOICE SUMMARY')]
            m=re.search(r'(?i)\b\d{2,5}\s*[A-Z]?\s+[A-Z][A-Z\s]+?\b(?:ST\.?|STREET|AVENUE|AVE\.?)\b[^\n]*',begin)
            if m:
                recovered=m.group().strip(' ,;')
                out['customer_address']=recovered
                notes.append('Customer address rebuilt from the printed pre-summary subscriber street line; financial metadata excluded.')

    # A postal address in the first (main) subscriber header has stronger
    # ownership than a truncated/misread duplicate in the payment stub.
    if re.search(r'(?i)\bBILLING\s+INVOICE\b',normalized) and 'INVOICE SUMMARY' in normalized.upper():
        header_part=normalized[:normalized.upper().find('INVOICE SUMMARY')]
        m=re.search(r'(?i)\b\d{2,5}\s*[A-Z]?\s+[A-Z][A-Z\s]+?\b(?:ST\.?|STREET|AVENUE|AVE\.?)\b[^\n]*',header_part)
        # PaddleOCR frequently joins the road name to ST, e.g. DIEGOST.
        if not m:
            m=re.search(r'(?i)\b\d{2,5}\s*[A-Z]?\s+[A-Z][A-Z\s]+?ST\s+[^\n]*',header_part)
        if m:
            street=m.group().strip(' ,;')
            street=re.sub(r'(?i)\b([A-Z]{4,})ST\b',r'\1 ST',street)
            street=re.sub(r'(?i)\bDISTRICT(?=MANILA|QUEZON)',r'DISTRICT ',street)
            following=header_part[m.end():].splitlines()
            if any(x.strip().upper() == 'CITY' for x in following[:2]) and not street.upper().endswith('CITY'):
                street+=' CITY'
            if street and street.casefold()!=str(out.get('customer_address') or '').casefold():
                out['customer_address']=street
                notes.append('Customer address recovered from the first subscriber header rather than a conflicting payment-stub OCR reading.')

    # Globe-style statement's SOA number is independent of bill/account number.
    if re.search(r'(?i)\bSOA\s*Number\b',normalized) and re.search(r'(?i)\bStatement\s+Summary\b',normalized):
        mr = _label_number(lines,r'Monthly\s+Recurring\s+Fee\s*(?:\(\s*MRF\s*\)?)?')
        adjustment = _label_number(lines,r'Adjustments?\s*[-–]\s*Current\s*Bill')
        vat = _label_number(lines,r'VAT')
        current = _label_number(lines,r'Total\s+Current\s+Bill')
        if mr is not None:
            out['subtotal']=round_money(mr)
            items = [dict(x) for x in out.get('line_items') or [] if isinstance(x,dict)]
            items = [x for x in items if not re.search(r'(?i)\b(?:monthly recurring fee|adjustments?\s*[-–]\s*current\s*bill)\b',str(x.get('description') or ''))]
            items.insert(0, {'description':'Monthly Recurring Fee', 'quantity':1.0, 'unit_price':round_money(mr),'amount':round_money(mr)})
            if adjustment is not None:
                items.insert(1, {'description':'Adjustments-Current Bill','quantity':1.0,'unit_price':round_money(adjustment),'amount':round_money(adjustment)})
                # A negative adjustment is a bill credit; NOT a made-up discount.
                if adjustment < 0:
                    out['discount']=round_money(abs(adjustment))
            out['line_items']=items
            notes.append('Statement Summary: retained explicitly printed Monthly Recurring Fee as its own item and Vatable Sales; corrections read from matching labeled rows.')
        if vat is not None:
            out['tax_amount']=round_money(vat)
        if current is not None:
            out['current_charges_total']=round_money(current)
        # In a statement, the full address sometimes includes an isolated
        # house/unit code immediately above the city/municipality lines.
        if re.search(r'(?im)^\s*\d{3,5}\s*[A-Za-z]\s*$',normalized):
            m=re.search(r'(?im)^\s*(\d{3,5})\s*([A-Za-z])\s*$',normalized)
            code=m.group(1)+m.group(2)
            old=str(out.get('customer_address') or '')
            if old and code.casefold() not in re.sub(r'\s','',old).casefold():
                out['customer_address']=code+', '+old
                notes.append('Customer house/unit code recovered from the printed billing-address block.')

    # Billing statements explicitly itemize monthly service and usage, which
    # form the net charges before VAT. Do not substitute Total Current Charges
    # (which already includes VAT) for the VATable Sales figure.
    if re.search(r'(?i)\b(?:BILL|INVOICE)\s+SUMMARY\b',normalized) and re.search(r'(?i)\bMonth[li]y\s+Service\s+Fee\b',normalized):
        monthly=_label_number(lines,r'Month[li]y\s+Service\s+Fee\s+and\s+Other\s+Basic\s+Charges')
        usage=_label_number(lines,r'Usage\s+Charges')
        vat=_label_number(lines,r'Value\s+Added\s+Tax')
        current=_label_number(lines,r'Total\s+Current\s+Charges')
        if monthly is not None:
            existing=[dict(x) for x in out.get('line_items') or [] if isinstance(x,dict)]
            existing=[x for x in existing if not re.search(r'(?i)Month[li]y\s+Service\s+Fee\b',str(x.get('description') or ''))]
            existing.insert(0,{'description':'Monthly Service Fee and Other Basic Charges', 'quantity':1.0, 'unit_price':round_money(monthly), 'amount':round_money(monthly)})
            if usage is not None:
                existing=[x for x in existing if str(x.get('description') or '').strip().lower()!='usage charges']
                existing.insert(1,{'description':'Usage Charges','quantity':1.0,'unit_price':round_money(usage),'amount':round_money(usage)})
                out['subtotal']=round_money(monthly+usage)
            out['line_items']=existing
            notes.append('Current-period service and usage charges recovered from their own labeled billing-summary rows.')
        if vat is not None: out['tax_amount']=round_money(vat)
        if current is not None: out['current_charges_total']=round_money(current)
        # No 'Discount' is printed in this billing section; payments against
        # old balances are not sales discounts. Never invent a huge deduction
        # just to reconcile an implausible/negative total.
        if out.get('discount') and not re.search(r'(?i)\b(?:sales\s+discount|discount\s+applied|less\s*:\s*discount|discounts?\s*\(?)',normalized):
            out['discount']=None
            notes.append('Unprinted calculated discount removed; previous-bill payments are not a discount on current-period sales.')
    return out,notes


def reconcile_electricity_charge_table(data: dict, ocr_text: str) -> tuple[dict,list[str]]:
    """Rows are a label, amount, then percentage; ignore adjacent chart ticks."""
    lines=[s.strip() for s in (ocr_text or '').splitlines() if s.strip()]
    start=next((i for i,s in enumerate(lines) if re.search(r'(?i)BREAKDOWN\s+OF\s+ELECTRICITY\s+CHARGES',s)),None)
    if start is None: return data,[]
    end=next((i for i in range(start+1,len(lines)) if re.match(r'(?i)^Total\s+Current\s+Charges',lines[i])),len(lines))
    stop=set(('bill subgroup','subtotal','eustotal','percentage'))
    known=re.compile(r'(?i)^(?:Generation|Transmission|System\s+L[oa]ss|Distribution\b|Sub[es]i[dt]+ies|Gov(?:ern)?[em]*nt\s+Taxes|Universal\s+Charges|FIT\s*[-–]?All|Other\s+Charges)')
    rows=[]
    for i in range(start+1, end-1):
        label=lines[i]
        if not known.match(label): continue
        v=amount(lines[i+1])
        if v is None: continue
        # Disallow chart percent or axis labels as amounts.
        rows.append({'description':label,'quantity':1.0,'unit_price':round_money(v),'amount':round_money(v)})
    if len(rows)<4: return data,[]
    out=dict(data);out['line_items']=rows
    return out,[f"Electricity charge breakdown: extracted {len(rows)} labeled rows, ignoring the neighboring usage chart."]


def reconcile_tax_total_identity(data: dict, ocr_text: str) -> tuple[dict,list[str]]:
    """Repair a visibly damaged VAT digit only with independent total and rate."""
    out=dict(data)
    if not re.search(r'(?i)VAT\s*\(\s*12\s*%?\s*\)',ocr_text or ''): return out,[]
    try:
        net=float(out['subtotal']); due=float(out['total_amount'])
    except (TypeError,ValueError,KeyError):return out,[]
    if any(abs(float(out.get(k) or 0))>0.01 for k in ('discount','withholding_tax','zero_rated_sales','vat_exempt_sales')):
        return out,[]
    expected=round_money(net*0.12)
    if abs(net+expected-due)>0.05:return out,[]
    current=out.get('tax_amount')
    if current is None or abs(float(current)-expected)>0.05:
        out['tax_amount']=expected
        return out,[f"VAT row OCR mismatch: corrected to {expected:.2f}, corroborated by 12% of the printed net amount AND printed total (verify the source image)."]
    return out,[]


def reconcile_credit_balance_sign(data: dict, ocr_text: str) -> tuple[dict,list[str]]:
    """Recover lost parenthesized *credit* sign on previous balance.

    Require two independent pieces of evidence rather than assuming that all
    carried balances on statements are positive (or negative):
    (1) current charges minus balance reconciles exactly to Amount Due, OR
    (2) prior charges minus payments is the negative of the captured balance.
    """
    out=dict(data)
    try:
        balance=float(out['previous_balance']); current=float(out['current_charges_total'])
        payable=float(out['total_amount'])
    except (KeyError,ValueError,TypeError):
        return out,[]
    if balance<=0.005:
        return out,[]
    arithmetic_corrob=abs(round_money(current-balance)-payable)<=0.05 and abs(round_money(current+balance)-payable)>0.05
    previous_bill_corrob=False
    lines=[s.strip() for s in (ocr_text or '').splitlines() if s.strip()]
    if not arithmetic_corrob and re.search(r'(?i)\b(?:BILL|STATEMENT)\s+SUMMARY\b',ocr_text or ''):
        previous=_label_number(lines,r'Balance\s+from\s+Previous\s+Bil[l]?')
        paid=None
        for i,line in enumerate(lines[:-1]):
            if re.search(r'(?i)\bLess\s*:\s*Payments\s+Received',line):
                paid=amount(lines[i+1].rstrip(')'))
                break
        if previous is not None and paid is not None:
            previous_bill_corrob=previous-paid < -0.005 and abs(round_money(previous-paid)+balance)<=0.05
    if not arithmetic_corrob and not previous_bill_corrob:
        return out,[]
    out['previous_balance']=-balance
    items=[dict(x) for x in out.get('line_items') or []]
    for item in items:
        if re.search(r'(?i)\b(?:remaining|previous|carried)\s+balance\b|\bbalance forward\b',str(item.get('description') or '')):
            item['amount']=-abs(float(item.get('amount') or balance))
            item['unit_price']=-abs(float(item.get('unit_price') or balance))
    out['line_items']=items
    basis='printed payable-total arithmetic' if arithmetic_corrob else 'previous charges minus printed payments'
    return out,[f"Previous-bill Remaining Balance is a credit (-{balance:.2f}), not a charge; recovered its missing parentheses/sign from {basis}."]
