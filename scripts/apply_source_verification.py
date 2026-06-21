"""Apply the 2026-06-20 deep per-site source verification (7 agents × 10 sources).

REMOVE = sources that publish no usable open calls for our scan:
  * covered by an API we already scan (EU F&T / World Bank procnotices / UNGM)
  * not a donor / wrong kind (think tank, VC, product fund)
  * paywalled, invite-only/proactive, or no open-call listing exists
FIX = corrected listing URL and/or access method (the donor DOES publish open
calls, but our URL/method was wrong).

DRY-RUN by default; --commit to apply. Reversible (re-add via the registry Add
form or re-import the CSV).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import source_registry as sr   # noqa: E402

# host -> short reason (for the report)
REMOVE = {
    "ec.europa.eu": "covered by EU Funding & Tenders API (api.tech.ec.europa.eu)",
    "international-partnerships.ec.europa.eu": "INTPA calls publish on EU F&T API",
    "civil-protection-humanitarian-aid.ec.europa.eu": "ECHO calls publish on EU F&T API",
    "worldbank.org": "covered by World Bank procnotices API",
    "unfpa.org": "UNFPA procurement publishes on UNGM (already scanned)",
    "resources.theglobalfund.org": "grant-lifecycle docs, not open calls (dup of theglobalfund.org)",
    "cgdev.org": "think tank that RECEIVES funding, not a donor",
    "catalyticopportunityfund.org": "reproductive-health product fund, no open-call listing",
    "thecatalystfund.com": "equity VC for startups, not an RFP/grant donor",
    "fundsforngospremium.com": "paywalled subscription product",
    "developmentaid.org": "paywalled aggregator — deadlines/apply behind login",
    "ciff.org": "proactive grantmaker — no unsolicited proposals / open calls",
    "ikeafoundation.org": "proactive — does not accept applications",
    "averydennison.com": "rolling eligibility page, no open-call listing",
    "google.org": "irregular impact challenges, no standing calls listing",
    "fundinnovation.dev": "year-round single portal, no discrete calls",
    "globalinnovation.fund": "rolling, last window closed, no open calls",
    "jica.go.jp": "no central English open-call listing (per-country PDFs only)",
    "ocrahope.org": "describes programs, no enumerated open calls (niche)",
}

# host -> {sample_url?, ingestion_method?}
FIX = {
    "researchnet-recherchenet.ca": {"sample_url": "https://www.researchnet-recherchenet.ca/rnr16/fodRss.do?type=ALL&chanTyp=ALL&lang=E", "ingestion_method": "RSS / feed"},
    "global-health-edctp3.europa.eu": {"sample_url": "https://www.global-health-edctp3.europa.eu/node/93/rss_en", "ingestion_method": "RSS / feed"},
    "cm.usembassy.gov": {"sample_url": "https://cm.usembassy.gov/category/grants/feed/", "ingestion_method": "RSS / feed"},
    "healthresearch.org": {"sample_url": "https://www.healthresearch.org/feed/", "ingestion_method": "RSS / feed"},
    "globalsouthopportunities.com": {"sample_url": "https://www.globalsouthopportunities.com/category/funding/feed/", "ingestion_method": "RSS / feed"},
    "www2.fundsforngos.org": {"sample_url": "https://www2.fundsforngos.org/feed/", "ingestion_method": "RSS / feed"},
    "scidev.net": {"sample_url": "https://www.scidev.net/global/rss.xml", "ingestion_method": "RSS / feed"},
    "povertyactionlab.org": {"sample_url": "https://www.povertyactionlab.org/funding", "ingestion_method": "JS page crawl"},
    "nihr.ac.uk": {"ingestion_method": "JS page crawl"},
    "solve.mit.edu": {"ingestion_method": "JS page crawl"},
    "submit.gatesfoundation.org": {"ingestion_method": "JS page crawl"},
    "cepi.net": {"ingestion_method": "JS page crawl"},
    "fundingprogrammesportal.gov.cy": {"ingestion_method": "JS page crawl"},
    "wellcome.org": {"ingestion_method": "JS page crawl"},   # Cloudflare 403 -> Playwright
    "theglobalfund.org": {"ingestion_method": "JS page crawl"},  # Oracle Fusion tenders portal
    "afd.fr": {"sample_url": "https://www.afd.fr/en/calls-for-projects/list?status%5Bongoing%5D=ongoing", "ingestion_method": "JS page crawl"},
    "afdb.org": {"sample_url": "https://www.afdb.org/en/projects-and-operations/procurement", "ingestion_method": "JS page crawl"},
    "zayedsustainabilityprize.com": {"sample_url": "https://zayedsustainabilityprize.com/en/submit", "ingestion_method": "JS page crawl"},
    "openphilanthropy.org": {"sample_url": "https://coefficientgiving.org/funds/", "ingestion_method": "JS page crawl"},  # rebranded
    "grantbite.com": {"sample_url": "https://www.grantbite.com/en/funding", "ingestion_method": "JS page crawl"},
    "frld.org": {"sample_url": "https://www.frld.org/news", "ingestion_method": "Page crawl"},
    "nestlefoundation.org": {"sample_url": "https://www.nestlefoundation.org/apply", "ingestion_method": "Page crawl"},
    "sidaction.org": {"sample_url": "https://www.sidaction.org/appel-a-projet/", "ingestion_method": "Page crawl"},
    "globalhealth.stanford.edu": {"sample_url": "https://seedfunding.stanford.edu/opportunities", "ingestion_method": "Page crawl"},
    "government.nl": {"sample_url": "https://english.rvo.nl/subsidies-programmes", "ingestion_method": "Page crawl"},
    "gatesfoundation.org": {"sample_url": "https://www.grandchallenges.org/grant-opportunities", "ingestion_method": "Page crawl"},
    "giz.de": {"sample_url": "https://ausschreibungen.giz.de", "ingestion_method": "Page crawl"},
    "robertcarrfund.org": {"sample_url": "https://robertcarrfund.org/funding/", "ingestion_method": "Page crawl"},
    "gcgh.grandchallenges.org": {"ingestion_method": "Page crawl"},   # static, was wrongly JS
    "worlddiabetesfoundation.submittable.com": {"sample_url": "https://worlddiabetesfoundation.submittable.com/api/v2/categories", "ingestion_method": "API"},  # Submittable JSON API (needs handler)
}


def main(commit: bool) -> int:
    have = {r["host"] for r in sr.list_rows()}
    rem = [h for h in REMOVE if h in have]
    print(f"=== REMOVE ({len(rem)}) ===")
    for h in rem:
        print(f"  - {h:<42} {REMOVE[h]}")
    print(f"\n=== FIX ({sum(1 for h in FIX if h in have)}) ===")
    for h, f in FIX.items():
        if h in have:
            print(f"  ~ {h:<42} {f}")
    miss = [h for h in (set(REMOVE) | set(FIX)) if h not in have]
    if miss:
        print(f"\n(not in registry, skipped: {', '.join(sorted(miss))})")
    if not commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return 0
    n = sr.delete_hosts(rem)
    f = 0
    for h, fields in FIX.items():
        if h in have and sr.update_row(h, fields, by="deep-verify"):
            f += 1
    print(f"\nCOMMITTED: removed {n}, fixed {f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
