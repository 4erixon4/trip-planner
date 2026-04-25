"""
Dual-write data layer.

READ  → Supabase only  (fast, no quota limits)
WRITE → Supabase (primary) + Google Sheets (audit/backup)

Exception: Nano Banana image_url stored only in Supabase (not yet implemented).
"""

import json
import re
import uuid
from datetime import datetime

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from supabase import create_client, Client as SupabaseClient

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
    "sheet_tab",   # name of the dedicated worksheet tab (Google Sheets side)
    "created_at",
]

ITINERARY_COLS = [
    "entry_id",
    "date",
    "day_number",
    "order",            # integer display order within a day (lower = earlier)
    "destination",
    "description",
    "price",
    "currency",
    "accommodation",
    "links",
    "additional_info",
    "icon",             # material icon name, e.g. "location_on"
    "maps_url",         # Google Maps URL or search query
    "time_start",       # HH:MM e.g. "14:00"
    "time_end",         # HH:MM optional e.g. "16:00"
    "image_prompt",     # user-provided seed for Nano Banana illustration
    "image_url",        # public URL of the generated illustration in Supabase Storage
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
    "links",            # "Label | URL , Label2 | URL2"
    "created_at",
]

TASK_COLS = [
    "task_id",
    "trip_id",
    "description",
    "assigned_to",      # Gmail address or "Unassigned"
    "priority",         # "High" | "Medium" | "Normal"
    "links",            # "Label | URL , Label2 | URL2"
    "done",             # bool – completed but kept for history
    "entry_id",         # optional link to an itinerary entry
    "created_at",
]

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

_DAY_TITLE_PREFIX = "daytitle_"

# ─────────────────────────────────────────────────────────────────────────────
# Clients
# ─────────────────────────────────────────────────────────────────────────────


@st.cache_resource(show_spinner=False)
def _get_supabase() -> SupabaseClient:
    return create_client(cfg.supabase_url, cfg.supabase_key)


def _sb() -> SupabaseClient:
    return _get_supabase()


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
# Google Sheets helpers (used only on write path)
# ─────────────────────────────────────────────────────────────────────────────

_tasks_migrated: bool = False
_expenses_migrated: bool = False
_migrated_tabs: set[str] = set()


def _safe_tab_name(name: str) -> str:
    cleaned = re.sub(r"[\\/*?\[\]:]", "", name).strip()
    cleaned = cleaned[:80] or "Trip"
    return cleaned


def _unique_tab_name(ss: gspread.Spreadsheet, base: str) -> str:
    existing = {ws.title for ws in ss.worksheets()}
    if base not in existing:
        return base
    counter = 2
    while f"{base} ({counter})" in existing:
        counter += 1
    return f"{base} ({counter})"


def _ensure_itinerary_cols(ws: gspread.Worksheet) -> None:
    header = ws.row_values(1)
    if not header:
        return
    missing = [col for col in ITINERARY_COLS if col not in header]
    if not missing:
        return
    needed_cols = len(header) + len(missing)
    if needed_cols > ws.col_count:
        ws.resize(rows=ws.row_count, cols=needed_cols)
    start_col = len(header) + 1
    for i, col_name in enumerate(missing):
        ws.update_cell(1, start_col + i, col_name)


def _ensure_expense_cols(ws) -> None:
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


def _trip_tab(trip_row: pd.Series) -> gspread.Worksheet:
    """Return the itinerary worksheet for a given trip row, migrating headers once."""
    tab_name = str(trip_row["sheet_tab"])
    ws = _spreadsheet().worksheet(tab_name)
    if tab_name not in _migrated_tabs:
        _ensure_itinerary_cols(ws)
        _migrated_tabs.add(tab_name)
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# One-time setup
# ─────────────────────────────────────────────────────────────────────────────


def ensure_sheets_exist() -> None:
    """Create the Trips/Expenses/Tasks worksheets in Google Sheets if absent."""
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
    except Exception:
        import traceback
        st.error(f"**Could not initialise Google Sheets:**\n```\n{traceback.format_exc()}\n```")


# ─────────────────────────────────────────────────────────────────────────────
# Trips CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_trips() -> pd.DataFrame:
    """READ from Supabase."""
    try:
        resp = _sb().table("trips").select("*").order("created_at").execute()
        if resp.data:
            df = pd.DataFrame(resp.data)
            # Ensure sheet_tab exists (needed for Google Sheets write path)
            if "sheet_tab" not in df.columns:
                df["sheet_tab"] = df["trip_name"].apply(_safe_tab_name)
            return df
        return pd.DataFrame(columns=TRIPS_COLS)
    except Exception:
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
    """WRITE to Google Sheets first (to create tab), then Supabase."""
    try:
        ss = _spreadsheet()
        base_tab = _safe_tab_name(trip_name)
        tab_name = _unique_tab_name(ss, base_tab)
        trip_id = f"trip_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat(timespec="seconds")

        # Google Sheets: create itinerary tab + metadata row
        itin_ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(ITINERARY_COLS))
        itin_ws.append_row(ITINERARY_COLS)
        trips_ws = ss.worksheet("Trips")
        trips_ws.append_row([
            trip_id, trip_name, country,
            str(start_date), str(end_date),
            notes, tab_name, now,
        ])

        # Supabase: insert trip row
        try:
            _sb().table("trips").insert({
                "trip_id": trip_id,
                "trip_name": trip_name,
                "country": country,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "notes": notes,
                "sheet_tab": tab_name,
                "created_at": now,
            }).execute()
        except Exception as sb_exc:
            st.warning(f"Supabase sync warning (add trip): {sb_exc}")

        return trip_id
    except Exception as exc:
        st.error(f"Error adding trip: {exc}")
        return None


def delete_trip(trip_id: str) -> bool:
    """WRITE to both Supabase (cascade) + Google Sheets."""
    try:
        # Google Sheets: remove metadata row and itinerary tab
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
                    ss.del_worksheet(ss.worksheet(tab_name))
                except gspread.exceptions.WorksheetNotFound:
                    pass
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (delete trip): {sh_exc}")

        # Supabase: delete trip (cascades to itinerary, expenses, tasks)
        _sb().table("trips").delete().eq("trip_id", trip_id).execute()
        return True
    except Exception as exc:
        st.error(f"Error deleting trip: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Itinerary CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_itinerary(trip_row: pd.Series) -> pd.DataFrame:
    """READ from Supabase."""
    try:
        trip_id = str(trip_row["trip_id"])
        resp = (
            _sb().table("itinerary")
            .select("*")
            .eq("trip_id", trip_id)
            .order("date")
            .execute()
        )
        if resp.data:
            df = pd.DataFrame(resp.data)
            return df.sort_values("date", ignore_index=True)
        return pd.DataFrame(columns=ITINERARY_COLS)
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
    image_prompt: str = "",
    image_url: str = "",
) -> str | None:
    """WRITE to Supabase + Google Sheets."""
    try:
        entry_id = f"entry_{uuid.uuid4().hex[:10]}"
        now = datetime.now().isoformat(timespec="seconds")
        trip_id = str(trip_row["trip_id"])

        # Supabase write (primary)
        _sb().table("itinerary").insert({
            "entry_id": entry_id,
            "trip_id": trip_id,
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
            "image_prompt": image_prompt,
            "image_url": image_url,
            "created_at": now,
        }).execute()

        # Google Sheets write (backup)
        try:
            ws = _trip_tab(trip_row)
            actual_header = ws.row_values(1)
            values = {
                "entry_id": entry_id, "date": str(entry_date),
                "day_number": day_number, "order": order,
                "destination": destination, "description": description,
                "price": price, "currency": currency,
                "accommodation": accommodation, "links": links,
                "additional_info": additional_info, "icon": icon,
                "maps_url": maps_url, "time_start": time_start,
                "time_end": time_end, "image_prompt": image_prompt,
                "image_url": image_url, "created_at": now,
            }
            ws.append_row([values.get(col, "") for col in actual_header])
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (add entry): {sh_exc}")

        return entry_id
    except Exception as exc:
        st.error(f"Error adding itinerary entry: {exc}")
        return None


def update_itinerary_entry(trip_row: pd.Series, entry_id: str, **fields) -> bool:
    """WRITE to Supabase + Google Sheets."""
    try:
        # Supabase update (primary)
        _sb().table("itinerary").update(
            {k: str(v) for k, v in fields.items()}
        ).eq("entry_id", entry_id).execute()

        # Google Sheets update (backup)
        try:
            ws = _trip_tab(trip_row)
            records = ws.get_all_records()
            actual_header = ws.row_values(1)
            col_index = {col: idx + 1 for idx, col in enumerate(actual_header)}
            for i, row in enumerate(records):
                if row["entry_id"] == entry_id:
                    for key, value in fields.items():
                        if key in col_index:
                            ws.update_cell(i + 2, col_index[key], str(value))
                    break
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (update entry): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error updating entry: {exc}")
        return False


def delete_itinerary_entry(trip_row: pd.Series, entry_id: str) -> bool:
    """WRITE to Supabase + Google Sheets."""
    try:
        # Supabase delete (primary)
        _sb().table("itinerary").delete().eq("entry_id", entry_id).execute()

        # Google Sheets delete (backup)
        try:
            ws = _trip_tab(trip_row)
            records = ws.get_all_records()
            for i, row in enumerate(records):
                if row["entry_id"] == entry_id:
                    ws.delete_rows(i + 2)
                    break
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (delete entry): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error deleting entry: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Day titles  (sentinel rows: entry_id = "daytitle_YYYY-MM-DD")
# ─────────────────────────────────────────────────────────────────────────────


def set_day_title(trip_row: pd.Series, date_str: str, title: str) -> bool:
    """Upsert a day-title sentinel row. WRITE to Supabase + Google Sheets."""
    try:
        eid = f"{_DAY_TITLE_PREFIX}{date_str}"
        trip_id = str(trip_row["trip_id"])
        now = datetime.now().isoformat(timespec="seconds")

        # Supabase upsert (primary)
        _sb().table("itinerary").upsert({
            "entry_id": eid,
            "trip_id": trip_id,
            "date": date_str,
            "destination": title,
            "created_at": now,
        }).execute()

        # Google Sheets upsert (backup)
        try:
            ws = _trip_tab(trip_row)
            records = ws.get_all_records()
            actual_header = ws.row_values(1)
            col_index = {col: idx + 1 for idx, col in enumerate(actual_header)}
            found = False
            for i, row in enumerate(records):
                if row.get("entry_id") == eid:
                    if "destination" in col_index:
                        ws.update_cell(i + 2, col_index["destination"], title)
                    found = True
                    break
            if not found:
                values = {col: "" for col in actual_header}
                values["entry_id"] = eid
                values["date"] = date_str
                values["destination"] = title
                ws.append_row([values.get(col, "") for col in actual_header])
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (day title): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error saving day title: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Expenses CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_expenses(trip_id: str) -> pd.DataFrame:
    """READ from Supabase."""
    try:
        resp = (
            _sb().table("expenses")
            .select("*")
            .eq("trip_id", trip_id)
            .order("date")
            .execute()
        )
        if resp.data:
            df = pd.DataFrame(resp.data)
            if "links" not in df.columns:
                df["links"] = ""
            return df
        return pd.DataFrame(columns=EXPENSE_COLS)
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
    """WRITE to Supabase + Google Sheets."""
    try:
        expense_id = f"exp_{uuid.uuid4().hex[:10]}"
        now = datetime.now().isoformat(timespec="seconds")

        # Supabase write (primary)
        _sb().table("expenses").insert({
            "expense_id": expense_id,
            "trip_id": trip_id,
            "date": str(entry_date),
            "category": category,
            "description": description,
            "amount": amount,
            "currency": currency,
            "links": links,
            "created_at": now,
        }).execute()

        # Google Sheets write (backup)
        try:
            ws = _spreadsheet().worksheet("Expenses")
            _ensure_expense_cols(ws)
            header = ws.row_values(1)
            data = {
                "expense_id": expense_id, "trip_id": trip_id,
                "date": str(entry_date), "category": category,
                "description": description, "amount": amount,
                "currency": currency, "links": links, "created_at": now,
            }
            ws.append_row([data.get(col, "") for col in header])
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (add expense): {sh_exc}")

        return expense_id
    except Exception as exc:
        st.error(f"Error adding expense: {exc}")
        return None


def delete_expense(expense_id: str) -> bool:
    """WRITE to Supabase + Google Sheets."""
    try:
        # Supabase delete (primary)
        _sb().table("expenses").delete().eq("expense_id", expense_id).execute()

        # Google Sheets delete (backup)
        try:
            ws = _spreadsheet().worksheet("Expenses")
            records = ws.get_all_records()
            for i, row in enumerate(records):
                if str(row.get("expense_id", "")) == str(expense_id):
                    ws.delete_rows(i + 2)
                    break
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (delete expense): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error deleting expense: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tasks CRUD
# ─────────────────────────────────────────────────────────────────────────────


def get_tasks(trip_id: str) -> pd.DataFrame:
    """READ from Supabase."""
    try:
        resp = (
            _sb().table("tasks")
            .select("*")
            .eq("trip_id", trip_id)
            .execute()
        )
        if resp.data:
            df = pd.DataFrame(resp.data)
            if "priority" not in df.columns:
                df["priority"] = "Normal"
            if "done" not in df.columns:
                df["done"] = False
            if "entry_id" not in df.columns:
                df["entry_id"] = ""
            df["done"] = df["done"].fillna(False).astype(bool)
            df["entry_id"] = df["entry_id"].fillna("").astype(str)
            return df
        return pd.DataFrame(columns=TASK_COLS)
    except Exception as exc:
        st.error(f"Error loading tasks: {exc}")
        return pd.DataFrame(columns=TASK_COLS)


def add_task(
    trip_id: str,
    description: str,
    assigned_to: str,
    priority: str = "Normal",
    links: str = "",
    entry_id: str = "",
) -> bool:
    """WRITE to Supabase + Google Sheets."""
    try:
        task_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec="seconds")

        # Supabase write (primary)
        _sb().table("tasks").insert({
            "task_id": task_id,
            "trip_id": trip_id,
            "description": description,
            "assigned_to": assigned_to,
            "priority": priority,
            "links": links,
            "done": False,
            "entry_id": entry_id,
            "created_at": now,
        }).execute()

        # Google Sheets write (backup)
        try:
            ws = _spreadsheet().worksheet("Tasks")
            _ensure_task_cols(ws)
            header = ws.row_values(1)
            data = {
                "task_id": task_id, "trip_id": trip_id,
                "description": description, "assigned_to": assigned_to,
                "priority": priority, "links": links,
                "done": False, "entry_id": entry_id, "created_at": now,
            }
            ws.append_row([data.get(col, "") for col in header])
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (add task): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error adding task: {exc}")
        return False


def mark_task_done(task_id: str) -> bool:
    """Mark a task as done (keeps it in history). Supabase only."""
    try:
        _sb().table("tasks").update({"done": True}).eq("task_id", task_id).execute()
        return True
    except Exception as exc:
        st.error(f"Error marking task done: {exc}")
        return False


def link_task_to_entry(task_id: str, entry_id: str) -> bool:
    """Attach a task to an itinerary entry. Supabase only."""
    try:
        _sb().table("tasks").update({"entry_id": entry_id}).eq("task_id", task_id).execute()
        return True
    except Exception:
        return False


def unlink_task_from_entry(task_id: str) -> bool:
    """Remove the entry link from a task. Supabase only."""
    try:
        _sb().table("tasks").update({"entry_id": ""}).eq("task_id", task_id).execute()
        return True
    except Exception:
        return False


def delete_task(task_id: str) -> bool:
    """WRITE to Supabase + Google Sheets."""
    try:
        # Supabase delete (primary)
        _sb().table("tasks").delete().eq("task_id", task_id).execute()

        # Google Sheets delete (backup)
        try:
            ws = _spreadsheet().worksheet("Tasks")
            records = ws.get_all_records()
            for i, row in enumerate(records):
                if str(row.get("task_id", "")) == str(task_id):
                    ws.delete_rows(i + 2)
                    break
        except Exception as sh_exc:
            st.warning(f"Sheets sync warning (delete task): {sh_exc}")

        return True
    except Exception as exc:
        st.error(f"Error deleting task: {exc}")
        return False
