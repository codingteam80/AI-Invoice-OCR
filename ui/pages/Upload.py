"""Streamlit page: upload one or more invoices and process them."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from config.settings import settings
from services.upload_service import handle_upload, UploadError
from services.invoice_service import process_invoice_file
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

files = st.file_uploader(
    "Drop invoice images or PDFs here",
    type=["png", "jpg", "jpeg", "tiff", "bmp", "pdf"],
    accept_multiple_files=True,
    disabled=locked,
)

if files and not locked and st.button("Process Invoices", type="primary"):
    # Snapshot the uploaded bytes now, then lock navigation and rerun so the
    # sidebar renders in its locked state *before* the (potentially slow)
    # processing loop below actually starts.
    st.session_state.pending_upload_files = [(f.name, f.read()) for f in files]
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
            with st.expander(f"✅ {r['file']} — {r.get('invoice_number', 'N/A')}{elapsed_suffix}", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Vendor", r.get("vendor_name", "-"))
                c2.metric("Total", f"{r.get('total_amount', 0):,.2f} {r.get('currency', '')}")
                c3.metric("Final Confidence", f"{(r.get('confidence_score') or 0)*100:.0f}%")
                c4.metric("Status", r.get("status", "-"))
                st.caption("Final Confidence estimates support for the final reconciled information using completeness, OCR quality, validation, and accounting/line-item consistency.")
                st.caption(f"OCR engine used: `{r.get('ocr_engine_used', '-')}`")
                if elapsed is not None:
                    st.caption(f"Uploaded and processed in {elapsed:.2f} seconds.")
                if r.get("status") == "needs_review":
                    st.warning("This invoice needs manual review — low confidence or data mismatch.")
                if r.get("vision_notes"):
                    st.warning("🔍 **Vision cross-check flagged possible mismatches:**\n\n"
                               + "\n".join(f"- {line}" for line in r["vision_notes"].split("\n")))
        else:
            st.error(f"❌ {r['file']}: {r.get('error')}{elapsed_suffix}")
elif not files and not locked:
    st.info(f"Supported formats: PNG, JPG, TIFF, BMP, PDF · Max size: {settings.MAX_UPLOAD_MB} MB")
