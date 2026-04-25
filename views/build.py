"""Build page — create / edit trips and their itinerary entries."""

from datetime import date, timedelta
import pandas as pd
import streamlit as st

from utils.sheets import (
    add_trip, delete_trip,
    add_itinerary_entry, update_itinerary_entry, delete_itinerary_entry,
    set_day_title, get_tasks, link_task_to_entry, unlink_task_from_entry,
)
from utils.images import (
    trigger_async_generation, delete_image, regenerate_sync,
    is_real_url, is_generating, is_failed,
)
from utils.gemini_helper import enrich_destination_info

from views._shared import (
    cached_trips, cached_itinerary, trip_day_number, format_price, parse_link,
    trip_picker, split_itinerary, sort_entries, DESTINATION_ICONS, draw_arrow,
)


@st.cache_data(ttl=15, show_spinner=False)
def _cached_tasks(trip_id: str):
    return get_tasks(trip_id)

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "ILS", "AUD", "CAD", "Other"]
ICON_KEYS   = list(DESTINATION_ICONS.keys())
ICON_LABELS = list(DESTINATION_ICONS.values())


# ─── Reorder helper ──────────────────────────────────────────────────────────
def _move_entry(trip_row, day_df: pd.DataFrame, idx: int, direction: str) -> None:
    """Swap this entry's order value with the adjacent entry above or below."""
    other_idx = idx - 1 if direction == "up" else idx + 1
    if other_idx < 0 or other_idx >= len(day_df):
        return

    entry = day_df.iloc[idx]
    other = day_df.iloc[other_idx]

    # Use stored order; fall back to positional (1-based × 10) when 0 or equal
    ord_entry = int(float(entry.get("order") or 0)) or (idx + 1) * 10
    ord_other = int(float(other.get("order") or 0)) or (other_idx + 1) * 10
    if ord_entry == ord_other:
        ord_entry = (idx + 1) * 10
        ord_other = (other_idx + 1) * 10

    update_itinerary_entry(trip_row, str(entry["entry_id"]), order=ord_other)
    update_itinerary_entry(trip_row, str(other["entry_id"]), order=ord_entry)
    st.cache_data.clear()
    st.rerun()


# ─── New trip form ───────────────────────────────────────────────────────────
def _new_trip_form() -> None:
    with st.form("new_trip_form", clear_on_submit=True, border=True):
        st.subheader("New trip", anchor=False)
        name    = st.text_input("Trip name", placeholder="e.g. Japan Spring 2026")
        country = st.text_input("Country / region", placeholder="e.g. Japan")
        c1, c2  = st.columns(2)
        with c1:
            start = st.date_input("Start date", value=date.today())
        with c2:
            end = st.date_input("End date", value=date.today() + timedelta(days=7))
        notes = st.text_area("Notes", placeholder="Visa, budget overview…", height=80)

        s, c = st.columns(2)
        with s:
            saved = st.form_submit_button("Save trip", icon=":material/save:", use_container_width=True)
        with c:
            cancelled = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if saved:
            if not name or not country:
                st.error("Trip name and country are required.")
            elif end < start:
                st.error("End date must be after start date.")
            else:
                trip_id = add_trip(name, country, start, end, notes)
                if trip_id:
                    st.success("Trip added.")
                    st.session_state["adding_trip"] = False
                    st.session_state["selected_trip_id"] = trip_id
                    st.cache_data.clear()
                    st.rerun()
        if cancelled:
            st.session_state["adding_trip"] = False
            st.rerun()


# ─── Add-entry form ──────────────────────────────────────────────────────────
def _add_entry_form(trip_row, existing_df: pd.DataFrame) -> None:
    trip_start = str(trip_row["start_date"])
    trip_end   = str(trip_row["end_date"])

    with st.form("add_entry_form", clear_on_submit=True, border=True):
        st.subheader("New destination", anchor=False)

        entry_date = st.date_input(
            "Date",
            value=date.fromisoformat(trip_start) if trip_start else date.today(),
            min_value=date.fromisoformat(trip_start) if trip_start else None,
            max_value=date.fromisoformat(trip_end)   if trip_end   else None,
        )

        # Time
        tc1, tc2 = st.columns(2)
        with tc1:
            time_start = st.text_input("From (HH:MM)", placeholder="e.g. 09:00")
        with tc2:
            time_end = st.text_input("Until (HH:MM, optional)", placeholder="e.g. 11:00")

        dest = st.text_input("Destination", placeholder="e.g. Fushimi Inari Shrine")
        desc = st.text_area("Description / what to do", height=80)

        # Icon
        icon_idx = st.selectbox(
            "Section icon",
            range(len(ICON_KEYS)),
            format_func=lambda i: f":material/{ICON_KEYS[i]}: {ICON_LABELS[i]}",
            key="add_icon",
        )
        chosen_icon = ICON_KEYS[icon_idx]

        # Maps URL
        maps_url = st.text_input(
            "Google Maps URL (optional)",
            placeholder="https://maps.google.com/?q=...",
        )

        # Price
        pc1, pc2 = st.columns([2, 1])
        with pc1:
            price = st.text_input("Price / cost", placeholder="e.g. 15")
        with pc2:
            currency = st.selectbox("Currency", CURRENCIES)

        accom = st.text_input("Accommodation")
        links = st.text_input("Links", placeholder="Label | https://... , Label2 | https://...")
        extra = st.text_area("Additional info", height=70)

        img_prompt = st.text_input(
            "Illustration prompt (optional)",
            placeholder="e.g. driving from Zion to Page",
            help="A short sentence; Nano Banana will generate a tiny cartoon for this card.",
        )

        s, c = st.columns(2)
        with s:
            saved = st.form_submit_button("Save entry", icon=":material/save:", use_container_width=True)
        with c:
            cancelled = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if saved:
            if not dest:
                st.error("Destination is required.")
            else:
                day_num = trip_day_number(trip_start, entry_date)
                date_str = str(entry_date)
                existing_day = existing_df[existing_df["date"] == date_str] if not existing_df.empty else pd.DataFrame()
                auto_order = (len(existing_day) + 1) * 10
                eid = add_itinerary_entry(
                    trip_row=trip_row,
                    entry_date=entry_date,
                    day_number=day_num,
                    destination=dest,
                    description=desc,
                    price=price,
                    currency=currency,
                    accommodation=accom,
                    links=links,
                    additional_info=extra,
                    icon=chosen_icon,
                    maps_url=maps_url,
                    time_start=time_start.strip(),
                    time_end=time_end.strip(),
                    order=auto_order,
                    image_prompt=img_prompt.strip(),
                )
                if eid:
                    if img_prompt.strip():
                        trigger_async_generation(
                            entry_id=eid,
                            user_prompt=img_prompt.strip(),
                        )
                        st.toast("Entry added — illustration generating in background…",
                                 icon=":material/auto_awesome:")
                    else:
                        st.toast("Entry added.", icon=":material/check:")
                    st.session_state["adding_entry"] = False
                    st.cache_data.clear()
                    st.rerun()
        if cancelled:
            st.session_state["adding_entry"] = False
            st.rerun()


# ─── Single entry card ───────────────────────────────────────────────────────
def _entry_card(
    trip_row,
    entry,
    day_entries: pd.DataFrame,
    entry_idx: int,
    tasks_df=None,
) -> None:
    eid       = entry["entry_id"]
    icon      = str(entry.get("icon", "") or "location_on")
    price_str = format_price(str(entry.get("price", "")), str(entry.get("currency", "")))
    accom     = str(entry.get("accommodation", "")).strip()
    links     = str(entry.get("links", "")).strip()
    extra     = str(entry.get("additional_info", "")).strip()
    maps_url  = str(entry.get("maps_url", "")).strip()
    t_start   = str(entry.get("time_start", "")).strip()
    t_end     = str(entry.get("time_end", "")).strip()
    img_url   = str(entry.get("image_url", "") or "").strip()
    n         = len(day_entries)

    # Tasks linked to this entry
    linked_tasks = pd.DataFrame()
    unlinked_tasks = pd.DataFrame()
    if tasks_df is not None and not tasks_df.empty:
        if "entry_id" in tasks_df.columns:
            linked_tasks   = tasks_df[tasks_df["entry_id"] == eid].copy()
            done_mask      = tasks_df["done"].fillna(False).astype(bool) if "done" in tasks_df.columns else pd.Series(False, index=tasks_df.index)
            unlinked_tasks = tasks_df[(tasks_df["entry_id"] == "") & (~done_mask)].copy()

    time_str  = (f"{t_start}-{t_end}" if t_end else t_start) if t_start else ""
    maps_link = maps_url or f"https://maps.google.com/?q={entry['destination'].replace(' ', '+')}"

    with st.container(border=True):
        # Header: stays on one row on mobile via horizontal container (columns stack)
        with st.container(
            horizontal=True,
            vertical_alignment="center",
            horizontal_alignment="distribute",
        ):
            st.markdown(f"##### :material/{icon}: {entry['destination']}")
            st.link_button(
                "", maps_link,
                icon=":material/location_on:",
                type="tertiary",
                help="Open in Google Maps",
            )

        if time_str:
            st.caption(f":material/schedule: {time_str}")

        if entry.get("description"):
            st.write(entry["description"])

        bits = []
        if price_str:
            bits.append(f":material/payments: {price_str}")
        if accom:
            bits.append(f":material/hotel: {accom}")
        if bits:
            st.caption("  ·  ".join(bits))

        if links:
            for raw in [l.strip() for l in links.split(",") if l.strip()]:
                label, url = parse_link(raw)
                st.link_button(label, url, icon=":material/link:", use_container_width=True)

        if extra:
            with st.expander("More info", icon=":material/info:"):
                st.write(extra)

        # ── Action bar: ↑ ↓ | enrich | edit | delete ────────────────────────
        with st.container(horizontal=True, gap="xxsmall"):
            if st.button("", icon=":material/arrow_upward:", key=f"up_{eid}",
                         disabled=(entry_idx == 0), help="Move up"):
                _move_entry(trip_row, day_entries, entry_idx, "up")

            if st.button("", icon=":material/arrow_downward:", key=f"dn_{eid}",
                         disabled=(entry_idx == n - 1), help="Move down"):
                _move_entry(trip_row, day_entries, entry_idx, "down")

            if st.button("", icon=":material/auto_awesome:", key=f"enrich_{eid}",
                         help="Enrich 'More info' with AI"):
                with st.spinner("Enriching with AI…"):
                    result = enrich_destination_info(
                        destination=str(entry.get("destination", "")),
                        description=str(entry.get("description", "")),
                        maps_url=str(entry.get("maps_url", "")),
                        additional_info=extra,
                    )
                if result and not result.startswith("❌"):
                    separator = "\n\n---\n**✨ AI Enrichment**\n"
                    new_info = (extra + separator + result) if extra else ("**✨ AI Enrichment**\n" + result)
                    update_itinerary_entry(trip_row, eid, additional_info=new_info)
                    st.toast("More info enriched!", icon=":material/auto_awesome:")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error(result)

            if st.button("", icon=":material/edit:", key=f"edit_{eid}", help="Edit"):
                st.session_state[f"editing_{eid}"] = not st.session_state.get(f"editing_{eid}", False)

            if st.button("", icon=":material/delete:", key=f"del_{eid}", help="Delete"):
                if delete_itinerary_entry(trip_row, eid):
                    st.cache_data.clear()
                    st.rerun()

        # ── Illustration status ───────────────────────────────────────────────
        if is_generating(img_url):
            st.info(":material/auto_awesome: Illustration generating… refresh in ~30s",
                    icon=":material/hourglass_top:")
        elif is_failed(img_url):
            st.error(f":material/broken_image: Illustration failed — {img_url[7:]}")
        elif is_real_url(img_url):
            st.caption(":material/image: Illustration ready ✓")

        # ── Linked tasks ─────────────────────────────────────────────────────
        if not linked_tasks.empty or not unlinked_tasks.empty:
            with st.expander(
                f":material/checklist: Tasks ({len(linked_tasks)})" if not linked_tasks.empty
                else ":material/checklist: Link tasks",
                icon=":material/checklist:",
            ):
                for _, t in linked_tasks.iterrows():
                    tid  = str(t["task_id"])
                    tdesc = str(t.get("description", "")).strip()
                    tdone = bool(t.get("done", False))
                    label = f":gray[~~{tdesc}~~]" if tdone else tdesc
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.caption(label)
                    with c2:
                        if st.button("", icon=":material/link_off:", key=f"unlink_{eid}_{tid}",
                                     type="tertiary", help="Unlink task"):
                            unlink_task_from_entry(tid)
                            st.cache_data.clear()
                            st.rerun()

                if not unlinked_tasks.empty:
                    opts = {
                        str(t["task_id"]): str(t.get("description", ""))
                        for _, t in unlinked_tasks.iterrows()
                    }
                    chosen_id = st.selectbox(
                        "Link a task",
                        options=[""] + list(opts.keys()),
                        format_func=lambda k: opts.get(k, "— pick —") if k else "— pick —",
                        key=f"link_sel_{eid}",
                        label_visibility="collapsed",
                    )
                    if chosen_id and st.button("Link", icon=":material/link:", key=f"link_btn_{eid}",
                                               type="primary"):
                        link_task_to_entry(chosen_id, eid)
                        st.cache_data.clear()
                        st.rerun()

        if st.session_state.get(f"editing_{eid}", False):
            _edit_entry_form(trip_row, entry)


def _edit_entry_form(trip_row, entry) -> None:
    eid = entry["entry_id"]
    cur_icon = str(entry.get("icon", "location_on") or "location_on")
    cur_icon_idx = ICON_KEYS.index(cur_icon) if cur_icon in ICON_KEYS else 0
    cur_order = int(entry.get("order") or 10)

    with st.form(f"edit_form_{eid}", clear_on_submit=False, border=True):
        upd_dest = st.text_input("Destination", value=entry["destination"])
        upd_desc = st.text_area("Description", value=str(entry.get("description", "")), height=80)

        tc1, tc2 = st.columns(2)
        with tc1:
            upd_ts = st.text_input("From (HH:MM)", value=str(entry.get("time_start", "") or ""))
        with tc2:
            upd_te = st.text_input("Until (HH:MM)", value=str(entry.get("time_end", "") or ""))

        upd_order = st.number_input("Position in day", min_value=1, value=cur_order, step=10)

        upd_icon_idx = st.selectbox(
            "Section icon",
            range(len(ICON_KEYS)),
            index=cur_icon_idx,
            format_func=lambda i: f":material/{ICON_KEYS[i]}: {ICON_LABELS[i]}",
            key=f"edit_icon_{eid}",
        )
        upd_maps = st.text_input("Google Maps URL", value=str(entry.get("maps_url", "") or ""))

        c1, c2 = st.columns([2, 1])
        with c1:
            upd_price = st.text_input("Price", value=str(entry.get("price", "")))
        with c2:
            cur_val = str(entry.get("currency", "USD"))
            cur_idx = CURRENCIES.index(cur_val) if cur_val in CURRENCIES else 0
            upd_currency = st.selectbox("Currency", CURRENCIES, index=cur_idx)

        upd_accom = st.text_input("Accommodation", value=str(entry.get("accommodation", "")))
        upd_links = st.text_input("Links", value=str(entry.get("links", "")),
                                   placeholder="Label | https://... , Label2 | https://...")
        upd_extra = st.text_area("Additional info", value=str(entry.get("additional_info", "")), height=70)

        cur_img_url = str(entry.get("image_url", "") or "").strip()
        cur_img_prompt = str(entry.get("image_prompt", "") or "")

        upd_img_prompt = st.text_input(
            "Illustration prompt",
            value=cur_img_prompt,
            placeholder="e.g. driving from Zion to Page",
            help="Clear this field and save to DELETE the current illustration. "
                 "Change it and tick Regenerate to create a new one.",
        )
        regen = st.checkbox(
            "Regenerate illustration now",
            value=False,
            key=f"regen_{eid}",
            disabled=not upd_img_prompt.strip(),
        )

        s, c = st.columns(2)
        with s:
            save = st.form_submit_button("Save", icon=":material/save:", use_container_width=True)
        with c:
            cancel = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if save:
            new_prompt = upd_img_prompt.strip()

            # Cleared prompt → delete existing image from bucket + DB
            if not new_prompt and cur_img_url:
                delete_image(cur_img_url)
                update_itinerary_entry(trip_row, eid, image_url="", image_prompt="")
                st.toast("Illustration removed.", icon=":material/check:")
                st.session_state[f"editing_{eid}"] = False
                st.cache_data.clear()
                st.rerun()

            if update_itinerary_entry(
                trip_row, eid,
                destination=upd_dest, description=upd_desc,
                price=upd_price, currency=upd_currency,
                accommodation=upd_accom, links=upd_links,
                additional_info=upd_extra,
                icon=ICON_KEYS[upd_icon_idx],
                maps_url=upd_maps,
                time_start=upd_ts.strip(),
                time_end=upd_te.strip(),
                order=int(upd_order),
                image_prompt=new_prompt,
            ):
                if regen and new_prompt:
                    with st.spinner("Generating illustration…"):
                        regenerate_sync(eid, new_prompt)
                elif new_prompt and not cur_img_url and not regen:
                    # Prompt was just added for first time but regen not checked
                    trigger_async_generation(eid, new_prompt)
                    st.toast("Illustration queued…", icon=":material/auto_awesome:")
                st.session_state[f"editing_{eid}"] = False
                st.cache_data.clear()
                st.rerun()
        if cancel:
            st.session_state[f"editing_{eid}"] = False
            st.rerun()


# ─── Page entry point ────────────────────────────────────────────────────────
def render() -> None:
    # Title + New trip button in the same row, distributed across the full width
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="bottom"):
        st.header(":material/edit_note: Build", anchor=False)
        if st.button(
            "New trip",
            icon=":material/add:",
            key="new_trip_btn",
        ):
            st.session_state["adding_trip"] = not st.session_state.get("adding_trip", False)

    if st.session_state.get("adding_trip", False):
        _new_trip_form()

    trip_row = trip_picker()
    if trip_row is None:
        return

    with st.container(border=True):
        st.subheader(trip_row["trip_name"], anchor=False)
        st.caption(
            f":material/public: {trip_row['country']}  ·  "
            f":material/calendar_month: {trip_row['start_date']} → {trip_row['end_date']}"
        )
        if trip_row.get("notes"):
            st.write(trip_row["notes"])
        # Delete trip — bottom right of the trip card
        _, del_col = st.columns([4, 1])
        with del_col:
            if st.button(
                "Delete",
                icon=":material/delete_forever:",
                type="tertiary",
                key="del_trip_btn",
                help="Delete this trip permanently",
            ):
                st.session_state["confirm_delete_trip"] = True
        if st.session_state.get("confirm_delete_trip"):
            st.warning("This will permanently delete the trip and all its data.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, delete", icon=":material/delete_forever:", key="del_trip_confirm", type="primary"):
                    if delete_trip(trip_row["trip_id"]):
                        st.session_state.pop("selected_trip_id", None)
                        st.session_state.pop("confirm_delete_trip", None)
                        st.cache_data.clear()
                        st.rerun()
            with c2:
                if st.button("Cancel", key="del_trip_cancel"):
                    st.session_state.pop("confirm_delete_trip", None)
                    st.rerun()

    if st.button("Add destination", icon=":material/add_location:", use_container_width=True,
                 type="primary", key="open_add_entry"):
        st.session_state["adding_entry"] = not st.session_state.get("adding_entry", False)

    itin_df  = cached_itinerary(str(trip_row["trip_id"]), str(trip_row.get("sheet_tab", "")))
    tasks_df = _cached_tasks(str(trip_row["trip_id"]))
    day_titles, entries_df = split_itinerary(itin_df)
    entries_df = sort_entries(entries_df)

    if st.session_state.get("adding_entry", False):
        _add_entry_form(trip_row, entries_df)

    if entries_df.empty and not day_titles:
        st.info("No entries yet — tap **Add destination** above.", icon=":material/info:")
        return

    trip_start = str(trip_row["start_date"])
    all_dates = sorted(entries_df["date"].unique()) if not entries_df.empty else []

    for entry_date in all_dates:
        day_num = trip_day_number(trip_start, date.fromisoformat(str(entry_date)))
        title   = day_titles.get(str(entry_date), "")
        heading = f"Day {day_num} — {entry_date}" + (f" — {title}" if title else "")

        st.subheader(heading, anchor=False, divider="gray")

        with st.expander("Set day title", icon=":material/label:"):
            new_title = st.text_input(
                "Title for this day",
                value=title,
                placeholder="e.g. San Francisco",
                key=f"daytitle_input_{entry_date}",
            )
            if st.button("Save title", icon=":material/save:", key=f"daytitle_save_{entry_date}"):
                if set_day_title(trip_row, str(entry_date), new_title.strip()):
                    st.cache_data.clear()
                    st.rerun()

        group = entries_df[entries_df["date"] == entry_date].reset_index(drop=True)
        for entry_idx, (_, entry) in enumerate(group.iterrows()):
            _entry_card(trip_row, entry, day_entries=group, entry_idx=entry_idx, tasks_df=tasks_df)
            if entry_idx < len(group) - 1:
                draw_arrow()
