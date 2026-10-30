"""Conservative QA hardening for utilities and service statements.

No filename or invoice-number lookup tables: all corrections require evidence
from the current document. Conflicting/illegible facts remain review issues.
"""
from __future__ import annotations

import re
from copy import deepcopy
from parser.currency_parser import to_float
from utils.date_utils import parse_date

_AMOUNT = r"\(?-?\d[\d,]*\.\d{2}\)?\s*(?:CR)?"
_VALUE = re.compile(r"^[P₱^\s]*" + _AMOUNT + r"\s*$", re.I)


def _money(s):
    s = str(s or '').strip()
    if not _VALUE.fullmatch(s):
        return None
    credit = bool(re.search(r'CR\s*$', s, re.I) or s.startswith('(') or s.lstrip('P₱^ ').startswith('('))
    value = to_float(re.sub(r'(?i)CR\s*$', '', s).strip(' ^P₱'))
    return -abs(value) if credit and value is not None else value


def _label_values(lines, regex, lookahead=3):
    """Read a value on the labeled line or immediately following numeric line.

    Avoids skipping intervening field labels when OCR interleaves columns.
    """
    pat = re.compile(regex, re.I)
    values = []
    for i, line in enumerate(lines):
        if not pat.fullmatch(line):
            continue
        inline = re.search(r'(?<!\w)(?:-?\d[\d,]*\.\d{2}\s*(?:CR)?)\s*$', line)
        if inline:
            val = _money(inline.group())
            if val is not None:
                values.append(val)
                continue
        for later in lines[i+1:i+1+lookahead]:
            if _money(later) is not None:
                values.append(_money(later))
                break
            if not re.fullmatch(r'[:P₱^\s-]+', later):
                break
    return values


def _single(values):
    distinct = {round(v, 2) for v in values if v is not None}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _poly_center(entry):
    pts = entry.get('bbox') or []
    if len(pts) < 4:
        return None
    try:
        return (sum(p[0] for p in pts)/len(pts), sum(p[1] for p in pts)/len(pts))
    except (TypeError, ValueError, IndexError):
        return None


def _header_columns(text, ocr_lines):
    """Recover independently printed recipient and business-center columns.

    Used only when an electricity bill clearly prints these two columns.
    The header's original order can be interleaved by the OCR reading order.
    """
    if not re.search(r'(?i)\b(?:electric bill|your electric bill)\b', text):
        return {}
    cells = [(e.get('text', '').strip(), *_poly_center(e)) for e in ocr_lines or [] if _poly_center(e)]
    offices = [(t,x,y) for t,x,y in cells if re.match(r'(?i)^(?:[A-Z ]{1,26}\s+)?BUS(?:INESS|\.)?\s*(?:CENTER|CTR\.?)$', t) and y < max((cy for _,_,cy in cells),default=0)*.22]
    if len(offices) != 1:
        return {}
    office, ox, oy = offices[0]
    header_end = min([y for t,x,y in cells if y>oy+10 and re.search(r'(?i)^(?:Meter\s*No|Your electric bill|Electric Bill|B-QL-\d|ICP MTR)',t)] or [oy+95])
    header = [(t,x,y) for t,x,y in cells if oy-7 <= y < header_end and not re.search(r'(?i)^\s*(?:Tel\.?|TIN|VAT|Billing|Meter|MERALCO|Page\b|Route|Print|B-QL-)',t)]
    office_lines = [t for t,x,y in sorted(header,key=lambda e:e[2]) if ox-100<x<ox+190 and y<=oy+95]
    # Do not accidentally include issuer logo or OCR from unrelated columns.
    office_lines = office_lines[:4]
    result = {}
    if office_lines and re.search(r'(?i)\bBUS(?:INESS|\.)?\s*(?:CENTER|CTR\.?)',office_lines[0]):
        result['vendor_address'] = ' '.join(office_lines).strip()
    recipient = [(t,x,y) for t,x,y in cells if y<header_end and y>=oy-25 and x<ox-120 and
                 not re.search(r'(?i)^(?:For inquiries|Tel|TIN|Billing|Meter|MERALCO|Page)',t)]
    recipient.sort(key=lambda e:e[2])
    if len(recipient)>=2 and len(recipient[0][0])>=5 and not re.search(r'(?i)\b(?:BILL|INVOICE|CTR|CENTER)\b',recipient[0][0]):
        result['customer_name']=recipient[0][0]
        block=[t for t,_,_ in recipient[1:5] if not re.search(r'(?i)^(?:Your electric bill|Page\s|Tel|TIN)',t)]
        if block: result['customer_address']=' '.join(block)
    return result



def _electricity_charge_rows(ocr_lines, current):
    """Recover a *complete*, arithmetically corroborated electricity table.

    Discards partial tables, OCR tokens from other columns and percent rates.
    No source value is guessed from a header total.
    """
    if current is None or not ocr_lines:
        return []
    cells=[]
    for entry in ocr_lines:
        center=_poly_center(entry)
        if not center:continue
        pts=entry.get('bbox') or []
        xs=[p[0] for p in pts];ys=[p[1] for p in pts]
        cells.append({'text':str(entry.get('text') or '').strip(), 'x':center[0], 'y':center[1],
                      'l':min(xs),'r':max(xs),'h':max(ys)-min(ys)})
    heading=next((c for c in cells if re.search(r'(?i)^(?:Bill Computation Summary|BREAKDOWN OF ELECTRICITY CHARGES)$',c['text'])),None)
    if not heading:return []
    labels=r'(?:Generation|Transmission|System Loss|Distribution\s*\(Meralco\)|Subsidies|Senior Citizen|Government Taxes|Un[iyj]versal Charges|Fi[Tt]-A[lI]{1,2}\s*\(Renewable\)|Applied Credits|GEA-All\s*\(Renewable\)|Lifeline|Other\s*Charges)'
    candidates=[c for c in cells if c['y']>heading['y'] and c['y']<heading['y']+700 and re.fullmatch(labels,c['text'],re.I)]
    if len(candidates)<4:return []
    rows=[]
    for label in sorted(candidates,key=lambda c:c['y']):
        valid=[]
        for cell in cells:
            val=_money(cell['text'])
            if val is None or cell['l']<=label['r'] or not (cell['x']>label['x']):continue
            delta=abs(cell['y']-label['y'])
            if delta>max(6,label['h']*.85,cell['h']*.85):continue
            valid.append((delta,cell['x'],val))
        if not valid:return []
        # Primary monetary column is the first numeric column to the right.
        val=min(valid,key=lambda k:(k[0],k[1]))[2]
        desc=re.sub(r'(?i)^Un[iyj]versal','Universal',label['text'])
        desc=re.sub(r'(?i)^Fi[Tt]-A[lI]{1,2}','FIT-All',desc)
        rows.append({'description':desc,'quantity':1,'unit_price':val,'amount':val})
    if abs(sum(r['amount'] for r in rows)-current)>.055:return []
    return rows

def reconcile_qa_source_review(data: dict, ocr_text: str, ocr_lines: list | None = None) -> tuple[dict,list[str]]:
    """Bounded document-evidence recovery and meaningful manual-review warnings."""
    out = deepcopy(data)
    notes = []
    text = ocr_text or ''
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    issues = list(out.get('_normalization_issues') or [])
    def set_field(key, value, description):
        if value is not None and out.get(key) != value:
            out[key] = value
            notes.append(description)
    def issue(message):
        if message not in issues:
            issues.append(message)

    # Explicit buyer identifiers override placeholders and identifiers borrowed
    # from account, service, printer or vendor blocks.
    for i,s in enumerate(lines):
        if re.fullmatch(r'(?i)customer\s*TIN\s*:?|buyer\s*TIN\s*:?',s):
            for t in lines[i+1:i+6]:
                if re.fullmatch(r'\d{3}-\d{3}-\d{3}-\d{3,5}',t):
                    set_field('customer_tax_id',t,'Customer TIN recovered from its independent printed label.')
                    break
                if re.search(r'(?i)^(?:vendor TIN|VAT Reg\.? TIN|payment)',t):break
            break

    is_energy = bool(re.search(r'(?i)\bMERALCO\b',text) and re.search(r'(?i)\belectric bill\b|\bBilling Invoice\b',text))
    if is_energy:
        # Some one-page billing-invoice copies omit the subscriber and bill
        # date. Never promote the payment due date or a charge table into them.
        if re.search(r'(?i)^\s*Billing Invoice\s*$', text, re.M) and not re.search(r'(?i)\b(?:Bill Date|Statement Date|Invoice Date)\b', text):
            if out.get('invoice_date') is not None:
                out['invoice_date'] = None
                issue('Invoice date not printed; a due date must not be used as the invoice date.')
            addr = str(out.get('customer_address') or '')
            if re.search(r'(?i)Distribution \(Meralco\)|Government Taxes|Generation,|FiT-All',addr):
                out['customer_address'] = None
                issue('Customer address is not printed; electricity-charge labels were excluded.')
        header = _header_columns(text,ocr_lines)
        if header.get('vendor_address'):
            set_field('vendor_address',header['vendor_address'],'Business-center address recovered from the seller header column.')
        if header.get('customer_name'):
            set_field('customer_name',header['customer_name'],'Customer recovered from the separate recipient header column.')
        if header.get('customer_address'):
            set_field('customer_address',header['customer_address'],'Customer postal address recovered from the separate recipient header column.')
        # When VAT summary is not printed, derived values from unrelated tax
        # categories must not be recorded as source-supported Vatable Sales/VAT.
        if not re.search(r'(?i)\bVAT(?:ABLE)?\s+SALES?\b|\bVAT\s+AMOUNT\b',text):
            if out.get('subtotal') is not None or out.get('tax_amount') is not None:
                out['subtotal'] = out['tax_amount'] = None
                notes.append('Unprinted electricity-bill VAT/net-sales guesses cleared.')
            issue('VATable Sales and VAT are not explicitly provided on this page; check the detailed tax breakdown before relying on these values.')
        rows=_electricity_charge_rows(ocr_lines,to_float(out.get('current_charges_total')))
        if rows:
            old=out.get('line_items') or []
            old_set={(str(x.get('description')),x.get('amount')) for x in old}
            new_set={(x['description'],x['amount']) for x in rows}
            if old_set!=new_set:
                out['line_items']=rows
                out['_line_items_basis']='current_charges_total'
                issues=[v for v in issues if not re.search(r'(?i)line_items row|line item.*mismatch',v)]
                notes.append('Electricity charge rows recovered from their individual printed amount columns; sum agrees with current charges.')
        # Billing/statement date, never a due date or printer permit date.
        # OCR string order may put the left-column billing period before the
        # right-column Bill Date. Match by x/y in that case.
        dated = []
        for label in ocr_lines or []:
            if not re.fullmatch(r'(?i)Bill\s*Date',str(label.get('text') or '').strip()):
                continue
            location = _poly_center(label)
            if not location:continue
            lx,ly=location
            for cell in ocr_lines or []:
                coord=_poly_center(cell)
                if not coord:continue
                x,y=coord
                if lx-45<x<lx+125 and 0<y-ly<36:
                    date=parse_date(str(cell.get('text') or '').strip())
                    if date:dated.append(date.isoformat())
        if len(set(dated))==1:
            set_field('invoice_date',dated[0],'Bill Date matched to its printed column, excluding the billing period.')
        elif not dated:
            for j,s in enumerate(lines):
                if re.fullmatch(r'(?i)Bill\s*Date',s):
                    d=parse_date(lines[j+1]) if j+1<len(lines) else None
                    if d:set_field('invoice_date',d.isoformat(),'Bill Date recovered from its adjacent printed label.')
                    break

    # Multiple VATable candidates in the *same* statement are evidence of an
    # OCR discrepancy, even when one value balances tax and total exactly.
    is_converge = 'CONVERGE' in text.upper() and 'CURRENT CHARGES' in text.upper()
    if is_converge:
        recurring = _single(_label_values(lines, r'Recurring Charges',lookahead=2))
        before_tax = _single(_label_values(lines, r'(?:TOTAL\s+)?CURRENT CHARGES BEFORE TAX',lookahead=2))
        if recurring is not None and before_tax is not None and abs(recurring-before_tax)>=.005:
            issue(f'Conflicting printed pre-tax values ({recurring:,.2f} and {before_tax:,.2f}); verify Vatable Sales against the original invoice.')

    # Very low-quality partial electricity receipts sometimes generate
    # implausible unit prices (e.g. many unrelated charges all at 0.02).
    # Keep evidence to review, not fabricated numeric line items.
    if is_energy and not rows:
        current=to_float(out.get('current_charges_total'))
        existing=[dict(r) for r in out.get('line_items') or []]
        values=[to_float(r.get('amount')) for r in existing]
        values=[v for v in values if v is not None]
        if len(values)>=5 and current is not None and abs(sum(values)-current)>max(.5,current*.1):
            from collections import Counter
            if max(Counter(round(v,2) for v in values).values())>=4:
                out['line_items']=[]
                issue('Electricity charge table has repeated implausible OCR amounts and does not reconcile; line-item prices need image verification.')
                notes.append('Uncorroborated repeated-placeholder electricity prices suppressed.')

    is_pldt = bool(re.search(r'(?i)\bPLDT\b|PHILIPPINE LONG DISTANCE TELEPHONE COMPANY',text) and re.search(r'(?i)STATEMENT\s+OF\s+ACCOUNT',text))
    if is_pldt:
        # A Statement of Account identifier is not the account/phone number
        # and should take priority when its own label is legible.
        soa_ids = re.findall(r'(?i)Statement\s*of\s*Acc[o0]unt\s*(?:Number|No\.?)\s*:?\s*(\d{8,14})',text)
        if len(set(soa_ids))==1:
            set_field('invoice_number',soa_ids[0],
                      'Statement of Account number recovered from its explicit label.')
        # Recover the payable from an expressly printed TOTAL AMOUNT DUE,
        # tolerating a space inside AMOUNT in older photo OCR.
        dues = _label_values(lines,r'TOTAL\s+AM\s*OUNT\s+DUE',lookahead=2)
        if len(set(dues))==1:
            set_field('total_amount',dues[0],
                      'Total payable recovered from its separately printed amount-due label.')
        vat = _single(_label_values(lines,r'Value Added Ta(?:x|ll)(?:\s*\(VAT\)\s*-\s*\d+%)?',lookahead=2))
        if vat is not None:
            set_field('tax_amount',vat,'VAT recovered from its independent printed tax line.')
        amounts = _label_values(lines,r'Month[lty]+\s+Service Fee(?: and Other Basic Charges|\s*\(VAT Exclusive\))?',lookahead=2)
        fee = _single(amounts)
        if fee is not None:
            # Only a directly printed service charge, not the carried balance.
            items=[dict(x) for x in out.get('line_items') or []]
            idx=next((i for i,it in enumerate(items) if re.search(r'(?i)Monthly Service Fee',str(it.get('description') or ''))),None)
            row={'description':'Monthly Service Fee and Other Basic Charges','quantity':1,'unit_price':fee,'amount':fee}
            if idx is None:
                items.insert(0,row)
            else:
                items[idx].update(row)
            # A named service plan may already represent exactly this fee.
            # Do not keep the identical plan AND its category subtotal.
            plan_rows = [it for it in items if re.search(r'(?i)\b(?:fibr|fiber|fibre|dsl|unli\s*plan|broadband\s*plan)\b',str(it.get('description') or '')) and abs((to_float(it.get('amount')) or 0)-fee)<.02]
            if len(plan_rows)==1:
                items=[it for it in items if not re.fullmatch(r'(?i)Monthly Service Fee and Other Basic Charges',str(it.get('description') or '').strip())]
                notes.append('Duplicated monthly category subtotal removed; individual printed service plan retained.')
            out['line_items']=items
            # If the printed net is not labeled, don't substitute a tax amount.
            if not re.search(r'(?i)\bVATABLE\s*SALES?\b',text):
                other = _single(_label_values(lines,r'Usage Charges',lookahead=2))
                set_field('subtotal',round(fee+(other or 0),2),'Vatable Sales recovered from printed current service charges, not VAT.')
        # Stand-alone CR markers on the carried balance are sign-bearing.
        for i,s in enumerate(lines):
            if re.fullmatch(r'(?i)Remaining Balance from Previous Bill(?:\s*\(VAT Inclusive\))?',s):
                val=_single([_money(t) for t in lines[i+1:i+4] if _money(t) is not None])
                if val is not None and val<0:
                    set_field('previous_balance',val,'Printed CR remaining balance preserved as a deduction.')
                break
        if out.get('previous_balance') is not None:
            balance=to_float(out['previous_balance'])
            total=to_float(out.get('total_amount'))
            current=to_float(out.get('current_charges_total'))
            # The sign is allowed to change only when current+credit=total,
            # and source printed an explicit CR flag for that same magnitude.
            if balance is not None and balance > 0 and current is not None and total is not None and abs(current-balance-total)<.02 and re.search(r'(?i)\b'+re.escape(f'{balance:.2f}')+r'\s*CR\b',text):
                set_field('previous_balance',-balance,'Credit sign independently confirmed by current charges and total payable.')
        current = to_float(out.get('current_charges_total'))
        total = to_float(out.get('total_amount'))
        if current is not None and total is not None and current>total:
            difference = round(current-total,2)
            printed_credits = [abs(_money(m.group())) for m in re.finditer(
                r'(?<!\d)\d[\d,]*\.\d{2}\s*CR\b',text,re.I) if _money(m.group()) is not None]
            if sum(abs(x-difference)<.015 for x in set(round(v,2) for v in printed_credits))==1:
                set_field('previous_balance',-difference,
                    'Credit remaining balance corroborated by an explicit CR amount and current-minus-total arithmetic.')
        # PLDT payment stubs repeat the recipient address after a standalone
        # 'Address' label. Do not accept account/balance columns interleaved by
        # OCR as part of the postal address.
        delivery=[]
        for i,label in enumerate(lines):
            if not re.fullmatch(r'(?i)Address\s*:?',label):
                continue
            for nxt in lines[i+1:i+5]:
                if re.search(r'(?i)^(?:Total Amount Due|Previous Charges|Current Charges|BIR|Date Issued|Account Number|Telephone|Due Date)',nxt):
                    break
                if re.search(r'(?i)\b(?:ST\.?|Street|Village|Homes|District|Camarin|Road|Brgy|City|Cavite|Ramon|Highway|Barangay)\b',nxt):
                    delivery.append(nxt.strip())
            if delivery:
                break
        if delivery:
            # One or more independently identified postal lines; preserve
            # them rather than concatenated stub payment amounts.
            cleaned_address=' '.join(delivery)
            previous=str(out.get('customer_address') or '')
            if len(cleaned_address)>=18 and (not previous or
                re.search(r'(?i)(?:Previous Charges|Current Charges|Total Amount Due|DATEISS|BILL SUMMARY)',previous)):
                set_field('customer_address',cleaned_address,
                    'Recipient address recovered from labeled payment-stub address, excluding bill-summary amounts.')
        # Stop subscriber delivery address at a separately printed bill-summary
        # section (OCR can concatenate the entire right column).
        buyer_addr=str(out.get('customer_address') or '')
        trimmed=re.split(r'(?i)\s*,?\s*BILL\s+SUMMARY\b',buyer_addr,maxsplit=1)[0].strip(' ,')
        if trimmed and trimmed!=buyer_addr:
            set_field('customer_address',trimmed,
                'Billing summary and charges removed from the recipient postal address.')
        # A PLDT seller postal address must not be copied from the subscriber.
        seller=str(out.get('vendor_address') or '').strip()
        buyer=str(out.get('customer_address') or '').strip()
        if seller and buyer and len(seller)>=3 and (seller.casefold() in buyer.casefold() or seller.lstrip('#').isdigit()):
            out['vendor_address']=None
            issue('Supplier address was taken from the subscriber location; seller address requires verification.')
        if out.get('invoice_number') and re.search(r'(?i)(?:PLDT|PHILIPPINE LONG DISTANCE TELEPHONE COMPANY)',text):
            identifier=str(out['invoice_number'])
            if not re.fullmatch(r'\d{8,14}',identifier):
                # PDFs sometimes have the account ID printed as a long mixed
                # alphanumeric form code instead of the SOA identifier.
                issue('Statement of Account number is not a clean printed numeric identifier; verify the separate SOA reference.')
        # Monetary VAT is not a billable line-item purchase.
    
    # Source statements can explicitly print a zero plan charge. A zero value
    # is still a valid row (do not drop it as a falsy/missing amount).
    if re.search(r'(?i)\bMonthly\s+(?:Plan|Recurring Fee)\b|\bMonthiy\s+Plan\b',text):
        plan_label = r'(?:Monthly|Monthiy)\s+Plan|Monthly\s+Recurring Fee(?:\s*\(MRF\))?'
        val = _single(_label_values(lines,plan_label,lookahead=2))
        if val is not None:
            items=[dict(it) for it in out.get('line_items') or []]
            exact=next((i for i,it in enumerate(items) if re.fullmatch(r'(?i)Monthly\s+Plan|Monthly\s+Recurring Fee(?:\s*\(MRF\))?',str(it.get('description') or '').strip())),None)
            if exact is None:
                description='Monthly Plan' if 'plan' in text.lower() and re.search(r'(?i)Monthly\s+Plan|Monthiy\s+Plan',text) else 'Monthly Recurring Fee (MRF)'
                items.insert(0,{'description':description,'quantity':1,'unit_price':val,'amount':val})
                out['line_items']=items
                notes.append('Explicit monthly fee included in line items, including printed zero amounts.')
            elif any(abs((to_float(items[exact].get(k)) or 0)-val)>.01 for k in ('unit_price','amount')):
                items[exact].update({'quantity':1,'unit_price':val,'amount':val})
                out['line_items']=items
                notes.append('Monthly fee line item corrected from the explicit printed service row.')

    # A Globe statement's separate current-bill adjustment is a deduction,
    # not VAT and not a second recurring fee. When the OCR loses the printed
    # adjustment row, recover its magnitude only from three independent
    # current-bill values and visibly flag the inferred row for confirmation.
    if re.search(r'(?i)\bGlobe\b',text) and re.search(r'(?i)Current Bill Charges',text):
        mrf = _single(_label_values(lines,r'Monthly Recurring Fee\s*\(?MRF\)?',lookahead=2))
        vat = to_float(out.get('tax_amount'))
        current = to_float(out.get('current_charges_total'))
        one_time = _single(_label_values(lines,r'One-time Charges',lookahead=2)) or 0.0
        if mrf is not None and vat is not None and current is not None:
            adjustment = round(mrf+one_time+vat-current,2)
            if .01<adjustment<current*.4:
                items=[dict(it) for it in out.get('line_items') or []]
                if not any(re.search(r'(?i)Adjustments?\s*-?\s*Current Bill',str(it.get('description') or '')) for it in items):
                    items.append({'description':'Adjustments - Current Bill','quantity':1,'unit_price':-adjustment,'amount':-adjustment})
                    out['line_items']=items
                    issue(f'Current-bill adjustment (-{adjustment:,.2f}) inferred from monthly fees, VAT and printed current bill; verify deduction against the source image.')
                    notes.append('Unresolved adjustment recovered by current-bill arithmetic; kept for verification.')

    # Where VAT/TAXES is a distinct financial summary, don't count it again
    # as a purchased item. Actual VAT and withholding still remain separate.
    if re.search(r'(?i)\b(?:STATEMENT OF ACCOUNT|BILL SUMMARY|CURRENT CHARGES|Statement Summary)\b',text):
        rows=[dict(it) for it in out.get('line_items') or []]
        kept=[r for r in rows if not re.fullmatch(r'(?i)(?:VAT|Value Added Ta(?:x|ll)|\+?TAXES)',str(r.get('description') or '').strip())]
        if len(kept)<len(rows):
            out['line_items']=kept
            notes.append('Tax summary rows excluded from purchased line items; tax fields retained.')

    # Branded statements identify the storefront separately from the legal
    # issuer; no recognition based on a filename or the recipient.
    if re.search(r'(?i)\bSUN\s+CELLULAR\b|\bSun\s+Cellular\s+Hotlines\b',text) and re.search(r'(?i)DIGITEL\s*MOBILE',text):
        set_field('vendor_name','SUN CELLULAR','Printed Sun Cellular trading brand selected; legal issuer retained in source text.')

    # A Sun Cellular summary prints the fee, usage, and credit on separate
    # lines. Use those rows only when they reproduce an independently printed
    # subtotal and then, with VAT, the independently printed current charges.
    if re.search(r'(?i)SUN\s+CELLULAR',text) and re.search(r'(?i)DIGITEL\s*MOBILE',text):
        def labelled_monetary(label_pattern):
            for i, label in enumerate(lines):
                if re.fullmatch(label_pattern,label,re.I):
                    for next_line in lines[i+1:i+3]:
                        value=_money(next_line)
                        if value is not None: return value
                        if re.search(r'[A-Za-z]{3,}',next_line): break
            return None
        plan=labelled_monetary(r'Mon(?:th|hy|hy)\s+S[ae]rvi(?:ce|oe)\s+Fee')
        usage=labelled_monetary(r'Usage\s+Charg(?:es|oe)')
        discount=labelled_monetary(r'Less\s*:\s*(?:D[iI]scounts?|Lscounts?)\s+and\s+Cr(?:edits|ads)')
        subtotal=labelled_monetary(r'Sub(?:total|lotal)')
        current=to_float(out.get('current_charges_total'))
        tax=to_float(out.get('tax_amount'))
        if all(v is not None for v in (plan,usage,discount,subtotal,current,tax)) and \
           abs(round(plan+usage+discount,2)-subtotal)<=.02 and abs(subtotal+tax-current)<=.02:
            set_field('subtotal',subtotal,'Vatable Sales recovered from separately printed service fee, usage, and credit.')
            new_rows=[{'description':'Monthly Service Fee','quantity':1,'unit_price':plan,'amount':plan},
                      {'description':'Usage Charges','quantity':1,'unit_price':usage,'amount':usage},
                      {'description':'Discounts and Credits','quantity':1,'unit_price':discount,'amount':discount}]
            if out.get('line_items')!=new_rows:
                out['line_items']=new_rows
                notes.append('Cellular current-charge rows reconciled against printed subtotal and VAT.')
        else:
            issue('Cellular line-item values could not be independently reconciled; verify service charges and discounts.')
        # When a label resembling Statement Date is not legible, the billing
        # period end is corroborating context but NOT proof of invoice date.
        if not re.search(r'(?i)Statement\s*Date\s*[:\n ]+\w',text):
            # If the date coincides with a clearly printed billing-period
            # START, it cannot safely substitute for Statement Date.
            starts=re.findall(r'(?i)\b(\d{1,2}[ -]?[A-Za-z]{3}[ -]?\d{4})\s*[-–]?\s*\d{1,2}[ -]?[A-Za-z]{3}[ -]?\d{4}\b',text)
            known_date=str(out.get('invoice_date') or '')
            if any((parse_date(x) and parse_date(x).isoformat()==known_date) for x in starts):
                out['invoice_date']=None
                notes.append('Billing-period start wrongly used as invoice date; cleared until source review.')
            issue('Cellular Statement Date label is unreadable; verify the invoice date against the original image.')

    # Cooperative legal name and common short name printed together. Do not
    # transfer the local office/meter location into vendor postal address.
    if re.search(r'(?i)PAMPANGA\s+II\s+ELECTRIC\s+COOPERATIVE',text) and re.search(r'(?i)PELCO\s*II',text):
        set_field('vendor_name','PELCO II (Pampanga II Electric Cooperative Inc.)',
                  'Legal cooperative name and printed PELCO II trading name preserved.')
        if not re.search(r'(?i)\b(?:Office Address|Company Address|Main Office)\b',text):
            if out.get('vendor_address'):
                out['vendor_address']=None
                issue('Supplier address is not explicitly stated; do not substitute the consumer service location.')

    # Globe bills sometimes lack an invoice-date field entirely. Billing
    # period and due date are not interchangeable with an invoice date.
    if re.search(r'(?i)\bGlobe\b',text) and re.search(r'(?i)Statement of Account',text):
        if not re.search(r'(?i)\b(?:Invoice|Statement|Bill|Issue)\s*Date\b',text):
            if out.get('invoice_date') is not None:
                out['invoice_date']=None
                issue('Invoice date not explicitly stated; billing period/due date cannot be substituted.')
        if not re.search(r'(?i)\bSOA\s*Number\b',text) and len(str(out.get('invoice_number') or ''))<=2:
            account_match=re.search(r'(?i)Account Number\s*\n\s*(\d{7,14})',text)
            if account_match:
                set_field('invoice_number',account_match[1],
                    'No dedicated statement number; account number used as provisional reference.')
                issue('Account number used as invoice reference because a distinct invoice identifier is not clearly printed; verify before accounting.')
        # Recover explicitly repeated recipient delivery-address lines.
        customer=str(out.get('customer_name') or '').strip()
        address=str(out.get('customer_address') or '').strip()
        if customer and (not address or len(address)<25):
            starts=[i for i,s in enumerate(lines) if s.casefold()==customer.casefold()]
            for idx in reversed(starts):
                candidates=[]
                for line in lines[idx+1:idx+9]:
                    if re.search(r'(?i)^PAYMENT\s+REMINDERS',line):continue
                    if re.search(r'(?i)^(?:TOTAL|Globe|Bill Number|Statement|Account|Previous|Current)',line):break
                    if re.search(r'(?i)\b(?:Street|St\.|Road|Rd\.|Purok|Silang|Cavite|Philippines|Metro Manila|Zamboanga|City|Barangay|Brgy)\b',line):
                        candidates.append(line)
                if len(candidates)>=2:
                    set_field('customer_address',' '.join(candidates),
                       'Customer delivery address recovered from repeated printed subscriber block.')
                    break

    # A number formatted like currency is not a valid statement identifier.
    # In a blurred cellular statement, a distinct upper-header number is safer
    # but still must be reviewed at character level.
    if re.search(r'(?i)SUN\s+CELLULAR',text) and re.fullmatch(r'\d+\.\d{2}',str(out.get('invoice_number') or '')):
        choices=[]
        for e in (ocr_lines or [])[:20]:
            t=str(e.get('text') or '').strip()
            pt=_poly_center(e)
            if re.fullmatch(r'\d{8,11}',t) and pt and pt[1]<120:
                choices.append(t)
        if len(set(choices))==1:
            set_field('invoice_number',choices[0],
                    'Small invoice/header identifier recovered from OCR; verify characters against the original image.')
            issue('Small cellular invoice number is low-resolution; verify every digit against the source.')

    # Short partial utility/telephone photos can contain a payable amount but
    # not enough identifiers to safely accept a complete invoice.
    if is_energy and len(lines)<35 and not re.search(r'(?i)\b(?:Bill Date|Billing\s*Statement|EB Invoice|Billing Invoice)\b',text):
        issue('Incomplete or low-resolution bill: identifiable totals alone are insufficient; verify image quality and invoice identity.')
    out['_normalization_issues']=issues
    return out,notes
