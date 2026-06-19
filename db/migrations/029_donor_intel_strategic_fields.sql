-- Migration 029 - donor_intel: add the strategic-intelligence fields that the
-- "Donor Intelligence Report" deck format captures but the matrix did not. These
-- power the redesigned (tabbed) Donors edit form, the organised detail view, and
-- the portrait PDF. Derived from docs/Example Donor Intelligence Profile.pdf
-- (NIHR GHR) and docs/DIV Funds Donnor_Intelligence_and_Strategic_guidance.pptx.
--
-- All TEXT (BLANK = 'not documented' / unknown -> never coerced to false). These
-- are narrative / structured-JSON intelligence fields; they do NOT feed the
-- classifier (core/donor_intel.py keys off the existing yes/no flags), so adding
-- them changes no scoring. Idempotent: ADD COLUMN IF NOT EXISTS.

-- Identity — the funder behind the fund (e.g. "UK DHSC via ODA", "USAID").
alter table donor_intel add column if not exists parent_organization     text;

-- About & strategy — current strategic priorities / rotating themes / framework
-- and the period they cover (e.g. "2026 theme: AMR; 2026-2030 '4 Is' framework").
alter table donor_intel add column if not exists strategic_priorities     text;

-- Scope — what the donor DOES fund vs what it explicitly does NOT fund. The
-- single most decision-relevant section of the deck (in-scope / out-of-scope).
alter table donor_intel add column if not exists in_scope                 text;
alter table donor_intel add column if not exists out_of_scope             text;

-- Selection — evaluation criteria, relative weights, and "what wins" signals.
alter table donor_intel add column if not exists selection_criteria       text;

-- Funding — named schemes / windows (free text, e.g. "GHR Themed; Global
-- Professorships; Fellowships") distinct from funding_mechanism (TYPES) — and
-- the structured funding tiers/bands/stages as a JSON array:
--   [{name, amount, duration, notes}, ...]  (Band 1/2/3, DIV Stage 1/2/3, ...)
alter table donor_intel add column if not exists funding_programs         text;
alter table donor_intel add column if not exists funding_tiers_json       text;

-- Application logistics — eligibility-to-lead narrative, key dates / deadlines,
-- and the submission portal link.
alter table donor_intel add column if not exists eligibility_notes        text;
alter table donor_intel add column if not exists application_deadlines    text;
alter table donor_intel add column if not exists submission_portal_url    text;

-- Strategic guidance — the org's own assessment of this donor: fit / comparative
-- advantages, gaps & risks to address, and the recommended approach / next steps.
alter table donor_intel add column if not exists strategic_fit_notes      text;
alter table donor_intel add column if not exists gaps_risks               text;
alter table donor_intel add column if not exists recommended_approach     text;

-- Note: past_projects_json (migration 025) gains two optional per-project keys
-- (stage, description) handled entirely in the JSON payload — no column change.
