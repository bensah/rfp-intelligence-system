# RFPIS — Eligibility scoring rework, criterion by criterion (START: MUST 1)

> Paste this whole file as the first message of a fresh session.

## Role & mission
You are continuing work on **RFPIS** (`D:\Apps\chai-rfp-intelligence`), a Streamlit +
Supabase platform that scans donor sources, extracts funding opportunities, and
screens them against a tenant org's eligibility. We are **rebuilding the eligibility
scoring, one criterion at a time**, starting with **MUST 1 (Legal status &
qualification)**, then MUST 2, etc.

This is the **core of the product** — wrong scoring = dissatisfied users. The earlier
`docs/scoring_decision_map.html` had wrong mappings/calculations because assumptions
were made that I never validated.

## HARD RULES (read before doing anything)
1. **No assumptions.** If anything below is not explicit, **STOP and ask me** before
   coding. Do not infer scoring math, field names, or thresholds.
2. **One criterion at a time.** Fully align → I confirm → you implement → I verify in
   the browser. Only MUST 1 is in scope until I say move on.
3. **New factor ⇒ full plumbing.** If a factor needs a field that doesn't exist:
   (a) add it to the right schema, (b) add it to the relevant **form(s)**, and
   (c) if it is a **funding-call** field, align **`scripts/migrate_excel.py`** AND the
   extraction path. Confirm the field name/placement with me first.
4. **Verify, don't fabricate.** Funding-call requirements are detected by regex/LLM —
   the LLM must be grounded (only flag what the text states; see existing
   `extract._amount_grounded` pattern + `llm_synthesis.compliance_flags`).
5. Don't auto-commit/push. UI changes need my browser confirmation before "done".
6. Each time you create a new migration script for supabase, produce a copy paste ready
   to execute directly in supabase

## Codebase grounding (current state — verify before changing, don't assume beyond this)
- **Criteria derivation:** `core/criteria_derive.py`.
  - `qualification_factors(org, rfp, donor, org_settings)` = MUST-1 sub-factors today:
    `applicant_type`, `hq_country`, `individual_award`, `local_registration`,
    `independent` — each a `_factor(key,name,source,met,*,fatal,active)` dict where
    `met ∈ {True,False,None}`, `fatal` = non-dynamic gate, `active` = imposed by this
    call/donor. ALL MUST-1 factors are currently `fatal=True`.
  - `derive_qualification(...)` rolls them to a label: any active `met is False` →
    "No, not eligible"; any active `met is None` → "Mostly, one item unclear"; else
    "Yes, fully".
  - `fatal_decline(org, rfp, donor, org_settings, rfp_compliance)` → `(decline?, trigger)`:
    Decline iff an **active fatal** factor `met is False`.
  - `factor_breakdown(...)` returns all 9 criteria's sub-factor lists (active +
    not-applicable) for the Review per-criterion cards.
- **Org schema:** `core/org_profile.py` (`DEFAULTS` dict + get/set_profile; stored as
  one JSON setting). Relevant fields: `legal_type`, `countries_registered`,
  `partners` (list of `{name, type, status, country}` — `status` is a multiselect of
  Donor / Implementing Partner / Collaborator), `funder_history`, `donor_registrations`,
  `org_stage`, `founding_year`, `org_has_sam_uei`, `org_tax_exempt`,
  `org_is_independent_entity`, `annual_budget_usd`, `largest_grant_usd`.
- **Org settings:** `core/settings.py` (separate from org_profile): `org_hq_country` /
  `org_country`, `org_is_us_entity`, `org_has_local_board`, `org_is_grassroot`,
  `org_is_multi_country`, `org_has_bd_team`.
- **Org fit-profile form:** `views/org_setup.py` (legal_type, HQ country, org_stage,
  countries_registered, the partners data_editor, and the grassroots / multi-country
  checkboxes currently under a "Competitiveness" section).
- **Donor intel:** `donor_intel` table; requirement flags already exist (migrations
  020+): `ngo_eligible`, `for_profit_eligible`, `hq_country_required`,
  `local_registration_required`, `independent_entity_required`, `org_stage_required`,
  `max_annual_budget_usd`, `min_track_record_usd`, `sam_uei_registration_required`,
  `tax_exempt_status_required`, `partnership_mandatory`, `local_partner_required`,
  `required_partner_type`, `required_partner_country`, … Donor form: `views/donors.py`.
- **Funding-call extraction:** `core/extract.py` (regex-first), `core/llm_extractor.py`,
  `core/llm_synthesis.py` (emits `compliance_flags` — currently cost-share / local-reg /
  partnership / audit / due-diligence / SAM-UEI / tax-exempt). Excel import:
  `scripts/migrate_excel.py`. RFP rows live in `rfp_submissions`.
- **Decision rule (current):** fatal-gate first (any active fatal factor failed →
  Decline) then composite bands (≥90 Proceed / 70–89 Park / <70 Decline). Concept doc:
  `docs/SCORING_DICTIONARY.md`.

---

# MUST 1 — Legal status & qualification (the spec to implement)

MUST 1 is a **COMPOSITE** of up to **6 sub-components**, NOT a single 2/1/0 dropdown.
Some sub-components have their own child checks (parent → child1 → child2 → MUST 1).

## Denominator = Funding call ⊕ Donor intel (the REQUIREMENTS)
Base = **6 points**. A component that is **absent** (not stated by the call and not a
donor-intel requirement) is **Not Applicable → dropped from the denominator** (it does
not count for or against). Each present component is a hard requirement that must be met.

These 6 must be **captured as requirements in donor intel** (donors that require them by
default have them checked) AND **detected from the funding call** by regex/LLM (grounded):

1. **Eligibility type** — named entity types eligible to apply (NGO, for-profit,
   government, enterprise, …). Usually on the call.
2. **HQ-country requirement** — the call requires the applicant be **HQ'd in the
   implementation country** (NOT the donor's HQ country). e.g. CHAI is HQ'd in the US →
   such a requirement disqualifies CHAI.
3. **Registration status** — where the applicant must be **registered** (e.g. must be in
   an LMIC / Africa / Asia / a specific country). Has **two child checks**:
   (a) **Registration country** vs the call's required region/country; and
   (b) a **grassroots/local check**: if the call/donor requires a grassroots/local org
   but the applicant is a multi-country org, that's a mismatch.
4. **Individual-PI** — child checks: (a) does the call require an **individual / a PI**
   (vs an organization)? If yes → (b) the **PI's required base country** (e.g. the CADC
   call required the lead PI + institution in **Canada**, and a second LMIC PI in the
   implementation country). Score after both checks.
5. **Org stage** — call/donor targets **early-stage / startups** (must be captured in
   donor intel too), vs "established". If no indication → assume any entity eligible.
   Calls usually only state when they want early-stage. Also a related signal: the call
   states an **annual-revenue ceiling** or a **prior-grant-size ceiling within last X
   years** above which an org is ineligible. **NOTE: org stage must NOT be a hard gate
   that flips MUST 1 to 0.**
6. **Prior beneficiary** — the call explicitly says prior grantees of this donor are
   **ineligible** (funding new orgs) OR explicitly that prior grantees **are** eligible.
   Detect during extraction (keywords) + a donor-intel flag.

## Numerator = Org (one-to-one matching). For each: absent requirement → drop; present & match → 1; present & no match → 0.
1. **Eligibility type** ↔ org **`legal_type`** ("Bid Fitness → Legal type" — CONFIRM
   exact field/location with me).
2. **HQ-country** ↔ org settings **`org_hq_country`** vs the call's required
   implementation/HQ country (component 2).
3. **Registration status** ↔ org **`countries_registered`** vs the call's required
   registration region/country **PLUS** a NEW field **"Entity Registered"** (see new
   fields) — score = Registration-country × Entity-Registered.
   - Note: the system only counts the org's **preference** countries (currently
     Cameroon + Mali), even though CHAI is registered in 35 countries.
4. **Individual-PI** ↔ a NEW **"Has well-established PI"** checkbox. Logic:
   - If the call requires a PI from an **eligible (in-scope) country** → the org's own
     PI qualifies it.
   - To satisfy a **foreign-PI** requirement, look at **Affiliated Partners**:
     `Type ∈ {Nonprofit/NGO, For-profit/private, Academic/research institutions}` AND
     `Status ∈ {Implementing Partner, Collaborator}` AND
     (`partner.Country ≠ org.countries_registered` **OR** `partner.Country = donor HQ country`).
   - The two conditions are deliberate: `Country ≠ countries_registered` = any country
     other than where the org operates; `Country = donor HQ country` covers a PI required
     in the **donor's** country; together they also cover a **3rd-party OECD** country
     (e.g. a Canadian funder, Cameroon implementation, but lead PI required in UK/Canada).
   - **Goal: never wrongly exclude an org that has a partner who can be the foreign PI.**
   - First check is "individual vs organization required" (the PI gate), THEN the
     PI-country check.
5. **Org stage** ↔ org **`org_stage`** (established vs early-stage dropdown already
   exists) vs the call/donor org-stage requirement. **Soft — must NOT flip MUST 1 to 0.**
6. **Prior beneficiary** ↔ if the call/donor requires it, check whether the donor is in
   the org's **"Donors we've already won grants / awards from"** (`funder_history`).

## NEW fields to add (CONFIRM names/placement before building)
**Org (`org_setup.py` + `org_profile.py`):**
- **Entity Registered** — single-select dropdown: **Grassroot/Local Organization |
  Multi-country Organization | Individual**. This **REPLACES** the two separate
  checkboxes "We are a grassroots / local NGO" and "We are a multi-country organization"
  (data-validation fix: you can't be both). **Move it** to be a column/field **after
  Legal type** (today the two checkboxes sit under "Competitiveness").
- **"Has well-established PI"** — checkbox under Org fit profile.
- Keep **"We are a US-based entity"** (`org_is_us_entity`) — distinct from HQ country:
  when checked, the org qualifies for US-based opportunities.

**Donor intel (`donor_intel` + `views/donors.py` + migration):** ensure all 6
requirement components above exist as fields so default-requiring donors can be flagged.
Some exist (HQ, registration, stage, NGO/for-profit eligibility); confirm which are
missing — likely: explicit **eligible-entity-type list**, **registration-region**,
**requires-PI + PI-country**, **early-stage/startup target**, **revenue/grant ceiling**,
**prior-beneficiary eligibility**.

**Funding call (`rfp_submissions` + extraction + `migrate_excel.py`):** add the
call-side captured fields for the 6 components; detect via regex/LLM (grounded) and add
to `compliance_flags`; map any Excel columns in `migrate_excel.py`.

## OPEN QUESTIONS — resolve with me BEFORE writing code
1. **Aggregation math:** how do the (up-to-)6 sub-scores roll into MUST 1's final value
   and the fatal verdict? Which components are **hard gates (fatal → auto-Decline)** vs
   **soft (score only)**? (You told me org-stage is soft; confirm the rest — likely
   eligibility-type / HQ / registration / individual-PI are hard, prior-beneficiary
   hard, org-stage soft — but DO NOT assume.) How does the won/N composite map to the
   "Yes, fully / Mostly / No" label and to the composite Bid-Strength contribution?
2. **"Bid Fitness → Legal type"** — exact field & screen.
3. Confirm the new field **names, dropdown options, and placement** before I add them.

## Workflow for this session
1. Confirm the open questions above.
2. Propose the MUST-1 data-model + derivation design (denominator/numerator factors,
   child checks, aggregation, fatal vs soft) and WAIT for my sign-off.
3. Implement schema + forms + extraction + `migrate_excel.py` + `criteria_derive`
   (`qualification_factors` / `derive_qualification` / `fatal_decline` / `factor_breakdown`).
4. I verify in the browser. Then we move to MUST 2.
