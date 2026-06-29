"""Retroactive data-quality cleanup of rfp_submissions.

The scan gate + deduper only act at ingest time, so rows inserted BEFORE a gate
fix (news/interview pages, closed/past calls, past deadlines) or before a deduper
fix (the same call from two sources) sit in the table until cleaned. This script
re-screens every live (non-duplicate) row against the CURRENT gate + deduper and:

  * DELETES rows that now fail a HARD data-quality gate — not-an-rfp (news /
    interview / archive), explicitly-closed calls, and past deadlines. These are
    bad data, and the fixed gate prevents re-insertion on the next scan.
  * FLAGS as is_duplicate=True any row that now matches an older surviving row
    (keeps the oldest as canonical), so the UI shows one RFP per call.

POLICY-driven rejects (theme / country / geography / applicant-type / language)
are deliberately LEFT ALONE — those reflect the org's current preferences, not
bad data, and flip automatically on the next scan.

    python scripts/cleanup_records.py            # dry-run (report only)
    python scripts/cleanup_records.py --commit    # apply deletes + dup flags
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.auto_scorer import is_eligible          # noqa: E402
from core.deduplicator import find_duplicates      # noqa: E402
from core.policies import get_policies             # noqa: E402
from db.supabase_client import get_client          # noqa: E402

# Reject-reason prefixes that mean BAD DATA — never a real open call, safe to
# delete (news/interview pages, closed calls, archive/search/listing URLs).
_JUNK_PREFIXES = ("not-an-rfp", "call explicitly closed",
                  "URL is a search", "URL lists", "title is a generic")
# Past-deadline rows were VALID when inserted — they just expired. Removed only
# with --include-expired (kept by default; the Records deadline filter hides them).
_EXPIRED_PREFIX = "deadline:"
# Everything else (theme / country / geography / eligibility / language /
# feasibility) is a policy decision and is left untouched.


def _classify(reason: str) -> str:
    r = (reason or "").lower()
    if any(r.startswith(p.lower()) for p in _JUNK_PREFIXES):
        return "junk"
    if r.startswith(_EXPIRED_PREFIX):
        return "expired"
    return "policy"


def main(argv: list[str]) -> int:
    commit = "--commit" in argv
    include_expired = "--include-expired" in argv
    sb = get_client()
    pol = get_policies()
    rows = (sb.table("rfp_submissions")
            .select("uid,opportunity_id,opportunity_title,opportunity_link,"
                    "funding_agency,submission_deadline,estimated_value,"
                    "brief_description,call_geographic_scope,submitted_at,source")
            .eq("is_duplicate", False).order("submitted_at").execute().data or [])
    print(f"=== cleanup {'(DRY RUN)' if not commit else '(COMMIT)'} — "
          f"{len(rows)} live rows ===\n")

    # 1. Re-screen pass — classify each row under the CURRENT gate.
    junk: list[dict] = []
    expired: list[dict] = []
    for r in rows:
        cand = {**r, "_source_class": "primary"}  # trust source; test data-quality gates
        ok, reason = is_eligible(cand, pol)
        if ok:
            continue
        kind = _classify(reason)
        if kind == "junk":
            junk.append({**r, "_reason": reason})
        elif kind == "expired":
            expired.append({**r, "_reason": reason})

    to_delete = junk + (expired if include_expired else [])
    delete_uids = {r["uid"] for r in to_delete}
    print(f"-- JUNK (always deleted): {len(junk)} --")
    for r in junk[:40]:
        print(f"  ✗ {r['_reason'][:34]:34} {(r['opportunity_title'] or '')[:50]}")
    tag = "DELETED (--include-expired)" if include_expired else "kept (use --include-expired to remove)"
    print(f"\n-- EXPIRED past-deadline: {len(expired)} — {tag} --")
    for r in expired[:40]:
        print(f"  • {r['_reason'][:34]:34} {(r['opportunity_title'] or '')[:50]}")

    # 2. Dedup pass over the SURVIVORS (oldest kept as canonical).
    survivors = [r for r in rows if r["uid"] not in delete_uids]
    kept: list[dict] = []
    dups: list[dict] = []
    for r in survivors:
        probe = {
            "opportunity_id": r.get("opportunity_id"),
            "opportunity_title": r.get("opportunity_title"),
            "opportunity_link": r.get("opportunity_link"),
            "funding_agency": r.get("funding_agency"),
            "submission_deadline": str(r.get("submission_deadline") or "") or None,
            "estimated_value": r.get("estimated_value"),
        }
        m = find_duplicates(probe, existing=kept)
        if m:
            dups.append({**r, "_of": m[0]["uid"], "_reason": m[0]["_reason"]})
        else:
            kept.append(r)
    print(f"\n-- duplicates to flag: {len(dups)} --")
    for r in dups[:40]:
        print(f"  ↪ of {r['_of']}: {(r['opportunity_title'] or '')[:46]}  [{r['_reason'][:30]}]")

    if not commit:
        print(f"\nDry-run. Would delete {len(to_delete)} row(s) "
              f"({len(junk)} junk"
              f"{f' + {len(expired)} expired' if include_expired else ''}) "
              f"+ flag {len(dups)} duplicate(s). Re-run with --commit to apply"
              f"{'' if include_expired else ' (add --include-expired to also drop expired)'}.")
        return 0

    # Apply.
    for r in to_delete:
        sb.table("rfp_submissions").delete().eq("uid", r["uid"]).execute()
    for r in dups:
        sb.table("rfp_submissions").update(
            {"is_duplicate": True, "duplicate_of_uid": r["_of"]}
        ).eq("uid", r["uid"]).execute()
    print(f"\n✓ Deleted {len(to_delete)} bad-data rows; flagged {len(dups)} "
          f"duplicates. {len(kept)} canonical rows remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
