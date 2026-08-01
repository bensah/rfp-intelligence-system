-- Migration 075 — per-tenant settings store (schedule / policies / team).
-- =========================================================================
-- Several "org config" blobs live in the GLOBAL app_settings key/value table and so are
-- shared by every tenant: the check-in SCHEDULE (schedule_json), the eligibility POLICIES
-- (scan_policies) and the TEAM roster (team_members_json). That's why a fresh tenant
-- (Taadom) still saw the sample country team's schedule, and why every tenant would screen against
-- the organisation's countries/themes.
--
-- This adds a per-tenant key/value store. core.settings.get_setting/set_setting become
-- tenant-aware for those keys: a tenant reads/writes its OWN value, and a fresh tenant
-- with no override falls back to CODE defaults (permissive policies, empty schedule/team)
-- — NOT to the organisation's config. the sample country team's CURRENT values are seeded below so it keeps
-- exactly what it has today.
--
-- SAFE + ADDITIVE + IDEMPOTENT.
-- =========================================================================

begin;

create table if not exists tenant_settings (
  tenant_id  uuid not null references tenants(id) on delete cascade,
  key        text not null,
  value      text,
  updated_by text,
  updated_at timestamptz not null default now(),
  primary key (tenant_id, key)
);

-- Seed the sample country team's current global schedule / policies / team into its own
-- tenant_settings so nothing changes for the organisation. Fresh tenants get code defaults.
insert into tenant_settings (tenant_id, key, value)
select t.id, s.key, s.value
  from tenants t
  join app_settings s
    on s.key in ('schedule_json', 'scan_policies', 'team_members_json')
 where t.name = 'the sample country team'
on conflict (tenant_id, key) do nothing;

commit;

-- Rollback:
--   drop table if exists tenant_settings;
