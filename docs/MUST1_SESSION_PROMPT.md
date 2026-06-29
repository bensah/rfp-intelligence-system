# RFPIS — Eligibility scoring rework, criterion by criterion (START: MUST 1)

> Paste this whole file as the FIRST message of a fresh session. It is self-contained.

---

## Role & mission
You are continuing work on **RFPIS** (`D:\Apps\chai-rfp-intelligence`), a Streamlit +
Supabase platform that scans donor sources, extracts funding opportunities, and screens
them against a tenant org's eligibility. We are **rebuilding the eligibility scoring, one
criterion at a time**, starting with **MUST 1 (Legal status & qualification)**, then
MUST 2, etc.

This is the **core of the product** — wrong scoring = dissatisfied users. The earlier
`docs/scoring_decision_map.html` produced wrong results because mappings and calculations
were assumed and never validated. We are redoing this carefully and explicitly.

---

## HARD RULES (read before doing ANYTHING)
1. **No assumptions.** If anything below is not explicit, **STOP and ask me** before
   coding. Do not infer scoring math, field names, thresholds, gates, or placements.
2. **One criterion at a time.** Align → I confirm → you implement → I verify in the
   browser → we move on. **Only MUST 1 is in scope** until I say otherwise.
3. **New factor ⇒ full plumbing.** If a factor needs a field that doesn't exist:
   (a) add it to the right **schema**, (b) add it to the relevant **form(s)**, and
   (c) if it is a **funding-call** field, align **`scripts/migrate_excel.py`** AND the
   **extraction path**. Confirm the field name/placement/options with me FIRST.
4. **Verify, don't fabricate.** Funding-call requirements are detected by regex/LLM —
   the LLM must be **grounded**: flag only what the text actually states. Follow the
   existing `extract._amount_grounded` pattern + `llm_synthesis.compliance_flags`.
5. **Don't auto-commit/push.** UI changes need my browser confirmation before "done".
6. **Every new Supabase migration ⇒ also give me a copy-paste-ready SQL block** I can run
   directly in the Supabase SQL editor.

---

## Codebase grounding (current state — VERIFY before changing; do not assume beyond this)
- **Criteria derivation:** `core/criteria_derive.py`
  - `qualification_factors(org, rfp, donor, org_settings)` = MUST-1 sub-factors today:
    `applicant_type`, `hq_country`, `individual_award`, `local_registration`,
    `independent` — each a `_factor(key,name,source,met,*,fatal,active)` dict where
    `met ∈ {True,False,None}`, `fatal` = non-dynamic gate, `active` = imposed by this
    call/donor. **ALL MUST-1 factors are currently `fatal=True`** (part of what we rework).
  - `derive_qualification(...)`, `fatal_decline(...)`, `factor_breakdown(...)` roll
    sub-factors into labels / decline triggers / the per-criterion Review cards.
- **Org schema:** `core/org_profile.py` (`DEFAULTS` dict + get/set_profile; one JSON
  setting). Relevant: `legal_type`, `countries_registered`, `countries_of_operation`
  (VERIFY exact key), `partners` (list of `{name, type, status, country}`; `status` is a
  multiselect of Donor / Implementing Partner / Collaborator), `funder_history`,
  `donor_registrations`, `org_stage`, `founding_year`, `annual_budget_usd`,
  `largest_grant_usd`, `org_has_sam_uei`, `org_tax_exempt`, `org_is_independent_entity`.
- **Org settings:** `core/settings.py`: `org_hq_country` / `org_country`,
  `org_is_us_entity`, `org_has_local_board`, `org_is_grassroot`, `org_is_multi_country`,
  `org_has_bd_team`.
- **Org fit-profile form:** `views/org_setup.py` → "Organization Details & Preferences"
  → **Bid Fitness** tab → "Organization fit profile — bid/no-bid matching": Legal type,
  Founding year, Co-financing capacity, Annual budget managed, Largest SINGLE grant ever,
  Smallest grant managed, Number of grants managed, Preferred award size (low/mid/max),
  Eligibility facts (Independent entity, Holds SAM.gov/UEI, Tax-exempt, **Org stage**),
  **Countries registered**, **Countries of operation**, Proposal languages, the partners
  data_editor ("Affiliated Partners and Collaborators"), and the grassroots / multi-country
  checkboxes (currently under "Competitiveness").
  Legal type options observed: Non-profit organization, Government, Higher Education,
  For-profit company, Individual, Tribal organization.
- **Donor intel:** `donor_intel` table; flags exist (migrations 020+): `ngo_eligible`,
  `for_profit_eligible`, `hq_country_required`, `local_registration_required`,
  `independent_entity_required`, `org_stage_required`, `max_annual_budget_usd`,
  `min_track_record_usd`, `sam_uei_registration_required`, `tax_exempt_status_required`,
  `partnership_mandatory`, `local_partner_required`, `required_partner_type`,
  `required_partner_country`, … Donor form: `views/donors.py`.
- **Funding-call extraction:** `core/extract.py` (regex-first), `core/llm_extractor.py`,
  `core/llm_synthesis.py` (`compliance_flags`). Excel import: `scripts/migrate_excel.py`.
  RFP rows live in `rfp_submissions`.

---

# MUST 1 — Legal status & qualification (the spec to implement)

MUST 1 is a **COMPOSITE of up to 9 independently scored items** (some have nested child
checks) — NOT a single 2/1/0 dropdown. "Up to 6" was never a fixed cap; what matters is
logical, item-by-item scoring. Each scored item contributes **equally** to the denominator.

## Scoring model (CONFIRMED)

**Each activated item gets a score that maps to a verdict contribution:**

| score | verdict contribution        |
|------:|-----------------------------|
| **0**   | No, not eligible          |
| **0.5** | Mostly, one item unclear  |
| **1**   | Yes, fully                |

- **HARD items** can only be **0 or 1** (all-or-nothing).
- **SOFT items** can be **0, 0.5, or 1**. "Soft" means *only* that a 0.5 middle ground
  exists — a soft item can still score **0 → No**. (This supersedes any earlier wording
  that said soft items can never force "No".)

**Denominator** = count of items **activated** (imposed) by the funding call OR donor
intel. A non-activated item is **dropped from BOTH numerator and denominator**. Denominator
may exceed 6 and ranges **0 … 9**.

**Numerator** = sum of the activated items' scores (0 / 0.5 / 1).

**Bid Strength (MUST 1 continuous value)** = **numerator ÷ denominator** (0–1). It is the
arithmetic ratio and is **NOT forced to 0** when the label is "No, not eligible" — a hard
mismatch sets the *label*, not the ratio. Bid Strength is undefined only when the
denominator is 0.

> Order of operations: (1) count activated items = denominator; (2) score only those
> activated items vs the org = numerator; (3) Bid Strength = numerator ÷ denominator;
> (4) assign the MUST 1 label by the decision order below.

## MUST 1 label — decision order (CONFIRMED — top-down, first match wins)
1. **Denominator = 0** (no item activated; usually thin call/donor data) → **"Not sure"**.
   Once ≥1 item is activated, MUST 1 can NEVER be "Not sure".
2. **Any activated item scored 0** → **0 / "No, not eligible"** (one mismatch overrides all
   positive matches — applies to hard items AND soft items that score 0).
3. **Any activated item scored 0.5** → **1 / "Mostly, one item unclear"**.
4. **All activated items scored 1** → **2 / "Yes, fully"**.

## Editability (CONFIRMED)
In the RFP Review edit view, expose each activated item's score (0 / 0.5 / 1) as editable so
the user can override the system; the user's edits re-derive the MUST 1 label and Bid
Strength.

## Funding-fit vs donor-fit weighting
Hold the prior **80/20** split until we have walked all 9 criteria and established how many
items each criterion has and which side (call vs donor) contributes each. Do NOT finalize
the composite weighting during MUST 1.

---

## The 9 scored items — requirement (denominator) ↔ org match (numerator) ↔ score

### A. Legal type  (HARD · 0/1)
- **Requirement:** named entity types eligible to apply (NGO, for-profit, government,
  enterprise, …) — usually on the call. e.g. "private for-profit only" disqualifies all
  other legal types.
- **Org match:** `legal_type` (existing dropdown) vs the call's eligible entity types.

### B. Entity type  (HARD · 0/1)  — moved out of "Registration status"
- **Requirement:** grassroots/local vs multi-country vs individual entity required.
  e.g. "grassroots/local required" disqualifies a multi-country applicant.
- **Org match:** NEW `entity_type` single-select (Grassroot/Local Organization |
  Multi-country Organization | Individual) vs the call/donor requirement.
- **Data-validation:** `legal_type` = Individual ⇒ `entity_type` forced to Individual
  (only Individual offered).
- **A and B are SEPARATE items**, each contributing 1 to the denominator (each has distinct
  merit, so they score independently).

### C. HQ-country  (HARD · 0/1)
- **Requirement:** applicant must be **HQ'd in the implementation country** (NOT the
  donor's HQ country). e.g. CHAI HQ'd in the US → disqualified.
- **Org match:** `org_hq_country` vs the call's required HQ / implementation country.
- **Distinct from `org_is_us_entity`** ("We are a US-based entity") — keep separate; when
  checked it qualifies the org for US-based opportunities. Do not conflate.

### D. Registration status  (HARD · 0/1)
- **Requirement:** where the applicant must be **registered** (LMIC / Africa / Asia / a
  specific country).
- **Org match:** match the call's required region/country against **`countries_registered`**
  first; if that does not satisfy, **fall back to `countries_of_operation`**. Either list
  satisfying → **1**; neither → **0**. (`countries_of_operation` is fallback only.)
- Match against whatever values the org has entered in those two fields (e.g. currently
  Cameroon + Mali). No separate "preference vs full registry" distinction.
- (Grassroots/local moved to item B; registration is now country/region only.)

### E. Individual-PI  (HARD · 0/1 · gate child → country child)
- **Child (a) — PI gate:** does the call require an **individual / a named PI** (vs an
  organization)? If NO → not activated, drop. If YES → continue to (b).
- **Child (b) — PI base country:**
  - PI required in an **eligible / in-scope country** (e.g. implementation country) → the
    org's **own** PI satisfies it via NEW **`has_established_pi`** ("Has well-established
    PI") checkbox.
  - PI required to be **FOREIGN** (donor country or a 3rd-party OECD country) → satisfy via
    **Affiliated Partners**:
    `Type ∈ {Nonprofit/NGO, For-profit/private, Academic/research institutions}`
    **AND** `Status ∈ {Implementing Partner, Collaborator}`
    **AND** ( `partner.Country ≠ org.countries_registered`
            **OR** `partner.Country = donor HQ country` ).
  - Both Country conditions are required: `≠ countries_registered` = any country other than
    where the org operates; `= donor HQ country` covers a PI required in the donor's
    country. Together they also cover a **3rd-party OECD** requirement (Canadian funder,
    Cameroon implementation, lead PI required in UK/Canada — the CADC pattern).
  - **GOAL: never wrongly exclude an org that has a partner who can serve as the foreign
    PI.**

### F. Org stage  (SOFT · 0/0.5/1)
- **Requirement:** call/donor targets **early-stage / startup** vs **established**
  (capturable in donor intel via the NEW stage multiselect). If no indication → not
  activated → dropped (any entity eligible).
- **Scoring (CONFIRMED, all four cases):**
  - call early-stage & org early-stage → **1** ("Yes, fully")
  - call established & org established → **1** ("Yes, fully")
  - call early-stage & org established → **0.5** ("Mostly" — an "established" org of only
    3–5 yrs may arguably still be a startup; nuanced by user input)
  - call established & org early-stage → **0** ("No, not eligible")

### G. Annual-budget ceiling  (HARD · 0/1)  — separate item, do not mix with H
- **Requirement:** the call/donor states a **maximum annual budget** above which an org is
  ineligible.
- **Org match:** ceiling vs org **`annual_budget_usd`** ("Annual budget managed").
  org **at/below** ceiling → **1** ("Yes, fully"); **above** → **0** ("No, not eligible").
- Donor-intel **`max_annual_budget_usd`** already covers this.

### H. Prior-grant / award ceiling  (HARD · 0/1)  — separate item, do not mix with G
- **Requirement:** the call/donor states a **maximum prior grant/award size** above which
  an org is ineligible. **No date window** — the size alone is the signal.
- **Org match:** ceiling vs org **`largest_grant_usd`** ("Largest SINGLE grant ever",
  undated — date does not matter). org **at/below** ceiling → **1**; **above** → **0**.
- A **max prior-grant ceiling** donor-intel field likely does NOT exist yet — add it (no
  "within last X years" window). `min_track_record_usd` is a MINIMUM, not this.

### I. Prior beneficiary  (SOFT · 0/0.5/1)  — handle BOTH polarities
- **Requirement:** the call/donor states a rule about prior/current grantees of **this
  donor**, in one of two **polarities**, and for current vs previous grants:
  - **Negative polarity:** prior/current grantees are **INELIGIBLE**.
  - **Positive polarity:** prior grantees **ARE eligible**.
  - **Normalize at extraction** to a single direction (a polarity flag) so the scoring
    function reads one consistent signal — the matching must not be confused by
    positive vs negative wording.
- **Org match (two sources):**
  - **If negative (prior/current grantees ineligible):**
    - org is a **current grantee** — donor present in NEW **"Active Donors"** → **0**
      ("No, not eligible").
    - org is a **previous grantee** — donor present in **`funder_history`** ("Donors we've
      already won grants / awards from") → **0.5** ("Mostly").
    - org is **neither** → **1** ("Yes, fully").
  - **If positive (prior grantees eligible):** no penalty → **1** ("Yes, fully")
    regardless of whether the org is a prior grantee (being one is not disqualifying).

---

## NEW fields to add (CONFIRMED — re-confirm exact placement at build time)

**Org (`org_setup.py` + `org_profile.py`):**
- **`entity_type`** — single-select (Grassroot/Local Organization | Multi-country
  Organization | Individual), placed **immediately after Legal type**. **REPLACES** the two
  checkboxes "We are a grassroots/local NGO" and "We are a multi-country organization" (drop
  both; `entity_type` is the single source of truth). Validation: `legal_type` = Individual
  ⇒ `entity_type` = Individual.
- **`has_established_pi`** ("Has well-established PI") — checkbox.
- **"Active Donors"** — multiselect under "Affiliated Partners and Collaborators (private,
  non-profit, donors, etc.)", options **pulled from the existing donor list** so the data
  standard stays consistent. Represents donors currently funding the org (current grantee).
- **Keep `org_is_us_entity`** ("We are a US-based entity") — distinct from HQ country.

**Donor intel (`donor_intel` + `views/donors.py` + migration):**
- **Applying-entity-stage requirement** — multiselect: **established | early-stage |
  startup**.
- **Max prior-grant / award ceiling** (USD) — NEW (no date window). Existing
  `max_annual_budget_usd` already covers the annual-budget ceiling (item G).
- **Prior-beneficiary rule** — capture polarity (grantees eligible vs ineligible) + scope
  (current vs previous grant).
- Ensure all 9 items have a requirement representation so default-requiring donors can be
  flagged. Confirm which are missing — likely: explicit **eligible-entity-type list**,
  **entity-type requirement** (grassroots/local/multi-country/individual),
  **registration-region**, **requires-PI + PI-country**, **prior-grant ceiling**,
  **prior-beneficiary polarity/scope**.

**Funding call (`rfp_submissions` + extraction + `migrate_excel.py`):** add call-side
captured fields for all 9 items; detect via grounded regex/LLM and add to
`compliance_flags`; map any Excel columns in `migrate_excel.py`. Prior beneficiary must
capture **polarity** (eligible/ineligible) AND **current-grant vs previous-grant**;
org-stage must capture early-stage/startup vs established; the two ceiling items must be
captured **separately** (annual budget vs prior grant).

---

## CONFIRMATIONS — all resolved (kept for the record)
1. **Prior grantees polarity:** scoring must read both directions — grantees eligible
   (positive → score 1) vs ineligible (negative → 0/0.5/1 by current/previous/neither).
   Normalize to one direction at extraction. **(Item I.)**
2. **Grant-ceiling window:** no dated grant history needed — match the undated
   `largest_grant_usd` against the call's ceiling. **(Item H.)**
3. **Two ceilings = two items:** annual-budget ceiling (G) and prior-grant ceiling (H) are
   scored **separately**, never combined. **(Items G & H.)**

---

## Workflow for this session
1. Confirmations above are resolved — proceed.
2. Propose the MUST-1 data-model + derivation design (the 9 items, child checks, the
   confirmed scoring/decision order, hard-vs-soft, dual Bid Strength + label outputs,
   editable overrides) and **WAIT for my sign-off**.
3. Implement: schema + form(s) + extraction + `migrate_excel.py` + `criteria_derive`
   (`qualification_factors` / `derive_qualification` / `fatal_decline` /
   `factor_breakdown`). For any Supabase migration, include a copy-paste-ready SQL block.
4. I verify in the browser. Then — and only then — we move to MUST 2.
