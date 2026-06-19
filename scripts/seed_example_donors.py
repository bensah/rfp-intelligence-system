"""Seed / enrich the two example donors from the shared intelligence decks:
NIHR (docs/Example Donor Intelligence Profile.pdf) and the USAID DIV Fund
(docs/DIV Funds Donnor_Intelligence_and_Strategic_guidance.pptx).

Run AFTER migration 029 is applied:
    python scripts/seed_example_donors.py

It MATCHES an existing donor_intel row by canonical_key / donor / donor_short /
aliases (so it enriches the rows you already have, in place — no duplicates) and
falls back to creating the row if absent. Upsert is PARTIAL: only the columns
below are written; every other field on the row is left untouched.

List fields (funding_mechanism, priority_program_areas, funding_scope_geographic)
and the JSON blocks (funding_tiers_json, past_projects_json) are stored exactly as
the app stores them (json.dumps), so the Donors page renders them natively.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.supabase_client import get_client  # noqa: E402


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _find_key(rows: list[dict], *needles: str) -> str | None:
    """Find an existing canonical_key whose donor/short/aliases/key matches any
    needle (normalised substring match)."""
    wants = [_norm(n) for n in needles]
    for r in rows:
        hay = " ".join(_norm(r.get(f)) for f in
                       ("canonical_key", "donor", "donor_short", "aliases"))
        if any(w and w in hay for w in wants):
            return r["canonical_key"]
    return None


# ── NIHR ────────────────────────────────────────────────────────────────────
NIHR = {
    "donor": "National Institute for Health and Care Research",
    "donor_short": "NIHR",
    "donor_category": "Bilaterals / government development agencies",
    "website": "https://www.nihr.ac.uk/explore-nihr/funding-programmes/global-health.htm",
    "founded": "2006",
    "parent_organization": None,   # superseded by funders_collaborators
    "funders_collaborators": json.dumps(["UK Department of Health and Social Care (DHSC)"]),
    "general_email": "nihrglobalhealth@nihr.ac.uk",
    "hq_country": "United Kingdom",
    "summary_description": (
        "The UK's largest funder of health and care research. Its Global Health "
        "Research (GHR) programme funds LMIC-led or LMIC–UK joint research "
        "partnerships using UK ODA — £734.5M committed since 2016 across the GHR "
        "portfolio (163 active awards, 90 completed, 1,429 researchers trained)."),
    "mission": "Improve the health and wealth of the nation through research.",
    "strategic_priorities": (
        "Global Health Research (GHR) spans five global-health-security focus areas: "
        "antimicrobial resistance (★ 2026 primary theme — bacterial & fungal "
        "pathogens, WHO list), pandemic preparedness, infectious diseases of poverty, "
        "the malnutrition–infection nexus, and vaccines/diagnostics/therapeutics. "
        "Guided by the '4 Is' framework: Impact, Inclusion, Innovation, Investment "
        "(value for money, ODA compliance, sustainability beyond the grant). The "
        "themed call rotates annually; 2026 is the first dedicated AMR theme."),
    "award_low_usd": "£0.5M",
    "award_high_usd": "£5M",
    "total_annual_funding_global": "~£20–30M (2026 AMR themed call, estimated)",
    "total_awards": "253 (163 active + 90 completed since 2016)",
    "total_funding_to_date": "£734.5M (GHR, since 2016)",
    "current_awards": "163 active",
    "past_awards": "90 completed since 2016",
    "funding_mechanism": json.dumps(["Grants"]),
    "funding_programs": (
        "GHR Themed (annual researcher-led calls on rotating health-security themes; "
        "2026 = AMR); Global Professorships; Global Advanced Fellowships; Partnerships "
        "(co-funding with other funders for strategic priorities)."),
    "funding_tiers_json": json.dumps([
        {"name": "Band 1", "amount": "£3–5M", "duration": "Up to 5 years",
         "notes": "Experienced multi-country teams with established partnerships; "
                  "formal MSc/PhD posts; 20–25% of budget for research capacity strengthening."},
        {"name": "Band 2", "amount": "£1.5–3M", "duration": "Up to 4 years",
         "notes": "Primary target for developing partnerships; LMIC–UK co-leads; "
                  "multi-country scope allowed; ~4 awards/year."},
        {"name": "Band 3", "amount": "£0.5–1.5M", "duration": "Up to 3 years",
         "notes": "Lower bar for early-stage teams; single-country focus acceptable; "
                  "good entry for a focused stewardship or diagnostics study."},
    ]),
    # Strategic priority areas — canonical keys from the shared taxonomy
    # (core/program_area_classifier.py), with 0–5 priority grades.
    "priority_program_areas": json.dumps([
        "IDs - Antimicrobial Resistance (AMR)", "Cross-cutting - Diagnostics",
        "Cross-cutting - Research", "HSS - Health Workforce",
        "HSS - Health Financing", "WCH - MNCH", "WCH - Vaccines",
        "Cross-cutting - Digital Health (+AI)", "IDs - Pandemic Response"]),
    "program_area_ratings": json.dumps({
        "IDs - Antimicrobial Resistance (AMR)": 5,
        "Cross-cutting - Diagnostics": 5,
        "Cross-cutting - Research": 5,
        "HSS - Health Workforce": 4,
        "WCH - MNCH": 4,
        "IDs - Pandemic Response": 4,
        "Cross-cutting - Digital Health (+AI)": 3,
        "WCH - Vaccines": 3,
        "HSS - Health Financing": 2,
    }),
    "funding_scope_geographic": json.dumps([
        "Low- and middle-income countries (LMICs)", "Sub-Saharan Africa", "Southern Asia"]),
    "in_scope": (
        "WHO priority bacterial & fungal pathogens (excl. TB); late-phase evaluations "
        "& real-world effectiveness studies; implementation, scale-up & sustainability "
        "evaluation; AMR in vulnerable groups (mother, newborn, child); strengthening "
        "EXISTING AMR surveillance systems; antimicrobial & diagnostic stewardship; "
        "health systems strengthening for AMR; multimorbidity & ageing interactions with AMR."),
    "out_of_scope": (
        "TB, viral or parasitic AMR; Phase 1 & 2 / proof-of-concept; environmental AMR "
        "without human-health outcomes; social-determinants-only research; setting up "
        "NEW surveillance systems; service delivery without a research question; "
        "humanitarian response; infrastructure or commodity procurement alone; "
        "stand-alone training or technical assistance with no research design."),
    "selection_criteria": (
        "Estimated committee weighting (high→low): LMIC relevance & demand, "
        "methodological quality, LMIC-led team, impact & sustainability, inclusive "
        "research, capacity strengthening, community engagement (CEI), partnership "
        "equity, value for money. Five winning traits: LMIC-led; implementation-"
        "focused; capacity-building embedded (20–25% budget, MSc/PhD posts for Bands "
        "1–2); equitable partnerships (co-leads, shared authorship); credible policy "
        "pathway (national-plan alignment, demonstrated government demand)."),
    "direct_local_org_eligible": "Yes — via competitive RFP / invited proposal",
    "active_route_status": "Active",
    "application_process": "Concept note → full proposal (two-stage)",
    "funding_cycle": "Annual",
    "reporting_requirements": "Narrative + financial reports",
    "application_deadlines": "2026 AMR themed call — Stage 1 outline due 1pm UK time, 8 July 2026 (max 5 A4 pages).",
    "submission_portal_url": "https://awardsmanagement.nihr.ac.uk",
    "recent_activity": "2026: first dedicated AMR themed call (GHR).",
    "eligibility_notes": (
        "Funds LMIC-led or LMIC–UK joint research partnerships — NOT implementers "
        "directly. An eligible LMIC academic/research institution must be the (joint) "
        "lead applicant; all co-applicants register on the NIHR Awards Management "
        "System (AMS). UK academic partners add methodological value, not replace LMIC "
        "leadership. Bands 1 & 2 require a joint lead."),
    "strategic_fit_notes": (
        "Strongest applicants pair an LMIC academic/research institution as (joint) "
        "lead with implementing organisations and a UK methods/health-economics "
        "partner. Demonstrated national government demand (e.g. a National Action Plan) "
        "and the ability to build on EXISTING surveillance/evidence are decisive. "
        "Multi-country reach and a prior evidence base (baselines, peer-reviewed "
        "outputs) strengthen Band 1–2 bids."),
    "gaps_risks": (
        "Common disqualifiers: framing work as service delivery / a programme rather "
        "than research; weak or vague methodology (the single most common rejection "
        "reason); no eligible LMIC lead applicant; missing health-economics / cost-"
        "effectiveness expertise; no credible policy-uptake pathway; under-budgeting "
        "research capacity strengthening (must be 20–25% for Bands 1–2)."),
    "recommended_approach": (
        "Frame the work as implementation research ('an intervention to be evaluated'), "
        "not service delivery. Secure an eligible LMIC academic lead early and register "
        "all co-applicants on NIHR AMS before the deadline. Band 2 (£1.5–3M, up to 4 "
        "yrs) is the typical entry for developing partnerships; Band 3 for single-"
        "country early-stage teams; Band 1 once a multi-country track record exists. "
        "Map every work-package output to a national policy objective and embed MSc/PhD "
        "training posts."),
    "verification_level": "high",
    "evidence_summary": (
        "Sourced from the NIHR GHR Themed Programme call (Ref 2026/402–404), the NIHR "
        "GHR webinar (12 May 2026), and NIHR portfolio data (April 2026)."),
    "source_urls": "https://www.nihr.ac.uk/explore-nihr/funding-programmes/global-health.htm\nhttps://awardsmanagement.nihr.ac.uk",
    "past_projects_json": json.dumps([
        {"title": "NIHR GHR — AMR Research Units & Groups", "amount": None,
         "currency": None, "year": "2023", "country": "Kenya · Nigeria · India",
         "stage": "Completed",
         "description": "LMIC research units/groups on carbapenems, neonatal sepsis and stewardship; UK–LMIC partnerships."},
        {"title": "Fleming Fund MAAP (DHSC/NIHR-linked)", "amount": None,
         "currency": None, "year": "2022", "country": "Cameroon", "stage": "Regional grant",
         "description": "AMR/AMU mapping 2017–2019 — the only rigorous national baseline; a direct foundation for the 2026 call."},
        {"title": "NIHR Global Research Professorship — AMR (NIHR300791)", "amount": None,
         "currency": None, "year": None, "country": "Sub-Saharan Africa", "stage": "Ongoing",
         "description": "Researcher co-authored Lancet 2024 (Naghavi et al.), cited in the 2026 call justification."},
        {"title": "NIHR/MRC — Neonatal AMR Sepsis", "amount": None, "currency": None,
         "year": None, "country": "Kenya · Malawi · Tanzania", "stage": "Completed",
         "description": "Neonatal AMR sepsis across 7 LMIC countries; a precedent for MNCH/AMR integration."},
        {"title": "NIHR — Diagnostic Stewardship AMR", "amount": None, "currency": None,
         "year": None, "country": "Sub-Saharan Africa", "stage": "2026 theme",
         "description": "Strengthening existing AMR surveillance systems and affordable rapid diagnostics."},
    ]),
    # Flags (yes only — blanks stay 'not documented'). Program areas now live in
    # priority_program_areas + program_area_ratings, NOT the deprecated *_fit flags.
    "ngo_eligible": "no", "for_profit_eligible": "no",
    "subrecipient_partner_possible": "yes", "grant_route": "yes",
    "open_call_unsolicited": "yes", "two_stage_application": "yes",
    "online_portal_submission": "yes", "lmic_africa_focus": "yes",
    "global_multi_country_scope": "yes", "partnership_mandatory": "yes",
    "local_partner_required": "yes",
    "concept_note_required": "yes", "full_technical_proposal_required": "yes",
    "detailed_budget_required": "yes", "budget_narrative_required": "yes",
    "theory_of_change_required": "yes", "mande_plan_required": "yes",
    "cvs_key_personnel_required": "yes", "letters_of_support_required": "yes",
    "ethics_irb_approval_required": "yes", "gender_inclusion_plan_required": "yes",
    "sustainability_exit_plan_required": "yes", "references_required": "yes",
}

# ── DIV Fund (independent evidence-to-scale fund; div.fund) ───────────────────
DIV = {
    "donor": "Development Innovation Ventures Fund",
    "donor_short": "DIV Fund",
    "donor_category": "International philanthropies & foundations",
    "website": "https://www.div.fund",
    "founded": None,
    "parent_organization": None,
    "hq_country": "United States",
    "summary_description": (
        "An independent, evidence-driven 'discovery engine for global development' — "
        "the DIV Fund fosters, tests and scales innovations that measurably improve "
        "the health and welfare of people living in poverty in low- and middle-income "
        "countries. It awards tiered grants (pilot → test → scale) selected on rigorous "
        "causal evidence, cost-effectiveness, and a credible path to reach 1M+ people "
        "with sustainable financing. Its founders previously built and ran "
        "evidence-to-scale programmes — CEO Sasha Gallant formerly led USAID's "
        "Development Innovation Ventures programme, CIO Jeff Brown is a former CEO of "
        "Evidence Action and the Global Innovation Fund, and board chair Michael Kremer "
        "is a 2019 Nobel laureate in economics."),
    "mission": (
        "Foster, test and scale innovative ideas that improve the health and welfare of "
        "people living in poverty around the world."),
    "strategic_priorities": (
        "Open to ALL sectors in LMICs — commercial and public-sector solutions alike. "
        "Recent portfolio spans Health, Agriculture/Food Security, Education, Economic "
        "Growth, WASH, Energy, Environment, and Democracy/Rights/Governance. Three "
        "investment principles: evidence of impact, cost-effectiveness, and durable "
        "scale (reaching at least 1M people, with a sustainable financing pathway)."),
    "award_low_usd": "$200K",
    "award_high_usd": "$1.5M",
    "total_awards": None,
    "total_funding_to_date": None,
    "funding_mechanism": json.dumps(["Grants"]),
    "funding_programs": (
        "Open, rolling RFP awarded in three evidence tiers: Stage 1 (Pilot, ≤ $200K), "
        "Stage 2 (Test & position for scale, ≤ $500K — exceptionally ≤ $750K), and "
        "Stage 3 (Transition to scale, ≤ $1.5M); each up to 5 years."),
    "funding_tiers_json": json.dumps([
        {"name": "Stage 1 — Pilot", "amount": "≤ $200K", "duration": "Up to 5 years",
         "notes": "Real-world pilots of early-stage innovations (post-prototype); "
                  "robust theory of change and a sketched evaluation plan."},
        {"name": "Stage 2 — Test & position for scale", "amount": "≤ $500K (exceptionally ≤ $750K)",
         "duration": "Up to 5 years",
         "notes": "Typical target tier. Rigorous causal evaluation (RCT / quasi-"
                  "experimental) of impact plus market-viability / scale assessment."},
        {"name": "Stage 3 — Transition to scale", "amount": "≤ $1.5M", "duration": "Up to 5 years",
         "notes": "Proven innovations scaling to widespread adoption; existing causal "
                  "evidence and a sustainable (public, commercial or hybrid) financing path."},
    ]),
    # Strategic priority areas — mapped from div.fund's own sectors + portfolio
    # distribution (Health 10 · Agriculture/Food Security 4 · Education 4 ·
    # Environment 2 · Economic Growth 1 · Democracy/Rights/Governance 1 · WASH 1),
    # nuanced by what their grantees actually do. Grades scale with that emphasis.
    "priority_program_areas": json.dumps([
        # Health (dominant sector) ---------------------------------------------
        "Cross-cutting - Digital Health (+AI)", "IDs - Malaria & NTDs",
        "WCH - MNCH", "WCH - Vaccines", "HSS - Health Workforce",
        "Cross-cutting - Research", "Cross-cutting - Diagnostics",
        # Agriculture / Food Security ------------------------------------------
        "AGRI - Food Security & Resilience", "AGRI - Smallholder Productivity",
        # Education ------------------------------------------------------------
        "EDU - Literacy & Numeracy", "EDU - Education Technology",
        # WASH -----------------------------------------------------------------
        "WASH - Safe Water", "WASH - Sanitation",
        # Environment / Energy -------------------------------------------------
        "ENV - Clean & Renewable Energy", "ENV - Climate Adaptation & Resilience",
        # Economic Growth ------------------------------------------------------
        "ECON - Social Protection", "ECON - Financial Inclusion",
        "ECON - Jobs & Skills",
        # Governance + Humanitarian (light) ------------------------------------
        "GOV - Democracy & Civic Participation", "HUM - Emergency Response"]),
    "program_area_ratings": json.dumps({
        # Health — DIV's largest, most active sector
        "Cross-cutting - Digital Health (+AI)": 5,   # Dimagi, Maisha Meds, Simprints
        "IDs - Malaria & NTDs": 5,                   # antimalarial access, malaria-vaccine uptake
        "WCH - MNCH": 4,
        "WCH - Vaccines": 4,
        "HSS - Health Workforce": 4,                 # community health workers
        "Cross-cutting - Research": 4,               # evidence generation is core to DIV
        "Cross-cutting - Diagnostics": 3,
        # Agriculture / Food Security (count 4)
        "AGRI - Food Security & Resilience": 4,
        "AGRI - Smallholder Productivity": 3,
        # Education (count 4)
        "EDU - Literacy & Numeracy": 4,              # Teaching at the Right Level
        "EDU - Education Technology": 2,
        # WASH (notable grantees: Evidence Action, WASH Institute, TERI)
        "WASH - Safe Water": 4,
        "WASH - Sanitation": 3,
        # Environment / Energy (count 2; BURN cookstoves)
        "ENV - Clean & Renewable Energy": 3,
        "ENV - Climate Adaptation & Resilience": 2,
        # Economic Growth (count 1; poverty graduation, cash transfers)
        "ECON - Social Protection": 3,
        "ECON - Financial Inclusion": 2,
        "ECON - Jobs & Skills": 2,
        # Governance (count 1) + Disaster Relief (light)
        "GOV - Democracy & Civic Participation": 2,
        "HUM - Emergency Response": 1,
    }),
    "funding_scope_geographic": json.dumps([
        "Low- and middle-income countries (LMICs)", "Sub-Saharan Africa", "Asia"]),
    "funders_collaborators": json.dumps([
        "Coefficient Giving", "GiveWell", "Livelihood Impact Fund", "CRI Foundation",
        "Global Development Incubator", "Anonymous Donors"]),
    "in_scope": (
        "Innovations ready for real-world testing in LMICs with potential to reach "
        "millions — products, services, policies, programs, delivery models, and "
        "rigorous evidence generation for widely-used approaches that lack evaluation. "
        "Commercial, public-sector and hybrid solutions are all eligible, in any sector."),
    "out_of_scope": (
        "Lab-stage or idea-stage innovations not yet ready for real-world testing; "
        "applications from individuals (not eligible); solutions with no credible path "
        "to durable scale or cost-effectiveness."),
    "selection_criteria": (
        "Three core principles: (1) Evidence of impact — causal measurement that the "
        "innovation caused the improvement; (2) Cost-effectiveness — more impact per "
        "dollar than alternatives; (3) Durable scale — a credible path to reach at "
        "least 1M people over ~10 years with sustainable (public, commercial or hybrid) "
        "financing. The evidence tier sets the bar — higher stages need stronger proof."),
    "active_route_status": "Active",
    "application_process": "Online portal submission",
    "funding_cycle": "Rolling / open call (no fixed deadline)",
    "reporting_requirements": "Milestone / deliverable-based",
    "application_deadlines": "Rolling / open call — no fixed deadline; apply anytime.",
    "submission_portal_url": "https://www.div.fund/apply",
    "recent_activity": "Actively accepting proposals across all sectors in LMICs (rolling RFP).",
    "eligibility_notes": (
        "Open to non-profits, social enterprises, universities, research institutions, "
        "technical-assistance providers, businesses and multi-organisation partnerships. "
        "Individuals are NOT eligible. Technical assistance is also offered to "
        "governments, bilateral / multilateral agencies and philanthropies."),
    "strategic_fit_notes": (
        "Best-fit applicants bring a discrete, testable innovation with a clear cost-"
        "per-beneficiary and a realistic route to durable scale (public, commercial or "
        "hybrid). Prior causal evidence and an engaged scale partner materially "
        "strengthen applications, particularly at Stage 2–3."),
    "gaps_risks": (
        "Common disqualifiers: lab/idea-stage innovations not ready for field testing; "
        "no rigorous (causal) evaluation design; no credible cost-effectiveness case; "
        "no realistic path to ~1M people; applications from individuals (ineligible)."),
    "recommended_approach": (
        "Target Stage 2 (Test, ≤ $500K) with a rigorous causal evaluation (RCT / "
        "quasi-experimental) and a documented cost-per-beneficiary. Use Stage 1 to "
        "establish proof-of-concept; pursue Stage 3 only with existing causal evidence "
        "and a financial-sustainability / scale-integration plan."),
    "verification_level": "high",
    "evidence_summary": (
        "Sourced directly from div.fund — the About, RFP (apply/rfp) and Portfolio "
        "pages (2026)."),
    "source_urls": ("https://www.div.fund/about\nhttps://www.div.fund/apply/rfp\n"
                    "https://www.div.fund/portfolio"),
    # 9 representative grantees across Stage 1–3 (links point to the DIV portfolio).
    "past_projects_json": json.dumps([
        {"title": "TERI — Riverbank filtration with sensors", "amount": 200000,
         "currency": "USD", "year": "2022", "country": "India", "stage": "Stage 1",
         "description": "Sensor-automated riverbank water filtration for safe water and "
                        "optimised irrigation; farmer-led collective-purchase viability test.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Simprints — Biometric ID for vaccination", "amount": None,
         "currency": "USD", "year": None, "country": "Ghana", "stage": "Stage 2",
         "description": "Fingerprint biometric ID to lift malaria-vaccine coverage by "
                        "accurately tracking children and dose completeness.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Semilla Nueva — Biofortified maize", "amount": None,
         "currency": "USD", "year": None, "country": "Guatemala",
         "stage": "Stage 2", "description": "Testing improved biofortified maize seeds "
                        "and subsidies to cut malnutrition.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Swoop Aero — Medical drones", "amount": 870000, "currency": "USD",
         "year": "2022", "country": "Malawi", "stage": "Stage 2",
         "description": "Drone delivery of medicines / vaccines / lab results to ~100 "
                        "remote facilities; a rigorous RCT on medical-drone impact.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Health Access Connect", "amount": 1500000, "currency": "USD",
         "year": "2024", "country": "Uganda", "stage": "Stage 2",
         "description": "Community-led outreach clinics improving access to care in "
                        "rural areas.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Dimagi (CommCare)", "amount": 1500000, "currency": "USD", "year": "2024",
         "country": "India / Nigeria", "stage": "Stage 2",
         "description": "Adding precision supervision to the CommCare CHW platform and "
                        "evaluating it at scale.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Maisha Meds", "amount": 5250000, "currency": "USD", "year": "2023",
         "country": "Kenya (+ UG/TZ/NG)", "stage": "Stage 3",
         "description": "Digital platform improving access to quality antimalarials in "
                        "private pharmacies; >1M patients/yr.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Evidence Action — Dispensers for Safe Water", "amount": 5500000,
         "currency": "USD", "year": "2012", "country": "Kenya · Uganda · Malawi",
         "stage": "Stage 3", "description": "Chlorine dispensers at water points; "
                        "~43% adoption vs ~6% control; <$0.50/person/yr; millions reached.",
         "link": "https://www.div.fund/portfolio"},
        {"title": "Pratham / TaRL Africa — Teaching at the Right Level", "amount": None,
         "currency": "USD", "year": None, "country": "Multi-country (incl. Zambia)",
         "stage": "Stage 3", "description": "Grouping learners by actual level; scaled "
                        "through government school systems — benefiting tens of millions of children.",
         "link": "https://www.div.fund/portfolio"},
    ]),
    # Flags. Program areas now live in priority_program_areas + program_area_ratings.
    "ngo_eligible": "yes", "for_profit_eligible": "yes",
    "subrecipient_partner_possible": "yes", "grant_route": "yes",
    "open_call_unsolicited": "yes", "online_portal_submission": "yes",
    "lmic_africa_focus": "yes", "global_multi_country_scope": "yes",
    "theory_of_change_required": "yes", "mande_plan_required": "yes",
    "detailed_budget_required": "yes", "partner_mou_required": "yes",
    "sustainability_exit_plan_required": "yes",
}


# ── DIV Fund backers / collaborators — added as donor records + partner options ──
COEFFICIENT_GIVING = {
    "donor": "Coefficient Giving", "donor_short": "Coefficient Giving",
    "aliases": "Open Philanthropy; Open Phil",
    "donor_category": "International philanthropies & foundations",
    "website": "https://www.coefficientgiving.org", "founded": "2017",
    "hq_country": "United States",
    "summary_description": (
        "A US philanthropic adviser and funder (formerly Open Philanthropy; rebranded "
        "in 2025) that finds and funds outstanding giving opportunities at scale — "
        ">$4B directed to date. Now operates multi-donor 'funds' other philanthropists "
        "can join; anchor backing from Dustin Moskovitz and Cari Tuna."),
    "mission": "Give as well as possible — maximise impact per dollar through evidence and reasoning.",
    "strategic_priorities": (
        "Program areas are structured as multi-donor funds: global health & wellbeing "
        "(incl. global health R&D and scientific research), pandemic preparedness / "
        "biosecurity, farm animal welfare, and risks from advanced AI."),
    "funding_mechanism": json.dumps(["Grants", "Program-related investments (equity/debt)"]),
    "priority_program_areas": json.dumps([
        "IDs - Pandemic Response", "Cross-cutting - Research", "WCH - Vaccines",
        "Cross-cutting - Diagnostics", "Cross-cutting - Digital Health (+AI)"]),
    "program_area_ratings": json.dumps({
        "IDs - Pandemic Response": 5, "Cross-cutting - Research": 5,
        "WCH - Vaccines": 3, "Cross-cutting - Diagnostics": 3,
        "Cross-cutting - Digital Health (+AI)": 3}),
    "funding_scope_geographic": json.dumps([
        "Global / worldwide", "Low- and middle-income countries (LMICs)"]),
    "eligibility_notes": (
        "Largely proactive / invitation-driven grantmaking sourced through its own "
        "research and trusted recommenders — not a broad open call. Funds nonprofits "
        "and, via program-related investments, some for-profits."),
    "verification_level": "medium",
    "evidence_summary": "From coefficientgiving.org and reporting on the 2025 Open Philanthropy → Coefficient Giving rebrand.",
    "source_urls": "https://www.coefficientgiving.org\nhttps://en.wikipedia.org/wiki/Coefficient_Giving",
    "ngo_eligible": "yes", "grant_route": "yes", "invitation_solicited": "yes",
    "open_call_unsolicited": "no",
}

GIVEWELL = {
    "donor": "GiveWell", "donor_short": "GiveWell",
    "donor_category": "International philanthropies & foundations",
    "website": "https://www.givewell.org", "founded": "2007",
    "hq_country": "United States",
    "summary_description": (
        "A US nonprofit charity evaluator turned major grantmaker (>$400M/yr). Finds "
        "and funds evidence-backed, highly cost-effective programmes that save or "
        "improve lives in low- and lower-middle-income countries, and publishes all "
        "its research openly. Channels funding via its Top Charities Fund and All "
        "Grants Fund."),
    "mission": "Find and fund the giving opportunities that save or improve the most lives per dollar.",
    "strategic_priorities": (
        "Evidence-backed, cost-effective global health & development — malaria (nets, "
        "seasonal chemoprevention), vitamin A supplementation, vaccination / "
        "immunisation incentives, safe water, and maternal & child health."),
    "funding_mechanism": json.dumps(["Grants"]),
    "priority_program_areas": json.dumps([
        "IDs - Malaria & NTDs", "WCH - Nutrition", "WCH - Vaccines",
        "WASH - Safe Water", "WCH - MNCH", "Cross-cutting - Research"]),
    "program_area_ratings": json.dumps({
        "IDs - Malaria & NTDs": 5, "WCH - Nutrition": 4, "WCH - Vaccines": 4,
        "WASH - Safe Water": 4, "WCH - MNCH": 3, "Cross-cutting - Research": 4}),
    "funding_scope_geographic": json.dumps([
        "Low- and middle-income countries (LMICs)", "Sub-Saharan Africa", "Asia"]),
    "eligibility_notes": (
        "Highly selective and research-led: funds a small set of vetted Top Charities "
        "plus targeted grants that clear a strict cost-effectiveness bar. Not a broad "
        "open call."),
    "verification_level": "high",
    "evidence_summary": "From givewell.org (How We Work, Top Charities Fund) and public profiles.",
    "source_urls": "https://www.givewell.org\nhttps://www.givewell.org/how-we-work",
    "ngo_eligible": "yes", "grant_route": "yes", "invitation_solicited": "yes",
}

LIVELIHOOD_IMPACT_FUND = {
    "donor": "Livelihood Impact Fund", "donor_short": "LIF",
    "donor_category": "International philanthropies & foundations",
    "founded": None, "hq_country": "United States",
    "summary_description": (
        "A US grantmaking foundation that helps people in poverty toward self-"
        "sufficiency with skills, capital and opportunities. Backs scalable programmes "
        "delivering at least 5× returns in future earnings per dollar, with multi-year, "
        "trust-based funding focused on measurable income growth. ~$23M granted in 2023 "
        "(64 awards, ~$100K–$300K each); works across Africa (originally Cambodia)."),
    "mission": "Improve the lives of the global poor by equipping individuals and families for self-sufficiency.",
    "strategic_priorities": (
        "Livelihoods & economic self-sufficiency — vocational / skills training, jobs, "
        "financial inclusion and economic empowerment, judged on measurable income growth."),
    "funding_mechanism": json.dumps(["Grants"]),
    "award_low_usd": "$100K", "award_high_usd": "$300K",
    "priority_program_areas": json.dumps([
        "ECON - Jobs & Skills", "EDU - Higher Education & TVET",
        "ECON - Financial Inclusion", "ECON - Social Protection", "GES - Youth Empowerment"]),
    "program_area_ratings": json.dumps({
        "ECON - Jobs & Skills": 5, "EDU - Higher Education & TVET": 4,
        "ECON - Financial Inclusion": 4, "ECON - Social Protection": 3,
        "GES - Youth Empowerment": 3}),
    "funding_scope_geographic": json.dumps([
        "Sub-Saharan Africa", "Low- and middle-income countries (LMICs)"]),
    "eligibility_notes": (
        "Trust-based, multi-year funding to vetted high-impact organisations; strongly "
        "outcomes-focused (income growth, lives transformed). Largely sourced / invited."),
    "verification_level": "medium",
    "evidence_summary": "From DevelopmentAid and BFA Global profiles of the Livelihood Impact Fund.",
    "source_urls": "https://bfaglobal.com/livelihood-impact-fund/",
    "ngo_eligible": "yes", "grant_route": "yes", "invitation_solicited": "yes",
}

CRI_FOUNDATION = {
    "donor": "CRI Foundation", "donor_short": "CRI",
    "aliases": "Child Relief International Foundation",
    "donor_category": "International philanthropies & foundations",
    "website": "https://crifoundation.org", "founded": "2006",
    "hq_country": "United States",
    "summary_description": (
        "A New York private foundation (formerly Child Relief International Foundation; "
        "founded 2006 by Andrew & Bonnie Weiss; >$135M assets) funding cost-effective, "
        "catalytic work to improve the lives of people in extreme poverty — primarily "
        "health in sub-Saharan Africa. Requires organisations to be evidence-based or "
        "evidence-generating with rigorous evaluation; partners with the DIV Fund "
        "(CRI-DIV collaborative) since 2019."),
    "mission": "Improve the lives of people in extreme poverty through cost-effective, catalytic, evidence-based giving.",
    "strategic_priorities": (
        "Cost-effective health in sub-Saharan Africa; evidence-based or evidence-"
        "generating interventions with rigorous evaluation; catalytic, collaborative "
        "funding (e.g. the CRI-DIV collaborative)."),
    "funding_mechanism": json.dumps(["Grants"]),
    "funders_collaborators": json.dumps(["Development Innovation Ventures Fund"]),
    "priority_program_areas": json.dumps([
        "Cross-cutting - Research", "IDs - Malaria & NTDs", "WCH - MNCH",
        "HSS - Health Workforce", "WCH - Nutrition"]),
    "program_area_ratings": json.dumps({
        "Cross-cutting - Research": 5, "IDs - Malaria & NTDs": 4, "WCH - MNCH": 4,
        "HSS - Health Workforce": 3, "WCH - Nutrition": 3}),
    "funding_scope_geographic": json.dumps([
        "Sub-Saharan Africa", "Low- and middle-income countries (LMICs)"]),
    "eligibility_notes": (
        "Funds evidence-based or evidence-generating organisations with rigorous "
        "evaluation frameworks; emphasis on cost-effective, catalytic opportunities, "
        "often through collaboratives (e.g. with the DIV Fund)."),
    "verification_level": "medium",
    "evidence_summary": "From crifoundation.org (Collaboratives, CRI-DIV) and public foundation profiles.",
    "source_urls": "https://crifoundation.org\nhttps://crifoundation.org/cri-div/",
    "ngo_eligible": "yes", "grant_route": "yes", "prior_track_record_required": "yes",
    "invitation_solicited": "yes",
}

GLOBAL_DEV_INCUBATOR = {
    "donor": "Global Development Incubator", "donor_short": "GDI",
    "donor_category": "International philanthropies & foundations",
    "website": "https://globaldevincubator.org", "founded": "2007",
    "hq_country": "United States",
    "summary_description": (
        "A Washington DC nonprofit incubator (founded 2007) that brings together ideas, "
        "leaders and capital to build and scale social-impact ventures. Runs a 12–36 "
        "month incubation (Discover → Design → Build → Exit) and has shaped 40+ ventures "
        "across health, agriculture, inclusive finance, climate, youth employment and "
        "economic inclusion in LMICs. Acts as an intermediary / venture-builder more "
        "than a pure grantmaker."),
    "mission": "Bring together ideas, leaders and capital to build and scale the next generation of social solutions.",
    "strategic_priorities": (
        "Builds & scales ventures across digital health, inclusive / smallholder "
        "finance, agriculture, climate & ecosystems, youth employment, economic "
        "inclusion and MSME development."),
    "funding_mechanism": json.dumps(["Technical assistance", "Co-financing", "Grants"]),
    "priority_program_areas": json.dumps([
        "Cross-cutting - Digital Health (+AI)", "ECON - Financial Inclusion",
        "AGRI - Smallholder Productivity", "ECON - Jobs & Skills",
        "ENV - Climate Adaptation & Resilience", "HSS - Health Workforce"]),
    "program_area_ratings": json.dumps({
        "Cross-cutting - Digital Health (+AI)": 4, "ECON - Financial Inclusion": 4,
        "AGRI - Smallholder Productivity": 4, "ECON - Jobs & Skills": 3,
        "ENV - Climate Adaptation & Resilience": 3, "HSS - Health Workforce": 3}),
    "funding_scope_geographic": json.dumps([
        "Low- and middle-income countries (LMICs)", "Sub-Saharan Africa"]),
    "eligibility_notes": (
        "Operates as an incubator / intermediary — partners with funders and "
        "entrepreneurs to build ventures; engagement is selective and relationship-"
        "driven rather than an open grant call."),
    "verification_level": "medium",
    "evidence_summary": "From globaldevincubator.org and the Devex organisation profile.",
    "source_urls": "https://globaldevincubator.org\nhttps://www.devex.com/organizations/global-development-incubator-gdi-56305",
    "ngo_eligible": "yes", "subrecipient_partner_possible": "yes", "grant_route": "yes",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "donor"


def main() -> int:
    sb = get_client()
    rows = sb.table("donor_intel").select(
        "canonical_key, donor, donor_short, aliases").execute().data or []

    plan = [
        (NIHR, ("nihr", "national institute for health and care research")),
        (DIV, ("div fund", "development innovation ventures", "usaid div")),
        # DIV Fund backers / collaborators — also added as full donor records.
        (COEFFICIENT_GIVING, ("coefficient giving", "open philanthropy", "open phil")),
        (GIVEWELL, ("givewell",)),
        (LIVELIHOOD_IMPACT_FUND, ("livelihood impact fund",)),
        (CRI_FOUNDATION, ("cri foundation", "child relief international")),
        (GLOBAL_DEV_INCUBATOR, ("global development incubator",)),
    ]
    for payload, needles in plan:
        key = _find_key(rows, *needles)
        if key:
            action = f"updating existing row '{key}'"
        else:
            key = _slug(payload["donor"])
            existing = {r["canonical_key"] for r in rows}
            i = 2
            while key in existing:
                key, i = f"{_slug(payload['donor'])}_{i}", i + 1
            action = f"creating new row '{key}'"
        payload = {**payload, "canonical_key": key}
        print(f"• {payload['donor']} — {action} ({len(payload)} fields)")
        resp = sb.table("donor_intel").upsert(
            payload, on_conflict="canonical_key").execute()
        if not getattr(resp, "data", None):
            print(f"  ✗ write returned no row (RLS / column mismatch?) — check migration 029.")
            return 1
    print("\nDone. Open the Donors page → tick NIHR / DIV Fund → 👁 View to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
