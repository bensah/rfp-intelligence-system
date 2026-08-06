"""Re-extract `call_geographic_scope` on rows carrying a STALE broad tier.

Why: `_extract_call_geographic_scope` only tags the "Global / worldwide" tier when a
GENUINE worldwide-eligibility phrase is present (`geographies.worldwide_ok`) — a bare
"global" is almost always part of an organisation or platform NAME ("United Nations
Global Marketplace", "Global Fund", "…Global Health"). Rows scanned BEFORE that guard
landed still carry the stray tag, and because MUST-4 treats an inclusive tier as
covering any org, they read "Yes, our own presence · 100%" on calls restricted to a
country the org has no footprint in. A Bangladesh-only UNICEF tender scored a full
geographic pass for a Cameroon/Mali org.

The re-scan does NOT repair them: the merge policy preserves existing fields, so a
stale scope survives every re-crawl.

SAFE BY DESIGN — it SUBTRACTS the stray tier and nothing else:
  * The corrected scope is the stored list MINUS the broad tier. It is NOT the
    re-extraction's output. Substituting the whole re-extracted list was tried and
    rejected on the dry run: it would have replaced one row's explicit
    ['Cameroon', 'Mali', 'Regional', 'Global'] with ['Australia', 'Canada'], and turned
    three ['global'] rows into ['China'] on incidental country mentions — destroying
    good stored data to fix a tag.
  * A row is only touched when the CURRENT extractor, run over that row's OWN stored
    text, does NOT produce the broad tier. The text is the evidence that the stored tag
    is stale; nothing is inferred.
  * Never writes an EMPTY scope. When the tier is the ONLY value there is no evidence of
    a restriction, so the row is left permissive — that Parks it for review rather than
    wrongly auto-Declining it.
  * Skips human-reviewed rows (`decision_date` stamped) unless --include-reviewed: a
    reviewer may have corrected the scope by hand, and their verdict wins.
  * Prints the exact before → after and the MUST-4 verdict change for every row, and
    dry-runs by default.

Usage:
    python scripts/backfill_stale_geo_scope.py                 # dry-run (report only)
    python scripts/backfill_stale_geo_scope.py --apply         # write
    python scripts/backfill_stale_geo_scope.py --tenant <uuid> # a specific tenant
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core import auto_scorer, criteria_derive, org_profile          # noqa: E402
from db.supabase_client import service_client                        # noqa: E402

# The tiers that open a call to an org with no footprint in the named countries. A stray
# one of these is the whole failure mode.
_BROAD_TIERS = {"global / worldwide", "global", "worldwide"}
_TEXT_FIELDS = ("opportunity_title", "brief_description", "notes", "focus_theme")


def _policies() -> dict:
    try:
        from core.policies import DEFAULT_POLICIES
        return DEFAULT_POLICIES
    except Exception:
        return {"countries": {"eligible": [], "broad_terms": []}}


def _has_broad(scope) -> bool:
    return any(str(s).strip().lower() in _BROAD_TIERS
               for s in criteria_derive._as_list(scope))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrected scopes")
    ap.add_argument("--tenant", help="restrict to one tenant_id")
    ap.add_argument("--include-reviewed", action="store_true",
                    help="also fix rows a human has reviewed (default: skip them)")
    args = ap.parse_args()

    pol = _policies()
    prof = org_profile.get_profile() or {}
    sb = service_client()
    q = sb.table("rfp_submissions").select("*")
    if args.tenant:
        q = q.eq("tenant_id", args.tenant)
    rows = q.limit(5000).execute().data or []

    fixes, skipped_blank, skipped_reviewed = [], [], []
    for r in rows:
        stored = criteria_derive._as_list(r.get("call_geographic_scope"))
        if not stored or not _has_broad(stored):
            continue
        text = " ".join(str(r.get(k) or "") for k in _TEXT_FIELDS).strip()
        if not text:
            continue
        fresh = auto_scorer._extract_call_geographic_scope(text, pol)
        if _has_broad(fresh):
            continue                       # the tier is GENUINE — the text still says so
        # SUBTRACT the stray tier; keep everything else the row already had. Do NOT
        # substitute `fresh` — see the module docstring.
        kept = [s for s in criteria_derive._as_list(stored)
                if str(s).strip().lower() not in _BROAD_TIERS]
        if not kept:
            skipped_blank.append(r["uid"])   # tier was the ONLY value → leave permissive
            continue
        fresh = kept
        reviewed = bool(str(r.get("decision_date") or "").strip())
        if reviewed and not args.include_reviewed:
            skipped_reviewed.append(r["uid"])
            continue
        before = criteria_derive.derive_geographic_fit(prof, r, {}, {})
        after = criteria_derive.derive_geographic_fit(
            prof, {**r, "call_geographic_scope": fresh}, {}, {})
        fixes.append((r["uid"], stored, fresh, before, after,
                      (r.get("opportunity_title") or "")[:52]))

    print(f"scanned {len(rows)} rows | stale broad tier: {len(fixes)}")
    if skipped_blank:
        print(f"  skipped (broad tier was the ONLY scope — left permissive): {len(skipped_blank)}")
    if skipped_reviewed:
        print(f"  skipped (human-reviewed; use --include-reviewed): {len(skipped_reviewed)}")
    print()
    for uid, before_scope, fresh, b, a, title in fixes:
        flag = "  <-- MUST-4 CHANGES" if b != a else ""
        print(f"  {uid:22} {title}")
        print(f"      scope {before_scope} -> {fresh}")
        print(f"      MUST-4 {b!r} -> {a!r}{flag}")
    changed = [f for f in fixes if f[3] != f[4]]
    print(f"\n{len(changed)} of {len(fixes)} change the MUST-4 verdict.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0
    ok = 0
    for uid, _b, fresh, *_ in fixes:
        try:
            sb.table("rfp_submissions").update(
                {"call_geographic_scope": fresh}).eq("uid", uid).execute()
            ok += 1
        except Exception as exc:                                  # pragma: no cover
            print(f"  !! {uid}: {exc}")
    print(f"\nwrote {ok} of {len(fixes)} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
