"""Streamlit page: spend analytics dashboard."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
from services.invoice_service import get_dashboard_stats
from services.report_service import spend_by_vendor, spend_by_month, spend_by_category
from services.invoice_service import list_invoices
from services.currency_totals import currency_code, totals_by_currency
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="Dashboard", page_icon="📊", layout="wide")

render_nav()
guard_locked_navigation()

st.title("📊 Dashboard")

stats = get_dashboard_stats()
c1, c2, c3 = st.columns(3)
c1.metric("Total Invoices", stats["total_invoices"])
c2.metric("Vendors", stats["vendor_count"])
c3.metric("Needs Review", stats["needs_review"])

all_invoices = list_invoices(limit=None, status=None)
currency_totals = totals_by_currency(all_invoices)
st.subheader("Total Spend by Currency")
if currency_totals:
    cols = st.columns(min(len(currency_totals), 4))
    for index, (code, amount) in enumerate(currency_totals.items()):
        cols[index % len(cols)].metric(code, f"{amount:,.2f}")
    currency_choice = st.selectbox("Currency for spending charts", sorted(currency_totals),
                                   index=sorted(currency_totals).index("PHP") if "PHP" in currency_totals else 0)
else:
    st.info("No invoice amounts have been saved yet.")
    currency_choice = "PHP"
st.caption("Currencies are not converted or added together. Each spending chart shows only the selected currency.")
st.divider()

st.subheader("Spend by Vendor")
vendor_totals = spend_by_vendor(currency=currency_choice)
if vendor_totals:
    df = pd.DataFrame(list(vendor_totals.items()), columns=["Vendor", "Total"]).head(15)
    st.bar_chart(df.set_index("Vendor"))
else:
    st.info("No data yet — upload some invoices first.")

st.subheader("Spend by Category")
category_totals = spend_by_category(currency=currency_choice)
if category_totals:
    df = pd.DataFrame(list(category_totals.items()), columns=["Category", "Total"])
    st.bar_chart(df.set_index("Category"))
else:
    st.info("No data yet — upload some invoices first.")

st.subheader("Spend by Month")
month_basis = st.radio("Group spending by", ["Invoice date", "Upload date"], horizontal=True)
month_totals = spend_by_month(date_basis="invoice" if month_basis == "Invoice date" else "uploaded", currency=currency_choice)
if month_totals:
    df = pd.DataFrame(list(month_totals.items()), columns=["Month", "Total"])
    st.bar_chart(df.set_index("Month"))
else:
    st.info("No data yet — upload some invoices first.")
