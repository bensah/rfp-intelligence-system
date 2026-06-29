# RFPIS Data Model & Dictionary — v1 DRAFT (for sign-off)

> **Status: proposal only — nothing renamed yet.** This is the design artifact to
> review BEFORE any migration. Once approved, renames execute **one comparison axis at a
> time** (SQL migration + code update + verification per step). Last updated 2026-06-29.

The goal: make the *scoring mapping auditable* — when you read a criterion you can see the
org field and the call/donor field share a **stem**, so a one-to-one comparison is obvious.

---

## 1. Naming convention

Every field carries a **semantic-role prefix** (not just a table prefix — `rfp_submissions`
mixes roles):

| Prefix | Role | Lives in |
|---|---|---|
| `org_` | Deploying-org attribute / preference | `app_settings.org_profile` (JSON) |
| `donor_` | Donor attribute or **requirement the donor imposes** | `donor_intel` |
| `call_` | **Call-extracted** attribute (this specific solicitation) | `rfp_submissions`, `extracted_solicitations` |
| `fund_` | **Derived** donor⊕call merge used in scoring (NOT stored) | computed in `core/criteria_derive` |
| `wf_` | Workflow / decision / team / award (org's process on the call) | `rfp_submissions` |
| *(none)* | Criterion **outputs** (the 9 labels + `alignment_score`/`auto_recommendation`) AND system/audit/identity columns | `rfp_submissions`, all tables |

> **Naming finalized 2026-06-29 (owner):** full words `donor_` / `call_` (not `don_`/`cal_`) for unambiguous self-documentation; `org_` kept; `fund_` = the derived donor⊕call merge; `wf_` = workflow. **Criterion OUTPUTS keep their bare names** (`qualification … bid_effort`, `alignment_score`, `auto_recommendation`) — they're well-known and not comparison inputs (Open-Q1). The §2 tables still written with `don_`/`cal_` read as `donor_`/`call_`; each axis's table is finalized to its exact names as that axis migrates (per §6).

**Rules**
1. **No double-prefix.** `org_stage` stays `org_stage` (not `org_org_stage`); `donor`→`donor_name` (not `donor_donor`).
2. **Comparable concepts share a stem** so org/donor/call line up: geography → `*_geographic_scope`; program areas → `*_priority_areas`; etc.
3. **Bands name their bounds**: a band field documents its constituent indicators, e.g. `org_target_band = (org_min_target, org_mid_target, org_max_target)`.
4. **System columns are exempt** (kept verbatim): `id, uid, created_at, updated_at, last_checked, last_update, canonical_key, row_type, content_hash, scraped_at, submitted_at, source_uid, duplicate_of_uid, is_duplicate, extraction_uid, opportunity_id, agency_code, funding_opportunity_number`.
5. **`fund_` and `score_` are derived** — `fund_*` is the in-code merge of `don_*` ∪ `cal_*`; `score_*` is the criterion-derivation output. Neither adds a stored column unless noted.

---

## 2. The comparison axes (the part that must be airtight)

Each axis lists the org field(s), the donor field(s), the call field(s), and the derived
merge — all sharing a stem so the mapping reads cleanly. **Def = definition · Src = source ·
Use = which criterion consumes it.**

### 2.1 Geography  → MUST-4, and the registration proxy in MUST-1   ✅ MIGRATED (migration 054)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_registered_countries` | org.`countries_registered` | Jurisdictions the org is legally registered in · org input · M1 registration, M4 own-presence |
| `org_operating_countries` | org.`countries_of_operation` | Where the org operates directly · org input · M4 via-presence |
| `donor_geographic_scope` | donor.`funding_scope_geographic` | Geographies the donor funds (UN regions/tiers/countries) · donor profile/LLM · M4 scope |
| `call_geographic_scope` | call.`geographic_scope` | Geography this call targets · call LLM/regex · M4 scope, M1 reg-proxy |
| `fund_geographic_scope` | *(derived)* | `donor_geographic_scope ∪ call_geographic_scope`, deduped · code · M4 denominator |

### 2.2 Program areas / themes  → MUST-2 (and PREFER-8 track record)   ✅ MIGRATED (migration 055)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_priority_areas` | org.`priority_areas` | Org's strategic priority sub-areas (taxonomy keys) · org input · M2 numerator |
| `org_priority_ratings` | org.`program_area_ratings` | `{sub-area: 0–5}` priority strength · org input · M2 band |
| `org_domain_expertise` / `org_domain_ratings` | org.`domains` / `domain_ratings` | Demonstrated **experience** areas + 0–5 strength · org input · PREFER-8 track record (NOT M2) |
| `donor_priority_areas` | donor.`priority_program_areas` | Donor's funded program areas · donor profile · M2 denominator |
| `donor_priority_ratings` | donor.`program_area_ratings` | `{sub-area: 0–5}` centrality to the donor · donor profile (default 5) · M2 band |
| `call_domain_areas` | call.`program_area` | Program/domain areas this call funds · call LLM · M2 (vs `org_priority_areas`, default band 5) + P8 track record (vs `org_domain_expertise`) |
| `fund_priority_areas` | *(derived)* | `donor_priority_areas ∪ call_domain_areas` (graded) · code · M2 denominator |

### 2.3 Award / funding size + ceilings  → MUST-3, PREFER-6   ✅ MIGRATED (migration 056)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_target_band` = `org_min_target` / `org_mid_target` / `org_max_target` | org.`funding_target_low/mid/max` | Org's preferred award-size band (USD) · org input · P6 band, M3 realistic-ask cap |
| `org_largest_grant` | org.`largest_grant_usd` | Biggest single grant ever managed · org input · M3 absorption anchor |
| `org_annual_budget` | org.`annual_budget_usd` | Annual funds managed (throughput) · org input · M3 budget-ceiling check |
| `org_lowest_grant` / `org_grants_count` | org.`lowest_grant_usd` / `number_of_grants_managed` | Range + track depth · org input · M3 stretch factor |
| `call_award_value` | call.`estimated_value` (+`currency`) | Award size stated by the call · call · P6, M3 ask |
| `call_award_ceiling` / `call_award_floor` | call.`award_ceiling` / `award_floor` | Per-award max/min · call · M3 ask |
| `donor_max_annual_budget` | donor.`max_annual_budget_usd` | Eligibility CEILING on org annual budget · donor · M3 budget ceiling |
| `donor_max_prior_grant` | donor.`max_prior_grant_usd` | Eligibility CEILING on largest prior grant · donor · M3 grant ceiling |
| `donor_min_track_record` | donor.`min_track_record_usd` | Floor on largest grant managed · donor · M3 (track) |
| `donor_award_low` / `donor_award_high` | donor.`award_low_usd` / `award_high_usd` | Donor's typical award range · donor · P6 fallback |

### 2.4 Legal status & eligibility  → MUST-1   ✅ MIGRATED (migration 057)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_legal_type` | org.`legal_type` | nonprofit/govt/academic/for-profit/individual · org · M1 applicant type |
| `org_entity_type` | org.`entity_type` | grassroot_local / multi_country / individual · org · M1 entity, P8 MCO |
| `org_hq_country` | settings.`org_hq_country`/`org_country` | Org HQ country · org · M1 HQ |
| `org_has_established_pi` | org.`has_established_pi` | Can field a well-established PI · org · M1 individual-PI |
| `org_active_donors` / `org_funder_history` | org.`active_donors` / `funder_history` | Current / past funders · org · M1 prior-beneficiary, P7 relationship |
| `donor_entity_type_required` | donor.`entity_type_required` | Required entity type · donor · M1 entity |
| `donor_hq_country_required` | donor.`hq_country_required` | Required HQ country · donor · M1 HQ |
| `donor_registration_region` | donor.`registration_region` | Required registration zone (else geo-proxy) · donor · M1 registration |
| `donor_requires_pi` / `donor_pi_country_scope` | donor.`requires_pi` / `pi_country_scope` | Individual-PI gate + base country · donor · M1 PI |
| `donor_prior_beneficiary_rule` | donor.`prior_beneficiary_rule` | eligible / ineligible_current/previous/any · donor · M1 prior-beneficiary |
| `donor_ngo_eligible` / `donor_for_profit_eligible` | donor.`ngo_eligible`/`for_profit_eligible` | Applicant-type eligibility · donor · M1 applicant type, route |

### 2.5 Cofinancing & compliance  → MUST-5   ✅ MIGRATED (migration 058)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_cofinancing_capacity` | org.`cofinancing_capacity` | none/limited/moderate/strong · org · M5 cofinance |
| `org_has_audited_financials` / `org_has_audit_report` | org.`has_audited_financials`/`has_audit_report` | Holds audited financials / audit report · org · M5 |
| `org_has_sam_uei` / `org_tax_exempt` | org.`org_has_sam_uei` / `org_tax_exempt` | SAM/UEI · tax-exempt · org · M5 |
| `org_has_safeguarding_policy` | org.`has_safeguarding_policy` | Safeguarding/PSEA policy · org · M5 |
| `org_authorized_signatory_donors` | org.`authorized_signatory_donors` | Donors an authorized signatory is already secured from · org · M5 |
| `org_has_partner_mou` / `org_has_govt_mou` / `org_has_govt_endorsement` | org.`has_partner_mou`/`has_govt_mou`/`has_govt_endorsement` | MOUs / endorsement on hand · org · M5 |
| `org_funding_routes` | org.`org_funding_routes` | Routes org can receive through (grant/procurement/loan/subrecipient/govt-ccm/direct) · org · M5 route |
| `org_has_local_board` | settings.`org_has_local_board` | Has a local board · org · M5 local board |
| `donor_*_required` family | donor.`cost_sharing_match_required`, `audited_financials_required`, `audit_report_required`, `sam_uei_registration_required`, `tax_exempt_status_required`, `safeguarding_policy_required`, `authorized_signatory_signoff_required`, `welcome_registration_required`, `partner_mou_required`, `govt_mou_required`, `govt_endorsement_letter_required`, `local_board_required`, `partnership_mandatory`, `funding_platform_registration_required` | Each = a compliance gate the donor imposes · donor · M5 (prefix all with `don_`, keep stem so org counterpart is obvious) |
| `donor_*_route` family | donor.`grant_route`, `procurement_tender_route`, `loan_dev_finance_route`, `subrecipient_partner_possible`, `direct_local_org_eligible`, `govt_or_ccm_route_required` | Routes the donor offers · donor · M5 route match vs `org_funding_routes` |
| `donor_submission_portal_url` | donor.`submission_portal_url` | Portal to register on · donor · M5 platform-reg vs `org_donor_registrations` |
| `call_compliance_flags` | call.`compliance_flags` | Call-stated requirements (LLM) merged into the donor gates · call · M5/M1 |

### 2.6 Relationship · competitiveness · bid-effort  → PREFER-7/8/9   ✅ MIGRATED (migration 059)
| New name | Old name | Def · Src · Use |
|---|---|---|
| `org_donor_registrations` | org.`donor_registrations` | Portals the org is registered on · org · M5 platform, P8 portal familiarity |
| `org_founding_year` | org.`founding_year` | Year founded → age · org · P8 incumbency (org-only attribute, no call counterpart — by design) |
| `org_has_bd_team` | settings.`org_has_bd_team` | Has a business-development team · org · P9 feasibility |
| `org_is_multi_country` / `org_is_grassroot` | derived from `org_entity_type` | P8 multi-country / grassroots match |
| `donor_funders_collaborators` | donor.`funders_collaborators` | Funder's partners/collaborators · donor · P7 shared-collaborator |
| `donor_multi_country_encouraged` | donor.`multi_country_encouraged` | Call encourages multi-country proposals · donor/call · P8 vs `org_is_multi_country` |
| `call_submission_deadline` | call.`submission_deadline` | Deadline · call · P9 time-to-deadline |

---

## 3. Derived & output fields (not source columns)
- `fund_*` — in-code merges (`_merge_rfp_compliance`, `_geo_scope`, `_strategic_items`). Documented above per axis.
- **Criterion outputs keep their bare names** (Open-Q1, resolved): the 9 labels `qualification … bid_effort`, plus `alignment_score` and `auto_recommendation`, stay as-is in `rfp_submissions`. They're outputs (not comparison inputs) and are referenced everywhere by these well-known names.

---

## 4. Non-comparison columns (still role-prefixed, listed for completeness)
- **donor_intel** — narrative/profile (`don_mission`, `don_vision`, `don_strategy_url`, `don_summary_description`, …); the `*_fit` program-area flags → `don_fit_*`; contacts (`don_hq_address`, `don_general_email`, `don_contact_persons`, …); the long `*_required` per-proposal doc family (M&E, ToC, logframe, CVs, budget…) → `don_doc_*` (NOT scored — documented as "easy per-proposal docs").
- **rfp_submissions** — `wf_*` workflow (`wf_decision`, `wf_decision_date`, `wf_stage`, `wf_progress_status`, `wf_proposal_lead`, `wf_assigned_to`, award/post-award `wf_amount_secured`…); `cal_*` call attributes (`cal_brief_description`, `cal_funding_agency`, `cal_solicitation_type`, `cal_instrument_type`, `cal_funding_window`, `cal_date_posted`, …).
- **extracted_solicitations** — `cal_*` raw-store mirror (`cal_opportunity_name`, `cal_funder_name`, `cal_deadline`, `cal_grant_amount`, …) + provenance (`field_provenance`, `extraction_confidence` kept).

*(v1 lists these at family level; full per-field one-liners will be filled in before each axis migrates — so the dictionary is complete table-by-table as we execute, not as one 350-row dump that's stale on day one.)*

---

## 5. Open questions for you
1. **`score_*` outputs** — rename the 9 criterion columns + `alignment_score`/`auto_recommendation` to `score_*`, or keep the familiar bare names? (They're outputs, not comparison inputs.) -> keep as-is
2. **`donor_*_required` stem alignment** — keep each gate's existing stem (e.g. `don_safeguarding_policy_required`) so it visibly pairs with `org_has_safeguarding_policy`? (Recommended.) -> keep but change the don_ to donor_ as indicated in the prompt
3. **Naming nits**: `org_geographic_scope` doesn't fit (org has *registered* + *operating*, two distinct geos) — I used `org_registered_countries` / `org_operating_countries`. OK? -> this is okay, that's distinctive, if we later ever create donor intel side of registered_countries, we will have donor_registered_countries so we don't conflate the two. 

---

## 6. Execution plan (after sign-off)
Per-axis, each a single reviewable PR-style step: **(1) Geography ✅ done (migration 054) → (2) Program areas ✅ done (migration 055) → (3) Award/funding ✅ done (migration 056) → (4) Eligibility ✅ done (migration 057) → (5) Compliance ✅ done (migration 058) → (6) Relationship/competitiveness/bid-effort ✅ done (migration 059) → (7) Workflow/outputs → (8) Non-comparison families.** Each step = `ALTER TABLE RENAME COLUMN` migration (idempotent) + JSON-key migration for org_profile + code update (criteria_derive, matching, features, scan_pipeline, llm_synthesis, views, scripts) + verify, with this doc's per-field one-liners filled in for that axis. ML feature names (`core/features`, `decision_model`) updated in lockstep.
