"""
Trip Planner — native Streamlit, sidebar navigation, Material icons.
"""

import streamlit as st

from utils.auth import require_auth
from utils.sheets import ensure_sheets_exist, _sb
from utils.images import ensure_bucket_public
from views import build, travel, expenses, todo, equipment, analyze

st.set_page_config(
    page_title="Trip Planner",
    page_icon=":material/park:",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ── Auth (persists across refreshes via st.user) ────────────────────────────
user = require_auth()

# ── One-time startup tasks (cached per process — not re-run on every rerun) ──
@st.cache_resource(show_spinner=False)
def _startup() -> str | None:
    """Returns an error string on failure, or None on success."""
    ensure_sheets_exist()
    try:
        _sb().table("trips").select("trip_id").limit(1).execute()
    except Exception as exc:
        return str(exc)
    ensure_bucket_public()
    return None

_startup_err = _startup()
if _startup_err:
    st.error(f"**Supabase connection failed:** {_startup_err}")
    st.stop()

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
_todo_page       = st.Page(todo.render,      title="To Do",     icon=":material/checklist:",    url_path="todo")
_build_page      = st.Page(build.render,     title="Build",     icon=":material/edit_note:",    url_path="build",    default=True)
_travel_page     = st.Page(travel.render,    title="Travel",    icon=":material/explore:",      url_path="travel")
_expenses_page   = st.Page(expenses.render,  title="Expenses",  icon=":material/receipt_long:", url_path="expenses")
_equipment_page  = st.Page(equipment.render, title="Equipment", icon=":material/luggage:",      url_path="equipment")
_analyze_page    = st.Page(analyze.render,   title="Analyze",   icon=":material/troubleshoot:", url_path="analyze")

pg = st.navigation(
    [_build_page, _travel_page, _expenses_page, _todo_page, _equipment_page, _analyze_page],
    position="sidebar",
)

if st.session_state.pop("goto_todo", False):
    st.switch_page(_todo_page)

pg.run()
