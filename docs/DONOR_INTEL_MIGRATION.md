# Donor Intelligence — schema + data migration runbook

RFPIS ships the donor-intelligence layer as an **open-source programmable
interface**: the code (schema, importer, classifier hook, Donors page) is in the
repo; the **intel data is yours**. This guide takes you from an empty database to
a populated, classifier-aware donor matrix.

The only workbook published here is
[`donor_intel_template.xlsx`](donor_intel_template.xlsx) — three public-knowledge
example donors (Gates, AfDB, the Global Fund) plus the full field spec. Your real
research never enters the repo (`*.xlsx` is git-ignored; the template is the lone
exception).

---

## What the data drives

The matched donor record becomes authoritative evidence in the scan classifier:

| Signal | Source field(s) |
| --- | --- |
| **MUST 4 — Compliant** | `local_board_required` (+ your org profile), `prefinance_required` |
| **PREFER 8 — Partnership** | `partnership_mandatory` |
| **PREFER 6 — Funding quality (fallback)** | `award_high_usd` / `award_low_usd` — used only when the RFP itself publishes no amount |

`BLANK` always means *not documented* (unknown) — it is never coerced to "no", so
a sparse record degrades gracefully to the keyword scorer instead of penalising.

---

## Step 1 — Create the schema

Run these in the Supabase SQL editor, in order (all are idempotent):

1. `db/migrations/020_donor_intel.sql` — creates `donor_intel` + `donor_source_seeds`.
2. `db/migrations/021_donor_intel_contact_fields.sql` — adds the institutional-contact
   and provenance columns the template uses (`hq_*`, `contact_*`, `award_size_basis`,
   `online_source_check_status`, `last_checked`, `row_type`).
3. `db/migrations/022_donor_contacts.sql` — creates `donor_contacts`, the one-to-many
   list of focal persons / additional contacts per donor (managed from the Donors
   page, not the workbook).
4. `db/migrations/025_donor_intel_profile_fields.sql` — adds the qualitative profile
   (About / footprint / intelligence / `past_projects_json`).
5. `db/migrations/026_donor_intel_founded.sql` — adds `founded` (year established).
6. `db/migrations/029_donor_intel_strategic_fields.sql` — adds the **strategic
   intelligence** fields surfaced by the tabbed edit form and the print-ready
   profile (see below). Apply this to populate the "Donor Intelligence Report"
   layout (NIHR / DIV examples in `docs/`).
7. `db/migrations/030_donor_program_area_ratings.sql` — adds `program_area_ratings`
   (JSON `{canonical child key: 0–5}`) for the graded **Strategic priority areas**.
8. `db/migrations/031_donor_funders_collaborators.sql` — adds `funders_collaborators`
   (JSON array of partner names) — the funders/philanthropies/partners behind or
   alongside a donor (e.g. the DIV Fund is backed by Coefficient Giving, GiveWell,
   Livelihood Impact Fund, CRI Foundation, Global Development Incubator, Anonymous
   Donors). Blank for most donors. Drawn from the shared partner vocabulary
   (`core/partners.py → ALL_PARTNERS`) merged with the donor catalog, the SAME list
   the org profile's "Trusted partners" pickers use (accepts typed additions for
   private firms / academic institutions). `past_projects_json` also gains an
   optional per-project `link` (JSON-only, no column change).

### Strategic priority areas — shared taxonomy + 0–5 grading (migration 030)

Program areas are captured against ONE shared hierarchical taxonomy
(`core/program_area_classifier.py` — Category → child sub-areas, e.g.
`Infectious Diseases → IDs - Malaria & NTDs`), now spanning **health AND social /
development** sectors (Education, Economic Development, Agriculture, WASH, Climate
& Environment, Governance, Gender & Inclusion, Humanitarian). The **identical**
picker + 0–5 grader is used by the donor profile (Donors → Scope & fit → Strategic
priority areas) and the org fit profile (Admin → Organisation → Strategic priority
areas, stored in `org_profile.program_area_ratings`). Only child sub-areas are
graded. `core.matching.strategic_fit_score()` correlates the two 0–5 vectors
(cosine) into the donor-thematic-fit score; it falls back to set overlap when
either side is ungraded. The legacy `*_fit` flag columns are retained for
back-compat but no longer edited in the UI.

### Strategic-intelligence fields (migration 029)

The donor profile is organised into the same sections as a the organisation BD "Donor
Intelligence Report" (the edit form is tabbed; the View detail + PDF print in that
order). Migration 029 adds:

| Field | Section | What it captures |
| --- | --- | --- |
| `parent_organization` | Identity | The funder behind the fund (e.g. "UK DHSC via ODA", "USAID"). |
| `strategic_priorities` | About & strategy | Current priorities / rotating themes / period (e.g. "2026 theme: AMR; 2026–2030"). |
| `in_scope` / `out_of_scope` | Scope & fit | What the donor **does** vs **does not** fund. |
| `selection_criteria` | Eligibility & process | Evaluation criteria, weights, and what wins. |
| `eligibility_notes` | Eligibility & process | Who can be lead / co-applicant, partnership & registration rules. |
| `application_deadlines` | Eligibility & process | Key dates / submission deadline. |
| `submission_portal_url` | Eligibility & process | Link to the application portal. |
| `funding_programs` | Funding | Named schemes / windows (distinct from `funding_mechanism` *types*). |
| `funding_tiers_json` | Funding | JSON array of bands/stages: `[{name, amount, duration, notes}]` (NIHR Band 1/2/3, DIV Stage 1/2/3). |
| `strategic_fit_notes` | Strategic guidance | Our alignment + comparative advantages. |
| `gaps_risks` | Strategic guidance | Gaps to address + application risks. |
| `recommended_approach` | Strategic guidance | Recommended tier/band, positioning, next steps. |

`past_projects_json` (migration 025) also gained two optional per-project keys —
`stage` and `description` — handled in the JSON payload, so no column change.

> If you add your own columns to the workbook later, add a matching
> `alter table donor_intel add column if not exists … text;` migration first —
> the importer upserts every header column, so an unknown column errors.

## Step 2 — Prepare your data

1. Copy `docs/donor_intel_template.xlsx` → `docs/donor_intel_matrix_app_ready.xlsx`
   (the path the importer reads). Keep it out of git — it holds your research.
2. Replace the three example rows with your own donors, one row per donor.

Conventions (also on the workbook's `readme` sheet):

- **`canonical_key`** — normalised lower-case key, **unique and stable** per donor.
  It is how an RFP's funder string is matched and the upsert conflict key. Never
  change it once imported.
- **Yes/no flags** — `yes` to assert; leave **blank** for unknown; `no` only when
  documented. Blank ≠ no.
- **`{country}` placeholder** — any cell may contain the literal token `{country}`.
  The app substitutes your **Admin → Organisation → Country** value at display time.
  Use it for focus-country-specific prose; keep geographic **scope** fields as
  regions (`LMIC`, `Sub-Saharan Africa`, `Global / multi-country`) so the data stays
  country-agnostic.
- **Award figures** — `award_low_usd` / `award_high_usd` / `total_annual_funding_global`
  accept formatted strings: `$0.150M`, `$50.00M`, `$8.6B`, `150k`. The classifier
  parses K/M/B suffixes.
- **Named-individual fields** — `contact_persons` / `contact_emails` / `contact_phones`
  / `contact_linkedin_urls` are blank by design. Don't mass-compile personal contacts;
  fill only from an official, current published source at outreach time.

## Step 3 — Import

```bash
python scripts/import_donor_intel.py
```

Upserts `donors → donor_intel` (on `canonical_key`) and `source_seeds →
donor_source_seeds` (on `donor,url`). **Re-runnable** — re-importing refreshes rows
in place, so iterate freely.

## Step 4 — Configure the org profile

In **Admin → Organisation**, set:

- **Country** — substituted wherever `{country}` appears.
- **`org_has_local_board`** — set `no` to make a donor that *requires* a local board
  a hard MUST-4 disqualifier. Blank = unknown (gate off).
- **`org_is_us_entity`** — `false` (default) rejects US-domestic-only RFPs at scan
  time; `true` for a US-based deployment.

## Step 5 — Verify

Open the **Donors** page (Donor Intelligence Mapping). You should see your records,
the category chart, and per-donor detail with `{country}` rendered as your org's
country. Run a scan and confirm donor-driven MUST 4 / PREFER 8 / PREFER 6 scoring.

---

## Contacts (focal persons & additional)

`donor_intel` carries **one** set of official/institutional contact fields per donor
(`general_email`, `main_phone`, `hq_*`, `donor_linkedin_url`, …) — populated from the
workbook. The **many** focal-person / additional contacts (official channels *or*
people the team has engaged) live in the separate `donor_contacts` table and are
managed entirely from the **Donors page edit dialog** (admin / super-user): a dynamic
table where you add as many rows as you like (Name, Role, Email, Phone, LinkedIn,
Address, Official?, Notes). They appear in the detail view just above the award range.

Because `donor_contacts` is keyed to `canonical_key` and the matrix re-import only
*upserts* (never deletes) donor rows, **UI-added contacts survive a matrix re-import** —
edit the workbook freely without losing your contact list. This is a private,
authorised-users-only list; source personal contacts from official published pages or
first-party relationships, never guesses.

## Updating later

Edit either the workbook (then re-run the importer) or a record directly on the
Donors page (admin / super-user only). Both write to the same `donor_intel` table;
contacts write to `donor_contacts`. Every UI save is committed straight to Supabase.
