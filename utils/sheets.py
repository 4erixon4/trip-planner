"""
Google Sheets integration.

Sheet layout (single spreadsheet):
  - "Trips"        : trip metadata (one row per trip)
  - "<trip_name>"  : one dedicated worksheet per trip, holding its itinerary entries
  - "Expenses"     : manual additional expenses across all trips
"""

import json
import re
import uuid
from datetime import datetime

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from utils.config import cfg

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

TRIPS_COLS = [
    "trip_id",
    "trip_name",
    "country",
    "start_date",
    "end_date",
    "notes",
    "sheet_tab",   # name of the dedicated worksheet for this trip
    "created_at",
]

ITINERARY_COLS = [
    "entry_id",
    "date",
    "day_number",
    "order",        # integer display order within a day (lower = earlier)
    "destination",
    "description",
    "price",
    "currency",
    "accommodation",
    "links",
    "additional_info",
    "icon",         # material icon name, e.g. "location_on"
    "maps_url",     # Google Maps URL or search query
    "time_start",   # HH:MM e.g. "14:00"
    "time_end",     # HH:MM optional e.g. "16:00"
    "created_at",
]

EXPENSE_COLS = [
    "expense_id",
    "trip_id",
    "date",
    "category",
    "description",
    "amount",
    "currency",
    "links",        # "Label | URL , Label2 | URL2"
    "created_at",
]

TASK_COLS = [
    "task_id",
    "trip_id",
    "description",
    "assigned_to",   # Gmail address or "Unassigned"
    "priority",      # "High" | "Medium" | "Normal"
    "links",         # "Label | URL , Label2 | URL2"
    "created_at",
]

_tasks_migrated: bool = False
_expenses_migrated: bool = False


def _ensure_expense_cols(ws) -> None:
    """Append any EXPENSE_COLS columns missing from the Expenses worksheet header."""
    global _expenses_migrated
    if _expenses_migrated:
        return
    header = ws.row_values(1)
    if not header:
        _expenses_migrated = True
        return
    missing = [col for col in EXPENSE_COLS if col not in header]
    if missing:
        needed = len(header) + len(missing)
        if needed > ws.col_count:
            ws.resize(rows=ws.row_count, cols=needed)
        start = len(header) + 1
        for i, col_name in enumerate(missing):
            ws.update_cell(1, start + i, col_name)
    _expenses_migrated = True


def _ensure_task_cols(ws) -> None:
    """Append any TASK_COLS columns missing from the Tasks worksheet header."""
    global _tasks_migrated
    if _tasks_migrated:
        return
    header = ws.row_values(1)
    if not header:
        _tasks_migrated = True
        return
    missing = [col for col in TASK_COLS if col not in header]
    if missing:
        needed = len(header) + len(missing)
        if needed > ws.col_count:
            ws.resize(rows=ws.row_count, cols=needed)
        start = len(header) + 1
        for i, col_name in enumerate(missing):
            ws.update_cell(1, start + i, col_name)
    _tasks_migrated = True

EXPENSE_CATEGORIES = [
    "Food & Dining",
    "Transport",
    "Accommodation",
    "Activities & Tours",
    "Shopping",
    "Health",
    "Communication",
    "Misc",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


@st.cache_resource(show_spinner=False)
def _get_client() -> gspread.Client:
    raw = cfg.service_account_json
    if isinstance(raw, str):
        info = json.loads(raw.strip())
    else:
        info = json.loads(json.dumps(dict(raw)))
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _spreadsheet() -> gspread.Spreadsheet:
    return _get_client().open_by_key(cfg.sheet_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _safe_tab_name(name: str) -> str:
    """Sanitise a trip name into a valid, unique-ish worksheet title (≤ 80 chars)."""
    cleaned = re.sub(r"[\\/*?\[\]:]", "", name).strip()
    cleaned = cleaned[:80] or "Trip"
    return cleaned


def _unique_tab_name(ss: gspread.Spreadsheet, base: str) -> str:
    """Append a counter suffix if the tab name already exists."""
    existing = {ws.title for ws in ss.worksheets()}
    if base not in existing:
        return base
    counter = 2
    while f"{base} ({counter})" in existing:
        counter += 1
    return f"{base} ({counter})"


# ─────────────────────────────────────────────────────────────────────────────
# One-time setup
# ─────────────────────────────────────────────────────────────────────────────


def ensure_sheets_exist() -> None:
    """Create the 'Trips', 'Expenses', and 'Tasks' metadata worksheets if they don't exist."""
    try:
        ss = _spreadsheet()
        existing = {ws.title for ws in ss.worksheets()}
        if "Trips" not in existing:
            ws = ss.add_worksheet(title="Trips", rows=1000, cols=len(TRIPS_COLS))
            ws.append_row(TRIPS_COLS)
        if "Expenses" not in existing:
            ws = ss.add_worksheet(title="Expenses", rows=2000, cols=len(EXPENSE_COLS))
            ws.append_row(EXPENSE_COLS)
        if "Tasks" not in existing:
            ws = ss.add_worksheet(title="Tasks", rows=2000, cols=len(TASK_COLS))
            ws.append_row(TASK_COLS)
    except Exception as exc:
        import traceback
        st.error(
            f"**Could not initialise Google Sheets:**\n```\n{traceback.format_exc()}\n```"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Trips CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_trips() -> pd.DataFrame:
    try:
        ws = _spreadsheet().worksheet("Trips")
        records = ws.get_all_records()
        return pd.DataFrame(records) if records else pd.DataFrame(columns=TRIPS_COLS)
    except Exception as exc:
        import traceback
        st.error(f"**Error loading trips:**\n```\n{traceback.format_exc()}\n```")
        return pd.DataFrame(columns=TRIPS_COLS)


def add_trip(
    trip_name: str,
    country: str,
    start_date,
    end_date,
    notes: str = "",
) -> str | None:
    """
    Creates a new trip row in 'Trips' AND a dedicated worksheet tab for
    that trip's itinerary.  Returns the trip_id, or None on failure.
    """
    try:
        ss = _spreadsheet()

        # 1. Determine a safe, unique worksheet tab name
        base_tab = _safe_tab_name(trip_name)
        tab_name = _unique_tab_name(ss, base_tab)

        # 2. Create the itinerary worksheet for this trip
        itin_ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(ITINERARY_COLS))
        itin_ws.append_row(ITINERARY_COLS)

        # 3. Record the trip in the Trips metadata sheet
        trip_id = f"trip_{uuid.uuid4().hex[:8]}"
        trips_ws = ss.worksheet("Trips")
        trips_ws.append_row(
            [
                trip_id,
                trip_name,
                country,
                str(start_date),
                str(end_date),
                notes,
                tab_name,
                datetime.now().isoformat(timespec="seconds"),
            ]
        )
        return trip_id

    except Exception as exc:
        st.error(f"Error adding trip: {exc}")
        return None


def delete_trip(trip_id: str) -> bool:
    """Delete the trip metadata row and its dedicated itinerary worksheet."""
    try:
        ss = _spreadsheet()
        trips_ws = ss.worksheet("Trips")
        records = trips_ws.get_all_records()

        tab_name = None
        for i, row in enumerate(records):
            if row["trip_id"] == trip_id:
                tab_name = row.get("sheet_tab")
                trips_ws.delete_rows(i + 2)
                break

        if tab_name:
            try:
                itin_ws = ss.worksheet(tab_name)
                ss.del_worksheet(itin_ws)
            except gspread.exceptions.WorksheetNotFound:
                pass  # already gone

        return True
    except Exception as exc:
        st.error(f"Error deleting trip: {exc}")
        return False


# Track which tabs have already been migrated in this process/session
_migrated_tabs: set[str] = set()


def _ensure_itinerary_cols(ws: gspread.Worksheet) -> None:
    """Append any ITINERARY_COLS columns missing from this worksheet header.

    Handles sheets created before new columns (icon, maps_url, order, …) were
    added to the schema. Expands the grid dimensions first to avoid the
    'exceeds grid limits' 400 error when the sheet has fewer columns than needed.
    """
    header = ws.row_values(1)
    if not header:
        return
    missing = [col for col in ITINERARY_COLS if col not in header]
    if not missing:
        return
    needed_cols = len(header) + len(missing)
    # Expand grid if current column count is too small
    if needed_cols > ws.col_count:
        ws.resize(rows=ws.row_count, cols=needed_cols)
    start_col = len(header) + 1
    for i, col_name in enumerate(missing):
        ws.update_cell(1, start_col + i, col_name)


def _trip_tab(trip_row: pd.Series) -> gspread.Worksheet:
    """Return the itinerary worksheet for a given trip row, migrating headers once."""
    tab_name = str(trip_row["sheet_tab"])
    ws = _spreadsheet().worksheet(tab_name)
    if tab_name not in _migrated_tabs:
        _ensure_itinerary_cols(ws)
        _migrated_tabs.add(tab_name)
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# Itinerary CRUD  (all operations target the trip-specific worksheet)
# ─────────────────────────────────────────────────────────────────────────────


def get_itinerary(trip_row: pd.Series) -> pd.DataFrame:
    try:
        ws = _trip_tab(trip_row)
        records = ws.get_all_records()
        df = pd.DataFrame(records) if records else pd.DataFrame(columns=ITINERARY_COLS)
        if not df.empty:
            df = df.sort_values("date", ignore_index=True)
        return df
    except Exception as exc:
        st.error(f"Error loading itinerary: {exc}")
        return pd.DataFrame(columns=ITINERARY_COLS)


def add_itinerary_entry(
    trip_row: pd.Series,
    entry_date,
    day_number: int,
    destination: str,
    description: str = "",
    price: str = "",
    currency: str = "USD",
    accommodation: str = "",
    links: str = "",
    additional_info: str = "",
    icon: str = "location_on",
    maps_url: str = "",
    time_start: str = "",
    time_end: str = "",
    order: int = 10,
) -> str | None:
    try:
        ws = _trip_tab(trip_row)
        entry_id = f"entry_{uuid.uuid4().hex[:10]}"
        # Build row aligned to the actual sheet header (handles old sheets missing new cols)
        actual_header = ws.row_values(1)
        values = {
            "entry_id": entry_id,
            "date": str(entry_date),
            "day_number": day_number,
            "order": order,
            "destination": destination,
            "description": description,
            "price": price,
            "currency": currency,
            "accommodation": accommodation,
            "links": links,
            "additional_info": additional_info,
            "icon": icon,
            "maps_url": maps_url,
            "time_start": time_start,
            "time_end": time_end,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        row = [values.get(col, "") for col in actual_header]
        ws.append_row(row)
        return entry_id
    except Exception as exc:
        st.error(f"Error adding itinerary entry: {exc}")
        return None


def update_itinerary_entry(trip_row: pd.Series, entry_id: str, **fields) -> bool:
    try:
        ws = _trip_tab(trip_row)
        records = ws.get_all_records()
        # Use actual sheet header for column positions (robust to schema evolution)
        actual_header = ws.row_values(1)
        col_index = {col: idx + 1 for idx, col in enumerate(actual_header)}

        for i, row in enumerate(records):
            if row["entry_id"] == entry_id:
                row_num = i + 2
                for key, value in fields.items():
                    if key in col_index:
                        ws.update_cell(row_num, col_index[key], str(value))
                return True

        st.warning(f"Entry {entry_id} not found.")
        return False
    except Exception as exc:
        st.error(f"Error updating entry: {exc}")
        return False


def delete_itinerary_entry(trip_row: pd.Series, entry_id: str) -> bool:
    try:
        ws = _trip_tab(trip_row)
        records = ws.get_all_records()
        for i, row in enumerate(records):
            if row["entry_id"] == entry_id:
                ws.delete_rows(i + 2)
                return True
        return False
    except Exception as exc:
        st.error(f"Error deleting entry: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Day titles  (stored as sentinel rows with entry_id = "daytitle_YYYY-MM-DD")
# ─────────────────────────────────────────────────────────────────────────────

_DAY_TITLE_PREFIX = "daytitle_"


def set_day_title(trip_row: pd.Series, date_str: str, title: str) -> bool:
    """Upsert a day-title sentinel row for the given date."""
    try:
        ws = _trip_tab(trip_row)
        eid = f"{_DAY_TITLE_PREFIX}{date_str}"
        records = ws.get_all_records()
        actual_header = ws.row_values(1)
        col_index = {col: idx + 1 for idx, col in enumerate(actual_header)}

        # Update if exists
        for i, row in enumerate(records):
            if row.get("entry_id") == eid:
                if "destination" in col_index:
                    ws.update_cell(i + 2, col_index["destination"], title)
                return True

        # Insert new sentinel row
        values = {col: "" for col in actual_header}
        values["entry_id"] = eid
        values["date"] = date_str
        values["destination"] = title
        ws.append_row([values.get(col, "") for col in actual_header])
        return True
    except Exception as exc:
        st.error(f"Error saving day title: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Expenses CRUD  (global "Expenses" worksheet, one row per manual expense)
# ─────────────────────────────────────────────────────────────────────────────


def get_expenses(trip_id: str) -> pd.DataFrame:
    """Return all manual expenses for the given trip, sorted by date."""
    try:
        ws = _spreadsheet().worksheet("Expenses")
        _ensure_expense_cols(ws)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=EXPENSE_COLS)
        df = pd.DataFrame(records)
        if "links" not in df.columns:
            df["links"] = ""
        df = df[df["trip_id"].astype(str) == str(trip_id)].reset_index(drop=True)
        if not df.empty:
            df = df.sort_values("date", ignore_index=True)
        return df
    except Exception as exc:
        st.error(f"Error loading expenses: {exc}")
        return pd.DataFrame(columns=EXPENSE_COLS)


def add_expense(
    trip_id: str,
    entry_date,
    category: str,
    description: str,
    amount: float,
    currency: str,
    links: str = "",
) -> str | None:
    try:
        ws = _spreadsheet().worksheet("Expenses")
        _ensure_expense_cols(ws)
        header = ws.row_values(1)
        expense_id = f"exp_{uuid.uuid4().hex[:10]}"
        data: dict = {
            "expense_id": expense_id,
            "trip_id": trip_id,
            "date": str(entry_date),
            "category": category,
            "description": description,
            "amount": amount,
            "currency": currency,
            "links": links,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        ws.append_row([data.get(col, "") for col in header])
        return expense_id
    except Exception as exc:
        st.error(f"Error adding expense: {exc}")
        return None


def delete_expense(expense_id: str) -> bool:
    try:
        ws = _spreadsheet().worksheet("Expenses")
        records = ws.get_all_records()
        for i, row in enumerate(records):
            if str(row.get("expense_id", "")) == str(expense_id):
                ws.delete_rows(i + 2)
                return True
        return False
    except Exception as exc:
        st.error(f"Error deleting expense: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tasks CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_tasks(trip_id: str) -> pd.DataFrame:
    try:
        ws = _spreadsheet().worksheet("Tasks")
        _ensure_task_cols(ws)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=TASK_COLS)
        df = pd.DataFrame(records)
        if "priority" not in df.columns:
            df["priority"] = "Normal"
        return df[df["trip_id"].astype(str) == str(trip_id)].reset_index(drop=True)
    except Exception as exc:
        st.error(f"Error loading tasks: {exc}")
        return pd.DataFrame(columns=TASK_COLS)


def add_task(
    trip_id: str,
    description: str,
    assigned_to: str,
    priority: str = "Normal",
    links: str = "",
) -> bool:
    try:
        ws = _spreadsheet().worksheet("Tasks")
        _ensure_task_cols(ws)
        header = ws.row_values(1)
        task_data: dict = {
            "task_id": str(uuid.uuid4()),
            "trip_id": trip_id,
            "description": description,
            "assigned_to": assigned_to,
            "priority": priority,
            "links": links,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        row = [task_data.get(col, "") for col in header]
        ws.append_row(row)
        return True
    except Exception as exc:
        st.error(f"Error adding task: {exc}")
        return False


def delete_task(task_id: str) -> bool:
    try:
        ws = _spreadsheet().worksheet("Tasks")
        records = ws.get_all_records()
        for i, row in enumerate(records):
            if str(row.get("task_id", "")) == str(task_id):
                ws.delete_rows(i + 2)
                return True
        return False
    except Exception as exc:
        st.error(f"Error deleting task: {exc}")
        return False
