"""
Logistical AI Helper — async 5-stage report agent.

Stages (each runs in its own background thread):
  1. logistics  — Gemini Pro (JSON)         : time mismatches, missing meals
  2. pace       — Gemini Pro (JSON)         : trip intensity / day-by-day pace tips
  3. gas        — Gemini Pro + Google Search: km commute + fuel suggestions
  4. financial  — Gemini Pro (JSON)         : missing prices/expenses, budget gaps
  5. tasks      — Gemini Flash (JSON)       : missing prep tasks & equipment

Lifecycle (status field per section in `analysis_reports`):
    pending → running → completed  (or → failed with *_error populated)

──────────────────────────────────────────────────────────────────────────────
Required Supabase schema  (run once in the SQL editor)
──────────────────────────────────────────────────────────────────────────────

create table if not exists analysis_reports (
  report_id         text primary key,
  trip_id           text not null references trips(trip_id) on delete cascade,
  created_at        timestamptz not null default now(),
  created_by        text,
  logistics_status  text not null default 'pending',
  logistics_summary text default '',
  logistics_error   text default '',
  gas_status        text not null default 'pending',
  gas_summary       text default '',
  gas_error         text default '',
  tasks_status      text not null default 'pending',
  tasks_summary     text default '',
  tasks_error       text default '',
  pace_status       text not null default 'pending',
  pace_summary      text default '',
  pace_error        text default '',
  financial_status  text not null default 'pending',
  financial_summary text default '',
  financial_error   text default ''
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

──────────────────────────────────────────────────────────────────────────────
Migration — adding the pace + financial sections to an existing install
──────────────────────────────────────────────────────────────────────────────

alter table analysis_reports
  add column if not exists pace_status       text not null default 'pending',
  add column if not exists pace_summary      text default '',
  add column if not exists pace_error        text default '',
  add column if not exists financial_status  text not null default 'pending',
  add column if not exists financial_summary text default '',
  add column if not exists financial_error   text default '',
  add column if not exists stamps_status     text not null default 'pending',
  add column if not exists stamps_summary    text default '',
  add column if not exists stamps_error      text default '';

──────────────────────────────────────────────────────────────────────────────
Migration — adding the title column to expenses
──────────────────────────────────────────────────────────────────────────────

alter table expenses
  add column if not exists title text default '';
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
    EXPENSE_CATEGORIES,
    _sb,
    add_equipment_item,
    add_expense,
    add_task,
    get_equipment,
    get_expenses,
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

VALID_KINDS = {
    "append_text",
    "append_bullets",
    "create_task",
    "create_equipment",
    "update_entry_price",
    "create_expense",
    "note",
}
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
                            "title":       {"type": "string"},
                            "description": {"type": "string"},
                            "priority":    {"type": "string"},
                            "notes":       {"type": "string"},
                            "due_date":    {"type": "string"},
                            "links":       {"type": "string"},
                            "owner":       {"type": "string"},
                            "price":       {"type": "string"},
                            "currency":    {"type": "string"},
                            "amount":      {"type": "number"},
                            "category":    {"type": "string"},
                            "date":        {"type": "string"},
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
    "append_text"        — short text/sentence to append to a destination's "Additional info"
    "append_bullets"     — markdown bullet list to append to a destination's "Additional info"
    "create_task"        — a new to-do item for the trip
    "create_equipment"   — a new packing-list item for someone
    "update_entry_price" — change the price/currency on an existing destination
    "create_expense"     — record a new expense in the Expenses page
    "note"               — informational only, nothing to apply
- For append_text / append_bullets, MUST set `target_entry_id` to one of the
  entry_ids from the itinerary listed below. The text/bullets goes in payload.content.
- For create_task, payload MUST include: description, priority (Normal|Medium|High).
  Optional: notes, due_date (YYYY-MM-DD), links, target_entry_id.
- For create_equipment, payload MUST include: description. Optional: owner (email).
- For update_entry_price, MUST set target_entry_id and payload.price (string)
  and payload.currency (e.g. USD, EUR, ILS).
- For create_expense, payload MUST include: amount (number), currency (string),
  category (one of: Food & Dining, Transport, Accommodation, Activities & Tours,
  Shopping, Health, Communication, Misc), title (short headline e.g. "Rental car
  insurance"). Optional: description (longer detail), date (YYYY-MM-DD, defaults
  to trip start_date).
- Severity: "info" (helpful), "warning" (likely problem), "error" (definite gap).
- Keep titles short (≤ 80 chars). Be specific, not generic.
- Do NOT duplicate existing tasks, equipment items, or expenses already listed in context.
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

    expenses: list[dict] = []
    exp_df = get_expenses(trip_id)
    if not exp_df.empty:
        for _, e in exp_df.iterrows():
            expenses.append({
                "date":        str(e.get("date", "") or ""),
                "category":    str(e.get("category", "") or ""),
                "description": str(e.get("description", "") or ""),
                "amount":      str(e.get("amount", "") or ""),
                "currency":    str(e.get("currency", "") or ""),
            })

    return {
        "trip": {
            "trip_id":    str(trip["trip_id"]),
            "trip_name":  str(trip["trip_name"]),
            "country":    str(trip["country"]),
            "start_date": str(trip["start_date"]),
            "end_date":   str(trip["end_date"]),
            "notes":      str(trip.get("notes", "") or ""),
        },
        "day_titles":      day_titles,
        "entries":         entries,
        "tasks":           tasks,
        "equipment":       equipment,
        "expenses":        expenses,
        "expense_categories": list(EXPENSE_CATEGORIES),
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


# ─── The five runners ────────────────────────────────────────────────────────


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
            "DO NOT comment on gas, distance to fuel stations, packing list, tasks, "
            "money, prices, or pace — other agents handle those.\n"
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


def _run_pace(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are a trip-pace adviser. Your PRIMARY job is to surface SMALL, "
            "ACTIONABLE adjustments that make the trip flow better while preserving "
            "the planned destinations and overall logic.\n"
            "\n"
            "Ground rules — read carefully:\n"
            " - The traveller WANTS an intense trip. 'Easy' is NOT the goal. NEVER "
            "   suggest removing destinations, shortening must-see stops, or 'taking "
            "   it easy' just because a day is full.\n"
            " - SMALL fix examples (these MUST use 'append_text' or 'append_bullets' "
            "   targeting the specific entry where the tip applies):\n"
            "     · 'Leave 20 min earlier to beat the queue at gate'\n"
            "     · 'Pre-book timed-entry tickets — saves ~30 min on arrival'\n"
            "     · 'Swap sit-down lunch here for grab-and-go to keep schedule'\n"
            "     · 'Add a 15-min coffee break between X and Y'\n"
            "     · 'Hit this stop before noon — it gets crowded after lunch'\n"
            "     · 'Hydrate / pack a snack — no food spots between leg A and B'\n"
            "   These are nudges of minutes-to-an-hour, not structural rewrites.\n"
            " - BIG advice examples (these MUST be 'note', NOT actionable — the "
            "   user reads them but cannot one-click apply):\n"
            "     · 'Consider swapping Day 3 and Day 5 entirely'\n"
            "     · 'This trip would breathe better if Day 7 became 2 days'\n"
            "     · 'Day 4 is impossible as planned — would need a 4am start AND "
            "        skipping at least one stop'\n"
            "     · Any suggestion that requires moving entries between days, "
            "        adding/removing destinations, or major time blocks.\n"
            "   Set severity='warning' or 'error' for impossible days, 'info' for "
            "   strategic suggestions.\n"
            " - Aim for MOSTLY small actionable findings. Big-picture notes should "
            "   be the exception, used only when a small tip cannot solve the issue.\n"
            "DO NOT touch prices, gas, equipment, or to-do tasks.\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL_PRO, prompt, _FINDINGS_SCHEMA, use_search=False)
        if "_error" in parsed:
            _section_to_failure(report_id, "pace", parsed["_error"])
            return
        _persist_findings(report_id, "pace", parsed)
        _section_to_completed(report_id, "pace", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "pace", traceback.format_exc())


def _run_financial(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are the trip's financial sanity-checker. Scan the itinerary and "
            "the recorded expenses, then surface BUDGET GAPS the traveller likely "
            "forgot.\n"
            "Look for:\n"
            " - Itinerary entries with NO price set that almost certainly cost money "
            "   (paid attractions, guided tours, paid parking, ferries, theme parks). "
            "   Suggest a realistic price using your knowledge of typical entry fees "
            "   for that destination → emit 'update_entry_price' with payload.price "
            "   and payload.currency.\n"
            " - Itinerary entries whose existing price looks WAY off vs. the typical "
            "   real-world cost → 'update_entry_price' with corrected value.\n"
            " - Categories of spend missing from the Expenses page that any trip of "
            "   this shape needs (e.g. car rental, fuel budget, travel insurance, "
            "   eSIM, tolls, airport transfers, tips). Emit 'create_expense' with "
            "   amount, currency, category (must be one of the listed expense_categories), "
            "   description, and date (default to trip start_date).\n"
            "DO NOT duplicate expenses already in the 'expenses' list. DO NOT touch "
            "tasks, equipment, gas-station selection, pace, or logistics.\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL_PRO, prompt, _FINDINGS_SCHEMA, use_search=False)
        if "_error" in parsed:
            _section_to_failure(report_id, "financial", parsed["_error"])
            return
        _persist_findings(report_id, "financial", parsed)
        _section_to_completed(report_id, "financial", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "financial", traceback.format_exc())


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


def _run_stamps(report_id: str, ctx: dict) -> None:
    try:
        prompt = (
            "You are an expert in collectible stamps, passport-book cancellations, "
            "and postal stickers — specifically US National Park passport stamps, "
            "National Forest / BLM / State-Park cancellations, unique post-office "
            "pictorial cancellations, and any other regional stamp/sticker programs "
            "relevant to the countries and regions in this itinerary.\n"
            "\n"
            "Your job:\n"
            "1. For each existing destination in the itinerary that is AT or near a "
            "   stamp/sticker location, add a short practical note (kind='append_bullets') "
            "   to that entry's 'Additional info'. Include: what stamp/program is "
            "   available, exactly where to get it (visitor center name, specific window, "
            "   post office address), hours if known, whether it is free or paid.\n"
            "2. If there is a NEARBY location NOT already in the itinerary where a "
            "   unique, rare, or highly sought-after stamp/cancellation is available — "
            "   AND it is a short detour of ≤ 30 minutes from an existing stop — "
            "   suggest it as an 'append_text' note on the closest existing entry "
            "   (not a full new destination). Keep it concise: 'X km off-route: "
            "   [Name] post office has a pictorial cancellation for [Program]'. "
            "   Do NOT suggest detours > 30 min or significant route changes.\n"
            "3. If the traveller should bring something specific before the trip "
            "   (e.g. a National Parks Passport book, extra blank-page inserts, a "
            "   self-inking stamp pad, an envelope for a cancellation by mail), "
            "   emit a 'create_task' finding with priority=High.\n"
            "4. Use 'note' ONLY for general regional tips with no clear single target "
            "   (e.g. 'This route passes through three passport stamp regions — "
            "   consider carrying the book in the car at all times').\n"
            "\n"
            "DO NOT suggest removing or rearranging destinations. DO NOT touch "
            "prices, pace, gas, or other logistics — other agents handle those.\n"
            f"{_SHARED_PROMPT_RULES}\n"
            f"Trip data (JSON):\n{_ctx_to_text(ctx)}"
        )
        parsed = generate_structured(MODEL_PRO, prompt, _FINDINGS_SCHEMA, use_search=False)
        if "_error" in parsed:
            _section_to_failure(report_id, "stamps", parsed["_error"])
            return
        _persist_findings(report_id, "stamps", parsed)
        _section_to_completed(report_id, "stamps", str(parsed.get("summary", "")))
    except Exception:
        _section_to_failure(report_id, "stamps", traceback.format_exc())


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

    for target in (_run_logistics, _run_pace, _run_gas, _run_financial, _run_stamps, _run_tasks):
        threading.Thread(
            target=target,
            args=(report_id, ctx),
            daemon=True,
        ).start()
    return report_id


def is_report_running(report: dict | None) -> bool:
    if not report:
        return False
    for s in ("logistics", "pace", "gas", "financial", "stamps", "tasks"):
        col = f"{s}_status"
        if col in report and str(report.get(col, "")) == "running":
            return True
    return False


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
                "pace":      "🏃 Pace tip",
                "gas":       "⛽ Fuel note",
                "financial": "💰 Budget note",
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

        elif kind == "update_entry_price":
            if not target:
                return False, "Finding has no target destination"
            entry = get_itinerary_entry(target)
            if not entry:
                return False, "Target destination no longer exists"
            new_price    = str(payload.get("price", "")).strip()
            new_currency = str(payload.get("currency", "")).strip() or str(entry.get("currency", "USD"))
            if not new_price:
                return False, "Empty price"
            trip_row = pd.Series({
                "trip_id":   str(entry.get("trip_id", "")),
                "sheet_tab": "",
            })
            if not update_itinerary_entry(
                trip_row, target, price=new_price, currency=new_currency,
            ):
                return False, "Failed to update destination price"

        elif kind == "create_expense":
            report_id = str(f.get("report_id", ""))
            rep = _sb().table("analysis_reports").select("trip_id").eq("report_id", report_id).limit(1).execute()
            trip_id = rep.data[0]["trip_id"] if rep.data else ""
            if not trip_id:
                return False, "Trip not found"
            try:
                amount = float(payload.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if amount <= 0:
                return False, "Amount must be positive"
            category = str(payload.get("category", "")).strip()
            if category not in EXPENSE_CATEGORIES:
                category = "Misc"
            title = str(payload.get("title", "") or "").strip() or str(f.get("title", "") or "").strip()
            if not title:
                return False, "Expense title missing"
            description = str(payload.get("description", "") or "").strip()
            currency = str(payload.get("currency", "")).strip() or "USD"
            # Default date to trip start if model didn't supply one
            date_val = str(payload.get("date", "")).strip()
            if not date_val:
                trip_resp = _sb().table("trips").select("start_date").eq("trip_id", trip_id).limit(1).execute()
                date_val = str(trip_resp.data[0]["start_date"]) if trip_resp.data else ""
            if not add_expense(
                trip_id     = trip_id,
                entry_date  = date_val,
                category    = category,
                title       = title,
                amount      = amount,
                currency    = currency,
                description = description,
                links       = str(payload.get("links", "") or ""),
            ):
                return False, "Failed to add expense"

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
