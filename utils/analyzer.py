"""
Logistical AI Helper — async 3-stage report agent.

Stages (each runs in its own background thread):
  1. logistics  — Gemini Pro (JSON)         : time mismatches, missing meals
  2. gas        — Gemini Pro + Google Search: km commute + fuel suggestions
  3. tasks      — Gemini Flash (JSON)        : missing prep tasks & equipment

Lifecycle (status field per section in `analysis_reports`):
    pending → running → completed  (or → failed with *_error populated)

──────────────────────────────────────────────────────────────────────────────
Required Supabase schema  (run once in the SQL editor)
──────────────────────────────────────────────────────────────────────────────

create table if not exists analysis_reports (
  report_id        text primary key,
  trip_id          text not null references trips(trip_id) on delete cascade,
  created_at       timestamptz not null default now(),
  created_by       text,
  logistics_status text not null default 'pending',
  logistics_summary text default '',
  logistics_error  text default '',
  gas_status       text not null default 'pending',
  gas_summary      text default '',
  gas_error        text default '',
  tasks_status     text not null default 'pending',
  tasks_summary    text default '',
  tasks_error      text default ''
);
create index if not exists analysis_reports_trip_idx
  on analysis_reports(trip_id, created_at desc);

create table if not exists analysis_findings (
  finding_id      text primary key,
  report_id       text not null references analysis_reports(report_id) on delete cascade,
  category        text not null,
  kind            text not null,
  title           text not null,
  description     text default '',
  severity        text not null default 'info',
  target_entry_id text default '',
  payload         jsonb not null default '{}'::jsonb,
  status          text not null default 'pending',
  acted_at        timestamptz,
  acted_by        text,
  created_at      timestamptz not null default now()
);
create index if not exists analysis_findings_report_idx
  on analysis_findings(report_id, category, status);
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime

import pandas as pd

from utils.gemini_helper import MODEL, MODEL_PRO, generate_structured
from utils.sheets import (
    _sb,
    add_equipment_item,
    add_task,
    get_equipment,
    get_finding,
    get_itinerary,
    get_itinerary_entry,
    get_running_report,
    get_tasks,
    get_trips,
    insert_analysis_report,
    insert_finding,
    update_finding_status,
    update_itinerary_entry,
    update_report_section,
)
from utils.config import cfg

# ─── Constants ───────────────────────────────────────────────────────────────

VALID_KINDS    = {"append_text", "append_bullets", "create_task", "create_equipment", "note"}
VALID_SEVERITY = {"info", "warning", "error"}
TASK_PRIORITIES = ("Normal", "Medium", "High")

# JSON schema (Gemini structured output). Used for logistics + tasks runs.
# Gas run uses Search grounding which forbids response_schema, so we instruct
# the model to return the same shape via the prompt.
_FINDINGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind":            {"type": "string", "enum": list(VALID_KINDS)},
                    "title":           {"type": "string"},
                    "description":     {"type": "string"},
                    "severity":        {"type": "string", "enum": list(VALID_SEVERITY)},
                    "target_entry_id": {"type": "string"},
                    "payload": {
                        "type": "object",
                        "properties": {
                            "content":     {"type": "string"},
                            "description": {"type": "string"},
                            "priority":    {"type": "string"},
                            "notes":       {"type": "string"},
                            "due_date":    {"type": "string"},
                            "links":       {"type": "string"},
                            "owner":       {"type": "string"},
                        },
                    },
                },
                "required": ["kind", "title", "description", "severity"],
            },
        },
    },
    "required": ["summary", "findings"],
}

_SHARED_PROMPT_RULES = """
Output rules (STRICT):
- Respond with a single JSON object matching the schema. No prose outside JSON.
- Each finding's `kind` must be one of:
    "append_text"     — short text/sentence to append to a destination's "Additional info"
    "append_bullets"  — markdown bullet list to append to a destination's "Additional info"
    "create_task"     — a new to-do item for the trip
    "create_equipment"— a new packing-list item for someone
    "note"            — informational only, nothing to apply
- For append_text / append_bullets, MUST set `target_entry_id` to one of the
  entry_ids from the itinerary listed below. The text/bullets goes in payload.content.
- For create_task, payload MUST include: description, priority (Normal|Medium|High).
  Optional: notes, due_date (YYYY-MM-DD), links, target_entry_id.
- For create_equipment, payload MUST include: description. Optional: owner (email).
- Severity: "info" (helpful), "warning" (likely problem), "error" (definite gap).
- Keep titles short (≤ 80 chars). Be specific, not generic.
- Do NOT duplicate existing tasks or equipment items already listed in context.
"""

# ─── Trip context builder ────────────────────────────────────────────────────


def _build_trip_context(trip_id: str) -> dict:
    """Compact dict snapshot of the trip used as input to every AI run."""
    trips = get_trips()
    if trips.empty:
        return {}
    matches = trips[trips["trip_id"] == trip_id]
    if matches.empty:
        return {}
    trip = matches.iloc[0]

    itin = get_itinerary(trip)
    entries: list[dict] = []
    day_titles: dict[str, str] = {}
    if not itin.empty:
        for _, row in itin.iterrows():
            eid = str(row.get("entry_id", ""))
            if eid.startswith("daytitle_"):
                day_titles[str(row.get("date", ""))] = str(row.get("destination", ""))
                continue
            entries.append({
                "entry_id":        eid,
                "date":            str(row.get("date", "")),
                "day_number":      int(float(row.get("day_number", 0) or 0)),
                "order":           int(float(row.get("order", 0) or 0)),
                "time_start":      str(row.get("time_start", "") or ""),
                "time_end":        str(row.get("time_end", "") or ""),
                "destination":     str(row.get("destination", "") or ""),
                "description":     str(row.get("description", "") or ""),
                "accommodation":   str(row.get("accommodation", "") or ""),
                "icon":            str(row.get("icon", "") or ""),
                "maps_url":        str(row.get("maps_url", "") or ""),
                "additional_info": str(row.get("additional_info", "") or ""),
                "price":           str(row.get("price", "") or ""),
                "currency":        str(row.get("currency", "") or ""),
            })
        entries.sort(key=lambda e: (e["date"], e["order"]))

    tasks_df = get_tasks(trip_id)
    tasks: list[dict] = []
    if not tasks_df.empty:
        for _, t in tasks_df.iterrows():
            tasks.append({
                "description": str(t.get("description", "") or ""),
                "priority":    str(t.get("priority", "Normal") or "Normal"),
                "done":        bool(t.get("done", False)),
                "assigned_to": str(t.get("assigned_to", "") or ""),
            })

    equipment: dict[str, list[str]] = {}
    for owner in cfg.approved_emails:
        eq_df = get_equipment(trip_id, owner)
        equipment[owner] = (
            eq_df["description"].astype(str).tolist() if not eq_df.empty else []
        )

    return {
        "trip": {
            "trip_id":    str(trip["trip_id"]),
            "trip_name":  str(trip["trip_name"]),
            "country":    str(trip["country"]),
            "start_date": str(trip["start_date"]),
            "end_date":   str(trip["end_date"]),
            "notes":      str(trip.get("notes", "") or ""),
        },
        "day_titles": day_titles,
        "entries":    entries,
        "tasks":      tasks,
        "equipment":  equipment,
        "approved_emails": list(cfg.approved_emails),
    }


def _ctx_to_text(ctx: dict) -> str:
    """Pretty serialise the context into a compact text block for the prompt."""
    return json.dumps(ctx, ensure_ascii=False, indent=2)


# ─── Persistence helpers used by background threads ──────────────────────────


def _persist_findings(report_id: str, category: str, parsed: dict) -> None:
    """Validate & write each finding row from a parsed AI response."""
    findings = parsed.get("findings") or []
    if not isinstance(findings, list):
        return
    for f in findings:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind", "")).strip()
        if kind not in VALID_KINDS:
            continue
        severity = str(f.get("severity", "info")).strip().lower()
        if severity not in VALID_SEVERITY:
            severity = "info"
        title = str(f.get("title", "")).strip()
        if not title:
            continue
        payload = f.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        target = str(f.get("target_entry_id", "") or "").strip()
        # `target_entry_id` may have been put inside payload by the model
        if not target and isinstance(payload.get("target_entry_id"), str):
            target = payload["target_entry_id"]

        insert_finding(
            finding_id      = f"find_{uuid.uuid4().hex[:10]}",
            report_id       = report_id,
            category        = category,
            kind            = kind,
            title           = title[:240],
            description     = str(f.get("description", "") or "")[:2000],
            severity        = severity,
            target_entry_id = target,
            payload         = payload,
        )


def _section_to_failure(report_id: str, section: str, exc_text: str) -> None:
    update_report_section(
        report_id, section,
        status="failed",
        error=exc_text[:1500],
        summary="",
    )


def _section_to_completed(report_id: str, section: str, summary: str) -> None:
    update_report_section(
        report_id, section,
        status="completed",
        summary=(summary or "")[:1500],
        error="",
    )


# ─── The three runners ───────────────────────────────────────────────────────


def _run_logistics(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are a meticulous trip-planning analyst. Review this trip and find "
            "LOGISTICAL problems only.\n"
            "Look for:\n"
            " - Time mismatches between consecutive same-day entries (commute too "
            "   short / overlap / impossibly tight transitions). Use destination "
            "   names and country to estimate realistic travel time.\n"
            " - Missing meals: breakfast is OPTIONAL, but every day MUST have an "
            "   identifiable LUNCH and DINNER (look for restaurant icons / words).\n"
            " - Day overpacking, illogical ordering, or dates outside the trip range.\n"
            "DO NOT comment on gas, distance to fuel stations, packing list, or tasks "
            "— other agents handle those.\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL_PRO, prompt, _FINDINGS_SCHEMA, use_search=False)
        if "_error" in parsed:
            _section_to_failure(report_id, "logistics", parsed["_error"])
            return
        _persist_findings(report_id, "logistics", parsed)
        _section_to_completed(report_id, "logistics", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "logistics", traceback.format_exc())


def _run_gas(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are a road-trip fuel-planning expert. Use Google Search to look up "
            "CURRENT gas-price information by US state / country relevant to this trip. "
            "Analyse the itinerary day-by-day for the route.\n"
            "Produce findings about:\n"
            " - Estimated kilometres between consecutive same-day destinations.\n"
            " - Recommended fuel-up points (which day & before which leg).\n"
            " - In each state/region the route crosses, name the cheapest typical chain "
            "   or known cheap-fuel cities, citing rough current prices when available.\n"
            "Prefer findings of kind 'append_text' or 'append_bullets' targeting the "
            "specific itinerary entry where the driver should refuel. Use 'create_task' "
            "for upfront prep (e.g. 'sign up for Costco gas card before trip').\n"
            "\n"
            "IMPORTANT: respond with a SINGLE JSON object, no prose, no markdown, with "
            "this exact shape:\n"
            "{\n"
            '  "summary": "string",\n'
            '  "findings": [\n'
            '    {"kind":"append_bullets|append_text|create_task|note",\n'
            '     "title":"...","description":"...","severity":"info|warning|error",\n'
            '     "target_entry_id":"entry_xxx or empty",\n'
            '     "payload":{"content":"...", "description":"...", "priority":"Normal|Medium|High"}}\n'
            "  ]\n"
            "}\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL_PRO, prompt, _FINDINGS_SCHEMA, use_search=True)
        if "_error" in parsed:
            _section_to_failure(report_id, "gas", parsed["_error"])
            return
        _persist_findings(report_id, "gas", parsed)
        _section_to_completed(report_id, "gas", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "gas", traceback.format_exc())


def _run_tasks(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are a trip-prep assistant. Surface MISSING tasks and MISSING equipment "
            "for this trip. Examples:\n"
            " - 'Book hotel for Day 3' if a destination's accommodation field is empty.\n"
            " - 'Reserve restaurant' for famous spots that need bookings.\n"
            " - Visa / parking / SIM card / car-rental confirmation when relevant.\n"
            " - Equipment based on activities: hiking shoes for hiking entries, "
            "   sunscreen + swimwear for beach, adapter by destination country, etc.\n"
            "Strictly DO NOT duplicate items already in the existing 'tasks' or "
            "'equipment' lists in the context. Be concise and concrete.\n"
            "When suggesting equipment, you may set payload.owner to one of "
            "approved_emails if the item clearly belongs to a specific person; "
            "otherwise leave owner empty and the user will choose.\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL, prompt, _FINDINGS_SCHEMA, use_search=False)
        if "_error" in parsed:
            _section_to_failure(report_id, "tasks", parsed["_error"])
            return
        _persist_findings(report_id, "tasks", parsed)
        _section_to_completed(report_id, "tasks", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "tasks", traceback.format_exc())


# ─── Public API ──────────────────────────────────────────────────────────────


def start_analysis(trip_id: str, user_email: str) -> str | None:
    """Kick off a new report. Returns the report_id, or None if locked."""
    if get_running_report(trip_id) is not None:
        return None

    ctx = _build_trip_context(trip_id)
    if not ctx:
        return None

    report_id = f"rep_{uuid.uuid4().hex[:10]}"
    if not insert_analysis_report(report_id, trip_id, user_email):
        return None

    for target in (_run_logistics, _run_gas, _run_tasks):
        threading.Thread(
            target=target,
            args=(report_id, ctx),
            daemon=True,
        ).start()
    return report_id


def is_report_running(report: dict | None) -> bool:
    if not report:
        return False
    return any(
        str(report.get(f"{s}_status", "")) == "running"
        for s in ("logistics", "gas", "tasks")
    )


# ─── Apply / dismiss ─────────────────────────────────────────────────────────


def _build_appended_info(existing: str, content: str, header: str) -> str:
    sep = f"\n\n---\n**{header}**\n"
    if existing.strip():
        return f"{existing}{sep}{content}"
    return f"**{header}**\n{content}"


def apply_finding(finding_id: str, user_email: str, owner_override: str = "") -> tuple[bool, str]:
    """Execute a finding's action. Returns (ok, message)."""
    f = get_finding(finding_id)
    if not f:
        return False, "Finding not found"
    if str(f.get("status", "")) != "pending":
        return False, "Already actioned"

    kind     = str(f.get("kind", ""))
    payload  = f.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    target   = str(f.get("target_entry_id", "") or "").strip()
    category = str(f.get("category", ""))

    try:
        if kind in ("append_text", "append_bullets"):
            if not target:
                return False, "Finding has no target destination"
            entry = get_itinerary_entry(target)
            if not entry:
                return False, "Target destination no longer exists"
            content = str(payload.get("content", "")).strip()
            if not content:
                return False, "Empty content"
            header = {
                "logistics": "🔍 Logistics check",
                "gas":       "⛽ Fuel note",
                "tasks":     "🤖 AI suggestion",
                "equipment": "🤖 AI suggestion",
            }.get(category, "🤖 AI suggestion")
            new_info = _build_appended_info(
                str(entry.get("additional_info", "") or ""),
                content,
                header,
            )
            trip_row = pd.Series({
                "trip_id":   str(entry.get("trip_id", "")),
                "sheet_tab": "",
            })
            if not update_itinerary_entry(trip_row, target, additional_info=new_info):
                return False, "Failed to update destination"

        elif kind == "create_task":
            desc = str(payload.get("description", "")).strip() or str(f.get("title", "")).strip()
            if not desc:
                return False, "Task description missing"
            priority = str(payload.get("priority", "Normal")).strip().capitalize()
            if priority not in TASK_PRIORITIES:
                priority = "Normal"
            # find trip_id via report
            report_id = str(f.get("report_id", ""))
            rep = _sb().table("analysis_reports").select("trip_id").eq("report_id", report_id).limit(1).execute()
            trip_id = rep.data[0]["trip_id"] if rep.data else ""
            if not trip_id:
                return False, "Trip not found"
            ok = add_task(
                trip_id     = trip_id,
                description = desc,
                assigned_to = str(payload.get("assigned_to", "") or "Unassigned"),
                priority    = priority,
                links       = str(payload.get("links", "") or ""),
                entry_id    = target or "",
                notes       = str(payload.get("notes", "") or ""),
                due_date    = str(payload.get("due_date", "") or ""),
            )
            if not ok:
                return False, "Failed to add task"

        elif kind == "create_equipment":
            desc = str(payload.get("description", "")).strip() or str(f.get("title", "")).strip()
            if not desc:
                return False, "Equipment description missing"
            report_id = str(f.get("report_id", ""))
            rep = _sb().table("analysis_reports").select("trip_id").eq("report_id", report_id).limit(1).execute()
            trip_id = rep.data[0]["trip_id"] if rep.data else ""
            if not trip_id:
                return False, "Trip not found"
            owner = (
                owner_override.strip()
                or str(payload.get("owner", "") or "").strip()
                or user_email
            )
            if not add_equipment_item(trip_id, owner, desc):
                return False, "Failed to add equipment item"

        elif kind == "note":
            pass  # nothing to apply, just mark accepted

        else:
            return False, f"Unknown kind: {kind}"

        update_finding_status(finding_id, "accepted", user_email)
        return True, "Applied"
    except Exception as exc:
        return False, f"Error: {exc}"


def dismiss_finding(finding_id: str, user_email: str) -> bool:
    return update_finding_status(finding_id, "dismissed", user_email)
