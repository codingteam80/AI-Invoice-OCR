"""Compact review-oriented UI for stored OCR field and line-item evidence.

All extraction-time confidence metadata remains in SQLite. The History page
shows only *actionable* review flags, rather than percentages on every field.
"""
from __future__ import annotations

from html import escape
from decimal import Decimal, InvalidOperation

import pandas as pd

from ai.field_display import evidence_for_invoice, field_review_summary


# Human edits invalidate the old automatic evidence, but are not themselves
# evidence that the human-corrected value needs another OCR review.
def needs_verification(meta: dict | None) -> bool:
    return bool(
        isinstance(meta, dict)
        and meta.get("warning")
        and meta.get("status") not in {"manual_edit", "not_provided", "derived"}
    )


def render_field(column, label: str, field: str, value: str, evidence: dict[str, dict]) -> None:
    """Show plain fields normally; highlight only values needing a review.

    Values and warning text are HTML-escaped because invoice content is
    untrusted. A hover title provides the explanation without a new paragraph.
    """
    meta = evidence.get(field) or {}
    if not needs_verification(meta):
        column.write(f"**{label}:** {value}")
        return

    warning = escape(str(meta["warning"]), quote=True)
    column.markdown(
        '<div style="background:rgba(245,158,11,0.12);'
        'border-left:3px solid #d97706; border-radius:5px;'
        'padding:6px 9px; margin:3px 0 7px 0;"'
        f' title="{warning}">'
        f'<strong>{escape(str(label))}:</strong> {escape(str(value))}'
        ' <span style="white-space:nowrap; color:#b45309; font-weight:600;'
        'font-size:0.82em;">⚠ Verify</span></div>',
        unsafe_allow_html=True,
    )


def format_line_item_quantity(value: object) -> str:
    """Show whole quantities without decimals, preserving true fractional values.

    This changes the read-only UI only. The stored quantity and any arithmetic
    continue using their original numeric values; nothing is rounded to an int.
    """
    if value is None or str(value).strip() == "":
        return "—"
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return "—"
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


LINE_ITEM_COLUMNS = {
    "description": "Item",
    "quantity": "Qty",
    "unit_price": "Unit Price",
    "amount": "Total Unit Price",
}


def line_items_review_table(line_items: list[dict], evidence: dict[str, dict]):
    """Build an uncluttered 4-column table with amber *cell* highlights.

    Streamlit's st.dataframe renders the background colors of Pandas Styler
    without requiring a new non-editable component or embedding HTML tables.
    Returns (styled_dataframe, review_reasons) for the History page.
    """
    rows = []
    flagged_cells = set()
    flagged_descriptions = []
    for ix, item in enumerate(line_items):
        row = {
            "Item": item.get("description") or "—",
            "Qty": format_line_item_quantity(item.get("quantity")),
            "Unit Price": f"{float(item['unit_price']):,.2f}" if item.get("unit_price") is not None else "—",
            "Total Unit Price": f"{float(item['amount']):,.2f}" if item.get("amount") is not None else "—",
        }
        for field, label in LINE_ITEM_COLUMNS.items():
            meta = evidence.get(f"line_items.{ix}.{field}")
            if needs_verification(meta):
                flagged_cells.add((ix, label))
                flagged_descriptions.append(
                    f"Row {ix + 1}, {label} ({item.get('description') or 'unnamed item'}): {meta['warning']}"
                )
        rows.append(row)
    df = pd.DataFrame(rows, columns=list(LINE_ITEM_COLUMNS.values()))

    def _style_cells(_df: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=_df.index, columns=_df.columns)
        for ix, col in flagged_cells:
            styles.at[ix, col] = "background-color: #fff0c2; color: #754600; font-weight: bold;"
        return styles

    return df.style.apply(_style_cells, axis=None), flagged_descriptions
