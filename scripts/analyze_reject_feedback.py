"""Learn from reject-verification feedback — surface where the gate's REASONS
(and decisions) disagree with humans, so we know exactly which rules to fix.

READ-ONLY. Joins each auto-reject's SYSTEM reason (scan_decisions.system_reject)
with the human verdict + CORRECTED reason (scan_decisions.reject_verification,
captured in the Verify tab). Reports:
  * verdict mix per system reason (valid / false / unsure) → which gates over-reject;
  * reason corrections (system said X, human says it should be Y) → which gates
    pick the WRONG reason — the highest-leverage rule fixes.

It does NOT rewrite any rules — gate logic stays human-authored. This tells a
human (or a future tuning pass) precisely where to act.

  python scripts/analyze_reject_feedback.py
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from db.supabase_client import get_client, safe_execute   # noqa: E402

_VERDICT = {"valid_reject": "valid", "false_reject": "false", "unsure": "unsure"}


def _fetch(event: str, cols: str) -> list[dict]:
    sb = get_client()
    out, start = [], 0
    while True:
        rows = safe_execute(sb.table("scan_decisions").select(cols)
                            .eq("event_type", event)
                            .order("created_at", desc=True)
                            .range(start, start + 999)).data or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        start += 1000
    return out


def main() -> int:
    # System reason per link (latest).
    sysr: dict[str, str] = {}
    for r in _fetch("system_reject", "opportunity_link,label,created_at"):
        link = (r.get("opportunity_link") or "").strip()
        if link and link not in sysr:
            sysr[link] = (r.get("label") or "?").strip()

    # Human verdict + corrected reason per link (latest).
    verif: dict[str, dict] = {}
    for r in _fetch("reject_verification", "opportunity_link,label,reason,created_at"):
        link = (r.get("opportunity_link") or "").strip()
        if link and link not in verif:
            verif[link] = {"verdict": (r.get("label") or "").strip(),
                           "corrected": (r.get("reason") or "").strip() or None}

    if not verif:
        print("No reject-verification feedback yet. Verify some rejects in the "
              "Verify tab (set Verdict + Correct reason), then re-run.")
        return 0

    print(f"system_reject links: {len(sysr)}   human-verified: {len(verif)}\n")

    # 1) Verdict mix per system reason — which gates over-reject.
    per_reason = defaultdict(Counter)
    for link, v in verif.items():
        sr = sysr.get(link, "?")
        per_reason[sr][_VERDICT.get(v["verdict"], v["verdict"])] += 1
    print("=" * 70)
    print("VERDICT MIX per SYSTEM reason  (false = gate was wrong to reject)")
    print("=" * 70)
    print(f"{'system reason':<28} {'valid':>6} {'false':>6} {'unsure':>7}  false%")
    for sr, c in sorted(per_reason.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values()) or 1
        print(f"{sr[:27]:<28} {c['valid']:>6} {c['false']:>6} {c['unsure']:>7}"
              f"  {100*c['false']/tot:5.0f}%")

    # 2) Reason corrections — system said X but human says Y (X != Y, not "_ok").
    print("\n" + "=" * 70)
    print("REASON CORRECTIONS  (system reason → human's correct reason)")
    print("  → these are the wrong-reason bugs; fix the gate rule that owns them")
    print("=" * 70)
    # Only the controlled correction CODES count (the Verify-tab vocabulary);
    # legacy rows stored the full system-reason string here — ignore those.
    _CODES = {"deadline", "not-an-rfp", "theme", "geography", "country",
              "eligibility", "type", "language", "aggregator", "feasibility"}
    affirmed = 0
    conflicts = Counter()
    for link, v in verif.items():
        corr = v.get("corrected")
        if corr == "_ok":
            affirmed += 1
            continue
        if corr not in _CODES:           # legacy / no correction captured
            continue
        sr = (sysr.get(link, "?") or "").split(":", 1)[0].strip()
        if corr != sr:
            conflicts[(sr, corr)] += 1
    print(f"(system reason affirmed by human: {affirmed})")
    if not conflicts:
        print("(no reason corrections captured yet — set 'Correct reason' in the "
              "Verify tab as you review)")
    else:
        for (sr, corr), n in conflicts.most_common(30):
            print(f"  {n:>3}×  system='{sr}'  →  should be '{corr}'")
    print("\nDONE — read-only. Use the corrections above to target rule fixes "
          "in core/auto_scorer.is_eligible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
