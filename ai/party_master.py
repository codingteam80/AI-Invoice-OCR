"""Final party-ownership / master-data / completeness pass (v1.71).

Runs LAST in the parse pipeline (after every OCR/LLM/vision reconcile step) and
fixes the failure modes seen on the real Tsukiden invoices:

1. Customer address text bleeding into ``vendor_address`` (all 5 invoices).
2. OCR typos in addresses (Julla/comer/Meraico/Cenire/...), and the vendor and
   customer addresses being swapped when the customer text is garbled.
3. Issuer slogans (e.g. SGV "Building a better working world") in an address.
4. ``Issue Date`` label (SGV) not being recognised as the invoice date.
5. Line items that do not add up to the invoice (SGV ``Expense 500.00`` and
   Globe ``Discounts (400.00)`` rows dropped) - recovered from OCR text only
   when exactly the missing amount is found, never guessed.

Every function is pure: ``(data, ...) -> (new_data, notes)``.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from utils.date_utils import parse_date

MASTER_PATH = Path(__file__).resolve().parents[1] / "config" / "party_master.json"

_SLOGANS = re.compile(r"(?i)\bbuilding\s+a\s+better(?:\s+working\s+world)?\b")


# ------------------------------------------------------------------ helpers
@lru_cache(maxsize=1)
def load_master() -> dict:
    try:
        return json.loads(MASTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"vendors": [], "customers": []}


def _digits(v) -> str:
    return re.sub(r"\D+", "", str(v or ""))


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(t) >= 2]


def _tidy_address(s: str | None) -> str | None:
    if not s:
        return s
    t = re.sub(r"\s+", " ", str(s))
    t = re.sub(r"\s*,\s*(?:[.,]\s*)+", ", ", t)        # ",.," / ", ,"
    t = re.sub(r"(?<=[A-Za-z0-9])\.{2,}", ".", t)        # "Ave.." -> "Ave."
    t = re.sub(r"(?<=[A-Za-z])\.(?=,)", "", t)           # "Philippines.," -> "Philippines,"
    t = re.sub(r",(?=\S)", ", ", t)                      # "Brgy.Old,X" -> "Brgy.Old, X"
    return t.strip(" ,;")


def _find_customer(data: dict) -> dict | None:
    tin = _digits(data.get("customer_tax_id"))[:9]
    name = _norm(data.get("customer_name"))
    for c in load_master().get("customers", []):
        if tin and tin == _digits(c.get("tin"))[:9]:
            return c
        if any(k in name for k in c.get("name_keys", [])):
            return c
    return None


def _find_vendor(data: dict) -> dict | None:
    tin = _digits(data.get("vendor_tax_id"))[:9]
    if not tin:
        return None
    for v in load_master().get("vendors", []):
        if tin == _digits(v.get("tin"))[:9]:
            return v
    return None


_GENERIC = {"metro", "manila", "city", "philippines", "ncr", "district", "street", "st", "avenue", "ave",
            "road", "rd", "floor", "center", "centre", "brgy", "barangay", "cor", "corner", "the", "of", "and"}


def _token_matches(tok: str, pool: list[str]) -> bool:
    if tok in pool:
        return True
    if len(tok) >= 4:
        return any(SequenceMatcher(None, tok, p).ratio() >= 0.75 for p in pool if len(p) >= 4)
    return False


# ------------------------------------------- 1) vendor / customer isolation
def isolate_party_addresses(data: dict) -> tuple[dict, list[str]]:
    """Strip customer address text (and slogans) from vendor_address; repair the
    customer address from known printed variants when it is garbled."""
    out = dict(data)
    notes: list[str] = []
    vaddr = str(out.get("vendor_address") or "").strip()
    caddr = str(out.get("customer_address") or "").strip()
    cust = _find_customer(out)
    variants = list(cust.get("address_variants", [])) if cust else []

    removed: list[str] = []
    if vaddr:
        cleaned = _SLOGANS.sub("", vaddr)
        reference = [caddr] + variants
        ref_tokens = _tokens(" ".join(reference))
        segs = [s.strip() for s in re.split(r",\s*", cleaned) if s.strip(" .,;")]
        # customer text is appended AFTER the seller's own address -> peel from the end
        while len(segs) > 1:
            toks = _tokens(segs[-1])
            hit = [t for t in toks if _token_matches(t, ref_tokens)]
            distinctive = [t for t in hit if t not in _GENERIC]
            if toks and len(hit) / len(toks) >= 0.6 and distinctive \
                    and (len(toks) >= 2 or re.fullmatch(r"\d{3,5}", toks[0] or "")):
                removed.insert(0, segs.pop())
            else:
                break
        new_v = _tidy_address(", ".join(segs))
        if new_v != vaddr:
            out["vendor_address"] = new_v
            notes.append("1.71 vendor-address isolation removed customer-address text/slogan from vendor_address"
                         + (f": {', '.join(removed)!r}" if removed else "."))

    # customer address repair against known printed variants
    if variants:
        candidates = [c for c in [caddr, ", ".join(removed)] if c]
        best = (0.0, None, None)
        for cand in candidates:
            for var in variants:
                r = _ratio(cand, var)
                if r > best[0]:
                    best = (r, var, cand)
        already_exact = caddr and any(_norm(caddr) == _norm(v) for v in variants)
        if best[1] and not already_exact and best[0] >= 0.6:
            if _norm(out.get("customer_address")) != _norm(best[1]):
                out["customer_address"] = best[1]
                notes.append(f"1.71 customer-address snapped to the known printed variant (similarity {best[0]:.2f}) to repair OCR typos.")
    if out.get("customer_address"):
        out["customer_address"] = _tidy_address(out["customer_address"])
    return out, notes


# ------------------------------------------------ 2) vendor master by TIN
def apply_vendor_master(data: dict) -> tuple[dict, list[str]]:
    v = _find_vendor(data)
    if not v or not v.get("vendor_address"):
        return data, []
    if re.sub(r"\s+", " ", str(data.get("vendor_address") or "")).strip() == v["vendor_address"]:
        return data, []
    out = dict(data)
    out["vendor_address"] = v["vendor_address"]
    return out, [f"1.71 vendor master (TIN {v.get('tin')}) supplied the exact printed vendor address for {v.get('label')}."]


# ----------------------------------------------------- 3) Issue Date label
_MONTH_DATE = re.compile(
    r"(?i)\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_DATE_BOILER = re.compile(r"(?i)date\s+issued|acknowledg|\bATP\b|accredit|permit|valid\s+until|expir")


def reconcile_issue_date(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    """``Issue Date:`` (not ``Date Issued``, which is a BIR/ATP boilerplate label)."""
    if not ocr_text or not re.search(r"(?i)\bissue\s+date\b", ocr_text):
        return data, []
    if re.search(r"(?i)\binvoice\s+date\b", ocr_text):
        return data, []               # explicit Invoice Date already handled upstream
    dates = []
    for line in ocr_text.splitlines():
        if _DATE_BOILER.search(line):
            continue
        for m in _MONTH_DATE.finditer(line):
            d = parse_date(m.group(0))
            if d:
                dates.append(d)
    if not dates:
        return data, []
    has_due = bool(re.search(r"(?i)\bdue\s+date\b", ocr_text))
    issue = min(dates[:2]) if has_due and len(dates) >= 2 else dates[0]
    iso = issue.isoformat()
    if data.get("invoice_date") == iso:
        return data, []
    out = dict(data)
    out["invoice_date"] = iso
    return out, [f"1.71 invoice date set to {iso} from the explicit 'Issue Date' label (BIR 'Date Issued' dates excluded)."]


# --------------------------------------------- 4) missing line-item recovery
_AMT = re.compile(r"(\()?\s*(?:PHP|P)?\s*(\d{1,3}(?:,\d{3})*\.\d{2})\s*(\))?", re.I)
_BAD_LABEL = re.compile(r"(?i)\b(total|sub\s*total|vat|tax|balance|amount|due|payment|previous|withholding|less|net|sales|tin|invoice|date|acct|account)\b")


def _sum_items(items) -> float:
    return round(sum(float((i or {}).get("amount") or 0) for i in items), 2)


def recover_missing_line_items(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    items = list(data.get("line_items") or [])
    if not items or not ocr_text:
        return data, []
    tax = float(data.get("tax_amount") or 0)
    sub = float(data.get("subtotal") or 0)
    total = float(data.get("total_amount") or 0)
    wht = float(data.get("withholding_tax") or 0)
    target = sub if (tax > 0 and sub > 0) else (total + wht)
    diff = round(target - _sum_items(items), 2)
    if abs(diff) < 0.01:
        return data, []
    # Version 2.0 already stores discounts separately and validators subtract
    # them from subtotal. Do not represent the same discount a second time as
    # a negative item or lower the subtotal by that discount again.
    discount = float(data.get("discount") or 0)
    if diff < 0 and discount > 0 and abs(diff + discount) < 0.01:
        return data, []

    lines = [re.sub(r"\s+", " ", l).strip() for l in ocr_text.splitlines()]
    hits = []
    for i, line in enumerate(lines):
        for m in _AMT.finditer(line):
            val = float(m.group(2).replace(",", ""))
            neg = bool(m.group(1) and m.group(3))
            signed = -val if neg else val
            if abs(signed - diff) > 0.005:
                continue
            same = _AMT.sub(" ", line[:m.start()]).strip(" :.-")
            label = same if len(re.findall(r"[A-Za-z]", same)) >= 3 else None
            if not label:
                cands = []
                for j in list(range(i - 1, max(-1, i - 7), -1)) + list(range(i + 1, min(len(lines), i + 3))):
                    cand = lines[j].strip(" :.-")
                    if len(re.findall(r"[A-Za-z]", cand)) >= 4 and not _AMT.search(cand) and not _BAD_LABEL.search(cand):
                        cands.append(cand)
                if signed < 0:
                    cands = [c for c in cands if re.search(r"(?i)discount|rebate|promo|adjust|credit", c)]
                label = cands[0] if cands else None
            if label and not _BAD_LABEL.search(label):
                hits.append((label, signed))
    labels = {re.sub(r"\W+", "", h[0].lower()) for h in hits}
    if len(labels) != 1:
        return data, []               # none or ambiguous -> never guess
    label, signed = hits[0]
    if any(abs(float(x.get("amount") or 0) - signed) < 0.005 and _norm(x.get("description")) == _norm(label) for x in items):
        return data, []
    out = dict(data)
    out["line_items"] = items + [{"description": label.strip().title() if label.isupper() else label.strip(),
                                  "quantity": 1.0, "unit_price": signed, "amount": signed}]
    new_sum = _sum_items(out["line_items"])
    notes = [f"1.71 recovered missing line item {label!r} = {signed:,.2f} from OCR so items reconcile to the invoice."]
    if tax <= 0 and abs(new_sum - target) < 0.02 and abs(sub - new_sum) > 0.01:
        out["subtotal"] = new_sum
        notes.append(f"1.71 subtotal aligned to reconciled line-item sum {new_sum:,.2f}.")
    return out, notes


# ------------------------------------------------------------- entry point
def apply_final_master_pass(data: dict, ocr_text: str) -> tuple[dict, list[str]]:
    notes: list[str] = []
    cur = dict(data)
    for step in (
        lambda d: isolate_party_addresses(d),
        lambda d: apply_vendor_master(d),
        lambda d: reconcile_issue_date(d, ocr_text),
        lambda d: recover_missing_line_items(d, ocr_text),
    ):
        cur, n = step(cur)
        notes.extend(n)
    return cur, notes
