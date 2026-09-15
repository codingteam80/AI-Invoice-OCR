"""Independent image-based verification using a local vision-capable Ollama model.

The verifier is fail-open: an unavailable/misconfigured vision model never blocks
invoice processing. This version records a detailed trace and falls back from
Ollama /api/generate to /api/chat when a multimodal generate request is rejected.
"""
from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
import requests
import cv2

from config.logging import get_logger
from config.settings import settings
from ai.prompt_builder import build_vision_verification_prompt
from ai.invoice_templates import get_template
from utils.invoice_template_detector import detect_invoice_template
from parser.currency_parser import to_float, normalize_discount
from utils.helpers import safe_json_loads, normalize_tin, is_valid_tin
from utils.pdf_utils import pdf_to_images, is_pdf

logger = get_logger("ai.vision_verifier")

# Generic verification now covers all identity + financial fields that commonly
# caused real errors in the 1.47 sample batch, not just a small subset.
VISION_CHECK_FIELDS = [
    "invoice_number", "invoice_date",
    "vendor_name", "vendor_address", "vendor_tax_id",
    "customer_name", "customer_address", "customer_tax_id",
    "plate_number",
    "subtotal", "tax_amount", "discount",
    "zero_rated_sales", "vat_exempt_sales", "total_amount", "currency",
]
WATSONS_VISION_CHECK_FIELDS = VISION_CHECK_FIELDS

_MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount", "discount", "zero_rated_sales", "vat_exempt_sales"}


def _image_to_base64(file_path: str) -> str | None:
    try:
        path = file_path
        if is_pdf(file_path):
            pages = pdf_to_images(file_path)
            if not pages:
                return None
            path = pages[0]
        return base64.b64encode(Path(path).read_bytes()).decode("utf-8")
    except Exception as e:
        logger.warning(f"Could not read {file_path} for vision verification: {e}")
        return None


def _usage(body: dict) -> dict:
    return {
        "prompt_eval_count": body.get("prompt_eval_count"),
        "eval_count": body.get("eval_count"),
        "total_duration": body.get("total_duration"),
        "load_duration": body.get("load_duration"),
        "prompt_eval_duration": body.get("prompt_eval_duration"),
        "eval_duration": body.get("eval_duration"),
    }


def _post_generate(prompt: str, image_b64: str, trace: dict) -> str | None:
    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": settings.LLM_TEMPERATURE, "num_ctx": settings.VISION_NUM_CTX},
    }
    attempt = {"endpoint": "/api/generate"}
    trace.setdefault("http_attempts", []).append(attempt)
    resp = requests.post(url, json=payload, timeout=settings.VISION_TIMEOUT_SECONDS)
    attempt["http_status"] = resp.status_code
    if not resp.ok:
        attempt["response_body"] = resp.text[:4000]
        # A 400 here is commonly an Ollama/model multimodal endpoint mismatch;
        # caller will try /api/chat using the same model/image.
        return None
    body = resp.json()
    attempt["usage"] = _usage(body)
    raw = body.get("response", "")
    attempt["raw_response"] = raw
    return raw


def _post_chat(prompt: str, image_b64: str, trace: dict) -> str | None:
    url = f"{settings.OLLAMA_HOST}/api/chat"
    payload = {
        "model": settings.VISION_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        "stream": False,
        "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": settings.LLM_TEMPERATURE, "num_ctx": settings.VISION_NUM_CTX},
    }
    attempt = {"endpoint": "/api/chat"}
    trace.setdefault("http_attempts", []).append(attempt)
    resp = requests.post(url, json=payload, timeout=settings.VISION_TIMEOUT_SECONDS)
    attempt["http_status"] = resp.status_code
    if not resp.ok:
        attempt["response_body"] = resp.text[:4000]
        return None
    body = resp.json()
    attempt["usage"] = _usage(body)
    raw = (body.get("message") or {}).get("content", "")
    attempt["raw_response"] = raw
    return raw



def _vendor_header_field_needs_recovery(extracted: dict) -> tuple[bool, bool]:
    """Return (need_address, need_tin) for the generic header second pass."""
    address = str(extracted.get("vendor_address") or "").strip()
    tin = normalize_tin(extracted.get("vendor_tax_id"))
    # A bare account/client number is not an address. Require some alphabetic
    # content and a minimally plausible length. 1.59 also retries addresses
    # that clearly look truncated: a block beginning only at postal-city level
    # (e.g. "1209 City of Makati...") or only at barangay level is usually the
    # tail of a longer seller address whose building/street lines were missed.
    truncated_tail = bool(
        re.match(r"(?i)^\s*\d{4}\s+city\b", address)
        or (re.match(r"(?i)^\s*barangay\b", address) and not re.search(r"(?i)\b(?:street|st\.?|avenue|ave\.?|road|tower|bldg|building|outlet|supermarket|corner|cor\.?)\b", address))
    )
    need_address = (not address or len(address) < 12 or not re.search(r"[A-Za-z]", address) or truncated_tail)
    need_tin = not is_valid_tin(tin)
    return need_address, need_tin


def _extract_labeled_tin_from_header_text(text: str) -> str | None:
    """Read only a TIN attached to an explicit seller TIN/VAT label.

    This deliberately does not accept a free-standing 9/12-digit number, so
    account/client/barcode values cannot masquerade as vendor_tax_id.
    """
    if not text:
        return None
    compact = re.sub(r"[ \t]+", " ", text)
    pat = re.compile(
        r"(?:VAT\s*(?:Reg(?:istered)?\.?\s*)?TIN|Tax\s*Identification\s*No\.?|\bTIN\b)"
        r"\s*[:#.-]*\s*(?:\n\s*)?([0-9][0-9 -]{7,20}[0-9])",
        re.IGNORECASE,
    )
    for m in pat.finditer(compact):
        candidate = normalize_tin(m.group(1).strip())
        if is_valid_tin(candidate):
            return candidate
    return None


_HEADER_ADDRESS_CUE_RE = re.compile(
    r"\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|bldg|building|tower|"
    r"floor|city|philippines|barangay|brgy\.?|corner|cor\.?|loop|park|district|ncr|"
    r"taguig|cebu|makati|pasig|mandaluyong|quezon)\b", re.IGNORECASE
)

def _address_from_header_ocr(text: str) -> str | None:
    if not text:
        return None
    out=[]
    seen=set()
    for raw in text.splitlines():
        line=re.sub(r"\s+", " ", raw).strip(" ,;|")
        low=line.lower()
        if len(line) < 8:
            continue
        if any(x in low for x in ("invoice", "bill to", "billed to", "customer", "account number", "client no", "vat reg", " tin", "tel.", "email", "www.")):
            continue
        if _HEADER_ADDRESS_CUE_RE.search(line) and line.lower() not in seen:
            out.append(line); seen.add(line.lower())
    return ", ".join(out) if out else None


def _write_vendor_header_variants(image_path: str) -> list[str]:
    img=cv2.imread(image_path)
    if img is None:
        return []
    h,w=img.shape[:2]
    ratio=max(0.12,min(0.45,float(settings.VENDOR_HEADER_CROP_RATIO)))
    crop=img[:max(1,int(h*ratio)), :]
    scale=max(1.0,min(5.0,float(settings.VENDOR_HEADER_UPSCALE)))
    crop=cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray=cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe=cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8,8)).apply(gray)
    variants=[crop, cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR), cv2.cvtColor(255-clahe, cv2.COLOR_GRAY2BGR)]
    paths=[]
    for i,var in enumerate(variants):
        out=Path(settings.TEMP_DIR)/f"vendor_header_{uuid.uuid4().hex}_{i}.png"
        cv2.imwrite(str(out), var); paths.append(str(out))
    return paths


def recover_vendor_header_fields(
    image_path: str, extracted: dict, trace: dict | None = None
) -> tuple[dict, list[str], set[str]]:
    """Generic second pass for tiny seller address/TIN text in the page header.

    Triggered only when the normal extraction left vendor_address/vendor_tax_id
    missing or suspicious. It upscales the top of the invoice, tries multiple
    contrast polarities with PaddleOCR, then (when enabled) asks the vision LLM
    a very small, seller-header-only question. Explicit OCR ``VAT Reg. TIN``
    evidence outranks vision.
    """
    if trace is None:
        trace={}
    need_address, need_tin = _vendor_header_field_needs_recovery(extracted)
    trace.update({"enabled": settings.VENDOR_HEADER_RECOVERY_ENABLED, "need_address": need_address, "need_tin": need_tin})
    if not settings.VENDOR_HEADER_RECOVERY_ENABLED or not (need_address or need_tin):
        trace["skipped_reason"] = "fields already plausible" if not (need_address or need_tin) else "disabled"
        return {}, [], set()

    variants=_write_vendor_header_variants(image_path)
    trace["crop_variants"] = variants
    if not variants:
        trace["error"]="could not create header crop"
        return {}, [], set()

    ocr_attempts=[]; best_text=""; best_score=-1.0
    try:
        from ocr import paddleocr_engine
        for vp in variants:
            try:
                r=paddleocr_engine.extract_text(vp)
                text=str(r.get("text") or "")
                score=len(re.sub(r"\s+", "", text)) + (150 if re.search(r"(?i)VAT\s*.*TIN|\bTIN\b", text) else 0)
                ocr_attempts.append({"path":vp,"text":text,"avg_confidence":r.get("avg_confidence"),"lines":r.get("lines",[])})
                if score>best_score:
                    best_score=score; best_text=text
            except Exception as e:
                ocr_attempts.append({"path":vp,"error":str(e)})
    except Exception as e:
        trace["ocr_error"]=str(e)
    trace["ocr_attempts"]=ocr_attempts
    trace["best_ocr_text"]=best_text

    recovered={}; notes=[]; locked=set()
    explicit_tin=_extract_labeled_tin_from_header_text(best_text)
    if explicit_tin:
        current_tin=normalize_tin(extracted.get("vendor_tax_id"))
        recovered["vendor_tax_id"]=explicit_tin; locked.add("vendor_tax_id")
        if current_tin and current_tin != explicit_tin:
            notes.append(
                f"Vendor header recovery: replaced vendor_tax_id={current_tin} with {explicit_tin}; "
                "the high-resolution header contains an explicit seller TIN/VAT label, which outranks an unlabeled account/client number."
            )
        else:
            notes.append(f"Vendor header recovery: vendor_tax_id={explicit_tin} read from an explicit TIN/VAT label in the high-resolution header OCR pass.")
    ocr_address=_address_from_header_ocr(best_text) if need_address else None

    # Keep the vision request tiny: it sees only the high-resolution header,
    # not the whole invoice, and is explicitly forbidden from using account or
    # client numbers as TINs.
    if settings.VISION_VERIFICATION_ENABLED:
        prompt=(
            'Read ONLY the seller/vendor header in this cropped invoice image. Return strict JSON: '
            '{"vendor_address": string|null, "vendor_tax_id": string|null}. '
            'vendor_tax_id must come from an explicit seller label such as "VAT Reg. TIN" or "TIN"; '
            'never use Account Number, Client No., Service ID, invoice number, barcode/control number, or customer TIN. '
            'vendor_address must include ALL consecutive seller location lines in the header (outlet/store, building/floor, street, barangay, city/district/country) but must NOT repeat the seller company name. '
            'Do not return Bill To/customer, bank/remittance, or printer/footer address. If uncertain return null.'
        )
        vtrace={}; raw=None
        try:
            b64=_image_to_base64(variants[1] if len(variants)>1 else variants[0])
            if b64:
                raw=_post_generate(prompt,b64,vtrace)
                if raw is None:
                    raw=_post_chat(prompt,b64,vtrace)
        except Exception as e:
            vtrace["error"]=str(e)
        vtrace["raw_response"]=raw
        parsed=safe_json_loads(raw) if raw else None
        vtrace["parsed_json"]=parsed
        trace["vision_header"]=vtrace
        if isinstance(parsed,dict):
            if "vendor_tax_id" not in recovered and need_tin:
                vt=normalize_tin(str(parsed.get("vendor_tax_id") or "").strip())
                if is_valid_tin(vt):
                    recovered["vendor_tax_id"]=vt; locked.add("vendor_tax_id")
                    notes.append(f"Vendor header recovery: vendor_tax_id={vt} recovered from the high-resolution header vision pass.")
            if need_address:
                va=re.sub(r"\s+", " ", str(parsed.get("vendor_address") or "")).strip(" ,;:")
                if len(va)>=12 and re.search(r"[A-Za-z]",va):
                    recovered["vendor_address"]=va; locked.add("vendor_address")
                    notes.append("Vendor header recovery: vendor_address recovered from the high-resolution header vision pass.")

    if need_address and "vendor_address" not in recovered and ocr_address:
        recovered["vendor_address"]=ocr_address; locked.add("vendor_address")
        notes.append("Vendor header recovery: vendor_address recovered from address-like lines in the high-resolution header OCR pass.")

    trace["recovered_fields"]=recovered
    trace["locked_fields"]=sorted(locked)
    for vp in variants:
        try: Path(vp).unlink(missing_ok=True)
        except OSError: pass
    return recovered, notes, locked



def _is_true_invoice_date_label(text: str) -> bool:
    t=re.sub(r'\s+',' ',str(text or '')).strip()
    if not re.search(r'(?i)\bdate\b',t):
        return False
    # Regulatory/administrative dates must never own the invoice-date crop.
    if re.search(r'(?i)due\s+date|date\s+issued|issue\s+date|date\s+of\s+atp|accreditation\s+date|expiration\s+date|expiry\s+date|permit.*date|valid\s+until',t):
        return False
    return bool(re.fullmatch(r'(?i)\s*(?:invoice\s+)?date\s*:?\s*.*',t))


def _write_date_crop(image_path: str, ocr_lines: list[dict]) -> str | None:
    """Crop only the explicit invoice Date field and its adjacent value.

    1.57 fixes a failure where ``Date of ATP`` in a printer footer became the
    crop anchor when the real ``Date:`` label and handwritten value were OCR'd
    as separate boxes.
    """
    img=cv2.imread(image_path)
    if img is None:
        return None
    targets=[]
    for l in ocr_lines or []:
        t=str(l.get('text') or '').strip()
        if _is_true_invoice_date_label(t):
            targets.append(l)
    if not targets:
        return None
    # Prefer the shortest literal label (``Date:``) when present; otherwise
    # use ``Invoice Date``. Both are safer than any unrelated date phrase.
    target=sorted(targets,key=lambda l:(0 if re.fullmatch(r'(?i)\s*date\s*:?\s*',str(l.get('text') or '')) else 1, len(str(l.get('text') or ''))))[0]
    try:
        box=target.get('bbox'); xs=[int(float(p[0])) for p in box]; ys=[int(float(p[1])) for p in box]
    except Exception:
        return None
    h,w=img.shape[:2]
    # Extend strongly to the right so a separate handwritten value box on the
    # same row is included, but keep the crop vertically narrow enough to avoid
    # footer/ATP dates elsewhere on the page.
    x1=max(0,min(xs)-60); x2=min(w,max(xs)+520)
    y1=max(0,min(ys)-55); y2=min(h,max(ys)+75)
    crop=img[y1:y2,x1:x2]
    if crop.size==0:
        return None
    crop=cv2.resize(crop,None,fx=3.0,fy=3.0,interpolation=cv2.INTER_CUBIC)
    out=Path(settings.TEMP_DIR)/f"invoice_date_crop_{uuid.uuid4().hex}.png"
    cv2.imwrite(str(out),crop)
    return str(out)


def verify_handwritten_invoice_date(
    image_path: str, ocr_lines: list[dict] | None, trace: dict | None = None
) -> tuple[str | None, list[str]]:
    """Dedicated high-resolution vision pass for a handwritten/garbled Date row.

    Trigger only when the explicit Date OCR row contains alphabetic/garbled
    content rather than a clean machine-readable numeric date.  The prompt is
    intentionally independent: it does not reveal the currently extracted date,
    reducing anchoring on an earlier month misread.
    """
    if trace is None: trace={}
    trace['enabled']=bool(getattr(settings,'HANDWRITTEN_DATE_VERIFY_ENABLED',True))
    if not trace['enabled'] or not settings.VISION_VERIFICATION_ENABLED:
        trace['skipped_reason']='disabled'
        return None,[]
    # 1.56: only the actual invoice/transaction Date row may suppress the
    # dedicated crop. Regulatory dates such as Date of ATP, Accreditation
    # Date, Expiration Date, permit dates, and issue dates are unrelated.
    date_lines=[]
    for l in ocr_lines or []:
        t=str(l.get('text') or '').strip()
        if _is_true_invoice_date_label(t):
            date_lines.append(t)
    if not date_lines:
        trace['skipped_reason']='no explicit invoice Date row in OCR'
        return None,[]
    raw=' | '.join(date_lines[:3])
    trace['ocr_date_rows']=date_lines[:3]
    # A clean date suppresses vision only when it appears on the same actual
    # invoice Date row, not elsewhere in the document.
    if any(re.search(r'\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b',x) or re.search(r'\b\d{1,2}-[A-Za-z]{3,9}-\d{2,4}\b',x) for x in date_lines[:3]):
        trace['skipped_reason']='invoice date row already clean/readable'
        return None,[]
    # Require signs that OCR is trying to read a handwritten/word-month date.
    if not re.search(r'[A-Za-z]{2,}',raw):
        trace['skipped_reason']='no handwritten/word-month signal'
        return None,[]
    crop=_write_date_crop(image_path,ocr_lines or [])
    if not crop:
        trace['skipped_reason']='could not create date crop'
        return None,[]
    trace['crop_path']=crop
    try:
        b64=base64.b64encode(Path(crop).read_bytes()).decode('utf-8')
        prompt=(
            'Read ONLY the invoice date visible in this cropped image. The date may be handwritten. '
            'Pay special attention to the MONTH word/abbreviation and distinguish similar handwriting such as May vs Feb. '
            'Do not infer or guess from any previous extraction. Return strict JSON only: '
            '{"invoice_date":"<exact date as seen>"} or {"invoice_date":null} if unreadable.'
        )
        local_trace={'http_attempts':[]}
        raw_resp=_post_generate(prompt,b64,local_trace)
        if raw_resp is None:
            raw_resp=_post_chat(prompt,b64,local_trace)
        trace['http_attempts']=local_trace.get('http_attempts',[])
        trace['raw_response']=raw_resp
        parsed=safe_json_loads(raw_resp or '')
        trace['parsed_json']=parsed
        if not isinstance(parsed,dict) or not parsed.get('invoice_date'):
            return None,[]
        from utils.date_utils import to_iso_with_context
        context='\n'.join(str(x.get('text') or '') for x in (ocr_lines or []))
        iso=to_iso_with_context(str(parsed['invoice_date']), context)
        trace['normalized_date']=iso
        if not iso:
            return None,[]
        return iso,[f"Dedicated handwritten-date crop read invoice_date as {iso}."]
    except Exception as e:
        trace['error']=str(e)
        logger.warning(f"Handwritten date verification failed open: {e}")
        return None,[]
    finally:
        try: Path(crop).unlink(missing_ok=True)
        except OSError: pass


def _bbox_rect(line: dict) -> tuple[int,int,int,int] | None:
    try:
        pts=line.get("bbox") or []
        xs=[int(float(p[0])) for p in pts]; ys=[int(float(p[1])) for p in pts]
        return min(xs),min(ys),max(xs),max(ys)
    except Exception:
        return None


def verify_statement_summary_crop(image_path: str, ocr_lines: list[dict] | None, trace: dict | None = None) -> tuple[dict, list[str]]:
    """Dedicated crop read for a Statement Summary block.

    It is intentionally narrow and returns only row-level billing values. A
    caller should treat it as supporting evidence; geometry remains preferred
    when both are coherent.
    """
    if trace is None: trace={}
    enabled=bool(getattr(settings,'STATEMENT_SUMMARY_VERIFY_ENABLED',True))
    trace['enabled']=enabled
    if not enabled or not settings.VISION_VERIFICATION_ENABLED:
        trace['skipped_reason']='disabled'
        return {},[]
    lines=ocr_lines or []
    anchor=next((l for l in lines if 'statement summary' in str(l.get('text') or '').lower()),None)
    total=next((l for l in lines if str(l.get('text') or '').strip().lower()=='total' and anchor and _bbox_rect(l) and _bbox_rect(anchor) and _bbox_rect(l)[1]>_bbox_rect(anchor)[1]),None)
    if not anchor or not total:
        trace['skipped_reason']='statement summary/total row not found'
        return {},[]
    img=cv2.imread(image_path)
    if img is None:
        trace['skipped_reason']='image unreadable'
        return {},[]
    a=_bbox_rect(anchor); t=_bbox_rect(total)
    if not a or not t:return {},[]
    h,w=img.shape[:2]
    x1=max(0,a[0]-50); x2=min(w,max(a[2]+500,t[2]+500,w-20))
    y1=max(0,a[1]-35); y2=min(h,t[3]+90)
    crop=img[y1:y2,x1:x2]
    if crop.size==0:return {},[]
    crop=cv2.resize(crop,None,fx=2.5,fy=2.5,interpolation=cv2.INTER_CUBIC)
    out=Path(settings.TEMP_DIR)/f"statement_summary_crop_{uuid.uuid4().hex}.png"
    cv2.imwrite(str(out),crop); trace['crop_path']=str(out)
    try:
        b64=base64.b64encode(out.read_bytes()).decode('utf-8')
        prompt=(
            'Read ONLY this Statement Summary. Return strict JSON only with numeric values or null: '
            '{"monthly_plan":null,"add_ons":null,"discounts":null,"current_charges_total":null}. '
            'Monthly Plan and Add-ons are positive current charges. Discounts is a positive deduction even if printed in parentheses. '
            'current_charges_total is the printed Total of this Statement Summary. Do not use Previous Bill, Remaining Balance, Payment, Due Date, or Amount to Pay.'
        )
        local={'http_attempts':[]}
        raw=_post_generate(prompt,b64,local)
        if raw is None: raw=_post_chat(prompt,b64,local)
        trace['http_attempts']=local.get('http_attempts',[]); trace['raw_response']=raw
        parsed=safe_json_loads(raw or ''); trace['parsed_json']=parsed
        if not isinstance(parsed,dict):return {},[]
        vals={}
        for k in ('monthly_plan','add_ons','discounts','current_charges_total'):
            v=to_float(parsed.get(k))
            if v is not None: vals[k]=round(abs(float(v)),2)
        if {'monthly_plan','current_charges_total'} <= set(vals):
            addons=vals.get('add_ons',0.0); disc=vals.get('discounts')
            derived=round(vals['monthly_plan']+addons-vals['current_charges_total'],2)
            if derived>=-0.01 and (disc is None or abs(disc-derived)>0.01): vals['discounts']=max(0.0,derived)
        trace['normalized']=vals
        return vals,["Dedicated Statement Summary crop produced row-level billing evidence."] if vals else []
    except Exception as e:
        trace['error']=str(e); return {},[]
    finally:
        try: out.unlink(missing_ok=True)
        except OSError: pass


def verify_customer_address_crop(image_path: str, ocr_lines: list[dict] | None, trace: dict | None = None) -> tuple[str | None, list[str]]:
    """Read a customer ADDRESS block from a focused crop when whole-page OCR is sparse."""
    if trace is None: trace={}
    enabled=bool(getattr(settings,'CUSTOMER_ADDRESS_VERIFY_ENABLED',True))
    trace['enabled']=enabled
    if not enabled or not settings.VISION_VERIFICATION_ENABLED:
        trace['skipped_reason']='disabled'; return None,[]
    lines=ocr_lines or []
    label=next((l for l in lines if re.fullmatch(r'(?i)\s*(?:business\s+)?address\s*:?\s*',str(l.get('text') or ''))),None)
    if not label:
        trace['skipped_reason']='ADDRESS label not found'; return None,[]
    b=_bbox_rect(label); img=cv2.imread(image_path)
    if not b or img is None:return None,[]
    h,w=img.shape[:2]
    # Main customer column only: deliberately exclude right-hand Terms/PO Ref.
    x1=max(0,b[0]-40); x2=min(w,int(w*0.70)); y1=max(0,b[1]-90); y2=min(h,b[3]+180)
    if x2<=x1:return None,[]
    crop=img[y1:y2,x1:x2]
    if crop.size==0:return None,[]
    crop=cv2.resize(crop,None,fx=2.5,fy=2.5,interpolation=cv2.INTER_CUBIC)
    out=Path(settings.TEMP_DIR)/f"customer_address_crop_{uuid.uuid4().hex}.png"
    cv2.imwrite(str(out),crop); trace['crop_path']=str(out)
    try:
        b64=base64.b64encode(out.read_bytes()).decode('utf-8')
        prompt=(
            'Read ONLY the customer/billed-to ADDRESS value in this crop. Return strict JSON only: {"customer_address":string|null}. '
            'Do not include the ADDRESS label itself, PO Ref No., Terms, TIN, Attention, telephone, fax, or other neighboring field labels/values. '
            'Preserve all readable address lines; if uncertain return null.'
        )
        local={'http_attempts':[]}
        raw=_post_generate(prompt,b64,local)
        if raw is None: raw=_post_chat(prompt,b64,local)
        trace['http_attempts']=local.get('http_attempts',[]); trace['raw_response']=raw
        parsed=safe_json_loads(raw or ''); trace['parsed_json']=parsed
        if not isinstance(parsed,dict):return None,[]
        value=re.sub(r'\s+',' ',str(parsed.get('customer_address') or '')).strip(' ,;:')
        if len(value)<12 or not re.search(r'[A-Za-z]',value):return None,[]
        # defensive cleanup of field labels if the model echoes them anyway
        value=re.split(r'(?i)\b(?:PO\s*Ref(?:erence)?\s*No\.?|Terms|TIN\s*No\.?|Attention)\s*:',value)[0].strip(' ,;:')
        trace['normalized_address']=value
        return value,["Dedicated customer-address crop recovered a fuller billed-to address."]
    except Exception as e:
        trace['error']=str(e); return None,[]
    finally:
        try: out.unlink(missing_ok=True)
        except OSError: pass


def _normalized_words(value: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(w) >= 3}


def verify_vendor_address_crop(
    image_path: str,
    current_address: str | None,
    ocr_lines: list[dict] | None,
    trace: dict | None = None,
) -> tuple[str | None, list[str]]:
    """Focused re-read of a tiny seller-header address when characters matter.

    This is a conservative 1.60 final pass.  It does *not* invent an address
    when one is absent.  It only tries to verify/correct an already extracted
    seller address that looks like compact header text (for example an ordinal
    street / named business district) and accepts the crop result only when it
    substantially overlaps the current address.  The small crop gives the
    vision model more pixels per character than the earlier whole-header pass.
    """
    if trace is None:
        trace = {}
    enabled = bool(getattr(settings, "VENDOR_ADDRESS_VERIFY_ENABLED", True))
    trace["enabled"] = enabled
    address = re.sub(r"\s+", " ", str(current_address or "")).strip(" ,;:")
    if not enabled or not settings.VISION_VERIFICATION_ENABLED:
        trace["skipped_reason"] = "disabled"
        return None, []
    if len(address) < 12:
        trace["skipped_reason"] = "no existing vendor address to verify"
        return None, []

    # Only spend an extra vision call on character-sensitive header addresses.
    # This trigger is intentionally generic: ordinal streets / business-district
    # wording / very compact multi-part headers are where OCR substitutions are
    # common.  Straightforward seller addresses keep the 1.59 result.
    if not re.search(r"(?i)\b(?:\d{1,3}(?:st|nd|rd|th)\s+street|global\s+city|business\s+park|tower|corner|outlet|barangay|district)\b", address):
        trace["skipped_reason"] = "address does not look character-sensitive"
        return None, []

    img = cv2.imread(image_path)
    if img is None:
        trace["skipped_reason"] = "image unreadable"
        return None, []
    h, w = img.shape[:2]

    # Seller microtext is overwhelmingly in the top header.  Use OCR address
    # cues when available to decide which half contains the most address-like
    # tokens; otherwise preserve the whole width of the top 18%.
    top_h = max(1, int(h * 0.28))
    cue_boxes = []
    cue_re = re.compile(r"(?i)\b(?:street|avenue|ave\.?|road|rd\.?|corner|global\s+city|business\s+park|city|philippines|tower|bldg|building|outlet|barangay|district|marcelino|arroceros)\b")
    for ln in ocr_lines or []:
        if cue_re.search(str(ln.get("text") or "")):
            r = _bbox_rect(ln)
            if r and r[1] <= top_h * 1.25:
                cue_boxes.append(r)
    if cue_boxes:
        avg_x = sum((r[0] + r[2]) / 2 for r in cue_boxes) / len(cue_boxes)
        if avg_x > w * 0.58:
            x1, x2 = int(w * 0.55), w
        elif avg_x < w * 0.42:
            x1, x2 = 0, int(w * 0.55)
        else:
            x1, x2 = 0, w
    else:
        x1, x2 = 0, w
    crop = img[0:top_h, x1:x2]
    if crop.size == 0:
        return None, []
    crop = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8)).apply(gray)
    enhanced = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    out = Path(settings.TEMP_DIR) / f"vendor_address_crop_{uuid.uuid4().hex}.png"
    cv2.imwrite(str(out), enhanced)
    trace["crop_path"] = str(out)
    trace["current_address"] = address
    try:
        b64 = base64.b64encode(out.read_bytes()).decode("utf-8")
        prompt = (
            'Read ONLY the COMPLETE seller/vendor postal address printed in this header crop, including outlet/store/location lines that are visibly part of the address. '
            'Return strict JSON only: {"vendor_address":string|null}. '
            'Transcribe exact visible characters; do not infer from company knowledge. '
            'Pay special attention to ordinal street numbers (for example 22nd vs 32nd) and similar-looking words/letters. '
            'Do not include the vendor/company name, VAT/TIN, invoice number, social-media handles, customer address, or remittance details. '
            f'The current OCR candidate is {address!r}; use it only as a locator, not as truth. If the crop is unreadable return null.'
        )
        local = {"http_attempts": []}
        raw = _post_generate(prompt, b64, local)
        if raw is None:
            raw = _post_chat(prompt, b64, local)
        trace["http_attempts"] = local.get("http_attempts", [])
        trace["raw_response"] = raw
        parsed = safe_json_loads(raw or "")
        trace["parsed_json"] = parsed
        if not isinstance(parsed, dict):
            return None, []
        value = re.sub(r"\s+", " ", str(parsed.get("vendor_address") or "")).strip(" ,;:")
        if len(value) < 12 or not re.search(r"[A-Za-z]", value):
            return None, []
        # Conservative acceptance: both readings must clearly refer to the same
        # address.  This lets a focused crop fix a few OCR characters without
        # swapping in some unrelated header/remittance address.
        a = _normalized_words(address)
        b = _normalized_words(value)
        overlap = len(a & b) / max(1, min(len(a), len(b)))
        trace["token_overlap"] = overlap
        if overlap < 0.55:
            trace["rejected_reason"] = "focused read does not sufficiently overlap current vendor address"
            return None, []
        if value.casefold() == address.casefold():
            return None, []
        trace["normalized_address"] = value
        return value, ["1.61 focused vendor-address crop recovered/corrected the complete seller header address while preserving address ownership."]
    except Exception as e:
        trace["error"] = str(e)
        return None, []
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass


def verify_plate_number_crop(
    image_path: str,
    ocr_lines: list[dict] | None,
    trace: dict | None = None,
) -> tuple[str | None, list[str]]:
    """High-resolution visual read of the explicit PLATE NO. field.

    OCR commonly confuses D/O, B/8, I/1, and S/5 on thermal parking tickets.
    The crop is anchored only to the explicit plate-label bounding box so an OR
    number, ticket number, or transaction ID cannot take ownership.
    """
    if trace is None:
        trace = {}
    enabled = bool(getattr(settings, "PLATE_NUMBER_VERIFY_ENABLED", True))
    trace["enabled"] = enabled
    if not enabled or not settings.VISION_VERIFICATION_ENABLED:
        trace["skipped_reason"] = "disabled"
        return None, []
    anchor = None
    for ln in ocr_lines or []:
        txt = str(ln.get("text") or "")
        if re.search(r"(?i)\bPLATE\s*(?:NO\.?|NUMBER)?\s*[:#]?", txt):
            anchor = ln
            break
    if not anchor:
        trace["skipped_reason"] = "explicit plate label not found"
        return None, []
    r = _bbox_rect(anchor)
    img = cv2.imread(image_path)
    if not r or img is None:
        trace["skipped_reason"] = "plate region/image unavailable"
        return None, []
    h, w = img.shape[:2]
    x1, y1, x2, y2 = r
    pad_x = max(35, int((x2-x1) * 0.30))
    pad_y = max(20, int((y2-y1) * 1.0))
    crop = img[max(0,y1-pad_y):min(h,y2+pad_y), max(0,x1-pad_x):min(w,x2+pad_x)]
    if crop.size == 0:
        return None, []
    crop = cv2.resize(crop, None, fx=5.0, fy=5.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8)).apply(gray)
    out = Path(settings.TEMP_DIR) / f"plate_number_crop_{uuid.uuid4().hex}.png"
    cv2.imwrite(str(out), gray)
    trace["crop_path"] = str(out)
    trace["ocr_anchor"] = str(anchor.get("text") or "")
    try:
        b64 = base64.b64encode(out.read_bytes()).decode("utf-8")
        prompt = (
            'Read ONLY the value printed after the PLATE NO. / PLATE NUMBER label in this crop. '
            'Return strict JSON only: {"plate_number":string|null}. '
            'Transcribe the exact letters and digits. Pay special attention to D versus O, B versus 8, I versus 1, and S versus 5. '
            'Do not use ticket number, invoice number, OR number, transaction number, cashier ID, or any other identifier. '
            'If even one plate character is genuinely unreadable, return null.'
        )
        local = {"http_attempts": []}
        raw = _post_generate(prompt, b64, local)
        if raw is None:
            raw = _post_chat(prompt, b64, local)
        trace["http_attempts"] = local.get("http_attempts", [])
        trace["raw_response"] = raw
        parsed = safe_json_loads(raw or "")
        trace["parsed_json"] = parsed
        if not isinstance(parsed, dict):
            return None, []
        value = re.sub(r"[^A-Za-z0-9]", "", str(parsed.get("plate_number") or "")).upper()
        if not re.fullmatch(r"[A-Z0-9]{4,10}", value):
            trace["rejected_reason"] = "vision plate is not a plausible compact alphanumeric plate"
            return None, []
        trace["normalized_plate"] = value
        return value, ["1.60 dedicated plate-number crop produced direct visual evidence from the explicit PLATE NO. field."]
    except Exception as e:
        trace["error"] = str(e)
        return None, []
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass


def verify_against_image(
    file_path: str,
    extracted: dict,
    template_name: str | None = None,
    trace: dict | None = None,
) -> tuple[dict, list[str]]:
    if trace is None:
        trace = {}
    trace.update({"enabled": settings.VISION_VERIFICATION_ENABLED, "model": settings.VISION_MODEL})
    if not settings.VISION_VERIFICATION_ENABLED:
        trace["skipped_reason"] = "VISION_VERIFICATION_ENABLED=false"
        return {}, []

    image_b64 = _image_to_base64(file_path)
    if not image_b64:
        trace["skipped_reason"] = "image could not be read"
        return {}, []

    if template_name is None:
        template_name = detect_invoice_template(str(extracted.get("vendor_name") or ""))
    fields = WATSONS_VISION_CHECK_FIELDS if template_name == "watsons" else VISION_CHECK_FIELDS
    fields_to_check = {f: extracted.get(f) for f in fields}
    trace["fields_sent"] = fields_to_check
    prompt = build_vision_verification_prompt(fields_to_check, template_name=template_name)
    trace["prompt"] = prompt

    raw = None
    try:
        raw = _post_generate(prompt, image_b64, trace)
        if raw is None:
            logger.warning("Vision /api/generate request rejected; retrying with /api/chat")
            raw = _post_chat(prompt, image_b64, trace)
    except requests.RequestException as e:
        trace["error"] = str(e)
        response = getattr(e, "response", None)
        if response is not None:
            trace["response_body"] = response.text[:4000]
        logger.warning(f"Vision verification call failed, skipping: {e}")
        return {}, []

    if raw is None:
        trace["error"] = "Both /api/generate and /api/chat were rejected"
        logger.warning("Vision verification failed on both Ollama multimodal endpoints; see diagnostic trace for response body.")
        return {}, []

    trace["raw_response"] = raw
    parsed = safe_json_loads(raw)
    trace["parsed_json"] = parsed
    if not isinstance(parsed, dict):
        trace["error"] = "Vision model did not return valid JSON"
        logger.warning("Vision model did not return valid JSON, skipping verification.")
        return {}, []

    mismatches = parsed.get("mismatches", [])
    if not isinstance(mismatches, list):
        trace["error"] = "mismatches is not a list"
        return {}, []

    corrections: dict = {}
    notes: list[str] = []
    for m in mismatches:
        if not isinstance(m, dict):
            continue
        field = m.get("field")
        if field not in fields:
            continue
        seen = m.get("image_shows", "?")
        note = m.get("note", "")
        old_value = fields_to_check.get(field)

        if field == "discount":
            new_value = normalize_discount(seen)
        elif field in _MONEY_FIELDS:
            new_value = to_float(seen)
        else:
            new_value = str(seen).strip() or None

        if new_value is not None:
            if field in _MONEY_FIELDS:
                old_comparable = to_float(old_value) if old_value is not None else None
            else:
                old_comparable = str(old_value).strip() if old_value is not None else None
            if old_comparable is not None and new_value == old_comparable:
                continue
            corrections[field] = new_value
            notes.append(
                f"Vision check: auto-corrected '{field}' from '{old_value}' to '{new_value}' — still needs review."
                + (f" ({note})" if note else "")
            )
        else:
            notes.append(
                f"Vision check: '{field}' extracted as '{old_value}' but the image appears to show '{seen}' "
                f"(could not auto-apply — please verify manually)." + (f" ({note})" if note else "")
            )

    trace["mismatches"] = mismatches
    trace["corrections"] = corrections
    trace["notes"] = notes
    return corrections, notes
