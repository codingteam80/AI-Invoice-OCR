"""Streamlit page: append-only user activity log."""
import html
from datetime import datetime
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import streamlit as st
from services.log_export_service import export_logs
from services.print_report_service import report_signature, pdf_print_html
import streamlit.components.v1 as components
from services.audit_service import list_logs, current_app_username, log_action
from ui.components.nav import render_nav, guard_locked_navigation
from ui.components.ai_search import render_ai_search
from services.search_service import apply_search_plan

st.set_page_config(page_title="Activity Log", page_icon="📋", layout="wide")
render_nav(); guard_locked_navigation()
st.title("📋 Activity Log")
st.caption(f"Signed in as: {current_app_username()} · Times shown in this PC's local time · Records are newest first.")


def _display_details(value) -> str:
    """Render both new and legacy multi-field audit details one field per line."""
    text = str(value or "")
    # v1.67 stored multi-field changes with a pipe separator. Keep old log
    # records readable after the upgrade without rewriting historical rows.
    text = text.replace(" | ", "\n")
    # Put the invoice identifier on its own line when field transitions follow.
    if text.startswith("Invoice #") and ": " in text and "→" in text:
        prefix, remainder = text.split(": ", 1)
        text = f"{prefix}:\n{remainder}"
    return text


def _render_log_table(rows: list[dict]) -> None:
    """Wide, scrollable activity table with most horizontal space for Details."""
    body_rows = []
    for row in rows:
        dt = row.get("date_time")
        dt_text = dt.strftime("%Y-%m-%d %I:%M:%S %p") if hasattr(dt, "strftime") else str(dt or "")
        details = html.escape(_display_details(row.get("details"))).replace("\n", "<br>")
        cells = [
            html.escape(dt_text),
            html.escape(str(row.get("user") or "")),
            html.escape(str(row.get("action") or "")),
            html.escape(str(row.get("entity_type") or "")),
            html.escape(str(row.get("entity_id") or "")),
            details,
        ]
        body_rows.append(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        )

    table_html = f"""
    <style>
      .audit-log-wrap {{
        width: 100%; max-height: 460px; overflow: auto;
        border: 1px solid rgba(128,128,128,.25); border-radius: 6px;
      }}
      table.audit-log {{
        width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 1rem;
      }}
      table.audit-log col.col-datetime {{ width: 13%; }}
      table.audit-log col.col-user {{ width: 8%; }}
      table.audit-log col.col-action {{ width: 10%; }}
      table.audit-log col.col-type {{ width: 7%; }}
      table.audit-log col.col-id {{ width: 5%; }}
      table.audit-log col.col-details {{ width: 57%; }}
      table.audit-log th, table.audit-log td {{
        padding: 7px 8px; text-align: left; vertical-align: top;
        border-bottom: 1px solid rgba(128,128,128,.20);
        white-space: normal; overflow-wrap: anywhere;
      }}
      table.audit-log th {{
        position: sticky; top: 0; z-index: 1;
        background: var(--background-color, white);
        font-weight: 600;
      }}
      table.audit-log td:nth-child(6) {{ line-height: 1.45; }}
    </style>
    <div class="audit-log-wrap">
      <table class="audit-log">
        <colgroup>
          <col class="col-datetime"><col class="col-user"><col class="col-action">
          <col class="col-type"><col class="col-id"><col class="col-details">
        </colgroup>
        <thead><tr>
          <th>Date &amp; Time</th><th>User</th><th>Action</th><th>Type</th><th>ID</th><th>Details</th>
        </tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>
    """
    st.markdown(table_html, unsafe_allow_html=True)


def _log_month_key(row: dict) -> str:
    """Return the audit-record month as ``YYYY-MM``."""
    raw = row.get("date_time")
    if hasattr(raw, "strftime"):
        return raw.strftime("%Y-%m")
    text = str(raw or "")
    return text[:7] if len(text) >= 7 else "unknown"


def _month_label(month_key: str) -> str:
    try:
        year, month = month_key.split("-")
        return date(int(year), int(month), 1).strftime("%B %Y")
    except (ValueError, AttributeError):
        return "Unknown date"


def _filter_logs(rows: list[dict]) -> list[dict]:
    """Filter Activity Log rows by month and the useful searchable columns.

    Date & Time is represented as a month multiselect, matching the Reports
    page's month-filter behavior. Details remains visible in the table but is
    intentionally not a filter because its free-form audit text is primarily
    for review rather than record selection.
    """
    month_keys = sorted(
        {_log_month_key(r) for r in rows if _log_month_key(r) != "unknown"},
        reverse=True,
    )
    month_label_map = {_month_label(key): key for key in month_keys}
    users = sorted({str(r.get("user") or "") for r in rows if r.get("user")})
    actions = sorted({str(r.get("action") or "") for r in rows if r.get("action")})
    types = sorted({str(r.get("entity_type") or "") for r in rows if r.get("entity_type")})

    r1c1, r1c2, r1c3 = st.columns(3)
    month_filter = r1c1.multiselect("Filter by Month", list(month_label_map.keys()))
    user_filter = r1c2.multiselect("Filter User", users)
    action_filter = r1c3.multiselect("Filter Action", actions)

    r2c1, r2c2 = st.columns(2)
    type_filter = r2c1.multiselect("Filter Type", types)
    id_filter = r2c2.text_input("Filter ID", placeholder="e.g. 15")

    filtered = rows
    if month_filter:
        chosen_month_keys = {month_label_map[label] for label in month_filter}
        filtered = [r for r in filtered if _log_month_key(r) in chosen_month_keys]
    if user_filter:
        filtered = [r for r in filtered if str(r.get("user") or "") in user_filter]
    if action_filter:
        filtered = [r for r in filtered if str(r.get("action") or "") in action_filter]
    if type_filter:
        filtered = [r for r in filtered if str(r.get("entity_type") or "") in type_filter]
    if id_filter.strip():
        needle = id_filter.strip().lower()
        filtered = [r for r in filtered if needle in str(r.get("entity_id") or "").lower()]
    return filtered


def _render_log_download(rows):
    # A content signature avoids showing a print preview from older filters/data.
    signature = report_signature(rows)
    if st.session_state.get("log_output_signature") != signature:
        st.session_state["log_output_signature"] = signature
        st.session_state.pop("log_print_pdf", None)
        st.session_state.pop("log_print_html", None)
    st.caption(f"Downloads and printing include all {len(rows)} record(s) matching the current filters and search.")
    fmt = st.radio("Log download format", ["xlsx", "csv", "pdf"], horizontal=True)
    st.download_button("Download filtered logs", data=export_logs(rows, fmt),
                       file_name=f"activity_logs_{datetime.now():%Y%m%d_%H%M%S}.{fmt}",
                       mime={"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}[fmt],
                       on_click=log_action, args=("DOWNLOAD LOGS", "log", None, f"Downloaded {len(rows)} filtered log(s) as {fmt}; log IDs: " + ",".join(str(r["id"]) for r in rows)))
    if st.button("Prepare log print preview"):
        try:
            pdf = export_logs(rows, "pdf")
            st.session_state["log_print_html"] = pdf_print_html(pdf, "Activity Log")
            st.session_state["log_print_pdf"] = pdf
            log_action("PRINT PREVIEW", "log", None, f"Prepared preview for {len(rows)} filtered log(s)")
        except Exception as exc:
            st.error(f"Could not prepare the log print preview: {exc}")
    if st.session_state.get("log_print_pdf"):
        st.caption("Print / choose printer below. Use the existing PDF export to download a copy. Use landscape and disable browser headers/footers.")
        components.html(st.session_state["log_print_html"], height=650, scrolling=True)


logs = list_logs(limit=None)
if not logs:
    st.info("No user activity has been recorded yet.")
else:
    ai_plan = render_ai_search(
        page="log",
        key="log",
        placeholder="e.g. Show EDIT actions from September by user Juan",
        context={
            "users": sorted({str(r.get("user")) for r in logs if r.get("user")}),
            "actions": sorted({str(r.get("action")) for r in logs if r.get("action")}),
            "types": sorted({str(r.get("entity_type")) for r in logs if r.get("entity_type")}),
        },
    )
    filtered_logs = _filter_logs(logs)
    filtered_logs = apply_search_plan(filtered_logs, ai_plan, "log")
    st.caption(f"Showing {len(filtered_logs)} of {len(logs)} log record(s).")
    if filtered_logs:
        _render_log_table(filtered_logs)
        _render_log_download(filtered_logs)
    else:
        st.session_state.pop("log_print_pdf", None)
        st.session_state.pop("log_print_html", None)
        st.info("No activity-log records match the selected filters.")
