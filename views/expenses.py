"""Expenses page — trip cost overview, manual expense tracking, NIS conversion."""

from datetime import date
import requests
import pandas as pd
import streamlit as st
import altair as alt

from utils.sheets import (
    get_expenses, add_expense, delete_expense,
    update_itinerary_entry,
    EXPENSE_CATEGORIES, EXPENSE_COLS,
)
from views._shared import (
    cached_itinerary, trip_picker, split_itinerary, parse_link,
)

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "ILS", "AUD", "CAD", "Other"]


# ─── Live exchange rates → ILS ────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _get_ils_rates() -> tuple[dict[str, float], str]:
    """Return ({currency: ILS_per_unit}, rate_date_str).

    Uses the free Frankfurter.app API (no key required).
    Base is EUR; derives all other pairs by cross-rate via EUR→ILS.
    ILS (Israeli Shekel) is supported natively by Frankfurter.
    """
    try:
        resp = requests.get("https://api.frankfurter.app/latest", timeout=6)
        resp.raise_for_status()
        data = resp.json()
        rates_from_eur: dict[str, float] = data.get("rates", {})
        ils_per_eur = float(rates_from_eur.get("ILS", 0))
        if ils_per_eur <= 0:
            return {}, ""
        result: dict[str, float] = {"EUR": ils_per_eur, "ILS": 1.0}
        for cur, eur_rate in rates_from_eur.items():
            if cur != "ILS" and float(eur_rate) > 0:
                result[cur] = ils_per_eur / float(eur_rate)
        return result, str(data.get("date", ""))
    except Exception:
        return {}, ""


def _to_ils(totals_by_cur: dict[str, float], ils_rates: dict[str, float]) -> tuple[float, list[str]]:
    """Sum all amounts into ILS. Returns (total_ils, [currencies_that_couldnt_convert])."""
    total = 0.0
    unknown: list[str] = []
    for cur, amt in totals_by_cur.items():
        rate = ils_rates.get(cur)
        if rate is not None:
            total += amt * rate
        else:
            unknown.append(cur)
    return total, unknown


@st.cache_data(ttl=30, show_spinner=False)
def _cached_expenses(trip_id: str) -> pd.DataFrame:
    return get_expenses(trip_id)


def _to_float(val) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _totals(entries_df: pd.DataFrame, exp_df: pd.DataFrame) -> dict[str, float]:
    """Sum all costs grouped by currency."""
    totals: dict[str, float] = {}

    # From itinerary price fields
    if not entries_df.empty and "price" in entries_df.columns:
        for _, row in entries_df.iterrows():
            amt = _to_float(row.get("price", 0))
            if amt <= 0:
                continue
            cur = str(row.get("currency", "USD")).strip() or "USD"
            totals[cur] = totals.get(cur, 0.0) + amt

    # From manual expenses
    if not exp_df.empty and "amount" in exp_df.columns:
        for _, row in exp_df.iterrows():
            amt = _to_float(row.get("amount", 0))
            if amt <= 0:
                continue
            cur = str(row.get("currency", "USD")).strip() or "USD"
            totals[cur] = totals.get(cur, 0.0) + amt

    return totals


def _itin_totals(entries_df: pd.DataFrame) -> dict[str, float]:
    """Itinerary-only totals by currency."""
    totals: dict[str, float] = {}
    if entries_df.empty or "price" not in entries_df.columns:
        return totals
    for _, row in entries_df.iterrows():
        amt = _to_float(row.get("price", 0))
        if amt <= 0:
            continue
        cur = str(row.get("currency", "USD")).strip() or "USD"
        totals[cur] = totals.get(cur, 0.0) + amt
    return totals


def _exp_totals(exp_df: pd.DataFrame) -> dict[str, float]:
    """Manual expenses totals by currency."""
    totals: dict[str, float] = {}
    if exp_df.empty or "amount" not in exp_df.columns:
        return totals
    for _, row in exp_df.iterrows():
        amt = _to_float(row.get("amount", 0))
        if amt <= 0:
            continue
        cur = str(row.get("currency", "USD")).strip() or "USD"
        totals[cur] = totals.get(cur, 0.0) + amt
    return totals


def _fmt_totals(totals: dict[str, float]) -> str:
    if not totals:
        return "—"
    return "  ·  ".join(f"**{v:,.2f}** {k}" for k, v in sorted(totals.items()))


# ─── Itinerary category save callback ────────────────────────────────────────
def _save_itin_category(entry_id: str, trip_id_str: str, key: str) -> None:
    new_cat = st.session_state.get(key, "")
    trip_row_ser = pd.Series({"trip_id": trip_id_str, "sheet_tab": ""})
    update_itinerary_entry(trip_row_ser, entry_id, category=new_cat)
    st.cache_data.clear()


# ─── Category stacked bar chart ───────────────────────────────────────────────
def _category_chart(
    entries_df: pd.DataFrame,
    exp_df: pd.DataFrame,
    ils_rates: dict[str, float],
) -> None:
    """Render a 100 % stacked horizontal bar showing spend by category in ₪."""
    cat_ils: dict[str, float] = {}

    if not entries_df.empty and "price" in entries_df.columns:
        for _, row in entries_df.iterrows():
            amt = _to_float(row.get("price", 0))
            if amt <= 0:
                continue
            cur  = str(row.get("currency", "USD") or "USD").strip() or "USD"
            rate = ils_rates.get(cur, 0.0)
            cat  = str(row.get("category") or "").strip() or "Uncategorized"
            cat_ils[cat] = cat_ils.get(cat, 0.0) + amt * rate

    if not exp_df.empty and "amount" in exp_df.columns:
        for _, row in exp_df.iterrows():
            amt = _to_float(row.get("amount", 0))
            if amt <= 0:
                continue
            cur  = str(row.get("currency", "USD") or "USD").strip() or "USD"
            rate = ils_rates.get(cur, 0.0)
            cat  = str(row.get("category") or "").strip() or "Misc"
            cat_ils[cat] = cat_ils.get(cat, 0.0) + amt * rate

    cat_ils = {k: v for k, v in cat_ils.items() if v > 0}
    if not cat_ils:
        return

    total = sum(cat_ils.values())
    df = pd.DataFrame([
        {
            "Category":   k,
            "Amount (₪)": round(v),
            "Pct %":      round(v / total * 100, 1),
        }
        for k, v in sorted(cat_ils.items(), key=lambda x: -x[1])
    ])

    chart = (
        alt.Chart(df)
        .mark_bar(height=40, cornerRadiusEnd=3)
        .encode(
            x=alt.X(
                "Amount (₪):Q",
                stack="normalize",
                axis=alt.Axis(format="%", title="", labels=True, ticks=False, grid=False),
            ),
            color=alt.Color(
                "Category:N",
                legend=alt.Legend(orient="bottom", columns=3, title=None),
            ),
            order=alt.Order("Amount (₪):Q", sort="descending"),
            tooltip=[
                alt.Tooltip("Category:N",    title="Category"),
                alt.Tooltip("Amount (₪):Q",  format=",.0f", title="Amount (₪)"),
                alt.Tooltip("Pct %:Q",       format=".1f",  title="% of total"),
            ],
        )
        .properties(height=80)
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption(":gray[Amounts converted to ₪ at live rates. Hover a segment for details.]")


# ─── Add expense form ─────────────────────────────────────────────────────────
def _add_expense_form(trip_id: str, trip_row) -> None:
    trip_start = str(trip_row["start_date"])
    trip_end   = str(trip_row["end_date"])

    with st.form("add_expense_form", clear_on_submit=True, border=True):
        st.subheader("New expense", anchor=False)
        min_date = date.fromisoformat(trip_start) if trip_start else None
        max_date = date.fromisoformat(trip_end)   if trip_end   else None
        today    = date.today()
        # Clamp today into the trip window so the default is always valid
        if min_date and max_date:
            default_date = max(min_date, min(today, max_date))
        else:
            default_date = today
        exp_date = st.date_input(
            "Date",
            value=default_date,
            min_value=min_date,
            max_value=max_date,
        )
        category = st.selectbox("Category", EXPENSE_CATEGORIES)
        title    = st.text_input(
            "Title",
            placeholder="e.g. Dinner at Fisherman's Wharf",
        )
        desc     = st.text_area(
            "Description (optional)",
            placeholder="Extra details — who came, what was ordered, why this expense, …",
            height=80,
        )

        c1, c2 = st.columns([2, 1])
        with c1:
            amount = st.number_input("Amount", min_value=0.0, step=1.0, format="%.2f")
        with c2:
            currency = st.selectbox("Currency", CURRENCIES)

        links = st.text_input(
            "Links (optional)",
            placeholder="Label | https://example.com , Label2 | https://...",
        )

        s, c = st.columns(2)
        with s:
            saved = st.form_submit_button("Save", icon=":material/save:", use_container_width=True)
        with c:
            cancelled = st.form_submit_button("Cancel", icon=":material/close:", use_container_width=True)

        if saved:
            if not title.strip() or amount <= 0:
                st.error("Title and a positive amount are required.")
            else:
                eid = add_expense(
                    trip_id     = trip_id,
                    entry_date  = exp_date,
                    category    = category,
                    title       = title.strip(),
                    amount      = amount,
                    currency    = currency,
                    description = desc.strip(),
                    links       = links.strip(),
                )
                if eid:
                    st.success("Expense saved.")
                    st.session_state["adding_expense"] = False
                    st.cache_data.clear()
                    st.rerun()
        if cancelled:
            st.session_state["adding_expense"] = False
            st.rerun()


# ─── Page entry point ─────────────────────────────────────────────────────────
def render() -> None:
    st.header(":material/receipt_long: Expenses", anchor=False)

    trip_row = trip_picker()
    if trip_row is None:
        return

    trip_id = str(trip_row["trip_id"])

    itin_df = cached_itinerary(str(trip_row["trip_id"]), str(trip_row.get("sheet_tab", "")))
    _, entries_df = split_itinerary(itin_df)
    exp_df = _cached_expenses(trip_id)

    overall   = _totals(entries_df, exp_df)
    itin_only = _itin_totals(entries_df)
    exp_only  = _exp_totals(exp_df)

    # Fetch live ILS rates (cached 1h)
    ils_rates, rate_date = _get_ils_rates()
    ils_total, no_rate_curs = _to_ils(overall, ils_rates) if overall else (0.0, [])

    # ── Summary ──────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.subheader(":material/payments: Grand total", anchor=False)
        if overall:
            cols = st.columns(len(overall))
            for col, (cur, total) in zip(cols, sorted(overall.items())):
                col.metric(cur, f"{total:,.2f}")
        else:
            st.caption("No costs recorded yet.")

        st.caption(
            f"Itinerary: {_fmt_totals(itin_only)}   ·   "
            f"Additional: {_fmt_totals(exp_only)}"
        )

        # NIS total row
        if overall and ils_rates:
            st.divider()
            note = ""
            if no_rate_curs:
                note = f" _(excludes: {', '.join(no_rate_curs)})_"
            st.metric(
                f"≈ Total in NIS{note}",
                f"₪ {ils_total:,.0f}",
                help=f"Live rates via frankfurter.app · {rate_date}",
            )
            # ── 100 % stacked category chart ─────────────────────────────────
            _category_chart(entries_df, exp_df, ils_rates)
        elif overall and not ils_rates:
            st.caption("⚠️ Could not fetch live exchange rates for NIS conversion.")

    # ── Itinerary costs breakdown ─────────────────────────────────────────────
    with st.expander("From itinerary", icon=":material/calendar_month:"):
        priced = entries_df[
            entries_df.get("price", pd.Series(dtype=str))
            .astype(str).str.strip().replace("", pd.NA).notna()
        ] if not entries_df.empty and "price" in entries_df.columns else pd.DataFrame()

        priced = priced[priced["price"].astype(str).str.strip().isin(["", "0"]) == False] if not priced.empty else priced  # noqa: E712

        if priced.empty:
            st.caption("No priced entries in itinerary.")
        else:
            cat_opts = [""] + EXPENSE_CATEGORIES
            for entry_date, group in priced.groupby("date"):
                st.caption(f"**{entry_date}**")
                for _, row in group.iterrows():
                    eid  = str(row.get("entry_id", ""))
                    amt  = _to_float(row.get("price", 0))
                    cur  = str(row.get("currency", "")).strip()
                    dest = str(row.get("destination", "")).strip()
                    cur_cat = str(row.get("category") or "").strip()
                    if amt <= 0:
                        continue
                    cat_key = f"itin_cat_{eid}"
                    cat_idx = cat_opts.index(cur_cat) if cur_cat in cat_opts else 0
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        st.write(f"{dest} — **{amt:,.2f} {cur}**")
                    with c2:
                        st.selectbox(
                            "Category",
                            options=cat_opts,
                            index=cat_idx,
                            key=cat_key,
                            format_func=lambda x: "— category —" if x == "" else x,
                            on_change=_save_itin_category,
                            args=(eid, trip_id, cat_key),
                            label_visibility="collapsed",
                        )

    # ── Add expense button + title in same row ────────────────────────────────
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="bottom"):
        st.subheader(":material/add_card: Additional expenses", anchor=False)
        add_exp_clicked = st.button("", icon=":material/add:", type="primary", key="add_exp_btn")

    if add_exp_clicked:
        st.session_state["adding_expense"] = not st.session_state.get("adding_expense", False)

    if st.session_state.get("adding_expense", False):
        _add_expense_form(trip_id, trip_row)

    # ── Manual expenses list ──────────────────────────────────────────────────
    if exp_df.empty:
        st.info("No additional expenses yet.", icon=":material/info:")
        return

    for exp_date, group in exp_df.groupby("date"):
        st.caption(f"**{exp_date}**")
        for _, exp in group.iterrows():
            exp_id = str(exp.get("expense_id", ""))
            amt    = _to_float(exp.get("amount", 0))
            cur    = str(exp.get("currency", "")).strip()
            cat    = str(exp.get("category", "")).strip()
            title  = str(exp.get("title", "") or "").strip()
            desc   = str(exp.get("description", "") or "").strip()
            links  = str(exp.get("links", "") or "").strip()

            # Backfill safety net: if a legacy row only has description, use it as title
            if not title:
                title, desc = desc, ""

            with st.container(border=True):
                st.write(f"**{title}**")
                if desc:
                    st.write(desc)
                st.caption(f":material/label: {cat}")
                with st.container(horizontal=True, horizontal_alignment="right",
                                  vertical_alignment="center"):
                    st.write(f"**{amt:,.2f} {cur}**")
                    if st.button("", icon=":material/delete:", key=f"del_exp_{exp_id}",
                                 type="tertiary", help="Delete expense"):
                        if delete_expense(exp_id):
                            st.cache_data.clear()
                            st.rerun()
                if links:
                    for raw in [l.strip() for l in links.split(",") if l.strip()]:
                        label, url = parse_link(raw)
                        st.link_button(label, url, icon=":material/link:", use_container_width=True)
