"""Streamlit page: export invoices to Excel / CSV / PDF."""
import sys
import html
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from config.constants import CATEGORY_OPTIONS
from services.invoice_service import list_invoices, list_custom_categories
from services.export_service import export_invoices
from services.audit_service import log_action
from exports.chart import generate_chart
from ui.components.nav import render_nav, guard_locked_navigation
from ui.components.ai_search import render_ai_search
from services.search_service import apply_search_plan

st.set_page_config(page_title="Reports & Export", page_icon="📁", layout="wide")

render_nav()
guard_locked_navigation()

st.title("📁 Reports & Export")


def _scroll_to_export_if_requested() -> None:
    """Return the viewport to Generate Export after Streamlit reruns/download clicks."""
    if st.session_state.pop("scroll_to_generate_export", False):
        components.html(
            """<script>
                function backToExport() {
                    var el = window.parent.document.getElementById("generate-export-anchor");
                    if (el) { el.scrollIntoView({behavior: "auto", block: "center"}); }
                }
                backToExport();
                setTimeout(backToExport, 80);
                setTimeout(backToExport, 250);
                setTimeout(backToExport, 600);
            </script>""",
            height=0,
        )


def _finish_confirmed_export_download(filename: str, invoice_count: int) -> None:
    log_action("DOWNLOAD EXPORT", "export", None, f"Downloaded {filename} ({invoice_count} invoice(s))")
    st.session_state.pop("report_export_confirmation", None)
    st.session_state["scroll_to_generate_export"] = True


def _record_export_download(filename: str, invoice_count: int) -> None:
    log_action("DOWNLOAD EXPORT", "export", None, f"Downloaded {filename} ({invoice_count} invoice(s))")
    st.session_state["scroll_to_generate_export"] = True


def _month_key(inv: dict) -> str:
    """'YYYY-MM' bucket based on Date Uploaded."""
    raw = inv.get("date_uploaded") or (inv.get("created_at") or "")[:10]
    return raw[:7] if raw else "unknown"


def _invoice_month_key(inv: dict) -> str:
    """'YYYY-MM' bucket based on Invoice Date."""
    raw = str(inv.get("invoice_date") or "")
    return raw[:7] if len(raw) >= 7 else "unknown"


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
        .tsukiden-report-table-wrap {{
            width: 100%; max-height: 460px; overflow: auto;
            border: 1px solid rgba(128,128,128,.25); border-radius: 6px;
        }}
        .tsukiden-report-table {{ width: 100%; border-collapse: collapse; font-size: 1.15rem; }}
        .tsukiden-report-table th {{
            position: sticky; top: 0; z-index: 1;
            background: #1F4E78; color: white; text-align: left; padding: 0.5rem 0.6rem;
        }}
        .tsukiden-report-table td {{ padding: 0.5rem 0.6rem; border-bottom: 1px solid #E4E9E3; }}
        </style>
        <div class="tsukiden-report-table-wrap">
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
invoice_month_keys = sorted(
    {_invoice_month_key(inv) for inv in all_invoices if _invoice_month_key(inv) != "unknown"}, reverse=True
)
invoice_month_label_map = {_month_label(k): k for k in invoice_month_keys}

vendors = sorted({inv["vendor_name"] for inv in all_invoices if inv.get("vendor_name")})
custom_categories = list_custom_categories()
all_categories = CATEGORY_OPTIONS + custom_categories

ai_plan = render_ai_search(
    page="reports",
    key="reports",
    placeholder="e.g. Show unlocked Transportation invoices from June to October above 10,000 pesos",
    context={
        "categories": all_categories,
        "statuses": sorted({str(inv.get("status")) for inv in all_invoices if inv.get("status")}),
        "vendors": vendors[:100],
    },
)

f1, f2, f3, f4 = st.columns(4)
month_choice = f1.multiselect("Filter by month(Date Uploaded)", list(month_label_map.keys()))
invoice_month_choice = f2.multiselect("Filter by Month(Invoice Date)", list(invoice_month_label_map.keys()))
category_choice = f3.multiselect("Filter by category", all_categories)
vendor_choice = f4.multiselect("Filter by vendor", vendors)

fmt = st.radio("Export format", ["xlsx", "csv", "pdf"], horizontal=True)

invoices = all_invoices
if month_choice:
    chosen_month_keys = {month_label_map[m] for m in month_choice}
    invoices = [inv for inv in invoices if _month_key(inv) in chosen_month_keys]
if invoice_month_choice:
    chosen_invoice_month_keys = {invoice_month_label_map[m] for m in invoice_month_choice}
    invoices = [inv for inv in invoices if _invoice_month_key(inv) in chosen_invoice_month_keys]
if category_choice:
    invoices = [inv for inv in invoices if (inv.get("category") or "Others") in category_choice]
if vendor_choice:
    invoices = [inv for inv in invoices if inv.get("vendor_name") in vendor_choice]

invoices = apply_search_plan(invoices, ai_plan, "reports")

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
                "Invoice Date": inv.get("invoice_date") or "-",
                "Date Uploaded": inv.get("date_uploaded") or (inv.get("created_at") or "")[:10] or "-",
                "Category": inv.get("category") or "-",
                "Total Amount Due": f"{(inv.get('total_amount') or 0):,.2f} {inv.get('currency') or ''}".strip(),
                "Locked": "🔒 Yes" if inv.get("locked") else "🔓 No",
            }
            for inv in invoices
        ]
    )
    _render_preview_table(table_df)

    # Vatable Sales and VAT sums are intentionally not shown on this page —
    # only Total Amount Due is. All three are still included in every
    # exported file (see exports/common.py: invoice_totals_row).
    total_sum = sum(inv.get("total_amount") or 0 for inv in invoices)
    st.metric("Total Amount Due (sum)", f"{total_sum:,.2f}")

st.subheader("📊 Chart")
chart_path_for_export = None
current_ids = tuple(sorted(inv["id"] for inv in invoices))

# A graph belongs to the exact invoice selection that created it. As soon as
# the filters change that selection, remove the old graph automatically.
stored_ids = st.session_state.get("report_chart_ids")
if stored_ids is not None and stored_ids != current_ids:
    st.session_state.pop("report_chart_path", None)
    st.session_state.pop("report_chart_ids", None)

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
        st.session_state["report_chart_ids"] = current_ids
        st.rerun()

    stored_chart_path = st.session_state.get("report_chart_path")
    if stored_chart_path and Path(stored_chart_path).exists():
        with c1:
            if st.button("🗑️ Remove Graph", type="secondary"):
                st.session_state.pop("report_chart_path", None)
                st.session_state.pop("report_chart_ids", None)
                st.rerun()
        with c2:
            try:
                st.image(stored_chart_path, use_container_width=True)
            except TypeError:
                st.image(stored_chart_path, use_column_width=True)
        chart_path_for_export = stored_chart_path

st.markdown('<div id="generate-export-anchor"></div>', unsafe_allow_html=True)

if total_count > 0:
    if all_locked:
        st.success(f"✅ All {total_count} invoice(s) in this selection are locked and ready to export.")
    else:
        unlocked_count = total_count - locked_count
        st.warning(
            f"⚠️ {unlocked_count} of {total_count} selected invoice(s) are not locked. "
            "You can still export them, but the export may contain information that has not been reviewed and confirmed."
        )
        st.page_link("pages/History.py", label="Review or lock invoices in History", icon="🗂️")

selection_signature = (current_ids, fmt)
pending = st.session_state.get("report_export_confirmation")
if pending and pending != selection_signature:
    st.session_state.pop("report_export_confirmation", None)
    st.session_state.pop("prepared_export_key", None)
    st.session_state.pop("prepared_export_name", None)
    st.session_state.pop("prepared_export_bytes", None)
    pending = None


def _generate_and_offer_export() -> None:
    paths = export_invoices(invoices, fmt=fmt, chart_path=chart_path_for_export)
    st.success(f"Exported {len(invoices)} invoice(s).")
    for p in paths:
        with open(p, "rb") as f:
            st.download_button(
                label=f"Download {Path(p).name}",
                data=f.read(),
                file_name=Path(p).name,
                key=f"download_{Path(p).name}",
                on_click=_record_export_download, args=(Path(p).name, len(invoices)),
            )
    if fmt == "csv" and chart_path_for_export:
        st.caption("CSV is plain text, so the graph is not embedded in the CSV file.")


if st.button("Generate Export", type="primary", disabled=not invoices):
    st.session_state["scroll_to_generate_export"] = True
    if all_locked:
        st.session_state.pop("report_export_confirmation", None)
        _generate_and_offer_export()
    else:
        st.session_state["report_export_confirmation"] = selection_signature
        st.rerun()

if st.session_state.get("report_export_confirmation") == selection_signature and not all_locked:
    st.warning(
        "Some selected invoices are not locked. Continue only if you want to export the current, unconfirmed values."
    )
    # Prepare the file while the confirmation is visible so the user's single
    # confirmation click can also be the browser download click.
    prepared_key = (selection_signature, chart_path_for_export)
    if st.session_state.get("prepared_export_key") != prepared_key:
        prepared_paths = export_invoices(invoices, fmt=fmt, chart_path=chart_path_for_export)
        p = Path(prepared_paths[0])
        st.session_state["prepared_export_key"] = prepared_key
        st.session_state["prepared_export_name"] = p.name
        st.session_state["prepared_export_bytes"] = p.read_bytes()
    confirm_col, cancel_col, _ = st.columns([2.2, 1, 4])
    with confirm_col:
        st.download_button(
            label="Continue Export & Download",
            data=st.session_state["prepared_export_bytes"],
            file_name=st.session_state["prepared_export_name"],
            mime={"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv", "pdf": "application/pdf"}[fmt],
            type="primary",
            on_click=_finish_confirmed_export_download,
            args=(st.session_state["prepared_export_name"], len(invoices)),
        )
    if cancel_col.button("Cancel"):
        st.session_state.pop("report_export_confirmation", None)
        st.session_state.pop("prepared_export_key", None)
        st.session_state.pop("prepared_export_name", None)
        st.session_state.pop("prepared_export_bytes", None)
        st.rerun()

# Prepare an isolated print document so sidebar/buttons are not printed.
from services.print_report_service import report_signature, build_report_pdf, pdf_print_html
print_signature = report_signature(invoices, chart_path_for_export)
if st.session_state.get("print_report_signature") != print_signature:
    st.session_state.pop("print_report_html", None)
    st.session_state.pop("print_report_pdf", None)
    st.session_state["print_report_signature"] = print_signature
st.subheader("Print report")
print_confirmed = all_locked
if invoices and not all_locked:
    print_confirmed = st.checkbox("I understand that this printout includes unlocked, unconfirmed invoices.", key=f"print_confirm_{print_signature}")
if st.button("Prepare print preview", disabled=not invoices or not print_confirmed):
    try:
        pdf = build_report_pdf(invoices, chart_path_for_export)
        st.session_state["print_report_html"] = pdf_print_html(pdf)
        st.session_state["print_report_pdf"] = pdf
        log_action("PRINT PREVIEW", "report", None, f"Prepared print preview for {len(invoices)} invoice(s); IDs: " + ",".join(str(inv["id"]) for inv in invoices))
    except Exception as exc:
        st.error(f"Could not prepare the print report: {exc}")
if st.session_state.get("print_report_html") and print_confirmed:
    st.caption("This is the same A3 landscape layout as Generate Export → PDF. Click Print / choose printer below. Disable browser headers/footers; A4 printers may need Fit to page.")
    components.html(st.session_state["print_report_html"], height=650, scrolling=True)

# Run after the export controls exist in the DOM so the target anchor can be found.
_scroll_to_export_if_requested()
