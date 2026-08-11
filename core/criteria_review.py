"""Review-screen criterion presentation — the label, the count, and the roll-up rules.

Extracted from `views/review_rfp.py` so it can be TESTED. The page imports Streamlit and
runs the auth gate at import time, so none of this was reachable from a unit test; the
rules below decide what a reviewer sees for all nine criteria, which is not something to
leave untested.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
A criterion's label and the component panel beneath it must never contradict each other.
They used to, in two different ways:

  1. The label was read from the DATABASE whenever the row had been reviewed, while the
     components were recomputed live on every render. A human's submit-time answer
     therefore froze the label while the components moved underneath it — PREFER-9 read
     "Tight but doable, with a team" beside its own components at 2/2 · 100%. That is
     fixed by never reading the stored label: see `criterion_label`.

  2. A human component verdict (`criteria_component_overrides`) was applied to the panel
     but NOT to the label, so overriding a component visibly changed the panel and left
     the label saying the opposite.

WHICH LABEL WINS
----------------
Two functions in this codebase can name a criterion:

  * `core.criteria_derive.derive_*` — the authoritative derivation. Richer: it reads the
    org profile, donor intel and call text directly, and for PREFER-6/PREFER-8 it applies
    weightings the component list does not carry.
  * the ROLL_UP rules below — a function of the COMPONENT SCORES only. This is the only
    one that can see a human's component override.

For seven of the nine criteria the two agree exactly (verified across the live table), so
either may be used. For `funding_quality` and `competitiveness` they are genuinely
DIFFERENT FORMULAS and disagree on 141 and 65 live rows respectively — the derivation
gates PREFER-6 on "can the award be sized at all" and weights PREFER-8's track record at
1.5 against a flat mean here.

So: the derivation names the criterion, EXCEPT where a human has overridden one of its
components — then the rules do, because a reviewer's verdict has to be able to move the
label. Overriding is the only way to reach the cruder formula, and it is a deliberate act.

...EXCEPT AGAIN for `funding_quality` and `competitiveness`, where the DERIVATION IS
AUTHORITATIVE unconditionally (owner 2026-08-10). Their derivations are not roll-ups at
all: PREFER-6 gates on whether the award can be sized before it looks at anything else,
and PREFER-8 is a weighted accumulator — the track record counts 1.5 and unmet donor
requirements SUBTRACT. A flat mean over the component list cannot express either, so
letting the mean name the criterion would replace a considered model with a cruder one the
moment somebody touched a component. See DERIVATION_AUTHORITATIVE.

The consequence is deliberate and must stay VISIBLE: for those two the component ratio can
disagree with the label beside it (65 live rows do, e.g. "Moderate" next to 3/4 · 75%).
That is not the frozen-label defect — both numbers are live, they are just measuring
different things — so `label_source_note` explains it in the panel rather than hiding
either one.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

# Criteria whose components are ALTERNATIVE ROUTES rather than requirements to be met
# together: one satisfied route IS the whole criterion, so an AND-style mean over all
# tiers would report the best possible outcome as 1/3 · 33%.
OR_KEYS = frozenset({"funder_relationship"})

# Criteria counted as Σ(component scores) ÷ activated components (a gate roll-up).
SUM_OVER_ACTIVE = frozenset({"qualification", "capacity", "cofinancing"})

# Criteria whose rule is a GATE rather than a ratio: any unmet active component decides the
# verdict outright (see `_cofin_rule`). They must not display a percentage, because the
# percentage looks like partial credit and the score gives none. Contrast PREFER-8, where
# 5 of 6 components genuinely earns the top band.
ALL_OR_NOTHING = frozenset({"cofinancing"})

# Criteria that are ONE component scored 0 / 0.5 / 1.
SINGLE_COMPONENT = frozenset({"strategic_fit", "geographic_fit"})

# Criteria the DERIVATION always names — a human component override cannot rename them
# (owner 2026-08-10). Their derivations are considered models, not roll-ups:
#   funding_quality — gates on whether the award can be sized at all, then reads the org's
#                     configured min/ceiling band, falling back to absolute award tiers.
#   competitiveness — a weighted accumulator: track record scores 1.5, and unmet donor
#                     requirements (grassroots, local board, co-financing, HQ) SUBTRACT.
# A flat mean over the component list expresses neither, so the mean must not be allowed to
# replace them. Overrides on their components are still recorded and still shown in the
# panel — they inform the count and the reviewer's own reading — they just don't rename the
# criterion. `label_source_note` says so on screen.
DERIVATION_AUTHORITATIVE = frozenset({"funding_quality", "competitiveness"})

# THE ONE COMPONENT A REVIEWER MAY NOT ACTIVATE.
# Every other component is editable, on the principle that a human who has read the call
# outranks the derivation — including asserting a requirement the extractor missed.
# SAM.gov / UEI is different in kind: it is a US-federal contractor registration, and for
# a funder that is not a US government body there is no fact a reviewer could learn from
# the call that would make it relevant. Leaving it editable would let it be scored 0 and
# drag MUST-5 down over a rule the funder never made. It stays greyed and out of the
# denominator unless the DERIVATION activates it (a grants.gov / US-federal call, or a
# donor record that explicitly demands it) — at which point it is editable like the rest.
NEVER_ACTIVATABLE = frozenset({"sam_uei"})


def is_editable(it: dict) -> bool:
    """May a reviewer set this component's value? Everything except an excluded-by-scope
    component the derivation has not activated (see NEVER_ACTIVATABLE)."""
    return bool(it.get("active")) or str(it.get("key")) not in NEVER_ACTIVATABLE


# Shown in the count slot when a criterion has nothing to count. It must NOT name a
# decision: "Proceed" / "Park" / "Decline" are the FINAL verdict for the whole
# opportunity, and a criterion-level "Park" read as though this one criterion had been
# parked. It also contradicted the Bid Strength breakdown, which was busy crediting the
# criterion its full weight at the same moment.
NOT_SCORED = "not scored"


def snap(v: Any) -> float:
    """Coerce any input to the nearest allowed component score: 0 / 0.5 / 1."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1.0, round(v * 2) / 2))


def component_score(it: dict) -> float:
    """A component's editable 0/0.5/1 score. MUST score-factors carry `score`; the
    PREFER met-based factors don't, so map met → score (True→1 · False→0 · None→0.5)
    so both kinds render in the same numeric component editor."""
    sc = it.get("score")
    if sc is not None:
        return snap(sc)
    met = it.get("met")
    return 1.0 if met is True else (0.0 if met is False else 0.5)


def is_scored(it: dict) -> bool:
    """Was this component actually MEASURED? A component with neither a score nor a
    met verdict is "not sure" — it is excluded from both numerator and denominator, and
    it must render as an em dash rather than 0.0 (which reads as a measured failure)."""
    return it.get("score") is not None or it.get("met") is not None


# ── roll-up rules: component scores → the criterion's response label ──────────────
# Labels MUST be members of core.scorer.CRITERION_RESPONSES[key] so a Save stores a
# valid value. Two need keyed access (`by_key`): relationship (grantee outranks contact)
# and bid-effort (a time x team matrix).
def _qual_rule(scores: list[float], by_key=None) -> str:
    """MUST-1: any 0 → No · any 0.5 → Mostly · all 1 → Yes."""
    if any(s <= 0.0 for s in scores):
        return "No, not eligible"
    if any(s == 0.5 for s in scores):
        return "Mostly, one item unclear"
    return "Yes, fully"


def _strat_rule(scores: list[float], by_key=None) -> str:
    """MUST-2: BEST-aligned theme wins — 1 → Strongly · 0.5 → Limited · else Off."""
    best = max(scores, default=0.0)
    return {1.0: "Strongly aligns", 0.5: "Limited priority"}.get(best, "Off-strategy")


def _cap_rule(scores: list[float], by_key=None) -> str:
    """MUST-3 (gate, like MUST-1): any 0 → beyond us · any 0.5 → stretch · all 1 →
    comfortably."""
    if any(s <= 0.0 for s in scores):
        return "No, beyond us"
    if any(s == 0.5 for s in scores):
        return "Yes, but a stretch"
    return "Yes, comfortably"


def _geo_rule(scores: list[float], by_key=None) -> str:
    """MUST-4: single tiered component — 1 → own presence · 0.5 → via a partner · else
    no presence."""
    best = max(scores, default=0.0)
    return {1.0: "Yes, our own presence",
            0.5: "Yes, via a partner"}.get(best, "No presence there")


def _cofin_rule(scores: list[float], by_key=None) -> str:
    """MUST-5 — Met / Not Met framing (it spans co-financing AND the compliance gates).
    ANY unmet active component → 'Not met' · any 0.5 → 'Partial, with effort' · all 1 →
    'Yes, fully met'."""
    if any(s <= 0.0 for s in scores):
        return "Not met"
    if any(s == 0.5 for s in scores):
        return "Partial, with effort"
    return "Yes, fully met"


def _fq_rule(scores: list[float], by_key=None) -> str:
    """PREFER-6 funding quality: ratio of active size-fit components."""
    if not scores:
        return "Not sure"
    r = sum(scores) / len(scores)
    return "High" if r >= 0.75 else ("Moderate" if r >= 0.4 else "Low")


def _rel_rule(scores: list[float], by_key=None) -> str:
    """PREFER-7 donor relationship (OR-tiers): grantee is strongest, then any contact."""
    bk = by_key or {}
    if bk.get("rel_grantee", 0.0) >= 1.0:
        return "Current/past grantee"
    if bk.get("rel_grantee", 0.0) >= 0.5 or bk.get("rel_contact", 0.0) >= 0.5:
        return "Some contact"
    return "None"


def _comp_rule(scores: list[float], by_key=None) -> str:
    """PREFER-8 competitiveness: track record dominates, else the overall signal ratio."""
    if not scores:
        return "Not sure"
    bk = by_key or {}
    r = sum(scores) / len(scores)
    track = bk.get("comp_track")
    if (track is not None and track >= 1.0) or r >= 0.66:
        return "Strong (limited field / incumbent / clear edge)"
    if (track is not None and track >= 0.5) or r >= 0.34:
        return "Moderate"
    return "Weak (wide-open)"


def _bid_rule(scores: list[float], by_key=None) -> str:
    """PREFER-9 bid effort: a time x business-development-team matrix."""
    bk = by_key or {}
    t = bk.get("bid_time", 1.0)               # inactive (no deadline) → assume ample
    has_team = bk.get("bid_team", 0.0) >= 1.0
    if t >= 1.0:
        return ("Ample time, sufficient resources" if has_team
                else "Ample time, but no dedicated team")
    if t >= 0.5:
        return ("Tight but doable, with a team" if has_team
                else "Tight, and no dedicated team")
    return ("Not enough time, even with a team" if has_team
            else "Not enough time, no team")


ROLL_UP_RULES: dict[str, Callable[..., str]] = {
    "qualification": _qual_rule, "strategic_fit": _strat_rule, "capacity": _cap_rule,
    "geographic_fit": _geo_rule, "cofinancing": _cofin_rule,
    "funding_quality": _fq_rule, "funder_relationship": _rel_rule,
    "competitiveness": _comp_rule, "bid_effort": _bid_rule,
}


def active_components(facts: Iterable[dict]) -> list[dict]:
    return [f for f in (facts or []) if f.get("active")]


def with_session_edits(facts: Iterable[dict],
                       edits: dict[str, float] | None) -> list[dict]:
    """Copy of `facts` with a reviewer's IN-PROGRESS component values applied, so the
    label and the count move as they edit — before anything is saved.

    Setting a value on a component the call never imposed ACTIVATES it: the reviewer is
    asserting the requirement applies, and it then counts in both numerator and
    denominator. This mirrors `criteria_derive.apply_component_overrides`, which is what
    happens to the same edit once it is persisted, so the pre-save and post-save views
    agree.

    `edits` holds only components the reviewer actually touched:

        0 / 0.5 / 1  a score  — ACTIVATES the component and counts it
        None ("—")   CLEARED  — "do not score this", so it deactivates and leaves the
                                denominator

    A component they never touched carries NO entry, and the derivation keeps driving it.
    The cleared case matters: a reviewer who sets a scan-scored component back to "—" is
    saying the system should not have scored it, and until that was honoured the derived
    score simply reappeared while the box sat on "—".
    """
    out = [dict(f) for f in (facts or [])]
    if not edits:
        return out
    for f in out:
        ck = str(f.get("key"))
        if ck not in edits:
            continue
        val = edits[ck]
        if val is None:                     # explicitly cleared
            f["score"] = None
            f["met"] = None
            f["active"] = False
            f["_override"] = True
            f["_cleared"] = True
            continue
        sc = snap(val)
        f["score"] = sc
        f["met"] = True if sc >= 1.0 else (False if sc <= 0.0 else None)
        f["active"] = True
        f["_override"] = True
    return out


def has_human_override(facts: Iterable[dict]) -> bool:
    """True when a reviewer's saved verdict is present on an ACTIVE component. An
    override also activates its component (see criteria_derive.apply_component_overrides),
    so a human asserting a requirement the call never stated counts here too."""
    return any(f.get("_override") for f in active_components(facts))


def roll_up(key: str, facts: Iterable[dict],
            session_scores: dict[str, float] | None = None) -> str | None:
    """The criterion label implied by its COMPONENT scores, or None when no component is
    active (nothing imposed → the components cannot name the criterion).

    `session_scores` — {component_key: score} for in-progress edits, so the label moves
    with the reviewer's keystrokes before anything is saved.
    """
    act = active_components(facts)
    if not act:
        return None
    # A CLEARED component arrives as None and has already been deactivated in `facts`, so
    # it is not in `act` at all; guard anyway so a stray None can never reach snap().
    session_scores = {k: v for k, v in (session_scores or {}).items() if v is not None}
    by_key: dict[str, float] = {}
    for f in act:
        ck = str(f.get("key"))
        by_key[ck] = snap(session_scores.get(ck, component_score(f)))
    return ROLL_UP_RULES[key](list(by_key.values()), by_key)


def criterion_label(key: str, facts: Iterable[dict], derived_label: Any,
                    session_scores: dict[str, float] | None = None,
                    *, force_roll_up: bool = False) -> str:
    """THE label for one criterion, in both view and edit mode.

    Never reads the stored column. The row's saved label is a historical record of what
    someone answered at submit time; using it as the display value is what let a frozen
    label sit beside live components (see the module docstring).

    The derivation names the criterion, except where a reviewer has overridden a
    component (or is editing one now) — then the component roll-up does, because a human
    verdict has to be able to move the label it is displayed beside.

    For DERIVATION_AUTHORITATIVE criteria (PREFER-6 / PREFER-8) that exception does NOT
    apply: their derivations are weighted models a flat component mean cannot express, so
    the mean must never replace them. `force_roll_up` still wins, so a caller that has
    genuinely no derived label to show can fall back.
    """
    rolled = roll_up(key, facts, session_scores)
    # `session_scores` may be all-None (every touched component cleared), which is still a
    # human intervention — has_human_override() catches it via the _override stamp.
    _human = (force_roll_up or bool(session_scores) or has_human_override(facts)
              or any(f.get("_cleared") for f in (facts or [])))
    if key in DERIVATION_AUTHORITATIVE and not force_roll_up:
        _human = False
    if rolled is not None and _human:
        return rolled
    if derived_label not in (None, ""):
        return str(derived_label)
    return rolled if rolled is not None else "Not sure"


def label_source_note(key: str, facts: Iterable[dict], label: Any) -> str:
    """Why the label and the component count can differ for this criterion — "" when they
    can't, so the note only appears where it is needed.

    Only DERIVATION_AUTHORITATIVE criteria produce one, and only when the component roll-up
    would actually have said something else. Without this the card looks like the
    frozen-label defect all over again ("Moderate" beside 3/4 · 75%), when in fact both
    numbers are live and simply measure different things.
    """
    if key not in DERIVATION_AUTHORITATIVE:
        return ""
    rolled = roll_up(key, facts)
    if rolled is None or str(rolled) == str(label):
        return ""
    if key == "competitiveness":
        return ("The label comes from the weighted competitiveness model — track record "
                "counts 1.5×, and unmet donor requirements subtract — not from this "
                f"component ratio, which on its own would read “{rolled}”.")
    return ("The label comes from the funding-quality model, which sizes the award "
            "against your configured targets before weighing anything else, not from "
            f"this component ratio, which on its own would read “{rolled}”.")


def criterion_count(key: str, facts: Iterable[dict], label: Any = None
                    ) -> tuple[str, int, int]:
    """(numerator_display, denominator, percent) for the criterion's count slot.

    A denominator of 0 means nothing was counted — the caller must render `NOT_SCORED`
    rather than "0/0 · 0%", and must never name a decision there.
    """
    act = active_components(facts)
    if key in SUM_OVER_ACTIVE:
        # Σ component scores ÷ activated components (NOT benefit-of-doubt won/total).
        num = sum((f.get("score") or 0) for f in act)
        total = len(act)
    elif key in SINGLE_COMPONENT:
        it0 = act[0] if act else None
        num = (it0.get("score") or 0) if it0 else 0
        total = 1 if act else 0
    elif key in OR_KEYS and any(f.get("met") is True for f in act):
        # Satisfied OR-criterion: the panel itself labels the unused tiers "not needed",
        # so they must not sit in the denominator. One satisfied route IS the criterion.
        return "1", 1, 100
    else:
        # MEASURABLE components only: one we can't tell from the call OR donor intel is
        # excluded from BOTH numerator and denominator, never a benefit-of-doubt "win".
        meas = [f for f in act if is_scored(f)]
        num = sum((f["score"] if f.get("score") is not None else (1.0 if f["met"] else 0.0))
                  for f in meas)
        total = len(meas)
    pct = round(num / total * 100) if total else 0
    return f"{num:g}", total, pct


def count_text(key: str, facts: Iterable[dict], label: Any,
               label_is_unsure: bool) -> str:
    """The count slot exactly as it appears in the card title: "2/2 · 100%", or
    `NOT_SCORED` when there is nothing to count.

    `label_is_unsure` — the criterion's own label is "Not sure" (criterion_score is
    None). For funding_quality that happens when the award cannot be sized at all, and a
    contradictory "0/1 · 0%" beside it is worse than saying nothing was scored.
    """
    _num, total, pct = criterion_count(key, facts, label)
    if not total or (key == "funding_quality" and label_is_unsure):
        return NOT_SCORED
    # `criterion_count` formats the numerator for display (SUM_OVER_ACTIVE criteria can
    # yield "1.5"), so compare numerically rather than trusting the type.
    try:
        _shortfall = float(total) - float(_num) > 0
    except (TypeError, ValueError):
        _shortfall = False
    if key in ALL_OR_NOTHING and _shortfall:
        # A PERCENTAGE HERE MISLEADS. This criterion's rule is a gate: any unmet component
        # makes the verdict "Not met", which scores zero points. Printing "2/3 · 67%" beside
        # "0.0/10" invited the reading that the points were miscalculated — they are not, but
        # the two numbers measure different things, and only one of them drives the score.
        # Naming what is unmet says the same thing without implying partial credit.
        missing = float(total) - float(_num)
        n = int(missing) if float(missing).is_integer() else missing
        return f"{n} unmet · all required"
    return f"{_num}/{total} · {pct}%"
