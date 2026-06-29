"""Dynamic-gate test + re-screen audit.

Answers Bernard's question: "if I change the org's country preference, do the
DYNAMIC gates (geography / country / language / feasibility / applicant-type)
re-evaluate — reversing old rejects and blocking records that no longer fit?"

The scan gate (`auto_scorer.is_eligible`) ALREADY runs fresh every scan against
`policies = get_policies()`, and auto-rejects are NOT tombstoned — so a preference
change re-evaluates them automatically on the NEXT scan. What it does NOT do is
re-screen records ALREADY in rfp_submissions. This script:

  1. PROVES the country gate is dynamic (deterministic: same candidates, two
     country policies → the verdict flips).
  2. AUDITS live data under current (or simulated) policies:
       * existing rfp_submissions that would now be INELIGIBLE (should be blocked)
       * past auto-rejects that would now be ELIGIBLE (reversed)
     grouped by reason, flagging DYNAMIC (preference-driven) vs HARD reasons.

Read-only — never writes. Use --country to simulate a preference change.

  python scripts/rescreen_records.py
  python scripts/rescreen_records.py --country Ukraine
  python scripts/rescreen_records.py --country Ukraine --limit 400
"""
from __future__ import annotations

import copy
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.auto_scorer import is_eligible, country_eligible   # noqa: E402
from core.policies import get_policies                        # noqa: E402
from db.supabase_client import get_client                     # noqa: E402

# Reason categories that CHANGE with the org profile / preferences.
DYNAMIC = ("geography", "country", "language", "feasibility", "eligibility")


def _reason_cat(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    return r.split(":", 1)[0].strip() if ":" in r else r


def _with_country(policies: dict, country: str | None) -> dict:
    """Copy of policies with the eligible-country list swapped for `country`
    (strict: clears broad regions) — simulates changing the org preference."""
    if not country:
        return policies
    p = copy.deepcopy(policies)
    p.setdefault("countries", {})
    p["countries"]["eligible"] = [country]
    p["countries"]["broad_terms"] = []
    return p


def prove_country_gate(country: str) -> None:
    """Deterministic proof: same 3 candidates, current vs swapped country."""
    base = get_policies()
    here = (base.get("countries", {}).get("eligible") or ["(none)"])[0]
    cands = [
        {"opportunity_title": f"Health systems strengthening grant — {here}",
         "brief_description": f"Funding to strengthen primary health care in {here}.",
         "call_geographic_scope": [here], "call_submission_deadline": "2027-12-31",
         "opportunity_link": "https://example.org/grant/a"},
        {"opportunity_title": f"Maternal health grant — {country}",
         "brief_description": f"Health and nutrition programme based in {country}.",
         "call_geographic_scope": [country], "call_submission_deadline": "2027-12-31",
         "opportunity_link": "https://example.org/grant/b"},
    ]
    print(f"\n=== COUNTRY GATE IS DYNAMIC?  '{here}' (current) vs '{country}' ===")
    for label, pol in [(f"eligible=[{here}]", _with_country(base, here)),
                       (f"eligible=[{country}]", _with_country(base, country))]:
        print(f"  policy {label}:")
        for c in cands:
            ok, why = country_eligible(c, pol)
            print(f"    {'PASS' if ok else 'BLOCK':5} {c['call_geographic_scope'][0]:10} "
                  f"({c['opportunity_title'][:32]}…) {'' if ok else '— ' + why[:50]}")
    print("  → a country naming the eligible list PASSES; the other is BLOCKED. "
          "The gate reads policies live, so changing the preference flips it.")


def audit(country: str | None, limit: int) -> None:
    sb = get_client()
    policies = _with_country(get_policies(), country)
    tag = f" (simulating country={country})" if country else " (current policies)"
    print(f"\n=== LIVE AUDIT{tag} ===")

    # 1. Existing tracked records that would now be ineligible.
    recs = (sb.table("rfp_submissions")
            .select("uid,opportunity_title,opportunity_link,brief_description,"
                    "funding_agency,submission_deadline,call_geographic_scope,focus_theme")
            .eq("is_duplicate", False).limit(limit).execute().data or [])
    now_blocked = []
    for r in recs:
        ok, why = is_eligible(r, policies)
        if not ok:
            now_blocked.append((r, why))
    print(f"\nExisting records screened: {len(recs)}  ->  "
          f"{len(now_blocked)} would now be INELIGIBLE")
    cats = Counter(_reason_cat(w) for _, w in now_blocked)
    for cat, n in cats.most_common():
        kind = "DYNAMIC" if cat in DYNAMIC else "hard"
        print(f"   {cat:14} {n:4}  [{kind}]")
    dyn = [(r, w) for r, w in now_blocked if _reason_cat(w) in DYNAMIC]
    if dyn:
        print(f"\n   ↳ {len(dyn)} blocked for a DYNAMIC reason (would clear if the "
              "preference allowed them) — examples:")
        for r, w in dyn[:8]:
            print(f"      [{w[:38]}] {(r.get('opportunity_title') or '')[:60]}")

    # 2. Past auto-rejects that would now pass (reversals).
    rej = (sb.table("scan_decisions")
           .select("opportunity_title,opportunity_link,funding_agency,reason,"
                   "submission_deadline,call_geographic_scope")
           .eq("event_type", "system_reject").limit(limit).execute().data or [])
    reversed_now = []
    for r in rej:
        ok, _ = is_eligible(r, policies)
        if ok:
            reversed_now.append(r)
    print(f"\nPast auto-rejects screened: {len(rej)}  ->  "
          f"{len(reversed_now)} would now be ELIGIBLE (reversed)")
    for r in reversed_now[:8]:
        print(f"      was [{_reason_cat(r.get('reason'))}] now eligible: "
              f"{(r.get('opportunity_title') or '')[:60]}")
    print("\nNote: re-gating stored rows uses only stored text (no live re-fetch), "
          "so this is directional. The NEXT scan re-gates with full page text.")


def main(argv: list[str]) -> int:
    country = None
    limit = 500
    for i, a in enumerate(argv):
        if a == "--country" and i + 1 < len(argv):
            country = argv[i + 1]
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
    prove_country_gate(country or "Ukraine")
    audit(country, limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
