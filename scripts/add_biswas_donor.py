"""Add the Biswas Family Foundation (Fast Grants) to the donor catalog.

One-off, idempotent seed (upsert on canonical_key) so the donor appears on the
Donors page. All fields below are taken from the foundation's official Fast
Grants page (https://www.biswasfamilyfoundation.org/science/fast-grants),
verified 2026-06-13 — nothing is guessed.

Run once (uses the same Supabase client as import_donor_intel.py):

    python scripts/add_biswas_donor.py

Requires migrations 020/021/025 applied (profile + structured columns).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.supabase_client import get_client  # noqa: E402

DONOR = {
    "canonical_key": "biswas_family_foundation",
    "donor": "Biswas Family Foundation",
    "donor_short": "Biswas / Fast Grants",
    "aliases": "Fast Grants; Biswas Foundation",
    "donor_category": "International philanthropies & foundations",
    "website": "https://www.biswasfamilyfoundation.org/science/fast-grants",
    "summary_description": (
        "The Biswas Family Foundation's Fast Grants program gives lightweight, "
        "fast-turnaround grants to early-career or pivoting researchers exploring "
        "ideas at the intersection of AI and health — model training/evaluation on "
        "biomedical data, dataset assembly, AI tools for scientists, and pilot "
        "experiments that de-risk larger funding. Up to ~$3M deployed annually "
        "across two cycles; decisions on the written application only (no interview)."
    ),
    # Funding footprint
    "donor_award_low": "$25,000",
    "donor_award_high": "$100,000",
    "total_annual_funding_global": "Up to $3M / year",
    "projected_budget": "Up to $3M / year",
    "projected_budget_period": "2026 (two cycles)",
    # Structured (JSON list) fields — same shape the Donors edit form writes.
    "funding_mechanism": json.dumps(["Grants"]),
    "donor_geographic_scope": json.dumps(["Global / worldwide"]),
    "donor_priority_areas": json.dumps(
        ["Digital health / data / AI", "Health research"]),
    # Intelligence
    "funding_cycle": "Biannual (twice a year)",
    "recent_activity": "2026 cycles — deadlines Jun 15 & Dec 15, 2026",
    "application_process": "Full proposal (single-stage)",
    "reporting_requirements": "Milestone / deliverable-based",
    "active_route_status": "Active",
    "direct_local_org_eligible": "Yes — via international partner only",
    # Eligibility / route flags (TEXT yes/no/blank)
    "ngo_eligible": "yes",
    "for_profit_eligible": "no",
    "grant_route": "yes",
    "open_call_unsolicited": "yes",
    "online_portal_submission": "yes",
    "prefinance_required": "none",
    "digital_health_data_ai_fit": "yes",
    # Provenance
    "verification_level": "high",
    "evidence_summary": (
        "Verified from the official Fast Grants page on 2026-06-13: OPEN program; "
        "$25K / $50K / $100K tiers; 12-month projects; indirect costs <=15%; "
        "worldwide-eligible institutions (US 501(c)(3) / university / nonprofit "
        "research institute, OR a foreign equivalent with an equivalency "
        "determination or a US-based 501(c)(3) fiscal sponsor). For-profit "
        "companies and individual researchers are NOT eligible. Apply via Airtable."
    ),
    "source_urls": (
        "https://www.biswasfamilyfoundation.org/science/fast-grants\n"
        "https://www.developmentaid.org/grants/view/1663997"
    ),
}


def main() -> None:
    sb = get_client()
    resp = sb.table("donor_intel").upsert(
        DONOR, on_conflict="canonical_key").execute()
    if getattr(resp, "data", None):
        print(f"donor_intel: upserted {DONOR['donor']} "
              f"(canonical_key={DONOR['canonical_key']})")
    else:
        print("WARNING: upsert returned no row — check RLS / columns "
              "(migrations 020/021/025 applied?).")


if __name__ == "__main__":
    main()
