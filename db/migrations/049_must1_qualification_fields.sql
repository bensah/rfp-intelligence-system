-- Migration 049 — donor_intel: NEW MUST-1 (Legal status & qualification) requirement
-- fields for the 9-item rework (spec: docs/MUST1_SESSION_PROMPT.md). Each activates a
-- MUST-1 item ONLY when documented; blank = not imposed → the item drops from the
-- denominator. All TEXT (matches the existing migration-032 eligibility convention).
-- Idempotent — safe to re-run.

-- A. (optional) explicit eligible legal/entity types — comma/semicolon list, e.g.
--    "nonprofit, for_profit". Falls back to ngo_eligible / for_profit_eligible.
alter table donor_intel add column if not exists eligible_entity_types text;

-- B. Entity-type requirement: grassroot_local | multi_country | individual.
alter table donor_intel add column if not exists entity_type_required text;

-- D. Registration region/country the applicant must be REGISTERED in (e.g. "LMIC",
--    "Africa", "Cameroon"). Blank → falls back to the call's geographic_scope.
alter table donor_intel add column if not exists registration_region text;

-- E. Individual-PI gate + the PI's required base country.
alter table donor_intel add column if not exists requires_pi text;        -- yes / no
alter table donor_intel add column if not exists pi_country_scope text;    -- in_scope | foreign

-- H. Prior-grant / award CEILING — applicant ineligible if its largest prior grant
--    EXCEEDS this. Distinct from min_track_record_usd (a FLOOR). Plain number string.
alter table donor_intel add column if not exists max_prior_grant_usd text;

-- I. Prior-beneficiary rule (polarity + scope):
--    eligible | ineligible_current | ineligible_previous | ineligible_any
alter table donor_intel add column if not exists prior_beneficiary_rule text;
