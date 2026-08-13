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
from config.constants import APP_NAME, APP_VERSION, COMPANY_NAME, COPYRIGHT_YEAR

# (path relative to the main script ui/streamlit_app.py, label, icon)
_PAGES = [
    ("streamlit_app.py", "Home", "🧾"),
    ("pages/Upload.py", "Upload", "📤"),
    ("pages/Dashboard.py", "Dashboard", "📊"),
    ("pages/History.py", "History", "🗂️"),
    ("pages/Reports.py", "Reports", "📁"),
]

_UPLOAD_PAGE = "pages/Upload.py"

_GLOBAL_FONT_CSS = """
<style>
/* Bigger base font size across the whole app. Most of Streamlit's built-in
   typography and spacing is defined in rem, which is relative to this, so
   raising it here scales text, buttons, inputs, and tables together rather
   than growing text out of proportion with everything else. Browsers
   default html to 16px; 18px is roughly a 12% bump. */
html {
    font-size: 18px;
}

/* A few Streamlit elements pin their own font-size in px rather than
   inheriting rem, so nudge those specifically too. */
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stDataFrame"] div,
[data-testid="stMetricValue"],
.stButton button,
.stSelectbox label,
.stMultiSelect label,
.stTextInput label {
    font-size: 1rem !important;
}

/* CSS can only see the browser viewport's width, not the screen's actual
   diagonal size — an 11" laptop and a 15" laptop can report the same
   viewport width depending on resolution/OS scaling. This is the closest
   proxy available: narrower windows (small/high-DPI screens, or a window
   that isn't maximized) get a further bump. */
@media (max-width: 900px) {
    html {
        font-size: 20px;
    }
}
</style>
"""

_SIDEBAR_CSS = """
<style>
[data-testid="stSidebarNav"] { display: none; }

/* Stretch the sidebar's own content area to full height and lay it out as
   a column, so the footer (margin-top: auto) sticks to the bottom instead
   of just trailing after the nav links. */
[data-testid="stSidebarUserContent"] {
    display: flex;
    flex-direction: column;
    min-height: calc(100vh - 3rem);
}
.tsukiden-sidebar-footer {
    margin-top: auto;
    padding-top: 1rem;
    border-top: 1px solid rgba(26, 35, 31, 0.12);
    font-size: 0.75rem;
    line-height: 1.5;
    color: #5b6b60;
}
</style>
"""


def is_upload_locked() -> bool:
    return bool(st.session_state.get("upload_in_progress", False))


def render_nav() -> None:
    """Render the sidebar: title, nav links (disabled but Upload while an
    upload is in progress), and a footer pinned to the bottom."""
    st.markdown(_GLOBAL_FONT_CSS, unsafe_allow_html=True)
    st.markdown(_SIDEBAR_CSS, unsafe_allow_html=True)
    locked = is_upload_locked()

    with st.sidebar:
        st.markdown(f"## 🧾 {APP_NAME}")
        if locked:
            st.warning("⏳ Upload in progress — other tabs are locked until it finishes.")
        for path, label, icon in _PAGES:
            st.page_link(path, label=label, icon=icon, disabled=locked and path != _UPLOAD_PAGE)

        st.markdown(
            f"""
            <div class="tsukiden-sidebar-footer">
            © {COPYRIGHT_YEAR} {COMPANY_NAME}<br>
            {APP_NAME} System • Version {APP_VERSION}
            </div>
            """,
            unsafe_allow_html=True,
        )


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
