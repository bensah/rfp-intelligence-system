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
from core.scorer import (
    BID_EFFORT_AMPLE_DAYS, BID_EFFORT_TIGHT_DAYS, bid_effort_label, days_until)

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
    # Prefer the headline award value; fall back to the extracted RANGE — the ceiling
    # (HIGHEST) then the floor — so a call that states its award as a range is still
    # sized. Mirrors MUST-3 capacity (which already reads call_award_ceiling) and honours
    # the "use the HIGHEST/MID of a range" rule.
    val = (_num(rfp.get("call_award_value"))
           or _num(rfp.get("call_award_ceiling"))
           or _num(rfp.get("call_award_floor")))
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


# --- MUST-3 IMPLEMENTATION CAPACITY (rework 2026-08-06; owner spec) -----------
# TWO components (was four):
#   1. Financial capacity for this award — a COMPOSITE of every VALUE-related check
#      (award absorption + the donor's annual-budget / prior-grant ceilings). All three
#      answer one question — "is this award the right SIZE for us?" — and all three need
#      the call's award value, so when extraction misses that value they ALL go blank at
#      once and MUST-3 reads as three separate unknowns instead of one. As one 0-1
#      component the criterion degrades honestly: it scores over whichever value checks
#      ARE determinable, and stays 0-1 even when only one of them is.
#   2. Experience requirement — org maturity vs the bar the call sets, DEFAULT-PASS when
#      neither the call nor donor intel states one (a call silent on experience is open
#      to a start-up and an established org alike).
# ("Org stage" was retired 2026-07-20 as a standalone component; the stage RESTRICTION a
# call can impose is now scored inside Experience — see _experience_factor.)
def _org_years(org: dict) -> int | None:
    """Years the org has existed (from founding_year), or None if unknown."""
    from datetime import date as _date
    fy = _num(org.get("org_founding_year"))
    return (_date.today().year - int(fy)) if (fy and fy >= 1900) else None


# An explicit minimum age/experience a call can state ("no less than 3 years since
# creation") — kept as a NUMBER of years rather than flattened into the coarse
# significant/moderate band, so the bar is scored as written.
_EXP_YEARS_RE = re.compile(r"^(\d{1,2})\s*\+?\s*(?:y|yr|yrs|year|years)?$")


def _experience_required_years(donor: dict) -> int | None:
    """Years of experience the CALL/donor requires (LLM-detected, `experience_required`).
    A bare number ('3', '5+', '10 years') is taken literally; otherwise the graded
    vocabulary maps 'significant'-type language → 10+ and subtler wording → 5+.
    None = no years bar imposed (welcomes early-stage / any applicant)."""
    lvl = str(donor.get("experience_required") or "").strip().lower()
    if not lvl:
        return None
    m = _EXP_YEARS_RE.match(lvl)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 50 else None
    if lvl in ("significant", "extensive", "strong", "high", "deep"):
        return 10
    if lvl in ("moderate", "some", "subtle", "relevant", "demonstrated"):
        return 5
    return None


# Org-maturity vocabulary — both sides (org profile `org_stage`, call/donor
# `org_stage_required`) describe the same two poles with different words.
_EARLY_STAGE_WORDS = {"early-stage", "early stage", "earlystage", "early",
                      "start-up", "startup", "new", "emerging", "young", "nascent"}
_ESTABLISHED_WORDS = {"established", "mature", "experienced", "long-standing"}


def _stage_token(v: Any) -> str | None:
    """Maturity wording → 'early' | 'established' | None (unknown / not restricted)."""
    s = str(v or "").strip().lower()
    if s in _EARLY_STAGE_WORDS:
        return "early"
    if s in _ESTABLISHED_WORDS:
        return "established"
    return None


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


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


def _capacity_value_parts(org: dict, rfp: dict, donor: dict) -> list[dict]:
    """The VALUE-related sub-checks behind the Financial-capacity composite, as
    {key, name, score, hard, detail}. Only DETERMINABLE checks are returned — a ceiling
    the call never states, or an absorption the org has no facts for, is simply absent
    and neither helps nor hurts. `hard` = a structural ceiling: exceeding it is an
    ineligibility the org cannot shrink before the deadline (drives fatal_decline).

    The two directions are deliberately opposite and both belong here:
      · ABSORPTION asks whether we are big enough to deliver this award (the common
        case — a large annually-managed budget is evidence we can carry it);
      · the CEILINGS ask whether we are too big for this fund (the rare case — a window
        reserved for organisations below a stated size)."""
    # A ceiling is only DETERMINABLE when the org's own figure is on file. `or 0.0` on a
    # missing value made an org with no recorded budget PASS a fatal ceiling, and the row
    # said so out loud: "our $0 annual budget vs the call's $1,000,000 ceiling". That is
    # absence dressed as a measurement — and in the permissive direction, so it silently
    # waved through orgs it should have flagged for review. Unknown → not determinable →
    # the sub-part is simply absent (owner 2026-08-07).
    parts: list[dict] = []
    mab = _num(donor.get("donor_max_annual_budget"))
    ob = _num(org.get("org_annual_budget"))
    if mab and ob:
        parts.append({"key": "budget_ceiling", "name": "Annual-budget ceiling",
                      "score": 1.0 if ob <= mab else 0.0, "hard": True,
                      "detail": f"our {_money(ob)} annual budget vs the call's "
                                f"{_money(mab)} ceiling"})
    mpg = _num(donor.get("donor_max_prior_grant"))
    og = _num(org.get("org_largest_grant"))
    if mpg and og:
        parts.append({"key": "grant_ceiling", "name": "Prior-grant ceiling",
                      "score": 1.0 if og <= mpg else 0.0, "hard": True,
                      "detail": f"our {_money(og)} largest grant vs the call's "
                                f"{_money(mpg)} ceiling"})
    aa = _award_absorption_score(org, rfp)
    if aa is not None:
        parts.append({"key": "award_absorption", "name": "Can absorb the award size",
                      "score": aa, "hard": False,
                      "detail": {1.0: "within our proven capacity",
                                 0.5: "a credible stretch"}.get(
                                     aa, "beyond our proven capacity")})
    return parts


def _financial_capacity_factor(org: dict, rfp: dict, donor: dict) -> dict:
    """MUST-3 component 1 — the value-related checks rolled into ONE 0-1 score (their
    mean over whichever are determinable). Inactive only when NOTHING about the money is
    knowable: the call states no award value and imposes no ceiling."""
    parts = _capacity_value_parts(org, rfp, donor)
    it = _qfactor("financial_capacity", "Financial capacity for this award",
                  active=bool(parts), hard=False,
                  score=(sum(p["score"] for p in parts) / len(parts)) if parts else None)
    it["_parts"] = parts
    # 🔒 only when a real structural ceiling is in play — a soft absorption stretch is
    # not an auto-Decline and must not wear the fatal-gate padlock.
    it["fatal"] = any(p["hard"] for p in parts)
    if parts:
        it["_detail"] = " · ".join(f"{p['name']}: {p['detail']}" for p in parts)
    return it


def _experience_factor(org: dict, donor: dict) -> dict:
    """MUST-3 component 2 — ALWAYS ACTIVE (owner 2026-08-06).

    A call that says nothing about maturity is open to a start-up and to an established
    organisation alike, so SILENCE IS A PASS (score 1, flagged `default` so the card reads
    "no restriction — defaults to pass") rather than a 'Not sure' that drags the criterion
    to Park on a requirement nobody imposed. A bar is scored only when the call, or donor
    intel, actually states one:
      · YEARS bar (`experience_required`) — meets it → 1 · within 2 years → 0.5 · else 0
      · STAGE bar (`org_stage_required`)  — the call restricts to early-stage OR to
        established organisations. This is the direction that was previously extracted
        but never scored: a window reserved for young organisations must score an
        established applicant 0, not wave it through.
    Both stated → the WEAKER governs (a bar is a bar)."""
    req_yrs = _experience_required_years(donor)
    req_stage = _stage_token(donor.get("org_stage_required"))
    if req_yrs is None and req_stage is None:
        it = _qfactor("experience", "Experience requirement", active=True,
                      score=1.0, hard=False, default=True)
        it["fatal"] = False
        return it
    yrs = _org_years(org)
    stage = _stage_token(org.get("org_stage"))
    scores: list[float] = []
    bits: list[str] = []
    if req_yrs is not None:
        if yrs is not None:
            sc = 1.0 if yrs >= req_yrs else (0.5 if yrs >= req_yrs - 2 else 0.0)
            bits.append(f"{yrs}y since founding vs the call's {req_yrs}y bar")
        elif stage == "established":
            sc = 1.0 if req_yrs <= 10 else 0.5
            bits.append(f"an established org vs the call's {req_yrs}y bar "
                        f"(our founding year is unrecorded)")
        elif stage == "early":
            sc = 0.0 if req_yrs >= 5 else 1.0
            bits.append(f"an early-stage org vs the call's {req_yrs}y bar "
                        f"(our founding year is unrecorded)")
        else:
            sc = 0.5
            bits.append(f"the call asks for {req_yrs}y; our founding year is unrecorded")
        scores.append(sc)
    if req_stage is not None:
        want = "early-stage" if req_stage == "early" else "established"
        if stage is None:
            sc = 0.5
            bits.append(f"the call is for {want} orgs; our stage is unrecorded")
        elif stage == req_stage:
            sc = 1.0
            bits.append(f"the call is for {want} orgs, and so are we")
        else:
            sc = 0.0
            bits.append(f"the call is for {want} orgs only; we are "
                        f"{'early-stage' if stage == 'early' else 'established'}")
        scores.append(sc)
    it = _qfactor("experience", "Experience requirement", active=True,
                  score=min(scores), hard=False)
    it["_detail"] = " · ".join(bits)
    it["fatal"] = False
    return it


def capacity_factors(org: dict, rfp: dict, donor: dict | None = None,
                     org_settings: dict | None = None) -> list[dict]:
    """MUST-3 components (owner 2026-08-06): Financial capacity for this award (the
    value-related composite) + Experience requirement (default-pass when unstated)."""
    org = org or {}
    donor = donor or {}
    return [_financial_capacity_factor(org, rfp, donor),
            _experience_factor(org, donor)]


def derive_capacity(org: dict, rfp: dict, donor: dict | None = None,
                    org_settings: dict | None = None,
                    rfp_compliance: dict | None = None) -> str | None:
    """MUST-3 label over the two components. A HARD ceiling the org EXCEEDS is
    structural → 'No, beyond us' whatever the rest says (it can be masked inside the
    composite mean, so it is checked directly); otherwise the weakest active component
    governs: any 0 → 'No, beyond us' · any partial → 'Yes, but a stretch' · all 1 →
    'Yes, comfortably'. Nothing active → 'Not sure'."""
    eff = _merge_rfp_compliance(donor, rfp_compliance)
    if any(p["hard"] and p["score"] <= 0.0
           for p in _capacity_value_parts(org or {}, rfp, eff)):
        return "No, beyond us"
    scores = [x["score"] for x in capacity_factors(org, rfp, eff, org_settings)
              if x["active"] and x["score"] is not None]
    if not scores:
        return "Not sure"
    if any(s <= 0.0 for s in scores):
        return "No, beyond us"
    if any(s < 1.0 for s in scores):
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
    an inclusive tier (LMIC/global/developing) reachable via the org's own presence.

    The stray-"Global"-tag defence lives at the SOURCE (core.extract.build_record +
    auto_scorer._extract_call_geographic_scope only tag the worldwide tier when a genuine
    worldwide phrase is present), so a "Global / worldwide" that reaches here is trusted —
    we do NOT second-guess it (an earlier strip-when-a-country-is-named heuristic wrongly
    auto-Declined genuinely-worldwide calls that also name a priority country)."""
    cs = list(countries or [])
    sc = _as_list(scope)
    if not cs or not sc:
        return False
    if set(_geo.expand(cs)) & set(_geo.expand(sc)):
        return True
    return bool(_is_inclusive_geo(sc))


def _country_overlap(countries: Any, scope: Any) -> bool:
    """A REAL country/region overlap — the org actually sits inside the stated scope.
    Distinct from `_covers_scope`, which ALSO passes on an inclusive tier
    ("Global / worldwide", LMIC) that is open to everyone regardless of footprint. Used
    to explain WHICH route matched, so the card never claims we are "based in scope"
    when the only thing that matched was an open-to-anyone tier."""
    cs, sc = list(countries or []), _as_list(scope)
    if not cs or not sc:
        return False
    return bool(set(_geo.expand(cs)) & set(_geo.expand(sc)))


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
    """The CALL's geographic scope governs MUST-4; the donor's scope is a FALLBACK used
    ONLY when the call states none — donor intel must never WIDEN an explicit call
    restriction (e.g. a broad donor 'LMIC' scope must not turn an 'India-only' call into a
    pass for an org with no India presence). Deduped (case-insensitive)."""
    raw = (_as_list(rfp.get("call_geographic_scope"))
           or _as_list((donor or {}).get("donor_geographic_scope")))
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
    if not (registered or operation or (org.get("partners") or [])
            or org.get("trusted_partners") or org_us):
        # We do not know where WE are. That is an unconfigured org profile, not a
        # measured absence of reach — and scoring it 0 made MUST-4 a fatal gate that
        # auto-Declined EVERY scoped call for a tenant who had not yet filled in their
        # countries. Not determinable → "Not sure" → Park for review (owner 2026-08-07).
        return {"active": False, "score": None, "label": "Not sure", "scope": scope,
                "via": "our own registered / operating countries are not recorded"}
    if _covers_scope(registered, scope) or (scope_us and org_us):
        # SAY WHICH ROUTE actually matched. `_covers_scope` passes either on a real
        # country overlap OR on an inclusive tier (Global / LMIC / …), and the card used
        # to claim "registered / based in scope" for both. On a call scoped
        # ["Bangladesh", "Global / worldwide"] against an org registered in Cameroon and
        # Mali that reads as a flat falsehood — the country overlap is empty; only the
        # open-to-anyone tier matched (owner 2026-08-06).
        return {"active": True, "score": 1.0, "label": "Yes, our own presence",
                "scope": scope,
                "via": ("registered / based in scope"
                        if (_country_overlap(registered, scope) or (scope_us and org_us))
                        else "call is open to any country — no overlap with our own "
                             "registered countries")}
    if _covers_scope(operation, scope) or _geo_partner_in_scope(org, scope):
        return {"active": True, "score": 0.5, "label": "Yes, via a partner", "scope": scope,
                "via": ("operating country / affiliated partner in scope"
                        if (_country_overlap(operation, scope)
                            or _geo_partner_in_scope(org, scope))
                        else "call is open to any country — no overlap with our own "
                             "operating countries")}
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
        if not v or _explicitly_not_imposed(v):
            continue
        col = _eff_column(k)
        if col in _RFP_VALUED_KEYS:
            if not str(eff.get(col) or "").strip():
                eff[col] = v
        else:
            eff[col] = True
    return eff


# A call can state that a requirement does NOT apply. The merge used to coerce EVERY
# non-empty value to True — and every one of these strings is truthy in Python — so
# "audited financials: not required" ACTIVATED a hard MUST-5 gate and scored it 0.
# Nothing sanitises the model's free-text here (only the MUST-1 enums are checked), so a
# single such emission both failed the call and, via donor_enrich, wrote "yes" into the
# donor record and poisoned that funder for every future call.
_NOT_IMPOSED = frozenset({
    "no", "false", "0", "n/a", "na", "none", "nil", "never", "absent",
    "not required", "not_required", "not applicable", "not_applicable",
    "not stated", "not specified", "unspecified", "unknown", "not mentioned",
})


def _explicitly_not_imposed(v: Any) -> bool:
    """True when a call/donor flag SAYS the requirement does not apply. Booleans are
    left to normal truthiness — only text is interpreted."""
    if isinstance(v, bool):
        return False
    return str(v).strip().lower() in _NOT_IMPOSED


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
            *, fatal: bool = False, active: bool = True,
            score: float | None = None) -> dict:
    d = {"key": key, "name": name, "source": source,
         "met": met, "fatal": fatal, "active": active}
    # Optional explicit 0/0.5/1 component score (e.g. a 3-tier deadline factor). When
    # absent, downstream maps met→score (True→1 · False→0 · None→0.5).
    if score is not None:
        d["score"] = score
    return d


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


# Component verdict symbols — SCORE-driven, and defined HERE (next to the `met`
# derivation above) so the two can't drift apart. `met` is a tri-state that collapses
# EVERY partial score to None, so a UI keying its symbol off `met` alone renders a
# measured 0.5 — a real partial match against real data — with the same "?" as a
# component nothing was ever known about. Those mean opposite things to a reviewer, so
# they get different symbols: ◐ is the middle ground between ✓ and ✗ (owner 2026-08-06).
MARK_MET = ("✓", "#1a7f37")
MARK_PARTIAL = ("◐", "#b8860b")
MARK_FAILED = ("✗", "#c0392b")
MARK_UNKNOWN = ("?", "#8a6d00")


def component_mark(factor: dict) -> tuple[str, str]:
    """(symbol, colour) for one component factor — from its SCORE whenever it has one,
    falling back to the `met` tri-state only when nothing was measurable."""
    sc = factor.get("score")
    if sc is None:
        return {True: MARK_MET, False: MARK_FAILED}.get(factor.get("met"), MARK_UNKNOWN)
    sc = float(sc)
    return MARK_MET if sc >= 1.0 else (MARK_FAILED if sc <= 0.0 else MARK_PARTIAL)


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
        # BLANK is the Settings UI's explicit "Unknown — don't apply this gate" (its own
        # help text says "'Unknown' leaves the gate off"), yet a blank scored 0 on a HARD
        # gate — the app enforcing a requirement the user had switched off. Active only
        # once the org has actually answered yes or no (owner 2026-08-07).
        ("local_board", "Local board",
         _need("donor_local_board_required")
         and str(os.get("org_has_local_board", "")).strip().lower() in ("yes", "no"),
         str(os.get("org_has_local_board", "")).strip().lower() == "yes"),
    ]
    for key, name, active, ok in _hard:
        items.append(_qfactor(key, name, active=active,
                              score=(1.0 if ok else 0.0), hard=True))

    # SAM.gov / UEI registration applies ONLY to US-federal (grants.gov) calls, or to a
    # donor that explicitly demands it. For EVERY other funder it is simply irrelevant, so
    # it is EXCLUDED (owner 2026-08-06) — it must not sit in the denominator at all.
    # It used to be emitted as an ACTIVE permissive pass (score 1). Because it was the
    # only always-active component, MUST-5's active set was never empty, so a call that
    # imposed nothing whatsoever still read "Yes, fully met · 1/1 · 100%" — full MUST-5
    # weight toward Proceed, certified by a default pass on a rule the funder never made.
    items.append(_qfactor("sam_uei", "SAM.gov / UEI registration",
                          active=bool(_need("donor_sam_uei_registration_required")
                                      or _is_us_federal(rfp)),
                          score=(1.0 if _sam_ok else 0.0), hard=True))

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

    # THE ALL-CLEAR (owner 2026-08-06). MUST-5 components are strict eligibility rules,
    # and they only exist when the call or donor intel states them. When NOTHING is
    # stated the honest answer is a full pass — we must not eliminate a strong-fit RFP
    # over data the funder never published — but that pass has to be VISIBLE as one
    # thing, not implied by a permissive default hiding among eleven greyed rows. So a
    # single explicit component carries it, and the Review card shows it alone.
    ac = _qfactor("compliance_all_clear",
                  "All compliance & co-financing requirements met",
                  active=False, score=None, hard=False)
    ac["fatal"] = False
    ac["_detail"] = ("no compliance or co-financing requirement stated by this call "
                     "or donor intel")
    items.append(ac)
    return _settle_all_clear(items)


def _settle_all_clear(items: list[dict]) -> list[dict]:
    """The MUST-5 all-clear default stands ONLY while nothing else is active.

    Re-run AFTER human overrides: `apply_component_overrides` can activate a real
    requirement a reviewer says applies, and the default must then retire rather than sit
    beside it inflating the denominator. A reviewer who scored the all-clear row itself
    is left alone — their verdict wins, as everywhere else."""
    ac = next((i for i in items if i.get("key") == "compliance_all_clear"), None)
    if ac is None or ac.get("_override"):
        return items
    if any(i.get("active") for i in items if i.get("key") != "compliance_all_clear"):
        ac["active"], ac["score"], ac["met"] = False, None, None
    else:
        ac["active"], ac["score"], ac["met"] = True, 1.0, True
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


def below_award_floor(rfp: dict, org: dict | None = None) -> bool:
    """True when the org has set a MINIMUM funding target, the call's award is KNOWN
    (headline value OR extracted range), and it falls BELOW that minimum — a decisive
    'the org would not prefer this' signal. The decision layer uses it to CAP a would-be
    Proceed at Park (an award below the org's floor should go to human review, not
    auto-advance), so a poor funding fit can pull even a perfect-MUST bid down — which the
    0.08 PREFER-6 weight alone cannot do. Unknown award or unset floor → False (never caps
    on missing data)."""
    lo = _num((org or {}).get("org_min_target"))
    val = _usd(rfp)
    return bool(lo and val and val < lo)


def _host_match(a: str, b: str) -> bool:
    """Two portal hosts match if identical OR one is a sub-domain of the other
    (dot-boundary suffix). So a registration on 'gavi.org' credits a call whose
    submission portal is 'portal.gavi.org' (a third-party / sub-domain portal),
    and vice-versa — without matching unrelated hosts like 'notgavi.org'."""
    a, b = (a or "").strip("."), (b or "").strip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _registered_on_portal(org: dict, rfp: dict, donor: dict | None) -> bool:
    """True when the org is FAMILIAR with the donor's application portal — either:
      (a) it has an ACTIVE portal registration whose host matches the donor's / the
          call's portal host (org.donor_registrations vs {donor.submission_portal_url,
          donor.website, rfp link}), sub-domain-aware; OR
      (b) it has a WORKING relationship with this funder — a past/current grantee
          (funder_history), an active donor, or an engaged donor — since having worked
          with a funder implies familiarity with how they take submissions.
    (b) intentionally overlaps PREFER-7 (relationship); here it feeds the PREFER-8
    'familiar with the portal' competitiveness edge (owner 2026-07-20)."""
    d = donor or {}
    # (b) known-funder familiarity — reuse the canonical donor matchers.
    if _is_past_grantee(org, rfp, d):
        return True
    if (_canonical_donor_match(org.get("org_active_donors"), d, rfp)
            or _canonical_donor_match(org.get("org_engaged_donors"), d, rfp)):
        return True
    # (a) explicit portal-registration host match (sub-domain-aware).
    regs = {clean_portal_url(r) for r in (org.get("org_donor_registrations") or []) if r}
    if not regs:
        return False
    portals = {p for p in (clean_portal_url(x) for x in
               (d.get("donor_submission_portal_url"), d.get("donor_website"),
                rfp.get("opportunity_link"))) if p}
    return any(_host_match(r, p) for r in regs for p in portals)


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


def _is_past_grantee(org: dict, rfp: dict, donor: dict | None) -> bool:
    """THE single "have we been funded by this funder before?" test.

    Canonical FIRST: both sides resolve through donor_intel's canonical key, so an
    acronym, an alias, a programme brand and the full legal name all land on the same
    donor. A call published under a programme brand used to miss the funder behind it —
    "Grand Challenges" never matched an org history holding "Bill & Melinda Gates
    Foundation", so PREFER-7 reported "not a grantee" for the org's longest-standing
    funder, and PREFER-8 lost its portal-familiarity edge at the same time.

    The raw-NAME test is kept as a fallback for free-typed funders that are not in the
    donor catalog at all. Used by PREFER-7 (both the label and the components) and by
    PREFER-8's portal familiarity, so those three can never disagree about the same
    fact."""
    hist = [h for h in (org.get("org_funder_history") or []) if h]
    if not hist:
        return False
    return bool(_canonical_donor_match(hist, donor, rfp)
                or _funder_in_history(rfp.get("funding_agency"), hist))


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
    if _is_past_grantee(org, rfp, donor):
        return "Current/past grantee"
    if (_canonical_donor_match(org.get("org_engaged_donors"), donor, rfp)
            or _shared_collaborator(org, donor) or _registered_on_portal(org, rfp, donor)):
        return "Some contact"
    if (not hist and not (org.get("org_donor_registrations") or [])
            and not (org.get("trusted_partners") or [])
            and not (org.get("org_engaged_donors") or [])):
        return None                    # no relationship data on file → Not sure
    return "None"


# A donor decision only exists once the proposal is IN — so any of these proves submission
# just as well as Progress = Completed. Mirrors the app-wide SUBMITTED rule used by the
# Report/Summary (views/report.py::_submitted_mask); keep the two in sync.
_SUBMITTED_DONOR_DECISIONS = {"approved", "under review", "not approved"}


def _is_completed(rfp: dict) -> bool:
    """The submission is already IN, so deadline runway is moot — it was submitted.

    Counts ANY durable proof of submission, not just Progress = Completed:
      * progress_status == Completed;
      * a real donor decision (approved / under review / not approved) — a donor can only
        decide on a proposal it received;
      * a recorded submission date (date_completed).
    This matters for BACKDATED intake: an RFP entered via Excel or the submission form
    months AFTER it went to the donor arrives with a deadline already in the past. Keying
    only on progress_status scored those as "not enough time" (PREFER-9 ✗) even though they
    were submitted on time — see the app-wide rule in views/report.py::_submitted_mask."""
    if str(rfp.get("progress_status") or "").strip().lower() == "completed":
        return True
    if str(rfp.get("donor_decision") or "").strip().lower() in _SUBMITTED_DONOR_DECISIONS:
        return True
    return bool(str(rfp.get("date_completed") or "").strip())


def needs_submission_check(rfp: dict) -> bool:
    """True when an RFP's deadline has ALREADY PASSED but nothing records a submission.

    BACKDATED INTAKE: proposals entered via Excel or the in-app form months after they went
    to the donor arrive with a past deadline. If the row isn't marked submitted, scoring
    reads it as "not enough time" (PREFER-9 ✗) and it looks like a missed opportunity —
    when in reality it was submitted on time and may be under review. The intake surfaces
    call this so the user can confirm: set Progress = Completed, or record the donor
    decision, and the time component counter-validates automatically (see _is_completed).
    """
    if _is_completed(rfp):
        return False
    days = days_until(rfp.get("call_submission_deadline"))
    return days is not None and days < 0


# Sentinel days-to-deadline meaning "the time axis was not measured, so it must not
# penalise" — used when the call was already submitted (the deadline WAS met) and when
# no deadline was ever captured (nothing to miss).
_TIME_NOT_MEASURED = 10_000


def derive_bid_effort(rfp: dict, org_settings: dict | None = None) -> str:
    """PREFER-9 label — NEVER None.

    An unknown deadline means the time axis could not be MEASURED, not that time ran
    out. Returning None left the Review card with no derived value, and
    `scorer.default_response` then fell back to the LAST option in the criterion's
    list — which for bid_effort (the one criterion with no "Not sure" option) is the
    WORST one, "Not enough time, no team". So a call whose deadline was simply never
    extracted scored 0 on PREFER-9 and showed a red badge, while its own component
    panel showed the time check EXCLUDED and the BD team MET (1/1 · 100%). That is not
    cosmetic: the 0 fed the Bid Strength gauge and the Proceed/Park/Decline suggestion.

    Unmeasured time is now EXCLUDED rather than failed — exactly what the Review
    editor's own rule already did (views.review_rfp._bid_rule: "inactive (no deadline)
    → assume ample"), so VIEW mode can no longer disagree with EDIT mode, and the label
    is driven by the resources axis alone."""
    bd = str((org_settings or {}).get("org_has_bd_team", "false")).lower() == "true"
    days = days_until(rfp.get("call_submission_deadline"))
    if _is_completed(rfp) or days is None:
        days = _TIME_NOT_MEASURED
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
    # ACTIVE only when the donor states a requirement AND the org has RECORDED its entity
    # type. `org_entity_type` defaults to "" — an unset profile field, not a declared
    # mismatch — and scoring that 0 auto-Declined on a fact nobody had entered. Absence of
    # ORG data is "Not sure" (excluded); a recorded type that differs is still a real 0.
    ent_req = str(donor.get("donor_entity_type_required") or "").strip().lower()
    org_ent = str(org.get("org_entity_type") or "").strip().lower()
    items.append(_qfactor("entity_type", "Entity type",
                          active=bool(ent_req and org_ent),
                          score=(1.0 if org_ent == ent_req else 0.0),
                          hard=True))

    # --- C. HQ country — the applicant must be HEADQUARTERED in a required country OR
    # REGION. REGION-AWARE: a requirement stated as a region (e.g. "Sub-Saharan Africa") is
    # expanded to its member countries (via geo.expand inside _covers), so an org HQ'd in
    # Cameroon PASSES while one HQ'd in the United States FAILS — the IDRC/ANeSA case
    # ("Organizations headquartered outside sub-Saharan Africa are also not eligible").
    # Both sides go through geo expansion, so a spelling/format variant (US == U.S. ==
    # United States) never mis-fires this HARD gate (which auto-Declines).
    #
    # IMPORTANT: this reads the org's HEADQUARTERS country (org_hq_country), NOT its
    # operating country — an org may OPERATE inside the required region yet be HQ'd
    # elsewhere; org_hq_country must be set to the true HQ (e.g. "United States") for this
    # gate to disqualify it. The fallback to org_country is only for orgs whose HQ ==
    # primary country.
    hq_req = [h for h in _as_list(donor.get("donor_hq_country_required"))
              if str(h).strip() and str(h).strip().lower() != "any"]
    detected = bool(hq_req)
    ohq = str(os.get("org_hq_country") or os.get("org_country") or "").strip()
    # Canonicalize BOTH sides first (SSA / sub-saharan → "Sub-Saharan Africa"; US / U.S. →
    # "United States") so a synonym or format variant still region-expands via _covers.
    _req_canon = [_geo.canonical_geo(h) for h in hq_req]
    _hq_ok = bool(ohq) and _covers(_req_canon, [_geo.canonical_geo(ohq)])
    items.append(_qfactor("donor_hq_country", "HQ country", active=detected,
                          score=(1.0 if _hq_ok else 0.0), hard=True))

    # --- D. Registration region — where the applicant must be INCORPORATED. Active only
    #    on an EXPLICIT rule: a stated registration region, an explicit "Any" (a real
    #    pass), or a US-federal call (SAM/UEI + US incorporation is a genuine registration
    #    requirement, not a geography).
    #
    #    The call's GEOGRAPHIC SCOPE is no longer used as a proxy (owner 2026-08-07).
    #    Where the money is SPENT is not where the applicant must be REGISTERED, and using
    #    it made this item MUST-4 wearing a legal-eligibility label: executed across eight
    #    scopes (Cameroon · India · Global · Sub-Saharan Africa · LMIC · Nigeria+Kenya ·
    #    United States · Bangladesh+Global) against an org registered in Cameroon, item D
    #    and MUST-4 `geo_presence` agreed 8/8. It carried no independent information, it
    #    double-counted geography into Bid Strength, and because `fatal_decline` checks
    #    MUST-1 FIRST the reviewer was told the blocker was "Registration region" when the
    #    real finding was geographic reach.
    #
    #    Geographic reach is NOT lost: MUST-4 still auto-Declines an org with no presence
    #    or partner in scope — with the correct trigger name.
    reg_req = _as_list(donor.get("donor_registration_region"))
    explicit_any = any(r.lower() == "any" for r in reg_req)
    region = [] if explicit_any else list(reg_req)
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

    # --- F. Repeat-applicant restriction (renamed + corrected 2026-08-07, owner-agreed).
    #    This is a RESTRICTION on applying again, not a requirement to have applied
    #    before, and the previous implementation had it exactly backwards: it scored 1
    #    when the org WAS a prior beneficiary and 0 when it was not, for ALL FOUR rule
    #    values. So `eligible` — whose own help text is "prior grantees explicitly
    #    welcome (no penalty)" — auto-DECLINED every org that had not previously been
    #    funded, and the three `ineligible_*` rules passed exactly the orgs they exist
    #    to bar.
    #
    #    The vocabulary (core.llm_synthesis._MUST1_ENUMS) means:
    #      eligible            — prior grantees welcome        → no restriction at all
    #      ineligible_current  — CURRENT grantees may not apply → bars org_active_donors
    #      ineligible_previous — PAST grantees may not apply    → bars org_funder_history
    #      ineligible_any      — both are barred
    #    Blank → not stated → excluded (a brand-new donor is never auto-declined).
    #
    #    Named "Repeat-applicant restriction" so it reads as an eligibility rule rather
    #    than a duplicate of PREFER-7's relationship advantage — they measure the same
    #    fact for opposite purposes and were being confused for each other.
    rule = str(donor.get("donor_prior_beneficiary_rule") or "").strip().lower()
    current = _canonical_donor_match(org.get("org_active_donors"), donor, rfp)
    past = _canonical_donor_match(org.get("org_funder_history"), donor, rfp)
    if rule == "ineligible_current":
        sc, active_f = (0.0 if current else 1.0), True
    elif rule == "ineligible_previous":
        sc, active_f = (0.0 if past else 1.0), True
    elif rule == "ineligible_any":
        sc, active_f = (0.0 if (current or past) else 1.0), True
    else:
        # "eligible" states there is NO restriction; blank states nothing at all.
        # Neither is a test the org can fail, so neither is scored.
        sc, active_f = None, False
    items.append(_qfactor("prior_beneficiary", "Repeat-applicant restriction",
                          active=active_f, score=sc, hard=True, default=False))

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
    """MUST-3 sub-factors = the 2 capacity components (see capacity_factors)."""
    return capacity_factors(org, rfp, donor, org_settings)


def _relationship_factors(org: dict, rfp: dict, donor: dict | None = None) -> list[dict]:
    # Match the funder history CANONICALLY, not by raw name. `_funder_in_history` compares
    # `rfp.funding_agency` as a STRING, so a call published under a programme brand missed
    # the funder behind it: "Grand Challenges" never matched an org history holding "Bill &
    # Melinda Gates Foundation", and PREFER-7 read "not a grantee" for the org's single
    # longest-standing funder. `_canonical_donor_match` already resolves both sides through
    # donor_intel's canonical key (acronym ⇄ alias ⇄ full name), which is how MUST-5's
    # authorized-signatory and PREFER-8's portal checks match the same fact — PREFER-7 was
    # the odd one out. The raw-name test is KEPT as a fallback for free-typed funders that
    # are not in the donor catalog at all.
    grantee = _is_past_grantee(org, rfp, donor)
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
    """PREFER-9 sub-factors: time-to-deadline × a business-development team.

    Time is a 3-TIER score, not a yes/no: >14d = 1.0 (ample) · 7-14d = 0.5 (tight) ·
    <7d = 0.0 (not enough). PREFER-9's classification is the BANDED AVERAGE of the two
    components (see core.scorer._SCORE_MAP / review_rfp._bid_rule), so a business-dev
    team can lift a tight (0.5) deadline to a partial PREFER-9.

    NO DEADLINE CAPTURED → the time component is EXCLUDED: "?", active=False, out of the
    denominator. There was nothing to judge, so it must not be scored in EITHER
    direction, and PREFER-9 rests on the one component that IS measurable — the BD team
    — giving a denominator of 1 (owner 2026-08-06).

    A permissive default pass (✓ "no restriction") was tried here and REJECTED: it reads
    as a positive finding about time on a call whose deadline was never captured, which
    is the same overclaim as the "Not enough time" it replaced, just in the opposite
    direction. Excluding it is the same treatment every other unstated component gets.
    The consequence is accepted deliberately: with NO BD team the label
    ("Ample time, but no dedicated team", worth 50%) sits beside a 0/1 ratio, because a
    fixed 6-label scale has no wording for "team missing, time unknown"."""
    osx = org_settings or {}
    bd = str(osx.get("org_has_bd_team", "false")).lower() == "true"
    if _is_completed(rfp):
        # Already submitted → the time gate was met; show it as full, not "not enough".
        time_name, time_score, time_active = "Submitted on time (already completed)", 1.0, True
    else:
        days = days_until(rfp.get("call_submission_deadline"))
        # The component NAME states the tier actually reached, not the whole scale. A
        # static "(>14d full · 7-14d partial)" advertised two bands on every row, hiding
        # the third (<7d = 0) and telling a reader nothing about THIS call (owner
        # 2026-08-06).
        time_name = "Time before the deadline"
        if days is None:
            time_score, time_active = None, False      # nothing to judge → "?" excluded
        elif days > BID_EFFORT_AMPLE_DAYS:            # > 14 days
            time_name += " (>14d full)"
            time_score, time_active = 1.0, True
        elif days >= BID_EFFORT_TIGHT_DAYS:           # 7-14 days
            time_name += " (7-14d partial)"
            time_score, time_active = 0.5, True
        elif days >= 0:                                # < 7 days
            time_name += " (<7d extremely tight)"
            time_score, time_active = 0.0, True
        else:
            # Past due. Scores 0 like any <7d row, but must not be CALLED "extremely
            # tight" — there is no time left at all. (Past-deadline calls are caught
            # upstream and don't reach the dashboard; this keeps the row honest if one
            # ever does.)
            time_name += " (deadline has passed)"
            time_score, time_active = 0.0, True
    # met is the legacy tri-state for read-mode cards: 1.0→✓ · 0.0→✗ · 0.5/None→?
    time_met = (None if time_score is None
                else True if time_score >= 1.0 else False if time_score <= 0.0 else None)
    return [
        _factor("bid_time", time_name, "R", time_met, active=time_active, score=time_score),
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
    # MUST-1 — identity / qualification gates.
    for f in qualification_factors(org, rfp, eff, org_settings):
        if f["active"] and f["met"] is False:
            return True, f["name"]
    # MUST-2 (strategic fit) is intentionally NOT a hard auto-Decline gate: unlike legal
    # status / geography / a budget ceiling, an off-strategy call is not a STRUCTURAL
    # impossibility, and the strategic component's 0/0.5/1 band can't cleanly separate
    # "org works here but deprioritises it" (rating ≤1 → band 0) from true zero overlap.
    # A hard gate here would auto-Decline and HIDE otherwise-strong calls from the review
    # queue. Its heavy composite weight already pushes an off-strategy call to Park/Decline
    # by overall strength, so it stays reviewable — matching the "see & review all" model.
    # MUST-3 — a HARD capacity ceiling the org exceeds (annual-budget / prior-grant
    # ceiling) is a structural ineligibility the org cannot shrink before the deadline.
    # Read from the composite's SUB-PARTS: the presented component is the mean of the
    # value checks, so a failed ceiling can average away to a passing-looking 0.5.
    # (Soft items — experience / award-absorption — are NOT gates.)
    for p in _capacity_value_parts(org, rfp, eff):
        if p["hard"] and p["score"] <= 0.0:
            return True, p["name"]
    # MUST-4 — no geographic reach (own presence or a partner in the call's scope).
    if derive_geographic_fit(org, rfp, org_settings, eff) == "No presence there":
        return True, "Geographic reach (no presence or partner)"
    # MUST-5 has NO structural auto-Decline gate (owner 2026-06-30): the hard credential
    # gates are all acquirable before the deadline, and funding-route / funding-platform
    # moved to PREFER-8 (Bid Strength only). So MUST-5 never forces a decline here.
    return False, None


def apply_component_overrides(breakdown: dict[str, list[dict]],
                              overrides: dict | None) -> dict[str, list[dict]]:
    """Merge HUMAN component verdicts on top of the derived breakdown, in place.

    `overrides` is {criterion_key: {component_key: score}} as persisted on
    rfp_submissions.criteria_component_overrides. A reviewer's verdict WINS over the
    derivation — the derivation is an inference from org profile / donor intel / call text
    and can be wrong or stale, whereas a human who has read the call is authoritative.
    Overridden factors are stamped `_override: True` so the UI can show who decided.

    An override on an INACTIVE component also ACTIVATES it: if a reviewer scores something
    the call didn't visibly impose, they're asserting it applies."""
    if not overrides or not isinstance(overrides, dict):
        return breakdown
    for crit, comps in overrides.items():
        if not isinstance(comps, dict):
            continue
        for it in breakdown.get(crit) or []:
            if str(it.get("key")) not in comps:
                continue
            try:
                sc = float(comps[str(it.get("key"))])
            except (TypeError, ValueError):
                continue
            sc = max(0.0, min(1.0, sc))
            it["score"] = sc
            it["met"] = True if sc >= 1.0 else (False if sc <= 0.0 else None)
            it["active"] = True
            it["_override"] = True
    return breakdown


def factor_breakdown(rfp: dict, org: dict | None = None, donor: dict | None = None,
                     org_settings: dict | None = None,
                     rfp_compliance: dict | None = None,
                     overrides: dict | None = None) -> dict[str, list[dict]]:
    """Per-criterion sub-factor lists for the Review per-criterion cards — for ALL
    9 criteria. Each factor carries `active` (whether THIS call/donor imposes it):
    inactive factors are KEPT so the card can show them as "? Not applicable", and
    they're excluded from the won/total denominator (see core.criteria_factors).

    `overrides` — persisted human component verdicts, applied LAST so they win over the
    derivation (see apply_component_overrides). MUST-5's all-clear default is then
    re-settled, since an override can activate a requirement the derivation didn't see."""
    org = org or {}
    eff = _merge_rfp_compliance(donor, rfp_compliance)
    out = {
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
    out = apply_component_overrides(out, overrides)
    _settle_all_clear(out["cofinancing"])
    return out
