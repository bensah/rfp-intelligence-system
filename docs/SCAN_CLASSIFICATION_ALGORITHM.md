# RFP Scan & Classification Algorithm — spec for review (v2)

> **Status: DRAFT for Bernard to review/tweak before implementation.**
> v2 incorporates Bernard's decisions: the **Rigor** strictness dial, the
> **per-donor requirements matrix**, deterministic (no-LLM) classification that
> **learns keywords from our own manual/Excel submissions**, and a
> **full-read-or-find-alternative-source** fetch policy so fields are (almost)
> never empty.

---

## 0. Goal & non-negotiables

- **No empty fields.** Every scanned RFP gets its data elements populated. A
  blank field that forces a human to scan the page is the **rare fallback**,
  not the norm we see today.
- **Deterministic, rule-based** (no paid LLM). Accuracy grows by **learning the
  keyword library from real submissions** (the Excel screener + manual form
  entries), not from a model subscription.
- **Admin-configurable** with a **Rigor** dial so strictness is tunable per
  criterion, per deployment/org.

---

## 1. Fetch policy (read it like a human; never give up at a paywall)

```
for each donor-source URL (Admin → Sources):
  1. List page → enumerate candidate opportunities (links + opportunity_id)
  2. for each NEW candidate:
     a. Fetch detail page (static HTTP first — cheapest)
     b. If the page is JS-rendered / thin → re-fetch with a headless browser
        (Playwright) so we read what a human sees. We process the rendered
        text/DOM IN PLACE — we do NOT bulk-download XML/PDFs (too heavy).
     c. If the RFP is paywalled (e.g. DevelopmentAid) → take the title +
        funder + opportunity_id and SEARCH for an open copy (the donor's own
        site, grants.gov, etc.). Most donors don't paywall RFPs, so an
        alternative public source almost always exists. Read that instead.
     d. Extract (Stage 2) + classify (Stage 3) + decide (Stage 4) + populate.
     e. Only if BOTH the source and any alternative are unreadable → leave the
        unresolved fields blank and flag the row "needs manual review".
```

**Escalation within a page:** title → synopsis/brief → full text. Stop as soon
as every criterion + data field resolves; only read the full announcement when
something is still unknown.

---

## 2. Stage 2 — structured field extraction (why fields are empty today)

Parse the donor page's **labelled fields first** (reliable, cheap). On
grants.gov these are explicit; per-donor parsers handle other layouts.

| RFP field (DB)        | Source label (grants.gov example)                     |
|-----------------------|-------------------------------------------------------|
| `opportunity_id`      | Funding Opportunity Number (HRSA-26-087)              |
| `opportunity_title`   | Funding Opportunity Title                             |
| `funding_agency`      | Agency                                                |
| `submission_deadline` | Current Closing Date for Applications                 |
| `date_posted`         | Posted Date                                           |
| `estimated_value`     | Award Ceiling (fallback: Estimated Total Program Funding) |
| `program_area`        | Category of Funding Activity + keyword classify of title/brief |
| `geographic_scope`    | Derived from Eligible Applicants (see §6)             |
| `applicant_role`      | Eligible Applicants + sub-signals                     |
| **`applicant_eligibility_text`** | **"Additional Information on Eligibility" (`synopsis.applicantEligibilityDesc`) — the DECISIVE geography signal (the "domestic" test, §6). Currently NOT extracted.** |
| `brief_description`   | Synopsis                                              |

> The current scraper skips these labelled fields → that's the #1 reason the
> app shows empty Program Area / Value / Geography. Fixing extraction alone
> recovers most fields.

---

## 3. Stage 3 — the **Rigor** model (the core mechanic)

Replaces "positive/negative keywords" with **True / Partial / False bands** per
criterion, plus a **Rigor dial (0–5)** that sets how strict the bands are.

### 3.1 Every criterion produces a numeric evidence score `s`
- **Numerical criteria** (funding amount, days-to-deadline, doc count): `s` is
  the raw number.
- **Qualitative criteria** (govt-alignment, monitorable, scale…): `s` is a
  **weighted keyword-match score** — this is how we "tie numerical values to
  the qualitative parts." More/stronger keyword hits → higher `s`.

### 3.2 Admin configures the True / Partial / False bands
e.g. Funding: `False < 50k`, `Partial 50k–100k`, `True > 100k`.
Each band is just a cut-point on `s`.

### 3.3 Rigor (0–5) shifts the bands
| Rigor | Meaning                                                                 |
|-------|-------------------------------------------------------------------------|
| **0** | **Ignore** this criterion — it doesn't affect the decision (auto-pass). |
| **1** | Very lenient — bands shifted down; weak evidence still scores True.      |
| **2** | Lenient — e.g. $50k now counts as **True** (your example).              |
| **3** | **Nominal** — bands applied exactly as configured.                       |
| **4** | Strict — bands shifted up; e.g. $50k drops to **Partial/False**.         |
| **5** | Absolute — **binary, no Partial**: must fully meet the True band or False. |

So one slider per criterion controls leniency, and `Rigor 5` removes the grey
zone entirely. Funding is the easy numeric example; the same dial works on the
qualitative scores because we scored them numerically in 3.1.

---

## 4. The 9 criteria (rules + where the score comes from)

Bands/keywords below are **defaults**; all are admin-editable (§7).

### MUST (any False → Decline · ≥2 Partial → Decline · 1 Partial → Park)

- **MUST 1 · Govt alignment** — score from gov-align keyword hits + "eligible
  applicants include governments/public institutions". Health/disease-control
  programs default high.
- **MUST 2 · Strategic fit** — **True** if `program_area` matches **any** of the
  org's strategic areas (the dropdown list) **and** the work is in an **LMIC**;
  **Partial** if it's related public health *outside* the known global-health
  areas (Water & Sanitation, Climate Change, etc.); **False** if no overlap.
- **MUST 3 · Implementable** — geography in scope (§6) + scope/timeline look
  executable (your confirmed definition).
- **MUST 4 · Compliant** — **looked up from the per-donor matrix (§5) first**;
  text keywords (prefinance/ethics/local-only) only as fallback.
- **MUST 5 · Resourcing = Timeline + Requirements** — timeline from
  days-to-deadline (`< 14` → bandwidth-limited); requirements **doc count from
  the per-donor matrix (§5)** (≥ heavy-threshold → resource-intensive).

### PREFER (need ≥3 of 4 = True, with all MUSTs True, to Proceed)

- **PREFER 6 · Funding quality** — tiers `<50k / 50–100k / >100k` → False /
  Partial / True (your confirmed defaults, all admin-set).
- **PREFER 7 · Monitorable** — keyword score (indicators, M&E, results
  framework) + achievability within scope/timeline.
- **PREFER 8 · Partnership** — **per-donor matrix first** (mandatory / optional
  / NGO-not-eligible) → False / True / False.
- **PREFER 9 · Scale** — scale-up language → True; pilot-only → False; unstated
  → Partial.

---

## 5. Per-donor requirements matrix (your "table to check each document")

A full document/requirement checklist — **one row per donor, one yes/no column
per requirement** (template: `docs/DONOR_REQUIREMENTS_MATRIX_TEMPLATE.csv`).
Bernard fills it by research; the scanner **looks up the donor** instead of
re-reading boilerplate. Unknown donors fall back to keyword scanning.

Column groups (all yes/no except where noted):
- **Eligibility / structure:** `ngo_eligible`, `for_profit_eligible`,
  `local_registration_required`, **`local_board_required`**,
  **`authorized_signatory_signoff_required`**,
  **`govt_endorsement_letter_required`**, `tax_exempt_status_required`,
  `sam_uei_registration_required`, `prior_track_record_required`,
  `partnership_mandatory`, `local_partner_required`.
- **Financial / compliance:** `prefinance_required`
  (none/partial/reimbursement_only), `cost_sharing_match_required`,
  `audited_financials_required`, `ethics_irb_approval_required`.
- **Package documents** (each `*_required`): concept_note, full_technical_proposal,
  detailed_budget, budget_narrative, logframe/results_framework, theory_of_change,
  workplan_timeline, mande_plan, cvs_key_personnel, org_capacity_statement,
  letters_of_support, partner_mou, registration_certificate, bank_details,
  due_diligence_questionnaire, safeguarding_policy, data_management_plan,
  risk_management_plan, sustainability_exit_plan, gender_inclusion_plan,
  environmental_safeguard, procurement_plan, org_chart_staffing, audit_report,
  references.
- **Process:** `two_stage_application`, `online_portal_submission`.

How it feeds the criteria:
- **MUST 5 package weight** = count of `*_required` document columns = yes
  (replaces the manual `heavy_doc_count` guess; threshold still admin-set).
- **Hard disqualifiers (→ MUST-fail → Decline):** `local_board_required` = yes
  **AND** the org has no local board (org profile, §7) → ineligible. Same
  pattern for any structural requirement the org can't satisfy. This is the
  exact past-disqualification you flagged.
- **MUST 4 Compliant:** `prefinance_required` = reimbursement_only → Partial;
  local-only financing → False; ethics/IRB-heavy → tighten.
- **PREFER 8 Partnership:** `partnership_mandatory` = yes → False; else → True.
- `authorized_signatory_signoff_required` + `govt_endorsement_letter_required`
  add to package weight + effort (govt endorsement is also a feasibility
  dependency that can delay submission).

---

## 6. Geography & eligibility HARD PRE-SCREEN (runs FIRST; short-circuits to Decline)

Before any MUST/PREFER scoring, run a cheap hard gate. Most US-federal RFPs are
out of scope for an LMIC deployment, so we reject them up front and skip the
expensive extraction/scoring — **this is exactly why the table fills with
irrelevant US-only rows today.**

**Decisive signal = the word "domestic".** Real example (HRSA-26-083): the
*Additional Information on Eligibility* field reads *"All **domestic** public or
private, non-profit or for-profit entities…"*. In US-federal language
"domestic" = US-based applicants only → an LMIC org (the deploying org) is **not
eligible as prime** → **Decline**. (The grants.gov API exposes this as
`synopsis.applicantEligibilityDesc` — we must extract it; today we don't.)

**Hard-reject (geography out of scope)** when `org_is_us_entity = false` AND the
target countries aren't covered, on ANY of:
- eligibility text contains US-domestic markers: `domestic`, `U.S.-based`,
  `United States`, `must be located in the United States`, `nationally
  available` (US-national framing); OR
- ALL `applicantTypes` are US-domestic categories (state/county/city/township
  governments, US school districts, US public-housing authorities, tribal
  governments, US 501(c)(3)s, US small businesses) with **no** foreign /
  international / "any entity (without domestic qualifier)" option; OR
- Assistance Listing / purpose is explicitly US-domestic (e.g. US rural health,
  Assistance Listing 93.912).

**What makes it eligible beyond the USA** (do NOT reject — continue to scoring),
any of:
- explicit `foreign entities` / `international organizations` / `non-U.S.` /
  `organizations located in [target country/region/LMIC/sub-Saharan Africa]`
  eligible;
- foreign subawards / foreign components explicitly allowed;
- global/LMIC program scope, or the target countries named.

**Sub-routing (not a reject):** a US-residency requirement flips
`applicant_role` to **Sub** ONLY if `org_is_us_entity = true`. For the deploying org
(false), US-domestic-only stays a hard Decline. The synopsis confirms PRIME
eligibility; the full NOFO (Funding Restrictions / Subawards / Foreign
Components) confirms partner routes — escalate to it only when the synopsis is
ambiguous on geography, not for clearly-domestic ones.

> Net effect: clearly-US-only RFPs are Declined with the reason
> *"US-domestic-only — out of scope for [org countries]"* and never clutter the
> Proceed/Park views.

---

## 7. Admin configuration (per deployment/org)

New **Admin → Settings → Scan classification**:
- Strategic program areas (seeds MUST 2) + "LMIC" geography list.
- Per-criterion **bands** (cut-points) **and a Rigor slider (0–5)**.
- Funding tiers (numeric).
- Resourcing: `min_days_to_deadline` (14), `heavy_doc_count` (4).
- Editable keyword bags per criterion.
- Country profile (`org_countries`, `org_is_us_entity`).
- **Org structural profile** — what the deployment CAN satisfy, compared against
  the donor matrix to catch disqualifiers: `org_has_local_board` (the deploying org
  = no), `org_can_provide_authorized_signoff`, `org_can_obtain_govt_endorsement`,
  `org_is_locally_registered`, `org_has_audited_financials`, etc. A donor
  requirement the org can't meet → hard Decline with the reason surfaced.

This is what lets a *different* org point the scanner at *their* priorities.

---

## 8. Building the keyword library from our own data (no LLM)

Our manual submissions + the migrated Excel screener are a labelled corpus:
each row has `brief_description` + `program_area` + the human `decision` and the
9 criteria values. So:

1. **Tokenize** `brief_description`/`title` per `program_area` → frequent,
   distinctive terms become that area's keyword bag (TF-IDF-style: terms common
   in HIV rows but rare elsewhere).
2. Tokenize per **criterion outcome** (e.g. text of rows where Partnership was
   marked mandatory) → seeds the PREFER 8 / MUST 4 bags.
3. Re-run periodically as members submit more → the library **learns** from
   real usage (the R/BioPortal annotator you shared is the same idea —
   annotate text against a controlled vocabulary — but ours is built from our
   own submissions and runs locally, no external API).

A one-off `scripts/build_keyword_library.py` does the initial extraction from
`rfp_submissions`; an admin button re-runs it later.

---

## 9. Code changes (once rules are agreed)

- `core/scraper.py` — **extract `synopsis.applicantEligibilityDesc`** (the
  "domestic" test, §6) + per-donor structured parsers + Playwright render
  fallback + paywall→search-for-alternative-source.
- `core/eligibility_gate.py` (new) — the §6 **hard geography/structural
  pre-screen** (US-domestic-only → Decline with reason), run BEFORE the
  MUST/PREFER scoring so out-of-scope US RFPs never reach the Proceed/Park views.
- `core/auto_scorer.py` — evidence-score + bands + **Rigor** engine; read donor
  matrix for MUST4/MUST5/PREFER8.
- `core/donor_matrix.py` (new) — load/lookup the donor CSV.
- `scripts/build_keyword_library.py` (new) — tokenize submissions → keyword bags.
- `core/settings.py` + Admin UI — §7 config (bands, Rigor sliders, matrix editor).

---

## 10. Confirm before I build
1. **Rigor model** (§3) — does the 0–5 behaviour match what you meant? (0=ignore,
   2=lenient/$50k→True, 4=strict, 5=binary/no-Partial.)
2. **Donor matrix columns** (§5) — add/remove any before you start filling it?
3. **Keyword-learning from `rfp_submissions`/Excel** (§8) — go ahead and seed
   from the current data?
4. Implementation order — I'd suggest: **(1) fix extraction** (kills the empty
   fields now) → **(2) Rigor engine + matrix** → **(3) keyword-learning** →
   **(4) Playwright/paywall fallback**. OK?
