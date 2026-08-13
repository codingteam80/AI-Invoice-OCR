"""Streamlit page: browse, search, and drill into processed invoices."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from config.constants import CATEGORY_OPTIONS
from database.repository import DuplicateInvoiceError, InvoiceLockedError
from services.invoice_service import (
    list_invoices, get_invoice, update_invoice, lock_invoice, unlock_invoice, delete_invoice,
)
from ui.components.nav import render_nav, guard_locked_navigation
from utils.file_utils import resolve_source_file
from utils.pdf_utils import pdf_to_images

st.set_page_config(page_title="Invoice History", page_icon="🗂️", layout="wide")

render_nav()
guard_locked_navigation()

st.title("🗂️ Invoice History")

STATUS_OPTIONS = ["processed", "needs_review", "failed", "pending"]

# Full invoice detail (filename, currency, category, net amount, VAT, etc.)
# is already available under "View full details for invoice ID" below, so
# this row table only needs the at-a-glance fields plus the action buttons.
# ID column width is left untouched; the action-button columns are sized
# tight to their icon-only buttons, and the freed-up space is redistributed
# across the remaining data columns.
ROW_WIDTHS = [0.5, 1.3, 1.6, 1.1, 1.3, 1.1, 1.0, 0.5, 0.5, 0.5]
COLUMN_LABELS = [
    "ID", "Invoice #", "Vendor", "Date", "Total Amount Due", "Status", "Confidence", "", "", "",
]


def _show_image(source):
    """st.image()'s width kwarg was renamed use_column_width -> use_container_width
    partway through Streamlit's 1.x line — support whichever this runtime has."""
    try:
        st.image(source, use_container_width=True)
    except TypeError:
        st.image(source, use_column_width=True)


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
        category = st.selectbox(
            "Category",
            CATEGORY_OPTIONS,
            index=CATEGORY_OPTIONS.index(inv["category"]) if inv.get("category") in CATEGORY_OPTIONS else len(CATEGORY_OPTIONS) - 1,
            help="AI-assigned during processing — change it here if it's wrong.",
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
            "category": category,
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


@st.dialog("🗑️ Delete Invoice")
def delete_invoice_dialog(inv: dict):
    st.warning(
        f"Delete invoice **{inv['invoice_number']}** (ID {inv['id']}, "
        f"{inv.get('vendor_name') or 'unknown vendor'})? This can't be undone."
    )
    c_confirm, c_cancel = st.columns(2)
    if c_confirm.button("🗑️ Yes, delete", type="primary", use_container_width=True):
        try:
            delete_invoice(inv["id"])
        except InvoiceLockedError:
            st.error("This invoice is locked. Unlock it first, then delete.")
            return
        except ValueError:
            st.error("Invoice not found — it may have already been deleted.")
            return
        st.success("Invoice deleted.")
        st.rerun()
    if c_cancel.button("Cancel", use_container_width=True):
        st.rerun()


def render_invoice_row_table(invoices: list[dict], key_prefix: str) -> None:
    """Renders the header + per-row Edit/Lock table for a given invoice list."""
    header_cols = st.columns(ROW_WIDTHS)
    for col, label in zip(header_cols, COLUMN_LABELS):
        # "**" + "" + "**" is "****", which Markdown parses as a horizontal
        # rule (4+ asterisks = thematic break) rather than empty bold text —
        # that's what was drawing a line above the button columns.
        col.markdown(f"**{label}**" if label else "")

    for inv in invoices:
        locked = bool(inv.get("locked"))
        row_cols = st.columns(ROW_WIDTHS)
        row_cols[0].write(f"{'🔒' if locked else ''} {inv['id']}")
        row_cols[1].write(inv["invoice_number"])
        row_cols[2].write(inv["vendor_name"])
        row_cols[3].write(inv.get("invoice_date") or "-")
        row_cols[4].write(f"{(inv.get('total_amount') or 0):,.2f} {inv.get('currency') or ''}".strip())
        row_cols[5].write(inv.get("status") or "-")
        row_cols[6].write(f"{(inv.get('confidence_score') or 0) * 100:.0f}%")
        if row_cols[7].button("✏️", key=f"edit_btn_{key_prefix}_{inv['id']}", disabled=locked, help="Edit"):
            edit_invoice_dialog(inv)
        lock_icon = "🔓" if locked else "🔒"
        if row_cols[8].button(
            lock_icon, key=f"lock_btn_{key_prefix}_{inv['id']}",
            help="Unlock" if locked else "Lock (confirm this row is correct)",
        ):
            if locked:
                unlock_invoice(inv["id"])
            else:
                lock_invoice(inv["id"])
            st.rerun()
        if row_cols[9].button(
            "🗑️", key=f"delete_btn_{key_prefix}_{inv['id']}",
            disabled=locked, help="Unlock first to delete" if locked else "Delete",
        ):
            delete_invoice_dialog(inv)


def render_category_tables(invoices: list[dict], scope_key: str) -> None:
    """Splits `invoices` into one table per category (Foods, Office Supplies,
    Furnitures, Others, ...). Status/category/vendor filtering happens one
    level up (see render_filters below) before invoices ever reach here."""
    if not invoices:
        st.info("No invoices for this period.")
        return

    by_category: dict[str, list[dict]] = {}
    for inv in invoices:
        cat = inv.get("category") or "Others"
        by_category.setdefault(cat, []).append(inv)

    # Known categories first (in their fixed order), then any surprises last.
    ordered_categories = [c for c in CATEGORY_OPTIONS if c in by_category]
    ordered_categories += [c for c in by_category if c not in CATEGORY_OPTIONS]

    for i, cat in enumerate(ordered_categories):
        cat_invoices = by_category[cat]
        locked_count = sum(1 for inv in cat_invoices if inv.get("locked"))
        st.markdown(f"#### 🏷️ {cat} — {len(cat_invoices)} invoice(s), {locked_count} locked")

        render_invoice_row_table(cat_invoices, key_prefix=f"{scope_key}_{cat}")
        total = sum(inv.get("total_amount") or 0 for inv in cat_invoices)
        st.caption(f"Subtotal for {cat}: {total:,.2f}")
        # Only between categories, not after the last one — otherwise this
        # divider lands directly next to the page-level divider that comes
        # after render_category_tables() returns, drawing two lines in a row.
        if i < len(ordered_categories) - 1:
            st.divider()


def render_filters(scope_key: str, invoices_pool: list[dict]) -> list[dict]:
    """Renders Filter by status/category/vendor as multiselects — each is a
    searchable text box you can also type into, not just a single-choice
    dropdown — and returns invoices_pool filtered by whatever's selected.
    Options are computed from invoices_pool itself so, e.g., the vendor list
    for the current-month filter only shows vendors seen this month."""
    vendors = sorted({inv["vendor_name"] for inv in invoices_pool if inv.get("vendor_name")})
    c1, c2, c3 = st.columns(3)
    statuses = c1.multiselect("Filter by status", STATUS_OPTIONS, key=f"status_filter_{scope_key}")
    categories = c2.multiselect("Filter by category", CATEGORY_OPTIONS, key=f"category_filter_{scope_key}")
    vendor_sel = c3.multiselect("Filter by vendor", vendors, key=f"vendor_filter_{scope_key}")

    filtered = invoices_pool
    if statuses:
        filtered = [inv for inv in filtered if inv.get("status") in statuses]
    if categories:
        filtered = [inv for inv in filtered if (inv.get("category") or "Others") in categories]
    if vendor_sel:
        filtered = [inv for inv in filtered if inv.get("vendor_name") in vendor_sel]
    return filtered


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


all_invoices = list_invoices(limit=2000, status=None)

if not all_invoices:
    st.info("No invoices found.")
    invoices = []
else:
    by_month: dict[str, list[dict]] = {}
    for inv in all_invoices:
        by_month.setdefault(_month_key(inv), []).append(inv)

    current_key = date.today().strftime("%Y-%m")
    current_pool = by_month.get(current_key, [])

    st.subheader(f"📅 {_month_label(current_key)} — Current Month")
    current_invoices = render_filters("current", current_pool)
    render_category_tables(current_invoices, scope_key="current")

    past_keys = sorted(
        (k for k in by_month if k != current_key and k != "unknown"), reverse=True
    )
    invoices = list(current_invoices)  # feeds the detail viewer below

    if past_keys:
        label_map = {f"{_month_label(k)} ({len(by_month[k])})": k for k in past_keys}
        chosen_label = st.selectbox(
            "📂 View a previous month", ["Select a month..."] + list(label_map.keys())
        )
        if chosen_label != "Select a month...":
            chosen_key = label_map[chosen_label]
            st.subheader(f"📅 {_month_label(chosen_key)}")
            prev_pool = by_month[chosen_key]
            prev_invoices = render_filters(chosen_key, prev_pool)
            render_category_tables(prev_invoices, scope_key=chosen_key)
            invoices += prev_invoices

if invoices:
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
        c1.write(f"**Category:** {detail.get('category') or '-'}")
        c2.write(f"**Net Amount:** {(detail.get('subtotal') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**VAT:** {(detail.get('tax_amount') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**Total Amount Due:** {(detail.get('total_amount') or 0):,.2f} {detail.get('currency')}")
        c2.write(f"**Status:** {detail.get('status')}")
        c2.write(f"**Confidence:** {(detail.get('confidence_score') or 0) * 100:.0f}%")
        c2.write(f"**Locked:** {'🔒 Yes' if detail.get('locked') else '🔓 No'}")

        b1, b2, b3 = st.columns(3)
        if b1.button("✏️ Edit this invoice", disabled=bool(detail.get("locked")), use_container_width=True):
            edit_invoice_dialog(detail)
        lock_label = "🔓 Unlock this invoice" if detail.get("locked") else "🔒 Lock this invoice"
        if b2.button(lock_label, use_container_width=True):
            if detail.get("locked"):
                unlock_invoice(detail["id"])
            else:
                lock_invoice(detail["id"])
            st.rerun()
        if b3.button(
            "🗑️ Delete this invoice", disabled=bool(detail.get("locked")), use_container_width=True
        ):
            delete_invoice_dialog(detail)

        if detail.get("vision_notes"):
            st.warning("🔍 **Vision cross-check flagged possible mismatches:**\n\n"
                       + "\n".join(f"- {line}" for line in detail["vision_notes"].split("\n")))

        with st.expander("🖼️ Invoice image"):
            resolved_file = resolve_source_file(detail.get("source_file"))
            if not resolved_file:
                st.caption("(no image on disk for this invoice)")
            elif resolved_file.suffix.lower() == ".pdf":
                try:
                    pages = pdf_to_images(str(resolved_file))
                except Exception as e:
                    st.caption(f"(couldn't render PDF preview: {e})")
                    pages = []
                if pages:
                    _show_image(pages[0])
                    if len(pages) > 1:
                        st.caption(f"Showing page 1 of {len(pages)}.")
            else:
                _show_image(str(resolved_file))

        with st.expander("Raw OCR text (debug)"):
            st.text(detail.get("raw_text") or "(no OCR text stored for this invoice)")
