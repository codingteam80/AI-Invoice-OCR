"""Streamlit page: export invoices to Excel / CSV / PDF."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
from config.constants import CATEGORY_OPTIONS
from services.invoice_service import list_invoices
from services.export_service import export_invoices
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


status_filter = st.selectbox("Include invoices with status", ["All", "processed", "needs_review", "failed", "pending"])

all_status_invoices = list_invoices(limit=1000, status=None if status_filter == "All" else status_filter)

month_keys = sorted(
    {_month_key(inv) for inv in all_status_invoices if _month_key(inv) != "unknown"}, reverse=True
)
month_label_map = {"All": "All"} | {_month_label(k): k for k in month_keys}

f1, f2 = st.columns(2)
month_choice = f1.selectbox("Filter by month", list(month_label_map.keys()))
category_choice = f2.selectbox("Filter by category", ["All"] + CATEGORY_OPTIONS)

fmt = st.radio("Export format", ["xlsx", "csv", "pdf"], horizontal=True)

invoices = all_status_invoices
if month_choice != "All":
    chosen_month_key = month_label_map[month_choice]
    invoices = [inv for inv in invoices if _month_key(inv) == chosen_month_key]
if category_choice != "All":
    invoices = [inv for inv in invoices if (inv.get("category") or "Others") == category_choice]

total_count = len(invoices)
locked_count = sum(1 for inv in invoices if inv.get("locked"))
all_locked = total_count > 0 and locked_count == total_count

st.subheader("Invoices in this selection")
if not invoices:
    st.info("No invoices match that filter.")
else:
    table_df = pd.DataFrame(
        [
            {
                "ID": inv["id"],
                "Invoice #": inv.get("invoice_number"),
                "Vendor": inv.get("vendor_name"),
                "Date": inv.get("invoice_date") or "-",
                "Net Amount": inv.get("subtotal") or 0,
                "VAT": inv.get("tax_amount") or 0,
                "Total Amount Due": inv.get("total_amount") or 0,
                "Currency": inv.get("currency") or "-",
                "Status": inv.get("status") or "-",
                "Category": inv.get("category") or "-",
                "Locked": "🔒 Yes" if inv.get("locked") else "🔓 No",
            }
            for inv in invoices
        ]
    )
    st.dataframe(table_df, use_container_width=True, hide_index=True)

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
    path = export_invoices(invoices, fmt=fmt)
    st.success(f"Exported {len(invoices)} invoice(s).")
    with open(path, "rb") as f:
        st.download_button(
            label=f"Download {Path(path).name}",
            data=f.read(),
            file_name=Path(path).name,
        )
