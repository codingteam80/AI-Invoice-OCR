"""Shared sidebar navigation.

Streamlit's built-in multipage nav (auto-generated from ui/pages/) can't be
disabled programmatically, so we hide it via CSS and render our own
st.page_link-based nav instead. That lets us grey out / disable every page
except Upload while an upload batch is being processed, so the user can't
click away from "Upload" mid-run.

Every page in the app should call `render_nav()` near the top (right after
`st.set_page_config`). Every page *except* Upload should also call
`guard_locked_navigation()` right after that — it's a defense-in-depth check
for direct URL navigation / browser back-button, which `disabled=True` on a
page_link can't prevent by itself.
"""
import streamlit as st

# (path relative to the main script ui/streamlit_app.py, label, icon)
_PAGES = [
    ("streamlit_app.py", "Home", "🧾"),
    ("pages/Upload.py", "Upload", "📤"),
    ("pages/Dashboard.py", "Dashboard", "📊"),
    ("pages/History.py", "History", "🗂️"),
    ("pages/Reports.py", "Reports", "📁"),
]

_UPLOAD_PAGE = "pages/Upload.py"

_HIDE_DEFAULT_NAV_CSS = """
<style>
[data-testid="stSidebarNav"] { display: none; }
</style>
"""


def is_upload_locked() -> bool:
    return bool(st.session_state.get("upload_in_progress", False))


def render_nav() -> None:
    """Render the sidebar nav, disabling every page but Upload while locked."""
    st.markdown(_HIDE_DEFAULT_NAV_CSS, unsafe_allow_html=True)
    locked = is_upload_locked()

    with st.sidebar:
        if locked:
            st.warning("⏳ Upload in progress — other tabs are locked until it finishes.")
        for path, label, icon in _PAGES:
            st.page_link(path, label=label, icon=icon, disabled=locked and path != _UPLOAD_PAGE)


def guard_locked_navigation() -> None:
    """Call at the top of every non-Upload page, right after render_nav().

    If an upload is in progress, stop the page from rendering any further
    and point the user back to Upload — this covers direct URL navigation
    or the browser back/forward buttons, which a disabled sidebar link
    alone doesn't prevent.
    """
    if is_upload_locked():
        st.warning(
            "An upload is currently being processed. This page is locked until "
            "it finishes — please wait on the Upload tab."
        )
        st.page_link(_UPLOAD_PAGE, label="Go to Upload", icon="📤")
        st.stop()
