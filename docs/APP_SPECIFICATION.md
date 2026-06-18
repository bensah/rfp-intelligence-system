# RFPIS — RFP Intelligence System
## Application Specification

> A multi-tenant product that discovers funding opportunities (RFPs/RFAs/CFPs), screens
> them against a deploying organization's eligibility & fit, recommends Proceed / Park /
> Decline, and learns from human decisions over time. the organisation's Business Development Team is
> the reference deployment; nothing is hard-coded to the organisation.

- **Stack:** Streamlit (Python) UI · Supabase/PostgreSQL data · deployed on Streamlit
  Community Cloud (auto-deploys on push to `main`); the deep-read scan runs on GitHub Actions.
- **Repo:** `bensah/rfp-intelligence-system` · **Live:** chai-rfpis.streamlit.app
- **Entry:** `Home.py` shim → `App.py` (`st.navigation` / `st.Page` multipage; views compiled by `core/render_view.py`).

---

## 1. Core concepts & RFP lifecycle

**Three RFP `source`s** (the `rfp_submissions.source` column):

| Source | How it enters | Decision origin |
|---|---|---|
| `migration` | Excel workbook import (team's familiar tool; slow-change baseline) | **Human-coded** Proceed/Park/Decline already in the sheet |
| `auto` | Rule-based scan (later + LLM in the the second tenant phase) | **System-generated** after the hard gate; overridable in team meetings |
| `manual` | In-app submission form (built; not yet a team workflow) | Human-entered |

**Lifecycle:** discover → **hard gate (Reject)** → score the **9 criteria** → **decision** (Proceed/Park/Decline) → team review/override → outcome tracking. The hard gate is a deterministic *filter* (you apply a rule, you don't predict it). The decision is the *prediction/judgement* on opportunities that survived the gate.

**Reject ≠ Decline.**
- **Reject** = hard-gate failure (RFP-intrinsic / org-policy): not-an-RFP, dead link, expired deadline, US-domestic-only, applicant-type mismatch, off-theme, off-geo, training/loan/consultancy/reimbursement type. Logged to `scan_decisions` as `system_reject`. Never a model class.
- **Decline** = a gate-survivor that, on the deeper org-fit assessment, is out of scope (org-attributable). A real decision class.

---

## 2. Roles & access

`users.role ∈ {admin, reviewer, collaborator}` (+ `super_user` superset in app logic).
- **admin / super_user** — full edit/delete, Admin panel (Setup, Records, Sources, Manual Scan, Blacklist, Learning data).
- **reviewer** — edit RFPs, set decisions, rate.
- **collaborator** — read; **can rate RFPs (👍/😐/👎) on the Review screen** (ungated) so team-meeting attendees feed the learning engine.

---

## 3. Use cases

| # | Use case | Actor | Summary |
|---|---|---|---|
| UC1 | **Auto-scan discovery** | system (cron/manual) | Crawl donor sources + search; hard-gate; objectively derive the 9 criteria; compute score + recommendation; insert non-duplicates. |
| UC2 | **Excel migration** | admin | Import historical RFPs with human decisions as the learning baseline. |
| UC3 | **Manual submission** | reviewer | In-app form to capture an RFP (source=`manual`). |
| UC4 | **Weekly team review** | reviewers (meeting) | Review dashboard: per-criterion dropdowns, composite match gauge, set decision, rate 👍/😐/👎. |
| UC5 | **Donor intelligence** | admin | Maintain `donor_intel` profiles (identity, scope, program-area fit, eligibility/route flags, requirements). |
| UC6 | **Org profile / Bid fitness** | admin | Define who the org is (legal type, founding, budgets, program areas, partners, registrations, languages, competitiveness inputs). |
| UC7 | **Scan preferences** | admin | Configure eligibility gates + auto-scoring policy (countries, themes, criteria rigor, exclusions, currency, raw JSON). |
| UC8 | **Org×Donor×RFP matching** | system + reviewer | Composite score (criteria + donor-org fit) → Proceed/Park/Decline, shown on Review. |
| UC9 | **Learning pipeline** | system | Capture rejects/decisions/feedback → (robust crawl) → train an assistive model. |
| UC10 | **Records management** | admin/reviewer | Filter/paginate, edit (~60 fields), delete, share (CSV/email/markdown), blacklist a source. |
| UC11 | **Data hygiene** | admin | Cleaning jobs (dedupe flags, reset logs, wipe migration rows, delete auto-scans, clear scan history, fresh-test reset, clear cache). |

---

## 4. Eligibility & scoring model

### 4.1 Hard gate (Reject) — `core/auto_scorer.is_eligible`, in order
blacklist → search/listing URL → listing/generic title → aggregator-link (DevelopmentAid; grants.gov **detail** pages are NOT aggregators) → past-tense grant → not-an-RFP (error/dead/soft-404 page) → individual-award → non-funding (job posting) → error page → opportunity-type opt-outs (training / loan / consultancy / **reimbursement**) → language → feasibility hard-reject → closed-call phrase → **deadline in future** (stale-posting rule) → **US-domestic-only** → **applicant-type mismatch** → country → theme. Any failure ⇒ Reject (not inserted), logged to `scan_decisions`.

### 4.2 The 9 criteria — definitions, response values, objective derivation
Stored per RFP (`rfp_submissions`). For the **auto** source they are **objectively derived** from org × RFP (× donor) facts (`core/criteria_derive.py`); humans override on Review. Each response normalizes to an ordinal **2 / 1 / 0 / null** via `core/scorer.criterion_score` (null = "Not sure" → treated as **missing**, excluded from the score — never a fabricated 0).

| Key (col) | Question | Responses → score | Auto-derivation |
|---|---|---|---|
| **qualification** (MUST 1) | Do we formally qualify? (org type, registration, mandatory donor registration e.g. SAM/PADOR, consortium-lead) | Yes, fully→2 · Mostly, one item unclear→1 · No, not eligible→0 · Not sure→null | Passed the hard gate ⇒ "Yes, fully" |
| **strategic_fit** (MUST 2) | Fits our strategic priorities **AND** track record? | Strong–priorities+experience→2 · Priority area, limited experience→1 · Experienced but off-strategy→1 · Neither→0 · Not sure→null | priorities(`org.priority_areas`) **and** experience(`org.domains`) vs `rfp.program_area` (taxonomy overlap) |
| **capacity** (MUST 3) | Can we deliver at the award size/scope? | Yes, comfortably→2 · Yes, but a stretch→1 · No, beyond us→0 · Not sure→null | value ≤ largest_grant→2 · ≤ annual_budget→1 · else→0 |
| **geographic_fit** (MUST 4) | Funder geography ↔ our presence/partner? | Own presence→2 · Via a partner→1 · No presence→0 · Not sure→null | geo overlap `countries_of_operation`; else has partners→1 |
| **cofinancing** (MUST 5) | Can we meet co-financing/match + compliance? | Yes/none required→2 · Partial, with effort→1 · No→0 · Not sure→null | cost-share required? × `org.cofinancing_capacity` |
| **funding_quality** (PREFER 6) | Funding terms (size/flex/duration)? | High→2 · Moderate→1 · Low→0 · Not sure→null | value tiers (≥2M High · ≥500k Moderate · else Low) |
| **funder_relationship** (PREFER 7) | Relationship with this funder? | Current/past grantee→2 · Some contact→1 · None→0 · Not sure→null | `funder_history` ∋ donor → grantee; else registered on donor/call portal → "Some contact" |
| **competitiveness** (PREFER 8) | How well-positioned to win? | Strong→2 · Moderate→1 · Weak→0 · Not sure→null | composite (see 4.3) |
| **bid_effort** (PREFER 9) | Proposal feasible in time/resources? | 6 labels (see 4.3) → 2/1/0 | days-to-deadline × BD-team (see 4.3) |

`feasibility` is a separate human field (High/Medium/Low); its negative keywords act as a scan-time hard reject.

### 4.3 Composite criteria
- **bid_effort** (`core/scorer.bid_effort_label`): time (Ample >14d / Tight 7–14d / Not enough <7d) × resources (BD/fundraising team = sufficient). Labels: Ample+team→4, Ample-no-team→3, Tight+team→3, Tight-no-team→2, NotEnough+team→1, NotEnough-no-team→0; collapse to 2/1/0 (<7d always 0). Thresholds tunable (`BID_EFFORT_AMPLE_DAYS=14`, `BID_EFFORT_TIGHT_DAYS=7`).
- **competitiveness** (`core/criteria_derive.derive_competitiveness`): org age (≥20y +1, ≥10y +0.5) + per-donor-requirement factors (grassroots/local-org, local board, co-financing, multi-country, HQ-country match) + portal-familiarity (+0.5). ≥1.0→Strong, ≤−0.5→Weak, else Moderate; no signal→null.

### 4.4 Decision & composite match
- **Rule baseline** (`auto_scorer._decision_from_criteria`): any MUST=No→Decline; ≥2 MUST=Partial→Decline; 1 Partial→Park; all MUST=Yes & ≥3 PREFER=Yes→Proceed; else Park. Guards downgrade Proceed→Park on sparse text / missing deadline / weak geo.
- **`alignment_score`** (0–100, `core/scorer`): weighted sum of the 9 (weights in `config/scoring_weights.yaml`), excluding null (missing) criteria.
- **Composite match** (`core/matching.composite_match`): **0.80 × criteria_score + 0.20 × donor-org extras**, with a **hard MUST gate** (any MUST=No → Decline), else thresholds **≥70 Proceed · 45–69 Park · <45 Decline**. Donor-org extras (each 0/0.5/1, neutral 0.5 when unknown): `donor_thematic_fit`, `donor_geographic_fit`, `donor_route_fit`. Shown on the Review gauge with a breakdown. Weights/thresholds are module constants.

---

## 5. Program-area taxonomy (`core/program_area_classifier.py`)

One hierarchical vocabulary, reused across forms + matching. **Canonical keys** are `"CATEGORY - Subarea"` (21 keys) — the single source of truth for classification (`rfp_submissions.program_area`), donor fit, and matching.

| Category (high-level, selectable) | Sub-areas |
|---|---|
| Women & Children's Health | Vaccines, SRH, Nutrition, MNCH |
| Non-Communicable Diseases | Mental Health, Diabetes, Cardiovascular Diseases, Cancer |
| Infectious Diseases | Tuberculosis, Pandemic Response, Malaria & NTDs, HIV/AIDS, Hepatitis |
| Health System Strengthening | Health Workforce, Health Financing |
| Cross-cutting | Market Shaping, Digital Health (+AI), Diagnostics, Climate & Health, Assistive Technology, Research |

- Display strips the prefix ("HSS - Health Financing" → "Health Financing"); categories are selectable as broad areas.
- `program_area_select.program_area_picker` = the reusable Category→sub-area widget (stores canonical keys or a Category name; `expand()` normalizes either for matching).
- `classify_program_areas(text)` keyword-classifies free text; `scripts/reclassify_program_areas.py` replaces a generic crawled `"Health"` with specific keys.

---

## 6. Data schemas (PostgreSQL / Supabase)

> DDL: `db/schema.sql` (base) + `db/migrations/*.sql`. RLS is permissive baseline (migration 023). All `id` are `uuid`/`bigserial`; timestamps `timestamptz default now()`.

### 6.1 `rfp_submissions` — the central record (one per opportunity)
| Column | Type | Meaning |
|---|---|---|
| `uid`, `form_id` | text unique | canonical id (e.g. `AS-260617-1731`); form_id = uid |
| `source` | text | `auto` / `manual` / `migration` |
| `submitted_by`, `submitted_by_email`, `submitted_at` | text/ts | provenance |
| `search_date` | ts | discovery date (the review reference) |
| **Opportunity** | | |
| `opportunity_id`, `opportunity_title`, `brief_description`, `opportunity_link` | text | call identity |
| `date_posted`, `submission_deadline`, `expected_award_date`, `time_to_award` | date/text | dates |
| `funding_agency` | text | donor (full name; matches `donor_intel.donor`) |
| `geographic_scope` | text[] | target geographies (geo vocabulary) |
| `program_area` | text[] | canonical taxonomy keys |
| `focus_theme` | text | high-level categories |
| `applicant_role` | text | Prime / Sub / Technical |
| `funding_window`, `submission_format` | text | instrument / format |
| `estimated_value` | numeric | award size |
| `currency` | text | award currency (→USD via FX layer) |
| `project_duration` | integer | months |
| **Eligibility scoring** | | |
| `feasibility` | text | High/Medium/Low (human) |
| `qualification`, `strategic_fit`, `capacity`, `geographic_fit`, `cofinancing` | text | the 5 MUST criteria (renamed by migration 028) |
| `funding_quality`, `funder_relationship`, `competitiveness`, `bid_effort` | text | the 4 PREFER criteria (renamed by 028) |
| `decline_flags_present` | boolean | decline-flag rule result |
| `key_risks` | text | reviewer note |
| `alignment_score` | numeric | 0–100 weighted score |
| `auto_recommendation` | text | rule-based Proceed/Park/Decline |
| **Decision & tracking** | | |
| `decision` | text | human decision (Proceed/Park/Decline) |
| `decision_date`, `decision_note` | date/text | decision metadata (note renamed from decision_rationale by 028) |
| `stage`, `progress_status`, `donor_decision`, `next_action`, `assigned_to`, `proposal_lead`, `contributors`(text[]), `reviewers`(text[]), `support_roles`, `amount_requested`, `amount_secured`, `currency_secured`, `donor_program_officer`, `kickoff_date`, `date_of_approval`, `date_completed`, `next_step`, `remarks`, `notes` | various | pipeline / outcome tracking |
| **Audit** | | |
| `review_week` | text | e.g. "Week 25 (15 Jun - 21 Jun)" |
| `is_duplicate`, `duplicate_of_uid` | bool/text | dedupe |
| `decision_overridden_by`, `decision_overridden_at` | text/ts | last human save |
| `created_at`, `updated_at` | ts | (updated_at via trigger) |

Indexes: review_week, decision, submission_deadline, funding_agency, is_duplicate.

### 6.2 `donor_intel` — donor profiles (migration 020); contacts in `donor_contacts` (022)
- **Identity:** `canonical_key` (unique), `donor` (full name), `donor_short` (code), `aliases`, `donor_category`, `website`, `funding_mechanism`, `award_low_usd`, `award_high_usd`, `total_annual_funding_global`.
- **Eligibility / route:** `ngo_eligible`, `for_profit_eligible`, `direct_local_org_eligible`, `govt_or_ccm_route_required`, `grant_route`, `procurement_tender_route`, `loan_dev_finance_route`, `subrecipient_partner_possible`, `open_call_unsolicited`, `invitation_solicited`, `two_stage_application`, `online_portal_submission`, `active_route_status`.
- **Geography:** `funding_scope_geographic`, `lmic_africa_focus`, `global_multi_country_scope`.
- **Program-area fit flags:** `priority_program_areas` + `*_fit` (infectious_diseases, hiv_aids, tb, malaria, immunization_vaccines, mnch, srhr_family_planning, nutrition, ncds, hss, digital_health_data_ai, education, economic_development, climate_environment, agriculture_food_security, governance_equity_rights).
- **Requirement flags (decisive for competitiveness/qualification):** `local_registration_required`, `local_board_required`, `authorized_signatory_signoff_required`, `govt_endorsement_letter_required`, `tax_exempt_status_required`, `sam_uei_registration_required`, `prior_track_record_required`, `partnership_mandatory`, `local_partner_required`, `prefinance_required`, `cost_sharing_match_required`, `audited_financials_required`, `ethics_irb_approval_required`.
- **Proposal-document requirements:** `concept_note_required`, `full_technical_proposal_required`, `detailed_budget_required`, `budget_narrative_required`, `logframe_results_framework_required`, `theory_of_change_required`, `workplan_timeline_required`, `mande_plan_required`, `cvs_key_personnel_required`, `org_capacity_statement_required`, `letters_of_support_required`, `partner_mou_required`, `registration_certificate_required`, `bank_details_required`, `due_diligence_questionnaire_required`, `safeguarding_policy_required`, `data_management_plan_required`, `risk_management_plan_required`, `sustainability_exit_plan_required`, `gender_inclusion_plan_required`, `environmental_safeguard_required`, `procurement_plan_required`, `org_chart_staffing_required`, `audit_report_required`, `references_required`.
- `donor_contacts(canonical_key→donor_intel, contact_name, role_title, email, phone, linkedin_url, address, is_official, notes)`.

### 6.3 `scan_decisions` — labeled-data capture (migration 027, append-only)
`id, created_at, event_type (system_reject|human_decision|feedback), label, reason, rfp_uid, opportunity_title, opportunity_link, funding_agency, source, geographic_scope, submission_deadline (date), alignment_score (numeric), features (jsonb), decided_by`. Indexes on event_type, opportunity_link, rfp_uid.
- `human_decision.label` ∈ Proceed/Park/Decline (the training target); `feedback.label` ∈ good/neutral/bad; `system_reject.label` = reason category.
- `features` jsonb = the model feature vector at decision time (`core/features.FEATURE_ORDER`).

### 6.4 `app_settings` — key/value store (migration 003)
`key (pk), value (text), updated_at, updated_by`. Holds scalars **and JSON blobs**:

| Key | Shape | Contents |
|---|---|---|
| `year` | scalar | active review year |
| `scan_policies` | JSON | eligibility + scoring policy (see 7) |
| `org_profile` | JSON | bid-fitness profile (see below) |
| `org_*` keys | scalars | branding + bid-fitness settings (see below) |
| `currencies_json` | JSON | currency overrides (code/label/symbol/aliases/usd_rate) |
| `team_members_json` | JSON | team member names |
| `org_logo_b64`, `org_logo_mime` | scalars | uploaded logo |

**Org settings keys** (`core/settings._ORG_DEFAULTS` — `set_org` silently drops anything not listed here): `org_name`, `org_short`, `org_country`, `org_team`, `org_is_us_entity`, `org_has_local_board`, `org_contact_email`, `org_logo_url`, `org_website`, `org_has_bd_team`, `org_is_grassroot`, `org_is_multi_country`, `org_hq_country`.

**`org_profile` JSON** (`core/org_profile.DEFAULT_PROFILE`): `founding_year`, `legal_type`, `donor_registrations[]`, `countries_registered[]`, `annual_budget_usd`, `largest_grant_usd`, `domains[]`, `priority_areas[]`, `countries_of_operation[]`, `trusted_partners[]`, `trusted_for_profit_partners[]`, `trusted_academic_institutions[]`, `cofinancing_capacity`(none/limited/moderate/strong), `funder_history[]`, `proposal_languages[]`.

### 6.5 Other tables (in `db/schema.sql`)
`users`, `meeting_logs`, `meeting_schedule`, `engagement_logs`, `active_grants`, `narrative_logs`, `donor_sources` (curated per-donor listing URLs for targeted scraping: donor_name/donor_code/rfp_listing_url/scrape_method/selectors), `scan_logs` (one row per source per run).

---

## 7. Configuration

- **`scan_policies`** (`core/policies.DEFAULT_POLICIES`): `countries{eligible[], broad_terms[], permissive_when_silent}`, `themes{required_any[], excluded_any[]}`, `exclusions{reject_training_only, reject_loans, reject_consultancies, reject_reimbursement}`, `eligibility{org_applicant_types[], reject_applicant_type_mismatch}`, `criteria{<key>{rigor, positive[], negative[]}}` (keyword fallback), `scoring_rules{usg_funders, funding_quality_tiers, resourcing_large_amount, criterion_defaults}`.
- **`core/partners.py`**: `PARTNERS` (100 bilateral/multilateral/INGO/philanthropy orgs as name/acronym/founded), `NONPROFIT_PARTNERS` (picker options), `DONOR_PORTALS` (clean portal hosts), `PARTNER_FOUNDED`, `clean_portal_url()`.
- **`config/scoring_weights.yaml`**: per-criterion weights + thresholds; **`config/themes.yaml`**: search keywords; **`config/dropdowns.yaml`**: UI dropdowns.

---

## 8. Scan pipeline (`core/scan_pipeline.py`)
crawl/seed (`core/scraper.py`, donor_sources + search) → resolve aggregator hits to the donor's own source (`core/source_resolver.py`, Serper) → first-pass gate (`is_eligible`) → cheap liveness/enrich (`core/live_check.py`, requests; rejects dead/soft-404, recovers deadline/desc) → robust deep-read (`core/deep_read.py`, Playwright, **GitHub Actions only**: error-check → follow PDF/companion/child link → re-extract → re-gate) → dedupe → insert/merge with `auto_score` (objective criteria + score + recommendation) → log rejects to `scan_decisions`.

---

## 9. Learning pipeline (3 phases)
- **Phase 1 — capture (live):** `core/decision_log.py` writes `scan_decisions` for every system_reject, human_decision (on save, confirmations + overrides, deduped), and 👍/😐/👎 feedback. `core/features.extract` captures the feature vector (`FEATURE_ORDER`: 9 criteria as 2/1/0 + alignment_score, geo_strength, has_deadline, days_to_deadline, decline_flags, funder_is_usg, log_value_usd, channel, text_len). Migrated decisions seeded by `scripts/harvest_human_decisions.py`.
- **Phase 2 — robust crawl (live):** `deep_read` upgraded (see §8).
- **Phase 3 — model (designed, not built):** ordinal logistic regression on `scan_decisions` (target = human decision; excludes `auto_recommendation` to avoid echoing the rule); cold-start gate (~80–100 labeled, ≥10–15/class) → shadow mode → assistive suggestion. Per-deployment model (multi-tenant). Feedback is 3-way (Proceed→Good / Park→Neutral / Decline→Bad).

---

## 10. Admin UI map
- **Setup** → App settings · **Organization Details & Preferences** sub-tabs: **Profile** (branding + eligibility gates) · **Bid Fitness** (org_profile + competitiveness) · **Team Members** · **Scan Preferences** (Countries/Themes/Criteria/Search terms/Currency/JSON-advanced).
- **Manage Users · User Access · Records** (Data: table + Edit/Delete/Share/Blacklist + Excel sync; Reset: cleaning jobs + Clear cache) · **Sources · Manual Scan · Blacklist · Learning data**.
- **Review** (`views/review_rfp.py`): per-criterion rich-response dropdowns, composite match gauge + donor-org breakdown, decision + 👍/😐/👎 feedback (ungated).

---

## 11. Scripts & migrations
- **Migration 028** (`db/migrations/028_rename_criteria_columns.sql`): renames the 9 criterion columns + decision_rationale→decision_note (idempotent, data preserved).
- **Scripts (dry-run default, `--commit` to write):** `reclassify_program_areas.py` (generic "Health" → taxonomy), `update_donor_founded.py` (fill donor founding years from PARTNERS), `harvest_human_decisions.py` (seed labeled decisions), `backfill_decision_features.py`.
- Migrations 026 (founded), 027 (scan_decisions) run manually in Supabase.

---

## 12. Operations
- **Deploy:** push to `main` → Streamlit Cloud auto-deploys. **Reboot** the Cloud app after `core/*` changes (stale `sys.modules`).
- **Secrets:** `SUPABASE_URL`, `SUPABASE_KEY` (Cloud + GitHub Actions); `SERPER_API_KEY` (real-Google source resolver); never hard-code.
- **Scan:** GitHub Actions `.github/workflows/scan.yml` (Fridays + manual) runs the Playwright deep-read; Cloud "Manual Scan" runs the requests-only path.

---

## 13. Known gaps / roadmap
1. ~~Competitiveness & cofinancing donor flags placeholder names~~ — **RESOLVED 2026-06-17**: `criteria_derive.py` + `matching.py` now use the REAL `donor_intel` columns — competitiveness factors `local_registration_required`/`local_partner_required` (grassroots), `local_board_required`, `cost_sharing_match_required`/`prefinance_required` (co-financing), `global_multi_country_scope` (multi-country), `hq_country` (HQ match); cofinancing reads `cost_sharing_match_required`/`prefinance_required` (authoritative) before parsing RFP text; route_fit uses `local_board_required`/`local_registration_required`.
2. **Phase 3 model** — build `scripts/train_decision_model.py` once labels accrue.
3. **LLM extraction (the second tenant phase)** — replace remaining keyword fallbacks + improve robust crawl.
4. **Manual submission** not yet a team workflow; **donor-name split** in submit form (source from `donor_intel`).
5. Streamlit tab rendering of the recent admin reorg verified structurally only — confirm visually.

---
*Generated 2026-06-17. Source of truth is the code; this spec summarizes it.*
