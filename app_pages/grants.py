"""Page 5 — Your Applied Funding.

The full log of every application we've SUBMITTED to a donor — Approved, Under Review,
or Not Approved (plus Progress=Completed rows awaiting a decision). Not-Approved
applications are KEPT here, not dropped, so the page is a complete applied-funding record.

Layout:
  1. KPI strip — counts row (Total Submitted / Approved / Under Review / Not Approved) +
     amounts row (Total Requested / Secured / Unsecured / Requested Balance)
  2. Per-application detail (dropdown of every submitted application) + full editor pop-up
  3. Applications by funder (paginated at 10 rows/page)

Source of truth: `rfp_submissions.donor_decision` (which mirrors the Excel "Donor Decision
Status" column). The `applied_funding` table provides the reporting status / type / due date /
owner, edited via the editor's Reporting tab.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core.pipeline import usd_value
from core.records import clean_record, clean_df
from db.supabase_client import get_client, safe_execute

sb = get_client()
# Any tenant member may edit grant statuses (routine team-meeting task); Delete/admin
# actions live elsewhere and stay gated.
from core import permissions as _perm
_user = st.session_state.get("app_user") or {}
_can_edit = _perm.can_edit_status(_user)
st.title("Your Applied Funding")


# -----------------------------------------------------------------------------
# Data fetch — deduplicated RFPs only, ACTIVE statuses only
# -----------------------------------------------------------------------------
_main, _rail = st.columns([3.4, 1], gap="medium")
with _rail:
    from views.opportunity_rail import render_opportunity_rail
    render_opportunity_rail()
with _main:
    @st.cache_data(ttl=60)
    def _fetch() -> tuple[pd.DataFrame, pd.DataFrame]:
        sbc = get_client()
        try:
            rfps = pd.DataFrame(
                safe_execute(sbc.table("rfp_submissions").select("*")
                             .eq("is_duplicate", False)).data or []
            )
            grants = clean_df(pd.DataFrame(
                safe_execute(sbc.table("applied_funding").select("*")).data or []))
        except Exception as exc:
            st.warning(f"Couldn't load grants data right now (network issue): {exc}")
            return pd.DataFrame(), pd.DataFrame()
        rfps = clean_df(rfps)
        if not rfps.empty:
            dd = rfps["donor_decision"].fillna("").astype(str).str.strip().str.lower()
            _ps_col = (rfps["progress_status"] if "progress_status" in rfps.columns
                       else pd.Series("", index=rfps.index))
            ps = _ps_col.fillna("").astype(str).str.strip().str.lower()
            # ACTIVE = Approved OR Under Review OR submitted-to-donor (progress=Completed).
            # A row marked Progress=Completed on Tracking LEAVES Tracking (it's submitted),
            # so it must ENTER Active Grants even if its donor_decision is still blank —
            # otherwise it falls into the gap between the two pages (count stuck low). A
            # Completed-but-undecided grant is bucketed as Under Review (awaiting the donor).
            # Not Approved / Discontinued correctly stay OUT.
            _completed = ps.eq("completed")
            # SUBMITTED = every application we've sent to a donor: Approved, Under Review, or
            # Not Approved — PLUS a Progress=Completed row (submitted, decision still pending).
            # This page keeps the full applied-funding log, so Not Approved is NOT dropped;
            # only never-submitted rows (e.g. Discontinued with no decision) stay out.
            rfps["_submitted"] = (dd.isin({"approved", "under review", "not approved"})
                                  | _completed)
            rfps["_approved"] = dd.eq("approved")
            rfps["_pending"] = (dd.eq("under review")
                                | (_completed & ~dd.isin({"approved", "not approved"})))
            rfps["_not_approved"] = dd.eq("not approved")
            # Display status = the raw donor_decision, EXCEPT a Completed-but-undecided grant
            # shows as "Under Review" (its effective bucket) so the badge/label match the KPI.
            rfps["_status_display"] = (rfps["donor_decision"].fillna("").astype(str)
                                       .str.strip())
            _needs_ur = _completed & ~dd.isin({"approved", "under review", "not approved"})
            rfps.loc[_needs_ur, "_status_display"] = "Under Review"
            rfps["_usd_requested"] = rfps.apply(
                lambda r: usd_value(r.get("amount_requested"), r.get("currency")), axis=1
            )
            rfps["_usd_secured"] = rfps.apply(
                lambda r: usd_value(r.get("amount_secured"), r.get("currency_secured")), axis=1
            )
        return rfps, grants


    rfps, grants = _fetch()
    if rfps.empty:
        st.info("No RFPs in the database yet.")
        st.stop()

    active = rfps[rfps["_submitted"]].copy()
    if active.empty:
        st.info(
            "No applied funding yet. An application shows up here once it's **submitted** "
            "to a donor — its `donor_decision` is Approved / Under Review / Not Approved, "
            "or its Progress is marked **Completed**."
        )
        st.stop()


    # -----------------------------------------------------------------------------
    # KPIs
    # -----------------------------------------------------------------------------
    total_submitted = int(len(active))
    approved = int(active["_approved"].sum())
    pending = int(active["_pending"].sum())
    not_approved = int(active["_not_approved"].sum())
    total_requested = float(active["_usd_requested"].sum())
    total_secured = float(active.loc[active["_approved"], "_usd_secured"].sum())
    # Unsecured = requested amount tied to declined (Not Approved) applications — funding lost.
    total_unsecured = float(active.loc[active["_not_approved"], "_usd_requested"].sum())
    # Requested balance = requested still in play = requested − secured (won) − unsecured
    # (lost) = the amount on applications still awaiting a donor decision.
    requested_balance = max(0.0, total_requested - total_secured - total_unsecured)
    win_rate = (approved / total_submitted * 100) if total_submitted else 0

    # Row 1 — counts. Row 2 — amounts (own row so the $ figures aren't truncated).
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Submitted", total_submitted)
    c2.metric("Approved", approved)
    c3.metric("Under Review", pending)
    c4.metric("Not Approved", not_approved)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Requested (USD)", f"${total_requested:,.0f}")
    m2.metric("Total Secured (USD)", f"${total_secured:,.0f}",
              help="Secured on Approved applications.")
    m3.metric("Total Unsecured (USD)", f"${total_unsecured:,.0f}",
              help="Requested amount tied to Not-Approved applications (funding lost).")
    m4.metric("Total Requested Balance (USD)", f"${requested_balance:,.0f}",
              help="Requested − Secured − Unsecured: funding still awaiting a decision.")
    st.caption(f"Win rate: **{win_rate:.0f}%** (Approved ÷ Total Submitted)")
    st.divider()


    # -----------------------------------------------------------------------------
    # Per-application detail (every submitted application) — appears BEFORE "by funder"
    # -----------------------------------------------------------------------------
    st.subheader("Per-grant detail")

    # Approved first, then Under Review; within each by submission deadline desc
    priority = active.assign(
        _ord=active["_approved"].astype(int) * 2 + active["_pending"].astype(int)
    ).sort_values(["_ord", "call_submission_deadline"], ascending=[False, False])

    # Title first (fully visible) — UID and decision as suffix so the title gets
    # all the horizontal space. Iterate over priority.itertuples() and build the
    # list explicitly (a dict comprehension would silently collapse rows whose
    # generated label collides, which can happen if titles are duplicates).
    option_pairs: list[tuple[str, str]] = []
    for _, gr in priority.iterrows():
        label = (
            f"{gr.get('opportunity_title') or '(no title)'}  ·  "
            f"{gr['uid']}  ·  {gr.get('_status_display') or gr.get('donor_decision') or '—'}"
        )
        option_pairs.append((label, gr["uid"]))
    labels = [lbl for lbl, _ in option_pairs]
    uid_by_label = {lbl: uid for lbl, uid in option_pairs}

    # Explicit `key=` so Streamlit retains the user's pick across reruns
    # (without it, a rerun triggered elsewhere on the page snaps the
    # selectbox back to index 0).
    pick = st.selectbox(
        "Pick an application", labels,
        key="grants_active_picker",
    )
    uid = uid_by_label[pick]
    r = clean_record(active[active["uid"] == uid].iloc[0].to_dict())

    # Status badge — uses the display status (Completed-but-undecided shows as Under Review).
    _status = r.get("_status_display") or r.get("donor_decision") or "—"
    DD_COLOR = {"approved": "#dcf5e3", "under review": "#fff4cc", "not approved": "#fde0e0"}
    bg = DD_COLOR.get(str(_status).lower(), "#eee")

    # Deadline chip — every grant here is SUBMITTED, so a passed deadline reads as an
    # outcome (Submitted / Awarded / Not approved), never "Overdue".
    from core.pipeline import deadline_status as _dl_status
    _dchip = _dl_status(r.get("call_submission_deadline"), submitted=True, decision=_status)
    _dchip_txt = f" · Deadline: **{_dchip}**" if _dchip else ""

    h1, h2 = st.columns([4, 1])
    h1.markdown(f"### {r.get('opportunity_title') or '(no title)'}")
    h1.caption(f"UID `{r['uid']}` · Funder: **{r.get('funding_agency') or '—'}**{_dchip_txt}")
    h2.markdown(
        f"<div style='background:{bg};padding:14px 18px;border-radius:8px;"
        f"text-align:center;font-weight:600;font-size:1.05rem;margin-top:6px'>"
        f"{_status}</div>",
        unsafe_allow_html=True,
    )

    _geo = r.get("call_geographic_scope")
    if isinstance(_geo, (list, tuple)):
        _geo_str = ", ".join(str(g) for g in _geo if g) or "—"
    else:
        _geo_str = str(_geo) if _geo else "—"

    # Lead / Sub Applicant display logic.
    # Role drives the defaults (using the deploying-org short name from
    # settings — defaults to "Org" if not configured):
    #   * Prime     → the deploying org is the lead; no sub by default.
    #   * Sub       → another institution leads; the deploying org is the sub.
    #   * Technical → the deploying org provides TA, neither lead nor sub.
    # Excel cells containing "N/A" / "NA" / blank are treated as empty.
    def _placeholder(v) -> str:
        # Guard on type: a blank cell arrives as NaN (a float), which `or ""` won't
        # catch (NaN is truthy) and .strip() then breaks (AttributeError on float).
        s = (v if isinstance(v, str) else "").strip()
        return "" if s.lower() in ("", "n/a", "na", "none", "—") else s

    _role = (r.get("applicant_role") if isinstance(r.get("applicant_role"), str) else "").strip()
    _role_lc = _role.lower()
    _raw_lead = _placeholder(r.get("lead_applicant"))
    _raw_sub = _placeholder(r.get("sub_applicant"))

    # Pull the deploying-org short name so this page works for any
    # deployment without code changes.
    from core.settings import get_org_short  # noqa: E402
    _org = get_org_short()

    if _role_lc == "prime":
        _lead_display = _raw_lead or _org
        _sub_display = _raw_sub or "—"
    elif _role_lc == "sub":
        # The deploying org is sub; lead is someone else. TBD if Excel is
        # missing the lead.
        _lead_display = _raw_lead or "TBD"
        _sub_display = _raw_sub or _org
    elif _role_lc == "technical":
        # The deploying org provides technical assistance only; doesn't
        # benefit from the grant.
        _lead_display = _raw_lead or "TBD"
        _sub_display = _raw_sub or "—"
    else:
        _lead_display = _raw_lead or "—"
        _sub_display = _raw_sub or "—"

    # Helper: filter pandas NaN, NaT, "nan" strings, None, etc. to a dash.
    def _fmt(v) -> str:
        if v is None:
            return "—"
        try:
            if pd.isna(v):
                return "—"
        except (TypeError, ValueError):
            pass
        s = str(v).strip()
        if not s or s.lower() in ("nan", "nat", "none", "<na>"):
            return "—"
        return s


    # Donor Decision Date — actual award date if the donor has decided,
    # otherwise the expected award date. NOT decision_date (that's the team
    # review date which is something different).
    _award = None
    if not grants.empty:
        _glink = grants[grants["form_id_link"] == uid]
        if not _glink.empty:
            _award = _glink.iloc[0].get("award_date")
    _donor_decision_date = _fmt(_award) if _fmt(_award) != "—" else _fmt(r.get("expected_award_date"))

    # Date Submitted = "Date Completed" on the Excel Form1 sheet
    # (= when the deploying org actually submitted the proposal).
    _date_submitted = _fmt(r.get("date_completed"))

    dc1, dc2, dc3, dc4 = st.columns(4)
    dc1.markdown(f"**Role**  \n{_role or '—'}")
    dc1.markdown(f"**Date Submitted**  \n{_date_submitted}")
    dc2.markdown(f"**Lead Applicant**  \n{_lead_display}")
    dc2.markdown(f"**Donor Decision Date**  \n{_donor_decision_date}")
    dc3.markdown(f"**Sub Applicant**  \n{_sub_display}")
    dc3.markdown(f"**Requested**  \n${(r.get('_usd_requested') or 0):,.0f} USD")
    dc4.markdown(f"**Geographic Scope**  \n{_geo_str}")
    dc4.markdown(f"**Secured**  \n${(r.get('_usd_secured') or 0):,.0f} USD")

    # Linked applied_funding row — for reporting status. If MULTIPLE rows share
    # the same form_id_link (data-quality glitch from prior syncs), pick the
    # most-recently-updated one and warn — that explains "the displayed status
    # doesn't match what I edited" reports.
    linked = grants[grants["form_id_link"] == uid] if not grants.empty else pd.DataFrame()
    if len(linked) > 1:
        if "updated_at" in linked.columns:
            linked = linked.sort_values("updated_at", ascending=False)
        st.warning(
            f"⚠ {len(linked)} applied_funding rows match this RFP (`form_id_link = {uid}`). "
            f"Displaying the most-recently-updated one (grant_id "
            f"`{linked.iloc[0].get('grant_id') or '?'}`). Clean up duplicates via "
            f"**Admin → Data → Active Grants**."
        )

    if not linked.empty:
        g = clean_record(linked.iloc[0].to_dict())
        # "Not applicable" report status (e.g. a grant that wasn't approved) N/A's the
        # dependent reporting questions — there's nothing to report on, so we don't show a
        # spurious report type / due date / overdue countdown.
        _rep_na = str(g.get("status") or "").strip().lower() == "not applicable"
        st.markdown("**Reporting**")
        rep1, rep2, rep3, rep4, rep5 = st.columns(5)
        # Markdown rather than st.metric so long text values (e.g. "Not Started")
        # are not truncated to "Comp…" by the metric component's narrow width.
        rep1.markdown(f"**Grant ID**  \n{_fmt(g.get('grant_id'))}")
        rep2.markdown(f"**Report type**  \n{'N/A' if _rep_na else _fmt(g.get('report_type'))}")
        rep3.markdown(f"**Report status**  \n{_fmt(g.get('status'))}")
        rep4.markdown(f"**Due**  \n{'N/A' if _rep_na else _fmt(g.get('report_due_date'))}")
        due = pd.to_datetime(g.get("report_due_date"), errors="coerce")
        if _rep_na:
            rep5.markdown("**Days to due**  \nN/A")
        elif pd.notna(due):
            delta = (due.date() - date.today()).days
            rep5.markdown(f"**Days to due**  \n{delta:+d}")
        else:
            rep5.markdown("**Days to due**  \n—")
        # Fall back to the rfp's date_completed when applied_funding.submitted_date
        # is empty — they refer to the same event (when the deploying org submitted).
        submitted_date = _fmt(g.get("submitted_date"))
        if submitted_date == "—":
            submitted_date = _fmt(r.get("date_completed"))
        owner = _fmt(g.get("owner"))
        remarks = _fmt(g.get("remarks"))
        cap_extra = f" · Remarks: {remarks}" if remarks != "—" else ""
        st.caption(f"Submitted: **{submitted_date}** · Owner: **{owner}**{cap_extra}")
    else:
        st.caption(
            "_No matching row in applied_funding table yet — fill in Grant ID, Report Type, "
            "Report Due Date, Status, and Owner via the Excel `Active_Grants_Log` sheet "
            "or via the Admin tools._"
        )

    # Recent meeting notes
    notes = (
        sb.table("meeting_logs")
        .select("meeting_date,actions,owner,deadline")
        .eq("rfp_uid", uid)
        .order("meeting_date", desc=True)
        .limit(5)
        .execute()
        .data
    )
    if notes:
        st.markdown("**Recent meeting notes**")
        for n in notes:
            st.markdown(
                f"- **{n.get('meeting_date')}** — _{n.get('actions') or '(no action)'}_ "
                f"(owner: {n.get('owner') or '—'}, due: {n.get('deadline') or '—'})"
            )

    # ── Edit this application (open to ANY tenant member) ───────────────────────
    # Pick an application above and edit the WHOLE record in ONE pop-up — status, progress,
    # applicants (Lead/Sub), Date Submitted, Amount Requested/Secured, AND reporting (report
    # status/type/due/owner → applied_funding). Reuses the shared editor; the Application tab is
    # hidden here (it belongs on Review) and a Reporting tab is shown so reporting is edited in
    # the same place. Delete inside stays admin-only. Setting donor_decision keeps/moves the
    # application between buckets on save.
    if _can_edit:
        from views.rfp_editor import render_rfp_editor
        if st.button("✏️ Edit grant details", type="primary", key=f"grant_edit_{uid}"):
            # Pass the RAW submissions row (not the display-augmented dict) so the editor
            # writes real column values. render_rfp_editor is an @st.dialog → opens a modal.
            _raw = clean_record(rfps[rfps["uid"] == uid].iloc[0].to_dict())
            render_rfp_editor(_raw, sb=sb, user=_user, is_admin=_perm.is_admin(_user),
                              show_application=False, show_reporting=True)
        st.caption("Opens the full editor — Status, Progress, Lead/Sub applicant, Date "
                   "Submitted, Amount Requested/Secured, and the **Reporting** tab.")

    st.divider()


    # -----------------------------------------------------------------------------
    # Applications by funder — the full submitted log, incl. Not Approved (paginated)
    # -----------------------------------------------------------------------------
    st.subheader("Applications by funder")
    st.caption(
        "Every application we've submitted, grouped by funder — including declined ones. "
        "**Submissions** sums the per-RFP `submissions` column (an RFP can have multiple "
        "donor-side submissions). **RFPs** is the distinct count of applications. **Requested** "
        "sums `amount_requested`; **Secured** sums `amount_secured` from Approved rows only."
    )

    # Submissions sums the per-row Submissions column (one RFP can have multiple
    # donor-side submissions — captured in rfp_submissions.submissions). RFPs is
    # the distinct count of unique RFP rows.
    active["_submissions_int"] = active["submissions"].fillna(1).astype(int)

    by_funder = (
        active.groupby("funding_agency")
        .agg(
            RFPs=("uid", "count"),
            Submissions=("_submissions_int", "sum"),
            Approved=("_approved", "sum"),
            Pending=("_pending", "sum"),
            NotApproved=("_not_approved", "sum"),
            Requested=("_usd_requested", "sum"),
            Secured=("_usd_secured", lambda s: float(s[active.loc[s.index, "_approved"]].sum())),
        )
        .reset_index()
        .rename(columns={"funding_agency": "Funder"})
        .sort_values("Submissions", ascending=False)
    )

    # Pagination — 10 rows/page
    PAGE_SIZE = 10
    n_rows = len(by_funder)
    n_pages = max(1, (n_rows + PAGE_SIZE - 1) // PAGE_SIZE)
    page_key = "by_funder_page"
    cur_page = max(1, min(st.session_state.get(page_key, 1), n_pages))

    pc1, pc2, pc3 = st.columns([1, 3, 1])
    if pc1.button("◀ Prev", disabled=cur_page == 1, key="funder_prev"):
        st.session_state[page_key] = cur_page - 1
        st.rerun()
    pc2.markdown(
        f"<div style='text-align:center;padding-top:6px;color:#555'>"
        f"Page <b>{cur_page}</b> of <b>{n_pages}</b> · {n_rows} funder{'s' if n_rows != 1 else ''}</div>",
        unsafe_allow_html=True,
    )
    if pc3.button("Next ▶", disabled=cur_page >= n_pages, key="funder_next"):
        st.session_state[page_key] = cur_page + 1
        st.rerun()

    start = (cur_page - 1) * PAGE_SIZE
    page_df = by_funder.iloc[start:start + PAGE_SIZE]
    st.dataframe(
        page_df,
        width='stretch',
        hide_index=True,
        column_config={
            "RFPs":        st.column_config.NumberColumn(
                "RFPs", format="%d", help="Distinct RFPs submitted to this funder"
            ),
            "Submissions": st.column_config.NumberColumn(
                "Submissions", format="%d",
                help="Sum of submission events (RFPs × Submissions per RFP)"
            ),
            "Approved":    st.column_config.NumberColumn("Approved", format="%d"),
            "Pending":     st.column_config.NumberColumn("Under Review",  format="%d"),
            "NotApproved": st.column_config.NumberColumn("Not Approved", format="%d"),
            "Requested":   st.column_config.NumberColumn("Requested (USD)", format="$%.0f"),
            "Secured":     st.column_config.NumberColumn("Secured (USD)",   format="$%.0f"),
        },
    )

    # Diagnostic: show per-row submissions values so we can verify what's in the DB
    with st.expander("🔍 Diagnostic — per-row submissions values", expanded=False):
        diag = active[["uid", "opportunity_title", "funding_agency",
                        "donor_decision", "submissions", "_submissions_int"]].copy()
        diag.columns = ["UID", "Title", "Funder", "Donor Decision",
                        "submissions (raw)", "submissions (used for sum)"]
        st.dataframe(diag, width='stretch', hide_index=True)
        st.caption(
            f"Sum check: total submissions across all active rows = "
            f"**{int(active['_submissions_int'].sum())}**"
        )
