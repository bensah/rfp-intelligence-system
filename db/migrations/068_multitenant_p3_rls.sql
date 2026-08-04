-- Migration 068 — MULTI-TENANT Phase 3: Row-Level Security (isolation goes LIVE).
-- =========================================================================
-- ⚠⚠ DO NOT APPLY until ALL prerequisites are TRUE, in this order:
--   1. PR #45 (Phase 2 code: tenant JWT + session-aware client) merged & deployed.
--   2. Migration 067 applied (tenants / tenant_memberships exist; tenant_id
--      backfilled on the scoped tables).
--   3. SUPABASE_JWT_SECRET set in Streamlit secrets (+ local .env) and VERIFIED:
--      log in, open Pipelines / Home / Donors and confirm data still loads. That
--      proves the per-user tenant JWT reaches PostgREST (claims populated) WHILE
--      RLS is still the permissive baseline — a safe dry run. ONLY THEN apply this.
--
-- ⚠ POINT OF NO RETURN: after this, any request WITHOUT a valid tenant_id claim
--   sees ZERO rows in the scoped tables. If the claim isn't flowing, the app looks
--   empty. Test with ONE admin first. The ROLLBACK block at the bottom re-opens
--   access instantly.
--
-- ⚠ FOLLOW-UP (app code, before these paths are used post-RLS): any writer that
--   uses the SERVICE-ROLE key bypasses RLS AND the auto-stamp trigger's claim is
--   NULL there, so it must set tenant_id EXPLICITLY. Known service-role writers to
--   scoped tables: scripts/migrate_excel.py (rfp_submissions + the *_logs), and any
--   admin/cron job that writes a scoped table. The live Streamlit app (per-session
--   tenant client) is covered by the auto-stamp trigger below.
-- =========================================================================

begin;

-- The tenant_id carried by the request JWT (NULL for anon / no claim / service-role).
create or replace function app_current_tenant_id() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'tenant_id', '')::uuid
$$;

-- ONE shared trigger fn: stamp tenant_id from the JWT claim when an insert omits it,
-- so existing INSERT code (app, per-session tenant client) needs no change.
create or replace function app_stamp_tenant_id() returns trigger
language plpgsql as $$
begin
  if new.tenant_id is null then
    new.tenant_id := app_current_tenant_id();
  end if;
  return new;
end $$;

-- ── tenants / tenant_memberships — READABLE by anyone (login resolution runs as
--    anon BEFORE a tenant JWT exists, and the onboarding type-ahead needs the tenant
--    NAMES, which aren't secret). WRITES limited to authenticated (service-role
--    bypasses RLS for provisioning). Membership rows only expose "user ↔ tenant",
--    acceptable for this internal tool; tighten later if needed.
alter table tenants enable row level security;
drop policy if exists tenants_read  on tenants;
drop policy if exists tenants_write on tenants;
create policy tenants_read  on tenants for select using (true);
create policy tenants_write on tenants for all to authenticated using (true) with check (true);

alter table tenant_memberships enable row level security;
drop policy if exists memberships_read  on tenant_memberships;
drop policy if exists memberships_write on tenant_memberships;
create policy memberships_read  on tenant_memberships for select using (true);
create policy memberships_write on tenant_memberships for all to authenticated using (true) with check (true);

-- ── The 8 SCOPED tables — strict tenant_id isolation + auto-stamp on insert.
--    (Must match migration 067's _SCOPED_TABLES; adjust BOTH together if the split
--    changes. Debatable: donor_intel/scan_decisions/donor_contacts — see PR #44.)
do $$
declare
  _t   text;
  _pol record;
  _scoped text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule','engagement_logs',
    'applied_funding','narrative_logs','scan_decisions','donor_contacts'];
begin
  foreach _t in array _scoped loop
    if to_regclass(_t) is null then
      raise notice 'skip: table % does not exist', _t;
      continue;
    end if;

    -- auto-stamp trigger (app inserts inherit the caller's tenant from the JWT claim)
    execute format('drop trigger if exists %I on %I', 'stamp_tenant_' || _t, _t);
    execute format('create trigger %I before insert on %I for each row '
                   'execute function app_stamp_tenant_id()', 'stamp_tenant_' || _t, _t);

    -- replace ALL existing policies (incl. the permissive 023/066 baseline) with
    -- tenant-scoped ones
    for _pol in
      select policyname from pg_policies where schemaname = 'public' and tablename = _t
    loop
      execute format('drop policy if exists %I on %I', _pol.policyname, _t);
    end loop;

    execute format('alter table %I enable row level security', _t);
    execute format('create policy %I on %I for select using (tenant_id = app_current_tenant_id())',
                   _t || '_sel', _t);
    execute format('create policy %I on %I for insert with check (tenant_id = app_current_tenant_id())',
                   _t || '_ins', _t);
    execute format('create policy %I on %I for update using (tenant_id = app_current_tenant_id()) '
                   'with check (tenant_id = app_current_tenant_id())', _t || '_upd', _t);
    execute format('create policy %I on %I for delete using (tenant_id = app_current_tenant_id())',
                   _t || '_del', _t);
  end loop;
end $$;

commit;

-- =========================================================================
-- ROLLBACK — instantly re-open access if the tenant claim isn't flowing:
--   do $$ declare _t text; _pol record;
--     _scoped text[] := array['rfp_submissions','meeting_logs','meeting_schedule',
--       'engagement_logs','applied_funding','narrative_logs','scan_decisions','donor_contacts'];
--   begin foreach _t in array _scoped loop
--     execute format('drop trigger if exists %I on %I', 'stamp_tenant_'||_t, _t);
--     for _pol in select policyname from pg_policies where tablename=_t loop
--       execute format('drop policy if exists %I on %I', _pol.policyname, _t); end loop;
--     execute format('create policy %I on %I for all using (true) with check (true)', _t||'_open', _t);
--   end loop; end $$;
-- =========================================================================
