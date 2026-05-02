"""To-Do page — per-trip task list with Gmail assignment and checkbox completion."""

import streamlit as st
import pandas as pd

from utils.sheets import add_task, delete_task, mark_task_done
from utils.config import cfg
from views._shared import (
    trip_picker, parse_link, cached_tasks,
    priority_badge, priority_sort_key,
)

PRIORITIES = ["Normal", "Medium", "High"]


def _priority_label(desc: str, priority: str) -> str:
    """Return markdown label with colored Material-icon priority badge."""
    badge = priority_badge(priority)
    if priority == "High":
        return f"{badge} :red[**{desc}**]"
    if priority == "Medium":
        return f"{badge} :orange[**{desc}**]"
    return f"{badge} {desc}"


def _add_task_form(trip_id: str, assignees: list[str]) -> None:
    with st.form("add_task_form", clear_on_submit=True, border=True):
        st.subheader("New task", anchor=False)
        desc     = st.text_input("Task", placeholder="e.g. Book airport transfer")
        notes    = st.text_area("Description (optional)", placeholder="Extra details…", height=70)
        due_date = st.date_input("Due date (optional)", value=None)
        assigned = st.selectbox("Assign to", ["Unassigned"] + assignees)
        priority = st.selectbox("Priority", PRIORITIES)
        links    = st.text_input(
            "Links (optional)",
            placeholder="Label | https://example.com , Label2 | https://...",
        )

        s, c = st.columns(2)
        with s:
            saved = st.form_submit_button("Save", icon=":material/save:", use_container_width=True)
        with c:
            cancelled = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if saved:
            if not desc.strip():
                st.error("Task description is required.")
            else:
                due_str = str(due_date) if due_date else ""
                if add_task(trip_id, desc.strip(), assigned, priority,
                            links.strip(), notes=notes.strip(), due_date=due_str):
                    st.cache_data.clear()
                    st.session_state["adding_task"] = False
                    st.rerun()
        if cancelled:
            st.session_state["adding_task"] = False
            st.rerun()


def _delete_task_cb(task_id: str) -> None:
    delete_task(task_id)
    st.cache_data.clear()


def render() -> None:
    # Title + Add task button in the same row
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="bottom"):
        st.header(":material/checklist: To Do", anchor=False)
        add_clicked = st.button(
            "Add task",
            icon=":material/add:",
            type="primary",
            key="add_task_btn",
        )

    trip_row = trip_picker()
    if trip_row is None:
        return

    trip_id   = str(trip_row["trip_id"])
    assignees = [e for e in cfg.approved_emails if e]
    me        = st.user.get("email", "")

    if add_clicked:
        st.session_state["adding_task"] = not st.session_state.get("adding_task", False)

    if st.session_state.get("adding_task", False):
        _add_task_form(trip_id, assignees)

    tasks_df = cached_tasks(trip_id)

    if tasks_df.empty:
        st.info("No tasks yet — add one above.", icon=":material/info:")
        return

    # Sort: priority first (High → Medium → Normal), then me-first, then alphabetical
    tasks_df = tasks_df.copy()
    tasks_df["_sort_p"] = tasks_df["priority"].apply(priority_sort_key)
    tasks_df["_sort_a"] = tasks_df["assigned_to"].apply(
        lambda a: ("0" if str(a) == me else "1") + str(a).lower()
    )
    tasks_df = (
        tasks_df
        .sort_values(["_sort_p", "_sort_a"])
        .drop(columns=["_sort_p", "_sort_a"])
        .reset_index(drop=True)
    )

    active_df = tasks_df[~tasks_df["done"]].copy() if "done" in tasks_df.columns else tasks_df.copy()
    done_df   = tasks_df[tasks_df["done"]].copy()  if "done" in tasks_df.columns else pd.DataFrame()

    def _render_task_row(task, is_done: bool) -> None:
        task_id  = str(task.get("task_id", ""))
        desc     = str(task.get("description", "")).strip()
        notes    = str(task.get("notes", "") or "").strip()
        due_date = str(task.get("due_date", "") or "").strip()
        assignee = str(task.get("assigned_to", "Unassigned")).strip()
        priority = str(task.get("priority", "Normal")).strip() or "Normal"
        links    = str(task.get("links", "") or "").strip()

        with st.container(border=True):
            if is_done:
                st.markdown(f":gray[~~{desc}~~]")
            else:
                checked = st.checkbox(
                    _priority_label(desc, priority),
                    key=f"task_chk_{task_id}",
                )
                if checked:
                    mark_task_done(task_id)
                    st.cache_data.clear()
                    st.rerun()

            if notes and not is_done:
                st.caption(notes)
            if due_date and not is_done:
                st.caption(f":material/event: {due_date}")

            with st.container(horizontal=True, horizontal_alignment="right",
                              vertical_alignment="center"):
                if assignee and assignee != "Unassigned":
                    label = ":gray[" + assignee + "]" if is_done else f":material/person: {assignee}"
                    st.caption(label)
                st.button(
                    "",
                    icon=":material/delete:",
                    type="tertiary",
                    key=f"task_del_{task_id}",
                    help="Delete task",
                    on_click=_delete_task_cb,
                    args=(task_id,),
                )
            if links and not is_done:
                for raw in [l.strip() for l in links.split(",") if l.strip()]:
                    label, url = parse_link(raw)
                    st.link_button(label, url, icon=":material/link:", use_container_width=True)

    for _, task in active_df.iterrows():
        _render_task_row(task, is_done=False)

    if not done_df.empty:
        st.caption(":material/check_circle: Completed")
        for _, task in done_df.iterrows():
            _render_task_row(task, is_done=True)
