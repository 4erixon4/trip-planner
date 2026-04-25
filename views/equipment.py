"""Equipment page — per-person packing checklist, persisted in Supabase."""

import streamlit as st
import pandas as pd

from utils.sheets import get_equipment, add_equipment_item, toggle_equipment_item, delete_equipment_item
from utils.config import cfg
from views._shared import trip_picker


@st.cache_data(ttl=30, show_spinner=False)
def _cached_equipment(trip_id: str, owner: str) -> pd.DataFrame:
    return get_equipment(trip_id, owner)


def _tab_label(email: str) -> str:
    """Derive a clean display name from an email address."""
    name = email.split("@")[0]          # e.g. "4eerikson"
    name = name.lstrip("0123456789")    # strip leading digits → "eerikson"
    return name.capitalize() if name else email


def _owner_tab(trip_id: str, owner: str) -> None:
    items_df = _cached_equipment(trip_id, owner)

    # Separate checked / unchecked
    if not items_df.empty and "checked" in items_df.columns:
        items_df["checked"] = items_df["checked"].fillna(False).astype(bool)
        active_df = items_df[~items_df["checked"]].reset_index(drop=True)
        done_df   = items_df[items_df["checked"]].reset_index(drop=True)
    else:
        active_df = items_df.reset_index(drop=True) if not items_df.empty else pd.DataFrame()
        done_df   = pd.DataFrame()

    def _item_row(row, is_checked: bool) -> None:
        item_id = str(row["item_id"])
        desc    = str(row.get("description", "")).strip()

        with st.container(border=True):
            with st.container(
                horizontal=True,
                vertical_alignment="center",
                horizontal_alignment="distribute",
            ):
                if is_checked:
                    st.markdown(f":gray[~~{desc}~~]")
                else:
                    ticked = st.checkbox(desc, value=False, key=f"eq_chk_{item_id}")
                    if ticked:
                        toggle_equipment_item(item_id, True)
                        st.cache_data.clear()
                        st.rerun()

                if st.button(
                    "",
                    icon=":material/delete:",
                    type="tertiary",
                    key=f"eq_del_{item_id}",
                    help="Remove item",
                ):
                    delete_equipment_item(item_id)
                    st.cache_data.clear()
                    st.rerun()

            # Un-check button for already-checked items
            if is_checked:
                if st.button(
                    "Uncheck",
                    icon=":material/undo:",
                    type="tertiary",
                    key=f"eq_undo_{item_id}",
                ):
                    toggle_equipment_item(item_id, False)
                    st.cache_data.clear()
                    st.rerun()

    # Active items
    for _, row in active_df.iterrows():
        _item_row(row, is_checked=False)

    # Checked items at the bottom
    if not done_df.empty:
        st.caption(f":material/check_circle: Packed ({len(done_df)})")
        for _, row in done_df.iterrows():
            _item_row(row, is_checked=True)

    if active_df.empty and done_df.empty:
        st.info("No items yet — add one below.", icon=":material/luggage:")

    # Inline add row
    st.divider()
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        new_item = st.text_input(
            "New item",
            placeholder="e.g. Passport, Sunscreen…",
            key=f"eq_new_{owner}_{trip_id}",
            label_visibility="collapsed",
        )
        add_clicked = st.button(
            "",
            icon=":material/add:",
            type="primary",
            key=f"eq_add_{owner}_{trip_id}",
            help="Add item",
        )

    if add_clicked:
        if new_item.strip():
            if add_equipment_item(trip_id, owner, new_item.strip()):
                st.cache_data.clear()
                st.rerun()
        else:
            st.warning("Please enter an item name.", icon=":material/warning:")


def render() -> None:
    st.header(":material/luggage: Equipment", anchor=False)

    trip_row = trip_picker()
    if trip_row is None:
        return

    trip_id   = str(trip_row["trip_id"])
    owners    = cfg.approved_emails

    if not owners:
        st.info("No approved users configured.", icon=":material/info:")
        return

    tabs = st.tabs([_tab_label(e) for e in owners])
    for tab, owner_email in zip(tabs, owners):
        with tab:
            _owner_tab(trip_id, owner_email)
