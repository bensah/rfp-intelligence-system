-- Migration 085 — apply multi-tenant RLS to the scoped data tables (RETROFIT of 068).
-- =========================================================================
-- 068 ("isolation goes live") never applied on this database, so DB-level tenant isolation
-- was never enforced — the app relied solely on the application-layer scoping wrapper in
-- db/supabase_client.py. This migration turns on Postgres RLS for the scoped tables, but
-- reconciled with the two features that shipped AFTER 068 (so it is NOT a verbatim 068):
--
--   * PUBLIC 'individual' tenants (migration 078): their rows on the user-facing ACTIVITY
--     tables are visible to everyone. The SELECT policy on those tables therefore allows
--     `tenant_id = mine OR tenant_id in (active individual tenants)`. WRITES stay strict.
--   * rfp_seen (migration 076): a scoped table 068 didn't list — included here (strict; a
--     public tenant's tombstones must NOT suppress others' screening).
--   * resource_suggestions (migration 080): already has its own proposer-scoped RLS — this
--     migration deliberately does NOT touch it.
--
-- PRECONDITIONS
--   1. SUPABASE_JWT_SECRET set in the deployed app AND data loads today. If the tenant
--      switcher / per-tenant pipelines work, the per-user JWT with a `tenant_id` claim is
--      already reaching PostgREST — that is exactly what these policies read.
--   2. Run migration 080 first (it created the helper fns app_current_tenant_id /
--      app_stamp_tenant_id). They are re-declared here idempotently for self-containment.
--
-- ⚠ POINT OF NO RETURN: after this, any request WITHOUT a valid tenant_id claim sees ZERO
--   rows in the scoped tables. TEST WITH ONE ADMIN IMMEDIATELY. The ROLLBACK block at the
--   bottom re-opens access instantly.
--
-- ⚠ SERVICE-ROLE WRITERS to scoped tables (scripts/migrate_excel.py, any cron/admin job)
--   bypass RLS and, having no JWT claim, are NOT auto-stamped — they must set tenant_id
--   EXPLICITLY. The live app (per-session tenant client) is covered by the trigger below.
--
-- SAFETY: this transaction ABORTS (changing nothing) if any scoped table still has rows
-- with NULL tenant_id — those would become invisible under RLS. Assign them first, re-run.
-- =========================================================================

begin;

-- Helper fns (normally from 068/080) — idempotent so 085 is self-contained.
create or replace function app_current_tenant_id() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'tenant_id', '')::uuid
$$;

create or replace function app_stamp_tenant_id() returns trigger
language plpgsql as $$
begin
  if new.tenant_id is null then
    new.tenant_id := app_current_tenant_id();
  end if;
  return new;
end $$;

-- ── 0) SAFETY GATE — abort if any scoped table has NULL tenant_id rows ───────────────────
do $$
declare
  _t   text;
  _n   bigint;
  _bad text := '';
  _scoped text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule','engagement_logs',
    'applied_funding','narrative_logs','scan_decisions','donor_contacts','rfp_seen'];
begin
  foreach _t in array _scoped loop
    if to_regclass(_t) is null then
      continue;
    end if;
    execute format('select count(*) from %I where tenant_id is null', _t) into _n;
    if _n > 0 then
      _bad := _bad || format(E'  - %s: %s row(s)\n', _t, _n);
    end if;
  end loop;
  if _bad <> '' then
    raise exception E'ABORTED — these scoped tables have NULL tenant_id rows that would become invisible under RLS. Assign each a tenant, then re-run migration 085:\n%', _bad;
  end if;
end $$;

-- ── 1) tenants / tenant_memberships — READABLE by anyone (login/onboarding run pre-JWT and
--       need tenant NAMES; the scoped SELECT policies below also subquery this). WRITES to
--       authenticated (service-role bypasses for provisioning). ────────────────────────────
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

-- ── 2) The scoped tables — strict tenant isolation + auto-stamp, with public-'individual'
--       read broadening on the user-facing ACTIVITY tables (078). ─────────────────────────
do $$
declare
  _t   text;
  _pol record;
  _all text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule','engagement_logs',
    'applied_funding','narrative_logs','scan_decisions','donor_contacts','rfp_seen'];
  -- Reads on these ALSO expose active 'individual' (public) tenants' rows to everyone —
  -- must mirror db.supabase_client._PUBLIC_VISIBLE_TABLES. EXCLUDES scan_decisions
  -- (per-tenant ML data) and rfp_seen (per-tenant tombstones).
  _public_visible text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule','engagement_logs',
    'applied_funding','narrative_logs','donor_contacts'];
  _has_kind boolean := exists(
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'tenants' and column_name = 'kind');
  _sel_using text;
begin
  foreach _t in array _all loop
    if to_regclass(_t) is null then
      raise notice 'skip: table % does not exist', _t;
      continue;
    end if;

    -- auto-stamp trigger — app inserts inherit the caller's tenant from the JWT claim
    execute format('drop trigger if exists %I on %I', 'stamp_tenant_' || _t, _t);
    execute format('create trigger %I before insert on %I for each row '
                   'execute function app_stamp_tenant_id()', 'stamp_tenant_' || _t, _t);

    -- replace ALL existing policies (incl. any permissive 023/066 baseline) with scoped ones
    for _pol in
      select policyname from pg_policies where schemaname = 'public' and tablename = _t
    loop
      execute format('drop policy if exists %I on %I', _pol.policyname, _t);
    end loop;

    execute format('alter table %I enable row level security', _t);

    -- SELECT: strict by default; broadened to active individual tenants on activity tables.
    if _has_kind and _t = any(_public_visible) then
      _sel_using := 'tenant_id = app_current_tenant_id() '
                 || 'or tenant_id in (select id from tenants '
                 || 'where kind = ''individual'' and status = ''active'')';
    else
      _sel_using := 'tenant_id = app_current_tenant_id()';
    end if;

    execute format('create policy %I on %I for select using (%s)', _t || '_sel', _t, _sel_using);
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
--       'engagement_logs','applied_funding','narrative_logs','scan_decisions',
--       'donor_contacts','rfp_seen'];
--   begin foreach _t in array _scoped loop
--     execute format('drop trigger if exists %I on %I', 'stamp_tenant_'||_t, _t);
--     for _pol in select policyname from pg_policies where tablename=_t loop
--       execute format('drop policy if exists %I on %I', _pol.policyname, _t); end loop;
--     execute format('create policy %I on %I for all using (true) with check (true)', _t||'_open', _t);
--   end loop; end $$;
-- (tenants / tenant_memberships policies are permissive read + authenticated write — leave
--  them, or `alter table … disable row level security` to fully revert.)
-- =========================================================================
