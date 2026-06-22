"""Verification & feedback view (Workstream A1 + A2).

A consolidated human-verification surface over what the scanner did, to grow and
CLEAN the labeled training set:

  A1 — Auto-rejected opportunities. The hard gate dropped these (scan_decisions
       system_reject) with NO human review. A verifier confirms or COUNTERS each:
         👍 should have entered (false reject — recoverable)
         😐 unsure
         👎 valid reject (confirm the gate)
       False-rejects can be recovered into rfp_submissions as tracked candidates.
       This is GATE-quality feedback — separate from the Proceed/Park/Decline
       model, which only ever sees gate-survivors.

  A2 — Inserted RFPs. Rate gate-survivors 👍/😐/👎 (good/neutral/bad) → more
       labels for the decision model. Mirrors the Review/Records feedback, here
       as one scannable list.

All actions are best-effort (telemetry must never break the page) and assistive.
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from core import decision_log, found_loader, source_registry
from core.type_detect import SOLICITATION_TYPES, INSTRUMENT_TYPES
from db.supabase_client import get_client, safe_execute

_PER_PAGE = 50   # batch-verify form: more rows/page = fewer Save+reload cycles
_REJECT_BADGE = {
    "false_reject": "👍 should have entered",
    "unsure": "😐 unsure",
    "valid_reject": "👎 valid reject",
}
_FB_BADGE = {"good": "👍 good", "neutral": "😐 neutral", "bad": "👎 bad"}


def _page_slice(items: list, key: str) -> list:
    """Render a page selector and return the current page's slice."""
    n = len(items)
    pages = max(1, (n + _PER_PAGE - 1) // _PER_PAGE)
    if pages > 1:
        pg = st.number_input(
            f"Page (1–{pages}, {n} items)", min_value=1, max_value=pages,
            value=1, step=1, key=f"{key}_pg")
    else:
        pg = 1
    start = (int(pg) - 1) * _PER_PAGE
    return items[start:start + _PER_PAGE]


def _link_md(title: str, link: str | None) -> str:
    t = html.escape((title or "(untitled)")[:160])
    if link:
        return f"[{t}]({link})"
    return t


def _row_id(r: dict) -> str:
    """Stable per-row key for widget keys + session verdict tracking."""
    return str(r.get("id") or r.get("uid") or r.get("opportunity_link")
               or r.get("opportunity_title") or id(r))


def _flash_set(key: str, msg: str) -> None:
    """Stash a success message to show AFTER the next st.rerun() (a message shown
    just before rerun is discarded by the rerun)."""
    st.session_state[f"_flash_{key}"] = msg


def _flash_show(key: str) -> None:
    msg = st.session_state.pop(f"_flash_{key}", None)
    if msg:
        st.success(msg)


def _csv_roundtrip(*, key: str, rows: list[dict], id_header: str, id_fn,
                   context_cols: list, editable: list, apply_row, user: dict,
                   aliases: dict | None = None) -> None:
    """Bulk human feedback at scale: Download → edit in Excel/Sheets → Upload.
    No Streamlit reloads while you label — the fast path for hundreds/thousands.

    editable = [(col, allowed_values|None, current_fn|None)] — columns the user
    fills (pre-filled from current_fn). apply_row(row, {col: value}, by) -> bool
    writes one edited row to the DB. Rows match on `id_header` (id_fn(row))."""
    import pandas as pd
    email = (user or {}).get("email")
    valid = [r for r in rows if id_fn(r)]
    # Download filename: drop the cosmetic "_csv"/"-csv" suffix from the widget key
    # and stamp the download time, e.g. rejv_20260621-153045.csv.
    base = key.removesuffix("_csv").removesuffix("-csv")
    with st.expander(f"⬇⬆ Bulk edit via CSV (Excel) — fastest for many rows "
                     f"({len(valid)})"):
        st.caption("Download → fill the editable column(s) in Excel/Sheets (leave "
                   "a row blank to skip it) → upload. Rows are matched by "
                   f"`{id_header}`; don't edit that column.")
        for col, opts, _ in editable:
            if opts:
                st.caption(f"**{col}** — allowed: {' · '.join(opts)}")
        recs = []
        for r in valid:
            row = {id_header: id_fn(r)}
            for hdr, fn in context_cols:
                row[hdr] = fn(r)
            for col, _opts, curfn in editable:
                row[col] = curfn(r) if curfn else ""
            recs.append(row)
        df = pd.DataFrame(recs)
        c1, c2 = st.columns([1, 2])
        c1.download_button(f"⬇ Download {len(df)} rows (CSV)",
                           df.to_csv(index=False).encode("utf-8"),
                           file_name=f"{base}_{datetime.now():%Y%m%d-%H%M%S}.csv",
                           mime="text/csv",
                           key=f"{key}_dl", width='stretch')
        up = c2.file_uploader("Upload edited CSV", type=["csv"], key=f"{key}_up",
                              label_visibility="collapsed")
        if up is not None and st.button("⬆ Apply uploaded CSV", type="primary",
                                        key=f"{key}_apply"):
            try:
                udf = pd.read_csv(up).fillna("")
            except Exception as exc:
                st.error(f"Couldn't read CSV: {exc}")
                return
            # Case-insensitive header resolution (+ aliases) so "Host"/"host",
            # "Listings URL"/"Sample", "Method"/"Ingestion" all work.
            ucol = {str(c).strip().lower(): c for c in udf.columns}

            def _col(name):
                if name.lower() in ucol:
                    return ucol[name.lower()]
                for alt in (aliases or {}).get(name, []):
                    if alt.lower() in ucol:
                        return ucol[alt.lower()]
                return None

            idcol = _col(id_header)
            if idcol is None:
                st.error(f"CSV has no **{id_header}** column — found: "
                         f"{', '.join(map(str, udf.columns))}. Nothing applied.")
                return
            rmap = {str(id_fn(r)).strip().lower(): r for r in valid}
            colmap = {c: _col(c) for c, _, _ in editable}
            n = err = matched = 0
            # Each apply_row hits the DB, so a large CSV is a slow loop — show a
            # spinner so the greyed-out page reads as "working", not "hung".
            with st.spinner(f"Applying {len(udf)} uploaded row(s)…"):
                for _, u in udf.iterrows():
                    r = rmap.get(str(u.get(idcol, "")).strip().lower())
                    if not r:
                        continue
                    matched += 1
                    vals = {c: (str(u.get(cm, "")).strip() if cm else "")
                            for c, cm in colmap.items()}
                    if not any(vals.values()):
                        continue
                    try:
                        if apply_row(r, vals, email):
                            n += 1
                    except Exception:
                        err += 1
            if matched == 0:
                st.error(f"0 rows matched on **{id_header}** — check that column's "
                         "values match the registry hosts. Nothing applied.")
                return
            # _flash_set (not st.success) so the result survives the st.rerun() and
            # shows OUTSIDE this expander, which collapses on rerun.
            _flash_set(base, f"✅ Applied {n} of {matched} matched row(s)."
                       + (f"  ({err} errored)" if err else ""))
            # Drop this table's cached inline-grid widget state so the rows
            # re-seed from the DB and show the just-uploaded values.
            for k in [k for k in list(st.session_state)
                      if isinstance(k, str) and k.startswith(f"{base}_")]:
                del st.session_state[k]
            st.rerun()


# Opportunity-type vocabulary — full the second tenant scope (current the organisation use is the first
# row: Grant…Tender). A human-set Type in the Verify tables is ground truth for the
# type classifier, logged to scan_decisions (event_type 'type_label').
# The Verify human type-capture uses the SOLICITATION axis (how to apply) — the
# instrument axis is auto-detected. Both vocabularies live in core.type_detect.
_TYPE_OPTS = SOLICITATION_TYPES


def _verify_table(*, user: dict, key: str, rows: list[dict],
                  label_map: dict, db_event: str, save_one, filter_key: str,
                  filter_label: str, extra_cols: list, recover: bool = False,
                  reason_opts: dict | None = None,
                  type_opts: list | None = None) -> None:
    """FORM-based verify list — the row controls live inside an `st.form`, so
    selecting a verdict / correct-reason does NOT reload the page (Streamlit only
    commits a form on submit). One reload per *page* (on Save), not per click —
    fixing the data_editor's reload-and-undo behaviour. Defaults come from the
    DATABASE, so saved verdicts show on return (page away/back, reload).

    label_map = {code: display}; reason_opts = {display: code} adds an in-form
    "Correct reason" selector (the right reason when the system reason is wrong —
    a learning signal); save_one(row, verdict_code, by, reason_code)."""
    email = (user or {}).get("email")
    code_map = {v: k for k, v in label_map.items()}
    db = decision_log.latest_verifications(db_event)              # link -> code
    db_reasons = decision_log.latest_reasons(db_event) if reason_opts else {}
    db_types = decision_log.latest_verifications("type_label") if type_opts else {}
    rev_reason = {c: d for d, c in (reason_opts or {}).items()}   # code -> display
    _flash_show(key)
    PER = 50
    v_opts = ["—"] + list(label_map.values())
    r_opts = ["—"] + (list(reason_opts.keys()) if reason_opts else [])
    t_opts = ["—"] + (list(type_opts) if type_opts else [])

    cats = ["All"] + sorted({(r.get(filter_key) or "—") for r in rows})
    cf, co, _navsp = st.columns([3, 1.4, 3])
    pick = cf.selectbox(f"Filter by {filter_label}", cats, key=f"{key}_filter")
    only_new = co.checkbox("Only unverified", value=False, key=f"{key}_new")

    items = []
    for r in rows:
        if pick != "All" and (r.get(filter_key) or "—") != pick:
            continue
        rid = _row_id(r)
        link = (r.get("opportunity_link") or "").strip()
        cur = label_map.get(db.get(link), "")           # DB verdict label
        if only_new and cur:
            continue
        items.append((r, rid, link, cur))

    # Bulk CSV path — fastest for hundreds/thousands (no per-selection reloads).
    def _vt_apply(r, vals, by):
        code = code_map.get(vals.get("Verdict", "").strip())
        rc = ((reason_opts or {}).get(vals.get("Correct reason", "").strip())
              if reason_opts else None)
        tv = (vals.get("Solicitation", "").strip() if type_opts else "")
        typed = bool(tv and tv != "—")
        if typed:
            decision_log.log_type_label(r, tv, by)
        if not code and rc and "valid_reject" in label_map:
            code = "valid_reject"      # reason-only correction on a valid reject
        if not code:
            return typed               # a Type-only correction still counts
        return save_one(r, code, by, rc)

    _ed = [("Verdict", list(label_map.values()), lambda r: label_map.get(
        db.get((r.get("opportunity_link") or "").strip()), ""))]
    if type_opts:
        _ed.append(("Solicitation", list(type_opts),
                    lambda r: db_types.get((r.get("opportunity_link") or "").strip(), "")))
    if reason_opts:
        # current_fn pulls the saved correction so a download round-trips the value
        # (code -> display via rev_reason); previously None → always blank in CSV.
        _ed.append(("Correct reason", list(reason_opts.keys()),
                    lambda r: rev_reason.get(
                        db_reasons.get((r.get("opportunity_link") or "").strip()), "")))
    _csv_roundtrip(
        key=f"{key}_csv", rows=[t[0] for t in items],
        id_header="opportunity_link",
        id_fn=lambda r: (r.get("opportunity_link") or "").strip(),
        context_cols=[("Title", lambda r: (r.get("opportunity_title") or "")[:120])]
        + list(extra_cols),
        editable=_ed, apply_row=_vt_apply, user=user)

    pages = max(1, (len(items) + PER - 1) // PER)
    pg = max(1, min(int(st.session_state.get(f"{key}_pg", 1)), pages))
    cc, cprev, cnext = st.columns([6, 1, 1])
    cc.markdown(
        "<div style='padding-top:.5rem;color:#475569;font-size:0.9rem;'>"
        f"{len(items)} item(s) · page {pg}/{pages} · pick verdicts (no reload), "
        "then <b>Save page</b>. Save before paging." + "</div>",
        unsafe_allow_html=True)
    if cprev.button("‹ Prev", key=f"{key}_prev", disabled=pg <= 1, width='stretch'):
        st.session_state[f"{key}_pg"] = pg - 1
        st.rerun()
    if cnext.button("Next ›", key=f"{key}_next", disabled=pg >= pages,
                    width='stretch'):
        st.session_state[f"{key}_pg"] = pg + 1
        st.rerun()
    st.session_state[f"{key}_pg"] = pg
    sl = items[(pg - 1) * PER: pg * PER]
    if not sl:
        st.success("Nothing here with these filters. 🎉")
        return

    if type_opts or reason_opts:
        # Verdict gets the most room so its radio options sit on ONE line (the
        # reject log has 4: — / Should've entered / Unsure / Valid reject); Type
        # and Correct-reason are compact dropdowns. Opportunity already wraps.
        widths = [4.0, 3.0]
        if type_opts:
            widths.append(1.5)
        if reason_opts:
            widths.append(2.0)
    else:
        widths = [6, 3]
    h = st.columns(widths)
    h[0].markdown("**Opportunity** · funder · reason")
    h[1].markdown("**Verdict**")
    _hi = 2
    if type_opts:
        h[_hi].markdown("**Solicitation**"); _hi += 1
    if reason_opts:
        h[_hi].markdown("**Correct reason**")

    with st.form(f"{key}_form_{pg}"):
        for r, rid, link, cur in sl:
            c = st.columns(widths)
            meta = " · ".join(f"{hdr}: {fn(r)}" for hdr, fn in extra_cols if fn(r))
            # Green "✓ verified" when a human verdict already exists in the DB.
            vbadge = ("<span style='color:#16a34a;font-weight:600;'>✓ verified</span>"
                      " · " if cur else "")
            title = (r.get("opportunity_title") or "(untitled)")[:120]
            link_md = f"[{title}]({link})" if link else title
            c[0].markdown(f"{link_md}  <span style='color:#94a3b8;font-size:0.8rem;'>"
                          f"· {vbadge}{meta}</span>", unsafe_allow_html=True)
            vk = f"{key}_v_{rid}"
            if vk not in st.session_state:
                st.session_state[vk] = cur if cur in v_opts else "—"
            c[1].radio("v", v_opts, key=vk, horizontal=True,
                       label_visibility="collapsed")
            ci = 2
            if type_opts:
                tk = f"{key}_t_{rid}"
                if tk not in st.session_state:
                    dt = db_types.get(link, "")
                    st.session_state[tk] = dt if dt in t_opts else "—"
                c[ci].selectbox("t", t_opts, key=tk, label_visibility="collapsed")
                ci += 1
            if reason_opts:
                rk = f"{key}_r_{rid}"
                if rk not in st.session_state:
                    st.session_state[rk] = rev_reason.get(db_reasons.get(link), "—")
                c[ci].selectbox("r", r_opts, key=rk, label_visibility="collapsed")
        submitted = st.form_submit_button(f"💾 Save page ({len(sl)})",
                                          type="primary")

    if submitted:
        n = 0
        for r, rid, link, cur in sl:
            code = code_map.get(st.session_state.get(f"{key}_v_{rid}", "—"))
            rlabel = st.session_state.get(f"{key}_r_{rid}", "—") if reason_opts else "—"
            reason_code = (reason_opts or {}).get(rlabel) if rlabel != "—" else None
            saved = False
            # Save when the verdict changed OR a correct-reason was supplied (the
            # logger de-dups, so re-saving an unchanged reason is a no-op).
            if code and (db.get(link) != code or reason_code) and \
                    save_one(r, code, email, reason_code):
                saved = True
            # Type is an independent label — save it even with no verdict change.
            if type_opts:
                tv = st.session_state.get(f"{key}_t_{rid}", "—")
                if tv != "—" and db_types.get(link) != tv and \
                        decision_log.log_type_label(r, tv, email):
                    saved = True
            if saved:
                n += 1
        _flash_set(key, f"✅ Saved {n} row(s) to the database.")
        st.rerun()

    if recover:
        rec_rows = [(r, rid) for (r, rid, link, cur) in sl
                    if code_map.get(cur) == "false_reject"]
        if rec_rows:
            with st.expander(f"⤴ Recover {len(rec_rows)} false-reject(s) "
                             "into Found Records"):
                for r, rid in rec_rows:
                    if st.button(f"Recover: {(r.get('opportunity_title') or '(untitled)')[:70]}",
                                 key=f"{key}_rec_{rid}"):
                        res = found_loader.load_candidate({
                            "opportunity_title": r.get("opportunity_title"),
                            "opportunity_link": r.get("opportunity_link"),
                            "funding_agency": r.get("funding_agency"),
                            "submission_deadline": r.get("submission_deadline"),
                        }, user, provenance="reject-recovery")
                        st.toast(f"Recovered as {res['uid']}." if res["ok"] else
                                 "Already tracked." if res["skipped"] else
                                 f"Failed: {res['reason']}",
                                 icon="⤴" if res["ok"] else "ℹ️")
                        st.rerun()


# ---------------------------------------------------------------------------
# A1 — auto-rejected opportunities
# ---------------------------------------------------------------------------
def _render_reject_log(user: dict) -> None:
    st.caption(
        "These never reached the team — the hard gate rejected them. "
        "**Should've entered** = the gate was wrong (recover it) · **Unsure** · "
        "**Valid reject** = the gate was right. Set the **Verdict** column, then "
        "**Save** — saved verdicts persist (shown from the database), so you can "
        "page away and back. *Reason* and *Closes* are separate columns: a future "
        "*Closes* date is just context, not the reject reason.")
    try:
        rows = safe_execute(
            get_client().table("scan_decisions")
            .select("id,created_at,label,reason,opportunity_title,"
                    "opportunity_link,funding_agency,submission_deadline")
            .eq("event_type", "system_reject")
            .order("created_at", desc=True).limit(3000)).data or []
    except Exception as exc:
        st.warning(f"Couldn't load rejects (migration 027 run?): {exc}")
        return
    if not rows:
        st.info("No auto-rejected opportunities logged yet.")
        return
    _verify_table(
        user=user, key="rejv", rows=rows,
        label_map={"false_reject": "Should've entered", "unsure": "Unsure",
                   "valid_reject": "Valid reject"},
        db_event="reject_verification",
        save_one=decision_log.log_reject_verification,
        filter_key="label", filter_label="reject reason",
        extra_cols=[("Funder", lambda r: r.get("funding_agency") or ""),
                    ("Rejection reason", lambda r: r.get("label") or "—"),
                    ("Deadline", lambda r: str(r.get("submission_deadline") or ""))],
        recover=True,
        # Correct-reason override → learning signal (system reason vs human).
        reason_opts={
            "System reason is correct": "_ok",
            "Deadline passed": "deadline",
            "Not an RFP": "not-an-rfp",
            "Off theme": "theme",
            "Wrong geography": "geography",
            "Wrong country": "country",
            "Applicant-type mismatch": "eligibility",
            "Wrong opportunity type": "type",
            "Language": "language",
            "Aggregator / blog / listing": "aggregator",
            "Feasibility": "feasibility",
        },
        type_opts=_TYPE_OPTS)


# ---------------------------------------------------------------------------
# A2 — inserted RFPs (rate gate-survivors)
# ---------------------------------------------------------------------------
def _render_inserted_feedback(user: dict) -> None:
    st.caption(
        "Gate-survivors already tracked. Rate each to grow the model's labels: "
        "**Good** = Proceed · **Neutral** = Park · **Bad** = Decline. Set the "
        "**Verdict** column, then **Save** — ratings persist (shown from the "
        "database).")
    try:
        rows = safe_execute(
            get_client().table("rfp_submissions")
            .select("uid,opportunity_title,opportunity_link,funding_agency,"
                    "source,decision,auto_recommendation,is_duplicate,updated_at")
            .order("updated_at", desc=True).limit(800)).data or []
    except Exception as exc:
        st.warning(f"Couldn't load records: {exc}")
        return
    rows = [r for r in rows if not r.get("is_duplicate")]
    if not rows:
        st.info("No records to rate yet.")
        return
    _verify_table(
        user=user, key="fbv", rows=rows,
        label_map={"good": "Good", "neutral": "Neutral", "bad": "Bad"},
        db_event="feedback", save_one=decision_log.log_feedback,
        filter_key="source", filter_label="source",
        extra_cols=[
            ("Funder", lambda r: r.get("funding_agency") or ""),
            ("Decision", lambda r: str(r.get("decision")
                                       or r.get("auto_recommendation") or "—").title()),
            ("Source", lambda r: (r.get("source") or "").title())],
        type_opts=_TYPE_OPTS)


# ---------------------------------------------------------------------------
# Source registry — classify hosts (aggregator vs primary) + push to catalogue
# ---------------------------------------------------------------------------
# Source-catalogue taxonomy (Bernard's standard, sentence case). "Source class"
# is authoritative; the scanner's coarse classification + confirmed status are
# DERIVED from it on save (no separate redundant "Class"/"Status" columns).
_SRC_OPTS = ["Unknown", "Primary source", "Opportunity Aggregator",
             "Application/resource host",
             # legacy values kept selectable so existing rows don't error:
             "Aggregator", "Intelligence platform", "Grant database",
             "Tender database", "Job aggregator", "ATS feed", "API provider"]
_VERIF_OPTS = ["Unverified", "Needs primary-source confirmation",
               "Primary verified", "Aggregator verified"]
_ACCESS_OPTS = ["Unknown", "Free", "Freemium", "Paid", "API", "RSS/feed",
                "Login required"]
# Unified "Method" vocabulary — the SAME dropdown for the registry (Verify) and
# the donor catalogue (Bernard: keep "Method" in both tables). Each maps 1:1 to a
# scan dispatch method in push_primaries:
#   API→rest_json · RSS / feed→rss · Page crawl→html · JS page crawl→html_js · Manual→manual
_METHOD_OPTS = ["API", "RSS / feed", "Page crawl", "JS page crawl", "Manual"]
_VERIFIED = ("Primary verified", "Aggregator verified")


def _norm_method(s: str) -> str:
    """Normalise any stored/legacy ingestion value to a unified Method option."""
    t = (s or "").lower()
    if "api" in t:
        return "API"
    if "rss" in t or "feed" in t or "newsletter" in t:
        return "RSS / feed"
    if "dynamic" in t or "js" in t or "playwright" in t:
        return "JS page crawl"
    if "manual" in t or "licensed" in t or "linked" in t:
        return "Manual"
    return "Page crawl"


def _src_class_of(r: dict) -> str:
    """Display Source class — stored value, else derived from coarse class."""
    return (r.get("source_class")
            or {"primary": "Primary source", "aggregator": "Opportunity Aggregator",
                "blog": "Opportunity Aggregator"}.get(r.get("classification"),
                                                      "Unknown"))


def _verif_of(r: dict) -> str:
    """Display Verification level — derived from (classification, status)."""
    if (r.get("status") or "").lower() == "confirmed":
        return "Primary verified" if r.get("classification") == "primary" \
            else "Aggregator verified"
    return ("Needs primary-source confirmation"
            if r.get("classification") not in (None, "", "unknown")
            else "Unverified")


def _derive_class(source_class: str) -> str:
    """Source class → coarse classification the scanner gate uses."""
    return {"Primary source": "primary", "Unknown": "unknown",
            "": "unknown"}.get(source_class, "aggregator")


def _render_source_registry(user: dict) -> None:
    import pandas as pd
    email = (user or {}).get("email")
    _flash_show("srcreg")
    st.caption(
        "Every host the scanner meets, with the source-catalogue taxonomy. Set "
        "**Source class** + **Verification** (the scanner trusts *Primary "
        "verified* primaries and rejects *…verified* aggregators/blogs), and fill "
        "**Access / Ingestion**. Edit inline, then **Save** — saved values persist "
        "(shown from the database). Push confirmed primaries into the Sources "
        "catalogue below.")

    # --- Manual add (always available, even when the registry is empty) ----
    with st.expander("➕ Add a source manually"):
        with st.form("srcreg_add"):
            d1, d2 = st.columns([3, 2])
            new_donor = d1.text_input("Source Name",
                                      placeholder="e.g. Wellcome Trust")
            new_code = d2.text_input("Code", placeholder="e.g. Wellcome")
            new_host = st.text_input(
                "Listing URL (or host)",
                placeholder="https://example.org/grants")
            a2, a3 = st.columns(2)
            new_sc = a2.selectbox("Source class", _SRC_OPTS,
                                  index=_SRC_OPTS.index("Primary source"))
            new_vf = a3.selectbox("Verification", _VERIF_OPTS,
                                  index=_VERIF_OPTS.index("Primary verified"))
            a4, a5 = st.columns(2)
            new_ac = a4.selectbox("Access", _ACCESS_OPTS,
                                  index=_ACCESS_OPTS.index("Free"))
            new_in = a5.selectbox("Method", _METHOD_OPTS,
                                  index=_METHOD_OPTS.index("Page crawl"))
            ty1, ty2 = st.columns(2)
            new_sol = ty1.multiselect(
                "Solicitation type(s) — how to apply", SOLICITATION_TYPES,
                help="NOFO/RFP/CFP/CFA/EOI/Tender… (how the call is announced).")
            new_inst = ty2.multiselect(
                "Instrument type(s) — the contract", INSTRUMENT_TYPES,
                help="Grant/Cooperative Agreement/Loan/Fellowship… (what's awarded).")
            new_notes = st.text_input("Notes (optional)")
            st.caption("Re-adding an existing host UPDATES it (use this to fix a "
                       "wrong listing URL).")
            if st.form_submit_button("➕ Add / update source", type="primary"):
                ok, msg = source_registry.add_row(new_host, {
                    "donor_name": new_donor or None, "donor_code": new_code or None,
                    "source_class": new_sc,
                    "classification": _derive_class(new_sc),
                    "status": "confirmed" if new_vf in _VERIFIED else "pending",
                    "access_model": new_ac, "ingestion_method": new_in,
                    "solicitation_types": new_sol or None,
                    "instrument_types": new_inst or None,
                    "sample_url": new_host if "/" in (new_host or "") else None,
                    "notes": new_notes or None,
                }, by=email)
                if ok:
                    _flash_set("srcreg", "✅ " + msg)
                    st.rerun()
                else:
                    st.warning(msg)

    rows = source_registry.list_rows()
    if not rows:
        st.info("Registry is empty. It fills during a scan — or seed it now:\n\n"
                "`python scripts/backfill_source_registry.py --commit`")
        return

    def _eff(r, field, disp_fn=None):
        if disp_fn:
            return disp_fn(r)
        return r.get(field) or ("Unknown" if field == "access_model"
                                else "page crawl" if field == "ingestion_method"
                                else "")

    f1, f2 = st.columns([3, 1])
    classes = ["All"] + sorted({_eff(r, "source_class", _src_class_of) for r in rows})
    pick = f1.selectbox("Filter by source class", classes, key="srcreg_cls")
    only_unv = f2.checkbox("Only unverified", value=False, key="srcreg_pend")

    items = []
    for r in rows:
        if pick != "All" and _eff(r, "source_class", _src_class_of) != pick:
            continue
        if only_unv and _eff(r, "verification_level", _verif_of) in _VERIFIED:
            continue
        items.append(r)

    def _sr_apply(r, vals, by):
        sc = vals.get("Source class") or _src_class_of(r)
        vl = vals.get("Verification") or _verif_of(r)
        f = {
            "source_class": None if sc in ("Unknown", "") else sc,
            "classification": _derive_class(sc),
            "status": "confirmed" if vl in _VERIFIED else "pending",
            "access_model": vals.get("Access") or None,
            "ingestion_method": (_norm_method(vals["Method"])
                                 if vals.get("Method") else None),
        }
        if vals.get("Source Name"):
            f["donor_name"] = vals["Source Name"]
        if vals.get("Code"):
            f["donor_code"] = vals["Code"]
        if vals.get("Host"):
            f["sample_url"] = vals["Host"]

        def _multi(v):
            return [t.strip() for t in (v or "").replace(",", ";").split(";")
                    if t.strip()] or None
        if "Solicitation types" in vals:
            f["solicitation_types"] = _multi(vals.get("Solicitation types"))
        if "Instrument types" in vals:
            f["instrument_types"] = _multi(vals.get("Instrument types"))
        return source_registry.update_row(r["host"], f, by)
    _csv_roundtrip(
        key="srcreg_csv", rows=items, id_header="host",
        id_fn=lambda r: r.get("host"),
        context_cols=[("ID", lambda r: r.get("source_uid")),
                      ("Hits", lambda r: r.get("hits") or 0)],
        editable=[("Source Name", None, lambda r: r.get("donor_name") or ""),
                  ("Code", None, lambda r: r.get("donor_code") or ""),
                  ("Host", None, lambda r: r.get("sample_url") or ""),
                  ("Source class", _SRC_OPTS, _src_class_of),
                  ("Verification", _VERIF_OPTS, _verif_of),
                  ("Access", _ACCESS_OPTS, lambda r: r.get("access_model") or "Unknown"),
                  ("Method", _METHOD_OPTS,
                   lambda r: _norm_method(r.get("ingestion_method"))),
                  ("Solicitation types", None,
                   lambda r: "; ".join(r.get("solicitation_types") or [])),
                  ("Instrument types", None,
                   lambda r: "; ".join(r.get("instrument_types") or []))],
        apply_row=_sr_apply, user=user,
        aliases={"Source Name": ["Donor"], "Host": ["Listings URL", "Sample"],
                 "Method": ["Ingestion"]})
    # Paginated FORM (selections don't reload — only "Save page" commits).
    PER = 50
    pages = max(1, (len(items) + PER - 1) // PER)
    pg = max(1, min(int(st.session_state.get("srcreg_pg", 1)), pages))
    cc, cprev, cnext = st.columns([6, 1, 1])
    cc.markdown(f"<div style='padding-top:.5rem;color:#475569;font-size:.9rem;'>"
                f"{len(items)} hosts · page {pg}/{pages} · edit (no reload per "
                "cell), then <b>Save page</b>. Save before paging.</div>",
                unsafe_allow_html=True)
    if cprev.button("‹ Prev", key="srcreg_prev", disabled=pg <= 1, width='stretch'):
        st.session_state["srcreg_pg"] = pg - 1
        st.rerun()
    if cnext.button("Next ›", key="srcreg_next", disabled=pg >= pages,
                    width='stretch'):
        st.session_state["srcreg_pg"] = pg + 1
        st.rerun()
    st.session_state["srcreg_pg"] = pg
    sl = items[(pg - 1) * PER: pg * PER]

    W = [2.4, 1.6, 1.5, 1.0, 1.2, 1.7, 1.7]
    hh = st.columns(W)
    for i, lbl in enumerate(["ID · Source Name · Code · Host", "Source class",
                             "Verification", "Access", "Method",
                             "Solicitation", "Instrument"]):
        hh[i].markdown(f"**{lbl}**")
    dbrows = {r["host"]: r for r in rows}
    with st.form(f"srcreg_form_{pg}"):
        for r in sl:
            host = r["host"]
            c = st.columns(W)
            sample = r.get("sample_url") or ""
            donor = r.get("donor_name") or host
            c[0].caption(f"`#{r.get('source_uid')}` · **{donor}** · "
                         f"{r.get('donor_code') or '—'} · `{host}`")
            uk = f"srcreg_url_{host}"
            if uk not in st.session_state:
                st.session_state[uk] = sample
            c[0].text_input("url", key=uk, label_visibility="collapsed",
                            placeholder="listing URL (editable)")
            for col, kfn, dval in (
                    ("sc", _src_class_of, None),
                    ("vf", _verif_of, None),
                    ("ac", None, r.get("access_model") or "Unknown"),
                    ("in", lambda r: _norm_method(r.get("ingestion_method")), None)):
                k = f"srcreg_{col}_{host}"
                if k not in st.session_state:
                    st.session_state[k] = kfn(r) if kfn else dval
            solk, instk = f"srcreg_sol_{host}", f"srcreg_inst_{host}"
            if solk not in st.session_state:
                st.session_state[solk] = [t for t in (r.get("solicitation_types") or [])
                                          if t in SOLICITATION_TYPES]
            if instk not in st.session_state:
                st.session_state[instk] = [t for t in (r.get("instrument_types") or [])
                                           if t in INSTRUMENT_TYPES]
            c[1].selectbox("sc", _SRC_OPTS, key=f"srcreg_sc_{host}",
                           label_visibility="collapsed")
            c[2].selectbox("vf", _VERIF_OPTS, key=f"srcreg_vf_{host}",
                           label_visibility="collapsed")
            c[3].selectbox("ac", _ACCESS_OPTS, key=f"srcreg_ac_{host}",
                           label_visibility="collapsed")
            c[4].selectbox("in", _METHOD_OPTS, key=f"srcreg_in_{host}",
                           label_visibility="collapsed")
            c[5].multiselect("sol", SOLICITATION_TYPES, key=solk,
                             label_visibility="collapsed")
            c[6].multiselect("inst", INSTRUMENT_TYPES, key=instk,
                             label_visibility="collapsed")
        sr_submitted = st.form_submit_button(f"💾 Save page ({len(sl)})",
                                             type="primary")

    if sr_submitted:
        n = 0
        for r in sl:
            host = r["host"]
            db = dbrows.get(host, {})
            sc = st.session_state.get(f"srcreg_sc_{host}", "Unknown")
            vl = st.session_state.get(f"srcreg_vf_{host}", "Unverified")
            fields = {
                "source_class": None if sc in ("Unknown", "") else sc,
                "classification": _derive_class(sc),
                "status": "confirmed" if vl in _VERIFIED else "pending",
                "access_model": st.session_state.get(f"srcreg_ac_{host}") or None,
                "ingestion_method": st.session_state.get(f"srcreg_in_{host}") or None,
                "solicitation_types":
                    st.session_state.get(f"srcreg_sol_{host}") or None,
                "instrument_types":
                    st.session_state.get(f"srcreg_inst_{host}") or None,
                "sample_url": st.session_state.get(f"srcreg_url_{host}") or None,
            }
            if (db.get("source_class") != fields["source_class"]
                    or db.get("classification") != fields["classification"]
                    or (db.get("status") or "pending") != fields["status"]
                    or db.get("access_model") != fields["access_model"]
                    or db.get("ingestion_method") != fields["ingestion_method"]
                    or (db.get("solicitation_types") or []) != (fields["solicitation_types"] or [])
                    or (db.get("instrument_types") or []) != (fields["instrument_types"] or [])
                    or (db.get("sample_url") or "") != (fields["sample_url"] or "")):
                if source_registry.update_row(host, fields, by=email):
                    n += 1
        _flash_set("srcreg", f"✅ Saved {n} host(s) to the database.")
        st.rerun()

    b2, _sp = st.columns([2.2, 4])

    if b2.button("⬆ Push confirmed primaries → Sources catalogue",
                 key="srcreg_push",
                 help="Insert confirmed-primary hosts into donor_sources "
                      "(de-duped by host)."):
        prim = [r["host"] for r in rows
                if r.get("classification") == "primary"
                and (r.get("status") or "").lower() == "confirmed"]
        res = source_registry.push_primaries(prim, by=email)
        if res.get("error"):
            st.error(f"Push failed: {res['error']}")
        else:
            st.success(
                f"Catalogue synced: {len(res['added'])} added · "
                f"{len(res.get('updated', []))} updated · "
                f"{len(res['skipped'])} skipped (not confirmed primary).")
            if res["added"]:
                st.caption("Added: " + ", ".join(res["added"][:20]))
            if res.get("updated"):
                st.caption("Updated: " + ", ".join(res["updated"][:20]))

    # Delete via row-select (no Del column) → button appears once hosts picked.
    st.divider()
    dsel = st.multiselect("Select host(s) to delete", [r["host"] for r in items],
                          key="srcreg_delsel")
    if dsel and st.button(f"🗑 Delete {len(dsel)} host(s)", key="srcreg_del"):
        source_registry.delete_hosts(dsel)
        _flash_set("srcreg", f"🗑 Deleted {len(dsel)} host(s).")
        st.rerun()


def render_verification(user: dict[str, Any], sb=None) -> None:
    st.subheader("Verification & feedback")
    st.caption(
        "Confirm or counter what the scanner did — every verdict grows and "
        "cleans the training set for the learning engine.")
    t_rej, t_ins, t_src = st.tabs(
        ["🚫 Auto-rejected (gate quality)", "✅ Inserted RFPs (model labels)",
         "🗂 Source registry (aggregator vs primary)"])
    with t_rej:
        _render_reject_log(user)
    with t_ins:
        _render_inserted_feedback(user)
    with t_src:
        _render_source_registry(user)
