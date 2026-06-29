# RFPIS Scoring & Eligibility — Concept Dictionary

> Single reference for how RFPIS evaluates an opportunity: every criterion, its
> response values + scores, the logic/factors/assumptions behind each, the
> composite Bid Strength, the decision rule, and the AI-written review fields.
> Source of truth for the user guide + FAQ. (Last updated 2026-06-26.)

> **2026-06-26 model change.** Every criterion decomposes into 1/0 **factors**, each
> tagged 🔒 **non-dynamic** (a structural eligibility gate the org can't fix before the
> deadline) or ⚙ **dynamic** (fixable — e.g. sign an MOU, register on SAM). The
> **Auto-decision gate is now fatal-factor-based, not "any MUST < 2"**: a Decline fires
> only when the org *explicitly fails a 🔒 factor*; ⚙ gaps lower the score and usually
> **Park** for review instead of hard-declining. MUST 1 was renamed **Legal status &
> qualification** and is now pure legal identity; all compliance paperwork moved to
> **MUST 5** (the compliance hub). PREFER 7 renamed **Donor relationship**. The Review
> screen shows the per-criterion pass/fail factors + the fatal trigger.

Each opportunity is judged on **org × donor × RFP** — the deploying org's profile
(Settings → Org profile), the funder's intelligence record (`donor_intel`), and the
specific call (the scraped/extracted RFP).

---

## 1. The 9 eligibility criteria

5 **MUST** (hard) + 4 **PREFER** (soft). Each response maps to a score **2 / 1 / 0**;
**"Not sure" = 0** (a real factor, never excluded). Internal keys in parentheses.

| # | Criterion | Responses (score) | How it's derived (org × donor × RFP) |
|---|---|---|---|
| MUST 1 | **Legal status & qualification** (`qualification`) | Yes, fully (2) · Mostly, one item unclear (1) · No, not eligible (0) · Not sure (0) | **Pure legal IDENTITY — all 🔒 non-dynamic & fatal:** applicant/legal type admitted, HQ country, individual-vs-org award, local registration (or local partner). Any active factor explicitly failed → Not eligible → **auto-Decline**; an unverifiable one → Mostly unclear. (Compliance paperwork moved to MUST 5.) |
| MUST 2 | **Strategic fit** (`strategic_fit`) | Strongly aligns (2) · Limited priority (1) · Off-strategy (0) · Not sure (0) | Cosine overlap of the org's graded priority areas (`program_area_ratings`) with the donor's graded priorities; the RFP's program area must intersect. |
| MUST 3 | **Implementation capacity** (`capacity`) | Yes, comfortably (2) · Yes, but a stretch (1) · No, beyond us (0) · Not sure (0) | **Multi-factorial.** Realistic ask = `min(award ceiling or value, org funding_target_max)` (a big pool is pursued as a slice). Compared to `largest_grant_usd`, **stretched** by experience (founding year → years active, org stage, `number_of_grants_managed`). ≤ largest → comfortably; ≤ anchor×stretch → stretch; else beyond us. |
| MUST 4 | **Geographic fit** (`geographic_fit`) | Yes, our own presence (2) · Yes, via a partner (1) · No presence there (0) · Not sure (0) | Org operating countries vs the RFP geography. Region/tier expansion (SSA → Cameroon) **and inclusive tiers** (LMIC / global / developing — these *contain* the org's countries) → own presence. Else partner if trusted partners exist. 🔒 **No presence AND no partner → fatal (auto-Decline)** — can't deliver there. |
| MUST 5 | **Cofinancing & compliance** (`cofinancing`) | Yes, none required (2) · Partial, with effort (1) · No, required (0) · Not sure (0) | **The compliance hub** (see §2). ⚙ **dynamic** (fixable): cost-share, audit, SAM/UEI, tax, mandatory partner/MOU, local board, pre-registration → set the 2/1/0. 🔒 **fatal floors**: org-stage / budget-ceiling / track-record the donor hard-codes + funding-route accessibility → any failed = **auto-Decline**. Default favourable when nothing required. |
| PREFER 6 | **Funding quality** (`funding_quality`) | High (2) · Moderate (1) · Low (0) · Not sure (0) | RFP award size vs the org's preferred band (`funding_target_low/mid/max`): in/above the sweet spot → High; toward the floor → Moderate; below the floor → Low. |
| PREFER 7 | **Donor relationship** (`funder_relationship`) | Current/past grantee (2) · Some contact (1) · None (0) · Not sure (0) | Past grantee of this donor (`funder_history`) → grantee. **Or a shared collaborator** — an org we partner with is also among the donor's partners/collaborators — **or** we're registered on their portal → "Some contact" (a warm route in). |
| PREFER 8 | **Competitiveness** (`competitiveness`) | Strong (2) · Moderate (1) · Weak (0) · Not sure (0) | Org edge for this call: domain rating, co-financing strength, multi-country presence, HQ-country match, incumbency signals. |
| PREFER 9 | **Bid effort** (`bid_effort`) | Ample time, sufficient resources (2) … No realistic shot (0) | Days to deadline × whether the org has a BD team (`org_has_bd_team`). Auto-derived; no "Not sure". |

**Assumption:** BLANK org/donor data ≠ "No". Derivations leave a value **only when
determinable**; where appropriate they **default favourably** (esp. MUST 5). Anything
genuinely undetermined surfaces as "Not sure" (= 0) so the score is honest.

---

## 2. MUST 5 composite (cofinancing + compliance hard-gates)

`derive_cofinancing` collects one factor per requirement the **donor** imposes and
checks whether the **org** meets it. **No requirements → "Yes, none required."**
Otherwise: all met → Yes (2); all-but-one → Partial (1); else No (0).

| Requirement (donor_intel) | Satisfied by (org) |
|---|---|
| `cost_sharing_match_required`, `prefinance_required`, `min_cofinancing_secured_pct`, or RFP cost-share | `cofinancing_capacity` ∈ strong/moderate |
| `local_registration_required`, `registration_certificate_required`, `welcome_registration_required` | `countries_registered` ∩ RFP geo, or a local partner |
| `partnership_mandatory`, `local_partner_required`, `partner_mou_required` | has partners / trusted partners |
| `sam_uei_registration_required` | `org_has_sam_uei` |
| `tax_exempt_status_required` | `org_tax_exempt` |
| `audit_report_required`, `audited_financials_required`, `due_diligence_questionnaire_required` | org established / has a budget |
| **Recipient eligibility / route** — `ngo_eligible`, `direct_local_org_eligible`, `subrecipient_partner_possible`, `grant_route` vs `loan_dev_finance_route`/`procurement_tender_route` | org legal type (NGO) can be a **direct recipient**; sovereign-only loan or NGO-ineligible (no sub-route) → unmet |

---

## 3. Bid Strength (the composite) — and the two scales

**Bid Strength = 80% × your-criteria + 20% × funder-fit.**
- **Your fit on the 9 criteria (80%)** = `alignment_score` — weighted average of the 9
  criterion scores (0–100), with **all 9 in the denominator** and "Not sure" = 0.
- **How well this funder fits you (20%)** = average of 4 funder-fit signals × 100.

Displayed to 1 decimal (e.g. `92.5 = (80% × 100.0) + (20% × 62.5)`); the headline
Bid Strength integer uses standard half-up rounding (92.5 → 93).

**Funder-fit signals** (each Yes ✓ = 1.0 / No ✗ = 0.0; 0.5 when no donor profile):
| Signal | Meaning |
|---|---|
| Funds your themes | org priority areas ∩ donor priorities (program-area taxonomy) |
| Funds your geographies | org countries vs **donor profile + RFP** geography (region/tier + inclusive tiers) |
| Uses a funding route you can access | **can we be a recipient** — org type × donor route (grant vs loan/procurement) × eligibility (ngo/direct/sub). Yes = directly fundable; partial = via partner; No = inaccessible channel |
| Existing relationship with funder | past grantee, shared collaborator, or portal contact |

### Two DISTINCT scales (do not conflate)
- **Decision** (the suggestion): a **🔒 fatal-factor gate first** — if the org explicitly
  fails any non-dynamic factor (MUST-1 legal identity, MUST-4 no geographic reach, or a
  MUST-5 fatal floor: stage / budget / track-record / funding route) → **Decline**,
  regardless of score. Otherwise band the composite: **Proceed ≥ 90 · Park 70–89 ·
  Decline < 70**. ⚙ Dynamic gaps (no SAM/MOU yet) are *not* fatal — they lower the score
  and typically **Park** for review (the org can fix them before the deadline); the high
  MUST weight (.65) still sinks genuinely weak bids below 70. *(Supersedes the old blanket
  "any MUST < 2 → Decline".)*
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
