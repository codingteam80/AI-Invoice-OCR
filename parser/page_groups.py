"""Group explicit continuation pages without collapsing unrelated invoices."""
import re

_PAGE = re.compile(r"\bpage\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.I)
_IDENTITY = re.compile(r"\b(?:invoice|statement|account)\s*(?:number|no\.?|#)\s*:?\s*([A-Z0-9][A-Z0-9/-]*)", re.I)


def group_invoice_pages(pages):
    groups = []
    for page in pages:
        text = page.get("text") or ""
        marker = _PAGE.search(text)
        identity = {m.group(1).strip().casefold() for m in _IDENTITY.finditer(text)}
        join = False
        if groups and marker and int(marker[1]) > 1:
            previous = groups[-1][-1]
            prev_marker = _PAGE.search(previous.get("text") or "")
            prev_identity = {m.group(1).strip().casefold() for m in _IDENTITY.finditer(previous.get("text") or "")}
            join = bool(prev_marker and int(marker[1]) == int(prev_marker[1]) + 1
                        and marker[2] == prev_marker[2]
                        and (not identity or not prev_identity or identity & prev_identity))
        if join:
            groups[-1].append(page)
        else:
            groups.append([page])
    result = []
    for group in groups:
        merged = dict(group[0])
        merged["source_page_numbers"] = [p.get("page_number", 1) for p in group]
        if len(group) > 1:
            merged["text"] = "\n\n".join(
                f"[DOCUMENT PAGE {p.get('page_number', 1)}]\n{p.get('text', '')}" for p in group)
            # Geometry reconciliations are page-local. Keep first-page geometry;
            # continuation text still reaches the model and text reconciliation.
            merged["continuation_pages"] = group[1:]
            merged["avg_confidence"] = min(p.get("avg_confidence", 0) for p in group)
            merged["engine"] = "+".join(sorted({p.get("engine") or "unknown" for p in group}))
        marker = _PAGE.search(group[0].get("text") or "")
        warnings = [w for page in group for w in (page.get("warnings") or [])]
        if marker and (int(marker[1]) != 1 or len(group) != int(marker[2])):
            warnings.append(f"Incomplete numbered document: received {len(group)} page(s), printed page count is {marker[2]}.")
        merged["warnings"] = warnings
        result.append(merged)
    return result
