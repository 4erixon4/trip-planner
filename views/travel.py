"""Travel page — auto-detects today's trip day and shows its entries."""

import base64
from datetime import date
import pandas as pd
import requests as _requests
import streamlit as st

from utils.gemini_helper import stream_destination_response
from utils.images import get_entry_images
from views._shared import (
    cached_itinerary, cached_tasks, trip_day_number, format_price, parse_link,
    trip_picker, split_itinerary, sort_entries, DESTINATION_ICONS, draw_arrow,
    priority_badge, priority_sort_key,
)

# ── Icon → emoji map for the HTML export ─────────────────────────────────────
_ICON_EMOJI = {
    "location_on": "📍", "location_city": "🏙️", "directions_car": "🚗",
    "directions_bus": "🚌", "train": "🚂", "flight": "✈️",
    "directions_boat": "⛵", "hiking": "🥾", "beach_access": "🏖️",
    "park": "🌳", "forest": "🌲", "restaurant": "🍽️", "local_cafe": "☕",
    "nightlife": "🎶", "museum": "🏛️", "photo_camera": "📷",
    "shopping_bag": "🛍️", "hotel": "🏨", "umbrella": "☂️",
    "sports": "⚽", "attractions": "🎡", "tour": "🗺️",
    "anchor": "⚓", "terrain": "⛰️",
}


def _build_day_html(
    trip_name: str,
    chosen_date: str,
    weekday: str,
    day_title: str,
    entries: pd.DataFrame,
    tasks_df: pd.DataFrame,
) -> str:
    """Return a print-ready HTML document for the selected day."""
    dn_str = f"{weekday}, {chosen_date}" + (f" — {day_title}" if day_title else "")

    rows_html = ""
    for _, e in entries.iterrows():
        eid      = str(e.get("entry_id", ""))
        icon     = _ICON_EMOJI.get(str(e.get("icon", "")), "📍")
        dest     = str(e.get("destination", ""))
        t_start  = str(e.get("time_start", "") or "").strip()
        t_end    = str(e.get("time_end", "")   or "").strip()
        time_str = (f"{t_start}–{t_end}" if t_end else t_start) if t_start else ""
        desc     = str(e.get("description", "") or "").strip()
        price    = format_price(str(e.get("price", "")), str(e.get("currency", "")))
        accom    = str(e.get("accommodation", "") or "").strip()
        links    = str(e.get("links", "")         or "").strip()
        extra    = str(e.get("additional_info", "") or "").strip()
        maps_url = str(e.get("maps_url", "") or "").strip() or \
                   f"https://maps.google.com/?q={dest.replace(' ', '+')}"

        # linked active tasks
        entry_task_rows = ""
        if not tasks_df.empty and "entry_id" in tasks_df.columns:
            active = tasks_df[(tasks_df["entry_id"] == eid) & (~tasks_df["done"].fillna(False).astype(bool))]
            _PRI = {"High": 0, "Medium": 1, "Normal": 2}
            active = active.copy()
            active["_p"] = active["priority"].apply(lambda p: _PRI.get(str(p).strip(), 2))
            active = active.sort_values("_p")
            for _, t in active.iterrows():
                tdesc = str(t.get("description", "")).strip()
                badge = "🔴 " if str(t.get("priority")) == "High" else ("🟡 " if str(t.get("priority")) == "Medium" else "")
                entry_task_rows += f"<li>{badge}{tdesc}</li>"

        # links html
        links_html = ""
        if links:
            for raw in [l.strip() for l in links.split(",") if l.strip()]:
                label, url = parse_link(raw)
                links_html += f'<a href="{url}" target="_blank">🔗 {label}</a>  '

        rows_html += f"""
        <div class="card">
          <h3>{icon} {dest}
            {"<span class='time'>" + time_str + "</span>" if time_str else ""}
            <a href="{maps_url}" target="_blank" class="maps-btn">📍 Maps</a>
          </h3>
          {"<p class='desc'>" + desc + "</p>" if desc else ""}
          {"<p class='meta'>" + "  ·  ".join(filter(None, [price, ("🏨 " + accom) if accom else ""])) + "</p>" if price or accom else ""}
          {"<p class='links'>" + links_html + "</p>" if links_html else ""}
          {"<details open><summary>More info</summary><p>" + extra.replace(chr(10), "<br>") + "</p></details>" if extra else ""}
          {"<details open><summary>Tasks</summary><ul>" + entry_task_rows + "</ul></details>" if entry_task_rows else ""}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{trip_name} — {dn_str}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          max-width: 780px; margin: 40px auto; color: #1a1a1a; padding: 0 20px; }}
  h1   {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .sub {{ color: #666; font-size: 0.95rem; margin-bottom: 24px; }}
  .card {{ border: 1px solid #e0e0e0; border-radius: 10px; padding: 16px 20px;
            margin-bottom: 16px; page-break-inside: avoid; }}
  h3   {{ margin: 0 0 8px; font-size: 1.05rem; display: flex; align-items: center; gap: 8px; }}
  .maps-btn {{ font-size: 0.78rem; font-weight: normal; color: #1a73e8;
               text-decoration: none; border: 1px solid #c5d8f5; border-radius: 4px;
               padding: 1px 7px; margin-left: 8px; white-space: nowrap; }}
  .maps-btn:hover {{ background: #e8f0fe; }}
  .time {{ background: #f0f0f0; border-radius: 4px; padding: 1px 7px;
            font-size: 0.82rem; font-weight: normal; margin-left: 6px; }}
  .desc {{ margin: 6px 0; }}
  .meta {{ color: #555; font-size: 0.88rem; margin: 4px 0; }}
  .links a {{ color: #1a73e8; font-size: 0.9rem; margin-right: 12px; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #555; font-size: 0.9rem; }}
  ul {{ margin: 6px 0 0 16px; padding: 0; }}
  li {{ font-size: 0.9rem; margin-bottom: 3px; }}
  @media print {{ .card {{ border-color: #ccc; }} }}
</style>
</head>
<body>
  <h1>{trip_name}</h1>
  <p class="sub">{dn_str}</p>
  {rows_html}
</body>
</html>"""


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
def _entry_card(entry: pd.Series, linked_tasks: pd.DataFrame | None = None) -> None:
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

        # Attached photos
        photos = get_entry_images(eid)
        if photos:
            with st.expander(f"Photos ({len(photos)})", icon=":material/photo_library:"):
                cols = st.columns(3)
                for i, ph in enumerate(photos):
                    with cols[i % 3]:
                        st.image(ph["url"], use_container_width=True)

        # Linked active tasks
        if linked_tasks is not None and not linked_tasks.empty:
            active = linked_tasks[~linked_tasks["done"]] if "done" in linked_tasks.columns else linked_tasks
            if not active.empty:
                active = active.copy()
                active["_p"] = active["priority"].apply(priority_sort_key)
                active = active.sort_values("_p").drop(columns=["_p"])
                with st.expander(f"Tasks ({len(active)})", icon=":material/checklist:"):
                    for _, t in active.iterrows():
                        tdesc    = str(t.get("description", "")).strip()
                        tnotes   = str(t.get("notes", "") or "").strip()
                        tdue     = str(t.get("due_date", "") or "").strip()
                        assignee = str(t.get("assigned_to", "")).strip()
                        pri      = str(t.get("priority", "Normal"))
                        badge    = priority_badge(pri)
                        suffix   = f"  :gray[— {assignee}]" if assignee and assignee != "Unassigned" else ""
                        st.markdown(f"- {badge} **{tdesc}**{suffix}")
                        if tnotes:
                            st.caption(f"  {tnotes}")
                        if tdue:
                            st.caption(f"  :material/event: {tdue}")


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

    trip_id = str(trip_row["trip_id"])
    itin_df  = cached_itinerary(trip_id, str(trip_row.get("sheet_tab", "")))
    tasks_df = cached_tasks(trip_id)
    day_titles, entries_df = split_itinerary(itin_df)
    entries_df = sort_entries(entries_df)

    if entries_df.empty:
        st.info(
            "No itinerary entries yet. Switch to the **Build** page to add some.",
            icon=":material/inbox:",
        )
        return

    # Day-picker — labels include the day title when set
    all_dates  = sorted(entries_df["date"].unique())
    all_dates_str = [str(d) for d in all_dates]
    labels: dict[str, str] = {}
    default_idx = 0
    for i, d in enumerate(all_dates):
        dn     = trip_day_number(str(trip_start), date.fromisoformat(str(d)))
        dtitle = day_titles.get(str(d), "")
        labels[d] = f"Day {dn} — {d}" + (f" — {dtitle}" if dtitle else "")
        if str(d) == str(today):
            default_idx = i

    # Auto-jump to today when within the trip, once per calendar day.
    # We track the last date we auto-set so manual day changes are preserved
    # within the same calendar day.
    today_str = str(today)
    if within_trip:
        last_auto = st.session_state.get("_travel_auto_date", "")
        if last_auto != today_str:
            if today_str in all_dates_str:
                st.session_state["travel_day_pick"] = today_str
            st.session_state["_travel_auto_date"] = today_str

    col_sel, col_dl = st.columns([0.7, 0.3])
    with col_sel:
        chosen = st.selectbox(
            "Showing",
            all_dates,
            index=default_idx,
            format_func=lambda d: labels[d],
            key="travel_day_pick",
        )

    # Weekday + optional day title
    weekday   = date.fromisoformat(str(chosen)).strftime("%A")
    day_title = day_titles.get(str(chosen), "")
    caption_parts = [f":gray[{weekday}]"]
    if day_title:
        caption_parts.append(f":material/label: {day_title}")
    st.caption("  ·  ".join(caption_parts))

    today_entries = entries_df[entries_df["date"] == str(chosen)]

    # Download button — generates HTML for the selected day
    with col_dl:
        st.space('small')
        if not today_entries.empty:
            html_bytes = _build_day_html(
                trip_name=str(trip_row["trip_name"]),
                chosen_date=str(chosen),
                weekday=weekday,
                day_title=day_title,
                entries=today_entries,
                tasks_df=tasks_df,
            ).encode("utf-8")
            st.download_button(
                "",
                data=html_bytes,
                file_name=f"day_{chosen}.html",
                mime="text/html",
                icon=":material/download:",
                help="Download day as HTML (open in browser → Print → Save as PDF)",
                use_container_width=True,
            )

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
        eid = str(entry.get("entry_id", ""))
        entry_tasks = None
        if not tasks_df.empty and "entry_id" in tasks_df.columns:
            entry_tasks = tasks_df[tasks_df["entry_id"] == eid]
        _entry_card(entry, linked_tasks=entry_tasks)
        if i < len(entries_list) - 1:
            draw_arrow()
