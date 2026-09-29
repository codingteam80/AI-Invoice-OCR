"""Streamlit page: upload one or more invoices and process them."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from config.settings import settings
from services.upload_service import handle_upload, UploadError
from services.invoice_service import process_invoice_file
from ai.vision_budget import vision_batch
from ui.components.nav import render_nav

st.set_page_config(page_title="Upload Invoices", page_icon="📤", layout="wide")

st.session_state.setdefault("upload_in_progress", False)
st.session_state.setdefault("pending_upload_files", None)
st.session_state.setdefault("upload_results", None)
st.session_state.setdefault("upload_batch_seconds", None)

render_nav()

st.title("📤 Upload Invoices")

locked = st.session_state.upload_in_progress

if locked:
    st.info("⏳ Processing invoices — please wait. Other tabs are locked until this finishes.")

st.caption("OCR engine is chosen automatically — PaddleOCR for printed invoices, TrOCR for handwritten ones.")
override_enabled = st.checkbox("Manually override handwriting detection", disabled=locked)
force_handwritten = None
if override_enabled:
    force_handwritten = st.radio(
        "Treat these files as:", ["Printed", "Handwritten"], horizontal=True, disabled=locked
    ) == "Handwritten"

enhance_image = st.checkbox(
    "Auto-crop & enhance photos (CamScanner-style)",
    value=settings.ENHANCE_IMAGE_ENABLED,
    disabled=locked,
    help=(
        "Straightens and crops each uploaded photo to the receipt's edges "
        "and boosts contrast, so History can show a cleaned-up version "
        "alongside the original. Doesn't affect OCR accuracy — this is "
        "purely a nicer view for you. Skipped for PDFs."
    ),
)

source = st.radio("Invoice source", ["Upload files", "Scan invoice"], horizontal=True, disabled=locked)
files = []
pending_input = None
if source == "Upload files":
    files = st.file_uploader(
        "Drop invoice images or PDFs here",
        type=["png", "jpg", "jpeg", "tiff", "bmp", "pdf"],
        accept_multiple_files=True, disabled=locked,
    )
    if files and not locked and st.button("Process Invoices", type="primary"):
        pending_input = [(f.name, f.getvalue()) for f in files]
else:
    from ui.components.scanner_input import render_scanner_input
    pending_input = render_scanner_input(locked)

if pending_input and not locked:
    st.session_state.pending_upload_files = pending_input
    st.session_state.upload_results = None
    st.session_state.force_handwritten = force_handwritten
    st.session_state.enhance_image = enhance_image
    st.session_state.upload_in_progress = True
    st.rerun()

if locked and st.session_state.pending_upload_files:
    pending = st.session_state.pending_upload_files
    progress = st.progress(0.0, text="Starting...")
    results = []
    batch_start = time.perf_counter()

    with vision_batch():
        for i, (name, data) in enumerate(pending):
            progress.progress(i / len(pending), text=f"Processing {name}...")
            file_start = time.perf_counter()
            try:
                saved_path = handle_upload(data, name)
            except UploadError as e:
                results.append({
                    "success": False, "error": str(e), "file": name,
                    "elapsed_seconds": time.perf_counter() - file_start,
                })
                continue

            # A single uploaded file (especially a multi-page PDF) can contain
            # more than one invoice — process_invoice_file() now returns a
            # list, one entry per invoice found, instead of a single dict.
            file_results = process_invoice_file(
                saved_path,
                force_handwritten=st.session_state.get("force_handwritten"),
                original_filename=name,
                enhance_image=st.session_state.get("enhance_image"),
            )
            elapsed = time.perf_counter() - file_start
            for result in file_results:
                result.setdefault("file", result.get("original_filename", name))
                result["elapsed_seconds"] = elapsed
                results.append(result)

    progress.progress(1.0, text="Done")

    # Unlock navigation and rerun so the sidebar (and this page) reflect the
    # finished state.
    st.session_state.upload_results = results
    st.session_state.upload_batch_seconds = time.perf_counter() - batch_start
    st.session_state.pending_upload_files = None
    st.session_state.upload_in_progress = False
    st.rerun()

if st.session_state.upload_results:
    if st.session_state.get("upload_batch_seconds") is not None:
        st.caption(f"⏱️ Total time: {st.session_state.upload_batch_seconds:.1f}s for {len(st.session_state.upload_results)} invoice(s).")
    for r in st.session_state.upload_results:
        elapsed = r.get("elapsed_seconds")
        elapsed_suffix = f" · ⏱️ {elapsed:.1f}s" if elapsed is not None else ""
        if r.get("duplicate"):
            st.warning(f"⚠️ {r['file']}: {r.get('error')}{elapsed_suffix}")
        elif r.get("success"):
            if "VISION_VERIFICATION_INCOMPLETE:" in (r.get("vision_notes") or ""):
                st.warning(f"{r['file']}: Image verification was incomplete. OCR/text results were saved; review them against the original invoice.")
            invoice_id = r.get("invoice_id")
            vendor = r.get("vendor_name") or "-"
            total_text = f"{r.get('total_amount', 0):,.2f} {r.get('currency', '')}".strip()
            status_text = r.get("status") or "-"
            ocr_text = r.get("ocr_engine_used") or "-"
            time_text = f"{elapsed:.1f}s" if elapsed is not None else "-"
            review_text = " · ⚠️ Review required" if status_text == "needs_review" else ""
            # Keep the saved-invoice summary and its processing details in ONE
            # clickable control. This avoids making users open a separate
            # Processing details expander just to see the result before
            # navigating to History.
            row_label = (
                f"✅ {r['file']} — {r.get('invoice_number', 'N/A')}\n\n"
                f"Vendor: {vendor} · Total: {total_text} · Status: {status_text} · "
                f"OCR: {ocr_text} · Processing time: {time_text}{review_text}"
            )
            help_lines = [
                "Open this saved invoice in History.",
                f"Vendor: {vendor}",
                f"Total: {total_text}",
                f"Status: {status_text}",
                f"OCR engine used: {ocr_text}",
                f"Processing time: {time_text}",
            ]
            if r.get("vision_notes"):
                help_lines.append("Vision cross-check notes:")
                help_lines.extend(r["vision_notes"].split("\n"))
            if st.button(
                row_label,
                key=f"open_saved_invoice_{invoice_id}",
                use_container_width=True,
                help="\n".join(help_lines),
            ):
                st.session_state["detail_selected_id"] = invoice_id
                st.session_state["editing_invoice_id"] = None
                st.session_state["scroll_to_detail"] = True
                st.switch_page("pages/History.py")
        else:
            st.error(f"❌ {r['file']}: {r.get('error')}{elapsed_suffix}")
elif not files and not locked:
    st.info(f"Supported formats: PNG, JPG, TIFF, BMP, PDF · Max size: {settings.MAX_UPLOAD_MB} MB")
