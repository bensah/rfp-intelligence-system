"""Feature extraction for the decision model (ML Phase 3).

ONE place that turns a candidate / `rfp_submissions` row into the named feature
dict the decision model trains and predicts on. Captured into
`scan_decisions.features` (jsonb) at decision time so the model can train
WITHOUT re-crawling (migration 027's intent), and reconstructable from a stored
row for backfill.

Design choices (see the Phase 3 spec):
  * Target is the HUMAN decision (Decline < Park < Proceed). These are the
    inputs only.
  * `auto_recommendation` is deliberately EXCLUDED — it's a deterministic
    function of the criteria, so feeding it would teach the model to echo the
    rule instead of learning a better mapping.
  * Criterion responses are NORMALISED to an ordinal score (2 / 1 / 0, None for
    "Not sure" / unscored) via core.scorer.criterion_score, so legacy
    True/Partial/False AND the new MS-Form rich labels ("Yes, via a partner",
    "Strong - priorities + experience", "High", …) feed the model on ONE scale
    (Bernard 2026-06-17: Yes=2, Partial=1, No=0). Other features keep readable
    values (geo_strength='strong', days_to_deadline=45).

Best-effort: never raises into a scan or a UI save — returns whatever it could
compute, with missing pieces left as None.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

from core.scorer import criterion_score

log = logging.getLogger(__name__)

# The model's parameter order (stable). The trainer reads this; don't reorder
# without bumping the stored model's feature_order.
_CRITERION_FEATURES = (
    "qualification", "strategic_fit", "capacity",
    "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness",
    "bid_effort",
)

# COMPONENT features — the sub-factors behind each criterion (the same components
# wired in core.criteria_derive.factor_breakdown). Each is the component's numeric
# score: 1.0 / 0.5 / 0.0 when ACTIVE (the call/donor imposes it or a proxy applies),
# None when "Not sure" (not stated → excluded). Captured so the model can learn from
# the FINE-GRAINED signals, not just the rolled-up 2/1/0 criterion labels. Keys are
# globally unique across criteria; feature name = "cmp_<component-key>". STABLE ORDER —
# append only (the model's feature_names contract); never reorder/remove.
_COMPONENT_KEYS: tuple[str, ...] = (
    # MUST-1 qualification
    "applicant_type", "entity_type", "donor_hq_country", "local_registration",
    "individual_pi", "prior_beneficiary",
    # MUST-2 strategic_fit
    "strat_fitness",
    # MUST-3 capacity
    "org_stage", "budget_ceiling", "grant_ceiling", "experience", "award_absorption",
    # MUST-4 geographic_fit
    "geo_presence",
    # MUST-5 cofinancing & compliance
    "cofinance", "audited_financials", "audit_report", "sam_uei", "tax_exempt",
    "safeguarding", "partner_mou", "govt_mou", "govt_endorsement", "local_board",
    "authorized_signatory", "partnership", "platform_reg", "route",
    # PREFER-6 funding_quality
    "fq_floor", "fq_ceiling", "fq_value", "fq_duration",
    # PREFER-7 funder_relationship
    "rel_grantee", "rel_contact",
    # PREFER-8 competitiveness
    "comp_track", "comp_age", "comp_portal", "comp_grassroots", "comp_multi", "comp_hq",
    # PREFER-9 bid_effort
    "bid_time", "bid_team",
)
COMPONENT_FEATURE_NAMES: tuple[str, ...] = tuple(f"cmp_{k}" for k in _COMPONENT_KEYS)

FEATURE_ORDER: tuple[str, ...] = _CRITERION_FEATURES + (
    "alignment_score", "geo_strength", "has_deadline", "days_to_deadline",
    "decline_flags_present", "funder_is_usg", "log_value_usd", "channel",
    "text_len",
) + COMPONENT_FEATURE_NAMES

def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_geo_strength(row: dict, policies: dict | None) -> str | None:
    try:
        from core.auto_scorer import _geo_strength
        return _geo_strength(row, policies or {})
    except Exception:
        return None


def _funder_is_usg(funder: str | None, policies: dict | None) -> bool | None:
    if not funder:
        return None
    try:
        rules = (policies or {}).get("scoring_rules", {}) or {}
        usg = rules.get("usg_funders") or {}
        pats = [p.lower() for p in (usg.get("patterns") or []) if p]
        fl = funder.lower()
        return any(p in fl for p in pats)
    except Exception:
        return None


def _log_value_usd(value: Any, currency: Any) -> float | None:
    try:
        v = float(str(value).replace(",", "").replace("$", "")) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        from core.dropdowns import usd_rate
        usd = v * float(usd_rate(currency or "USD"))
    except Exception:
        usd = v
    return round(math.log1p(max(0.0, usd)), 4)


def _resolve_org() -> tuple[dict, dict]:
    """Best-effort (org_profile, org_settings) for component computation."""
    prof, sett = {}, {}
    try:
        from core.org_profile import get_profile
        prof = get_profile() or {}
    except Exception:
        pass
    try:
        from core.settings import get_org
        sett = get_org() or {}
    except Exception:
        pass
    return prof, sett


def _resolve_donor(funder: Any) -> dict | None:
    """Best-effort donor_intel row by funder name (for component context)."""
    f = (str(funder or "").strip())
    if not f:
        return None
    try:
        from db.supabase_client import get_client
        rows = (get_client().table("donor_intel").select("*")
                .ilike("donor", f).limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception:
        return None


def _rfp_compliance(row: dict) -> dict | None:
    """The RFP's own stored compliance_flags (JSON text or dict), if any."""
    cf = row.get("call_compliance_flags")
    if isinstance(cf, dict):
        return cf
    if isinstance(cf, str) and cf.strip():
        try:
            import json
            v = json.loads(cf)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _component_scores(row: dict, org: dict, donor: dict | None,
                      org_settings: dict, rfp_compliance: dict | None) -> dict:
    """Per-component numeric scores via criteria_derive.factor_breakdown — keyed
    cmp_<component-key>. ACTIVE component → its score (1/0.5/0); inactive ('Not
    sure') → None. PREFER components use met (True/False/None) → 1/0/None."""
    out: dict[str, Any] = {n: None for n in COMPONENT_FEATURE_NAMES}
    try:
        from core import criteria_derive as cd
        bd = cd.factor_breakdown(row, org or {}, donor, org_settings or {}, rfp_compliance)
    except Exception as exc:
        log.debug("features._component_scores failed: %s", exc)
        return out
    for _crit, items in (bd or {}).items():
        for it in items or []:
            key = it.get("key")
            name = f"cmp_{key}"
            if name not in out:
                continue
            if not it.get("active", True):
                out[name] = None                      # 'Not sure' — excluded
            elif it.get("score") is not None:
                out[name] = float(it["score"])        # _qfactor components (0/0.5/1)
            else:
                met = it.get("met")                   # _factor (PREFER) components
                out[name] = (1.0 if met is True else 0.0 if met is False else None)
    return out


def _channel(row: dict) -> str:
    origin = (row.get("_source_origin") or row.get("source") or "").lower()
    link = (row.get("opportunity_link") or "").lower()
    if "grants.gov" in origin or "grants.gov" in link or "(kw=" in origin:
        return "grants.gov"
    if row.get("_resolved_from_aggregator") or row.get("_aggregator_link"):
        return "aggregator-resolved"
    if "rss" in origin or link.endswith((".xml", ".rss")):
        return "rss"
    return "web"


def extract(row: dict, policies: dict | None = None, *,
            asof: date | None = None, org: dict | None = None,
            donor: dict | None = None, org_settings: dict | None = None) -> dict[str, Any]:
    """Build the named feature dict for a candidate / rfp_submissions row.

    `asof` dates the deadline distance (default today; backfill passes the
    decision's own date). `policies` is loaded lazily if omitted. `org` / `donor`
    / `org_settings` give the component sub-factors their context — callers that
    already have them (scan, review save) should pass them; otherwise they're
    best-effort resolved, and ONLY for scored rows (pre-scoring rejects skip the
    component lookups so bulk reject-logging stays cheap)."""
    if not isinstance(row, dict):
        return {}
    if policies is None:
        try:
            from core.policies import get_policies
            policies = get_policies()
        except Exception:
            policies = {}
    if asof is None:
        asof = date.today()

    feats: dict[str, Any] = {}
    for k in _CRITERION_FEATURES:
        feats[k] = criterion_score(row.get(k))   # 2 / 1 / 0 / None

    sc = row.get("alignment_score")
    try:
        feats["alignment_score"] = float(sc) if sc not in (None, "") else None
    except (TypeError, ValueError):
        feats["alignment_score"] = None

    feats["geo_strength"] = _safe_geo_strength(row, policies)

    dl = _parse_date(row.get("call_submission_deadline"))
    feats["has_deadline"] = bool(dl)
    feats["days_to_deadline"] = (dl - asof).days if dl else None

    df = row.get("decline_flags_present")
    feats["decline_flags_present"] = bool(df) if df is not None else None

    feats["funder_is_usg"] = _funder_is_usg(row.get("funding_agency"), policies)
    feats["log_value_usd"] = _log_value_usd(
        row.get("call_award_value"), row.get("currency"))
    feats["channel"] = _channel(row)
    feats["text_len"] = len((row.get("brief_description") or "").strip())

    # ---- Component sub-factor features (the same ones wired under each criterion).
    # Compute when we have org context OR the row is scored (a real decision). For
    # pre-scoring system-rejects (all criteria None and no org passed) leave them
    # None — they're judged on geo/deadline/channel, not org-specific components.
    scored = any(feats[k] is not None for k in _CRITERION_FEATURES)
    if org is None and (scored or donor is not None):
        org, org_settings = _resolve_org()
    if org is not None:
        if donor is None and row.get("funding_agency"):
            donor = _resolve_donor(row.get("funding_agency"))
        feats.update(_component_scores(
            row, org, donor, org_settings or {}, _rfp_compliance(row)))
    else:
        for _n in COMPONENT_FEATURE_NAMES:
            feats[_n] = None
    return feats
