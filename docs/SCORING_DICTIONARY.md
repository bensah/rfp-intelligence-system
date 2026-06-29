# RFPIS Scoring & Eligibility — Concept Dictionary

> Single reference for how RFPIS evaluates an opportunity: every criterion, its
> response values + scores, the logic/factors/assumptions behind each, the
> composite Bid Strength, the decision rule, and the AI-written review fields.
> Source of truth for the user guide + FAQ. (Last updated 2026-06-29.)

> **2026-06-29 model change — ACTIVE-ONLY scoring.** Every criterion decomposes into
> **components** scored **1 / 0.5 / 0**. A component is scored ONLY when the call/donor
> imposes it OR a proxy applies (e.g. MUST-1 registration via the call's geo-scope, MUST-3
> award-absorption from org financials). An **undetected** component is **"Not sure"** —
> excluded from that criterion's denominator, NOT a permissive pass. A criterion with NO
> active component resolves to **"Not sure" → value 1 (Park)**: an unknown routes to manual
> review — it never auto-passes and never auto-Declines. A clearly **detected** failure
> still scores 0 and gates. The **Auto-decision gate is fatal-factor-based**: a Decline
> fires only when the org *explicitly fails an active 🔒 non-dynamic factor* — a MUST-1
> identity gate, no MUST-4 geographic reach, or an inaccessible MUST-5 funding route. Other
> hard gaps (SAM, MOUs, ceilings) lower the score and **Park** for review. MUST 1 = legal
> identity; the stage/budget/prior-grant ceilings live in **MUST 3**; MUST 5 is the
> compliance hub. PREFER 7 = **Donor relationship**.

Each opportunity is judged on **org × donor × RFP** — the deploying org's profile
(Settings → Org profile), the funder's intelligence record (`donor_intel`), and the
specific call (the scraped/extracted RFP).

---

## 1. The 9 eligibility criteria

5 **MUST** (hard) + 4 **PREFER** (soft). Components score **1 / 0.5 / 0**; the criterion
label maps to **2 / 1 / 0**. **"Not sure" → value 1 (Park)** — an undetermined criterion
routes to review, not a zero. Internal keys in parentheses.

| # | Criterion | Responses (score) | How it's derived (org × donor × RFP) — ACTIVE components only |
|---|---|---|---|
| MUST 1 | **Legal status & qualification** (`qualification`) | Yes, fully (2) · Mostly, one item unclear (1) · No, not eligible (0) · Not sure (→1) | **Legal IDENTITY, up to 6 items (🔒 fatal):** legal type admitted · entity type · HQ country · **registration** (explicit rule, or the call's geo-scope as a proxy) · individual-PI · **prior-beneficiary** (active only when the donor states a rule → org must be in `funder_history`/`active_donors`). Active items only; any active item explicitly failed → **auto-Decline**. No active item → Not sure. |
| MUST 2 | **Strategic fit** (`strategic_fit`) | Strongly aligns (2) · Limited priority (1) · Off-strategy (0) · Not sure (→1) | ONE component — best-matched theme's priority band (min of org-band, call-band) across the org's graded `program_area_ratings` ∩ the donor/RFP priorities. No call/org theme data → Not sure. |
| MUST 3 | **Implementation capacity** (`capacity`) | Yes, comfortably (2) · Yes, but a stretch (1) · No, beyond us (0) · Not sure (→1) | Up to 5 components: **org stage · annual-budget ceiling · prior-grant ceiling** (these 3 moved here from MUST 1) · **experience** (call-stated) · **award-absorption** = realistic ask `min(award, funding_target_max)` vs `largest_grant_usd` **stretched** by experience/stage/#grants. Each active only when stated/determinable. |
| MUST 4 | **Geographic fit** (`geographic_fit`) | Yes, our own presence (2) · Yes, via a partner (1) · No presence there (0) · Not sure (→1) | ONE tiered component. Scope = call ∪ donor (with **US-only / grants.gov** inference → United States). `countries_registered` ∩ scope → own presence; operating country / qualifying partner → via partner; inclusive tiers (LMIC/global) credited. 🔒 **No reach → fatal (auto-Decline)**. No scope at all → Not sure. |
| MUST 5 | **Cofinancing & compliance** (`cofinancing`) | Yes, none required (2) · Partial, with effort (1) · No, required (0) · Not sure (→1) | **Compliance hub** (see §2). ONE soft component — co-financing/pre-finance capacity (0/0.5/1); the rest are 🔒 hard 0/1 gates active only when the donor/call imposes them. **Only the funding route auto-Declines**; other unmet gates (acquirable before the deadline) lower the score / Park. Nothing imposed → Not sure. |
| PREFER 6 | **Funding quality** (`funding_quality`) | High (2) · Moderate (1) · Low (0) · Not sure (→1) | RFP award size vs the org's preferred band (`funding_target_low/mid/max`): in/above the sweet spot → High; toward the floor → Moderate; below the floor → Low. No award value → Not sure. |
| PREFER 7 | **Donor relationship** (`funder_relationship`) | Current/past grantee (2) · Some contact (1) · None (0) · Not sure (→1) | Past grantee of this donor (`funder_history`) → grantee. **Or a shared collaborator** — an org we partner with is also among the donor's partners/collaborators — **or** we're registered on their portal → "Some contact" (a warm route in). |
| PREFER 8 | **Competitiveness** (`competitiveness`) | Strong (2) · Moderate (1) · Weak (0) · Not sure (→1) | Org edge: domain rating, co-financing strength, **multi-country** (`multi_country_encouraged` ↔ org Entity type = Multi-country Organization), HQ-country match, incumbency. |
| PREFER 9 | **Bid effort** (`bid_effort`) | Ample time, sufficient resources (2) … No realistic shot (0) | Days to deadline × whether the org has a BD team (`org_has_bd_team`). Auto-derived; no "Not sure". |

**Assumption:** BLANK org/donor data ≠ "No". A criterion is judged on its **active**
components only (those the call/donor imposes or a proxy determines); undetected
components are **"Not sure" — excluded** from the denominator, and a fully-unknown
criterion scores **value 1 (Park)** so an unknown is reviewed, not penalised.

---

## 2. MUST 5 composite (cofinancing + compliance hard-gates)

`derive_cofinancing` emits a component per requirement, **active only when the donor/call
imposes it**. Gate over active components: any 0 → **No, required**; any 0.5 → **Partial,
with effort**; all 1 → **Yes, none required**. **No active component → "Not sure" (Park).**
SAM.gov no longer shows for a private funder — it's active only on a US-federal/grants.gov
call (or `sam_uei_registration_required`).

| Component (active when…) | Satisfied by (org) | Type |
|---|---|---|
| Co-financing / pre-finance — `cost_sharing_match_required`, `min_cofinancing_secured_pct`, `prefinance_required=reimbursement_only`, or RFP cost-share | `cofinancing_capacity` (strong/moderate→1 · limited→0.5 · none→0) | soft |
| Audited financials / Audit report — `audited_financials_required` / `audit_report_required` | `has_audited_financials` / `has_audit_report` | hard |
| SAM.gov/UEI — `sam_uei_registration_required` **or US-federal call** | `org_has_sam_uei` (or a SAM registration) | hard |
| Tax-exempt — `tax_exempt_status_required` | `org_tax_exempt` | hard |
| Safeguarding policy — `safeguarding_policy_required` | `has_safeguarding_policy` | hard |
| Authorized signatory — `authorized_signatory_signoff_required` / `welcome_registration_required` | this call's donor ∈ `authorized_signatory_donors` | hard |
| Partner MOU / Govt MOU / Govt endorsement / Local board | `has_partner_mou` / `has_govt_mou` / `has_govt_endorsement` / settings `org_has_local_board` | hard |
| Mandatory partnership — `partnership_mandatory` | an Implementing/Collaborator partner | hard |
| Funding-platform registration — `funding_platform_registration_required` | `submission_portal_url` ∩ `donor_registrations` | hard |
| **Funding route** — donor offers grant/procurement/loan/subrecipient/govt-CCM/direct | `org_funding_routes` overlaps ≥1 offered route | 🔒 fatal |

*(Local-partner is NOT repeated here — it's covered by MUST-1 Entity type = grassroot/local. Stage/budget/track ceilings moved to MUST 3 / MUST 1.)*

---

## 3. Bid Strength (the composite) — and the two scales

**Bid Strength = the 9 weighted criteria (100%).** = `alignment_score` — the weighted
average of the 9 criterion scores (0–100), with **all 9 in the denominator** and **"Not
sure" = value 1 (0.5 of the scale, the Park midpoint)** so an unknown reads as
review-worthy, not a fail. Weights: MUST .65 (M1 .15 · M2 .15 · M3 .15 · M4 .10 · M5 .10) +
PREFER .35 (P6 .08 · P7 .08 · P8 .10 · P9 .09) = 1.0. Each criterion already blends the
call's and the donor's requirements (see the source tags in `scoring_decision_map.html`).

> **No "funder-fit" add-on (changed 2026-06-29).** The earlier 80 / 20 split bolted on a
> 20% "funder-fit" score of 4 signals (themes / geography / route / relationship) that
> **duplicated** MUST-2 / MUST-4 / MUST-5-route / PREFER-7 — double-counting them. It was
> removed; those funder signals now count once, inside the criteria where they belong.
> `matching.donor_org_extras` is retained for diagnostics only and no longer affects the score.

Displayed to 1 decimal; the headline Bid Strength integer uses half-up rounding (92.5 → 93).

> **PREFER 8 / 9 are capability/feasibility proxies, not 1:1 matches** (acceptable by
> design). **Competitiveness** scores the org's *edge* given the call (domain track-record
> in the call's area, age/incumbency, multi-country ↔ `multi_country_encouraged`, HQ
> match, portal familiarity) — some inputs (e.g. age) are org-only attributes with no
> call counterpart. **Bid effort** = feasibility = *time to the deadline* (call) × *whether
> the org has a BD team* (org) — two independent inputs ANDed, not compared.

### Two DISTINCT scales (do not conflate)
- **Decision** (the suggestion): a **🔒 fatal-factor gate first** — if the org explicitly
  fails an **active** non-dynamic factor (a MUST-1 identity gate, MUST-4 no geographic
  reach, or an inaccessible **MUST-5 funding route**) → **Decline**, regardless of score.
  Otherwise band the composite: **Proceed ≥ 90 · Park 70–89 · Decline < 70**. Other unmet
  hard gates (SAM, MOUs, ceilings — acquirable before the deadline) and **"Not sure"**
  criteria are *not* fatal — they lower the score and typically **Park**; the high MUST
  weight (.65) still sinks genuinely weak bids below 70. *(Supersedes both "any MUST < 2 →
  Decline" and the old stage/budget/track fatal floors.)*
- **Fitness label** (Strong / Moderate / Weak): **70+ / 45–69 / < 45** — describes overall
  match strength only; it does **not** set the decision.

---

## 4. Decision vs Auto-decision

- **Auto-decision** (`auto_recommendation`) = the system's suggestion from §3.
- **Decision** (`decision`) = the **human's** call; stays **"Pending"** until a reviewer
  sets it. Never auto-filled from the model (that would pollute the learning signal).
  The human decision is the supreme label the scorer learns from (👍/😐/👎 feedback +
  recorded decisions).

---

## 5. AI-written review fields (LLM synthesis, gate-passed rows only)

Written by `core.llm_synthesis` during screening for rows that pass the gate
(Decline/Park/Proceed) — never for rejected candidates:

| Field | What it is |
|---|---|
| `brief_description` | 5–8-sentence plain-prose summary (≤ 1000 chars): what's funded, eligibility, amounts, duration, deadline. |
| `program_area` (Focus Areas) | LLM classification from the canonical taxonomy (bare sub-labels, no category prefix). |
| `key_risks` | One grounded sentence — the single most material risk of this org pursuing this call. |
| `decision_note` (rationale draft) | 1–2 sentences explaining the Auto-decision (human edit wins). |
| `how_to_apply` | High-level numbered steps to apply, with portal/links. Shown on Tracking with an **Apply** button (`apply_url`). |

---

## 6. Key assumptions / conventions
- Currency display: `US $2,000,000`; non-USD shows the original + inline `≈US $…`.
- `annual_budget_usd` = funds the org **manages per year** (throughput), not one grant;
  `largest_grant_usd` = biggest **single** grant ever — distinct concepts.
- Off-theme is a hard extraction gate; geography is **not** (it's a tenant screening
  tier), so region/LMIC-framed calls are kept and judged at MUST 4.
- "$" in any displayed LLM/user text is escaped so it never renders as LaTeX.
