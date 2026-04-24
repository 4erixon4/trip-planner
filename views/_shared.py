"""Helpers shared by build / travel views."""

from datetime import date
import streamlit as st
import pandas as pd

from utils.sheets import get_trips, get_itinerary

_DAY_TITLE_PREFIX = "daytitle_"

# Material icon options for destination type
DESTINATION_ICONS: dict[str, str] = {
    "location_on":      "Place",
    "location_city":    "City",
    "directions_car":   "Car / Drive",
    "directions_bus":   "Bus",
    "train":            "Train",
    "flight":           "Flight",
    "directions_boat":  "Boat",
    "hiking":           "Hiking",
    "beach_access":     "Beach",
    "park":             "Park / Nature",
    "forest":           "Forest",
    "restaurant":       "Restaurant",
    "local_cafe":       "Café",
    "nightlife":        "Nightlife",
    "museum":           "Museum",
    "photo_camera":     "Photography",
    "shopping_bag":     "Shopping",
    "hotel":            "Hotel / Stay",
    "umbrella":         "Relax",
    "sports":           "Sports",
    "attractions":      "Attraction",
    "tour":             "Tour",
    "anchor":           "Port / Marina",
    "terrain":          "Mountain",
}


@st.cache_data(ttl=30, show_spinner=False)
def cached_trips() -> pd.DataFrame:
    return get_trips()


@st.cache_data(ttl=20, show_spinner=False)
def cached_itinerary(tab_name: str) -> pd.DataFrame:
    fake_row = pd.Series({"sheet_tab": tab_name})
    return get_itinerary(fake_row)


def trip_day_number(trip_start: str, target_date: date) -> int:
    try:
        start = date.fromisoformat(str(trip_start))
        return (target_date - start).days + 1
    except Exception:
        return 1


def parse_link(raw: str) -> tuple[str, str]:
    """Return (label, url) from a raw link string.

    Supports two formats:
      - 'Label | https://example.com'  → label is the user-provided text
      - 'https://example.com'          → label falls back to the domain name
    """
    if "|" in raw:
        label, _, url = raw.partition("|")
        return label.strip(), url.strip()
    url = raw.strip()
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc or url
        label = netloc.replace("www.", "")
    except Exception:
        label = url
    return label, url


def format_price(price: str, currency: str) -> str:
    if not price or str(price).strip() in ("", "0"):
        return ""
    return f"{price} {currency}"


def sort_entries(df: pd.DataFrame) -> pd.DataFrame:
    """Sort entries within each day by the 'order' field (numeric, ascending).
    Handles sheets that don't yet have an 'order' column gracefully.
    """
    if df.empty:
        return df
    df = df.copy()
    if "order" in df.columns:
        order_series = pd.to_numeric(df["order"], errors="coerce").fillna(0)
    else:
        order_series = pd.Series(0, index=df.index)
    df["_ord"] = order_series
    df = df.sort_values(["date", "_ord"], ignore_index=True).drop(columns=["_ord"])
    return df


def split_itinerary(df: pd.DataFrame) -> tuple[dict[str, str], pd.DataFrame]:
    """Return (day_titles_dict, regular_entries_df) from a raw itinerary df."""
    if df.empty:
        return {}, df
    is_title = df["entry_id"].str.startswith(_DAY_TITLE_PREFIX, na=False)
    titles = {
        str(row["date"]): str(row.get("destination", ""))
        for _, row in df[is_title].iterrows()
    }
    entries = df[~is_title].reset_index(drop=True)
    return titles, entries


def trip_picker() -> pd.Series | None:
    """Sidebar-independent trip dropdown. Returns the selected trip row or None."""
    trips_df = cached_trips()
    if trips_df.empty:
        st.info("No trips yet. Create one in the Build page.", icon=":material/info:")
        return None

    options = {
        row["trip_id"]: f"{row['trip_name']} ({row['start_date']} → {row['end_date']})"
        for _, row in trips_df.iterrows()
    }

    # Preserve selection across pages via session_state
    selected = st.selectbox(
        "Trip",
        options=list(options.keys()),
        format_func=lambda k: options[k],
        key="selected_trip_id",
    )
    return trips_df[trips_df["trip_id"] == selected].iloc[0]


def draw_arrow() -> None:
    """Render the down-arrow separator, centered and capped to ~1/3 of the screen."""
    _, mid, _ = st.columns([1.5, 1, 1.5])
    with mid:
        with st.container(horizontal=True, horizontal_alignment="center"):
            st.image("down arrow.png", width=110)
