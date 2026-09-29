"""Streamlit page (admin only): create and manage login accounts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
from config.constants import MIN_PASSWORD_LENGTH, USER_ROLES
from services.audit_service import audit_utc_to_local
from services.user_service import (
    UserError, create_user, list_users, reset_password, set_active, set_role,
)
from ui.components.auth import current_user, is_admin
from ui.components.nav import render_nav, guard_locked_navigation

st.set_page_config(page_title="Users", page_icon="👥", layout="wide")
render_nav()
guard_locked_navigation()

st.title("👥 Users")
if not is_admin():
    st.error("Only administrators can manage user accounts.")
    st.stop()

me = current_user()
flash = st.session_state.pop("users_flash", None)
if flash:
    st.success(flash)


def _run(action, success_msg: str) -> None:
    try:
        action()
    except UserError as exc:
        st.error(str(exc))
    else:
        st.session_state["users_flash"] = success_msg
        st.rerun()


st.subheader("Add account")
with st.form("add_user_form", clear_on_submit=True):
    c1, c2, c3 = st.columns([1.2, 1.2, 0.8])
    new_name = c1.text_input("Username")
    new_pw = c2.text_input(f"Temporary password (min {MIN_PASSWORD_LENGTH})", type="password")
    new_role = c3.selectbox("Role", USER_ROLES, index=USER_ROLES.index("user"))
    if st.form_submit_button("Create account", type="primary"):
        _run(lambda: create_user(new_name, new_pw, new_role),
             f"Account '{new_name.strip().lower()}' created. They must set their own password at first login.")

st.divider()
st.subheader("Accounts")


def _fmt_dt(value) -> str:
    local = audit_utc_to_local(value)
    return local.strftime("%Y-%m-%d %I:%M %p") if local else "Never"


for u in list_users():
    is_me = u["id"] == me["id"]
    with st.container(border=True):
        top = st.columns([1.4, 0.8, 0.9, 1.4])
        top[0].markdown(f"**{u['username']}**" + (" _(you)_" if is_me else ""))
        top[1].write(u["role"].capitalize())
        top[2].write("🟢 Active" if u["is_active"] else "⚪ Deactivated")
        top[3].caption(f"Last login: {_fmt_dt(u['last_login_at'])}")

        a, b, c = st.columns([1, 1, 2])
        if u["is_active"]:
            if a.button("Deactivate", key=f"deact_{u['id']}", disabled=is_me):
                _run(lambda u=u: set_active(u["id"], False, me["id"]), f"'{u['username']}' deactivated.")
        else:
            if a.button("Reactivate", key=f"react_{u['id']}"):
                _run(lambda u=u: set_active(u["id"], True, me["id"]), f"'{u['username']}' reactivated.")

        other_role = "user" if u["role"] == "admin" else "admin"
        if b.button(f"Make {other_role}", key=f"role_{u['id']}", disabled=is_me):
            _run(lambda u=u, r=other_role: set_role(u["id"], r, me["id"]), f"'{u['username']}' is now {other_role}.")

        with c.popover("Reset password"):
            pw = st.text_input("New temporary password", type="password", key=f"pw_{u['id']}")
            if st.button("Save", key=f"pwsave_{u['id']}"):
                _run(lambda u=u, pw=pw: reset_password(u["id"], pw),
                     f"Password reset for '{u['username']}'. They must change it at next login.")
