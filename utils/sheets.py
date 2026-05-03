"""
Dual-write data layer.

READ  → Supabase only  (fast, no quota limits)
WRITE → Supabase (primary) + Google Sheets (audit/backup)

Exception: Nano Banana image_url stored only in Supabase (not yet implemented).
"""

import json
import re
import uuid
from datetime import datetime, date as _date, timedelta

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
    "category",         # expense category for this entry (matches EXPENSE_CATEGORIES)
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
    "title",            # short headline shown bold in the UI
    "description",      # optional longer detail / context
    "amount",
    "currency",
    "links",            # "Label | URL , Label2 | URL2"
    "created_at",
]

TASK_COLS = [
    "task_id",
    "trip_id",
    "description",
    "notes",            # optional longer details / context
    "due_date",         # optional ISO date string "YYYY-MM-DD"
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

# ── Toggle Google Sheets backup sync ─────────────────────────────────────────
# Set to True to re-enable dual-write to Google Sheets as an audit backup.
# When False every write goes to Supabase only (faster, no quota issues).
SHEETS_SYNC = False

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
    if not SHEETS_SYNC:
        return
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
    """WRITE to Supabase (primary). Optionally syncs to Google Sheets when SHEETS_SYNC=True."""
    try:
        trip_id  = f"trip_{uuid.uuid4().hex[:8]}"
        now      = datetime.now().isoformat(timespec="seconds")
        tab_name = _safe_tab_name(trip_name)

        if SHEETS_SYNC:
            try:
                ss       = _spreadsheet()
                tab_name = _unique_tab_name(ss, tab_name)
                itin_ws  = ss.add_worksheet(title=tab_name, rows=1000, cols=len(ITINERARY_COLS))
                itin_ws.append_row(ITINERARY_COLS)
                trips_ws = ss.worksheet("Trips")
                trips_ws.append_row([
                    trip_id, trip_name, country,
                    str(start_date), str(end_date),
                    notes, tab_name, now,
                ])
            except Exception as sh_exc:
                st.warning(f"Sheets sync warning (add trip): {sh_exc}")

        _sb().table("trips").insert({
            "trip_id":    trip_id,
            "trip_name":  trip_name,
            "country":    country,
            "start_date": str(start_date),
            "end_date":   str(end_date),
            "notes":      notes,
            "sheet_tab":  tab_name,
            "created_at": now,
        }).execute()

        return trip_id
    except Exception as exc:
        st.error(f"Error adding trip: {exc}")
        return None


def delete_trip(trip_id: str) -> bool:
    """WRITE to Supabase (cascade). Optionally syncs to Google Sheets when SHEETS_SYNC=True."""
    try:
        if SHEETS_SYNC:
            try:
                ss = _spreadsheet()
                trips_ws = ss.worksheet("Trips")
                records  = trips_ws.get_all_records()
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
            if "category" not in df.columns:
                df["category"] = ""
            df["category"] = df["category"].fillna("").astype(str)
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

        if SHEETS_SYNC:
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

        if SHEETS_SYNC:
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

        if SHEETS_SYNC:
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

        if SHEETS_SYNC:
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
            if "title" not in df.columns:
                df["title"] = ""
            if "description" not in df.columns:
                df["description"] = ""
            # Backfill: for legacy rows missing a title, fall back to description.
            df["title"] = df.apply(
                lambda r: str(r.get("title") or "").strip()
                          or str(r.get("description") or "").strip(),
                axis=1,
            )
            return df
        return pd.DataFrame(columns=EXPENSE_COLS)
    except Exception as exc:
        st.error(f"Error loading expenses: {exc}")
        return pd.DataFrame(columns=EXPENSE_COLS)


def add_expense(
    trip_id: str,
    entry_date,
    category: str,
    title: str,
    amount: float,
    currency: str,
    description: str = "",
    links: str = "",
) -> str | None:
    """WRITE to Supabase + Google Sheets."""
    try:
        expense_id = f"exp_{uuid.uuid4().hex[:10]}"
        now = datetime.now().isoformat(timespec="seconds")

        # Supabase write (primary)
        _sb().table("expenses").insert({
            "expense_id":  expense_id,
            "trip_id":     trip_id,
            "date":        str(entry_date),
            "category":    category,
            "title":       title,
            "description": description,
            "amount":      amount,
            "currency":    currency,
            "links":       links,
            "created_at":  now,
        }).execute()

        if SHEETS_SYNC:
            try:
                ws = _spreadsheet().worksheet("Expenses")
                _ensure_expense_cols(ws)
                header = ws.row_values(1)
                data = {
                    "expense_id":  expense_id, "trip_id": trip_id,
                    "date":        str(entry_date), "category": category,
                    "title":       title,
                    "description": description, "amount": amount,
                    "currency":    currency, "links": links, "created_at": now,
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

        if SHEETS_SYNC:
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
            if "notes" not in df.columns:
                df["notes"] = ""
            if "due_date" not in df.columns:
                df["due_date"] = ""
            df["done"]     = df["done"].fillna(False).astype(bool)
            df["entry_id"] = df["entry_id"].fillna("").astype(str)
            df["notes"]    = df["notes"].fillna("").astype(str)
            df["due_date"] = df["due_date"].fillna("").astype(str)
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
    notes: str = "",
    due_date: str = "",
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
            "notes": notes,
            "due_date": due_date or None,
            "assigned_to": assigned_to,
            "priority": priority,
            "links": links,
            "done": False,
            "entry_id": entry_id,
            "created_at": now,
        }).execute()

        if SHEETS_SYNC:
            try:
                ws = _spreadsheet().worksheet("Tasks")
                _ensure_task_cols(ws)
                header = ws.row_values(1)
                data = {
                    "task_id": task_id, "trip_id": trip_id,
                    "description": description, "notes": notes,
                    "due_date": due_date, "assigned_to": assigned_to,
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


# ─────────────────────────────────────────────────────────────────────────────
# Equipment CRUD  (Supabase only — no Sheets sync)
# ─────────────────────────────────────────────────────────────────────────────


def get_equipment(trip_id: str, owner: str) -> pd.DataFrame:
    """READ from Supabase."""
    try:
        resp = (
            _sb().table("equipment")
            .select("*")
            .eq("trip_id", trip_id)
            .eq("owner", owner)
            .order("created_at")
            .execute()
        )
        if resp.data:
            return pd.DataFrame(resp.data)
        return pd.DataFrame(columns=["item_id", "trip_id", "owner", "description", "checked", "created_at"])
    except Exception as exc:
        st.error(f"Error loading equipment: {exc}")
        return pd.DataFrame()


def add_equipment_item(trip_id: str, owner: str, description: str) -> bool:
    try:
        _sb().table("equipment").insert({
            "item_id": f"eq_{uuid.uuid4().hex[:10]}",
            "trip_id": trip_id,
            "owner": owner,
            "description": description,
            "checked": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }).execute()
        return True
    except Exception as exc:
        st.error(f"Error adding item: {exc}")
        return False


def toggle_equipment_item(item_id: str, checked: bool) -> bool:
    try:
        _sb().table("equipment").update({"checked": checked}).eq("item_id", item_id).execute()
        return True
    except Exception:
        return False


def delete_equipment_item(item_id: str) -> bool:
    try:
        _sb().table("equipment").delete().eq("item_id", item_id).execute()
        return True
    except Exception:
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

        if SHEETS_SYNC:
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


# ─────────────────────────────────────────────────────────────────────────────
# Date shift  (bulk itinerary mutation)
# ─────────────────────────────────────────────────────────────────────────────


def move_day_entries(
    trip_id: str,
    trip_start_str: str,
    trip_end_str: str,
    from_date_str: str,
    to_date_str: str,
    dry_run: bool = False,
) -> tuple[bool, str, list[tuple[str, str]]]:
    """Shift this day and all subsequent days by delta = to_date - from_date.

    Returns (True, "", [(old_date, new_date), ...]) listing every day that moves.
    Returns (False, error_msg, []) on bounds violation or DB error.
    When dry_run=True the DB is never touched.
    """
    try:
        from_dt = _date.fromisoformat(from_date_str)
        to_dt   = _date.fromisoformat(to_date_str)
        delta   = (to_dt - from_dt).days

        if delta == 0:
            return True, "", []

        trip_start_dt = _date.fromisoformat(trip_start_str)
        trip_end_dt   = _date.fromisoformat(trip_end_str) if trip_end_str else None

        # Gather all unique dates >= from_date for this trip
        resp = (
            _sb().table("itinerary")
            .select("entry_id,date")
            .eq("trip_id", trip_id)
            .gte("date", from_date_str)
            .order("date")
            .execute()
        )
        if not resp.data:
            return True, "", []

        unique_dates = sorted(set(r["date"] for r in resp.data))

        # Validate bounds for every date that would move
        pairs: list[tuple[str, str]] = []
        for d_str in unique_dates:
            old_dt = _date.fromisoformat(d_str)
            new_dt = old_dt + _timedelta(days=delta)
            if new_dt < trip_start_dt:
                return False, (
                    f"Shifting would move **{d_str}** to **{new_dt}**, "
                    f"which is before the trip start ({trip_start_str})."
                ), []
            if trip_end_dt and new_dt > trip_end_dt:
                return False, (
                    f"Shifting would move **{d_str}** to **{new_dt}**, "
                    f"which is past the trip end ({trip_end_str})."
                ), []
            pairs.append((d_str, str(new_dt)))

        if dry_run:
            return True, "", pairs

        # Apply — process in reverse when moving forward to avoid transient collisions
        ordered = pairs if delta < 0 else list(reversed(pairs))
        for old_d, new_d in ordered:
            new_day_num = (_date.fromisoformat(new_d) - trip_start_dt).days + 1
            rows = [r for r in resp.data if r["date"] == old_d]
            for row in rows:
                _sb().table("itinerary").update({
                    "date":       new_d,
                    "day_number": new_day_num,
                }).eq("entry_id", str(row["entry_id"])).execute()

        return True, "", pairs
    except Exception as exc:
        return False, str(exc), []


def shift_itinerary_dates(
    trip_id: str,
    trip_start_str: str,
    from_date_str: str,
    delta_days: int,
) -> tuple[bool, int]:
    """Shift every itinerary row (including day-title sentinels) whose `date` ≥
    `from_date_str` forward by `delta_days` days.

    Also updates `day_number` relative to the trip start.
    Returns (success, rows_updated).
    """
    if delta_days == 0:
        return True, 0
    try:
        resp = (
            _sb().table("itinerary")
            .select("entry_id,date,day_number")
            .eq("trip_id", trip_id)
            .gte("date", from_date_str)
            .execute()
        )
        if not resp.data:
            return True, 0

        trip_start_dt = _date.fromisoformat(trip_start_str)
        count = 0
        for row in resp.data:
            old_date = _date.fromisoformat(str(row["date"]))
            new_date = old_date + timedelta(days=delta_days)
            new_day  = (new_date - trip_start_dt).days + 1
            _sb().table("itinerary").update({
                "date":       str(new_date),
                "day_number": new_day,
            }).eq("entry_id", str(row["entry_id"])).execute()
            count += 1

        return True, count
    except Exception as exc:
        st.error(f"Error shifting dates: {exc}")
        return False, 0


# ─────────────────────────────────────────────────────────────────────────────
# Analysis CRUD  (Supabase only — AI agent reports + findings)
# ─────────────────────────────────────────────────────────────────────────────

ANALYSIS_SECTIONS = ("logistics", "pace", "gas", "financial", "tasks", "stamps")
ANALYSIS_TERMINAL = ("completed", "failed")


def insert_analysis_report(report_id: str, trip_id: str, created_by: str) -> bool:
    """Create a fresh report row with all sections set to 'running'."""
    try:
        now = datetime.now().isoformat(timespec="seconds")
        row = {
            "report_id":  report_id,
            "trip_id":    trip_id,
            "created_at": now,
            "created_by": created_by,
        }
        for section in ANALYSIS_SECTIONS:
            row[f"{section}_status"] = "running"
        _sb().table("analysis_reports").insert(row).execute()
        return True
    except Exception as exc:
        st.error(f"Error creating analysis report: {exc}")
        return False


def update_report_section(report_id: str, section: str, **fields) -> bool:
    """Update one section's status / summary / error on a report.

    `section` must be one of ANALYSIS_SECTIONS.
    Allowed kwargs: status, summary, error.
    """
    if section not in ANALYSIS_SECTIONS:
        return False
    payload: dict[str, str] = {}
    if "status"  in fields: payload[f"{section}_status"]  = str(fields["status"])
    if "summary" in fields: payload[f"{section}_summary"] = str(fields["summary"] or "")
    if "error"   in fields: payload[f"{section}_error"]   = str(fields["error"]   or "")
    if not payload:
        return False
    try:
        _sb().table("analysis_reports").update(payload).eq("report_id", report_id).execute()
        return True
    except Exception:
        return False


def get_running_report(trip_id: str) -> dict | None:
    """Return the latest report for the trip if ANY section is still running.

    NOTE: only checks columns that are present in the row. New sections that
    haven't been migrated into the table yet won't trip the running check.
    """
    try:
        resp = (
            _sb().table("analysis_reports")
            .select("*")
            .eq("trip_id", trip_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
        for s in ANALYSIS_SECTIONS:
            col = f"{s}_status"
            if col in row and str(row.get(col, "")) == "running":
                return row
        return None
    except Exception:
        return None


def get_latest_report(trip_id: str) -> dict | None:
    """Return the most recently created report row for this trip (any status)."""
    try:
        resp = (
            _sb().table("analysis_reports")
            .select("*")
            .eq("trip_id", trip_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


def get_report(report_id: str) -> dict | None:
    try:
        resp = (
            _sb().table("analysis_reports")
            .select("*")
            .eq("report_id", report_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


def list_reports(trip_id: str) -> pd.DataFrame:
    """Return all reports for a trip, newest first."""
    try:
        resp = (
            _sb().table("analysis_reports")
            .select("*")
            .eq("trip_id", trip_id)
            .order("created_at", desc=True)
            .execute()
        )
        return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def insert_finding(
    finding_id:      str,
    report_id:       str,
    category:        str,
    kind:            str,
    title:           str,
    description:     str = "",
    severity:        str = "info",
    target_entry_id: str = "",
    payload:         dict | None = None,
) -> bool:
    try:
        now = datetime.now().isoformat(timespec="seconds")
        _sb().table("analysis_findings").insert({
            "finding_id":      finding_id,
            "report_id":       report_id,
            "category":        category,
            "kind":            kind,
            "title":           title,
            "description":     description,
            "severity":        severity,
            "target_entry_id": target_entry_id or "",
            "payload":         payload or {},
            "status":          "pending",
            "created_at":      now,
        }).execute()
        return True
    except Exception:
        return False


def update_finding_status(finding_id: str, status: str, user: str) -> bool:
    """Mark a finding accepted / dismissed (or back to pending)."""
    try:
        _sb().table("analysis_findings").update({
            "status":   status,
            "acted_at": datetime.now().isoformat(timespec="seconds"),
            "acted_by": user,
        }).eq("finding_id", finding_id).execute()
        return True
    except Exception:
        return False


def list_findings(
    report_id: str,
    category:  str | None = None,
    status:    str | None = None,
) -> pd.DataFrame:
    """Return findings for a report, optionally filtered by category / status."""
    try:
        q = (
            _sb().table("analysis_findings")
            .select("*")
            .eq("report_id", report_id)
            .order("created_at")
        )
        if category:
            q = q.eq("category", category)
        if status:
            q = q.eq("status", status)
        resp = q.execute()
        return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_finding(finding_id: str) -> dict | None:
    try:
        resp = (
            _sb().table("analysis_findings")
            .select("*")
            .eq("finding_id", finding_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


def get_itinerary_entry(entry_id: str) -> dict | None:
    """Fetch a single itinerary entry by id (used when applying findings)."""
    try:
        resp = (
            _sb().table("itinerary")
            .select("*")
            .eq("entry_id", entry_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None
