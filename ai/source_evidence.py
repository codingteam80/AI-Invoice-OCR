"""Final source-layout evidence: keep columns and explicit zeroes intact.

No invoice filenames, invoice IDs or expected financial values are embedded here.
"""
import re
import statistics
from parser.currency_parser import to_float
from utils.date_utils import parse_date

NUM = re.compile(r'^(?:PHP|USD|P|\$|₱)?\s*\(?-?\d[\d, ]*(?:\.\d{1,2})?\)?\s*[.|]?$',re.I)

def number(text):
    s=str(text).strip().rstrip('|').strip()
    if s.endswith('.') and s.count('.')>1:s=s[:-1].strip()
    if not NUM.fullmatch(s):return None
    return to_float(s)

def geometry(lines):
    out=[]
    for line in lines or []:
        b=line.get('bbox')
        if not b or len(b)<4:continue
        xs=[p[0] for p in b];ys=[p[1] for p in b]
        out.append(dict(text=str(line.get('text','')).strip(),x=min(xs),r=max(xs),y=sum(ys)/len(ys),h=max(ys)-min(ys),b=b))
    return out

def is_unused_form(text):
    # Preprinted receipt numbers / printer accreditation dates are not sales.
    t=text.upper()
    if not ('RECEIVED FROM' in t and 'ADDRESS OF BUYER' in t and 'TOTAL RECEIPTS' in t):return False
    body=t.split('IN PAYMENT FOR THE FOLLOWING',1)[-1].split("SELLER",1)[0]
    return not re.search(r'\d',body) and not re.search(r'\d[\d,]*\.\d{2}',t)

def _row_value(label, cells, x_max=None, taken=None):
    # Estimate row slope from the label's own quadrilateral; this compensates
    # for photographed/skewed tables where the right column sits lower.
    b=label['b'];dx=b[1][0]-b[0][0]
    slope=(b[1][1]-b[0][1])/dx if dx else 0
    wide=[c for c in cells if c['r']-c['x']>max(x['r'] for x in cells)*.15]
    slopes=[(c['b'][1][1]-c['b'][0][1])/max(1,c['b'][1][0]-c['b'][0][0]) for c in wide]
    slope=statistics.median(slopes) if slopes else slope
    slope=max(-.15,min(.15,slope))
    candidates=[]
    for c in cells:
        if taken is not None and id(c) in taken:continue
        v=number(c['text']) if re.search(r'\.\d{1,2}',c['text']) else None
        if v is None or c['x']<=label['r'] or (x_max is not None and c['r']>x_max):continue
        delta=abs(c['y']-label['y']-slope*(c['x']-label['x']))
        if delta<max(label['h'],c['h'])*.55:candidates.append((delta,c,v))
    if not candidates:return None
    candidates.sort(key=lambda item:item[0])
    if taken is not None:taken.add(id(candidates[0][1]))
    return candidates[0][2]

def _table_items(cells):
    headers={}
    patterns={'description':r'(?:description|item summary)','quantity':r'qty\.?','unit_price':r'(?:unit price|rate)','amount':r'(?:amount|total)'}
    for key,pat in patterns.items():
        hits=[c for c in cells if re.fullmatch(pat,c['text'],re.I)]
        if hits:headers[key]=hits[0]
    if len(headers)!=4:return []
    desc,q,p,a=[headers[k] for k in patterns]
    if not(desc['x']<q['x']<p['x']<a['x']):return []
    centers=[(c['x']+c['r'])/2 for c in [desc,q,p,a]]
    slope=(a['y']-desc['y'])/max(1,centers[-1]-centers[0])
    norm=lambda c:c['y']-slope*((c['x']+c['r'])/2-centers[0])
    start=max(norm(c) for c in headers.values())+max(c['h'] for c in headers.values())*.6
    stop=min([norm(c) for c in cells if norm(c)>start and re.match(r'(?i)^(?:subtotal|invoice total|to pay by|total amount|vatable)',c['text'])] or [float('inf')])
    descriptions=[c for c in cells if start<norm(c)<stop and desc['x']-desc['h']<c['x']<(centers[0]+centers[1])/2 and re.search('[A-Za-z]',c['text']) and not re.fullmatch(r'[A-Z]+-\d+',c['text'])]
    rows=[]
    for d in sorted(descriptions,key=norm):
        values=[]
        for j in range(1,4):
            left=(centers[j-1]+centers[j])/2
            right=(centers[j]+centers[j+1])/2 if j<3 else float('inf')
            hits=[c for c in cells if left<=(c['x']+c['r'])/2<right and abs(norm(c)-norm(d))<max(c['h'],d['h'])*.85 and number(c['text']) is not None]
            if len(hits)!=1:break
            values.append(number(hits[0]['text']))
        if len(values)==3:
            rows.append(dict(description=d['text'],quantity=values[0],unit_price=values[1],amount=values[2]))
    # A partial table is not stronger evidence than the complete existing table.
    return rows if len(rows)==len(descriptions) else []

def reconcile_source_layout(data,text,lines,regions=None):
    out=dict(data);notes=[];cells=geometry(lines);regions=regions or {}
    def put(k,v):
        if out.get(k)!=v:notes.append('Source layout recovered '+k+'.')
        out[k]=v
    items=_table_items(cells)
    if items:
        put('line_items',items)
        out['_normalization_issues']=[s for s in out.get('_normalization_issues',[]) if not s.startswith('line_items row ')]
    # Retail table totals: explicitly printed subtotal has priority over an item.
    retail=bool(re.search(r'(?i)item\s*no\.',text) and re.search(r'(?i)unit\s*price',text))
    parking=bool(re.search(r'(?i)parking\s*fee',text))
    travel=bool(re.search(r'(?i)VATABLE SALES',text) and re.search(r'(?i)\bROE\b',text))
    if retail or parking or travel:
        patterns={'subtotal':r'(?:subtotal|VATable(?:\s+Sales)?)','tax_amount':r'(?:VAT(?:\s*\(12%\))?|VAT Amount\s*\(?12%\)?)','vat_exempt_sales':r'VAT[- ]Exempt Sales','zero_rated_sales':r'(?:Zero Rated(?: Sales)?|Zaro Rated Sales)'}
        taken=set() if travel else None
        for field,pat in patterns.items():
            for c in cells:
                if re.fullmatch(pat,c['text'],re.I):
                    limit=(max(x['r'] for x in cells)*.5 if travel else None)
                    value=_row_value(c,cells,limit,taken)
                    if value is not None or (travel and field in ('zero_rated_sales','vat_exempt_sales')):put(field,value)
                    break
        # Remove model-created billing balances for receipts, not statements.
        put('current_charges_total',None)
        if travel:
            put('discount',None)
        if retail and out.get('subtotal') is not None and out.get('total_amount') is not None:
            tax=out.get('tax_amount');sub=out['subtotal'];total=out['total_amount']
            # OCR correction needs TWO independent constraints: explicit 12% and
            # total-subtotal. Never repair only to make a sum look plausible.
            if re.search(r'VAT\s*\(12%\)',text,re.I) and abs(round(sub*.12,2)-(total-sub))<.02:
                expected=round(total-sub,2)
                if tax is not None and abs(tax-expected)>.02:
                    put('tax_amount',expected)
                    notes.append('VAT OCR digit corrected using printed 12% plus printed subtotal/total agreement.')
        if parking:
            out['_line_items_basis']='total_amount'
            tax=out.get('tax_amount');total=out.get('total_amount')
            if tax is not None and total is not None and re.search(r'(?i)VAT Amount.*12%',text):
                net=round(total-tax,2)
                if abs(round(net*.12,2)-tax)<.02 and out.get('subtotal') is not None:
                    put('subtotal',net)

    # Labelled TIN keeps the printed groups; only digit-context OCR confusions.
    for c in cells:
        m=re.search(r'VAT\s*REG\.?\s*TIN\s*:?\s*([0-9SOIl-]{10,})',c['text'],re.I)
        if m:
            put('vendor_tax_id',m[1].translate(str.maketrans({'S':'5','O':'0','I':'1','l':'1'})));break
    if retail:
        addr=str(out.get('customer_address') or '')
        put('customer_address',re.split(r'(?i)\bitem\s*no\.?',addr)[0].strip(' ,') or None)
    # Numbered utility statements: keep the full contiguous postal block.
    raw=[s.strip() for s in text.splitlines() if s.strip()]
    if re.search(r'EB Invoice\s+(?:No|Number)',text,re.I):
        for i,s in enumerate(raw):
            if re.search(r'BUS\.\s*CENTER',s,re.I):
                end=next((j for j in range(i+1,min(i+6,len(raw))) if re.match(r'(?i)(?:Tel|TIN)',raw[j])),i+1)
                put('vendor_address',' '.join(raw[i:end]))
                start=max(0,i-6)
                name=str(out.get('customer_name') or '').casefold().rstrip('.')
                for j in range(start,i):
                    if name and raw[j].casefold().rstrip('.')==name:
                        put('customer_address',' '.join(raw[j:i]));break
                break
    if 'Statement of Account No.' in text:
        for i,s in enumerate(raw):
            if re.match(r'^\*?\s*UNIT\b',s,re.I):
                end=next((j for j in range(i+1,min(i+8,len(raw))) if re.match(r'(?i)Previous Charges',raw[j])),i+1)
                put('customer_address',' '.join(raw[i:end]).lstrip('* '));break
    # Monthly-plan summaries have a price per plan, with discounts separate.
    if re.search(r'Monthly Recurring Fee',text,re.I):
        amounts={}
        for label in ['Monthly Plan','Add-ons','Discounts']:
            c=next((c for c in cells if c['text'].casefold()==label.casefold()),None)
            if c:
                v=_row_value(c,cells)
                if v is not None:amounts[label]=v
        if all(k in amounts for k in ['Monthly Plan','Add-ons','Discounts']):
            subtotal=round(amounts['Monthly Plan']+amounts['Add-ons'],2)
            discount=abs(amounts['Discounts']);current=out.get('current_charges_total')
            if current is not None and abs(subtotal-discount-current)<.02:
                put('subtotal',subtotal);put('discount',discount)
                put('line_items',[dict(description=k,quantity=1,unit_price=amounts[k],amount=amounts[k]) for k in ['Monthly Plan','Add-ons']])
                out['_normalization_issues']=[s for s in out.get('_normalization_issues',[]) if not s.startswith('line_items row ')]
    # Region OCR supplies buyer text missed by the whole-page detector.
    buyer=regions.get('buyer','')
    if buyer:
        match=re.search(r'(?is)ADDRESS\s*:?\s*(.*?)\s*TIN\s*(?:NO)?',buyer)
        if match:
            addr=' '.join(line.strip() for line in match[1].splitlines() if len(line.strip())>2).strip()
            before=buyer[:match.start()]
            unit=re.search(r'(?im)^Unit .+$',before)
            if unit:addr=unit[0]+' '+addr
            if re.search(r'(?i)(?:street|st\.?|ave\.?|floor|unit|centre|center)',addr):
                put('customer_address',addr)
                from ai.party_master import isolate_party_addresses
                repaired,_=isolate_party_addresses(out)
                put('customer_address',repaired.get('customer_address'))
    header=regions.get('header','')
    plate=re.search(r'(?i)PLATE\s*NO\.?\s*:?\s*([A-Z]{2,3}\s*\d{3,4})',header)
    if parking and plate:put('plate_number',plate[1].replace(' ',''))
    # Configured recurring parties are explicit master data, not inferred facts.
    from ai.party_master import load_master
    combined=(header+'\n'+text).casefold()
    for entry in load_master().get('source_verified_vendors',[]):
        if all(s.casefold() in combined for s in entry['evidence_keys']):
            put('vendor_address',entry['vendor_address'])
            notes.append('Vendor address supplied by reviewed party master: '+entry['label'])
    from ai.field_ownership import reconcile_owned_fields
    out, owned_notes = reconcile_owned_fields(out, text, lines, regions)
    return out, notes + owned_notes


def review_fallback(text,lines):
    """Retain recoverable OCR evidence after a model timeout, always for review."""
    if is_unused_form(text):return None
    if not re.search(r'(?i)invoice|receipt|billing',text) or not (re.search(r'\d[\d,]*\.\d{2}',text) or len([c for c in geometry(lines) if number(c['text']) is not None])>=3):return None
    data={'vendor_name':next((s.strip() for s in text.splitlines() if s.strip()),None),'line_items':[],
          '_normalization_issues':['Model extraction unavailable: OCR-only recovery requires manual review.']}
    m=re.search(r'(?i)invoice\s*(?:no\.?|number|#)\s*:?\s*([\w-]+)',text)
    if m and re.search(r'\d',m[1]):data['invoice_number']=m[1]
    m=re.search(r'(?i)(?:invoice\s*)?date\s*:?\s*([A-Za-z]{3,9}\s*\d{1,2},?\s*\d{4}|\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{4}|\d{1,2}/\d{1,2}/\d{2,4})',text)
    if m:
        d=parse_date(m[1]);data['invoice_date']=d.isoformat() if d else None
    data['currency']='PHP' if re.search(r'(?i)\bPHP\b|Philippines|\bVAT\b',text) else None
    data,_=reconcile_source_layout(data,text,lines)
    return data
