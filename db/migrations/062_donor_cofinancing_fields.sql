-- Migration 062 — New donor_intel capture fields (Workstream 1; owner 2026-06-29).
-- Three columns, all already source-prefixed donor_* per the data-model convention:
--   * donor_state_party_cofinancing_required — donor requires government / counterpart
--     ("state party") co-financing. DISTINCT from cost-share match (donor_cost_sharing_
--     match_required is unchanged) — a separate question. Drives MUST-5 cofinance.
--   * donor_indirect_cost_disallowed — donor/call does NOT allow indirect / overhead
--     costs (constraint flag, consistent with the other *_required booleans: present =
--     imposed). Captured for reviewers + How-to-apply context; not yet scored.
--   * donor_fund_use_conditions — free text: conditions on how the awarded funds may be
--     used (eligible vs ineligible cost categories, restrictions).
-- Flags are stored as text 'yes'/'no' to match every existing donor_intel requirement
-- column (see app_pages/donors.py). Idempotent add — safe to re-run.

do $$
begin
  if not exists (select 1 from information_schema.columns
                 where table_name='donor_intel' and column_name='donor_state_party_cofinancing_required') then
    alter table donor_intel add column donor_state_party_cofinancing_required text;
  end if;
  if not exists (select 1 from information_schema.columns
                 where table_name='donor_intel' and column_name='donor_indirect_cost_disallowed') then
    alter table donor_intel add column donor_indirect_cost_disallowed text;
  end if;
  if not exists (select 1 from information_schema.columns
                 where table_name='donor_intel' and column_name='donor_fund_use_conditions') then
    alter table donor_intel add column donor_fund_use_conditions text;
  end if;
end $$;
