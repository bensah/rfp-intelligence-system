"""Seed the decision-model training set from EXISTING human-coded decisions
(ML Phase 3).

The Excel-migrated + manually-entered RFPs already carry a human Proceed / Park
/ Decline in `rfp_submissions.decision` — gold labels that never reached
`scan_decisions` (they came in via migration, not the review-save path). This
turns each into a `human_decision` row with its feature vector, so the model
can train on real history NOW instead of waiting for fresh reviews.

WHAT COUNTS AS A HUMAN LABEL
  * source != 'auto'  (migration / manual / submitted = a person set it), OR
  * decision_overridden_by is set (a reviewer touched an auto row).
  source='auto' WITHOUT a human override is EXCLUDED — that decision is the
  rule's own output (auto_recommendation); harvesting it would teach the model
  to echo the rule (leakage).

Idempotent: skips rfps that already have a human_decision logged. Dry-run by
default — pass --commit to write.

  python scripts/harvest_human_decisions.py            # preview
  python scripts/harvest_human_decisions.py --commit   # write
"""
from __future__ import annotations

import collections
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import features as F          # noqa: E402
from core.policies import get_policies  # noqa: E402
from db.supabase_client import get_client  # noqa: E402

_TABLE = "scan_decisions"
_VALID = {"Proceed", "Park", "Decline"}
_BATCH = 200


def _asof(row):
    for k in ("decision_date", "decision_overridden_at", "created_at"):
        v = row.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
            except Exception:
                continue
    return None


def _is_human(row) -> bool:
    return (row.get("source") or "").strip().lower() != "auto" \
        or bool(row.get("decision_overridden_by"))


def main(commit: bool) -> int:
    sb = get_client()
    policies = get_policies()

    rows = (sb.table("rfp_submissions").select("*").limit(20000).execute().data or [])
    decided = [r for r in rows
               if (r.get("decision") or "").strip().title() in _VALID and _is_human(r)]

    # Idempotency: which rfps already have a human_decision logged?
    try:
        existing = (sb.table(_TABLE).select("rfp_uid")
                    .eq("event_type", "human_decision").execute().data or [])
        seen = {e.get("rfp_uid") for e in existing if e.get("rfp_uid")}
    except Exception:
        seen = set()

    todo = [r for r in decided if r.get("uid") not in seen]
    by_cls = collections.Counter((r.get("decision") or "").title() for r in todo)
    by_src = collections.Counter((r.get("source") or "?") for r in todo)
    print(f"human-coded decisions found : {len(decided)}")
    print(f"already logged (skipped)    : {len(decided) - len(todo)}")
    print(f"to harvest                  : {len(todo)}  {dict(by_cls)}  src={dict(by_src)}")
    excluded_auto = sum(1 for r in rows
                        if (r.get('decision') or '').title() in _VALID and not _is_human(r))
    print(f"excluded (auto, no override): {excluded_auto}")

    if not todo:
        print("\nNothing to write.")
        return 0
    if not commit:
        print("\nDRY RUN — re-run with --commit to write these rows.")
        return 0

    def _scope_text(gs):
        if isinstance(gs, (list, tuple)):
            return ", ".join(str(x) for x in gs if x) or None
        return gs or None

    recs = []
    for r in todo:
        recs.append({
            "event_type": "human_decision",
            "label": (r.get("decision") or "").strip().title(),
            "reason": "harvested from migrated/coded decision",
            "rfp_uid": r.get("uid"),
            "opportunity_title": (r.get("opportunity_title") or "")[:500] or None,
            "opportunity_link": r.get("opportunity_link"),
            "funding_agency": r.get("funding_agency"),
            "source": r.get("source"),
            "geographic_scope": _scope_text(r.get("geographic_scope")),
            "submission_deadline": (str(r.get("submission_deadline"))[:10]
                                    if r.get("submission_deadline") else None),
            "alignment_score": r.get("alignment_score"),
            "features": F.extract(r, policies, asof=_asof(r)) or None,
            "decided_by": r.get("decision_overridden_by") or (r.get("source") or "migration"),
        })

    written = 0
    for i in range(0, len(recs), _BATCH):
        sb.table(_TABLE).insert(recs[i:i + _BATCH]).execute()
        written += len(recs[i:i + _BATCH])
    print(f"\nWrote {written} human_decision rows to {_TABLE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(commit="--commit" in sys.argv))
