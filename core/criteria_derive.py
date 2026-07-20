"""Objective derivation of the 9 eligibility criteria for the AUTO-SCAN.

Each criterion is computed from ORG × RFP (× DONOR) facts — factoring the FULL
criterion definition — and returns the canonical response LABEL
(core.scorer.CRITERION_RESPONSES). None = "Not sure" (can't determine from the
data → treated as missing, never a fabricated 0). Human review can still
override any value; this only runs in auto_score for the auto-scan path.

Definitions encoded (see the bid/no-bid questionnaire):
  qualification        passed the hard gate ⇒ formally eligible
  strategic_fit        org STRATEGY (org.priority_areas + program_area_ratings)
                       correlated with the DONOR's graded priorities → Strongly
                       aligns / Limited priority / Off-strategy (experience excluded)
  capacity             rfp.estimated_value vs org.largest_grant_usd / annual_budget_usd
  geographic_fit       rfp.call_geographic_scope vs org.org_operating_countries / trusted_partners
  cofinancing          rfp cost-share requirement vs org.cofinancing_capacity
  funding_quality      rfp.estimated_value tiers
  funder_relationship  rfp.funding_agency in org.funder_history
  competitiveness      org TRACK RECORD (org.domains + domain_ratings) on the RFP's
                       exact program area + donor-requirement fit (board/grassroots/…)
  bid_effort           days-to-deadline × org_has_bd_team (core.scorer.bid_effort_label)
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from core import geographies as _geo
from core import program_area_classifier as _pa
from core.partners import clean_portal_url
from core.scorer import bid_effort_label, days_until

_PA_KEYS = set(_pa.PROGRAM_AREA_KEYWORDS)

# Cues that a (US-federal) call welcomes NON-US / international applicants. Absent
# these on a grants.gov call, we assume US-organisations-only.
_INTL_ELIGIBLE_RE = re.compile(
    r"\b(international|foreign|outside the (?:us|u\.s\.|united states)|non-?u\.?s\.?|"
    r"any country|globally|world-?wide|developing countr|"
    r"low-?\s*and\s*middle-?income|lmics?)\b", re.I)

# EXPLICIT US-only / foreign-ineligible statements (e.g. grants.gov 360505:
# "Foreign entities are not eligible…"). A high-precision hard signal that the
# applicant MUST be a US entity — gates geographic fit + MUST-1 registration.
_US_ONLY_RE = re.compile(
    r"\b(foreign (?:entit|organi[sz]ation|applicant)\w*\s+(?:are\s+)?not\s+eligible|"
    r"(?:u\.?s\.?|united states|domestic)\s+(?:based\s+)?(?:entit|organi[sz]ation|"
    r"applicant)\w*\s+only|only\s+(?:u\.?s\.?|united states|domestic)\s+"
    r"(?:entit|organi[sz]ation|applicant)|must be (?:a )?(?:u\.?s\.?|united states|"
    r"domestic)[- ](?:based\s+)?(?:entit|organi[sz]ation))\b", re.I)


def _us_only_call(rfp: dict) -> bool:
    """True when the call EXPLICITLY restricts to US / domestic entities."""
    blob = " ".join(str(rfp.get(x) or "") for x in
                    ("brief_description", "notes", "opportunity_title"))
    return bool(_US_ONLY_RE.search(blob))


def _is_us_federal(rfp: dict) -> bool:
    """True for a US-federal call (grants.gov-sourced or explicitly US-only) with no
    international-eligibility cue — these carry SAM.gov/UEI + US-registration gates."""
    link = f"{rfp.get('opportunity_link') or ''} {rfp.get('source') or ''}".lower()
    intl = _INTL_ELIGIBLE_RE.search(
        f"{rfp.get('brief_description') or ''} {rfp.get('notes') or ''}")
    return (("grants.gov" in link and not intl) or _us_only_call(rfp))


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for x in v:
            s = str(x).strip()
            if s.startswith("["):              # double-encoded: a list element that is
                out.extend(_as_list(s))        # itself a JSON-stringified list
            elif s:
                out.append(s)
        return out
    s = str(v).strip()
    if s.startswith("["):                      # JSON list (donor multi-selects)
        try:
            j = json.loads(s)
            if isinstance(j, list):
                return [str(x).strip() for x in j if str(x).strip()]
        except (ValueError, TypeError):
            pass
    return [x.strip() for x in s.split(",") if x.strip()]


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "").replace("$", "")) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _usd(rfp: dict) -> float | None:
    val = _num(rfp.get("call_award_value"))
    if not val:
        return None
    try:
        from core.dropdowns import usd_rate
        return val * float(usd_rate(rfp.get("currency") or "USD"))
    except Exception:
        return val


def _rfp_program_keys(rfp: dict) -> set[str]:
    """Canonical program-area keys for an RFP — empty if generic ('Health') or
    unclassified. call_domain_areas may store canonical keys, category names, OR bare
    sub-area LABELS (the extractor saves labels like 'SRH', not keys like 'WCH - SRH'),
    so resolve them ALL via program_area_classifier.expand — which returns empty for a
    generic umbrella term ('Health') we can't judge a specific track record against."""
    return _pa.expand(_as_list(rfp.get("call_domain_areas")))


# --- per-criterion derivations (return a CRITERION_RESPONSES label or None) ---
# MUST-2 STRATEGIC FIT helpers (rework 2026-06-28; spec from owner) ------------
def _ratings(v: Any) -> dict:
    """Program-area ratings as a {key: 0-5} dict (donor stores JSON text; org a dict)."""
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        j = json.loads(v)
        return j if isinstance(j, dict) else {}
    except (ValueError, TypeError):
        return {}


def _band(score: Any) -> float:
    """0-5 priority score → band value: 0-1 → 0.0 · 2-3 → 0.5 · 4-5 → 1.0."""
    try:
        s = float(score or 0)
    except (TypeError, ValueError):
        s = 0.0
    return 1.0 if s >= 4 else (0.5 if s >= 2 else 0.0)


# Sub-area LABEL ("Malaria & NTDs") → canonical KEY ("IDs - Malaria & NTDs"). The
# call & donor sides store sub-area LABELS while the org stores full KEYS, so we
# normalise both to the key before matching (else nothing ever overlaps).
_LABEL_TO_KEY = {}
for _k in _PA_KEYS:
    _lbl = str(_pa.subarea_label(_k) or "").strip().lower()
    if _lbl:
        _LABEL_TO_KEY.setdefault(_lbl, _k)


def _canon_theme(a: str) -> str:
    """Canonicalise a theme to its taxonomy KEY: full keys pass through; a sub-area
    label maps to its key; a category name / unknown stays as-is."""
    a = str(a).strip()
    if not a or a in _PA_KEYS:
        return a
    return _LABEL_TO_KEY.get(a.lower(), a)


def _theme_scores(areas: Any, ratings: dict, default: float = 5.0) -> dict:
    """Map taxonomy themes (sub-area labels/keys and/or category names) → {token:
    score}, canonicalising each to its KEY first. Each sub-area ALSO contributes its
    parent CATEGORY token (match at category OR sub-area level). Rating is read by the
    ORIGINAL string (ratings are keyed as stored); missing → `default` (5)."""
    out: dict[str, float] = {}
    for a in _as_list(areas):
        a = str(a).strip()
        if not a:
            continue
        r = ratings.get(a)
        try:
            sc = float(r) if r not in (None, "") else float(default)
        except (TypeError, ValueError):
            sc = float(default)
        token = _canon_theme(a)
        out[token] = max(out.get(token, 0.0), sc)
        cat = _pa.category_full(token)
        if cat and cat != token:              # token is a sub-area key → add its category
            out[cat] = max(out.get(cat, 0.0), sc)
    return out


def _theme_scores_flat(areas: Any, ratings: dict, default: float = 5.0) -> dict:
    """Like _theme_scores but WITHOUT parent-category tokens — the funder's LISTED
    priorities only (canonicalised to keys; Y = how many priorities the funder has)."""
    out: dict[str, float] = {}
    for a in _as_list(areas):
        a = str(a).strip()
        if not a:
            continue
        r = ratings.get(a)
        try:
            sc = float(r) if r not in (None, "") else float(default)
        except (TypeError, ValueError):
            sc = float(default)
        out[_canon_theme(a)] = max(out.get(_canon_theme(a), 0.0), sc)
    return out


def _org_match_score(theme: str, org_tokens: dict) -> float | None:
    """Best org score for a call theme — exact token, else its parent-category token.
    org_tokens (built with _theme_scores) ALSO carries category tokens, so a sub-area
    call matches an org at the category level, and a category call matches an org that
    listed a sub-area under it. None when the org doesn't share the theme."""
    if theme in org_tokens:
        return org_tokens[theme]
    cat = _pa.category_full(theme)
    if cat and cat != theme and cat in org_tokens:
        return org_tokens[cat]
    return None


def _strategic_items(org: dict, rfp: dict, donor: dict | None = None) -> list[dict] | None:
    """One MUST-2 item per FUNDER theme. CALL/DONOR themes (denominator Y) = LLM-detected
    program areas (rfp.program_area, default 5) ∪ donor priority areas (donor ratings,
    default 5) — listed themes only, no category duplication. ORG themes (numerator) =
    strategic priority areas (+ratings), FALLBACK to domains/track-record only when
    strategy is undefined; matched at category OR sub-area level. item.score =
    min(band(org), band(call)) when the org shares it, else 0. None when there are NO
    call themes or NO org themes → 'Not sure'."""
    donor = donor or {}
    call = _theme_scores_flat(rfp.get("call_domain_areas"), {}, 5.0)
    for k, v in _theme_scores_flat(donor.get("donor_priority_areas"),
                                   _ratings(donor.get("donor_priority_ratings")), 5.0).items():
        call[k] = max(call.get(k, 0.0), v)
    if not call:
        return None                           # nothing imposed → Not sure
    if _as_list(org.get("org_priority_areas")):
        org_tokens = _theme_scores(org.get("org_priority_areas"),
                                   _ratings(org.get("org_priority_ratings")), 5.0)
    else:                                     # fallback: track-record domains
        org_tokens = _theme_scores(org.get("org_domain_expertise"),
                                   _ratings(org.get("org_domain_ratings")), 5.0)
    if not org_tokens:
        return None                           # no org strategy/domain data → Not sure
    items: list[dict] = []
    for token in sorted(call):
        oscore = _org_match_score(token, org_tokens)
        score = 0.0 if oscore is None else min(_band(oscore), _band(call[token]))
        label = _pa.subarea_label(token) if " - " in token else token
        it = _qfactor(f"strat::{token}", f"Strategic fit · {label}",
                      active=True, score=score, hard=False)
        it["_term"] = label                   # for the 2-line Review summary
        items.append(it)
    return items


def derive_strategic_fit(org: dict, rfp: dict, donor: dict | None = None) -> str | None:
    """STRATEGIC FIT (MUST-2, rework). Theme overlap (call/donor ∩ org, at category OR
    sub-area level) gates it; the matched theme's priority-band agreement scores it —
    min of the two bands, BEST overlapping theme wins. No call/org theme data →
    'Not sure'; overlap absent or only low-priority → 'Off-strategy' (0)."""
    items = _strategic_items(org, rfp, donor)
    if not items:
        return "Not sure"                      # no call/org theme data → Not sure (Park)
    best = max(i["score"] for i in items)
    return {1.0: "Strongly aligns", 0.5: "Limited priority"}.get(best, "Off-strategy")


def strategic_bid_strength(org: dict, rfp: dict,
                           donor: dict | None = None) -> tuple[int, int, float]:
    """(matched_themes, total_funder_themes, best_band) — MUST-2 is ONE component:
    best_band ∈ {0,0.5,1} is the component score (best matched theme), matched/total is
    the theme-coverage transparency (Bid Strength numerator/denominator)."""
    items = _strategic_items(org, rfp, donor)
    if not items:
        return 0, 0, 0.0                       # no theme data → Not sure (denom 0)
    total = len(items)
    matched = sum(1 for i in items if i["score"] > 0)
    best = max((i["score"] for i in items), default=0.0)
    return matched, total, best


# --- MUST-3 IMPLEMENTATION CAPACITY (rework 2026-06-28; owner spec) -----------
# Composite of up to 4 components: Annual-budget ceiling · Prior-grant ceiling
# (these two MOVED here from MUST-1) · Experience requirement (call-LLM-detected) ·
# Award-absorption (can the org deliver THIS award size). ("Org stage" was retired
# 2026-07-20 as redundant with Experience — see capacity_factors.)
def _org_years(org: dict) -> int | None:
    """Years the org has existed (from founding_year), or None if unknown."""
    from datetime import date as _date
    fy = _num(org.get("org_founding_year"))
    return (_date.today().year - int(fy)) if (fy and fy >= 1900) else None


def _experience_required_years(donor: dict) -> int | None:
    """Required years of experience the CALL signals (LLM-detected, field
    `experience_required`): 'significant'-type language → 10+, subtler → 5+.
    None (not imposed / wants early-stage / any) → component disabled."""
    lvl = str(donor.get("experience_required") or "").strip().lower()
    if lvl in ("significant", "extensive", "strong", "high", "deep", "10", "10+"):
        return 10
    if lvl in ("moderate", "some", "subtle", "relevant", "demonstrated", "5", "5+"):
        return 5
    return None


def _award_absorption_score(org: dict, rfp: dict) -> float | None:
    """Can the org ABSORB this award? Absorption capacity MULTIPLIES with track record
    — an org that has managed a $X grant can credibly handle several × that (NOT a
    division of the envelope). capacity = anchor (largest grant managed, else annual
    budget) × an experience FACTOR (years · stage · #grants). The award the org would
    PURSUE = min(call award size, org funding_target_max) — orgs target a TIER within a
    big pool, they don't take the whole envelope. ask ≤ capacity → comfortably(1) ·
    ≤ 1.5× capacity → stretch(0.5) · else beyond(0). None when there's no amount or no
    capacity facts (→ caller defaults to a pass)."""
    ask = _num(rfp.get("call_award_ceiling")) or _usd(rfp)
    target_max = _num(org.get("org_max_target"))
    if ask and target_max:
        ask = min(ask, target_max)        # pursue a tier within the envelope, not the whole pool
    if not ask:
        return None
    largest = _num(org.get("org_largest_grant"))
    annual = _num(org.get("org_annual_budget"))
    if not largest and not annual:
        return None
    anchor = max(x for x in (largest, annual) if x)
    # Experience MULTIPLIER — absorption grows with track record. A $5M-grant org can
    # credibly manage several times that, more so the longer/larger its history.
    yrs = _org_years(org)
    factor = 2.0
    if (yrs and yrs >= 10) or str(org.get("org_stage") or "").lower() in ("established", "mature"):
        factor = 5.0
    elif yrs and yrs >= 5:
        factor = 3.0
    n_grants = _num(org.get("org_grants_count"))
    if n_grants and n_grants >= 20:
        factor += 2.0
    elif n_grants and n_grants >= 5:
        factor += 1.0
    capacity = anchor * factor
    if ask <= capacity:
        return 1.0                        # within multiplied absorption capacity
    if ask <= capacity * 1.5:
        return 0.5                        # a credible stretch
    return 0.0


def capacity_factors(org: dict, rfp: dict, donor: dict | None = None,
                     org_settings: dict | None = None) -> list[dict]:
    """MUST-3 components — ACTIVE-ONLY (owner 2026-06-29b). A component is active only
    when the call/donor imposes it (budget ceiling / grant ceiling / experience) OR
    it's determinable from org+call (award-absorption). Undetected → 'Not sure'
    (excluded). HARD: budget/grant ceilings (unknown org value → 0 → pass). SOFT:
    experience, award-absorption. No active component → derive returns 'Not sure'
    (Park). ("Org stage" retired 2026-07-20 — redundant with Experience.)"""
    org = org or {}
    donor = donor or {}
    items: list[dict] = []

    # 1. (RETIRED 2026-07-20) "Org stage" was redundant with "Experience requirement" —
    #    both matched org maturity against the call's bar (stage-category vs years). Per
    #    owner, keep ONE maturity component (experience) and drop org-stage as a scored
    #    component. The org's stage still informs MUST-3 via the award-absorption STRETCH
    #    (_award_absorption_score), and org age still scores the PREFER-8 competitiveness
    #    edge ("Established (10+ years)") — those are different roles, not a capacity gate.

    # 2. Annual-budget ceiling — active only when the donor states it (unknown org
    #    budget → 0 → pass below the ceiling).
    mab = _num(donor.get("donor_max_annual_budget"))
    items.append(_qfactor("budget_ceiling", "Annual-budget ceiling", active=bool(mab),
                          score=(1.0 if (_num(org.get("org_annual_budget")) or 0.0) <= (mab or 0)
                                 else 0.0), hard=True))

    # 3. Prior-grant ceiling — active only when the donor states it.
    mpg = _num(donor.get("donor_max_prior_grant"))
    items.append(_qfactor("grant_ceiling", "Prior-grant ceiling", active=bool(mpg),
                          score=(1.0 if (_num(org.get("org_largest_grant")) or 0.0) <= (mpg or 0)
                                 else 0.0), hard=True))

    # 4. Experience requirement — active only when the call requires it (LLM-detected).
    #    ≥N→1 · within 2y→0.5 · else 0 · unknown founding year→0.5.
    req = _experience_required_years(donor)
    yrs = _org_years(org)
    sc = (0.5 if yrs is None else
          1.0 if (req and yrs >= req) else 0.5 if (req and yrs >= req - 2) else 0.0)
    items.append(_qfactor("experience", f"Experience ≥ {req}y" if req else "Experience requirement",
                          active=req is not None, score=sc, hard=False))

    # 5. Award-absorption — active when the award size IS determinable (call states a
    #    value AND the org has capacity facts); else Not sure.
    aa = _award_absorption_score(org, rfp)
    items.append(_qfactor("award_absorption", "Can absorb the award size",
                          active=aa is not None, score=aa if aa is not None else 0.0,
                          hard=False))
    return items


def derive_capacity(org: dict, rfp: dict, donor: dict | None = None,
                    org_settings: dict | None = None,
                    rfp_compliance: dict | None = None) -> str | None:
    """MUST-3 label (gate logic like MUST-1): any active component 0 → 'No, beyond
    us'; any 0.5 → 'Yes, but a stretch'; all 1 → 'Yes, comfortably'; nothing active
    → None ('Not sure')."""
    items = [x for x in capacity_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if x["active"] and x["score"] is not None]
    if not items:
        return "Not sure"
    scores = [x["score"] for x in items]
    if any(s <= 0.0 for s in scores):
        return "No, beyond us"
    if any(s == 0.5 for s in scores):
        return "Yes, but a stretch"
    return "Yes, comfortably"


def capacity_bid_strength(org: dict, rfp: dict, donor: dict | None = None,
                          org_settings: dict | None = None,
                          rfp_compliance: dict | None = None) -> tuple[float, int]:
    """(numerator, denominator) over ACTIVE MUST-3 components — Σ scores ÷ count."""
    items = [x for x in capacity_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if x["active"] and x["score"] is not None]
    return sum(x["score"] for x in items), len(items)


# Inclusive geo tiers — a call open to any of these is reachable by an org that
# operates in qualifying (LMIC/developing) countries via its OWN presence.
_INCLUSIVE_GEO_MARKERS = (
    "lmic", "low- and middle-income", "low and middle income", "low-income",
    "lower-income", "developing countr", "global south", "global", "globally",
    "worldwide", "international", "any country", "all countries",
    "multiple countries", "multi-country",
)


def _is_inclusive_geo(rfp_geo: list) -> bool:
    blob = " ".join(str(g).lower() for g in (rfp_geo or []))
    return any(m in blob for m in _INCLUSIVE_GEO_MARKERS)


# --- MUST-4 GEOGRAPHIC FIT (rework 2026-06-28; owner spec) --------------------
# Tiered single component: registered ∩ scope → own presence (1) · operation ∩ scope
# OR a qualifying affiliated partner in scope → via a partner (0.5) · neither → 0.
_GEO_PARTNER_TYPES = ("academic / research institutions", "nonprofit / ngo",
                      "for-profit / private")
_GEO_PARTNER_STATUS = ("implementing partner", "collaborator")
_US_NAMES = {"united states", "united states of america", "usa", "u.s.", "us"}


def _covers_scope(countries: Any, scope: Any) -> bool:
    """Any country in `countries` falls within `scope` (geo expansion), OR `scope` is
    an inclusive tier (LMIC/global/developing) reachable via the org's own presence."""
    cs = list(countries or [])
    sc = _as_list(scope)
    if not cs or not sc:
        return False
    if set(_geo.expand(cs)) & set(_geo.expand(sc)):
        return True
    return bool(_is_inclusive_geo(sc))


def _geo_partner_in_scope(org: dict, scope: Any) -> bool:
    """A qualifying affiliated partner (Academic/NGO/For-profit · Implementing/
    Collaborator) located within the call/donor scope."""
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type", "")).strip().lower()
        stv = p.get("status")
        stats = {str(s).strip().lower()
                 for s in (stv if isinstance(stv, (list, tuple)) else [stv])}
        if (ptype in _GEO_PARTNER_TYPES and (stats & set(_GEO_PARTNER_STATUS))
                and _covers_scope([p.get("country")], scope)):
            return True
    return False


def _geo_scope(rfp: dict, donor: dict | None) -> list[str]:
    """Call call_geographic_scope ∪ donor donor_geographic_scope — deduped (case-insensitive)."""
    raw = _as_list(rfp.get("call_geographic_scope")) + _as_list((donor or {}).get("donor_geographic_scope"))
    seen, out = set(), []
    for s in raw:
        k = str(s).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _geo_presence(org: dict, rfp: dict, donor: dict | None = None,
                  org_settings: dict | None = None) -> dict:
    """MUST-4 tiered result: {active, score 1/0.5/0, label, scope, via}. ACTIVE-ONLY
    (owner 2026-06-29b): a US-federal / US-only call (no intl cue) → scope = United
    States; a call/donor with a stated scope → tiered match; NO scope at all → 'Not
    sure' (active=False, excluded)."""
    scope = _geo_scope(rfp, donor)
    if not scope:
        if _is_us_federal(rfp):
            scope = ["United States"]              # US-federal / US-only default scope
        else:
            return {"active": False, "score": None, "label": "Not sure", "scope": [],
                    "via": "no geographic scope stated"}
    scope_us = any(str(s).strip().lower() in _US_NAMES for s in scope)
    org_us = str((org_settings or {}).get("org_is_us_entity", "")).lower() == "true"
    registered = org.get("org_registered_countries") or []
    operation = org.get("org_operating_countries") or []
    if _covers_scope(registered, scope) or (scope_us and org_us):
        return {"active": True, "score": 1.0, "label": "Yes, our own presence",
                "scope": scope, "via": "registered / based in scope"}
    if _covers_scope(operation, scope) or _geo_partner_in_scope(org, scope):
        return {"active": True, "score": 0.5, "label": "Yes, via a partner", "scope": scope,
                "via": "operating country / affiliated partner in scope"}
    return {"active": True, "score": 0.0, "label": "No presence there", "scope": scope,
            "via": ""}


def derive_geographic_fit(org: dict, rfp: dict, org_settings: dict | None = None,
                          donor: dict | None = None) -> str | None:
    """MUST-4 GEOGRAPHIC FIT label — registered ∩ scope → 'Yes, our own presence';
    operation ∩ scope / qualifying partner → 'Yes, via a partner'; neither → 'No
    presence there'. Scope = call ∪ donor; no scope → 'Not sure' (Park)."""
    return _geo_presence(org, rfp, donor, org_settings)["label"]


def geographic_bid_strength(org: dict, rfp: dict, org_settings: dict | None = None,
                            donor: dict | None = None) -> tuple[float, int]:
    """(score, denom) — MUST-4 is ONE tiered component (own=1 · via partner=0.5 ·
    none=0); denom 0 when no scope (Not sure)."""
    g = _geo_presence(org, rfp, donor, org_settings)
    return (g["score"], 1) if g["active"] else (0.0, 0)


_COST_SHARE_RE = re.compile(r"cost[\s-]*shar\w*\s*(?:required)?\s*[:=]\s*([^|]+)", re.I)


def _cost_share_required(rfp: dict) -> bool | None:
    """True/False if the RFP states a cost-share, None if not mentioned."""
    notes = f"{rfp.get('notes') or ''} {rfp.get('brief_description') or ''}"
    m = _COST_SHARE_RE.search(notes)
    if not m:
        return None
    v = m.group(1).strip().lower()
    return False if v[:2] in ("no", "0%", "0 ") or v.startswith(("none", "not")) else True


# MUST-1 requirement keys that carry a VALUE (not a yes/no flag). When the call
# itself states one (extraction → rfp_compliance), we keep the actual value rather
# than coercing it to True — and only when the donor record is blank for that key.
_RFP_VALUED_KEYS = frozenset({
    "donor_entity_type_required", "donor_registration_region",
    "donor_requires_pi", "donor_pi_country_scope", "donor_max_prior_grant", "donor_max_annual_budget",
    "donor_hq_country_required", "org_stage_required", "donor_prior_beneficiary_rule",
    "experience_required",                       # MUST-3 experience (call-LLM detected)
})


# The LLM (`compliance_flags` / `must1_requirements`) emits BARE keys — its stable
# vocabulary — but after the data-model rename the criteria read source-prefixed
# columns (donor_*). Map bare→column so a requirement stated only in the CALL still
# drives MUST-1/MUST-5. Most flags just gain a `donor_` prefix; these few changed
# stem or intentionally stay bare:
_LLM_KEY_ALIASES = {
    "max_prior_grant_usd": "donor_max_prior_grant",
    "max_annual_budget_usd": "donor_max_annual_budget",
    "org_stage_required": "org_stage_required",
    "experience_required": "experience_required",
}


def _eff_column(k: str) -> str:
    """LLM-emitted (bare) compliance key → the column the criteria actually read."""
    if k in _LLM_KEY_ALIASES:
        return _LLM_KEY_ALIASES[k]
    if k.startswith(("donor_", "call_", "org_")):
        return k
    return "donor_" + k


def _merge_rfp_compliance(donor: dict | None, rfp_compliance: dict | None) -> dict:
    """Donor profile augmented with requirements the RFP ITSELF states (LLM-
    extracted `compliance_flags`) — triangulates donor × RFP so a requirement
    stated only in the call still drives MUST-1/MUST-5. RFP-true overrides absent
    donor. VALUED keys keep their actual value (e.g. pi_country_scope='foreign')
    instead of being flattened to True, but never overwrite a non-blank donor value."""
    eff = dict(donor or {})
    for k, v in (rfp_compliance or {}).items():
        if not v:
            continue
        col = _eff_column(k)
        if col in _RFP_VALUED_KEYS:
            if not str(eff.get(col) or "").strip():
                eff[col] = v
        else:
            eff[col] = True
    return eff


# --- factor model (shared by derivation + the Review pass/fail panel) --------
# A FACTOR is one 1/0 check that contributes to a criterion:
#   met    — True = org satisfies it · False = org fails it · None = can't tell.
#   fatal  — True = NON-DYNAMIC eligibility gate: if met is False the whole
#            opportunity is auto-Declined (a structural ineligibility the org
#            cannot fix before the deadline). False = ⚙ dynamic / graded — it only
#            moves the score.
#   active — the requirement is actually imposed by this RFP/donor (inactive
#            factors are neither scored nor shown).
def _factor(key: str, name: str, source: str, met: bool | None,
            *, fatal: bool = False, active: bool = True) -> dict:
    return {"key": key, "name": name, "source": source,
            "met": met, "fatal": fatal, "active": active}


# A MUST-1 ITEM (2026-06-28 rework). Like `_factor` but score-based: an ACTIVE
# item carries a `score` ∈ {0.0, 0.5, 1.0} (hard items only 0/1; soft items may be
# 0.5). `met` is DERIVED for legacy readers (Review cards / `fatal_decline`):
# 1.0→True, 0.0→False, 0.5→None. fatal stays True (any active item at 0 declines).
def _qfactor(key: str, name: str, *, active: bool, score: float | None,
             hard: bool, source: str = "DO", default: bool = False) -> dict:
    sc = float(score) if (active and score is not None) else None
    met = None if sc is None else (True if sc >= 1.0 else (False if sc <= 0.0 else None))
    # `default` = the call/donor imposes NO requirement here, so the component takes
    # the permissive default (score 1, a pass) — shown as "(no restriction)" so a
    # default pass reads differently from a verified match. ("Not sure" is obsolete.)
    return {"key": key, "name": name, "source": source, "active": bool(active),
            "score": sc, "hard": bool(hard), "met": met, "fatal": True,
            "default": bool(default)}


# --- MUST-5 COFINANCING & COMPLIANCE helpers (rework 2026-06-29; owner spec) ---
def _has_qualifying_partner(org: dict) -> bool:
    """Org lists a partner acting as Implementing Partner or Collaborator."""
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        stv = p.get("status")
        stats = {str(s).strip().lower()
                 for s in (stv if isinstance(stv, (list, tuple)) else [stv])}
        if stats & {"implementing partner", "collaborator"}:
            return True
    return bool(org.get("trusted_partners"))          # legacy flat-list fallback


def _has_required_partner(org: dict, rfp: dict, donor: dict) -> bool:
    """A partner matching the donor's required partner TYPE and/or COUNTRY (a 'local
    partner' with no explicit country falls back to the call's geographic scope)."""
    req_types = {t.lower() for t in _as_list(donor.get("donor_required_partner_type")) if t.lower() != "any"}
    req_ctrys = {c.lower() for c in _as_list(donor.get("donor_required_partner_country")) if c.lower() != "any"}
    if _truthy(donor.get("donor_local_partner_required")) and not req_ctrys:
        req_ctrys = {c.lower() for c in _as_list(rfp.get("call_geographic_scope"))}
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        pt = str(p.get("type", "")).strip().lower()
        pc = str(p.get("country", "")).strip().lower()
        if req_types and pt not in req_types:
            continue
        if req_ctrys and not _covers_scope([pc], list(req_ctrys)):
            continue
        return True
    return False


# Funding-route vocabulary — donor flag → canonical route token. The org declares
# which of these it can RECEIVE through (org_profile.org_funding_routes, same tokens).
_ROUTE_FLAG_TOKENS = {
    "donor_grant_route": "grant",
    "donor_procurement_tender_route": "procurement",
    "donor_loan_dev_finance_route": "loan",
    "donor_subrecipient_partner_possible": "subrecipient",
    "donor_govt_or_ccm_route_required": "govt_ccm",
    "donor_direct_local_org_eligible": "direct",
}
# (token, human label) — shared with the org setup multi-select.
ROUTE_OPTIONS = [
    ("grant", "Grants"),
    ("procurement", "Procurement / contracts"),
    ("loan", "Loans / concessional finance"),
    ("subrecipient", "As a subrecipient / partner"),
    ("govt_ccm", "Government / CCM channel"),
    ("direct", "Direct (local entity)"),
]
_ROUTE_LABEL_TO_TOKEN = {lbl.lower(): tok for tok, lbl in ROUTE_OPTIONS}
_ROUTE_LABEL_TO_TOKEN.update({tok: tok for tok, _ in ROUTE_OPTIONS})


def _offered_routes(donor: dict) -> set[str]:
    """Route tokens the call/donor makes available (from the donor route flags —
    populated from the donor profile AND call extraction via _merge_rfp_compliance)."""
    return {tok for flag, tok in _ROUTE_FLAG_TOKENS.items() if _truthy(donor.get(flag))}


def _org_route_set(org: dict) -> set[str]:
    """Route tokens the org can RECEIVE through (org_profile.org_funding_routes)."""
    out = set()
    for r in _as_list(org.get("org_funding_routes")):
        tok = _ROUTE_LABEL_TO_TOKEN.get(str(r).strip().lower())
        if tok:
            out.add(tok)
    return out


def _canonical_donor_match(names: Any, donor: dict | None, rfp: dict) -> bool:
    """True when any donor in `names` (an org's stored donor list) refers to THIS call's
    donor. ROBUST matching (owner 2026-06-30): both sides are first resolved to a
    donor_intel canonical_key via match_donor, so an acronym ("BMGF"), a short name
    ("Gates Foundation") and the full legal name ("Bill & Melinda Gates Foundation") all
    resolve to the same donor however the user typed it. Falls back to normalised exact /
    ≥4-char substring matching for free-typed donors not yet in the catalog."""
    names = [n.get("name") if isinstance(n, dict) else n for n in (names or [])]
    names = [str(n).strip() for n in names if str(n or "").strip()]
    if not names:
        return False
    donor = donor or {}
    # 1) canonical-key match — robust to acronym / full-name / alias / minor variants.
    try:
        from core.donor_intel import match_donor
        call_keys = {k for k in (
            (donor.get("canonical_key") or "").strip(),
            ((match_donor(rfp.get("funding_agency")) or {}).get("canonical_key") or "").strip(),
        ) if k}
        if call_keys:
            for n in names:
                m = match_donor(n)
                if m and (m.get("canonical_key") or "").strip() in call_keys:
                    return True
    except Exception:
        pass
    # 2) fallback — normalised exact / ≥4-char substring match (free-typed donors).
    have = _name_set(names)
    targets = _name_set([donor.get("donor"), donor.get("donor_short"),
                         rfp.get("funding_agency")] + _as_list(donor.get("donor_aliases")))
    for a in have:
        for b in targets:
            if a and b and (a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))):
                return True
    return False


def _signatory_donor_match(org: dict, donor: dict, rfp: dict) -> bool:
    """True when THIS call's donor is in the org's list of donors it has already
    obtained an authorized-signatory sign-off from (org.authorized_signatory_donors)."""
    return _canonical_donor_match(org.get("org_authorized_signatory_donors"), donor, rfp)


def compliance_factors(org: dict, rfp: dict, donor: dict | None = None,
                       org_settings: dict | None = None) -> list[dict]:
    """MUST-5 COFINANCING & COMPLIANCE (rework 2026-06-29; ACTIVE-ONLY 2026-06-29b).
    Each component is ACTIVE only when the donor explicitly imposes it OR the call
    detects it (no proxy applies to these credentials). An undetected component is
    'Not sure' (active=False) — excluded from the count and shown greyed, NOT a
    permissive pass. Hard pre-acquire gates (audited financials/report, SAM-UEI,
    tax-exempt, safeguarding, authorized-signatory donors, partner/govt MOU, govt
    endorsement, local board, partnership, portal registration) + funding-route match;
    ONE SOFT item — co-financing / pre-finance capacity. If NOTHING is detected the
    criterion has no active components → derive_cofinancing returns 'Not sure' (Park)."""
    org = org or {}
    donor = donor or {}
    os = org_settings or {}

    def _need(*fields) -> bool:
        return any(_truthy(donor.get(x)) for x in fields)

    items: list[dict] = []

    # 1. Co-financing / pre-finance capacity (SOFT) — active ONLY on a money-up-front
    #    signal (match %, secured-cofinancing %, reimbursement-only, or a cost-share
    #    clause in the call). cofinancing_capacity → strong/moderate 1 · limited 0.5 ·
    #    none 0. (cost_share + prefinance were redundant → merged.)
    cap = str(org.get("org_cofinancing_capacity") or "").strip().lower()
    cap_sc = (1.0 if cap in ("strong", "moderate")
              else 0.5 if cap == "limited"
              else 0.0 if cap in ("weak", "none", "no") else 0.5)
    a_cofin = bool(_need("donor_cost_sharing_match_required", "donor_min_cofinancing_secured_pct",
                         "donor_state_party_cofinancing_required")
                   or str(donor.get("donor_prefinance_required") or "").strip().lower() == "reimbursement_only"
                   or _cost_share_required(rfp))
    items.append(_qfactor("cofinance", "Co-financing / pre-finance capacity",
                          active=a_cofin, score=cap_sc, hard=False))

    # HARD credential gates — ACTIVE only when the donor/call imposes it; then the org
    # must already hold it (else 0). Undetected → Not sure (excluded).
    _sam_ok = bool(org.get("org_has_sam_uei")) or any(
        "sam" in str(r).lower() for r in (org.get("org_donor_registrations") or []))
    _hard = [
        ("audited_financials", "Audited financials", _need("donor_audited_financials_required"),
         bool(org.get("org_has_audited_financials"))),
        ("audit_report", "Audit report", _need("donor_audit_report_required"),
         bool(org.get("org_has_audit_report"))),
        ("tax_exempt", "Tax-exempt status", _need("donor_tax_exempt_status_required"),
         bool(org.get("org_tax_exempt"))),
        ("safeguarding", "Safeguarding policy", _need("donor_safeguarding_policy_required"),
         bool(org.get("org_has_safeguarding_policy"))),
        ("partner_mou", "Partner MOU", _need("donor_partner_mou_required"),
         bool(org.get("org_has_partner_mou"))),
        ("govt_mou", "Government MOU", _need("donor_govt_mou_required"),
         bool(org.get("org_has_govt_mou"))),
        ("govt_endorsement", "Govt endorsement letter", _need("donor_govt_endorsement_letter_required"),
         bool(org.get("org_has_govt_endorsement"))),
        ("local_board", "Local board", _need("donor_local_board_required"),
         str(os.get("org_has_local_board", "")).lower() == "yes"),
    ]
    for key, name, active, ok in _hard:
        items.append(_qfactor(key, name, active=active,
                              score=(1.0 if ok else 0.0), hard=True))

    # SAM.gov / UEI registration applies ONLY to US-federal (grants.gov) calls (or a donor
    # that explicitly demands it). For every other donor it's irrelevant → a permissive
    # pass (score 1, "no restriction"), NOT a 'Not sure' that drags the criterion down.
    if _need("donor_sam_uei_registration_required") or _is_us_federal(rfp):
        items.append(_qfactor("sam_uei", "SAM.gov / UEI registration",
                              active=True, score=(1.0 if _sam_ok else 0.0), hard=True))
    else:
        items.append(_qfactor("sam_uei", "SAM.gov / UEI registration",
                              active=True, score=1.0, hard=False, default=True))

    # Authorized-signatory — matched to the org's list of donors it has ALREADY
    # obtained an authorized signatory from (not a generic checkbox).
    a_sig = _need("donor_authorized_signatory_signoff_required", "donor_welcome_registration_required")
    items.append(_qfactor("authorized_signatory", "Authorized signatory (this donor)",
                          active=a_sig,
                          score=(1.0 if _signatory_donor_match(org, donor, rfp) else 0.0),
                          hard=True))

    # Partnership mandatory — org has an Implementing/Collaborator partner. (The
    # 'local partner' requirement is NOT repeated here — it's covered by MUST-1 Entity
    # type = grassroot/local.)
    a_part = _need("donor_partnership_mandatory")
    items.append(_qfactor("partnership", "Mandatory partnership", active=a_part,
                          score=(1.0 if _has_qualifying_partner(org) else 0.0), hard=True))

    # NOTE: Funding-platform registration and Funding-route accessibility are NOT MUST
    # gates (owner 2026-06-30) — neither is a hard eligibility floor. They live under
    # PREFER-8 Competitiveness (comp_portal / comp_route), where they only inform Bid
    # Strength. So MUST-5 has NO structural auto-Decline: every remaining hard credential
    # gate is acquirable before the deadline → none is fatal.
    for it in items:
        it["fatal"] = False
    return items


def cofinancing_bid_strength(org: dict, rfp: dict, donor: dict | None = None,
                             org_settings: dict | None = None,
                             rfp_compliance: dict | None = None) -> tuple[float, int]:
    """(numerator, denominator) over the MUST-5 components — Σ scores ÷ count."""
    items = [f for f in compliance_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if f["active"] and f["score"] is not None]
    return sum(f["score"] for f in items), len(items)


def derive_cofinancing(org: dict, rfp: dict, donor: dict | None = None,
                       rfp_compliance: dict | None = None,
                       org_settings: dict | None = None) -> str | None:
    """MUST-5 label (gate logic over ACTIVE components only): any 0 → 'Not met';
    any 0.5 → 'Partial, with effort'; all 1 → 'Yes, fully met'. NO active component
    (nothing the call/donor imposes) → 'Not sure' → scores value 1 (Park). MUST-5 spans
    co-financing AND the compliance gates (SAM/tax-exempt/…), so ANY unmet requirement —
    a hard non-dynamic gate OR co-financing — forces 'Not met' even if the rest are met."""
    scores = [f["score"] for f in compliance_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if f["active"] and f["score"] is not None]
    if not scores:
        return "Not sure"
    if any(s <= 0.0 for s in scores):
        return "Not met"
    if any(s == 0.5 for s in scores):
        return "Partial, with effort"
    return "Yes, fully met"


def _award_quality_label(rfp: dict, org: dict | None = None,
                         policies: dict | None = None) -> str | None:
    """ORG-RELATIVE attractiveness of the award SIZE. Bands the RFP value against
    the org's preferred targets (low/mid/max) using GEOMETRIC midpoints
    (money is multiplicative): cut1=sqrt(low*mid), cut2=sqrt(mid*max).
      value <= cut1 -> Low(0) · <= cut2 -> Moderate(1) · > cut2 -> High(2).
    Falls back to absolute tiers when the org hasn't set targets."""
    val = _usd(rfp)
    if not val:
        return "Not sure"                  # no award value stated → can't size → Park
    org = org or {}
    lo = _num(org.get("org_min_target"))
    mid = _num(org.get("org_mid_target"))
    mx = _num(org.get("org_max_target"))
    if lo and mid and mx and lo <= mid <= mx:
        cut1, cut2 = math.sqrt(lo * mid), math.sqrt(mid * mx)
        if val > cut2:
            return "High"
        if val > cut1:
            return "Moderate"
        return "Low"
    # Fallback: absolute tiers (default hi $2M / mid $500K), tunable via policies.
    hi, mid2 = 2_000_000.0, 500_000.0
    try:
        tiers = ((policies or {}).get("scoring_rules", {})
                 .get("funding_quality_tiers", {}).get("tiers") or [])
        ths = sorted((float(t["threshold_usd"]) for t in tiers if t.get("threshold_usd")),
                     reverse=True)
        if len(ths) >= 2:
            hi, mid2 = ths[0], ths[1]
    except Exception:
        pass
    if val >= hi:
        return "High"
    if val >= mid2:
        return "Moderate"
    return "Low"


def _duration_score(rfp: dict) -> float | None:
    """PREFER-6 duration tier (project_duration, in MONTHS): <=6 -> 0 (low) ·
    >6 and <12 -> 0.5 (moderate) · >=12 -> 1 (high). None when no duration is
    captured -> contributes nothing (the criterion stays 'Not sure' on its own)."""
    d = _num(rfp.get("project_duration"))
    if d is None:
        return None
    if d <= 6:
        return 0.0
    if d < 12:
        return 0.5
    return 1.0


def derive_funding_quality(rfp: dict, org: dict | None = None,
                           policies: dict | None = None) -> str | None:
    """PREFER-6 label, kept CONSISTENT with its displayed component count (owner
    2026-06-30): the mean of the MET/ACTIVE sub-factors. A component that doesn't
    apply — e.g. a project DURATION the call never states — is EXCLUDED entirely; its
    absence neither helps nor hurts, so an org that meets every STATED component scores
    High rather than being dragged to Moderate. When the org hasn't set its min/ceiling
    targets there are no size components to count, so we fall back to the org-relative
    award-SIZE band (duration blended only if present). 'Not sure' when the award value
    is unstated (can't size the award → Park)."""
    if _usd(rfp) is None:
        return "Not sure"
    org = org or {}
    has_band = bool(_num(org.get("org_min_target")) and _num(org.get("org_max_target")))
    parts: list[float] = []
    for f in _funding_quality_factors(rfp, org):
        if not f.get("active"):
            continue                                   # inapplicable → excluded
        s = f.get("score")
        if s is not None:
            parts.append(float(s))
        elif f.get("met") is not None:
            parts.append(1.0 if f.get("met") else 0.0)
    if has_band and parts:
        avg = sum(parts) / len(parts)
        return "High" if avg >= 0.75 else ("Moderate" if avg >= 0.4 else "Low")
    # No org band configured → size on the award-VALUE band; duration only if present.
    award = _award_quality_label(rfp, org, policies)
    a = {"High": 1.0, "Moderate": 0.5, "Low": 0.0}.get(award)   # None if "Not sure"/None
    d = _duration_score(rfp)
    blend = [s for s in (a, d) if s is not None]
    if not blend:
        return "Not sure"
    avg = sum(blend) / len(blend)
    return "High" if avg >= 0.75 else ("Moderate" if avg >= 0.4 else "Low")


def _registered_on_portal(org: dict, rfp: dict, donor: dict | None) -> bool:
    """True when the org has registered on the donor's / the call's portal —
    org.donor_registrations (clean hosts) ∩ {donor.submission_portal_url,
    donor.website, rfp link} host."""
    regs = {clean_portal_url(r) for r in (org.get("org_donor_registrations") or []) if r}
    if not regs:
        return False
    d = donor or {}
    portals = {clean_portal_url(x) for x in
               (d.get("donor_submission_portal_url"), d.get("donor_website"),
                rfp.get("opportunity_link")) if x}
    return bool(regs & {p for p in portals if p})


def _name_set(items) -> set[str]:
    """Normalised name set from a list of strings or {name:...} dicts."""
    out = set()
    for x in items or []:
        n = x.get("name") if isinstance(x, dict) else x
        n = re.sub(r"[^a-z0-9]+", " ", str(n or "").lower()).strip()
        if n:
            out.add(n)
    return out


def _shared_collaborator(org: dict, donor: dict | None) -> bool:
    """True when an organisation WE work with (our partners) also appears among
    the DONOR's partners/collaborators — a warm route into the funder."""
    if not donor:
        return False
    ours = _name_set(org.get("trusted_partners")) | _name_set(org.get("partners"))
    theirs = set()
    for f in ("donor_funders_collaborators", "key_partners", "implementing_partners",
              "partners", "co_funders"):
        theirs |= _name_set(_as_list(donor.get(f)))
    for a in ours:
        for b in theirs:
            if a and b and (a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))):
                return True
    return False


def _funder_in_history(funding_agency: Any, hist: list[str]) -> bool:
    """True iff the call's funder matches an entry in the org's funder_history.
    Substring match requires BOTH sides ≥4 chars (so a short funder code like
    "EU"/"WHO" can't spuriously match "European…"/"WHObla…") — exact match always
    counts. Mirrors core.matching._names_overlap to keep PREFER-7 and funder-fit
    consistent."""
    raw = str(funding_agency or "")
    # "ACRONYM - Donor Name" (Excel migration) → also try the name after the separator,
    # so "BMGF - Gates Foundation" matches a "Gates Foundation" funder-history entry.
    variants = {_norm_rel(raw)}
    if " - " in raw:
        variants.add(_norm_rel(raw.split(" - ", 1)[1]))
    variants.discard("")
    if not variants:
        return False
    for h in hist:
        hn = _norm_rel(h)
        if not hn:
            continue
        for fa in variants:
            if fa == hn or (len(fa) >= 4 and len(hn) >= 4 and (fa in hn or hn in fa)):
                return True
    return False


def _norm_rel(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def derive_funder_relationship(org: dict, rfp: dict, donor: dict | None = None) -> str | None:
    """FUNDER OR PARTNER relationship (PREFER 7). Past grantee of this donor
    (org.funder_history ∋ donor) → "Current/past grantee" (strongest). Else a warm
    route in → "Some contact": we've ENGAGED this donor before (org.engaged_donors),
    OR a SHARED COLLABORATOR — an org we partner with is also among the donor's
    partners/collaborators — OR we're registered on their portal. Else "None"; None
    only when we hold no relationship data."""
    hist = [h for h in (org.get("org_funder_history") or []) if h]
    if _funder_in_history(rfp.get("funding_agency"), hist):
        return "Current/past grantee"
    if (_canonical_donor_match(org.get("org_engaged_donors"), donor, rfp)
            or _shared_collaborator(org, donor) or _registered_on_portal(org, rfp, donor)):
        return "Some contact"
    if (not hist and not (org.get("org_donor_registrations") or [])
            and not (org.get("trusted_partners") or [])
            and not (org.get("org_engaged_donors") or [])):
        return None                    # no relationship data on file → Not sure
    return "None"


def _is_completed(rfp: dict) -> bool:
    """The submission is already in (Progress status = Completed), so deadline runway
    is moot — it was clearly submitted on time."""
    return str(rfp.get("progress_status") or "").strip().lower() == "completed"


def derive_bid_effort(rfp: dict, org_settings: dict | None = None) -> str | None:
    bd = str((org_settings or {}).get("org_has_bd_team", "false")).lower() == "true"
    # Already submitted (Completed) → treat time as ample (it was met), don't penalise.
    days = 10_000 if _is_completed(rfp) else days_until(rfp.get("call_submission_deadline"))
    return bid_effort_label(days, bd)


# Real donor_intel requirement columns (migration 020). Values are text
# (Yes/No/Required/…) → _truthy. Absent/blank flags simply skip that factor.
_GRASSROOT_FLAGS = ("donor_local_registration_required", "donor_local_partner_required")
_BOARD_FLAGS = ("donor_local_board_required",)
_COFIN_FLAGS = ("donor_cost_sharing_match_required", "donor_prefinance_required")
# multi_country_encouraged (migration 053) = the call EXPLICITLY encourages
# multi-country proposals → matched to org Entity type = Multi-country Organization
# (org_is_multi_country). global_multi_country_scope kept as a weaker legacy cue.
_MULTI_FLAGS = ("donor_multi_country_encouraged", "donor_global_multi_country_scope")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "required", "x")


def _flag(donor: dict, names) -> bool:
    return any(_truthy(donor.get(n)) for n in names)


def derive_competitiveness(org: dict, rfp: dict, donor: dict | None = None,
                           org_settings: dict | None = None) -> str | None:
    """Composite "how well-positioned are we to win?" — org age (older=stronger)
    plus, per the donor's requirements: grassroots/local-org, local board,
    co-financing, multi-country presence, and HQ-country match. None when there's
    no signal at all (reviewer picks). Each factor degrades gracefully if its
    donor flag is absent."""
    from datetime import date
    org = org or {}
    org_settings = org_settings or {}
    score, signals = 0.0, 0

    # Track record on the RFP's EXACT program area — the strongest competitiveness
    # signal. Build the org's 0–5 domain (experience) vector and read the best
    # rating across the call's program keys: strong record = an edge; none = wide open.
    _tr = _track_record_band(org, rfp, donor)
    if _tr is not None:
        signals += 1
        _sc = _tr[0]                                       # 0 / 0.5 / 1 (org ÷ donor band)
        score += 1.5 if _sc >= 1.0 else (0.5 if _sc >= 0.5 else -1.0)

    fy = _num(org.get("org_founding_year"))
    if fy:
        signals += 1
        age = date.today().year - int(fy)
        score += 1.0 if age >= 20 else (0.5 if age >= 10 else 0.0)

    # Portal familiarity — registered on the donor's / call's portal = an edge
    # (knows the application process). Positive-only (no penalty if not).
    if _registered_on_portal(org, rfp, donor):
        signals += 1
        score += 0.5

    if donor:
        if _flag(donor, _GRASSROOT_FLAGS):
            signals += 1
            score += 0.5 if _truthy(org_settings.get("org_is_grassroot")) else -1.0
        if _flag(donor, _BOARD_FLAGS):
            signals += 1
            has_board = str(org_settings.get("org_has_local_board", "")).lower() == "yes"
            score += 0.5 if has_board else -1.0
        if _flag(donor, _COFIN_FLAGS):
            signals += 1
            strong = str(org.get("org_cofinancing_capacity") or "").lower() == "strong"
            score += 0.5 if strong else -0.5
        if _flag(donor, _MULTI_FLAGS):
            signals += 1
            score += 1.0 if _truthy(org_settings.get("org_is_multi_country")) else -0.5
        dhq = _funder_country(rfp, donor).strip().lower()
        ohq = str(org_settings.get("org_hq_country")
                  or org_settings.get("org_country") or "").strip().lower()
        if dhq and ohq:
            signals += 1
            if dhq == ohq:
                score += 1.0

    if signals == 0:
        return "Not sure"                  # no competitiveness signal → Park
    if score >= 1.0:
        return "Strong (limited field / incumbent / clear edge)"
    if score <= -0.5:
        return "Weak (wide-open)"
    return "Moderate"


# Org legal-type → eligibility bucket; donor eligibility flags per bucket.
_ORG_TYPE_BUCKET = {
    "nonprofit": "ngo", "ngo": "ngo", "charity": "ngo", "not-for-profit": "ngo",
    "government": "govt", "govt": "govt", "public": "govt",
    "higher_ed": "academic", "academic": "academic", "university": "academic",
    "for_profit": "for_profit", "for-profit": "for_profit", "business": "for_profit",
}
_DONOR_TYPE_FLAG = {"ngo": "donor_ngo_eligible", "for_profit": "donor_for_profit_eligible"}

# High-precision RFP-text cues that the award is for an INDIVIDUAL applicant
# (early-career investigator / single PI / fellowship) — an org can't apply.
_INDIVIDUAL_APPLICANT_RE = re.compile(
    r"\b(individual investigators?|single principal investigator|"
    r"co-?principal investigators?\s+are\s+ineligible|"
    r"early[- ]career investigators?|junior faculty|"
    r"post-?doctoral\s+(?:student|fellow|researcher|scholar)s?|"
    r"for individuals(?:\s+not\s+an?\s+organi[sz]ation)?)\b", re.I)


def _partner_match(org: dict, ptype: Any, pcountry: Any) -> bool:
    """True if the org lists a partner matching the required type and/or country."""
    pt = str(ptype or "").strip().lower()
    pc = str(pcountry or "").strip().lower()
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        if pt and pt not in str(p.get("type", "")).strip().lower():
            continue
        if pc and pc != str(p.get("country", "")).strip().lower():
            continue
        return True
    return False


# --- MUST-1 helpers (2026-06-28 rework) --------------------------------------
_EARLY_STAGE = {"early-stage", "early stage", "early", "startup", "start-up",
                "seed", "emerging", "nascent"}
_ESTABLISHED_STAGE = {"established", "mature", "growth", "scale", "scaleup",
                      "scale-up", "scaling"}


def _stage_family(v: Any) -> str:
    """Normalise an org-stage value to 'early' | 'established' | '' (unknown)."""
    s = str(v or "").strip().lower()
    if s in _EARLY_STAGE:
        return "early"
    if s in _ESTABLISHED_STAGE:
        return "established"
    if "early" in s or "startup" in s or "start-up" in s:
        return "early"
    if "establish" in s or "mature" in s:
        return "established"
    return ""


def _covers(region_terms: Any, countries: Any) -> bool:
    """True when a country list covers a required region/country set (via geo
    expansion of both sides) — used for MUST-1 registration matching."""
    region = _as_list(region_terms)
    own = list(countries or [])
    if not region or not own:
        return False
    return bool(set(_geo.expand(own)) & set(_geo.expand(region)))


def _region_covered(region: Any, org: dict) -> bool:
    """True if the org is REGISTERED in the required region, else (fallback) it
    OPERATES there. Country/UN-region matching goes through geo expansion; an
    inclusive tier (LMIC / developing / global / multi-country) is reachable via the
    org's OWN presence, so ANY registered/operating country satisfies it."""
    regd = org.get("org_registered_countries") or []
    ops = org.get("org_operating_countries") or []
    if _covers(region, regd) or _covers(region, ops):
        return True
    if _is_inclusive_geo(_as_list(region)) and (regd or ops):
        return True
    return False


# Partner types/statuses that can serve as a FOREIGN PI (MUST-1 item E child).
_PI_PARTNER_TYPES = ("nonprofit / ngo", "academic / research institutions",
                     "for-profit / private")
_PI_PARTNER_STATUS = ("implementing partner", "collaborator")


def _foreign_pi_partner(org: dict, donor: dict | None) -> bool:
    """True when the org has an affiliated partner able to be the FOREIGN PI:
    type ∈ research/NGO/for-profit AND status ∈ implementing/collaborator AND the
    partner sits OUTSIDE the org's registration countries OR in the donor's HQ
    country (covers donor-country and 3rd-party-OECD PI requirements — CADC case)."""
    regd = {str(c).strip().lower() for c in (org.get("org_registered_countries") or [])}
    dhq = str((donor or {}).get("donor_hq_country") or "").strip().lower()
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type", "")).strip().lower()
        stv = p.get("status")
        stats = {str(s).strip().lower()
                 for s in (stv if isinstance(stv, (list, tuple)) else [stv])}
        pc = str(p.get("country", "")).strip().lower()
        if (ptype in _PI_PARTNER_TYPES and (stats & set(_PI_PARTNER_STATUS))
                and pc and ((pc not in regd) or (dhq and pc == dhq))):
            return True
    return False


def qualification_factors(org: dict, rfp: dict, donor: dict | None = None,
                          org_settings: dict | None = None) -> list[dict]:
    """MUST-1 — LEGAL STATUS & QUALIFICATION (reworked 2026-06-28; D/F revised
    2026-06-29). SIX scored items: A legal type · B entity type · C HQ · D registration
    · E individual-PI · F prior beneficiary — all HARD (0/1). Strict rule: an active item
    the org can't demonstrably meet scores 0. D Registration uses the call's GEOGRAPHIC
    SCOPE as a proxy when no explicit rule is stated; F Prior beneficiary is ACTIVE ONLY
    when the donor states a prior-beneficiary rule (else not-applicable). Roll-up →
    `derive_qualification` / `qualification_bid_strength`. (Org stage + annual-budget
    + prior-grant ceilings MOVED to MUST-3 `capacity_factors`; 'independent entity'
    deactivated.)"""
    org = org or {}
    donor = donor or {}
    os = org_settings or {}
    items: list[dict] = []

    # ACTIVE-ONLY model (owner 2026-06-29b): a component is ACTIVE only when the
    # donor/call imposes it (or a proxy determines it — D registration via geo scope).
    # An undetected component is 'Not sure' (active=False) — excluded from the count
    # and shown greyed, NOT a permissive pass. No active item → derive returns 'Not
    # sure' (Park).

    # --- A. Legal type — reuses ngo_eligible / for_profit_eligible
    legal = str(org.get("org_legal_type") or "").strip().lower()
    bucket = _ORG_TYPE_BUCKET.get(legal, "")
    flag = _DONOR_TYPE_FLAG.get(bucket)
    detected = bool(bucket and flag and str(donor.get(flag) or "").strip() != "")
    admitted = True
    if detected:
        fv = str(donor.get(flag) or "").strip().lower()
        if fv == "yes":
            admitted = True
        elif fv == "no":
            admitted = False
        else:
            admits = {b for b, fl in _DONOR_TYPE_FLAG.items()
                      if str(donor.get(fl) or "").strip().lower() == "yes"}
            admitted = (not admits) or (bucket in admits)
    items.append(_qfactor("applicant_type", "Eligible legal type", active=detected,
                          score=(1.0 if admitted else 0.0), hard=True))

    # --- B. Entity type -------------------------------------------------------
    ent_req = str(donor.get("donor_entity_type_required") or "").strip().lower()
    org_ent = str(org.get("org_entity_type") or "").strip().lower()
    items.append(_qfactor("entity_type", "Entity type", active=bool(ent_req),
                          score=(1.0 if (org_ent and org_ent == ent_req) else 0.0),
                          hard=True))

    # --- C. HQ country — HQ in one of the required countries ------------------
    hq_req = [h.lower() for h in _as_list(donor.get("donor_hq_country_required"))]
    detected = bool(hq_req and "any" not in hq_req)
    ohq = str(os.get("org_hq_country") or os.get("org_country") or "").strip().lower()
    items.append(_qfactor("donor_hq_country", "HQ country", active=detected,
                          score=(1.0 if (ohq and ohq in hq_req) else 0.0), hard=True))

    # --- D. Registration region — GEO-SCOPE PROXY (owner 2026-06-29). Active when the
    #    donor states a region, when it explicitly says "Any" (a real pass), OR via the
    #    call's geographic scope as a proxy. NO region AND no scope → Not sure (excluded).
    reg_req = _as_list(donor.get("donor_registration_region"))
    explicit_any = any(r.lower() == "any" for r in reg_req)
    region = ([] if explicit_any else
              (reg_req or _as_list(rfp.get("call_geographic_scope"))
               or _as_list(donor.get("donor_geographic_scope"))))
    if not region and not explicit_any and _is_us_federal(rfp):
        region = ["United States"]               # US-federal / US-only → must be US-registered
    items.append(_qfactor("local_registration", "Registration region",
                          active=bool(explicit_any or region),
                          score=(1.0 if (explicit_any or _region_covered(region, org)) else 0.0),
                          hard=True))

    # --- E. Individual-PI — PI gate then base country -------------------------
    _qtext = " ".join(str(rfp.get(x) or "") for x in
                      ("opportunity_title", "brief_description", "notes"))
    detected = bool(_truthy(donor.get("donor_requires_pi"))
                    or _INDIVIDUAL_APPLICANT_RE.search(_qtext))
    if str(donor.get("donor_pi_country_scope") or "").strip().lower() == "foreign":
        ok = _foreign_pi_partner(org, donor)                       # via affiliated partner
    else:                                                          # in-scope / unspecified
        ok = bool(org.get("org_has_established_pi"))                   # our own PI
    items.append(_qfactor("individual_pi", "Individual / PI", active=detected,
                          score=(1.0 if ok else 0.0), hard=True))

    # (Org stage, annual-budget ceiling, prior-grant ceiling MOVED to MUST-3.)

    # --- F. Prior beneficiary — PRIOR-RELATIONSHIP REQUIRED (owner 2026-06-29). ACTIVE
    #    ONLY when the donor/call states a prior-beneficiary rule; otherwise NOT
    #    APPLICABLE (excluded — so a brand-new donor is never auto-declined). When
    #    active: this donor in the org's prior-funding list (active_donors ∪
    #    funder_history) → 1, not listed → 0. Verifiable, so no permissive default.
    rule = str(donor.get("donor_prior_beneficiary_rule") or "").strip().lower()
    if rule:
        fa = rfp.get("funding_agency")
        prior = (_funder_in_history(fa, [d for d in (org.get("org_active_donors") or []) if d])
                 or _funder_in_history(fa, [d for d in (org.get("org_funder_history") or []) if d]))
        sc = 1.0 if prior else 0.0
    else:
        sc = 1.0
    items.append(_qfactor("prior_beneficiary", "Prior beneficiary (relationship with this donor)",
                          active=bool(rule), score=sc, hard=True, default=False))

    return items


def qualification_bid_strength(org: dict, rfp: dict, donor: dict | None = None,
                               org_settings: dict | None = None,
                               rfp_compliance: dict | None = None) -> tuple[float, int]:
    """(numerator, denominator) over ACTIVE MUST-1 items — numerator = Σ scores,
    denominator = count. Bid Strength = numerator ÷ denominator (caller divides;
    denominator 0 → undefined → 'Not sure'). Transparency only — NOT forced to 0
    when the label is a decline. `rfp_compliance` folds in call-stated requirements."""
    items = [x for x in qualification_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if x["active"] and x["score"] is not None]            # denominator = ACTIVE only
    return sum(x["score"] for x in items), len(items)


def derive_qualification(org: dict, rfp: dict, donor: dict | None = None,
                         org_settings: dict | None = None,
                         rfp_compliance: dict | None = None) -> str:
    """MUST-1 label (2026-06-28 rework). Decision order over ACTIVE items:
      denominator 0 (nothing imposed) → 'Not sure';
      any item scored 0 → 'No, not eligible' (one mismatch overrides all);
      any item scored 0.5 → 'Mostly, one item unclear';
      all items scored 1 → 'Yes, fully'.
    `rfp_compliance` folds in requirements the CALL itself states (extraction)."""
    scores = [x["score"] for x in qualification_factors(
        org, rfp, _merge_rfp_compliance(donor, rfp_compliance), org_settings)
        if x["active"] and x["score"] is not None]            # ACTIVE items only
    if not scores:
        return "Not sure"                                     # denominator 0
    if any(s <= 0.0 for s in scores):
        return "No, not eligible"
    if any(s == 0.5 for s in scores):
        return "Mostly, one item unclear"
    return "Yes, fully"


def derive_criteria(rfp: dict, org: dict | None = None, donor: dict | None = None,
                    org_settings: dict | None = None,
                    policies: dict | None = None) -> dict[str, str | None]:
    """All 9 derived labels (None where not determinable)."""
    org = org or {}
    return {
        "qualification": derive_qualification(org, rfp, donor, org_settings),
        "strategic_fit": derive_strategic_fit(org, rfp, donor),
        "capacity": derive_capacity(org, rfp, donor, org_settings),
        "geographic_fit": derive_geographic_fit(org, rfp, org_settings, donor),
        "cofinancing": derive_cofinancing(org, rfp, donor, org_settings=org_settings),
        "funding_quality": derive_funding_quality(rfp, org, policies),
        "funder_relationship": derive_funder_relationship(org, rfp, donor),
        "competitiveness": derive_competitiveness(org, rfp, donor, org_settings),
        "bid_effort": derive_bid_effort(rfp, org_settings),
    }


# --- factor breakdowns for the graded criteria (Review pass/fail panel) ------
def _geo_factors(org: dict, rfp: dict, org_settings: dict | None = None,
                 donor: dict | None = None) -> list[dict]:
    """MUST-4 is ONE tiered component "Geographic presence" (own=1 · via partner=0.5 ·
    none=0). Carries `_via` + `_scope` for the Review info line. No scope → permissive
    default pass."""
    g = _geo_presence(org, rfp, donor, org_settings)
    it = _qfactor("geo_presence", "Geographic presence", active=g["active"],
                  score=g["score"], hard=False)
    it["_via"] = g.get("via", "")
    it["_scope"] = ", ".join(g.get("scope") or [])
    return [it]


def _capacity_factors(org: dict, rfp: dict, donor: dict | None = None,
                      org_settings: dict | None = None) -> list[dict]:
    """MUST-3 sub-factors = the 5 capacity components (see capacity_factors)."""
    return capacity_factors(org, rfp, donor, org_settings)


def _relationship_factors(org: dict, rfp: dict, donor: dict | None = None) -> list[dict]:
    grantee = _funder_in_history(rfp.get("funding_agency"),
                                 [h for h in (org.get("org_funder_history") or []) if h])
    contact = bool(_shared_collaborator(org, donor) or _registered_on_portal(org, rfp, donor))
    # Donor engaged — we've had prior contact (meetings / concept notes / EOIs) with this
    # call's donor though no funding yet. Matched robustly to org.engaged_donors.
    engaged = _canonical_donor_match(org.get("org_engaged_donors"), donor, rfp)
    # OR-tiers: any one satisfies PREFER 7 (grantee is the strongest). Tagged
    # so the Review panel shows them as alternative routes, not all-required.
    return [
        _factor("rel_grantee", "Past / current grantee of this donor", "DO", grantee),
        _factor("rel_engaged", "Donor engaged (prior contact, no funding yet)", "DO", engaged),
        _factor("rel_contact", "Shared collaborator or registered", "DO", contact),
    ]


def fatal_decline(org: dict | None, rfp: dict, donor: dict | None = None,
                  org_settings: dict | None = None,
                  rfp_compliance: dict | None = None) -> tuple[bool, str | None]:
    """THE auto-Decline gate (replaces the blanket 'any MUST<2 → Decline').
    Returns (decline?, trigger_name). True ONLY when a 🔒 non-dynamic factor is
    EXPLICITLY failed (met is False) — a structural ineligibility the org can't
    fix before the deadline: a MUST-1 identity gate or no geographic reach. (MUST-5
    has no fatal floor — its credential gates are acquirable.) Unknowns (None) never
    trigger a decline — they only soften the score (→ usually Park for review)."""
    org = org or {}
    eff = _merge_rfp_compliance(donor, rfp_compliance)
    for f in qualification_factors(org, rfp, eff, org_settings):
        if f["active"] and f["met"] is False:
            return True, f["name"]
    if derive_geographic_fit(org, rfp, org_settings, eff) == "No presence there":
        return True, "Geographic reach (no presence or partner)"
    # MUST-5 has NO structural auto-Decline gate (owner 2026-06-30): the hard credential
    # gates are all acquirable before the deadline, and funding-route / funding-platform
    # moved to PREFER-8 (Bid Strength only). So MUST-5 never forces a decline here.
    return False, None


def _strategic_factors(org: dict, rfp: dict, donor: dict | None = None) -> list[dict]:
    """MUST-2 is ONE component — "Strategic priority fitness" — scored 0/0.5/1: theme
    overlap (≥1 funder theme the org shares) is the gate; the BEST matched theme's
    priority-band agreement is the score. The matched/detected theme counts + terms are
    carried as metadata for display only (the funder's themes are NOT separate
    components). No call/org theme data → 'Not sure' (active=False, excluded → Park)."""
    items = _strategic_items(org, rfp, donor)
    if not items:
        it = _qfactor("strat_fitness", "Strategic priority fitness",
                      active=False, score=None, hard=False)
        it["_matched"] = 0
        it["_detected"] = 0
        it["_terms"] = ""
        return [it]
    matched = [i for i in items if i["score"] > 0]
    best = max((i["score"] for i in items), default=0.0)
    it = _qfactor("strat_fitness", "Strategic priority fitness",
                  active=True, score=best, hard=False)
    it["_matched"] = len(matched)
    it["_detected"] = len(items)
    it["_terms"] = ", ".join(i.get("_term") or "" for i in matched)
    return [it]


def _funding_quality_factors(rfp: dict, org: dict | None = None) -> list[dict]:
    """PREFER-6 sub-factors: award size vs the org's preferred band, JOINED with the
    project duration tier (<=6mo low · 6-12 moderate · >=12 high)."""
    org = org or {}
    val = _usd(rfp)
    lo = _num(org.get("org_min_target"))
    mx = _num(org.get("org_max_target"))
    _dur = _duration_score(rfp)
    return [
        _factor("fq_floor", "At/above your minimum target size", "RG",
                (val >= lo) if (val and lo) else None, active=bool(lo)),
        _factor("fq_ceiling", "Within your absorptive ceiling", "RG",
                (val <= mx) if (val and mx) else None, active=bool(mx)),
        _factor("fq_value", "Award value stated by the call", "R",
                bool(val), active=True),
        # Duration tier as a 0/0.5/1 score-factor (absent → inactive → Not sure).
        _qfactor("fq_duration", "Project duration (longer preferred)",
                 active=_dur is not None, score=_dur, hard=False, source="R"),
    ]


# Currency → funder HQ country (LAST-RESORT inference; owner 2026-06-30). USD is too
# universal to localise (everyone advertises in $), but a call denominated in a national
# currency strongly implies the funder's country (CAD → Canada, GBP → UK, …).
_CURRENCY_COUNTRY = {
    "CAD": "Canada", "GBP": "United Kingdom", "AUD": "Australia", "JPY": "Japan",
    "CHF": "Switzerland", "SEK": "Sweden", "NOK": "Norway", "DKK": "Denmark",
    "INR": "India", "ZAR": "South Africa", "NZD": "New Zealand",
}


def _funder_country(rfp: dict, donor: dict | None) -> str:
    """Best-effort funder HQ country, in priority order: donor_intel HQ → a country named
    on the call (funder location / agency country) → a national award currency
    (CAD→Canada, GBP→UK, …; USD is too universal to localise). '' when undeterminable."""
    donor = donor or {}
    for v in (donor.get("donor_hq_country"), rfp.get("funder_country"),
              rfp.get("funding_agency_country"), rfp.get("funder_location")):
        s = str(v or "").strip()
        if s:
            return s
    return _CURRENCY_COUNTRY.get(str(rfp.get("currency") or "").strip().upper(), "")


def _track_record_band(org: dict, rfp: dict, donor: dict | None):
    """PREFER-8 track record — the org's TRACK-RECORD rating (0–5) in the call's program
    area, judged against the DONOR's PRIORITY for that area (default 5 when the donor
    isn't graded for it). Returns (score 0/0.5/1, org_rating, donor_priority, area_label)
    or None when the call has no classifiable program area / the org has no expertise.
    Scored on the WEAKER of the two — band(min(org, donor)): a strong track record in an
    area the donor barely prioritises is no edge, and vice-versa. 4–5 → High (1.0) · 2–3
    → Moderate (0.5) · else Low (0.0). The call area with the best band wins (tie → the
    org's strongest). So org 3 vs donor 5 → Moderate (3/5); org 5 vs donor 5 → High."""
    rfp_keys = _rfp_program_keys(rfp)
    if not rfp_keys or not (org.get("org_domain_expertise") or org.get("org_domain_ratings")):
        return None
    from core.matching import _priority_vector
    dvec = _priority_vector(org.get("org_domain_expertise"), org.get("org_domain_ratings"))
    donor = donor or {}
    dprio = _theme_scores_flat(donor.get("donor_priority_areas"),
                               _ratings(donor.get("donor_priority_ratings")), 5.0)
    best = None                                  # (score, org_rating, donor_priority, key)
    for k in sorted(rfp_keys):
        org_r = max(0.0, float(dvec.get(k, 0.0) or 0.0))
        donor_p = float(dprio.get(k, 5.0) or 5.0)
        cand = (_band(min(org_r, donor_p)), org_r, donor_p, k)
        if best is None or cand > best:          # max band, then org rating, then donor
            best = cand
    score, org_r, donor_p, k = best
    label = _pa.subarea_label(k) if (k and " - " in k) else k
    return score, org_r, donor_p, label


def _competitiveness_factors(org: dict, rfp: dict, donor: dict | None = None,
                             org_settings: dict | None = None) -> list[dict]:
    """PREFER-8 sub-factors mirroring derive_competitiveness signals."""
    from datetime import date
    org = org or {}
    donor = donor or {}
    osx = org_settings or {}
    fy = _num(org.get("org_founding_year"))
    dhq = _funder_country(rfp, donor).strip().lower()
    ohq = str(osx.get("org_hq_country") or osx.get("org_country") or "").strip().lower()
    # Track record — a GRADED component (org rating ÷ donor priority), shown with its
    # band + ratio so the user can see why (e.g. "Moderate (3/5)").
    _tr = _track_record_band(org, rfp, donor)
    comp_track = _qfactor("comp_track", "Track record in this program area",
                          active=_tr is not None, score=(_tr[0] if _tr else None),
                          hard=False, source="RG")
    if _tr is not None:
        _sc, _orgr, _dprio, _area = _tr
        _bandlbl = "High" if _sc >= 1.0 else ("Moderate" if _sc >= 0.5 else "Low")
        comp_track["_detail"] = (f"{_bandlbl} (your {_orgr:g} vs donor {_dprio:g}"
                                 + (f" · {_area}" if _area else "") + ")")
    out = [
        comp_track,
        _factor("comp_age", "Established (10+ years)", "G",
                (fy is not None and (date.today().year - int(fy)) >= 10) if fy else None,
                active=bool(fy)),
        _factor("comp_portal", "Familiar with the donor's portal", "DG",
                _registered_on_portal(org, rfp, donor), active=True),
    ]
    # Funding route accessible — moved here from MUST-5 (no longer a hard gate, owner
    # 2026-06-30): a soft competitiveness signal that only informs Bid Strength. Active
    # when the donor/call offers route(s); met when the org can RECEIVE through ≥1 of
    # them. Org hasn't declared its routes → 'Not sure' (None), excluded — no penalty.
    offered = _offered_routes(donor)
    if offered:
        org_routes = _org_route_set(org)
        out.append(_factor("comp_route", "Funding route accessible", "DG",
                           (bool(org_routes & offered) if org_routes else None),
                           active=True))
    if _flag(donor, _GRASSROOT_FLAGS):
        out.append(_factor("comp_grassroots", "Grassroots / local-org status", "DG",
                           _truthy(osx.get("org_is_grassroot"))))
    if _flag(donor, _MULTI_FLAGS):
        out.append(_factor("comp_multi", "Multi-country presence", "DG",
                           _truthy(osx.get("org_is_multi_country"))))
    # HQ-country match is a POSITIVE-ONLY edge (a local-HQ advantage), never a penalty:
    # most international funders sit in a different country than the org, so a mismatch
    # is the norm, not a failing. Active (a ✓) ONLY when it actually matches; otherwise
    # excluded — so an international funder no longer shows a red ✗ here. (derive_
    # competitiveness already scores it positive-only.)
    out.append(_factor("comp_hq", "HQ-country match with funder", "DG",
                       True if (dhq and ohq and dhq == ohq) else None,
                       active=bool(dhq and ohq and dhq == ohq)))
    return out


def _bid_effort_factors(rfp: dict, org_settings: dict | None = None) -> list[dict]:
    """PREFER-9 sub-factors: time-to-deadline × a business-development team."""
    osx = org_settings or {}
    bd = str(osx.get("org_has_bd_team", "false")).lower() == "true"
    if _is_completed(rfp):
        # Already submitted → the time gate was met; show it as such, not "not enough".
        time_name, time_met, time_active = "Submitted on time (already completed)", True, True
    else:
        days = days_until(rfp.get("call_submission_deadline"))
        time_name = "Enough time before the deadline (>14d)"
        time_met = (days is not None and days > 14) if days is not None else None
        time_active = days is not None
    return [
        _factor("bid_time", time_name, "R", time_met, active=time_active),
        _factor("bid_team", "Has a business-development team", "G", bool(bd), active=True),
    ]


def fatal_decline(org: dict | None, rfp: dict, donor: dict | None = None,
                  org_settings: dict | None = None,
                  rfp_compliance: dict | None = None) -> tuple[bool, str | None]:
    """THE auto-Decline gate (replaces the blanket 'any MUST<2 → Decline').
    Returns (decline?, trigger_name). True ONLY when a 🔒 non-dynamic factor is
    EXPLICITLY failed (met is False) — a structural ineligibility the org can't
    fix before the deadline: a MUST-1 identity gate or no geographic reach. (MUST-5
    has no fatal floor — its credential gates are acquirable.) Unknowns (None) never
    trigger a decline — they only soften the score (→ usually Park for review)."""
    org = org or {}
    eff = _merge_rfp_compliance(donor, rfp_compliance)
    for f in qualification_factors(org, rfp, eff, org_settings):
        if f["active"] and f["met"] is False:
            return True, f["name"]
    if derive_geographic_fit(org, rfp, org_settings, eff) == "No presence there":
        return True, "Geographic reach (no presence or partner)"
    # MUST-5 has NO structural auto-Decline gate (owner 2026-06-30): the hard credential
    # gates are all acquirable before the deadline, and funding-route / funding-platform
    # moved to PREFER-8 (Bid Strength only). So MUST-5 never forces a decline here.
    return False, None


def factor_breakdown(rfp: dict, org: dict | None = None, donor: dict | None = None,
                     org_settings: dict | None = None,
                     rfp_compliance: dict | None = None) -> dict[str, list[dict]]:
    """Per-criterion sub-factor lists for the Review per-criterion cards — for ALL
    9 criteria. Each factor carries `active` (whether THIS call/donor imposes it):
    inactive factors are KEPT so the card can show them as "? Not applicable", and
    they're excluded from the won/total denominator (see core.criteria_factors)."""
    org = org or {}
    eff = _merge_rfp_compliance(donor, rfp_compliance)
    return {
        "qualification": qualification_factors(org, rfp, eff, org_settings),
        "strategic_fit": _strategic_factors(org, rfp, donor),
        "capacity": _capacity_factors(org, rfp, eff, org_settings),
        "geographic_fit": _geo_factors(org, rfp, org_settings, eff),
        "cofinancing": compliance_factors(org, rfp, eff, org_settings),
        "funding_quality": _funding_quality_factors(rfp, org),
        "funder_relationship": _relationship_factors(org, rfp, donor),
        "competitiveness": _competitiveness_factors(org, rfp, eff, org_settings),
        "bid_effort": _bid_effort_factors(rfp, org_settings),
    }
