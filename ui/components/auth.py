"""Local login gate with an acknowledged browser-cookie bridge.

Every page calls require_login through render_nav. Cookies restore the login
after a new WebSocket connection; explicit sign-in always routes to Home.
"""
import secrets
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from config.constants import APP_NAME, AUTH_SESSION_KEY, MIN_PASSWORD_LENGTH
from services.audit_service import log_action
from services.user_service import UserError, authenticate, bootstrap_auth, change_own_password
from services.browser_session_service import (
    COOKIE_NAME, create_browser_session, browser_session_user, revoke_browser_session,
)

_HIDE_SIDEBAR_CSS = "<style>[data-testid='stSidebar'], [data-testid='collapsedControl'] {display: none;}</style>"
_cookie_bridge = components.declare_component(
    "invoice_login_cookie", path=str(Path(__file__).with_name("auth_cookie")),
)


@st.cache_resource
def _bootstrap_once() -> bool:
    bootstrap_auth()
    return True


def current_user() -> dict | None:
    return st.session_state.get(AUTH_SESSION_KEY)


def is_admin() -> bool:
    user = current_user()
    return bool(user and user.get("role") == "admin")


def _request(action: str, token: str = "") -> dict:
    return {"action": action, "token": token, "request_id": secrets.token_hex(12)}


def _finish_cookie_transition() -> None:
    request = st.session_state.setdefault("_auth_cookie_request", _request("read"))
    reply = _cookie_bridge(cookie_name=COOKIE_NAME, **request, key="auth_cookie_bridge", default=None)
    matching = isinstance(reply, dict) and reply.get("request_id") == request["request_id"]
    transition = st.session_state.get("_auth_transition")
    if transition:
        if not matching:
            st.caption("Signing out…" if transition == "logout" else "Signing in…")
            st.stop()
        if not reply.get("ok"):
            revoke_browser_session(request.get("token"))
            st.error("The browser blocked the login cookie. Allow cookies for this local app, then reload and sign in.")
            st.stop()
        st.session_state.pop("_auth_transition", None)
        st.session_state["_auth_restored"] = True
        st.session_state["_auth_cookie_request"] = _request("read")
        if transition != "logout":
            token = request["token"]
            user = browser_session_user(token)
            if user is None:
                st.error("This login is no longer valid. Reload to sign in again.")
                st.stop()
            st.session_state["_auth_token"] = token
            st.session_state[AUTH_SESSION_KEY] = user
            if transition == "login":
                log_action("LOGIN", "user", user["id"], f"'{user['username']}' signed in", username=user["username"])
        st.switch_page("streamlit_app.py")
    if not st.session_state.get("_auth_restored"):
        if not matching:
            st.caption("Restoring sign-in…")
            st.stop()
        token = reply.get("token")
        user = browser_session_user(token)
        if user:
            st.session_state["_auth_token"] = token
            st.session_state[AUTH_SESSION_KEY] = user
        st.session_state["_auth_restored"] = True


def _begin_login(user: dict, transition: str = "login") -> None:
    old_token = st.session_state.get("_auth_token")
    revoke_browser_session(old_token)
    token = create_browser_session(user["id"])
    # A fresh login never inherits another account's results, pending files,
    # history selection, form values, or edit state.
    st.session_state.clear()
    st.session_state["_auth_cookie_request"] = _request("write", token)
    st.session_state["_auth_transition"] = transition
    st.rerun()


def _render_login() -> None:
    st.markdown(_HIDE_SIDEBAR_CSS, unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(f"## 🧾 {APP_NAME}")
        st.caption("Sign in to continue.")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            user = authenticate(username, password)
            if user is None:
                st.error("Wrong username or password, or the account is deactivated.")
            else:
                _begin_login(user)


def _render_forced_password_change(user: dict) -> None:
    st.markdown(_HIDE_SIDEBAR_CSS, unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown("## 🔑 Set a new password")
        st.info(f"Hi {user['username']}, you must choose your own password before continuing.")
        with st.form("force_change_form"):
            old = st.text_input("Current password", type="password")
            new = st.text_input(f"New password (min {MIN_PASSWORD_LENGTH} characters)", type="password")
            again = st.text_input("Repeat new password", type="password")
            submitted = st.form_submit_button("Save password", type="primary", use_container_width=True)
        if submitted:
            if new != again:
                st.error("The new passwords don't match.")
            else:
                try:
                    updated = change_own_password(user["id"], old, new)
                except UserError as exc:
                    st.error(str(exc))
                else:
                    _begin_login(updated, transition="password_change")
        if st.button("Cancel and sign out"):
            logout()


def logout(log: bool = True) -> None:
    user = current_user()
    if user and log:
        log_action("LOGOUT", "user", user["id"], f"'{user['username']}' signed out", username=user["username"])
    revoke_browser_session(st.session_state.get("_auth_token"))
    st.session_state.clear()
    st.session_state["_auth_cookie_request"] = _request("clear")
    st.session_state["_auth_transition"] = "logout"
    st.rerun()


def require_login() -> dict:
    _bootstrap_once()
    _finish_cookie_transition()
    # Revalidate on each rerun so old tabs cannot revive a revoked login.
    user = browser_session_user(st.session_state.get("_auth_token"))
    if user is None:
        st.session_state.pop(AUTH_SESSION_KEY, None)
        _render_login()
        st.stop()
    st.session_state[AUTH_SESSION_KEY] = user
    if user.get("must_change_password"):
        _render_forced_password_change(user)
        st.stop()
    return user


def render_account_box(locked: bool = False) -> None:
    user = current_user()
    if not user:
        return
    role = "Admin" if user["role"] == "admin" else "User"
    st.markdown(f"👤 **{user['username']}** · {role}")
    if st.button("Sign out", key="sidebar_logout", disabled=locked, use_container_width=True):
        logout()
