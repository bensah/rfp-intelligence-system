"""Backfill scan_decisions.features for rows captured before feature logging
existed (ML Phase 3 prep).

For each scan_decisions row with an empty `features` jsonb:
  * if it links to an rfp_submissions row (rfp_uid), rebuild the FULL feature
    vector from that stored row — no re-crawl needed (every feature is derivable
    from columns already on the row);
  * otherwise (e.g. a system_reject logged during a scan, with no inserted row),
    rebuild a PARTIAL vector from the columnar fields the scan_decisions row
    already carries (geo / deadline / funder / channel).

Dated to each decision's own created_at so days_to_deadline reflects the moment
of the decision, not today. Idempotent (only touches rows where features IS
NULL) and best-effort. Run:  python scripts/backfill_decision_features.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Allow running as a plain script (repo root on the path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import features as F          # noqa: E402
from core.policies import get_policies  # noqa: E402
from db.supabase_client import get_client  # noqa: E402

_TABLE = "scan_decisions"
_BATCH = 200


def _asof(created_at):
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).date()
    except Exception:
        return None


def main() -> int:
    sb = get_client()
    policies = get_policies()

    rows = (sb.table(_TABLE)
            .select("id,rfp_uid,created_at,opportunity_link,funding_agency,"
                    "source,geographic_scope,submission_deadline,alignment_score")
            .is_("features", "null")
            .execute().data or [])
    if not rows:
        print("Nothing to backfill — all scan_decisions rows already have features.")
        return 0
    print(f"{len(rows)} row(s) need features.")

    # Batch-load the linked rfp_submissions for full reconstruction.
    uids = sorted({r.get("rfp_uid") for r in rows if r.get("rfp_uid")})
    sub_by_uid: dict[str, dict] = {}
    for i in range(0, len(uids), _BATCH):
        chunk = uids[i:i + _BATCH]
        try:
            data = (sb.table("rfp_submissions").select("*")
                    .in_("uid", chunk).execute().data or [])
            for s in data:
                sub_by_uid[s.get("uid")] = s
        except Exception as exc:
            print(f"  warn: rfp_submissions fetch failed for a chunk: {exc}")

    updated = full = partial = 0
    for r in rows:
        src = sub_by_uid.get(r.get("rfp_uid"))
        if src:
            full += 1
        else:
            # Minimal row from the scan_decisions columnar fields.
            src = {
                "opportunity_link": r.get("opportunity_link"),
                "funding_agency": r.get("funding_agency"),
                "source": r.get("source"),
                "geographic_scope": r.get("geographic_scope"),
                "submission_deadline": r.get("submission_deadline"),
                "alignment_score": r.get("alignment_score"),
            }
            partial += 1
        feats = F.extract(src, policies, asof=_asof(r.get("created_at")))
        if not feats:
            continue
        try:
            sb.table(_TABLE).update({"features": feats}).eq("id", r["id"]).execute()
            updated += 1
        except Exception as exc:
            print(f"  warn: update failed for {r.get('id')}: {exc}")

    print(f"Backfilled {updated} row(s): {full} full (from rfp_submissions), "
          f"{partial} partial (columnar only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
