"""View — Pending Actions tab on the Activity page.

Surfaces every open follow-up across two sources:
  * BDT Meetings    — meeting_logs.is_resolved = false
  * Partner Engagements — engagement_logs.is_resolved = false
                          (column added by migration 014; if absent we
                          fall back to "outcome non-empty" as the proxy
                          so the view still renders pre-migration)

Each section gets:
  * Two KPI cards (Unresolved count + % Unresolved)
  * Per-owner summary line ("Bernard (3) · Rowan (2) · …")
  * Filterable list (date range + owner multiselect)
  * Inline Resolved / Not Resolved toggle (same UX as the per-week
    meeting_logs view, no jumping pages to close an item)

Why one consolidated tab rather than two scattered "show unresolved"
expanders on the existing tabs: the user wants a single screen they
can prep against before the Monday BDT call, with names and counts
visible at a glance.
"""
from __future__ import annotations

import streamlit as st

# ── DEBUG MARKER ────────────────────────────────────────────────────────
# Hard-coded literal that MUST render if exec() reaches the top of this
# file. If you don't see this marker in the Pending Actions tab, the
# view isn't being loaded at all (compile cache stale, file not
# deployed, or render_view returning early). Remove once Pending
# Actions is confirmed working end-to-end.
st.caption(":wrench: pending_actions.py loaded — DEBUG MARKER v1")
# ─────────────────────────────────────────────────────────────────────────

from datetime import date, datetime, timedelta  # noqa: E402

import pandas as pd  # noqa: E402

from db.supabase_client import get_client  # noqa: E402

# Wrapper page already gated auth; just pick up the user.
user = st.session_state.get("app_user") or {}
role = user.get("role", "collaborator")
can_edit = role in ("super_user", "admin", "reviewer", "collaborator")
sb = get_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _first_name(name: str | None) -> str:
    """First token of a person's name (or the whole string if single token).
    Used in the per-owner summary so "Jane Doe" displays as "Bernard"."""
    if not name:
        return "(unassigned)"
    parts = str(name).strip().split()
    return parts[0] if parts else "(unassigned)"


def _owner_summary(series: pd.Series) -> str:
    """Compact 'First (count) · First (count) · …' summary line, sorted
    by count desc, capped at 12 names to keep the line readable.
    Comma-separated owner cells split into individual people first."""
    if series.empty:
        return "—"
    # Comma-split (Owner cells like "Alex Kim, Jane Doe" should
    # count as two people each contributing 1).
    exploded: list[str] = []
    for v in series.dropna():
        for piece in str(v).split(","):
            p = piece.strip()
            if p:
                exploded.append(p)
    if not exploded:
        return "—"
    s = pd.Series(exploded)
    by_first = s.map(_first_name).value_counts()
    parts = [f"**{first}** ({n})" for first, n in by_first.head(12).items()]
    extra = "" if len(by_first) <= 12 else f"  + {len(by_first) - 12} more"
    return " · ".join(parts) + extra


def _kpi_row(unresolved: int, total: int, *, key_prefix: str) -> None:
    """Render the two KPI tiles (count + %)."""
    pct = (unresolved / total * 100) if total else 0.0
    c1, c2 = st.columns(2)
    c1.metric("Unresolved actions", unresolved,
              help=f"Open items out of {total} total in scope.")
    c2.metric("% Unresolved", f"{pct:.0f}%")


def _date_range_picker(label: str, df: pd.DataFrame, col: str,
                       *, key: str) -> tuple[date | None, date | None]:
    """Compact two-date range picker bound to the [min, max] of the column.
    Returns (None, None) if the column is empty / all NaT — caller treats
    that as "no date filter applied"."""
    if df.empty or col not in df.columns:
        return (None, None)
    dt = pd.to_datetime(df[col], errors="coerce").dropna()
    if dt.empty:
        return (None, None)
    lo, hi = dt.min().date(), dt.max().date()
    val = st.date_input(label, value=(lo, hi), min_value=lo, max_value=hi,
                        key=key)
    if isinstance(val, tuple) and len(val) == 2:
        return val
    if isinstance(val, date):
        return (val, val)
    return (lo, hi)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.title("Pending Actions")
st.caption(
    "Every open follow-up across BDT meetings + partner engagements. Use "
    "this screen to prep before the Monday BDT call: see counts, who owns "
    "what, and resolve items inline."
)


# ===========================================================================
# Section 1 — BDT Meetings
# ===========================================================================
st.markdown("### 🗓️ BDT Meetings")

# NOTE: @st.cache_data was removed from this and the engagements fetch
# below on 2026-06-06. When this view runs via render_view's exec(), the
# cached function's qualified name resolves as `views.pending_actions.
# _fetch_meetings` but Streamlit's cache occasionally couldn't hash the
# exec namespace which silently returned a "missing argument" warning —
# the section rendered as a blank placeholder. Direct calls are fine
# for a per-render fetch of ~tens of rows.

def _fetch_meetings() -> pd.DataFrame:
    res = (
        get_client()
        .table("meeting_logs")
        .select("id,meeting_date,donor_title,remarks,actions,owner,"
                "deadline,is_resolved,rfp_uid")
        .order("deadline")
        .execute()
    )
    df = pd.DataFrame(res.data or [])
    if not df.empty:
        df["meeting_date"] = pd.to_datetime(df["meeting_date"], errors="coerce")
        df["deadline"] = pd.to_datetime(df["deadline"], errors="coerce")
        df["is_resolved"] = df["is_resolved"].fillna(False).astype(bool)
    return df


try:
    df_m = _fetch_meetings()
except Exception as exc:
    st.error(
        f"⚠ Could not load meeting_logs: `{type(exc).__name__}: {exc}`. "
        f"Confirm the table exists in Supabase and RLS is disabled."
    )
    df_m = pd.DataFrame()

df_m_open = df_m[~df_m["is_resolved"]] if not df_m.empty else df_m

_kpi_row(len(df_m_open), len(df_m), key_prefix="pa_m_kpi")

if df_m_open.empty:
    st.success("No unresolved meeting actions. 🎉")
else:
    # Per-owner summary
    st.markdown(
        "**By owner:** " + _owner_summary(df_m_open["owner"])
    )

    # Filters
    with st.expander("Filters", expanded=False):
        fc1, fc2 = st.columns([2, 3])
        owners_all = sorted(df_m_open["owner"].dropna().unique().tolist())
        f_owners = fc1.multiselect("Owner", owners_all, key="pa_m_owners")
        with fc2:
            lo, hi = _date_range_picker(
                "Deadline range", df_m_open, "deadline",
                key="pa_m_date_range",
            )

    # Apply filters
    show = df_m_open.copy()
    if f_owners:
        show = show[show["owner"].isin(f_owners)]
    if lo and hi:
        show = show[
            (show["deadline"].dt.date >= lo)
            & (show["deadline"].dt.date <= hi)
        ]
    show = show.sort_values("deadline", na_position="last")

    st.caption(f"Showing **{len(show)}** of {len(df_m_open)} open items.")

    if show.empty:
        st.info("No items match the filters.")
    else:
        # Table header
        h = st.columns([2.3, 3, 3, 1.3, 1.2, 1.3])
        h[0].markdown("**Linked RFP / Donor**")
        h[1].markdown("**Issues**")
        h[2].markdown("**Action**")
        h[3].markdown("**Owner**")
        h[4].markdown("**Deadline**")
        h[5].markdown("**Status**")
        st.markdown("<hr style='margin:4px 0 8px'/>", unsafe_allow_html=True)

        for _, n in show.iterrows():
            row = st.columns([2.3, 3, 3, 1.3, 1.2, 1.3])
            label_link = (n.get("donor_title") or "").strip()
            if not label_link or label_link.lower() == "nan":
                label_link = f"`{n.get('rfp_uid') or '—'}`"
            row[0].markdown(label_link[:90])
            row[1].markdown(n.get("remarks") or "—")
            row[2].markdown(n.get("actions") or "—")
            row[3].markdown(_first_name(n.get("owner")) if n.get("owner")
                            else "—")
            dl = n.get("deadline")
            row[4].markdown(dl.strftime("%Y-%m-%d") if pd.notna(dl) else "—")
            if row[5].button(
                "Not Resolved",
                key=f"pa_m_resolve_{n['id']}",
                use_container_width=True,
                disabled=not can_edit,
            ):
                sb.table("meeting_logs").update({"is_resolved": True}) \
                    .eq("id", n["id"]).execute()
                st.cache_data.clear()
                st.rerun()


st.divider()


# ===========================================================================
# Section 2 — Partner Engagements
# ===========================================================================
st.markdown("### 🤝 Partner Engagements")

# @st.cache_data removed for the same exec-namespace reason — see the
# meetings fetch above.
def _fetch_engagements() -> tuple[pd.DataFrame, bool]:
    """Returns (dataframe, has_is_resolved_column).

    Pre-migration-014, engagement_logs has no is_resolved column. We
    detect this by attempting the select and catching the error; if it
    fails we re-fetch without is_resolved and signal the caller. The UI
    then falls back to "outcome non-empty" as the pending proxy."""
    cli = get_client()
    try:
        res = (
            cli.table("engagement_logs")
            .select("id,engagement_date,donor,engagement_type,application_lead,"
                    "purpose,outcome,is_resolved,linked_rfp_uid")
            .order("engagement_date", desc=True)
            .execute()
        )
        df = pd.DataFrame(res.data or [])
        if not df.empty:
            df["engagement_date"] = pd.to_datetime(
                df["engagement_date"], errors="coerce")
            df["is_resolved"] = df["is_resolved"].fillna(False).astype(bool)
        return df, True
    except Exception:
        # Migration 014 hasn't been applied — fall back without the column.
        res = (
            cli.table("engagement_logs")
            .select("id,engagement_date,donor,engagement_type,application_lead,"
                    "purpose,outcome,linked_rfp_uid")
            .order("engagement_date", desc=True)
            .execute()
        )
        df = pd.DataFrame(res.data or [])
        if not df.empty:
            df["engagement_date"] = pd.to_datetime(
                df["engagement_date"], errors="coerce")
        return df, False


try:
    df_e, has_resolved_col = _fetch_engagements()
except Exception as exc:
    st.error(
        f"⚠ Could not load engagement_logs: `{type(exc).__name__}: {exc}`. "
        f"Confirm the table exists in Supabase and RLS is disabled."
    )
    df_e, has_resolved_col = pd.DataFrame(), False

if df_e.empty:
    st.info("No partner engagements logged yet.")
else:
    if has_resolved_col:
        df_e_open = df_e[~df_e["is_resolved"]]
        scope_note = ""
    else:
        # Pre-migration fallback: treat engagements with non-empty
        # outcome as "follow-up captured but resolution-status unknown".
        # Show a banner pointing the admin at migration 014.
        st.warning(
            "engagement_logs is missing the `is_resolved` column. "
            "Apply **migration 014** to enable per-engagement resolution "
            "tracking. Until then, this section lists engagements with a "
            "non-empty `outcome` as the proxy for pending follow-ups."
        )
        has_outcome = df_e["outcome"].fillna("").astype(str).str.strip() != ""
        df_e_open = df_e[has_outcome]
        scope_note = " (proxy: outcome non-empty)"

    _kpi_row(len(df_e_open), len(df_e), key_prefix="pa_e_kpi")

    if df_e_open.empty:
        st.success("No unresolved partner engagements." + scope_note)
    else:
        st.markdown(
            "**By internal lead:** "
            + _owner_summary(df_e_open["application_lead"])
        )

        with st.expander("Filters", expanded=False):
            fc1, fc2 = st.columns([2, 3])
            leads_all = sorted(df_e_open["application_lead"]
                               .dropna().unique().tolist())
            f_leads = fc1.multiselect("Internal lead", leads_all,
                                       key="pa_e_leads")
            with fc2:
                lo_e, hi_e = _date_range_picker(
                    "Engagement date range", df_e_open,
                    "engagement_date", key="pa_e_date_range",
                )

        show_e = df_e_open.copy()
        if f_leads:
            show_e = show_e[show_e["application_lead"].isin(f_leads)]
        if lo_e and hi_e:
            show_e = show_e[
                (show_e["engagement_date"].dt.date >= lo_e)
                & (show_e["engagement_date"].dt.date <= hi_e)
            ]
        show_e = show_e.sort_values("engagement_date",
                                     ascending=False, na_position="last")

        st.caption(
            f"Showing **{len(show_e)}** of {len(df_e_open)} open "
            f"engagements{scope_note}."
        )

        if show_e.empty:
            st.info("No engagements match the filters.")
        else:
            h = st.columns([1.2, 2, 1.8, 1.5, 3, 1.3])
            h[0].markdown("**Date**")
            h[1].markdown("**Donor**")
            h[2].markdown("**Type**")
            h[3].markdown("**Internal lead**")
            h[4].markdown("**Outcome / follow-up**")
            h[5].markdown("**Status**")
            st.markdown("<hr style='margin:4px 0 8px'/>",
                        unsafe_allow_html=True)

            for _, e in show_e.iterrows():
                row = st.columns([1.2, 2, 1.8, 1.5, 3, 1.3])
                ed = e.get("engagement_date")
                row[0].markdown(ed.strftime("%Y-%m-%d")
                                if pd.notna(ed) else "—")
                row[1].markdown((e.get("donor") or "—")[:60])
                row[2].markdown(e.get("engagement_type") or "—")
                row[3].markdown(_first_name(e.get("application_lead"))
                                if e.get("application_lead") else "—")
                row[4].markdown((e.get("outcome") or "—")[:240])
                if has_resolved_col:
                    if row[5].button(
                        "Not Resolved",
                        key=f"pa_e_resolve_{e['id']}",
                        use_container_width=True,
                        disabled=not can_edit,
                    ):
                        sb.table("engagement_logs") \
                            .update({"is_resolved": True}) \
                            .eq("id", e["id"]).execute()
                        st.cache_data.clear()
                        st.rerun()
                else:
                    # No column → no toggle, just a disabled placeholder.
                    row[5].caption("apply migration 014")
