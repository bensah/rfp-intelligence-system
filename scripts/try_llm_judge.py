"""Smoke-test the OpenAI-compatible LLM judge against real org policy.

Set the endpoint/model via env (any OpenAI-compatible provider — see
core/llm_judge.py docstring), then run:

    pip install openai
    python scripts/try_llm_judge.py

It prints the structured verdict for a few sample candidates (one health/in-scope
call, one off-theme, one wrong-geography, one news page) so you can eyeball
quality + latency before wiring it into the pipeline. If LLM_JUDGE_* env vars
aren't set it tells you and exits (the real pipeline would fall back to regex).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import llm_judge          # noqa: E402
from core.policies import get_policies  # noqa: E402

SAMPLES = [
    {"opportunity_title": "Nexa Funding Opportunity: Africa, Latin America and the Caribbean",
     "opportunity_link": "https://www.grandchallenges.ca/funding-opportunity-nexa/",
     "brief_description": "We are investing in bold solutions across Africa, Latin "
     "America and the Caribbean that enable local health actors to turn climate-"
     "driven health risk signals into timely health service delivery, reducing harm "
     "from mosquito-borne infections. Apply by July 22, 2026."},
    {"opportunity_title": "SPN - Mali - Supply of Medical Equipment and Hospital Diagnostics",
     "opportunity_link": "https://www.afdb.org/en/documents/spn-mali-medical-equipment",
     "brief_description": "Specific Procurement Notice. The Ministry of Health of "
     "Mali invites sealed bids for the supply and installation of hospital "
     "diagnostic equipment. Submission deadline 30 September 2026."},
    {"opportunity_title": "Call for research projects on adaptation in the Mediterranean region",
     "opportunity_link": "https://www.afd.fr/en/calls-for-projects/mediterranean",
     "brief_description": "Funding for research on climate adaptation across the "
     "Mediterranean basin (Southern Europe, North Africa, the Levant)."},
    {"opportunity_title": "Interview with an MMV African call for proposals grantee: Dr Cheuka",
     "opportunity_link": "https://www.mmv.org/news-resources-search/interview-grantee-cheuka",
     "brief_description": "A written interview with a grantee of a past MMV call, "
     "discussing their malaria drug-discovery research career."},
]


def main() -> int:
    if not llm_judge.is_enabled():
        print("LLM judge is NOT enabled. Set these env vars (see core/llm_judge.py):")
        print("  LLM_JUDGE_BASE_URL, LLM_JUDGE_API_KEY, LLM_JUDGE_MODEL")
        print("and `pip install openai`. The pipeline falls back to regex when off.")
        return 1
    pol = get_policies()
    import os
    print(f"endpoint={os.environ.get('LLM_JUDGE_BASE_URL')} "
          f"model={os.environ.get('LLM_JUDGE_MODEL')}\n")
    for c in SAMPLES:
        t0 = time.time()
        j = llm_judge.judge(c, pol)
        dt = time.time() - t0
        print(f"### {c['opportunity_title'][:60]}  ({dt:.1f}s)")
        if not j:
            print("   (no verdict — call failed)\n")
            continue
        print(f"   open_call={j['is_open_call']} type={j['solicitation_type']} "
              f"deadline={j['submission_deadline']} value={j['estimated_value']}{j['currency'] or ''}")
        print(f"   country_eligible={j['country_eligible']} theme_relevant={j['theme_relevant']} "
              f"conf={j['confidence']}")
        print(f"   reason: {j['reason']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
