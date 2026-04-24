"""
Authentication via Streamlit's built-in st.user / st.login / st.logout.
Session persists across page refreshes automatically via signed cookie.
Requires streamlit >= 1.42 and Authlib, plus an [auth] block in secrets.toml.
"""

import streamlit as st
from utils.config import cfg


def require_auth():
    if not st.user.is_logged_in:
        _login_screen()
        st.stop()

    if st.user.email not in cfg.approved_emails:
        st.error(f"Access denied — {st.user.email} is not on the approved list.")
        st.button("Log out", on_click=st.logout, icon=":material/logout:")
        st.stop()

    return st.user


def _login_screen() -> None:
    for _ in range(6):
        st.write("")

    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("<h1 style='text-align:center'>🌴 Trip Planner</h1>", unsafe_allow_html=True)
        st.write("")
        if st.button(
            "Continue with Google",
            type="primary",
            icon=":material/login:",
            use_container_width=True,
            key="login_btn",
        ):
            st.login("google")
