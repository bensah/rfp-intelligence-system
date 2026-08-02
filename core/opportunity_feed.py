"""Opportunity-feed classification — the brain behind the live right-rail cards.

Pure/deterministic (pass `today` for tests). Given a list of rfp_submissions rows, it
splits them into three ranked top-5 feeds shown side-by-side with the Entity view and on
the Pipeline page:

  * TOP FUNDING   — the headline opportunities, fit-agnostic: ranked by a prominence
                    score = funding envelope + geographic breadth + deadline urgency.
                    (Shows the biggest/most-urgent calls whether or not the entity qualifies.)
  * TOP MATCHES   — strong fit: auto-recommendation / decision is Proceed or Park, or the
                    alignment score clears the strong-fit bar. Ranked by alignment.
  * ALSO INTERESTING — everything else that isn't a match and isn't expired, freshest first.

Reads only rfp_submissions fields, so it works off whatever is in the current entity's
pipeline (Option-C screening lands the whole shared pool there, mostly Decline for a thin
profile — which is exactly why Top Funding can surface calls the entity doesn't match).
"""
from __future__ import annotations

import math
from datetime import date, datetime

STRONG_FIT = 65.0          # alignment_score at/above this = a strong fit
_TOP_N = 5
_PROCEED_PARK = {"proceed", "park"}

# Geographic-breadth keywords → breadth score (broader scope = more widely open).
_GLOBAL = ("global", "worldwide", "any country", "all countries", "all eligible countries")
_BROAD = ("sub-saharan", "ssa", "lmic", "low- and middle", "low and middle", "africa",
          "asia", "latin america", "developing countries", "multi-country",
          "multiple countries", "regional", "eligible countries", "oda")


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _amount_score(amount: float) -> float:
    """Log-scaled 0..1 (≈1 at $1B)."""
    if amount <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(amount + 1) / 9.0))


def _geo_text(scope) -> str:
    if isinstance(scope, (list, tuple)):
        return " ".join(str(s) for s in scope).lower()
    return str(scope or "").lower()


def _geo_score(scope) -> float:
    t = _geo_text(scope)
    if not t:
        return 0.2
    if any(k in t for k in _GLOBAL):
        return 1.0
    if any(k in t for k in _BROAD):
        return 0.7
    # comma / "and" separated list of places → multi-country
    if "," in t or " and " in t:
        return 0.5
    return 0.35


def _parse_date(v):
    if not v:
        return None
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _days_until(deadline, today: date):
    d = _parse_date(deadline)
    return (d - today).days if d else None


def _urgency_score(days) -> float:
    """Sooner (but not past) = more urgent. 1.0 today → 0.0 at ~120 days out."""
    if days is None:
        return 0.15               # unknown deadline → mild
    if days < 0:
        return 0.0                # expired
    return max(0.0, 1.0 - min(days, 120) / 120.0)


def _is_expired(deadline, today: date) -> bool:
    d = _parse_date(deadline)
    return d is not None and d < today


def _recency_key(row) -> str:
    return str(row.get("date_posted") or row.get("search_date")
               or row.get("created_at") or "")


def _item(row, today: date) -> dict:
    amt = _f(row.get("call_award_value"))
    days = _days_until(row.get("call_submission_deadline"), today)
    return {
        "uid": row.get("uid"),
        "title": row.get("opportunity_title") or "(untitled opportunity)",
        "funder": row.get("funding_agency") or "",
        "amount": amt,
        "currency": row.get("currency") or "USD",
        "deadline": (str(row.get("call_submission_deadline"))[:10]
                     if row.get("call_submission_deadline") else None),
        "days_until": days,
        "geo": (_geo_text(row.get("call_geographic_scope")) or ""),
        "alignment": _f(row.get("alignment_score")),
        "link": row.get("opportunity_link") or "",
        "recommendation": (row.get("decision") or row.get("auto_recommendation") or ""),
        "_prominence": (0.5 * _amount_score(amt)
                        + 0.3 * _geo_score(row.get("call_geographic_scope"))
                        + 0.2 * _urgency_score(days)),
    }


def _is_match(row) -> bool:
    rec = str(row.get("auto_recommendation") or "").strip().lower()
    dec = str(row.get("decision") or "").strip().lower()
    return (rec in _PROCEED_PARK or dec in _PROCEED_PARK
            or _f(row.get("alignment_score")) >= STRONG_FIT)


def _row_key(r: dict):
    """Stable identity for cross-bucket dedup — uid, else link, else title."""
    return (r.get("uid") or r.get("opportunity_link")
            or r.get("opportunity_title") or id(r))


def _prominence_of(r: dict, today: date) -> float:
    days = _days_until(r.get("call_submission_deadline"), today)
    return (0.5 * _amount_score(_f(r.get("call_award_value")))
            + 0.3 * _geo_score(r.get("call_geographic_scope"))
            + 0.2 * _urgency_score(days))


def _geo_ineligible(geo_reject, row) -> bool:
    """True when `geo_reject` says the call's geography HARD-excludes the org. Fail-OPEN:
    any error → False (keep showing) so a bug in the gate empties nothing — worst case we
    fall back to today's fit-agnostic behaviour rather than a blank card."""
    if geo_reject is None:
        return False
    try:
        return bool(geo_reject(row))
    except Exception:
        return False


def classify(rows: list[dict], today: date | None = None,
             geo_reject=None) -> dict[str, list[dict]]:
    """Split rows into {'top_funding', 'top_matches', 'other'} — each a ranked ≤5 list of
    item dicts. The three cards are MUTUALLY EXCLUSIVE: every opportunity appears in AT
    MOST ONE, at its single best placement, so the rail never shows the same call twice.
    Priority: Top Matches (strong fit — most actionable) → Top Funding (biggest/most-urgent
    of what's left) → Also Interesting (freshest of the rest). Live deadlines only.

    `geo_reject` (optional callable row→bool) is the org's HARD geographic gate — e.g.
    auto_scorer.geographic_exclusion_reject bound to the tenant's policies. When given, the
    two FIT-AGNOSTIC discovery cards (Top Funding, Also Interesting) EXCLUDE calls whose
    geography hard-excludes the org (so a Congo-DRC tenant never sees a Samoa-only call),
    while calls that are global / non-geo-tagged / inclusive stay (the gate keeps those),
    which is exactly the "if none match, feature non-geo-tagged" fallback. Top Matches is
    NOT geo-filtered — a human Proceed/Park or strong-alignment row is shown regardless."""
    today = today or date.today()
    rows = rows or []
    live = [r for r in rows if not _is_expired(r.get("call_submission_deadline"), today)]
    # Fit-agnostic discovery pool, minus hard geo-mismatches. Global / non-geo-tagged /
    # inclusive calls are NOT rejected by the gate, so they remain as the natural fallback.
    discover = [r for r in live if not _geo_ineligible(geo_reject, r)]

    used: set = set()

    def _take(candidate_rows, sort_key, n: int = _TOP_N) -> list[dict]:
        out: list[dict] = []
        for r in sorted(candidate_rows, key=sort_key, reverse=True):
            k = _row_key(r)
            if k in used:
                continue                       # already placed in a higher-priority card
            used.add(k)
            out.append(_item(r, today))
            if len(out) >= n:
                break
        return out

    # 1. Top Matches — strong fit; the most actionable placement wins the opportunity.
    #    NOT geo-filtered: a human decision / strong alignment shows regardless of the gate.
    top_matches = _take(
        [r for r in live if _is_match(r)],
        lambda r: (_f(r.get("alignment_score")), _f(r.get("call_award_value"))))
    # 2. Top Funding — biggest / most-urgent of the GEO-ELIGIBLE remaining rows.
    top_funding = _take(discover, lambda r: _prominence_of(r, today))
    # 3. Also interesting — freshest of whatever geo-eligible rows are still unplaced.
    other = _take(discover, _recency_key)

    return {"top_funding": top_funding, "top_matches": top_matches, "other": other}
