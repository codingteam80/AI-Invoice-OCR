"""Conservative, image-backed recovery of conflicting totals / missing service rows.

Never substitute arithmetic for an image reading; ambiguous reads remain reviewable.
"""
import math
import re
from pathlib import Path
import uuid
import cv2
from config.settings import settings
from ai.source_evidence import geometry
from parser.currency_parser import to_float
from utils.helpers import safe_json_loads

FIELDS = ('subtotal', 'tax_amount', 'zero_rated_sales', 'vat_exempt_sales', 'discount', 'withholding_tax', 'total_amount')
PATTERN = r'(?i)^(?:VATable Sales|VAT[- ]Exempt Sales|Zero[- ]Rated Sales|TOTAL AMOUNT DUE|Total Sales|Less:|Add: VAT|VAT$)'


def _amount(value):
    if isinstance(value, bool):
        return None
    n = to_float(value)
    return n if n is not None and math.isfinite(n) and n >= 0 else None


def _balance(d):
    vals = [_amount(d.get(k)) for k in FIELDS]
    if any(v is None for v in vals):
        return None
    sub, vat, zero, exempt, disc, wht, total = vals
    return abs(sub + vat + zero + exempt - disc - wht - total) <= .02


def _read_crop(image, rect, prompt, trace):
    from ai.vision_verifier import _image_to_base64, _post_generate, _post_chat
    x0, y0, x1, y1 = rect
    crop = image[y0:y1, x0:x1]
    if not crop.size:
        return None
    path = Path(settings.TEMP_DIR) / ('focused_review_' + uuid.uuid4().hex + '.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not cv2.imwrite(str(path), crop):
            raise OSError('Could not create focused invoice image')
        trace['crop'] = list(rect)
        b64 = _image_to_base64(str(path))
        if not b64:
            raise OSError('Could not read focused invoice image')
        raw = _post_generate(prompt, b64, trace)
        if raw is None:
            raw = _post_chat(prompt, b64, trace)
        parsed = safe_json_loads(raw) if raw else None
        trace['parsed_json'] = parsed
        if not isinstance(parsed, dict):
            trace['error'] = 'Focused image review returned no valid JSON object'
            return None
        return parsed
    except Exception as exc:
        trace['error'] = str(exc)
        return None
    finally:
        path.unlink(missing_ok=True)


def review_unresolved_regions(data, image_path, lines, trace):
    out = dict(data)
    notes = []
    if not settings.VISION_VERIFICATION_ENABLED:
        trace['skipped_reason'] = 'Vision verification disabled'
        return out, notes
    cells = geometry(lines)
    summary = [c for c in cells if re.search(PATTERN, c['text'])]
    service = [c for c in cells if re.fullmatch(r'Nature of Service|Description of Service(?:s)?', c['text'], re.I)]
    # Billing statements require separate current/previous-balance accounting.
    financial = _balance(out) is False and out.get('current_charges_total') is None and out.get('previous_balance') in (None, 0)
    items = not out.get('line_items') and service and summary
    if not (financial and summary or items):
        trace['skipped_reason'] = 'No supported unresolved summary or missing service table'
        return out, notes
    image = cv2.imread(str(image_path))
    if image is None:
        trace['error'] = 'Focused review image unavailable'
        return out, notes
    h, w = image.shape[:2]
    top = max(0, int(min(c['y'] - c['h'] for c in summary)))
    bottom = min(h, int(max(c['y'] + c['h'] * 2 for c in summary)))
    if financial and summary:
        ft = trace.setdefault('financial', {})
        parsed = _read_crop(image, (0, top, w, bottom),
            'Read this invoice financial summary independently. Return a JSON object with keys '
            + ', '.join(FIELDS) + '. subtotal means VATable Sales. Read each number from its own labeled row. '
            'Use 0 only for a clearly visible blank or zero row; use null when absent or unclear. '
            'Do not calculate, infer or force a balance. Distinguish discounts from withholding tax. '
            'Return only the object.', ft)
        if parsed:
            read = {k: _amount(parsed.get(k)) for k in FIELDS}
            changes = [k for k in FIELDS if read[k] != _amount(out.get(k))]
            # A second independent read must preserve the printed payable and
            # correct exactly one component while confirming the remaining rows.
            if _balance(read) and len(changes) == 1 and changes[0] != 'total_amount' and read['total_amount'] == _amount(out.get('total_amount')):
                field = changes[0]
                old = out.get(field)
                out[field] = read[field]
                ft['accepted_field'] = field
                notes.append(f'Focused image evidence corrected {field} from {old} to {read[field]}; the other summary rows and total were independently confirmed.')
            else:
                ft['rejected_reason'] = 'Image read incomplete, contradictory, or changed more than one field; retained values for review.'
    if items:
        st = trace.setdefault('service_items', {})
        start = max(0, int(min(c['y'] - c['h'] for c in service)))
        if start < top:
            parsed = _read_crop(image, (0, start, w, top),
                'Read only completed service rows in this table. Return JSON {"line_items":[{"description":string,"amount":number}]}. '
                'Transcribe visible handwriting; do not guess unclear words or amounts. Exclude headers and blank rows. '
                'Do not invent quantity or unit price. If uncertain return {"line_items":[]}.', st)
            rows = parsed.get('line_items') if parsed else None
            if isinstance(rows, list) and rows:
                valid = []
                for row in rows:
                    if not isinstance(row, dict):
                        break
                    desc = str(row.get('description') or '').strip()
                    amount = _amount(row.get('amount'))
                    if not desc or not re.search('[A-Za-z]', desc) or amount is None:
                        break
                    valid.append(dict(description=desc, quantity=1.0, unit_price=amount, amount=amount))
                total = _amount(out.get('total_amount'))
                if len(valid) == len(rows) and total is not None and abs(sum(r['amount'] for r in valid) - total) <= .02 and not out.get('discount') and not out.get('withholding_tax'):
                    out['line_items'] = valid
                    st['accepted'] = True
                    notes.append('Missing service rows recovered from the table image; row amounts agree with the printed payable. Each amount-only row is represented as one service charge; quantity 1 and unit price equal to the row amount are display conventions, not printed values.')
                else:
                    st['rejected_reason'] = 'Service rows were incomplete or did not agree with the payable.'
    return out, notes
