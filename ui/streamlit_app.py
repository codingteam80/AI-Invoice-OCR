"""Main Streamlit entrypoint — sets page config and shows the landing dashboard.
Run with:  streamlit run ui/streamlit_app.py
(Multi-page navigation picks up files from ui/pages/ automatically.)
"""
import sys
from pathlib import Path

# Ensure project root is importable when run via `streamlit run ui/streamlit_app.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from database.database import init_db
from services.invoice_service import get_dashboard_stats
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="AI Invoice OCR", page_icon="🧾", layout="wide")
init_db()

render_nav()
guard_locked_navigation()

st.title("🧾 AI Invoice OCR")
st.caption("Local OCR + LLM-powered invoice data extraction")

stats = get_dashboard_stats()
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Invoices", stats["total_invoices"])
col2.metric("Total Amount", f"{stats['total_amount']:,.2f}")
col3.metric("Vendors", stats["vendor_count"])
col4.metric("Needs Review", stats["needs_review"])

st.divider()
st.markdown(
    """
    Use the sidebar to navigate:
    - **Upload** — submit new invoices for processing
    - **Dashboard** — spend analytics and charts
    - **History** — browse and search processed invoices
    - **Reports** — export data to Excel / CSV / PDF
    """
)
