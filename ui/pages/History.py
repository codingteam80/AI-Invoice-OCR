"""Streamlit page: browse, search, and drill into processed invoices."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
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
ROW_WIDTHS = [0.4, 0.5, 1.3, 1.6, 1.1, 1.3, 1.1, 1.0, 0.5, 0.5, 0.5]
COLUMN_LABELS = [
    "", "ID", "Invoice #", "Vendor", "Date", "Total Amount Due", "Status", "Confidence", "", "", "",
]


def _show_image(source):
    """st.image()'s width kwarg was renamed use_column_width -> use_container_width
    partway through Streamlit's 1.x line — support whichever this runtime has."""
    try:
        st.image(source, use_container_width=True)
    except TypeError:
        st.image(source, use_column_width=True)


def _render_invoice_image(inv: dict) -> None:
    """Resolves and displays the source file for `inv` — first page only
    for PDFs. Shared by the invoice detail viewer and the Edit Invoice
    dialog, so a user can see the original document while correcting the
    extracted fields next to it.

    When an enhanced (auto-cropped/straightened) version exists — see
    services/invoice_service.py::_generate_enhanced_image() — offers a
    toggle so the person can compare it against the original photo,
    CamScanner-style. Defaults to showing the enhanced version when one
    exists, since that's usually the more useful/readable view; falls
    back to the original silently if the enhanced file has since gone
    missing from disk.
    """
    resolved_enhanced = resolve_source_file(inv.get("enhanced_image_path"))
    resolved_original = resolve_source_file(inv.get("source_file"))

    resolved_file = resolved_original
    if resolved_enhanced:
        # Keyed by invoice id so each invoice's toggle is independent and
        # doesn't leak its state onto a different invoice's widget.
        choice = st.radio(
            "View",
            ["Enhanced (auto-cropped)", "Original"],
            horizontal=True,
            key=f"img_view_choice_{inv.get('id')}",
            label_visibility="collapsed",
        )
        if choice == "Enhanced (auto-cropped)":
            resolved_file = resolved_enhanced

    if not resolved_file:
        st.caption("(no image on disk for this invoice)")
        return
    if resolved_file.suffix.lower() == ".pdf":
        try:
            pages = pdf_to_images(str(resolved_file))
        except Exception as e:
            st.caption(f"(couldn't render PDF preview: {e})")
            return
        if pages:
            _show_image(pages[0])
            if len(pages) > 1:
                st.caption(f"Showing page 1 of {len(pages)}.")
    else:
        _show_image(str(resolved_file))


def _parse_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)  # raises ValueError -> caught by caller


def _request_detail_switch(target_id: int, enter_edit: bool) -> None:
    """Central entry point for 'show invoice `target_id` in the View full
    details section, optionally opening its edit form' — used by the
    row-table ✏️ buttons and the detail section's own Edit button.

    MUST be wired as a button's on_click callback (never called inline
    inside an `if button(...):` block) — it sets detail_selected_id, which
    is the View-full-details selectbox's own widget key. Streamlit forbids
    setting a widget's session_state key after that widget has already
    rendered once in the current script run; on_click callbacks run before
    the next rerun (and its widgets) exist, so they're always safe, no
    matter where on the page the button sits relative to the selectbox."""
    st.session_state["detail_selected_id"] = target_id
    editing_id = st.session_state.get("editing_invoice_id")
    if editing_id is None or editing_id == target_id:
        # No conflicting edit in progress — apply immediately.
        if enter_edit:
            st.session_state["editing_invoice_id"] = target_id
    else:
        # Someone else's edit is open — leave editing_invoice_id alone.
        # The declarative mismatch check will catch this on rerun and show
        # the discard/keep-editing prompt instead of jumping straight in.
        st.session_state["pending_enter_edit"] = enter_edit
    st.session_state["scroll_to_detail"] = True


def _keep_editing_callback(editing_id: int) -> None:
    """on_click callback for the 'Keep editing' button in the mid-edit
    switch guard — reverts the selectbox back to the invoice actually
    being edited. Must be a callback for the same reason as
    _request_detail_switch: this runs after the selectbox has already
    rendered once this run."""
    st.session_state["detail_selected_id"] = editing_id
    st.session_state.pop("pending_enter_edit", None)
    st.session_state["scroll_to_detail"] = True


def _scroll_to_if_flagged(flag_key: str, anchor_id: str) -> None:
    """Streamlit has no built-in scroll-to-element — this is the standard
    workaround: an invisible component iframe that's same-origin with the
    main page, so it can reach into window.parent.document and scroll it.
    Only fires once per request (the flag is popped, not just read)."""
    if st.session_state.pop(flag_key, False):
        components.html(
            f"""<script>
                var el = window.parent.document.getElementById("{anchor_id}");
                if (el) {{ el.scrollIntoView({{behavior: "smooth", block: "start"}}); }}
            </script>""",
            height=0,
        )


def render_edit_form(inv: dict):
    """Inline replacement for the old modal Edit Invoice dialog — renders
    directly in the page flow (not a popup), so it isn't capped to
    st.dialog's two width presets. See _request_detail_switch for how a
    user gets routed here."""
    if inv.get("locked"):
        st.warning("This invoice is locked. Unlock it on the History page before editing.")
        if st.button("Close"):
            st.session_state["editing_invoice_id"] = None
            st.rerun()
        return

    st.subheader(f"✏️ Editing Invoice {inv.get('invoice_number') or inv['id']}")
    st.caption(
        f"Invoice ID {inv['id']} · Filename: {inv.get('original_filename') or '—'}\n\n"
        "Correct any fields the OCR/extraction got wrong — useful for invoices "
        "with misaligned labels/values or blurred scans."
    )

    main_col, image_col = st.columns([2, 1])

    with main_col:
        invoice_number = st.text_input("Invoice #", value=inv.get("invoice_number") or "")
        col1, col2 = st.columns(2)
        with col1:
            vendor_name = st.text_input("Vendor", value=inv.get("vendor_name") or "")
            vendor_address = st.text_input("Vendor Address", value=inv.get("vendor_address") or "")
            vendor_tax_id = st.text_input("Vendor TIN", value=inv.get("vendor_tax_id") or "")
            customer_name = st.text_input("Customer", value=inv.get("customer_name") or "")
            customer_address = st.text_input("Customer Address", value=inv.get("customer_address") or "")
            customer_tax_id = st.text_input("Customer TIN", value=inv.get("customer_tax_id") or "")
            invoice_date_str = st.text_input(
                "Invoice Date (YYYY-MM-DD)", value=inv.get("invoice_date") or ""
            )
            currency = st.text_input("Currency", value=inv.get("currency") or "USD")
        with col2:
            subtotal = st.number_input(
                "Net Amount (Vatable Sales / Subtotal)",
                value=float(inv.get("subtotal") or 0.0), step=0.01, format="%.2f"
            )
            discount = st.number_input(
                "Discount", value=float(inv.get("discount") or 0.0), step=0.01, format="%.2f",
                help="Enter the discount deducted from the invoice total. Leave at 0.00 if there is no discount.",
            )
            tax_amount = st.number_input(
                "VAT", value=float(inv.get("tax_amount") or 0.0), step=0.01, format="%.2f"
            )
            zero_rated_sales = st.number_input(
                "Zero-Rated Sales", value=float(inv.get("zero_rated_sales") or 0.0),
                step=0.01, format="%.2f",
                help="Leave at 0.00 if this invoice has no zero-rated sales column printed on it.",
            )
            vat_exempt_sales = st.number_input(
                "VAT-Exempt Sales", value=float(inv.get("vat_exempt_sales") or 0.0),
                step=0.01, format="%.2f",
                help="Leave at 0.00 if this invoice has no VAT-exempt sales column printed on it.",
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

        computed = subtotal + tax_amount + zero_rated_sales + vat_exempt_sales - discount
        if abs(computed - total_amount) > max(0.02 * total_amount, 0.01):
            st.warning(
                f"Net Amount + VAT + Zero-Rated + VAT-Exempt - Discount = {computed:,.2f}, which doesn't "
                f"match Total Amount Due ({total_amount:,.2f}). You can still save if that's "
                f"correct for this invoice."
            )

        st.markdown("**Items purchased**")
        st.caption(
            "Edit any cell directly. To add a row, start typing in the blank row at the "
            "bottom. To delete a row, select it (checkbox on the left) and press the 🗑️ "
            "icon that appears above the table."
        )
        line_items_df = pd.DataFrame(
            [
                {
                    "Description": li.get("description") or "",
                    "Quantity": float(li.get("quantity") or 0.0),
                    "Unit Price": float(li.get("unit_price") or 0.0),
                    "Total Unit Price": float(li.get("amount") or 0.0),
                }
                for li in (inv.get("line_items") or [])
            ],
            columns=["Description", "Quantity", "Unit Price", "Total Unit Price"],
        )
        edited_line_items = st.data_editor(
            line_items_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key=f"line_items_editor_{inv['id']}",
            column_config={
                "Description": st.column_config.TextColumn("Description"),
                "Quantity": st.column_config.NumberColumn("Quantity", min_value=0.0, step=1.0, format="%.2f"),
                "Unit Price": st.column_config.NumberColumn("Unit Price", min_value=0.0, step=0.01, format="%.2f"),
                "Total Unit Price": st.column_config.NumberColumn("Total Unit Price", min_value=0.0, step=0.01, format="%.2f"),
            },
        )
        # New blank rows from "dynamic" mode come back with NaN, not 0/"" —
        # fillna before summing or reading values below, or the sum/save
        # step would silently propagate NaN into the database.
        edited_line_items = edited_line_items.fillna(
            {"Description": "", "Quantity": 0.0, "Unit Price": 0.0, "Total Unit Price": 0.0}
        )
        items_sum = edited_line_items["Total Unit Price"].sum() if not edited_line_items.empty else 0.0
        st.caption(f"Sum of Total Unit Price: {items_sum:,.2f}")

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

            new_line_items = []
            for _, row in edited_line_items.iterrows():
                description = str(row["Description"]).strip()
                if not description:
                    continue  # blank template row from "+" that was never filled in
                new_line_items.append({
                    "description": description,
                    "quantity": float(row["Quantity"]),
                    "unit_price": float(row["Unit Price"]),
                    "amount": float(row["Total Unit Price"]),
                })

            updates = {
                "invoice_number": invoice_number.strip(),
                "vendor_name": vendor_name.strip(),
                "vendor_address": vendor_address.strip() or None,
                "vendor_tax_id": vendor_tax_id.strip() or None,
                "customer_name": customer_name.strip() or None,
                "customer_address": customer_address.strip() or None,
                "customer_tax_id": customer_tax_id.strip() or None,
                "invoice_date": parsed_invoice_date,
                "currency": currency.strip() or "USD",
                "subtotal": subtotal,
                "tax_amount": tax_amount,
                "discount": discount or None,
                "zero_rated_sales": zero_rated_sales or None,
                "vat_exempt_sales": vat_exempt_sales or None,
                "total_amount": total_amount,
                "status": status,
                "category": category,
                "line_items": new_line_items,
            }
            try:
                update_invoice(inv["id"], updates)
            except DuplicateInvoiceError as e:
                st.error(str(e))
                return
            except InvoiceLockedError as e:
                st.error(str(e))
                return

            st.session_state["editing_invoice_id"] = None
            st.success("Invoice updated.")
            st.rerun()

        if c_cancel.button("Cancel", use_container_width=True):
            st.session_state["editing_invoice_id"] = None
            st.rerun()

    with image_col:
        st.markdown("**🖼️ Invoice Image**")
        _render_invoice_image(inv)


@st.dialog("🗑️ Delete Invoice")
def delete_invoice_dialog(inv: dict, scroll_to_prev_month: bool = False):
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
        if scroll_to_prev_month:
            st.session_state["scroll_to_prev_month"] = True
        st.success("Invoice deleted.")
        st.rerun()
    if c_cancel.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("🗑️ Delete Selected Invoices")
def bulk_delete_dialog(selected: list[dict]):
    st.warning(
        f"Delete **{len(selected)}** selected invoice(s)? This can't be undone.\n\n"
        + "\n".join(f"- {inv['invoice_number']} ({inv.get('vendor_name') or 'unknown vendor'})" for inv in selected[:20])
        + ("\n- ..." if len(selected) > 20 else "")
    )
    c_confirm, c_cancel = st.columns(2)
    if c_confirm.button(f"🗑️ Yes, delete {len(selected)}", type="primary", use_container_width=True):
        failures = []
        for inv in selected:
            try:
                delete_invoice(inv["id"])
            except (InvoiceLockedError, ValueError) as e:
                failures.append(f"{inv['invoice_number']}: {e}")
        if failures:
            st.error("Some invoices couldn't be deleted:\n" + "\n".join(f"- {f}" for f in failures))
        else:
            st.success(f"Deleted {len(selected)} invoice(s).")
            st.rerun()
    if c_cancel.button("Cancel", use_container_width=True):
        st.rerun()


def render_invoice_row_table(invoices: list[dict], key_prefix: str, scope_key: str) -> None:
    """Renders the header + per-row select/Edit/Lock table for a given
    invoice list. `scope_key` (shared across categories within the same
    month section) is what the page-level "select all" checkbox and
    "delete selected" button key off of — `key_prefix` stays
    category-specific so edit/lock/delete button keys stay unique."""
    header_cols = st.columns(ROW_WIDTHS)
    for col, label in zip(header_cols, COLUMN_LABELS):
        # "**" + "" + "**" is "****", which Markdown parses as a horizontal
        # rule (4+ asterisks = thematic break) rather than empty bold text —
        # that's what was drawing a line above the button columns.
        col.markdown(f"**{label}**" if label else "")

    for inv in invoices:
        locked = bool(inv.get("locked"))
        row_cols = st.columns(ROW_WIDTHS)
        row_cols[0].checkbox(
            "", key=f"sel_{scope_key}_{inv['id']}", disabled=locked,
            label_visibility="collapsed", help="Unlock first to select" if locked else "Select",
        )
        row_cols[1].write(f"{'🔒' if locked else ''} {inv['id']}")
        row_cols[2].write(inv["invoice_number"])
        row_cols[3].write(inv["vendor_name"])
        row_cols[4].write(inv.get("invoice_date") or "-")
        row_cols[5].write(f"{(inv.get('total_amount') or 0):,.2f} {inv.get('currency') or ''}".strip())
        row_cols[6].write(inv.get("status") or "-")
        row_cols[7].write(f"{(inv.get('confidence_score') or 0) * 100:.0f}%")
        row_cols[8].button(
            "✏️", key=f"edit_btn_{key_prefix}_{inv['id']}", disabled=locked, help="Edit",
            on_click=_request_detail_switch, args=(inv["id"],), kwargs={"enter_edit": True},
        )
        lock_icon = "🔓" if locked else "🔒"
        if row_cols[9].button(
            lock_icon, key=f"lock_btn_{key_prefix}_{inv['id']}",
            help="Unlock" if locked else "Lock (confirm this row is correct)",
        ):
            if locked:
                unlock_invoice(inv["id"])
            else:
                lock_invoice(inv["id"])
            if scope_key != "current":
                st.session_state["scroll_to_prev_month"] = True
            st.rerun()
        if row_cols[10].button(
            "🗑️", key=f"delete_btn_{key_prefix}_{inv['id']}",
            disabled=locked, help="Unlock first to delete" if locked else "Delete",
        ):
            delete_invoice_dialog(inv, scroll_to_prev_month=(scope_key != "current"))


def _select_all_callback(scope_key: str, invoice_ids: list[int], value: bool) -> None:
    for inv_id in invoice_ids:
        st.session_state[f"sel_{scope_key}_{inv_id}"] = value


def render_category_tables(invoices: list[dict], scope_key: str) -> None:
    """Splits `invoices` into one table per category (Foods, Office Supplies,
    Furnitures, Others, ...). Status/category/vendor filtering happens one
    level up (see render_filters below) before invoices ever reach here.

    Also renders a "select all" checkbox and "delete selected" button
    covering every unlocked invoice currently shown for this scope (i.e.
    across all its category tables, not just one) — see suggestion #1
    (bulk select/delete)."""
    if not invoices:
        st.info("No invoices for this period.")
        return

    unlocked_ids = [inv["id"] for inv in invoices if not inv.get("locked")]
    sel_c1, sel_c2 = st.columns([1, 3])
    sel_c1.checkbox(
        f"Select all ({len(unlocked_ids)})",
        key=f"select_all_{scope_key}",
        disabled=not unlocked_ids,
        on_change=lambda: _select_all_callback(
            scope_key, unlocked_ids, st.session_state[f"select_all_{scope_key}"]
        ),
    )
    selected = [inv for inv in invoices if st.session_state.get(f"sel_{scope_key}_{inv['id']}")]
    if sel_c2.button(
        f"🗑️ Delete selected ({len(selected)})", disabled=not selected, key=f"bulk_delete_{scope_key}",
    ):
        bulk_delete_dialog(selected)

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

        render_invoice_row_table(cat_invoices, key_prefix=f"{scope_key}_{cat}", scope_key=scope_key)
        total = sum(inv.get("total_amount") or 0 for inv in cat_invoices)
        st.caption(f"Sum of Total Amount Due: {total:,.2f}")
        # Only between categories, not after the last one — otherwise this
        # divider lands directly next to the page-level divider that comes
        # after render_category_tables() returns, drawing two lines in a row.
        if i < len(ordered_categories) - 1:
            st.divider()


def render_filters(scope_key: str, invoices_pool: list[dict]) -> list[dict]:
    """Renders a free-text search box plus Filter by status/category/vendor
    multiselects — each multiselect is a searchable text box you can also
    type into, not just a single-choice dropdown — and returns
    invoices_pool filtered by whatever's selected. Options are computed
    from invoices_pool itself so, e.g., the vendor list for the
    current-month filter only shows vendors seen this month.

    Each month section (current month, and whichever previous month is
    picked below) gets its OWN search bar/filters, scoped by `scope_key` —
    searching "current" never touches what's selected for a previous month.
    """
    search_term = st.text_input(
        "🔍 Search invoice #, vendor, or customer",
        key=f"search_{scope_key}",
        placeholder="e.g. INV-2026 or Jollibee",
    )

    vendors = sorted({inv["vendor_name"] for inv in invoices_pool if inv.get("vendor_name")})
    c1, c2, c3 = st.columns(3)
    statuses = c1.multiselect("Filter by status", STATUS_OPTIONS, key=f"status_filter_{scope_key}")
    categories = c2.multiselect("Filter by category", CATEGORY_OPTIONS, key=f"category_filter_{scope_key}")
    vendor_sel = c3.multiselect("Filter by vendor", vendors, key=f"vendor_filter_{scope_key}")

    filtered = invoices_pool
    if search_term.strip():
        needle = search_term.strip().lower()
        filtered = [
            inv for inv in filtered
            if needle in (inv.get("invoice_number") or "").lower()
            or needle in (inv.get("vendor_name") or "").lower()
            or needle in (inv.get("customer_name") or "").lower()
        ]
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
        st.markdown('<div id="prev-month-anchor"></div>', unsafe_allow_html=True)
        # Guard against a stale selection if the previously-chosen month's
        # last invoice just got deleted, leaving it no longer in past_keys.
        if st.session_state.get("prev_month_selected") not in ([None] + past_keys):
            st.session_state["prev_month_selected"] = None

        chosen_key = st.selectbox(
            "📂 View a previous month",
            [None] + past_keys,
            # format_func means the STORED value is the stable month key
            # ("2026-06"), while only the DISPLAYED label embeds the live
            # invoice count — so deleting/locking a row (which changes that
            # count) can't break the identity match and reset the selection
            # back to "Select a month...".
            format_func=lambda k: "Select a month..." if k is None else f"{_month_label(k)} ({len(by_month[k])})",
            key="prev_month_selected",
        )
        if chosen_key:
            st.subheader(f"📅 {_month_label(chosen_key)}")
            prev_pool = by_month[chosen_key]
            prev_invoices = render_filters(chosen_key, prev_pool)
            render_category_tables(prev_invoices, scope_key=chosen_key)
            invoices += prev_invoices

if invoices:
    st.divider()
    st.markdown('<div id="invoice-detail-anchor"></div>', unsafe_allow_html=True)

    valid_ids = [inv["id"] for inv in invoices]
    selected_id = st.selectbox(
        "View full details for invoice ID", [None] + valid_ids, key="detail_selected_id"
    )
    editing_id = st.session_state.get("editing_invoice_id")

    if editing_id is not None and selected_id != editing_id:
        # The dropdown (directly, or via a row-table ✏️ button on a
        # different invoice) was changed while an edit was still open —
        # don't silently discard it.
        st.warning(
            f"⚠️ You have unsaved edits open for invoice ID {editing_id}. "
            "Switching now will discard them."
        )
        wc1, wc2 = st.columns(2)
        if wc1.button("Discard changes and switch", type="primary", key="discard_switch"):
            st.session_state["editing_invoice_id"] = (
                selected_id if st.session_state.pop("pending_enter_edit", False) else None
            )
            st.session_state["scroll_to_detail"] = True
            st.rerun()
        wc2.button("Keep editing", key="keep_editing", on_click=_keep_editing_callback, args=(editing_id,))
    elif selected_id:
        detail = get_invoice(selected_id)

        if editing_id == selected_id:
            render_edit_form(detail)
        else:
            st.subheader(f"Invoice {detail['invoice_number']}")
            c1, c2 = st.columns(2)
            c1.write(f"**Vendor:** {detail['vendor_name']}")
            c1.write(f"**Vendor Address:** {detail.get('vendor_address') or '-'}")
            c1.write(f"**Vendor TIN:** {detail.get('vendor_tax_id') or '-'}")
            c1.write(f"**Customer:** {detail.get('customer_name') or '-'}")
            c1.write(f"**Customer Address:** {detail.get('customer_address') or '-'}")
            c1.write(f"**Customer TIN:** {detail.get('customer_tax_id') or '-'}")
            c1.write(f"**Date:** {detail.get('invoice_date') or '-'}")
            c1.write(f"**Filename:** {detail.get('original_filename') or '-'}")
            c1.write(f"**Category:** {detail.get('category') or '-'}")
            _subtotal = detail.get("subtotal")
            _subtotal_display = (
                f"{_subtotal:,.2f} {detail.get('currency')}" if _subtotal is not None
                else "— (not extracted)"
            )
            c2.write(f"**Net Amount (Vatable Sales):** {_subtotal_display}")
            c2.write(f"**Discount:** {(detail.get('discount') or 0):,.2f} {detail.get('currency')}")
            c2.write(f"**VAT:** {(detail.get('tax_amount') or 0):,.2f} {detail.get('currency')}")
            if detail.get("zero_rated_sales") is not None:
                c2.write(f"**Zero-Rated Sales:** {detail['zero_rated_sales']:,.2f} {detail.get('currency')}")
            if detail.get("vat_exempt_sales") is not None:
                c2.write(f"**VAT-Exempt Sales:** {detail['vat_exempt_sales']:,.2f} {detail.get('currency')}")
            c2.write(f"**Total Amount Due:** {(detail.get('total_amount') or 0):,.2f} {detail.get('currency')}")
            c2.write(f"**Status:** {detail.get('status')}")
            c2.write(f"**Confidence:** {(detail.get('confidence_score') or 0) * 100:.0f}%")
            c2.write(f"**Locked:** {'🔒 Yes' if detail.get('locked') else '🔓 No'}")

            line_items = detail.get("line_items") or []
            if line_items:
                st.markdown("**Items purchased:**")
                st.dataframe(
                    [
                        {
                            "Item": li.get("description") or "-",
                            "Qty": li.get("quantity") or 0,
                            "Unit Price": f"{(li.get('unit_price') or 0):,.2f}",
                            "Total Unit Price": f"{(li.get('amount') or 0):,.2f}",
                        }
                        for li in line_items
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
                items_sum = sum(li.get("amount") or 0 for li in line_items)
                st.caption(f"Sum of Total Unit Price: {items_sum:,.2f} {detail.get('currency') or ''}".strip())
            else:
                st.caption("No individual line items were extracted for this invoice.")

            b1, b2, b3 = st.columns(3)
            b1.button(
                "✏️ Edit this invoice", disabled=bool(detail.get("locked")), use_container_width=True,
                on_click=_request_detail_switch, args=(detail["id"],), kwargs={"enter_edit": True},
            )
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
                _render_invoice_image(detail)

            with st.expander("Raw OCR text (debug)"):
                st.text(detail.get("raw_text") or "(no OCR text stored for this invoice)")

    _scroll_to_if_flagged("scroll_to_detail", "invoice-detail-anchor")
    _scroll_to_if_flagged("scroll_to_prev_month", "prev-month-anchor")
