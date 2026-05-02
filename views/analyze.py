"""Analyze page — async AI report (logistics / gas / tasks) with accept-able findings."""

from __future__ import annotations

import streamlit as st
import pandas as pd

from utils.analyzer import (
    apply_finding,
    dismiss_finding,
    is_report_running,
    start_analysis,
)
from utils.sheets import (
    get_latest_report,
    get_report,
    list_findings,
    list_reports,
    get_running_report,
)
from utils.config import cfg
from views._shared import trip_picker, cached_itinerary

# ─── Section metadata ─────────────────────────────────────────────────────────

SECTIONS: list[tuple[str, str, str, str]] = [
    # (key, label, material icon, category-in-findings)
    ("logistics", "Logistics",          "schedule",        "logistics"),
    ("gas",       "Gas & Distance",     "local_gas_station", "gas"),
    ("tasks",     "Tasks & Equipment",  "task_alt",        "tasks"),
]

_STATUS_ICON = {
    "pending":   ":material/hourglass_empty:",
    "running":   ":material/progress_activity:",
    "completed": ":material/check_circle:",
    "failed":    ":material/error:",
}

_SEVERITY_BADGE = {
    "info":    ":blue[:material/info:]",
    "warning": ":orange[:material/warning:]",
    "error":   ":red[:material/error:]",
}

_KIND_LABEL = {
    "append_text":      "Append note to destination",
    "append_bullets":   "Append bullets to destination",
    "create_task":      "Create task",
    "create_equipment": "Add equipment item",
    "note":             "Informational",
}

_KIND_ICON = {
    "append_text":      "edit_note",
    "append_bullets":   "format_list_bulleted",
    "create_task":      "checklist",
    "create_equipment": "luggage",
    "note":             "info",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _entry_lookup(trip_id: str) -> dict[str, str]:
    """Return {entry_id: destination} for the trip's itinerary."""
    df = cached_itinerary(trip_id, "")
    if df.empty or "entry_id" not in df.columns:
        return {}
    return {
        str(r["entry_id"]): str(r.get("destination", ""))
        for _, r in df.iterrows()
        if not str(r["entry_id"]).startswith("daytitle_")
    }


def _render_finding(
    finding: pd.Series,
    entries_map: dict[str, str],
    user_email: str,
) -> None:
    fid       = str(finding["finding_id"])
    kind      = str(finding.get("kind", ""))
    title     = str(finding.get("title", ""))
    desc      = str(finding.get("description", ""))
    severity  = str(finding.get("severity", "info"))
    target    = str(finding.get("target_entry_id", "") or "")
    status    = str(finding.get("status", "pending"))
    payload   = finding.get("payload") or {}
    if isinstance(payload, str):
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = {}

    badge = _SEVERITY_BADGE.get(severity, _SEVERITY_BADGE["info"])

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"{badge} **{title}**")

        st.caption(f":material/{_KIND_ICON.get(kind, 'info')}: {_KIND_LABEL.get(kind, kind)}")

        if desc:
            st.write(desc)

        # Destination link
        if target and target in entries_map:
            st.caption(f":material/place: → {entries_map[target]}")
        elif target:
            st.caption(f":material/place: → (entry no longer in itinerary)")

        # Show payload preview
        content_preview = str(payload.get("content", "") or "").strip()
        if content_preview:
            with st.expander("Preview", icon=":material/visibility:"):
                st.markdown(content_preview)

        if kind == "create_task":
            bits = []
            pri = str(payload.get("priority", "")).strip()
            if pri:
                bits.append(f":material/flag: {pri}")
            due = str(payload.get("due_date", "") or "").strip()
            if due:
                bits.append(f":material/event: {due}")
            assigned = str(payload.get("assigned_to", "") or "").strip()
            if assigned:
                bits.append(f":material/person: {assigned}")
            if bits:
                st.caption("  ·  ".join(bits))

        # Owner selector for create_equipment
        owner_override = ""
        if kind == "create_equipment" and status == "pending":
            owners = [e for e in cfg.approved_emails if e]
            if owners:
                default_owner = (
                    str(payload.get("owner", "") or "").strip()
                    or (user_email if user_email in owners else owners[0])
                )
                idx = owners.index(default_owner) if default_owner in owners else 0
                owner_override = st.selectbox(
                    "Pack for",
                    options=owners,
                    index=idx,
                    key=f"owner_{fid}",
                )

        # Action buttons
        if status == "pending":
            with st.container(horizontal=True, gap="small"):
                if st.button(
                    "Accept",
                    icon=":material/check:",
                    type="primary",
                    key=f"acc_{fid}",
                ):
                    ok, msg = apply_finding(fid, user_email, owner_override=owner_override)
                    if ok:
                        st.toast("Applied", icon=":material/check:")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(msg)
                if st.button(
                    "Dismiss",
                    icon=":material/close:",
                    type="tertiary",
                    key=f"dis_{fid}",
                ):
                    if dismiss_finding(fid, user_email):
                        st.toast("Dismissed", icon=":material/close:")
                        st.rerun()
        else:
            label_icon = ":material/check_circle:" if status == "accepted" else ":material/cancel:"
            color_fn = ":green" if status == "accepted" else ":gray"
            actor = str(finding.get("acted_by", "") or "")
            tail = f" · {actor}" if actor else ""
            st.caption(f"{color_fn}[{label_icon} {status.capitalize()}{tail}]")


def _render_section(
    report: dict,
    section_key: str,
    label: str,
    icon: str,
    category: str,
    entries_map: dict[str, str],
    user_email: str,
) -> None:
    status  = str(report.get(f"{section_key}_status", "pending"))
    summary = str(report.get(f"{section_key}_summary", "") or "")
    error   = str(report.get(f"{section_key}_error", "") or "")
    status_icon = _STATUS_ICON.get(status, _STATUS_ICON["pending"])

    findings_df = list_findings(report["report_id"], category=category)
    pending_df  = (
        findings_df[findings_df["status"] == "pending"] if not findings_df.empty else pd.DataFrame()
    )
    actioned_df = (
        findings_df[findings_df["status"] != "pending"] if not findings_df.empty else pd.DataFrame()
    )

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
            st.markdown(f"##### {status_icon} :material/{icon}: {label}")
            count_lbl = ""
            if status == "completed":
                count_lbl = f"{len(pending_df)} open · {len(actioned_df)} actioned"
            elif status == "running":
                count_lbl = "analysing…"
            elif status == "failed":
                count_lbl = "failed"
            if count_lbl:
                st.caption(count_lbl)

        if status == "running":
            st.info("Working on it… results will appear here when ready.",
                    icon=":material/progress_activity:")
            return

        if status == "failed":
            st.error(f"This section failed.\n\n```\n{error[:600]}\n```")
            return

        if summary:
            st.caption(summary)

        if pending_df.empty and actioned_df.empty:
            st.caption(":gray[No suggestions for this section.]")
            return

        for _, finding in pending_df.iterrows():
            _render_finding(finding, entries_map, user_email)

        if not actioned_df.empty:
            with st.expander(
                f"Actioned ({len(actioned_df)})",
                icon=":material/history:",
            ):
                for _, finding in actioned_df.iterrows():
                    _render_finding(finding, entries_map, user_email)


@st.fragment(run_every=3)
def _live_report_card(report_id: str, trip_id: str, user_email: str) -> None:
    """Auto-refreshing card that re-fetches the report every 3s while running."""
    rep = get_report(report_id)
    if not rep:
        st.warning("Report not found.", icon=":material/warning:")
        return

    entries_map = _entry_lookup(trip_id)

    created_at = str(rep.get("created_at", ""))
    with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
        st.caption(f":material/event: {created_at}  ·  :material/person: {rep.get('created_by','')}")
        if is_report_running(rep):
            st.caption(":blue[:material/sync: live]")

    for key, label, icon, category in SECTIONS:
        _render_section(rep, key, label, icon, category, entries_map, user_email)

    # When the report transitions from running → terminal, fire ONE app-level
    # rerun so the parent page re-enables the "Run new analysis" button.
    finish_key = f"_finish_seen_{report_id}"
    if not is_report_running(rep) and not st.session_state.get(finish_key, False):
        st.session_state[finish_key] = True
        st.rerun(scope="app")


def _history_view(trip_id: str, user_email: str, exclude_report_id: str = "") -> None:
    df = list_reports(trip_id)
    if df.empty:
        st.caption(":gray[No prior reports.]")
        return
    if exclude_report_id:
        df = df[df["report_id"] != exclude_report_id]
    if df.empty:
        st.caption(":gray[No prior reports.]")
        return

    options = {
        str(r["report_id"]): f"{str(r.get('created_at',''))[:19]}  ·  {str(r.get('created_by',''))}"
        for _, r in df.iterrows()
    }
    chosen = st.selectbox(
        "Pick a past report",
        options=list(options.keys()),
        format_func=lambda k: options[k],
        key=f"history_picker_{trip_id}",
    )
    if not chosen:
        return
    rep = get_report(chosen)
    if not rep:
        return
    entries_map = _entry_lookup(trip_id)
    for key, label, icon, category in SECTIONS:
        _render_section(rep, key, label, icon, category, entries_map, user_email)


# ─── Page entry point ────────────────────────────────────────────────────────


def render() -> None:
    user_email = st.user.get("email", "") if hasattr(st, "user") else ""

    trip_row = trip_picker()
    if trip_row is None:
        st.header(":material/troubleshoot: Analyze", anchor=False)
        return

    trip_id = str(trip_row["trip_id"])
    running = get_running_report(trip_id)

    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.header(":material/troubleshoot: Analyze", anchor=False)
        run_clicked = st.button(
            "Run new analysis",
            icon=":material/auto_awesome:",
            type="primary",
            disabled=running is not None,
            help=("Already running — wait for the current report to finish."
                  if running is not None
                  else "Send the trip to the AI agent for a 3-part review."),
            key="analyze_run_btn",
        )

    st.caption(
        f"Reviewing **{trip_row['trip_name']}** ({trip_row['country']} · "
        f"{trip_row['start_date']} → {trip_row['end_date']})"
    )

    if run_clicked:
        new_id = start_analysis(trip_id, user_email)
        if new_id is None:
            st.warning("A report is already in progress for this trip.",
                       icon=":material/info:")
        else:
            st.toast("Analysis started — running in the background.",
                     icon=":material/auto_awesome:")
            st.rerun()

    latest = get_latest_report(trip_id)
    if latest is None:
        st.info(
            "No analyses yet. Click **Run new analysis** to send the trip to the AI agent.",
            icon=":material/info:",
        )
        return

    st.subheader("Latest report", anchor=False, divider="gray")
    _live_report_card(latest["report_id"], trip_id, user_email)

    with st.expander("Past reports", icon=":material/history:"):
        _history_view(trip_id, user_email, exclude_report_id=str(latest["report_id"]))
