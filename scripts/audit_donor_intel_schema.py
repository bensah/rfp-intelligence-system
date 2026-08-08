"""Propose the donor_intel column cull (action #11) — REPORT ONLY, never writes.

The owner's specification for what donor_intel should be:

    "a comprehensive donor intelligence dataset that profiles the donor completely into
     a set of structured questions, to serve as FALLBACK when the funding call itself
     doesn't carry all the information — the general guidelines a donor publishes for
     applicants, plus the strict requirements they want applicants to comply with."

…and what it should NOT be:

    "take out redundancies, take out questions that don't make sense under a donor
     profile, remove anything that is subject to the funding CALL (data-management plan,
     M&E plan, CVs of key personnel — case by case per call, and donors are not strict
     about them). While we wired these requirements, on the org-profile side there is no
     alternative to match — essentially a useless match-making indicator."

FOUR TESTS a column must pass to be kept:
  T1  a property of the DONOR (standing guidance), not of an individual call
  T2  has a plausible ORG-PROFILE counterpart to match against, OR is descriptive
      context a human reads (identity / narrative)
  T3  non-redundant — no other column already carries the same fact
  T4  structured, or could reasonably become so

This prints the proposed verdict per column with the EVIDENCE — fill rate across the
real donor rows, and whether anything in the repo still reads it — so the cull can be
signed off before a single column is dropped. It writes nothing and touches no schema.

Usage:
    python scripts/audit_donor_intel_schema.py            # full report
    python scripts/audit_donor_intel_schema.py --drops    # just the drop list
    python scripts/audit_donor_intel_schema.py --sql      # emit the migration to review
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from db.supabase_client import service_client                     # noqa: E402

# ── The proposal ────────────────────────────────────────────────────────────────
# Grouped by the REASON for dropping, because the rationale is the point, not the list.
# Anything not named below is KEPT.

# T1 — the CALL decides these, not the donor. Each is a per-application document whose
# requirement varies call by call, and none has an org-profile counterpart: an org
# cannot pre-hold "a data-management plan" the way it holds a tax-exempt certificate.
CALL_SPECIFIC = [
    "donor_bank_details_required", "donor_budget_narrative_required",
    "donor_concept_note_required", "donor_cvs_key_personnel_required",
    "donor_data_management_plan_required", "donor_detailed_budget_required",
    "donor_due_diligence_questionnaire_required", "donor_environmental_safeguard_required",
    "donor_ethics_irb_approval_required", "donor_full_technical_proposal_required",
    "donor_gender_inclusion_plan_required", "donor_letters_of_support_required",
    "donor_logframe_results_framework_required", "donor_mande_plan_required",
    "donor_procurement_plan_required", "donor_references_required",
    "donor_registration_certificate_required", "donor_risk_management_plan_required",
    "donor_sustainability_exit_plan_required", "donor_theory_of_change_required",
    "donor_workplan_timeline_required", "org_capacity_statement_required",
    "org_chart_staffing_required",
]

# T3 — the same fact already lives in donor_priority_areas + donor_priority_ratings,
# which is what MUST-2 actually reads. These booleans are a second, coarser copy that
# nothing reconciles against the first.
REDUNDANT_THEME_FLAGS = [
    "donor_agriculture_food_security_fit", "donor_climate_environment_fit",
    "donor_digital_health_data_ai_fit", "donor_economic_development_fit",
    "donor_education_fit", "donor_governance_equity_rights_fit", "donor_hiv_aids_fit",
    "donor_hss_fit", "donor_immunization_vaccines_fit", "donor_infectious_diseases_fit",
    "donor_malaria_fit", "donor_mnch_fit", "donor_ncds_fit", "donor_nutrition_fit",
    "donor_srhr_family_planning_fit", "donor_tb_fit",
]

# Dead — zero of the donor rows filled, and nothing reads them.
DEAD = [
    "donor_contact_linkedin_urls", "donor_contact_persons", "donor_contact_phones",
    "donor_fund_use_conditions", "donor_funded_geographies",
    "donor_funding_platform_registration_required", "donor_funding_platform_url",
    "donor_independent_entity_required", "donor_min_track_record",
]

# T3 — duplicated by a better-populated neighbour.
SUPERSEDED = {
    "donor_indirect_cost_disallowed": "donor_indirect_cost_max_pct (0% is the same fact)",
    "donor_in_scope": "donor_geographic_scope",
    "donor_out_of_scope": "donor_geographic_scope",
    "donor_past_awards": "donor_past_projects_json",
    "donor_current_awards": "donor_past_projects_json",
    "donor_strategic_fit_notes": "notes",
    "donor_gaps_risks": "notes",
    "donor_recommended_approach": "notes",
}

# T4 — keep the fact, but it must stop being a free-text box.
RESTRUCTURE = {
    "donor_funding_programs": ("the owner: 'unclear what kind of data we need to input'. "
                               "Replace with rows of {programme, award low, award high, "
                               "duration months, cycle} — the shape donor_funding_tiers_json "
                               "already uses successfully"),
    "donor_funding_cycle": "enum: rolling | annual | biannual | quarterly | ad-hoc",
    "donor_application_process": "enum: open call | invitation only | two-stage | EOI first",
    "award_size_basis": "enum: per project | per year | total programme",
}

REASONS = [
    ("call-specific — the call decides, not the donor; no org counterpart", CALL_SPECIFIC),
    ("redundant — donor_priority_areas/_ratings already carry this", REDUNDANT_THEME_FLAGS),
    ("dead — nothing filled, nothing reads it", DEAD),
    ("superseded by a better-populated column", list(SUPERSEDED)),
]


def _repo_reads(col: str) -> list[str]:
    """Every non-script repo file that mentions the column name in quotes."""
    hits = []
    for d in ("core", "views", "app_pages", "auth"):
        for f in (_ROOT / d).rglob("*.py"):
            if "sync-conflict" in f.name:
                continue
            try:
                if re.search(r"[\"']" + re.escape(col) + r"[\"']",
                             f.read_text(encoding="utf-8")):
                    hits.append(str(f.relative_to(_ROOT)).replace("\\", "/"))
            except Exception:
                pass
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drops", action="store_true", help="just the drop list")
    ap.add_argument("--sql", action="store_true", help="emit the migration for review")
    args = ap.parse_args()

    rows = service_client().table("donor_intel").select("*").limit(500).execute().data or []
    total = len(rows)
    present = set(rows[0]) if rows else set()

    def filled(c: str) -> int:
        return sum(1 for r in rows
                   if r.get(c) not in (None, "", [], {})
                   and str(r.get(c)).strip().lower() not in ("false", "none", "-", "—"))

    proposed, blocked = [], []
    for reason, cols in REASONS:
        for c in cols:
            if c not in present:
                continue
            reads = _repo_reads(c)
            (blocked if reads else proposed).append((c, reason, filled(c), reads))

    if not args.drops and not args.sql:
        print(f"donor_intel: {len(present)} columns across {total} donors\n")
        for reason, cols in REASONS:
            live = [c for c in cols if c in present]
            print(f"-- {reason}  ({len(live)})")
            for c in sorted(live):
                rd = _repo_reads(c)
                flag = f"   [!] still read by {', '.join(rd)}" if rd else ""
                print(f"     {c:48} filled {filled(c):3}/{total}{flag}")
            print()
        print("-- restructure (keep the fact, change the shape)")
        for c, how in RESTRUCTURE.items():
            if c in present:
                print(f"     {c:48} filled {filled(c):3}/{total}")
                print(f"        -> {how}")
        print()

    print(f"SAFE TO DROP : {len(proposed)}")
    for c, reason, n, _ in sorted(proposed):
        print(f"   {c:48} ({n:3}/{total})  {reason.split(' — ')[0]}")
    if blocked:
        print(f"\nBLOCKED — still read in code; remove the reader first: {len(blocked)}")
        for c, _reason, n, reads in sorted(blocked):
            print(f"   {c:48} ({n:3}/{total})  <- {', '.join(reads)}")

    if args.sql:
        print("\n-- REVIEW BEFORE RUNNING. Not auto-applied.")
        print("-- Take a backup of donor_intel first: the data in these columns is gone.")
        for c, *_ in sorted(proposed):
            print(f"ALTER TABLE donor_intel DROP COLUMN IF EXISTS {c};")

    print("\nREPORT ONLY — nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
