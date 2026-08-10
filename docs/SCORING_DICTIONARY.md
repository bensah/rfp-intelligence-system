# RFPIS Scoring & Eligibility — Concept Dictionary

> Single reference for how RFPIS evaluates an opportunity: every criterion, its
> response values + scores, **the definition of every scoring component** (§7), the
> composite Bid Strength, the decision rule, the data-confidence overlay, and the
> AI-written review fields. Source of truth for the user guide + FAQ.
> Source files: `criteria_derive.py` · `assessment.py` (single live-scoring source) ·
> `matching.py` · `auto_scorer.py` · `scorer.py` · `data_quality.py` · `features.py`.
> (Last updated 2026-07-05.)

> **2026-07-05 changes (this reference now reflects them):**
> • **MUST 5 has NO fatal gate** — funding-route and funding-platform registration moved
>   to **PREFER 8**; every remaining MUST-5 gate is acquirable, so the only auto-Decline
>   gates are **MUST 1** (identity) and **MUST 4** (no geographic reach).
> • **SAM.gov/UEI** is a US-federal-only gate; for any other donor it's a **permissive
>   pass (value 1, "no restriction")**, not "Not sure".
> • **PREFER 6** label = the **mean of its met/active components** (an unstated duration
>   is excluded → all stated components met = **High**); duration is auto-extracted from
>   prose ranges (range → max/ceiling).
> • **PREFER 7** gains a 3rd tier — **Donor engaged** (`rel_engaged`, `engaged_donors`);
>   donor matching is canonical-key based (acronym ⇄ full name).
> • **PREFER 8**: track record = `band(min(org rating, donor priority))`; **funding route
>   + portal registration** now score here; **HQ-country match is positive-only** and the
>   funder country is inferred (donor HQ → call → currency).
> • **Data-confidence overlay (E3c/E3d)** — completeness → High/Medium/Low; a
>   **Low-confidence "Proceed" auto-parks** (§4).
> • **Single source of truth** — every surface (Review, Screen, Records table, View modal)
>   scores LIVE via `assessment.assess_row`; the stored `alignment_score` columns are a
>   scan-time snapshot only.

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
> identity; the budget/prior-grant ceilings live in **MUST 3**; MUST 5 is the
> compliance hub. PREFER 7 = **Donor relationship**.

> **2026-08-06 — component symbols on the Review card.** `✓` met · `◐` **partly met**
> (measured, between pass and fail) · `✗` failed · `?` **Not sure** (not stated by this
> call → excluded from the denominator) · `○` alternative route not needed · `🔒` fatal
> gate. `◐` was introduced because `?` was doing two jobs: a component we *measured* at
> 0.5 — real data, a partial match — rendered identically to one we knew nothing about.
> The symbol now follows the **score**, not the `met` tri-state (which collapses every
> partial to `None`). Single source: `core.criteria_derive.component_mark`.

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
| MUST 3 | **Implementation capacity** (`capacity`) | Yes, comfortably (2) · Yes, but a stretch (1) · No, beyond us (0) · Not sure (→1) | **2 components (reworked 2026-08-06):** **financial capacity** = one 0–1 composite over the value checks that are determinable — award-absorption (realistic ask `min(award, funding_target_max)` vs `largest_grant_usd`, **stretched** by years/stage/#grants) + the 🔒 annual-budget and prior-grant **ceilings**; and **experience** — org maturity vs the call's years bar *or* stage restriction, **default-pass when neither is stated**. The ceilings remain auto-Decline gates, checked on the sub-parts (the composite mean can hide a failed one). See §5 for the full breakdown. |
| MUST 4 | **Geographic fit** (`geographic_fit`) | Yes, our own presence (2) · Yes, via a partner (1) · No presence there (0) · Not sure (→1) | ONE tiered component. Scope = call ∪ donor (with **US-only / grants.gov** inference → United States). `countries_registered` ∩ scope → own presence; operating country / qualifying partner → via partner; inclusive tiers (LMIC/global) credited. 🔒 **No reach → fatal (auto-Decline)**. No scope at all → Not sure. |
| MUST 5 | **Cofinancing & compliance** (`cofinancing`) | Yes, none required (2) · Partial, with effort (1) · No, required (0) · Not sure (→1) | **Compliance hub** (see §2). TWO soft components — co-financing capacity and pre-financing capacity, scored separately (0/0.5/1); the rest are hard 0/1 gates active only when the donor/call imposes them. **No fatal gate** — every gate is acquirable before the deadline, so an unmet one lowers the score / Parks (never auto-Declines). **Nothing imposed → the `compliance_all_clear` component = 1/1, a full pass shown alone** (2026-08-06); SAM/UEI is excluded outside US-federal calls. |
| PREFER 6 | **Funding quality** (`funding_quality`) | High (2) · Moderate (1) · Low (0) · Not sure (→1) | **Mean of the MET active components** — floor/ceiling/value-stated + duration (see §7). An **unstated duration is excluded** (its absence neither helps nor hurts), so all *stated* components met → **High**. Falls back to the award-size band vs `funding_target_low/mid/max` when the org set no targets. No award value → Not sure. |
| PREFER 7 | **Donor relationship** (`funder_relationship`) | Current/past grantee (2) · Some contact (1) · None (0) · Not sure (→1) | 3 OR-tiers (best wins): past grantee of this donor (`funder_history`) → grantee; **donor engaged** (`engaged_donors` — prior contact, no funding yet) **or** shared collaborator **or** registered on their portal → "Some contact". Donor matching is canonical-key (acronym ⇄ full name). |
| PREFER 8 | **Competitiveness** (`competitiveness`) | Strong (2) · Moderate (1) · Weak (0) · Not sure (→1) | Org edge: **track record** = `band(min(org domain rating, donor priority))` in the call's area · age/incumbency · portal familiarity · **funding-route access** · **multi-country** (`multi_country_encouraged` ↔ org MCO) · **HQ-country match** (positive-only; funder country inferred from donor HQ → call → award currency). |
| PREFER 9 | **Bid effort** (`bid_effort`) | Ample time, sufficient resources (2) … No realistic shot (0) | Days to deadline × whether the org has a BD team (`org_has_bd_team`). Auto-derived; no "Not sure". |

**Assumption:** BLANK org/donor data ≠ "No". A criterion is judged on its **active**
components only (those the call/donor imposes or a proxy determines); undetected
components are **"Not sure" — excluded** from the denominator, and a fully-unknown
criterion scores **value 1 (Park)** so an unknown is reviewed, not penalised.

---

## 2. MUST 5 composite (cofinancing + compliance hard-gates)

`derive_cofinancing` emits a component per requirement, **active only when the donor/call
imposes it**. Gate over active components: any 0 → **No, required**; any 0.5 → **Partial,
with effort**; all 1 → **Yes, none required**.
**MUST 5 has NO fatal gate (2026-07-05)** — every gate below is acquirable before the
deadline, so an unmet one lowers the score / Parks; it never auto-Declines. (Funding route
and funding-platform registration were moved to **PREFER 8**.)

> **2026-08-06 — the all-clear, and SAM/UEI scoping.** These are **strict eligibility
> rules that exist only when the call or donor intel states them**, so:
>
> * **SAM.gov/UEI is EXCLUDED** unless the call is US-federal *or* the donor explicitly
>   demands it. It used to be emitted as an always-active *permissive pass* — and because
>   it was the only unconditionally-active component, MUST-5's active set was never empty.
>   A call that imposed **nothing at all** therefore read **"Yes, fully met · 1/1 · 100%"**,
>   certified by a default pass on a rule the funder never made. **83% of the live
>   pipeline (211/253 rows) sat in exactly that state.**
> * **Nothing stated → one explicit component**, `compliance_all_clear` ("All compliance &
>   co-financing requirements met"), scoring **1/1**, and the Review card shows it
>   **alone** — the greyed "not stated by this call" rows are hidden, since they are noise
>   once the answer is "nothing was imposed". Still a **full pass**: a strong-fit RFP must
>   not be eliminated over data the funder never published.
> * **One or more stated → those alone form the denominator.** Hard gates, no middle
>   ground: org holds it → 1, doesn't → 0. The all-clear retires automatically, including
>   after a human override activates a requirement the derivation didn't see
>   (`_settle_all_clear`, re-run inside `factor_breakdown`).
>
> **Bid Strength is unchanged by this** — verified across all 253 live rows, 0 label
> changes. A 1.0 component can never flip the any-0 / any-0.5 gate, so removing the
> SAM/UEI pass moves no score; it only stops it padding the numerator *and* denominator.
> 42 rows now show an honest ratio (e.g. `2/3 → 1/2`).

| Component (active when…) | Satisfied by (org) | Type |
|---|---|---|
| Co-financing — `cofinancing_required` = Required (migration 092); legacy `cost_sharing_match_required` / `state_party_cofinancing_required` / `min_cofinancing_secured_pct` > 0 / an RFP cost-share clause still activate it | `cofinancing_capacity`: none→0 "Not met" · limited→0.5 "Partial, with effort" · strong→1 "Yes, fully met". **Both sides must be known** — Not required / Not sure / blank, or an unrecorded capacity, leaves it unscored | soft |
| Pre-financing — `prefinance_required` = Required. `reimbursement_only` is **not** a requirement: it says when money arrives, not who may apply | `prefinance_capacity`, the SAME three levels, never read off `cofinancing_capacity` | soft |
| Audited financials / Audit report — `audited_financials_required` / `audit_report_required` | `has_audited_financials` / `has_audit_report` | hard |
| SAM.gov/UEI — **US-federal (grants.gov) call only**, or `sam_uei_registration_required` | `org_has_sam_uei` (or a SAM registration). **For every other funder it is EXCLUDED entirely** (2026-08-06) — out of the denominator, not a free pass inside it | hard |
| Tax-exempt — `tax_exempt_status_required` | `org_tax_exempt` | hard |
| Safeguarding policy — `safeguarding_policy_required` | `has_safeguarding_policy` | hard |
| Authorized signatory — `authorized_signatory_signoff_required` / `welcome_registration_required` | this call's donor ∈ `authorized_signatory_donors` (canonical-key matched) | hard |
| Partner MOU / Govt MOU / Govt endorsement / Local board | `has_partner_mou` / `has_govt_mou` / `has_govt_endorsement` / settings `org_has_local_board` | hard |
| Mandatory partnership — `partnership_mandatory` | an Implementing/Collaborator partner | hard |

*(Local-partner is NOT repeated here — it's covered by MUST-1 Entity type = grassroot/local. Stage/budget/track ceilings moved to MUST 3 / MUST 1. Funding route + funding-platform registration moved to PREFER 8 — see §7.)*

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
  fails an **active** non-dynamic factor (a **MUST-1 identity gate** or **MUST-4 no
  geographic reach**) → **Decline**, regardless of score. **MUST 5 no longer contributes a
  fatal gate (2026-07-05).** Otherwise band the composite: **Proceed ≥ 90 · Park 70–89 ·
  Decline < 70**. Unmet hard gates (SAM, MOUs, ceilings, funding route — all acquirable)
  and **"Not sure"** criteria are *not* fatal — they lower the score and typically
  **Park**; the high MUST weight (.65) still sinks genuinely weak bids below 70. Finally
  the **confidence overlay** (§4) can downgrade a thin-data Proceed to Park.
- **Fitness label** (Strong / Moderate / Weak): **70+ / 45–69 / < 45** — describes overall
  match strength only; it does **not** set the decision.

---

## 4. Decision vs Auto-decision

- **Auto-decision** (`auto_recommendation`) = the system's suggestion from §3.
- **Decision** (`decision`) = the **human's** call; stays **"Pending"** until a reviewer
  sets it. Never auto-filled from the model (that would pollute the learning signal).
  The human decision is the supreme label the scorer learns from (👍/😐/👎 feedback +
  recorded decisions).

### Data-confidence overlay (E3c/E3d — `data_quality.py`)
Every prediction carries a **confidence** = how much DATA backs it, blended from **donor
mapping completeness** (`donor_completeness`) + **call-extraction completeness**
(`call_completeness`) → **High / Medium / Low** (`confidence_band`, call weighted higher).
Shown on Review and as a column on the Screen table. **E3d guard (`confidence_adjusted`):**
a **Low-confidence "Proceed" is downgraded to "Park"** ("thin data — verify the donor
mapping / call before Proceeding"); Medium/High and non-Proceed decisions pass through
unchanged. A shaky prediction on sparse data never auto-commits.

> **Single source of truth (2026-07-05).** The Bid Strength, Auto-decision, probability
> tier and the 9 criteria shown on the **Review screen, Screen table, Records table and
> View-RFP modal** are ALL computed LIVE from `assessment.assess_row` (which wraps the
> same `derive_criteria → composite_match → fatal_decline → band` path as scan-time
> scoring). The stored `alignment_score` / `auto_recommendation` columns are a scan-time
> **snapshot** — they can lag until a rescan, so displays never rely on them.

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
- `brief_description` is **HTML-stripped to clean text** on ingest + display (`records.strip_html`), so scraped WordPress/RSS markup never shows literal `<p>`/`<a>` tags.

---

## 7. Component dictionary (every scoring component)

Each criterion decomposes into the **components** below (internal keys as used in
`criteria_derive.py` and the ML feature vector `features._COMPONENT_KEYS`, prefixed
`cmp_`). A component is **active** only when the call/donor imposes it or a proxy applies;
inactive → "Not sure", excluded from that criterion's denominator. Scores are **1 / 0.5 / 0**
unless noted; 🔒 marks a fatal gate (failing it auto-Declines).

### MUST 1 · Legal status & qualification (`qualification`) — 🔒 identity gates
| Key | Component | Active / scored |
|---|---|---|
| `applicant_type` | Legal type admitted | Donor states eligible applicant types; org `legal_type` ∈ them |
| `entity_type` | Entity type (NGO / for-profit / academic / grassroot / MCO …) | Donor imposes an entity-type rule; org `entity_type` matches |
| `donor_hq_country` | HQ-country requirement | Donor requires a specific HQ country; org HQ matches |
| `local_registration` | 🔒 Registration region | Explicit rule, else the call's **geo-scope as proxy**; org `registered_countries` covers it (broad tiers expanded) |
| `individual_pi` | Individual / PI eligibility | Call requires an in-scope-country PI; org has an established PI or qualifying partner |
| `prior_beneficiary` | Prior-beneficiary rule | Donor states one; org ∈ `funder_history` / `active_donors` |

### MUST 2 · Strategic fit (`strategic_fit`)
| Key | Component | Active / scored |
|---|---|---|
| `strat_fitness` | Best-matched theme priority | `min(band(org priority), band(call/donor priority))` over shared program areas; no theme data → Not sure |

### MUST 3 · Implementation capacity (`capacity`) — **2 components** (reworked 2026-08-06)
| Key | Component | Active / scored |
|---|---|---|
| `financial_capacity` | **Financial capacity for this award** (composite, soft) | The **mean of whichever value checks are determinable** (below) — active unless *nothing* about the money is knowable. Stays on a 0–1 scale even when only one check applies. |
| `experience` | Experience requirement (soft) | **Always active.** No bar stated by the call *or* donor intel → **default pass = 1** ("no restriction"). A stated bar is scored; both bars stated → the **weaker** governs. |

**Sub-parts of `financial_capacity`** — not separate components; they are averaged into the one score. All three need the call's award value, so when extraction misses it they blank together rather than showing as three independent unknowns:

| Sub-part | Direction | Scored | 🔒 |
|---|---|---|---|
| `award_absorption` | *Are we big enough?* (the common case) | Realistic ask `min(award, target_max)` vs `largest_grant`/`annual_budget`, **stretched ×2–7** by years/stage/#grants: within → 1 · ≤1.5× → 0.5 · beyond → 0 | soft |
| `budget_ceiling` | *Are we too big?* (rare — a window reserved for small orgs) | Donor states a max annual budget; org `annual_budget` within it | **fatal** |
| `grant_ceiling` | *Are we too big?* | Donor states a max prior grant; org `largest_grant` within it | **fatal** |

> The two ceilings stay **auto-Decline gates** and are read from the sub-parts directly, *not* from the composite — a failed ceiling averaged with a passing absorption yields 0.5, which would otherwise read as a harmless "stretch". `derive_capacity` and `fatal_decline` both check the hard sub-parts first.

**`experience` scoring** — the bar can arrive two ways, and until 2026-08-06 only the first was scored:

| Bar | Source | Scored |
|---|---|---|
| **Years** | `experience_required` — a bare number (`"3"`, `"5+"`) is taken literally ("no less than 3 years since creation"); else `significant`→10y, `moderate`→5y | meets it → 1 · within 2y → 0.5 · else 0. Founding year unrecorded → fall back to `org_stage` (established → 1 · early-stage → 0 when the bar ≥5y · unknown → 0.5) |
| **Stage** | `org_stage_required` — `early-stage` \| `established`, extracted only when the call *restricts* | same stage → 1 · opposite stage → **0** · org stage unrecorded → 0.5 |

> The stage bar closes a real gap: a fund reserved for **young organisations** now scores an established applicant **0** instead of passing it through. Neither experience bar is fatal — an experience mismatch lowers the score and Parks, it does not auto-Decline.

| Key | Status |
|---|---|
| ~~`org_stage`~~ | **Retired 2026-07-20** — its restriction is now scored inside `experience`; org stage still feeds the award-absorption *stretch* and the PREFER-8 `comp_age` edge. |
| ~~`budget_ceiling`~~ / ~~`grant_ceiling`~~ / ~~`award_absorption`~~ | **Folded into `financial_capacity` 2026-08-06** as sub-parts. Their ML feature slots (`cmp_*`) are retained and always `None`; `cmp_financial_capacity` is appended last (positional feature contract). |

### MUST 4 · Geographic fit (`geographic_fit`) — 🔒 fatal at 0
| Key | Component | Active / scored |
|---|---|---|
| `geo_presence` | 🔒 Presence in scope | Own presence (registered ∩ scope) = 1 · via qualifying partner = 0.5 · none = 0; inclusive tiers (LMIC/global) credited. No scope → Not sure |

### MUST 5 · Cofinancing & compliance (`cofinancing`) — no fatal gate
| Key | Component | Active / scored |
|---|---|---|
| `cofinance` | Co-financing capacity (soft) | A co-financing requirement imposed; `cofinancing_capacity` strong/mod→1 · limited→0.5 · none→0 |
| `prefinance` | Pre-financing capacity (soft) | Pre-financing explicitly required **and** `org_prefinance_capacity` recorded. Never scored off `cofinancing_capacity` — different capability. A reimbursing funder is reported as a delivery risk, unscored |
| `audited_financials` / `audit_report` | Audited financials / Audit report | Donor requires; org holds it |
| `sam_uei` | SAM.gov / UEI | **US-federal call only** (or donor demands it); else **excluded** (2026-08-06 — was a permissive pass = 1); org holds SAM |
| `compliance_all_clear` | **All compliance & co-financing requirements met** | Active **only when no other component is** — the explicit "nothing was imposed, so this is a full pass" row (= 1). Shown alone; the greyed rows are hidden. Retires the moment any real requirement is detected or a reviewer overrides one in |
| `tax_exempt` | Tax-exempt status | Donor requires; `org_tax_exempt` |
| `safeguarding` | Safeguarding / PSEA policy | Donor requires; org holds it |
| `partner_mou` / `govt_mou` / `govt_endorsement` / `local_board` | MOUs / endorsement / local board | Donor requires each; org holds it |
| `authorized_signatory` | Authorized-signatory sign-off | Donor requires; this call's donor ∈ `authorized_signatory_donors` |
| `partnership` | Mandatory partnership | Donor requires; org has an Implementing/Collaborator partner |

### PREFER 6 · Funding quality (`funding_quality`) — label = mean of met components
| Key | Component | Active / scored |
|---|---|---|
| `fq_floor` | At/above your minimum target size | Org set `min_target`; award ≥ it |
| `fq_ceiling` | Within your absorptive ceiling | Org set `max_target`; award ≤ it |
| `fq_value` | Award value stated | Call states an award value |
| `fq_duration` | Project duration (longer preferred) | Duration stated (prose ranges → **max**); ≥12mo→1 · 6–12→0.5 · ≤6→0. **Absent → excluded** (not counted) |

### PREFER 7 · Donor relationship (`funder_relationship`) — 3 OR-tiers
| Key | Component | Active / scored |
|---|---|---|
| `rel_grantee` | Past / current grantee | Call's donor ∈ `funder_history` (canonical-key matched) |
| `rel_engaged` | Donor engaged (prior contact, no funding yet) | Call's donor ∈ `engaged_donors` |
| `rel_contact` | Shared collaborator or portal-registered | Shared partner with the donor, or registered on their portal |

### PREFER 8 · Competitiveness (`competitiveness`)
| Key | Component | Active / scored |
|---|---|---|
| `comp_track` | Track record in this program area | `band(min(org domain rating, donor priority))` — donor priority defaults 5; 4–5→High · 2–3→Moderate |
| `comp_age` | Established (10+ years) | Org founding year known |
| `comp_portal` | Familiar with the donor's portal | True when (a) an ACTIVE portal registration host matches the donor/call portal host — **sub-domain-aware** (`gavi.org` credits `portal.gavi.org`) — OR (b) the org has a **working relationship** with the funder (past/current grantee, active, or engaged donor), since having worked with a funder implies portal familiarity. (b) overlaps PREFER-7 by design. |
| `comp_route` | Funding route accessible | Donor offers route(s); `org_funding_routes` overlaps ≥1 (was MUST-5, no longer fatal) |
| `comp_grassroots` | Grassroots / local-org status | Donor flags grassroots/local; org is |
| `comp_multi` | Multi-country presence | `multi_country_encouraged`; org is MCO |
| `comp_hq` | HQ-country match with funder | **Positive-only** ✓ when org HQ = funder country (inferred donor HQ → call → award currency); no match → excluded, never a ✗ |

### PREFER 9 · Bid effort (`bid_effort`)
| Key | Component | Active / scored |
|---|---|---|
| `bid_time` | Time to deadline | Days until `call_submission_deadline`, **3-tier**: `>14d = 1.0` (ample) · `7–14d = 0.5` (tight) · `<7d = 0.0` (not enough) |
| `bid_team` | Business-development team | `org_has_bd_team` (1 / 0) |

**Classification = the banded AVERAGE of the two component scores** (mean of `bid_time` and `bid_team`):

| Mean | Band |
|---|---|
| `> 0.75` | **Full (2)** — needs *both* ample time and a team, e.g. (1 + 1)/2 = 1.0 |
| `0.50 – 0.75` | **Partial (1)** — one strong side, e.g. (0.5 + 1)/2 = 0.75, or (0 + 1)/2 = 0.5 |
| `< 0.50` | **None (0)** — e.g. (0.5 + 0)/2 = 0.25, or (0 + 0)/2 = 0.0 |

A business-development team can lift a *tight* or *short* deadline to Partial; it cannot alone reach Full. (Prior model was time-dominant — a `<7d` deadline forced 0 regardless of the team; changed 2026-07-20 to the averaging model above.)
