"""Streamlit page: browse, search, and drill into processed invoices."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
from database.repository import DuplicateInvoiceError, InvoiceLockedError
from services.invoice_service import (
    list_invoices, get_invoice, update_invoice, lock_invoice, unlock_invoice,
)
from services.search_service import search
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="Invoice History", page_icon="🗂️", layout="wide")

render_nav()
guard_locked_navigation()

st.title("🗂️ Invoice History")

STATUS_OPTIONS = ["processed", "needs_review", "failed", "pending"]

ROW_WIDTHS = [0.5, 1.0, 1.3, 1.1, 0.8, 0.9, 0.7, 1.0, 0.6, 0.9, 0.8, 0.7, 0.9]
COLUMN_LABELS = [
    "ID", "Invoice #", "Filename", "Vendor", "Date",
    "Net Amount", "VAT", "Total Amount Due", "Currency", "Status", "Confidence", "", "",
]


def _parse_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)  # raises ValueError -> caught by caller


@st.dialog("✏️ Edit Invoice", width="large")
def edit_invoice_dialog(inv: dict):
    if inv.get("locked"):
        st.warning("This invoice is locked. Unlock it on the History page before editing.")
        if st.button("Close"):
            st.rerun()
        return

    st.caption(
        f"Invoice ID {inv['id']} · Filename: {inv.get('original_filename') or '—'}\n\n"
        "Correct any fields the OCR/extraction got wrong — useful for invoices "
        "with misaligned labels/values or blurred scans."
    )

    invoice_number = st.text_input("Invoice #", value=inv.get("invoice_number") or "")
    col1, col2 = st.columns(2)
    with col1:
        vendor_name = st.text_input("Vendor", value=inv.get("vendor_name") or "")
        customer_name = st.text_input("Customer", value=inv.get("customer_name") or "")
        invoice_date_str = st.text_input(
            "Invoice Date (YYYY-MM-DD)", value=inv.get("invoice_date") or ""
        )
        currency = st.text_input("Currency", value=inv.get("currency") or "USD")
    with col2:
        subtotal = st.number_input(
            "Net Amount (Subtotal)", value=float(inv.get("subtotal") or 0.0), step=0.01, format="%.2f"
        )
        tax_amount = st.number_input(
            "VAT", value=float(inv.get("tax_amount") or 0.0), step=0.01, format="%.2f"
        )
        total_amount = st.number_input(
            "Total Amount Due", value=float(inv.get("total_amount") or 0.0), step=0.01, format="%.2f"
        )
        status = st.selectbox(
            "Status",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(inv["status"]) if inv.get("status") in STATUS_OPTIONS else 0,
        )

    computed = subtotal + tax_amount
    if abs(computed - total_amount) > max(0.02 * total_amount, 0.01):
        st.warning(
            f"Net Amount + VAT = {computed:,.2f}, which doesn't match Total Amount Due "
            f"({total_amount:,.2f}). You can still save if that's correct for this invoice."
        )

    c_save, c_cancel = st.columns(2)
    if c_save.button("💾 Save changes", type="primary", use_container_width=True):
        if not invoice_number.strip():
            st.error("Invoice # can't be empty.")
            return
        if not vendor_name.strip():
            st.error("Vendor can't be empty.")
            return
        try:
            parsed_invoice_date = _parse_date(invoice_date_str)
        except ValueError:
            st.error("Date must be in YYYY-MM-DD format.")
            return

        updates = {
            "invoice_number": invoice_number.strip(),
            "vendor_name": vendor_name.strip(),
            "customer_name": customer_name.strip() or None,
            "invoice_date": parsed_invoice_date,
            "currency": currency.strip() or "USD",
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "status": status,
        }
        try:
            update_invoice(inv["id"], updates)
        except DuplicateInvoiceError as e:
            st.error(str(e))
            return
        except InvoiceLockedError as e:
            st.error(str(e))
            return

        st.success("Invoice updated.")
        st.rerun()

    if c_cancel.button("Cancel", use_container_width=True):
        st.rerun()


query = st.text_input("Search by invoice #, vendor, or customer")
status_filter = st.selectbox("Filter by status", ["All"] + STATUS_OPTIONS)

if query:
    invoices = search(query)
else:
    invoices = list_invoices(limit=200, status=None if status_filter == "All" else status_filter)

if not invoices:
    st.info("No invoices found.")
else:
    locked_count = sum(1 for inv in invoices if inv.get("locked"))
    st.caption(
        f"{len(invoices)} invoice(s) — {locked_count} locked. "
        "Click ✏️ Edit to correct a field, then 🔒 Lock once it's confirmed correct."
    )

    header_cols = st.columns(ROW_WIDTHS)
    for col, label in zip(header_cols, COLUMN_LABELS):
        col.markdown(f"**{label}**")

    for inv in invoices:
        locked = bool(inv.get("locked"))
        row_cols = st.columns(ROW_WIDTHS)
        row_cols[0].write(f"{'🔒' if locked else ''} {inv['id']}")
        row_cols[1].write(inv["invoice_number"])
        row_cols[2].write(inv.get("original_filename") or "—")
        row_cols[3].write(inv["vendor_name"])
        row_cols[4].write(inv.get("invoice_date") or "-")
        row_cols[5].write(f"{(inv.get('subtotal') or 0):,.2f}")
        row_cols[6].write(f"{(inv.get('tax_amount') or 0):,.2f}")
        row_cols[7].write(f"{(inv.get('total_amount') or 0):,.2f}")
        row_cols[8].write(inv.get("currency") or "-")
        row_cols[9].write(inv.get("status") or "-")
        row_cols[10].write(f"{(inv.get('confidence_score') or 0) * 100:.0f}%")
        if row_cols[11].button("✏️", key=f"edit_btn_{inv['id']}", disabled=locked, help="Edit"):
            edit_invoice_dialog(inv)
        lock_icon = "🔓" if locked else "🔒"
        if row_cols[12].button(
            lock_icon, key=f"lock_btn_{inv['id']}",
            help="Unlock" if locked else "Lock (confirm this row is correct)",
        ):
            if locked:
                unlock_invoice(inv["id"])
            else:
                lock_invoice(inv["id"])
            st.rerun()

    st.divider()

    selected_id = st.selectbox("View full details for invoice ID", [None] + [inv["id"] for inv in invoices])
    if selected_id:
        detail = get_invoice(selected_id)
        st.subheader(f"Invoice {detail['invoice_number']}")
        c1, c2 = st.columns(2)
        c1.write(f"**Vendor:** {detail['vendor_name']}")
        c1.write(f"**Customer:** {detail.get('customer_name') or '-'}")
        c1.write(f"**Date:** {detail.get('invoice_date') or '-'}")
        c1.write(f"**Filename:** {detail.get('original_filename') or '-'}")
        c2.write(f"**Net Amount:** {(detail.get('subtotal') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**VAT:** {(detail.get('tax_amount') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**Total Amount Due:** {(detail.get('total_amount') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**Status:** {detail.get('status')}")
        c2.write(f"**Confidence:** {(detail.get('confidence_score') or 0) * 100:.0f}%")
        c2.write(f"**Locked:** {'🔒 Yes' if detail.get('locked') else '🔓 No'}")

        b1, b2 = st.columns(2)
        if b1.button("✏️ Edit this invoice", disabled=bool(detail.get("locked")), use_container_width=True):
            edit_invoice_dialog(detail)
        lock_label = "🔓 Unlock this invoice" if detail.get("locked") else "🔒 Lock this invoice"
        if b2.button(lock_label, use_container_width=True):
            if detail.get("locked"):
                unlock_invoice(detail["id"])
            else:
                lock_invoice(detail["id"])
            st.rerun()

        if detail.get("line_items"):
            st.write("**Line Items**")
            st.dataframe(pd.DataFrame(detail["line_items"]), use_container_width=True, hide_index=True)

        with st.expander("Raw OCR text (debug)"):
            st.text(detail.get("raw_text") or "(no OCR text stored for this invoice)")
