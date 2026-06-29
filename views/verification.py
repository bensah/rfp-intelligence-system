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
from core.records import clean_df
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
        df = clean_df(pd.DataFrame(recs))
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


# Opportunity-type vocabulary — full Taadom scope (current CHAI use is the first
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
                    # Default the Solicitation dropdown to a human label if one
                    # exists, else the auto-detected type stored on the row
                    # (scan-time: 'Other' for not-an-rfp, else NOFO/RFP/CFP/… from
                    # title+body). Saves the reviewer pre-clicking the obvious one.
                    dt = db_types.get(link) or r.get("solicitation_type") or ""
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
                            "call_submission_deadline": r.get("call_submission_deadline"),
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
                    "opportunity_link,funding_agency,submission_deadline,"
                    "solicitation_type")
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
                    ("Deadline", lambda r: str(r.get("call_submission_deadline") or ""))],
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


@st.dialog("Source detail", width="large")
def _srcreg_view_dialog(r: dict) -> None:
    """Polished read-only view of one registry source (pop-up)."""
    host = r.get("host") or ""
    name = r.get("donor_name") or host
    ver = (r.get("status") or "").lower() == "confirmed"
    pushed = bool(r.get("in_catalogue"))

    def _pill(txt: str, on: bool) -> str:
        bg, fg = ("#dcfce7", "#15803d") if on else ("#e2e8f0", "#475569")
        return (f"<span style='background:{bg};color:{fg};padding:3px 11px;"
                f"border-radius:20px;font-size:.78rem;font-weight:700'>{html.escape(txt)}</span>")
    st.markdown(
        "<div style='background:linear-gradient(95deg,#0f766e,#0d9488);color:#fff;"
        "padding:14px 18px;border-radius:11px'>"
        f"<div style='font-size:1.2rem;font-weight:700'>{html.escape(str(name))}</div>"
        f"<div style='opacity:.9;font-size:.82rem;margin-top:2px;font-family:monospace'>{html.escape(str(host))}</div>"
        "<div style='margin-top:9px;display:flex;gap:7px;flex-wrap:wrap'>"
        + _pill('✅ Verified' if ver else '— Unverified', ver)
        + _pill('✅ Pushed to Catalogue' if pushed else '— Not pushed', pushed)
        + f"<span style='background:rgba(255,255,255,.22);padding:3px 11px;border-radius:20px;"
        f"font-size:.78rem'>{html.escape(_src_class_of(r))}</span></div></div>",
        unsafe_allow_html=True)
    lu = r.get("listings_url") or ""
    if lu:
        st.markdown(f"**🔗 Listings URL**  \n[{lu}]({lu})")
    g1, g2 = st.columns(2)
    g1.markdown(f"**Code**  \n{r.get('donor_code') or '—'}")
    g1.markdown(f"**Access**  \n{r.get('access_model') or '—'}")
    g1.markdown(f"**Method**  \n{_norm_method(r.get('ingestion_method'))}")
    g1.markdown(f"**Hits**  \n{r.get('hits') or 0}")
    g2.markdown(f"**Solicitation types**  \n{', '.join(r.get('solicitation_types') or []) or '—'}")
    g2.markdown(f"**Instrument types**  \n{', '.join(r.get('instrument_types') or []) or '—'}")
    g2.markdown(f"**Verified by**  \n{r.get('verified_by') or '—'}")
    g2.markdown(f"**Verified at**  \n{str(r.get('verified_at') or '—')[:19]}")
    if r.get("sample_url"):
        st.caption(f"Sample opportunity: {r.get('sample_url')}")
    if r.get("notes"):
        st.markdown(f"**Notes**  \n{r.get('notes')}")


@st.dialog("Edit source", width="large")
def _srcreg_edit_dialog(r: dict, email) -> None:
    """Edit ONE registry source. 'Verify & Save' also marks it confirmed."""
    host = r.get("host") or ""
    st.markdown(f"**{r.get('donor_name') or host}** · `{host}`")
    name = st.text_input("Source name", value=r.get("donor_name") or "")
    code = st.text_input("Code", value=r.get("donor_code") or "")
    lu = st.text_input("Listings URL", value=r.get("listings_url") or "",
                       help="The page or API that LISTS this source's opportunities "
                            "(what the scanner ingests) — the main field to keep accurate.")
    su = st.text_input("Sample opportunity URL (optional)", value=r.get("sample_url") or "",
                       help="An example of ONE opportunity — optional; not the listings page.")
    c1, c2 = st.columns(2)
    _sc0, _vf0 = _src_class_of(r), _verif_of(r)
    sc = c1.selectbox("Source class", _SRC_OPTS,
                      index=_SRC_OPTS.index(_sc0) if _sc0 in _SRC_OPTS else 0)
    vf = c2.selectbox("Verification", _VERIF_OPTS,
                      index=_VERIF_OPTS.index(_vf0) if _vf0 in _VERIF_OPTS else 0)
    c3, c4 = st.columns(2)
    _ac0, _me0 = (r.get("access_model") or "Unknown"), _norm_method(r.get("ingestion_method"))
    ac = c3.selectbox("Access", _ACCESS_OPTS,
                      index=_ACCESS_OPTS.index(_ac0) if _ac0 in _ACCESS_OPTS else 0)
    me = c4.selectbox("Method", _METHOD_OPTS,
                      index=_METHOD_OPTS.index(_me0) if _me0 in _METHOD_OPTS else 2)
    sol = st.multiselect("Solicitation type(s)", SOLICITATION_TYPES,
                         default=[t for t in (r.get("solicitation_types") or []) if t in SOLICITATION_TYPES])
    inst = st.multiselect("Instrument type(s)", INSTRUMENT_TYPES,
                          default=[t for t in (r.get("instrument_types") or []) if t in INSTRUMENT_TYPES])
    notes = st.text_input("Notes", value=r.get("notes") or "")

    def _save(verify: bool) -> None:
        f = {
            "donor_name": name or None, "donor_code": code or None,
            "listings_url": lu or None, "sample_url": su or None,
            "source_class": None if sc in ("Unknown", "") else sc,
            "classification": _derive_class(sc),
            "access_model": ac or None, "ingestion_method": me or None,
            "solicitation_types": sol or None, "instrument_types": inst or None,
            "notes": notes or None,
            "status": "confirmed" if (verify or vf in _VERIFIED) else "pending",
        }
        if source_registry.update_row(host, f, by=email):
            _flash_set("srcreg", ("✅ Verified & saved " if verify else "💾 Saved ") + host)
            st.rerun()
        else:
            st.error("Save failed — the DB rejected the write (column/RLS). Nothing changed.")
    bb1, bb2 = st.columns(2)
    if bb1.button("✓ Verify & Save", type="primary", key="srcreg_edit_vs"):
        _save(verify=True)
    if bb2.button("💾 Save (keep status)", key="srcreg_edit_save"):
        _save(verify=False)


def _render_source_registry(user: dict) -> None:
    import pandas as pd
    email = (user or {}).get("email")
    _flash_show("srcreg")
    st.caption(
        "Every host the scanner meets, with the source-catalogue taxonomy. Tick a "
        "row to **👁 View** or **✏ Edit** it (Edit's *Verify & Save* marks it "
        "confirmed); tick several for bulk **Push / Delete / Verify**. **Listings "
        "URL** = the page/API the scanner ingests (the key field). Saved values "
        "persist in the database. Bulk-edit many at once via the CSV below.")

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
                    "listings_url": new_host if "/" in (new_host or "") else None,
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

    f1, f2, f3 = st.columns([3, 1, 1])
    classes = ["All"] + sorted({_eff(r, "source_class", _src_class_of) for r in rows})
    pick = f1.selectbox("Filter by source class", classes, key="srcreg_cls")
    only_unv = f2.checkbox("Only unverified", value=False, key="srcreg_pend",
                           help="Hide rows already confirmed — focus on what's left to verify.")
    only_unpushed = f3.checkbox("Only not-pushed", value=False, key="srcreg_unpushed",
                                help="Hide rows already pushed to the Catalogue.")

    items = []
    for r in rows:
        if pick != "All" and _eff(r, "source_class", _src_class_of) != pick:
            continue
        if only_unv and (r.get("status") or "").lower() == "confirmed":
            continue
        if only_unpushed and r.get("in_catalogue"):
            continue
        items.append(r)

    # ── ONE clean selectable table — leads with the full Listings URL + Verified /
    # Pushed markers. Tick row(s) → bulk Push / Delete / Verify; tick ONE → View /
    # Edit pop-up. (Editing many at once → the CSV expander below.)
    _seldf = pd.DataFrame([{
        "Source name": (r.get("donor_name") or r.get("host"))[:42],
        "Listings URL": (r.get("listings_url") or r.get("sample_url")
                         or f"https://{r.get('host')}/"),
        "Class": _src_class_of(r),
        "Verified": "✅" if (r.get("status") or "").lower() == "confirmed" else "—",
        "Pushed": "✅" if r.get("in_catalogue") else "—",
        "Method": _norm_method(r.get("ingestion_method")),
        "Solicitation": "; ".join(r.get("solicitation_types") or []),
        "Instrument": "; ".join(r.get("instrument_types") or []),
        "Hits": int(r.get("hits") or 0),
    } for r in items])
    st.caption(f"**{len(items)}** hosts · **✅ Verified** = confirmed · **✅ Pushed** = in the "
               "Catalogue. Tick a row → View / Edit / Push / Delete / Verify below.")
    _ev = st.dataframe(
        _seldf, hide_index=True, width='stretch', selection_mode="multi-row",
        on_select="rerun", key="srcreg_seltable",
        column_config={
            "Listings URL": st.column_config.LinkColumn(width="large"),
            "Verified": st.column_config.TextColumn(width="small"),
            "Pushed": st.column_config.TextColumn(width="small"),
            "Hits": st.column_config.NumberColumn(width="small")})
    _selrows = (_ev.selection.rows if _ev and getattr(_ev, "selection", None) else [])
    _sel = [items[i] for i in _selrows if i < len(items)]
    _selhosts = [r["host"] for r in _sel]
    _one = _sel[0] if len(_sel) == 1 else None
    bv, be, bp, bd, bm = st.columns(5)
    if bv.button("👁 View", key="srcreg_view", disabled=_one is None, width='stretch',
                 help="Select ONE row to view its full detail."):
        _srcreg_view_dialog(_one)
    if be.button("✏ Edit", key="srcreg_edit", disabled=_one is None, width='stretch',
                 help="Select ONE row to edit it ('Verify & Save' marks it verified)."):
        _srcreg_edit_dialog(_one, email)
    if bp.button(f"⬆ Push ({len(_selhosts)})", key="srcreg_push_sel", type="primary",
                 disabled=not _selhosts, width='stretch',
                 help="Push selected CONFIRMED-PRIMARY hosts to the Catalogue "
                      "(deduped; non-confirmed-primary rows skipped)."):
        res = source_registry.push_primaries(_selhosts, by=email)
        _sk = res.get("skipped") or []
        _flash_set("srcreg", f"✅ Catalogue: {len(res['added'])} added · "
                   f"{len(res.get('updated', []))} updated"
                   + (f" · {len(_sk)} skipped (not confirmed primary)" if _sk else ""))
        st.rerun()
    if bd.button(f"🗑 Delete ({len(_selhosts)})", key="srcreg_del_sel",
                 disabled=not _selhosts, width='stretch'):
        source_registry.delete_hosts(_selhosts)
        _flash_set("srcreg", f"🗑 Deleted {len(_selhosts)} host(s).")
        st.rerun()
    if bm.button(f"✓ Verify ({len(_selhosts)})", key="srcreg_ver_sel",
                 disabled=not _selhosts, width='stretch',
                 help="Mark selected hosts confirmed (verified)."):
        _n = sum(1 for h in _selhosts
                 if source_registry.update_row(h, {"status": "confirmed"}, by=email))
        _flash_set("srcreg", f"✅ Marked {_n} host(s) verified.")
        st.rerun()

    # ── Reconcile registry ↔ Catalogue (verified sources only). Pushes every
    # confirmed-primary host (deduped by host) and recomputes the Pushed markers
    # both ways, so the two tables stay in sync.
    _sy, _ = st.columns([2.6, 5])
    if _sy.button("🔄 Sync all verified → Catalogue", key="srcreg_sync",
                  help="Push every confirmed-primary host to the donor_sources Catalogue "
                       "(deduped) and refresh the Pushed markers both ways."):
        prim = [r["host"] for r in rows
                if r.get("classification") == "primary"
                and (r.get("status") or "").lower() == "confirmed"]
        res = source_registry.push_primaries(prim, by=email)
        rec = source_registry.reconcile_in_catalogue()
        _flash_set("srcreg", f"🔄 Synced {len(prim)} verified primaries → "
                   f"{len(res.get('added', []))} added · {len(res.get('updated', []))} updated · "
                   f"markers +{rec.get('marked', 0)}/-{rec.get('cleared', 0)}"
                   + (f" · ⚠ push error: {res['error']}" if res.get("error") else ""))
        st.rerun()

    # ── Optional bulk power-edit (many rows at once) via CSV/Excel round-trip.
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
        if vals.get("Listings URL"):
            f["listings_url"] = vals["Listings URL"]

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
                  ("Listings URL", None, lambda r: r.get("listings_url") or ""),
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
        aliases={"Source Name": ["Donor"], "Listings URL": ["Host", "Sample", "Listings"],
                 "Method": ["Ingestion"]})


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
