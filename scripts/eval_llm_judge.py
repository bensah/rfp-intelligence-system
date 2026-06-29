"""Phase 1 eval — score the LLM judge against your human gate verdicts.

Ground truth = `scan_decisions` rows with event_type='reject_verification':
  valid_reject -> the hard gate was RIGHT to drop it  (expected: do NOT admit)
  false_reject -> the hard gate was WRONG; should enter (expected: ADMIT)
  unsure       -> no ground truth, skipped.

For each labeled reject we re-fetch the live page text (scan_decisions stores
only title+link, not the body the judge needs), run llm_judge.judge(), and
compare the judge's admit/reject call to the human verdict. Reports a confusion
matrix, accuracy, how many gate-mistakes the judge would RECOVER, how many
correct rejects it would wrongly re-admit, latency, and projected $ at
gpt-4o-mini rates (the run itself is $0 on Ollama Cloud's free tier).

Run (after setting LLM_JUDGE_* in .env):
    python scripts/eval_llm_judge.py --limit 25
"""
from __future__ import annotations

import argparse
import sys
import time
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

import requests                                  # noqa: E402
from bs4 import BeautifulSoup                    # noqa: E402

from core import llm_judge                       # noqa: E402
from core.policies import get_policies           # noqa: E402
from db.supabase_client import get_client        # noqa: E402

_UA = {"User-Agent": "Mozilla/5.0 (compatible; RFPIS-eval/1.0)"}
# gpt-4o-mini list price (USD / 1M tokens) — for the projected-cost line only.
_MINI_IN, _MINI_OUT = 0.15, 0.60
_EST_IN, _EST_OUT = 2000, 250                     # rough tokens / call


def fetch_body(url: str, timeout: float = 12.0) -> str:
    if not url:
        return ""
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        return soup.get_text(" ", strip=True)[:6000]
    except Exception:
        return ""


def llm_admits(j: dict) -> bool:
    """The judge's overall verdict: would this candidate ENTER the pipeline?
    Admit iff it's an open call, not closed, on-theme, and the org's country is
    not explicitly ineligible (None = no geography stated -> don't block)."""
    return bool(
        j.get("is_open_call")
        and not j.get("is_closed")
        and j.get("theme_relevant")
        and j.get("country_eligible") is not False
    )


# event -> {label: expected_admit}. reject_verification tests the GATE on drops;
# feedback tests the GATE on SURVIVORS (good should stay, bad should have been cut).
_GT = {
    "reject_verification": {"valid_reject": False, "false_reject": True},
    "feedback": {"good": True, "bad": False},     # neutral skipped
}


def load_ground_truth(event: str, limit: int) -> list[dict]:
    """Latest verdict per link for the given event_type, keeping only labels with
    a defined expected-admit mapping."""
    keep = _GT[event]
    sb = get_client()
    rows = (sb.table("scan_decisions")
            .select("opportunity_title, opportunity_link, funding_agency, "
                    "geographic_scope, label, created_at")
            .eq("event_type", event)
            .order("created_at", desc=True).limit(5000).execute().data or [])
    seen, out = set(), []
    for r in rows:                               # newest-first -> first wins
        link = (r.get("opportunity_link") or "").strip()
        label = (r.get("label") or "").strip().lower()
        if not link or link in seen or label not in keep:
            continue
        seen.add(link)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25, help="max labeled rows to test")
    ap.add_argument("--event", default="reject_verification", choices=list(_GT),
                    help="reject_verification (gate on drops) | feedback (gate on survivors)")
    ap.add_argument("--model", default=None, help="override LLM_JUDGE_MODEL")
    ap.add_argument("--no-fetch", action="store_true", help="judge on title only")
    args = ap.parse_args()

    if not llm_judge.is_enabled():
        print("LLM judge not enabled — set LLM_JUDGE_BASE_URL / _API_KEY / _MODEL "
              "in .env and `pip install openai`.")
        return 1

    pol = get_policies()
    expect_map = _GT[args.event]
    gt = load_ground_truth(args.event, args.limit)
    if not gt:
        print(f"No {args.event} rows found. Label some in the Verify tab first.")
        return 1

    n_pos = sum(1 for r in gt if expect_map.get(r["label"].lower()))
    print(f"Testing {len(gt)} '{args.event}' rows  "
          f"({n_pos} should-admit, {len(gt) - n_pos} should-reject)")
    print(f"Model: {args.model or '(env LLM_JUDGE_MODEL)'}   "
          f"fetch_body={not args.no_fetch}\n")

    tp = fp = tn = fn = skipped = 0
    total_dt = 0.0
    for i, r in enumerate(gt, 1):
        title = r.get("opportunity_title") or ""
        link = r.get("opportunity_link") or ""
        expect_admit = bool(expect_map.get(r["label"].lower()))
        body = "" if args.no_fetch else fetch_body(link)
        cand = {"opportunity_title": title, "opportunity_link": link,
                "funding_agency": r.get("funding_agency"),
                "brief_description": body or r.get("geographic_scope") or title}
        t0 = time.time()
        j = llm_judge.judge(cand, pol, model=args.model)
        dt = time.time() - t0
        total_dt += dt
        if not j:
            skipped += 1
            print(f"{i:>2}. [judge failed] {title[:55]}")
            continue
        admit = llm_admits(j)
        if expect_admit and admit:
            tp += 1; tag = "✓ keeps good"
        elif expect_admit and not admit:
            fn += 1; tag = "✗ drops good"
        elif not expect_admit and not admit:
            tn += 1; tag = "✓ catches bad"
        else:
            fp += 1; tag = "✗ misses bad"
        print(f"{i:>2}. {tag:<22} human={r['label']:<12} "
              f"llm_admit={admit} ({dt:.0f}s) {title[:45]}")

    judged = tp + fp + tn + fn
    print("\n" + "=" * 60)
    if judged:
        acc = (tp + tn) / judged
        n_neg = tn + fp                          # truly should-reject
        catch = tn / n_neg if n_neg else 0.0
        keep = tp / (tp + fn) if (tp + fn) else 0.0
        print(f"Accuracy vs human:    {acc:.0%}  ({tp + tn}/{judged})")
        print(f"Catches should-reject: {tn}/{n_neg}  ({catch:.0%})  "
              "<- bad items the LLM correctly blocks")
        print(f"Keeps should-admit:    {tp}/{tp + fn}  ({keep:.0%})  "
              "<- good items the LLM correctly lets in")
        print(f"Confusion: TP={tp} TN={tn} FP={fp} FN={fn}  skipped(failed)={skipped}")
        print(f"Avg latency: {total_dt / max(judged,1):.1f}s/call")
    proj = judged * (_EST_IN * _MINI_IN + _EST_OUT * _MINI_OUT) / 1_000_000
    print(f"\nThis run: $0 (Ollama Cloud free tier). "
          f"Same {judged} calls on gpt-4o-mini would cost ~${proj:.3f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
