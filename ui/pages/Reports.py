"""Streamlit page: export invoices to Excel / CSV / PDF."""
import sys
import html
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
from config.constants import CATEGORY_OPTIONS
from services.invoice_service import list_invoices
from services.export_service import export_invoices
from exports.chart import generate_chart
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="Reports & Export", page_icon="📁", layout="wide")

render_nav()
guard_locked_navigation()

st.title("📁 Reports & Export")


def _month_key(inv: dict) -> str:
    """'YYYY-MM' bucket for an invoice — prefers the invoice date, falls
    back to when it was uploaded if the date couldn't be read off the doc."""
    raw = inv.get("invoice_date") or (inv.get("created_at") or "")[:10]
    return raw[:7] if raw else "unknown"


def _month_label(month_key: str) -> str:
    try:
        y, m = month_key.split("-")
        return date(int(y), int(m), 1).strftime("%B %Y")
    except (ValueError, AttributeError):
        return "Unknown date"


def _render_preview_table(df: pd.DataFrame) -> None:
    """Renders `df` as a plain HTML table instead of st.dataframe.

    st.dataframe's interactive grid (glide-data-grid) draws cell text on an
    HTML <canvas>, so its font-size is hardcoded and CSS can't touch it —
    that's why a previous font-size bump for the rest of the app had no
    effect here. A hand-built table sidesteps that entirely, and also gives
    exact column widths (ID/Locked at ~45% of a normal column's width,
    comfortably over the "at least 1/3" ask) instead of the small/medium/
    large presets st.dataframe is limited to.
    """
    narrow_cols = {"ID", "Locked"}
    narrow_pct, n_narrow = 8, sum(1 for c in df.columns if c in narrow_cols)
    n_wide = len(df.columns) - n_narrow
    wide_pct = (100 - narrow_pct * n_narrow) / n_wide if n_wide else 0

    header_cells = "".join(
        f'<th style="width:{narrow_pct if col in narrow_cols else wide_pct:.1f}%">{html.escape(str(col))}</th>'
        for col in df.columns
    )
    body_rows = "".join(
        f'<tr style="background:{"#F7FBF6" if i % 2 else "#FFFFFF"}">'
        + "".join(f"<td>{html.escape(str(v))}</td>" for v in row)
        + "</tr>"
        for i, (_, row) in enumerate(df.iterrows())
    )

    st.markdown(
        f"""
        <style>
        .tsukiden-report-table {{ width: 100%; border-collapse: collapse; font-size: 1.15rem; }}
        .tsukiden-report-table th {{
            background: #1F4E78; color: white; text-align: left; padding: 0.5rem 0.6rem;
        }}
        .tsukiden-report-table td {{ padding: 0.5rem 0.6rem; border-bottom: 1px solid #E4E9E3; }}
        </style>
        <div style="overflow-x:auto;">
        <table class="tsukiden-report-table">
            <thead><tr>{header_cells}</tr></thead>
            <tbody>{body_rows}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


all_invoices = list_invoices(limit=1000, status=None)

month_keys = sorted(
    {_month_key(inv) for inv in all_invoices if _month_key(inv) != "unknown"}, reverse=True
)
month_label_map = {_month_label(k): k for k in month_keys}

vendors = sorted({inv["vendor_name"] for inv in all_invoices if inv.get("vendor_name")})

f1, f2, f3 = st.columns(3)
month_choice = f1.multiselect("Filter by month", list(month_label_map.keys()))
category_choice = f2.multiselect("Filter by category", CATEGORY_OPTIONS)
vendor_choice = f3.multiselect("Filter by vendor", vendors)

fmt = st.radio("Export format", ["xlsx", "csv", "pdf"], horizontal=True)

invoices = all_invoices
if month_choice:
    chosen_month_keys = {month_label_map[m] for m in month_choice}
    invoices = [inv for inv in invoices if _month_key(inv) in chosen_month_keys]
if category_choice:
    invoices = [inv for inv in invoices if (inv.get("category") or "Others") in category_choice]
if vendor_choice:
    invoices = [inv for inv in invoices if inv.get("vendor_name") in vendor_choice]

total_count = len(invoices)
locked_count = sum(1 for inv in invoices if inv.get("locked"))
all_locked = total_count > 0 and locked_count == total_count

st.subheader("Invoices in this selection")
if not invoices:
    st.info("No invoices match that filter.")
else:
    # On-screen preview only shows the at-a-glance fields; the exported
    # file (see export_service.py / exports/common.py) includes every
    # invoice field except Status and Locked.
    table_df = pd.DataFrame(
        [
            {
                "ID": inv["id"],
                "Invoice #": inv.get("invoice_number"),
                "Vendor": inv.get("vendor_name"),
                "Date": inv.get("invoice_date") or "-",
                "Category": inv.get("category") or "-",
                "Total Amount Due": f"{(inv.get('total_amount') or 0):,.2f} {inv.get('currency') or ''}".strip(),
                "Locked": "🔒 Yes" if inv.get("locked") else "🔓 No",
            }
            for inv in invoices
        ]
    )
    _render_preview_table(table_df)

    # Net Amount and VAT sums are intentionally not shown on this page —
    # only Total Amount Due is. All three are still included in every
    # exported file (see exports/common.py: invoice_totals_row).
    total_sum = sum(inv.get("total_amount") or 0 for inv in invoices)
    st.metric("Total Amount Due (sum)", f"{total_sum:,.2f}")

st.subheader("📊 Chart")
chart_path_for_export = None
if not invoices:
    st.caption("No invoices to chart — adjust the filters above.")
else:
    c1, c2 = st.columns([1, 3])
    chart_group_by = c1.multiselect(
        "Group chart by", ["Vendor", "Category", "Month"], default=["Vendor"], key="report_chart_group_by"
    )
    if c1.button("📊 Generate Graph", disabled=not chart_group_by):
        chart_path = generate_chart(invoices, group_by=chart_group_by)
        st.session_state["report_chart_path"] = chart_path
        st.session_state["report_chart_ids"] = tuple(sorted(inv["id"] for inv in invoices))

    # Only used in the export below if it still matches the currently
    # filtered invoices — otherwise it's a stale graph from an earlier filter.
    stored_chart_path = st.session_state.get("report_chart_path")
    if stored_chart_path and Path(stored_chart_path).exists():
        with c2:
            # use_container_width was added to st.image() later than for most
            # other widgets — older Streamlit installs raise a TypeError, so
            # fall back to the older use_column_width param in that case.
            try:
                st.image(stored_chart_path, use_container_width=True)
            except TypeError:
                st.image(stored_chart_path, use_column_width=True)
            current_ids = tuple(sorted(inv["id"] for inv in invoices))
            if st.session_state.get("report_chart_ids") == current_ids:
                chart_path_for_export = stored_chart_path
            else:
                st.caption(
                    "⚠️ Filters have changed since this graph was generated — click "
                    "**Generate Graph** to refresh it. The export below won't include this outdated graph."
                )

if total_count > 0:
    if all_locked:
        st.success(f"✅ All {total_count} invoice(s) in this selection are locked and ready to export.")
    else:
        st.warning(
            f"🔒 {locked_count}/{total_count} invoice(s) locked. Every invoice in this "
            "selection must be reviewed and locked on the History page before you can export."
        )
        st.page_link("pages/History.py", label="Go lock the remaining invoices", icon="🗂️")

if st.button("Generate Export", type="primary", disabled=not all_locked):
    paths = export_invoices(invoices, fmt=fmt, chart_path=chart_path_for_export)
    st.success(f"Exported {len(invoices)} invoice(s).")
    for p in paths:
        with open(p, "rb") as f:
            st.download_button(
                label=f"Download {Path(p).name}",
                data=f.read(),
                file_name=Path(p).name,
                key=f"download_{Path(p).name}",
            )
    if fmt == "csv" and chart_path_for_export:
        st.caption("CSV is plain text, so the graph couldn't be embedded in it — it's included as a separate PNG above instead.")
