"""1.58 final-information confidence scoring.

Confidence now estimates support for the final reconciled result rather than
raw OCR quality alone. Successful reconciliation notes are not penalties.
"""
from __future__ import annotations
from config.constants import REQUIRED_FIELDS
from config.settings import settings
from models.prediction import Prediction, FieldConfidence
from utils.helpers import clamp

HANDWRITING_CONFIDENCE_PENALTY = 0.35

def _num(v):
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None

def _financial_consistency(d):
    sub=_num(d.get("subtotal")); total=_num(d.get("total_amount"))
    if sub is None or total is None: return None, None
    tax=_num(d.get("tax_amount")) or 0.0; zero=_num(d.get("zero_rated_sales")) or 0.0
    exempt=_num(d.get("vat_exempt_sales")) or 0.0; disc=_num(d.get("discount")) or 0.0
    current=_num(d.get("current_charges_total")); target=current if current is not None else total
    current_ok=abs((sub+tax+zero+exempt-disc)-target)<=max(0.05,abs(target)*0.002)
    prev=_num(d.get("previous_balance")); billing_ok=None
    if current is not None and prev is not None:
        billing_ok=abs((current+prev)-total)<=max(0.05,abs(total)*0.002)
    return current_ok,billing_ok

def _line_item_consistency(d):
    checked=ok=0
    for li in d.get("line_items") or []:
        desc=str((li or {}).get("description") or "").strip().lower()
        if desc in {"remaining balance","previous balance","carried balance","balance forward"}: continue
        q=_num((li or {}).get("quantity")); p=_num((li or {}).get("unit_price")); a=_num((li or {}).get("amount"))
        if q is None or p is None or a is None or q<=0 or p<0 or a<0: continue
        checked+=1
        if abs(q*p-a)<=max(0.05,abs(a)*0.01): ok+=1
    return None if not checked else ok==checked

def score_extraction(extracted: dict, ocr_avg_confidence: float, validation_issues: list[str], ocr_engine: str | None=None) -> Prediction:
    ocr=clamp(float(ocr_avg_confidence or 0.0)); issue_text=" ".join(str(x).lower() for x in validation_issues)
    financial_ok,billing_ok=_financial_consistency(extracted); items_ok=_line_item_consistency(extracted)
    fields=list(dict.fromkeys(REQUIRED_FIELDS+["vendor_tax_id","customer_name","customer_address","subtotal","tax_amount","discount","zero_rated_sales","vat_exempt_sales","current_charges_total","previous_balance"]))
    fcs=[]
    financial_fields={"subtotal","tax_amount","discount","zero_rated_sales","vat_exempt_sales","current_charges_total","previous_balance","total_amount"}
    for field in fields:
        value=extracted.get(field); present=value not in (None,"",[])
        if not present:
            fcs.append(FieldConfidence(field=field,value=value,confidence=0.0,source="missing")); continue
        conf=0.66+0.20*ocr; hit=field.lower() in issue_text
        if not hit: conf+=0.06
        if field in financial_fields:
            if financial_ok is True: conf+=0.07
            elif financial_ok is False: conf-=0.18
            if field in {"current_charges_total","previous_balance","total_amount"} and billing_ok is True: conf+=0.04
        if field in {"invoice_number","invoice_date","vendor_name","vendor_tax_id"} and not hit: conf+=0.03
        if hit: conf*=0.58
        fcs.append(FieldConfidence(field=field,value=value,confidence=round(clamp(conf),3),source="final_evidence"))
    required_present=sum(extracted.get(f) not in (None,"",[]) for f in REQUIRED_FIELDS)
    completeness=required_present/max(1,len(REQUIRED_FIELDS))
    overall=0.52+0.20*ocr+0.18*completeness
    if financial_ok is True: overall+=0.07
    elif financial_ok is False: overall-=0.14
    if billing_ok is True: overall+=0.03
    elif billing_ok is False: overall-=0.10
    if items_ok is True: overall+=0.03
    elif items_ok is False: overall-=0.06
    overall-=min(0.50,0.15*len(validation_issues))
    engine_tags=set((ocr_engine or "").split("+"))
    handwriting_used="trocr" in engine_tags
    # 1.58: TrOCR remains a review flag, but a mixed PaddleOCR+TrOCR run no
    # longer destroys the displayed correctness score for an otherwise
    # strongly reconciled printed invoice. Pure TrOCR remains more heavily
    # discounted. The review flag is preserved for either case.
    if handwriting_used:
        overall -= 0.25 if engine_tags == {"trocr"} else 0.08
    overall=clamp(overall)
    needs_review=overall<settings.CONFIDENCE_THRESHOLD or bool(validation_issues) or handwriting_used
    return Prediction(raw_json=extracted,field_confidences=fcs,overall_confidence=round(overall,3),needs_review=needs_review,warnings=validation_issues)
