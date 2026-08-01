-- Migration 067 — MULTI-TENANT Phase 1: schema + backfill.
-- =========================================================================
-- Introduces the tenant model (a tenant = a the organisation country / global team, e.g.
-- "the sample country team", "Sample Global Team") and scopes every PER-TENANT data
-- table to a tenant_id, backfilling all existing rows to the the sample country team tenant.
--
-- SAFE + ADDITIVE + IDEMPOTENT. This migration does NOT enable Row-Level Security
-- and does NOT change any app behaviour: tenant_id is populated but nothing reads
-- or gates on it yet. Isolation (RLS via a per-user JWT tenant claim) lands in a
-- LATER phase, so running this early cannot break the live app.
--
-- SHARED vs TENANT-SCOPED (see _SCOPED_TABLES below). Aligned with the platform
-- architecture: the crawl fills a SHARED curated store + SHARED donor/source
-- knowledge that feeds EVERY tenant's screening; each tenant keeps its OWN
-- pipeline, logs, grants and review decisions.
--   SHARED  (no tenant_id): extracted_solicitations, donor_intel, donor_sources,
--           donor_source_seeds, source_registry, rfp_seen, scan_blacklist,
--           scan_logs, users, password_reset_requests, app_settings.
--   SCOPED  (tenant_id):    rfp_submissions, meeting_logs, meeting_schedule,
--           engagement_logs, active_grants, narrative_logs, scan_decisions,
--           donor_contacts.
--   ^ donor_contacts + donor_intel + scan_decisions are the debatable calls —
--     confirm before running (see the PR description).
-- =========================================================================

begin;

-- 1. TENANTS — one row per the organisation country / global team. `name` is unique and feeds
--    the onboarding type-ahead. org_profile moves here from app_settings (per-tenant).
create table if not exists tenants (
  id           uuid primary key default gen_random_uuid(),
  name         text not null unique,
  slug         text unique,
  status       text not null default 'active'
               check (status in ('active','suspended')),
  org_profile  jsonb not null default '{}'::jsonb,   -- per-tenant org profile
  created_by   uuid references users(id),            -- the first user / founding admin
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- 2. MEMBERSHIPS — which global user belongs to which tenant, their role there, and
--    approval status. Joining an existing tenant starts 'pending' → that tenant's
--    admin approves → 'active' (see Phase 5). Users stay GLOBAL (users table); a user
--    could later belong to more than one tenant.
create table if not exists tenant_memberships (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  user_id      uuid not null references users(id) on delete cascade,
  role         text not null default 'collaborator'
               check (role in ('super_user','admin','reviewer','collaborator')),
  status       text not null default 'pending'
               check (status in ('pending','active','rejected','revoked')),
  requested_at timestamptz not null default now(),
  decided_by   uuid references users(id),
  decided_at   timestamptz,
  unique (tenant_id, user_id)
);
create index if not exists idx_tenant_memberships_user   on tenant_memberships(user_id);
create index if not exists idx_tenant_memberships_tenant on tenant_memberships(tenant_id);

-- 3. SEED the the sample country team tenant, carrying the existing single org_profile into it.
--    (app_settings is key/value with a TEXT value → cast to jsonb; missing → '{}'.)
insert into tenants (name, slug, status, org_profile)
values (
  'the sample country team', 'chai-cameroon', 'active',
  coalesce((select value from app_settings where key = 'org_profile'), '{}')::jsonb
)
on conflict (name) do nothing;

-- 3b. Make every EXISTING user an ACTIVE member of the sample country team, preserving their role.
insert into tenant_memberships (tenant_id, user_id, role, status, decided_at)
select t.id, u.id, coalesce(u.role, 'collaborator'), 'active', now()
from users u
cross join (select id from tenants where name = 'the sample country team') t
on conflict (tenant_id, user_id) do nothing;

-- 4. Add tenant_id to each PER-TENANT table, backfill existing rows to the sample country team,
--    and index it. Edit the _SCOPED_TABLES array below to change the split BEFORE running.
do $$
declare
  _tid uuid;
  _t   text;
  _scoped text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule','engagement_logs',
    'active_grants','narrative_logs','scan_decisions','donor_contacts'
  ];
begin
  select id into _tid from tenants where name = 'the sample country team';
  if _tid is null then
    raise exception 'the sample country team tenant seed failed — aborting';
  end if;
  foreach _t in array _scoped loop
    if to_regclass(_t) is null then
      raise notice 'skip: table % does not exist', _t;
      continue;
    end if;
    execute format('alter table %I add column if not exists tenant_id uuid references tenants(id)', _t);
    execute format('update %I set tenant_id = $1 where tenant_id is null', _t) using _tid;
    execute format('create index if not exists %I on %I(tenant_id)',
                   'idx_' || _t || '_tenant', _t);
  end loop;
end $$;

commit;

-- =========================================================================
-- ROLLBACK (if ever needed — no data is lost; tenant_id is purely additive):
--   do $$ declare _t text; begin
--     foreach _t in array array['rfp_submissions','meeting_logs','meeting_schedule',
--       'engagement_logs','active_grants','narrative_logs','scan_decisions','donor_contacts']
--     loop execute format('alter table %I drop column if exists tenant_id', _t); end loop;
--   end $$;
--   drop table if exists tenant_memberships;
--   drop table if exists tenants;
-- =========================================================================
