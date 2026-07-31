"""Streamlit page: export invoices to Excel / CSV / PDF."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from services.invoice_service import list_invoices
from services.export_service import export_invoices
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="Reports & Export", page_icon="📁", layout="wide")

render_nav()
guard_locked_navigation()

st.title("📁 Reports & Export")

status_filter = st.selectbox("Include invoices with status", ["All", "processed", "needs_review", "failed", "pending"])
fmt = st.radio("Export format", ["xlsx", "csv", "pdf"], horizontal=True)

invoices = list_invoices(limit=1000, status=None if status_filter == "All" else status_filter)
total_count = len(invoices)
locked_count = sum(1 for inv in invoices if inv.get("locked"))
all_locked = total_count > 0 and locked_count == total_count

if total_count == 0:
    st.info("No invoices match that filter.")
elif all_locked:
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
