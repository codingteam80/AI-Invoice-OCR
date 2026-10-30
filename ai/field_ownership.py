"""Label-owned field recovery independent of model and batch state."""
import re


def _compact(value):
    return re.sub(r'[^a-z0-9]','',str(value or '').casefold())


def reconcile_owned_fields(data,text,lines,regions=None):
    from ai.source_evidence import geometry, number
    from ai.party_master import isolate_party_addresses, load_master
    out=dict(data);notes=[];cells=geometry(lines);regions=regions or {}
    def put(field,value):
        if value is not None and value!='' and out.get(field)!=value:
            out[field]=value;notes.append('Label-owned evidence recovered '+field+'.')
    # A bare No. near a SERVICE INVOICE heading is not a printer permit number.
    headings=[c for c in cells if re.fullmatch(r'(?:(?:SERVICE|SALES)\s+)?INVOICE',c['text'],re.I)]
    for heading in headings:
        labels=[c for c in cells if re.fullmatch(r'N[Ooº°]\.?\s*:?\s*',c['text'],re.I) and 0<c['y']-heading['y']<heading['h']*4 and abs(c['x']-heading['x'])<heading['h']*6]
        for label in labels:
            ids=[c for c in cells if re.fullmatch(r'\d{3,12}',c['text']) and c['x']>label['r'] and abs(c['y']-label['y'])<max(c['h'],label['h'])]
            if len(ids)==1:put('invoice_number',ids[0]['text'])
    # Customer TIN uses its own labeled block, even if the model omitted the buyer.
    for label in cells:
        if re.fullmatch(r'Customer\s*TIN\s*:?\s*',label['text'],re.I):
            candidates=[c for c in cells if re.fullmatch(r'\d{3}-\d{3}-\d{3}-\d{3,5}',c['text']) and 0<c['y']-label['y']<label['h']*2.2 and abs(c['x']-label['x'])<label['h']*2]
            if len(candidates)==1:put('customer_tax_id',candidates[0]['text'])
    # Two-column telecom bill: account metadata on the right, recipient block left.
    if re.search(r'Monthly Recurring Fee',text,re.I):
        anchors=[c for c in cells if re.fullmatch(r'Corporate ID',c['text'],re.I)]
        if anchors:
            a=anchors[0]
            block=[c for c in cells if c['r']<a['x'] and a['y']-a['h']*2<c['y']<a['y']+a['h']*7]
            names=[c for c in block if re.search(r'(?i)\b(?:inc\.?|corp\.?|corporation|company|colleges)\s*$',c['text'])]
            if names:
                name=min(names,key=lambda c:c['y']);put('customer_name',name['text'])
                rest=sorted([c for c in block if c['y']>name['y']+name['h']*.5],key=lambda c:c['y'])
                start=next((i for i,c in enumerate(rest) if re.search(r'(?i)\d.*(?:floor|center|centre|st\b)|street|\bunit\b|\batega',c['text'])),None)
                if start is not None:
                    address=[]
                    for c in rest[start:]:
                        if re.search(r'(?i)\bplan\b|\bMbps\b|statement|charges',c['text']) or len(c['text'].strip())<3:break
                        address.append(c['text'].strip())
                        if re.search(r',\s*\d{4}$',c['text']):break
                    put('customer_address',' '.join(address))
    # Crop OCR may put the long first address line BEFORE ADDRESS and may read
    # Unit as Unt. Locate the postal span, not one exact spelling or label order.
    buyer=regions.get('buyer','')
    if buyer:
        rows=[s.strip() for s in buyer.splitlines() if s.strip()]
        first=next((i for i,s in enumerate(rows) if re.search(r'(?i)\b(?:un[il]?t|unt)\s*\d+|\d+\w*\s*(?:floor|fl[oi]or)',s)),None)
        if first is not None:
            last=next((i for i in range(first+1,len(rows)) if re.match(r'(?i)^TIN\s*(?:NO)?\s*$',rows[i])),len(rows))
            parts=[s for s in rows[first:last] if not re.fullmatch(r'(?i)(?:ADDRESS\s*:|PO|P|BILLED TO\s*:)',s)]
            put('customer_address',' '.join(parts))
            repaired,_=isolate_party_addresses(out)
            put('customer_address',repaired.get('customer_address'))
    # A foreign-unit-cost table with ROE has local-currency line totals. The VAT
    # row is a separate tax entry; it must not become the ticket's unit price.
    if re.search(r'(?im)^\s*ROE\s*$',text) and re.search(r'(?im)^\s*VATABLE SALES\s*$',text):
        rows=[]
        for c in cells:
            if re.fullmatch(r'SERVICE FEE\s*\(TICKET\)',c['text'],re.I):
                # Rightmost numeric cell on the same row is the local total.
                right=[x for x in cells if x['x']>max(z['r'] for z in cells)*.75 and number(x['text']) is not None]
                value=None
                qtys=[x for x in cells if re.fullmatch(r'\d+(?:\.\d+)?',x['text']) and max(z['r'] for z in cells)*.63<x['x']<max(z['r'] for z in cells)*.78 and abs(x['y']-c['y'])<c['h']*2]
                if qtys:
                    q=min(qtys,key=lambda x:abs(x['y']-c['y']))
                    aligned=[x for x in right if abs(x['y']-q['y'])<max(x['h'],q['h'])*.6]
                    if len(aligned)==1:value=number(aligned[0]['text'])
                if value is not None:
                    qty=number(q['text'])
                    if qty and qty>0:rows.append({'description':c['text'],'quantity':qty,'unit_price':round(value/qty,2),'amount':value})
        subtotal=out.get('subtotal')
        if rows and subtotal is not None and abs(sum(r['amount'] for r in rows)-subtotal)<.02:
            put('line_items',rows)
            out['_normalization_issues']=[x for x in out.get('_normalization_issues',[]) if not x.startswith('line_items row ')]
            put('currency','PHP') if re.search(r'\bPHP\b',text,re.I) else None
    # Source currency beats model guesses. Country is a fallback only; printed
    # foreign currency always wins, except an explicit ROE/local-total table.
    foreign=set(re.findall(r'\b(USD|EUR|GBP|JPY|AUD|CAD|SGD|CNY)\b',text.upper()))
    if '$' in text and not foreign:foreign.add('USD')
    local=bool(re.search(r'\bPHP\b|₱|(?<![A-Za-z])P\s*(?=\d[\d,]*(?:\.\d{2})?\b)',text,re.I))
    if not foreign and (local or re.search(r'\bPhilippines\b',text,re.I)):put('currency','PHP')
    elif len(foreign)==1 and not local:put('currency',next(iter(foreign)))
    # Reviewed identities require multiple source cues. They never match merely
    # because the model guessed a similar vendor name.
    compact=_compact(text)
    for entry in load_master().get('source_verified_identities',[]):
        if (all(_compact(k) in compact for k in entry['evidence_keys'])
                and not any(_compact(k) in compact for k in entry.get('exclude_evidence_keys', []))):
            put('vendor_name',entry['vendor_name'])
            if not foreign:put('currency',entry.get('currency'))
            notes.append('Reviewed issuer identity applied: '+entry['vendor_name'])
    from ai.document_evidence import reconcile_document_evidence
    out, final_notes = reconcile_document_evidence(out, text, lines)
    return out, notes + final_notes
