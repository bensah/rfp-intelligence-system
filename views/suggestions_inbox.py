"""Developer review inbox for the Phase B suggestion queue (Settings → Suggestions).

Lists PENDING proposals to the shared central resources (donor_intel + donor_sources) from
across all tenants, shows a field-level diff, and lets a developer-tenant Super User
Approve → auto-apply or Reject. Gated hard on permissions.is_developer_super — a
non-developer never reaches the list (core.suggestions.list_pending returns [] for them,
and every mutating call re-checks the gate).
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from core import permissions, suggestions


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✓ yes" if v else "✗ no"
    s = str(v)
    return (s[:140] + "…") if len(s) > 140 else (s or "—")


def _diff_rows(sug: dict) -> pd.DataFrame:
    diff = sug.get("proposed_diff") or {}
    base = sug.get("base_snapshot") or {}
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except Exception:
            diff = {}
    if isinstance(base, str):
        try:
            base = json.loads(base)
        except Exception:
            base = {}
    rows = [{"Field": k, "Current": _fmt(base.get(k)), "Proposed": _fmt(v)}
            for k, v in diff.items()]
    return pd.DataFrame(rows or [{"Field": "—", "Current": "—", "Proposed": "—"}])


def render_suggestions_inbox(user: dict, sb) -> None:
    st.subheader("Suggestions — review & apply")
    # Hard gate (defense in depth; the tab is only shown to _dev_super in admin.py).
    if not permissions.is_developer_super(user):
        st.info(
            "🔒 **Suggestions review is a developer task.** Proposed changes to the shared "
            "donor mapping & sources catalog are reviewed by a developer-tenant Super "
            "User.")
        return

    st.caption(
        "Proposals from non-developer users to the shared **donor mapping** and **sources "
        "catalog**. Approve to auto-apply the change, or reject. Applied changes take effect "
        "immediately across all tenants.")

    try:
        pending = suggestions.list_pending(user)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Couldn't load suggestions (did you run migration 080?): {exc}")
        return

    if not pending:
        st.success("✓ No suggestions awaiting review.")
        return

    st.markdown(f"**📬 {len(pending)} suggestion(s) pending review**")
    _rows = []
    for s in pending:
        _rt = "Donor mapping" if s.get("resource_type") == "donor_intel" else "Source catalog"
        _kind = "➕ Add" if not s.get("target_id") else "✏️ Edit"
        _n = len(s.get("proposed_diff") or {})
        _rows.append({
            "id": s.get("id"),
            "Resource": _rt,
            "Change": _kind,
            "Target": s.get("target_label") or (s.get("target_id") or "(new)"),
            "Fields": _n,
            "By": s.get("proposer_email") or "—",
            "When": (s.get("created_at") or "")[:16].replace("T", " "),
        })
    _df = pd.DataFrame(_rows)
    _sel = st.dataframe(
        _df[["Resource", "Change", "Target", "Fields", "By", "When"]],
        hide_index=True, width="stretch", key="suggestions_table",
        selection_mode="single-row", on_select="rerun",
        column_config={
            "Fields": st.column_config.NumberColumn("Fields", width="small"),
        })

    _picked = (getattr(_sel, "selection", None) or {}).get("rows") or []
    if not _picked:
        st.caption("Select a suggestion to see the proposed change.")
        return

    sug = pending[_picked[0]]
    st.divider()
    _rt = "Donor mapping" if sug.get("resource_type") == "donor_intel" else "Source catalog"
    _kind = "add a new record" if not sug.get("target_id") else "edit an existing record"
    st.markdown(f"**{_rt}** · proposal to {_kind} · "
                f"by `{sug.get('proposer_email') or '—'}`")

    # Re-resolve the REAL target this apply will write — never trust the proposer-typed
    # label. Show the actual current record + warn if the typed label diverges (a spoof
    # signal) or the target no longer exists.
    _tgt = suggestions.resolve_target(sug, user)
    if not _tgt["is_add"]:
        if not _tgt["exists"]:
            st.error(
                f"⚠ **Target no longer exists** (id `{_tgt['target_id']}`). The record was "
                "deleted or renamed since this was proposed — reject it, or ask for a re-file "
                "as an add. Approving will fail.")
        else:
            st.markdown(f"🎯 **Applies to:** `{_tgt['real_label']}`  "
                        f"(`{_tgt['target_id']}`)")
            if _tgt["mismatch"]:
                st.warning(
                    f"⚠ The proposer labelled this **{sug.get('target_label')}**, but the "
                    f"target row is actually **{_tgt['real_label']}**. Verify this is the "
                    "record they meant before approving.")
    if sug.get("rationale"):
        st.info(f"💬 {sug['rationale']}")

    st.dataframe(_diff_rows(sug), hide_index=True, width="stretch",
                 column_config={
                     "Field": st.column_config.TextColumn("Field", width="medium"),
                     "Current": st.column_config.TextColumn("Current value"),
                     "Proposed": st.column_config.TextColumn("Proposed value"),
                 })

    b1, b2, b3, _sp = st.columns([1.6, 1.4, 2, 3])
    _note_key = f"sug_reject_note_{sug.get('id')}"
    if b1.button("✅ Approve & apply", type="primary", width="stretch",
                 key="sug_approve"):
        try:
            res = suggestions.approve(sug["id"], user)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Approve failed: {exc}")
            return
        _msgs = ["✓ Applied."]
        if res.get("stale_fields"):
            _msgs.append("⚠ Applied over newer values in: "
                         + ", ".join(res["stale_fields"]))
        if res.get("invalid_fields"):
            _msgs.append("Skipped unknown field(s): "
                         + ", ".join(res["invalid_fields"]))
        st.session_state["_sug_flash"] = "  \n".join(_msgs)
        st.rerun()
    if b2.button("🗑 Reject", width="stretch", key="sug_reject"):
        try:
            suggestions.reject(sug["id"], user,
                               note=(st.session_state.get(_note_key) or None))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Reject failed: {exc}")
            return
        st.session_state["_sug_flash"] = "Suggestion rejected."
        st.rerun()
    b3.text_input("Reject note (optional)", key=_note_key,
                  label_visibility="collapsed", placeholder="Reason for rejecting…")

    _flash = st.session_state.pop("_sug_flash", None)
    if _flash:
        st.toast(_flash, icon="💡")
