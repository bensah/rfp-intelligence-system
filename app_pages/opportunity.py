"""One opportunity, on its own page — /opportunity?uid=<uid>.

Every title in the Live Opportunity Feed used to link to the bare `/pipelines` page: the
same destination for all of them, so the click told you nothing and you still had to hunt
for the row. And the Featured card ranks the SHARED catalog, whose calls are not in
`rfp_submissions` at all — there was no page that could show one.

This page shows the FULL extracted detail for either store (see
core.opportunity_detail), and for a catalog call it offers **Track this opportunity**,
which runs it through the same objective scorer the scan uses and lands it in the pipeline
with derived criteria, an alignment score and a recommendation — classified against the
eligibility criteria, awaiting a human decision.
"""
from __future__ import annotations

import html as _html
from urllib.parse import quote as _quote

import streamlit as st

from core import opportunity_detail as _od
from db.supabase_client import get_client

user = st.session_state.get("app_user") or {}

_uid = (st.query_params.get("uid") or "").strip()


def _pipeline_reader(uid: str):
    # Tenant-scoped: a reviewer must only ever see their own entity's screened rows.
    rows = (get_client().table("rfp_submissions").select(_od.PIPELINE_FIELDS)
            .eq("uid", uid).limit(1).execute().data or [])
    return rows[0] if rows else None


def _catalog_reader(uid: str):
    # NOT tenant-scoped, by design: the catalog is the shared pool the Featured card ranks,
    # which is exactly what makes a screening miss recoverable. It holds no tenant data.
    from db.supabase_client import service_client
    rows = (service_client().table("extracted_solicitations").select(_od.CATALOG_FIELDS)
            .eq("uid", uid).limit(1).execute().data or [])
    return rows[0] if rows else None


if not _uid:
    st.title("Opportunity")
    st.info("No opportunity selected. Open one from the **Live Opportunity Feed** on "
            "Home or Pipelines.")
    st.markdown("[📚 Go to Pipelines](/pipelines)")
    st.stop()

try:
    _res = _od.load(_uid, pipeline_reader=_pipeline_reader,
                    catalog_reader=_catalog_reader)
except Exception as exc:
    st.error(f"Couldn't load `{_uid}` right now: {exc}")
    st.stop()

_kind, _row = _res["kind"], _res["row"]
if not _kind:
    st.title("Opportunity")
    st.warning(f"Couldn't find an opportunity with uid `{_uid}`. It may have been "
               "deleted, or it belongs to another entity.")
    st.markdown("[📚 Back to Pipelines](/pipelines)")
    st.stop()


def _esc(v) -> str:
    # display_value untangles the jsonb columns (real list / JSON-encoded string /
    # double-encoded list) so a raw Python repr never reaches the page. "$" is neutralised
    # so Streamlit's markdown doesn't render a money value as a LaTeX block.
    return _html.escape(_od.display_value(v)).replace("$", "&#36;")


# ── Header ───────────────────────────────────────────────────────────────────
st.title(_od.title_of(_kind, _row))
_funder = _row.get("funder_name") or _row.get("funding_agency") or "—"
st.caption(f"UID `{_uid}` · Funder: **{_esc(_funder)}** · "
           + ("in your pipeline" if _kind == _od.KIND_PIPELINE
              else "from the shared catalogue — not yet screened for your entity"))

_link = _od.link_of(_kind, _row)
if _link:
    st.markdown(f"[Open the call ↗]({_link})")

# ── Action row ───────────────────────────────────────────────────────────────
if _kind == _od.KIND_PIPELINE:
    _a1, _a2, _a3 = st.columns([1.6, 1.3, 3])
    # A markdown link, not st.page_link: page_link cannot carry a query string, and
    # /pipelines WITHOUT the uid lands on the tab list where the reviewer has to hunt for
    # the row again — which is the complaint that started this. `?uid=` opens the focused
    # single-RFP Review view (already implemented in app_pages/pipelines.py).
    _a1.markdown(f"#### [✏️ Open in Review](/pipelines?uid={_quote(_uid)})")
    _a1.caption("Score the criteria and record the team decision.")
    if _od.is_screened(_kind, _row):
        _a2.metric("Bid Strength", f"{float(_row.get('alignment_score') or 0):.0f}/100")
else:
    st.markdown("")
    _t1, _t2 = st.columns([1.6, 4])
    # Tracking mints a NEW uid, so this page keeps resolving to the catalogue row
    # afterwards. Look the tenant's own row up by call URL so a revisit says "already
    # tracked" instead of offering the button again.
    _already = st.session_state.get(f"_opp_tracked_{_uid}")
    if not _already:
        try:
            _mine = (get_client().table("rfp_submissions")
                     .select("uid,opportunity_link").limit(2000).execute().data or [])
            _already = _od.tracked_uid(_row, _mine)
        except Exception:
            _already = None
    if _already:
        _t1.success("✓ Tracked")
        _t2.caption(f"Now in your pipeline as `{_already}` — open **Pipelines → Review** "
                    "to score it and record a decision.")
    elif _t1.button("➕ Track this opportunity", type="primary", width='stretch',
                    help="Run it through the same scorer the scan uses and add it to your "
                         "pipeline, scored and classified against the eligibility "
                         "criteria, awaiting your decision."):
        from core import found_loader
        _cand = _od.to_candidate(_row)
        # provenance is NOT "search": that path re-runs the eligibility gate, and this call
        # already came from the crawl + extraction. A Featured call may have MISSED this
        # tenant's soft gate, which is the whole reason it is offered here — re-gating it
        # would refuse exactly the recovery the card exists to make possible.
        _res2 = found_loader.load_candidate(_cand, user, provenance="opportunity-page")
        if _res2.get("ok"):
            st.session_state[f"_opp_tracked_{_uid}"] = _res2["uid"]
            st.success(f"Tracked as `{_res2['uid']}` — system recommendation: "
                       f"**{_res2.get('reason') or 'scored'}**.")
            st.cache_data.clear()          # the rail + pipeline lists are cached
            st.rerun()
        elif _res2.get("skipped"):
            st.info("Already in your pipeline — not added twice. Open **Pipelines → "
                    "Review** to find it.")
        else:
            st.warning(f"Couldn't track it: {_res2.get('reason') or 'unknown error'}")
    else:
        _t2.caption("Tracking scores it against your eligibility criteria and puts it in "
                    "your pipeline for review. Nothing is decided for you.")

st.divider()

# ── Narrative ────────────────────────────────────────────────────────────────
_narr = _od.narrative_of(_kind, _row)
if _kind == _od.KIND_PIPELINE:
    # Same display guard the Review card uses: never show a raw attachment/legalese dump.
    try:
        from core.records import clean_brief as _clean_brief
        _narr = _clean_brief(_row.get("brief_description"), _row.get("raw_text")) or ""
    except Exception:
        pass
if _narr:
    st.markdown("#### What this call is")
    st.markdown(
        f"<div style='background:#fff;border:1px solid #e6e6e6;border-radius:10px;"
        f"padding:14px 16px;color:#333;line-height:1.6'>"
        f"{_esc(_narr).replace(chr(10), '<br>')}</div>", unsafe_allow_html=True)
    st.markdown("")

# ── Detail sections ──────────────────────────────────────────────────────────
_secs = _od.sections(_kind, _row)
if not _secs:
    st.info("No further detail was extracted for this call.")
else:
    _cols = st.columns(2)
    for _i, (_title, _fields) in enumerate(_secs):
        with _cols[_i % 2]:
            with st.container(border=True):
                st.markdown(
                    f"<div style='font-weight:700;color:#16734a;margin-bottom:8px;"
                    f"font-size:0.95rem'>{_esc(_title)}</div>", unsafe_allow_html=True)
                for _label, _val in _fields:
                    st.markdown(
                        f"<div style='margin-bottom:9px'>"
                        f"<div style='font-weight:600;color:#243524;font-size:0.85rem'>"
                        f"{_esc(_label)}</div>"
                        f"<div style='color:#5a5a5a;font-size:0.9rem'>"
                        f"{_esc(_val)}</div></div>", unsafe_allow_html=True)

# ── Attachments / resource links (catalogue only) ────────────────────────────
if _kind == _od.KIND_CATALOG:
    for _fld, _hdr in (("resource_links", "Resource links"),
                       ("attachments", "Attachments")):
        _vals = _row.get(_fld)
        if isinstance(_vals, str):
            _vals = [_vals] if _vals.strip() else []
        if _vals:
            st.markdown(f"#### {_hdr}")
            for _v in _vals:
                _u = str(_v).strip()
                if _u.startswith("http"):
                    st.markdown(f"- [{_esc(_u)}]({_u})")
                elif _u:
                    st.markdown(f"- {_esc(_u)}")
