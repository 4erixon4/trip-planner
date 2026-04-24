"""Travel page — auto-detects today's trip day and shows its entries."""

import base64
from datetime import date
import pandas as pd
import requests as _requests
import streamlit as st

from utils.gemini_helper import stream_destination_response
from views._shared import (
    cached_itinerary, trip_day_number, format_price, parse_link,
    trip_picker, split_itinerary, sort_entries, DESTINATION_ICONS, draw_arrow,
)


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_image(url: str) -> bytes | None:
    """
    Fetch image bytes from Supabase Storage server-side.
    Results are cached for 5 min so each image is fetched at most once per session.
    """
    try:
        r = _requests.get(url, timeout=10)
        return r.content if r.ok else None
    except Exception:
        return None


def _entry_context(entry) -> str:
    """Build a rich context string from all available entry data for Gemini."""
    parts = []
    for field, label in [
        ("destination",    "Destination"),
        ("description",    "Description"),
        ("accommodation",  "Accommodation"),
        ("additional_info","Additional info"),
        ("links",          "Links"),
        ("price",          "Price"),
        ("currency",       "Currency"),
    ]:
        val = str(entry.get(field, "") or "").strip()
        if val:
            parts.append(f"{label}: {val}")
    return "\n".join(parts)


# ─── Gemini chat dialog ───────────────────────────────────────────────────────
@st.dialog("Ask Gemini", width="large")
def _gemini_dialog(entry: pd.Series) -> None:
    dest    = str(entry["destination"])
    eid     = str(entry["entry_id"])
    hist_key = f"gemini_hist_{eid}"
    context  = _entry_context(entry)

    if hist_key not in st.session_state:
        st.session_state[hist_key] = []

    st.subheader(f":material/auto_awesome: {dest}", anchor=False)
    st.caption("Ask anything — history, highlights, local tips, practical advice…")
    st.divider()

    @st.fragment
    def _chat() -> None:
        # Render conversation history
        for msg in st.session_state[hist_key]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        question = st.text_input(
            "Your question",
            placeholder="e.g. What's the best time to go? Any hidden gems nearby?",
            key=f"dlg_q_{eid}",
            label_visibility="collapsed",
        )

        col_ask, col_clear = st.columns([3, 1])
        with col_ask:
            ask = st.button(
                "Ask Gemini",
                icon=":material/auto_awesome:",
                type="primary",
                use_container_width=True,
                key=f"dlg_ask_{eid}",
            )
        with col_clear:
            if st.button(
                "Clear",
                icon=":material/delete_sweep:",
                use_container_width=True,
                key=f"dlg_clear_{eid}",
            ):
                st.session_state[hist_key] = []
                st.rerun(scope="fragment")

        if ask:
            if not question.strip():
                st.warning("Type a question first.")
            else:
                # Show user bubble immediately, then stream the assistant reply
                with st.chat_message("user"):
                    st.markdown(question.strip())

                with st.chat_message("assistant"):
                    full_response = st.write_stream(
                        stream_destination_response(dest, question.strip(), context)
                    )

                st.session_state[hist_key].append({"role": "user",      "content": question.strip()})
                st.session_state[hist_key].append({"role": "assistant", "content": full_response or ""})
                st.rerun(scope="fragment")

    _chat()


# ─── Entry card ───────────────────────────────────────────────────────────────
def _entry_card(entry: pd.Series) -> None:
    eid       = entry["entry_id"]
    icon      = str(entry.get("icon", "") or "location_on")
    price_str = format_price(str(entry.get("price", "")), str(entry.get("currency", "")))
    accom     = str(entry.get("accommodation", "")).strip()
    links     = str(entry.get("links", "")).strip()
    extra     = str(entry.get("additional_info", "")).strip()
    maps_url  = str(entry.get("maps_url", "")).strip()
    t_start   = str(entry.get("time_start", "")).strip()
    t_end     = str(entry.get("time_end", "")).strip()
    image_url = str(entry.get("image_url", "") or "").strip()

    if icon not in DESTINATION_ICONS:
        icon = "location_on"

    time_str  = (f"{t_start}-{t_end}" if t_end else t_start) if t_start else ""
    maps_link = maps_url or f"https://maps.google.com/?q={entry['destination'].replace(' ', '+')}"

    # Fetch image early so we can decide how to render the header
    img_bytes = None
    if image_url and image_url.startswith("http"):
        img_bytes = _fetch_image(image_url)

    with st.container(border=True):
        # Header row: thumbnail OR material icon · title · action buttons
        # Header: single HTML element for image+title (avoids nested horizontal containers
        # fighting each other on mobile), buttons pushed to the right via distribute.
        with st.container(
            horizontal=True,
            vertical_alignment="center",
            horizontal_alignment="distribute",
        ):
            # Left: image (inline) + title as one markdown block
            if img_bytes:
                b64 = base64.b64encode(img_bytes).decode()
                st.markdown(
                    f'<span style="display:inline-flex;align-items:center;gap:10px;">'
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'style="width:44px;height:44px;object-fit:cover;border-radius:10px;flex-shrink:0;">'
                    f'<strong style="white-space:nowrap;">{entry["destination"]}</strong>'
                    f'</span>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(f"##### :material/{icon}: {entry['destination']}")

            # Right: action buttons pinned to far right
            with st.container(horizontal=True, horizontal_alignment="right", gap="xsmall"):
                st.link_button(
                    "",
                    maps_link,
                    icon=":material/location_on:",
                    type="tertiary",
                    help="Open in Google Maps",
                )
                if st.button(
                    "",
                    icon=":material/auto_awesome:",
                    type="primary",
                    key=f"gem_btn_{eid}",
                    help="Ask Gemini about this destination",
                ):
                    _gemini_dialog(entry)

        # Body content
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



# ─── Page entry point ─────────────────────────────────────────────────────────
def render() -> None:
    st.header(":material/explore: Travel", anchor=False)

    trip_row = trip_picker()
    if trip_row is None:
        return

    try:
        trip_start = date.fromisoformat(str(trip_row["start_date"]))
        trip_end   = date.fromisoformat(str(trip_row["end_date"]))
    except Exception:
        st.error("Invalid trip dates.")
        return

    today = date.today()
    within_trip = trip_start <= today <= trip_end

    if within_trip:
        day_num = (today - trip_start).days + 1
        with st.container(border=True):
            st.subheader(
                f":material/today: Day {day_num}  ·  {today.strftime('%A, %B %d %Y')}",
                anchor=False,
            )
            st.caption(trip_row["trip_name"])
    else:
        # Show a dismissing toast once per session per trip (not a persistent warning)
        toast_key = f"trip_toast_{trip_row['trip_id']}"
        if not st.session_state.get(toast_key):
            status = "hasn't started yet" if today < trip_start else "has ended"
            st.toast(f"{trip_row['trip_name']} {status}.", icon=":material/info:")
            st.session_state[toast_key] = True
        today = trip_start if today < trip_start else trip_end

    itin_df = cached_itinerary(str(trip_row["trip_id"]), str(trip_row.get("sheet_tab", "")))
    day_titles, entries_df = split_itinerary(itin_df)
    entries_df = sort_entries(entries_df)

    if entries_df.empty:
        st.info(
            "No itinerary entries yet. Switch to the **Build** page to add some.",
            icon=":material/inbox:",
        )
        return

    # Day-picker — labels include the day title when set
    all_dates = sorted(entries_df["date"].unique())
    labels: dict[str, str] = {}
    default_idx = 0
    for i, d in enumerate(all_dates):
        dn     = trip_day_number(str(trip_start), date.fromisoformat(str(d)))
        dtitle = day_titles.get(str(d), "")
        labels[d] = f"Day {dn} — {d}" + (f" — {dtitle}" if dtitle else "")
        if str(d) == str(today):
            default_idx = i

    chosen = st.selectbox(
        "Showing",
        all_dates,
        index=default_idx,
        format_func=lambda d: labels[d],
        key="travel_day_pick",
    )

    # Day title caption
    day_title = day_titles.get(str(chosen), "")
    if day_title:
        st.caption(f":material/label: {day_title}")

    today_entries = entries_df[entries_df["date"] == str(chosen)]
    if today_entries.empty:
        st.info(f"No entries for {chosen}.", icon=":material/inbox:")
        return

    # Show refresh button when any illustration is generating or when entries have a
    # prompt but the cached data may not yet reflect the completed image_url
    has_pending = any(
        str(e.get("image_url", "")) in ("generating", "") and str(e.get("image_prompt", "")).strip()
        for _, e in today_entries.iterrows()
    )
    if has_pending:
        if st.button("↻ Refresh illustrations", icon=":material/refresh:", type="tertiary"):
            st.cache_data.clear()
            st.rerun()

    entries_list = list(today_entries.iterrows())
    for i, (_, entry) in enumerate(entries_list):
        _entry_card(entry)
        if i < len(entries_list) - 1:
            draw_arrow()
