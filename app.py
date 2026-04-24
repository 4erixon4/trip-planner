"""
Trip Planner — native Streamlit, sidebar navigation, Material icons.
"""

import streamlit as st

from utils.auth import require_auth
from utils.sheets import ensure_sheets_exist
from views import build, travel, expenses, todo

st.set_page_config(
    page_title="Trip Planner",
    page_icon=":material/park:",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ── Auth (persists across refreshes via st.user) ────────────────────────────
user = require_auth()

# ── Sheets bootstrap (creates "Trips" worksheet if needed) ──────────────────
ensure_sheets_exist()

# ── Sidebar: greeting + logout + navigation ──────────────────────────────────
with st.sidebar:
    name = st.user.get("name") or st.user.get("email", "")
    st.caption(f":material/person: {name}")
    st.button(
        "Log out",
        icon=":material/logout:",
        on_click=st.logout,
        use_container_width=True,
        key="sidebar_logout",
    )
    st.divider()

# ── Multi-page navigation (sidebar) ─────────────────────────────────────────
pg = st.navigation(
    [
        st.Page(build.render,    title="Build",    icon=":material/edit_note:",    url_path="build",    default=True),
        st.Page(travel.render,   title="Travel",   icon=":material/explore:",      url_path="travel"),
        st.Page(expenses.render, title="Expenses", icon=":material/receipt_long:", url_path="expenses"),
        st.Page(todo.render,     title="To Do",    icon=":material/checklist:",    url_path="todo"),
    ],
    position="sidebar",
)
pg.run()
