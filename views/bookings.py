"""Bookings & Reservations page — manage hotels, flights, activities, etc."""

from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td
import pandas as pd
import streamlit as st

from utils.sheets import (
    add_booking, update_booking, delete_booking,
    BOOKING_TYPES, BOOKING_STATUSES,
)
from views._shared import (
    trip_picker, parse_link, cached_bookings, cached_itinerary, split_itinerary,
)

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "ILS", "AUD", "CAD", "Other"]

STATUS_BADGE = {
    "Confirmed": ":green[:material/check_circle:]",
    "Pending":   ":orange[:material/schedule:]",
    "Cancelled": ":gray[:material/cancel:]",
}

TYPE_ICON = {
    "Hotel":      ":material/hotel:",
    "Hostel":     ":material/cabin:",
    "Flight":     ":material/flight:",
    "Train":      ":material/train:",
    "Car rental": ":material/directions_car:",
    "Activity":   ":material/local_activity:",
    "Restaurant": ":material/restaurant:",
    "Other":      ":material/bookmark:",
}


def _to_date(s) -> _date | None:
    if not s:
        return None
    try:
        return _date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _to_dt(s) -> _dt | None:
    """Parse an ISO datetime string. Accepts trailing 'Z' and date-only strings."""
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return _dt.fromisoformat(raw)
    except ValueError:
        try:
            return _dt.combine(_date.fromisoformat(raw[:10]), _time(23, 59))
        except ValueError:
            return None


def _format_countdown(deadline: _dt) -> tuple[str, str]:
    """Return (badge_markdown, color_name). Color: 'green', 'orange', 'red', 'gray'."""
    now = _dt.now(deadline.tzinfo) if deadline.tzinfo else _dt.now()
    diff = deadline - now

    if diff.total_seconds() <= 0:
        return ":gray[:material/lock_clock: Cancellation expired]", "gray"

    total_hours = int(diff.total_seconds() // 3600)
    days, hours = divmod(total_hours, 24)

    if days >= 1:
        text = f"{days}d {hours}h"
    elif hours >= 1:
        mins = int((diff.total_seconds() % 3600) // 60)
        text = f"{hours}h {mins}m"
    else:
        mins = max(int(diff.total_seconds() // 60), 1)
        text = f"{mins}m"

    if days >= 7:
        color = "green"
    elif days >= 2:
        color = "orange"
    else:
        color = "red"

    return f":{color}[:material/timer: Free cancel in {text}]", color


# Booking types whose check-in → check-out range is HALF-OPEN
# (check_out is the day you LEAVE / RETURN, not a covered night).
# Hotels: stay nights of [check_in, check_out).
# Car rentals: vehicle held during [pickup, return) hours of those days.
HALF_OPEN_TYPES = {"Hotel", "Hostel", "Car rental"}


def _effective_range(btype: str, ci: _date | None, co: _date | None) -> tuple[_date | None, _date | None]:
    """Return inclusive [start, end] occupancy range for overlap math.

    For lodging/rental: the check-out date is NOT occupied, so end = check_out − 1 day.
    For event-style bookings: the closed range is [check_in, check_out].
    Missing check_out is treated as a single-day booking.
    """
    if not ci:
        return None, None
    if not co or co <= ci:
        return ci, ci
    if btype in HALF_OPEN_TYPES:
        end = co - _td(days=1)
        return ci, end if end >= ci else ci
    return ci, co


def _detect_overlaps(df: pd.DataFrame) -> dict[str, set[str]]:
    """Map booking_id -> set of OTHER booking_ids it overlaps with.
    Cancelled bookings are excluded from overlap calculation."""
    out: dict[str, set[str]] = {}
    if df.empty:
        return out
    rows = []
    for _, r in df.iterrows():
        if str(r.get("status", "")) == "Cancelled":
            continue
        start, end = _effective_range(
            str(r.get("type", "") or ""),
            _to_date(r.get("check_in")),
            _to_date(r.get("check_out")),
        )
        if not start:
            continue
        rows.append({
            "id":    str(r["booking_id"]),
            "title": str(r.get("title", "")),
            "start": start,
            "end":   end,
        })
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["start"] <= b["end"] and b["start"] <= a["end"]:
                out.setdefault(a["id"], set()).add(b["id"])
                out.setdefault(b["id"], set()).add(a["id"])
    return out


def _add_booking_form(trip_id: str, trip_row) -> None:
    trip_start = _to_date(trip_row.get("start_date"))
    trip_end   = _to_date(trip_row.get("end_date"))
    today      = _date.today()
    default_in = max(trip_start, min(today, trip_end)) if trip_start and trip_end else today

    with st.form("add_booking_form", clear_on_submit=True, border=True):
        st.subheader("New booking", anchor=False)

        title = st.text_input("Title", placeholder="e.g. Hotel Mendocino — Mendocino, CA")
        c1, c2 = st.columns(2)
        with c1:
            btype = st.selectbox("Type", BOOKING_TYPES)
        with c2:
            status = st.selectbox("Status", BOOKING_STATUSES, index=0)

        d1, d2 = st.columns(2)
        with d1:
            check_in  = st.date_input("Check-in", value=default_in,
                                       min_value=trip_start, max_value=trip_end)
        with d2:
            check_out = st.date_input("Check-out (optional)", value=None,
                                       min_value=trip_start, max_value=trip_end)

        location = st.text_input("Location", placeholder="e.g. Yosemite Valley, CA")

        m1, m2 = st.columns([2, 1])
        with m1:
            amount = st.number_input("Amount", min_value=0.0, step=1.0, format="%.2f")
        with m2:
            currency = st.selectbox("Currency", CURRENCIES)

        url   = st.text_input("Booking URL", placeholder="https://…")
        conf  = st.text_input("Confirmation code (optional)")

        st.caption(":material/event_busy: Free cancellation deadline (optional)")
        fc1, fc2 = st.columns([2, 1])
        with fc1:
            fc_date = st.date_input("Date", value=None,
                                    min_value=_date.today() - _td(days=1),
                                    key="add_fc_date", label_visibility="collapsed")
        with fc2:
            fc_time = st.time_input("Time", value=_time(23, 59),
                                    key="add_fc_time", label_visibility="collapsed")

        descr = st.text_area("Notes (optional)", height=70,
                             placeholder="What's included, cancellation policy, etc.")

        s, c = st.columns(2)
        with s:
            saved = st.form_submit_button("Save", icon=":material/save:", use_container_width=True)
        with c:
            cancelled = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if saved:
            if not title.strip():
                st.error("Title is required.")
            elif check_out and check_out < check_in:
                st.error("Check-out cannot be before check-in.")
            else:
                fc_iso = (
                    _dt.combine(fc_date, fc_time or _time(23, 59)).isoformat(timespec="minutes")
                    if fc_date else ""
                )
                bid = add_booking(
                    trip_id           = trip_id,
                    title             = title.strip(),
                    btype             = btype,
                    status            = status,
                    check_in          = str(check_in) if check_in else "",
                    check_out         = str(check_out) if check_out else "",
                    amount            = float(amount or 0),
                    currency          = currency,
                    location          = location.strip(),
                    confirmation_code = conf.strip(),
                    url               = url.strip(),
                    description       = descr.strip(),
                    free_cancellation = fc_iso,
                )
                if bid:
                    st.cache_data.clear()
                    st.session_state["adding_booking"] = False
                    st.rerun()
        if cancelled:
            st.session_state["adding_booking"] = False
            st.rerun()


def _edit_booking_form(trip_row, booking: pd.Series) -> None:
    bid          = str(booking["booking_id"])
    trip_start   = _to_date(trip_row.get("start_date"))
    trip_end     = _to_date(trip_row.get("end_date"))
    title_v      = str(booking.get("title", "") or "")
    btype_v      = str(booking.get("type", "Hotel") or "Hotel")
    status_v     = str(booking.get("status", "Confirmed") or "Confirmed")
    in_v         = _to_date(booking.get("check_in"))
    out_v        = _to_date(booking.get("check_out"))
    amt_v        = float(booking.get("amount", 0) or 0)
    cur_v        = str(booking.get("currency", "USD") or "USD")
    loc_v        = str(booking.get("location", "") or "")
    conf_v       = str(booking.get("confirmation_code", "") or "")
    url_v        = str(booking.get("url", "") or "")
    desc_v       = str(booking.get("description", "") or "")

    new_title = st.text_input("Title", value=title_v, key=f"ebk_title_{bid}")
    c1, c2 = st.columns(2)
    with c1:
        ti  = BOOKING_TYPES.index(btype_v) if btype_v in BOOKING_TYPES else 0
        new_btype = st.selectbox("Type", BOOKING_TYPES, index=ti, key=f"ebk_type_{bid}")
    with c2:
        si  = BOOKING_STATUSES.index(status_v) if status_v in BOOKING_STATUSES else 0
        new_status = st.selectbox("Status", BOOKING_STATUSES, index=si, key=f"ebk_status_{bid}")

    d1, d2 = st.columns(2)
    with d1:
        new_in  = st.date_input("Check-in", value=in_v or trip_start,
                                min_value=trip_start, max_value=trip_end,
                                key=f"ebk_in_{bid}")
    with d2:
        new_out = st.date_input("Check-out (optional)", value=out_v,
                                min_value=trip_start, max_value=trip_end,
                                key=f"ebk_out_{bid}")

    new_loc = st.text_input("Location", value=loc_v, key=f"ebk_loc_{bid}")

    m1, m2 = st.columns([2, 1])
    with m1:
        new_amt = st.number_input("Amount", value=amt_v, min_value=0.0,
                                  step=1.0, format="%.2f", key=f"ebk_amt_{bid}")
    with m2:
        ci = CURRENCIES.index(cur_v) if cur_v in CURRENCIES else 0
        new_cur = st.selectbox("Currency", CURRENCIES, index=ci, key=f"ebk_cur_{bid}")

    new_url   = st.text_input("Booking URL", value=url_v, key=f"ebk_url_{bid}")
    new_conf  = st.text_input("Confirmation code", value=conf_v, key=f"ebk_conf_{bid}")

    fc_v = _to_dt(booking.get("free_cancellation"))
    st.caption(":material/event_busy: Free cancellation deadline (optional)")
    fc1, fc2 = st.columns([2, 1])
    with fc1:
        new_fc_date = st.date_input(
            "Date",
            value=fc_v.date() if fc_v else None,
            min_value=_date.today() - _td(days=365),
            key=f"ebk_fc_date_{bid}",
            label_visibility="collapsed",
        )
    with fc2:
        new_fc_time = st.time_input(
            "Time",
            value=fc_v.time() if fc_v else _time(23, 59),
            key=f"ebk_fc_time_{bid}",
            label_visibility="collapsed",
        )

    new_descr = st.text_area("Notes", value=desc_v, height=70, key=f"ebk_descr_{bid}")

    col_s, col_c = st.columns(2)
    with col_s:
        if st.button("Save", icon=":material/save:", type="primary",
                     key=f"ebk_save_{bid}", use_container_width=True):
            if not new_title.strip():
                st.error("Title is required.")
            elif new_out and new_out < new_in:
                st.error("Check-out cannot be before check-in.")
            else:
                new_fc_iso = (
                    _dt.combine(new_fc_date, new_fc_time or _time(23, 59)).isoformat(timespec="minutes")
                    if new_fc_date else ""
                )
                if update_booking(
                    booking_id        = bid,
                    title             = new_title.strip(),
                    btype             = new_btype,
                    status            = new_status,
                    check_in          = str(new_in) if new_in else "",
                    check_out         = str(new_out) if new_out else "",
                    amount            = float(new_amt or 0),
                    currency          = new_cur,
                    location          = new_loc.strip(),
                    confirmation_code = new_conf.strip(),
                    url               = new_url.strip(),
                    description       = new_descr.strip(),
                    free_cancellation = new_fc_iso,
                ):
                    st.session_state[f"edit_bk_{bid}"] = False
                    st.cache_data.clear()
                    st.rerun()
    with col_c:
        if st.button("Cancel", icon=":material/close:",
                     key=f"ebk_cancel_{bid}", use_container_width=True):
            st.session_state[f"edit_bk_{bid}"] = False
            st.rerun()


def _render_booking_card(
    trip_row,
    booking: pd.Series,
    overlaps: dict[str, set[str]],
    bookings_df: pd.DataFrame,
    linked_entries_by_booking: dict[str, list[dict]],
) -> None:
    bid       = str(booking["booking_id"])
    title     = str(booking.get("title", "") or "(untitled)")
    btype     = str(booking.get("type", "Other") or "Other")
    status    = str(booking.get("status", "Confirmed") or "Confirmed")
    check_in  = _to_date(booking.get("check_in"))
    check_out = _to_date(booking.get("check_out"))
    amt       = float(booking.get("amount", 0) or 0)
    cur       = str(booking.get("currency", "") or "")
    loc       = str(booking.get("location", "") or "")
    url       = str(booking.get("url", "") or "")
    conf      = str(booking.get("confirmation_code", "") or "")
    descr     = str(booking.get("description", "") or "")
    edit_key  = f"edit_bk_{bid}"

    overlap_set = overlaps.get(bid, set())

    with st.container(border=True):
        if st.session_state.get(edit_key, False):
            _edit_booking_form(trip_row, booking)
            return

        # ── Header row ────────────────────────────────────────────────────
        type_icon = TYPE_ICON.get(btype, ":material/bookmark:")
        status_badge = STATUS_BADGE.get(status, "")
        fc_dt = _to_dt(booking.get("free_cancellation"))

        with st.container(horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="top"):
            st.markdown(f"### {type_icon} {title}")
            with st.container():
                st.markdown(f"{status_badge} {status}")
                if fc_dt:
                    badge, _color = _format_countdown(fc_dt)
                    st.markdown(
                        badge,
                        help=f"Free until {fc_dt.strftime('%Y-%m-%d %H:%M')}",
                    )

        meta_bits = []
        if check_in:
            if check_out and check_out != check_in:
                meta_bits.append(f":material/calendar_month: {check_in} → {check_out}")
            else:
                meta_bits.append(f":material/calendar_month: {check_in}")
        if loc:
            meta_bits.append(f":material/location_on: {loc}")
        if amt > 0:
            meta_bits.append(f":material/payments: **{amt:,.2f} {cur}**")
        if conf:
            meta_bits.append(f":material/confirmation_number: `{conf}`")

        if meta_bits:
            st.caption("  ·  ".join(meta_bits))

        if descr:
            st.write(descr)

        # ── Linked itinerary destinations ────────────────────────────────
        linked = linked_entries_by_booking.get(bid, [])
        if linked:
            with st.expander(f":material/link: Linked destinations ({len(linked)})"):
                for e in linked:
                    st.caption(
                        f"**{e['date']}** — {e['destination']}"
                        + (f"  ·  {e['time_start']}" if e.get("time_start") else "")
                    )

        # ── Action row ───────────────────────────────────────────────────
        with st.container(horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center"):
            # Left side — overlap indicator (or filler when none)
            with st.container(horizontal=True, vertical_alignment="center"):
                if overlap_set:
                    overlap_titles = ", ".join(
                        str(r["title"]) for _, r in
                        bookings_df[bookings_df["booking_id"].isin(overlap_set)].iterrows()
                    )
                    n = len(overlap_set)
                    st.markdown(
                        f":red[:material/warning:] **:red[Overlap × {n}]**",
                        help=f"Overlaps with: {overlap_titles}",
                    )

            # Right side — actions
            with st.container(horizontal=True, vertical_alignment="center"):
                if url:
                    st.link_button("Open booking", url, icon=":material/open_in_new:")
                if st.button("", icon=":material/edit:", type="tertiary",
                             key=f"bk_edit_{bid}", help="Edit booking"):
                    st.session_state[edit_key] = True
                    st.rerun()
                if st.button("", icon=":material/delete:", type="tertiary",
                             key=f"bk_del_{bid}", help="Delete booking"):
                    if delete_booking(bid):
                        st.cache_data.clear()
                        st.rerun()


def render() -> None:
    with st.container(horizontal=True, horizontal_alignment="distribute",
                      vertical_alignment="bottom"):
        st.header(":material/book_online: Bookings & Reservations", anchor=False)
        add_clicked = st.button(
            "Add booking",
            icon=":material/add:",
            type="primary",
            key="add_booking_btn",
        )

    trip_row = trip_picker()
    if trip_row is None:
        return

    trip_id = str(trip_row["trip_id"])

    if add_clicked:
        st.session_state["adding_booking"] = not st.session_state.get("adding_booking", False)

    if st.session_state.get("adding_booking", False):
        _add_booking_form(trip_id, trip_row)

    bookings_df = cached_bookings(trip_id)

    if bookings_df.empty:
        st.info("No bookings yet — add one above.", icon=":material/info:")
        return

    # Build a map of booking_id -> linked itinerary entries (for the Linked block)
    linked_entries_by_booking: dict[str, list[dict]] = {}
    itin_df = cached_itinerary(trip_id)
    if not itin_df.empty and "booking_id" in itin_df.columns:
        _, entries_df = split_itinerary(itin_df)
        for _, e in entries_df.iterrows():
            bid = str(e.get("booking_id", "") or "")
            if not bid:
                continue
            linked_entries_by_booking.setdefault(bid, []).append({
                "date":        str(e.get("date", "")),
                "destination": str(e.get("destination", "")),
                "time_start":  str(e.get("time_start", "") or ""),
            })

    overlaps = _detect_overlaps(bookings_df)

    # Top-of-page summary
    overlap_count = len({bid for bid in overlaps})
    total_amt: dict[str, float] = {}
    for _, r in bookings_df.iterrows():
        if str(r.get("status", "")) == "Cancelled":
            continue
        c = str(r.get("currency", "") or "USD")
        total_amt[c] = total_amt.get(c, 0.0) + float(r.get("amount", 0) or 0)

    with st.container(horizontal=True, horizontal_alignment="distribute",
                      vertical_alignment="center"):
        st.caption(f":material/inventory_2: {len(bookings_df)} booking(s)")
        if overlap_count:
            st.caption(f":red[:material/warning: {overlap_count} overlapping]")
        if total_amt:
            totals_str = " + ".join(f"{v:,.0f} {c}" for c, v in total_amt.items())
            st.caption(f":material/payments: {totals_str}")

    st.divider()

    # ── Group cards by check_in date ──────────────────────────────────────
    bookings_df = bookings_df.sort_values("check_in", na_position="last").reset_index(drop=True)
    for _, b in bookings_df.iterrows():
        _render_booking_card(
            trip_row, b, overlaps, bookings_df, linked_entries_by_booking,
        )
