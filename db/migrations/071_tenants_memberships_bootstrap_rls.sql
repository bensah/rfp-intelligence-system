-- Migration 071 — bootstrap RLS policies for tenants + tenant_memberships.
-- =========================================================================
-- WHY THIS EXISTS
--   Migration 067 CREATED tenants / tenant_memberships but did NOT enable RLS on
--   them. If RLS then got turned on for those two tables (a Supabase dashboard
--   "Enable RLS" click, or a partial apply of 068) WITHOUT policies, the tables
--   default-DENY every request that isn't service-role:
--     * SELECT returns 0 rows   → onboarding thinks the user has no tenant
--                                  (the "Set up your workspace" wizard shows for
--                                   users who already have a membership);
--     * INSERT raises 42501     → "new row violates row-level security policy for
--                                  table tenants" when creating an org.
--
--   This migration (idempotently) puts the CORRECT bootstrap policies in place:
--   tenant NAMES are not secret and login/onboarding resolution needs them, so
--   SELECT is broad; writes are limited to `authenticated` (service-role bypasses
--   RLS entirely, so provisioning/admin paths are unaffected).
--
--   NOTE: this is EXACTLY the tenants/tenant_memberships portion of the drafted
--   migration 068 (Phase-3 RLS). It is safe to apply on its own now; when 068 is
--   applied later it re-creates the same policies (idempotent) and additionally
--   locks down the 8 SCOPED data tables. Keep the two in sync.
--
--   The app-code fix in this same change routes onboarding + tenant-admin through
--   the RLS-BYPASSING service client, so those flows work even before this SQL is
--   applied. This SQL is still needed so the PER-TENANT org profile
--   (core/org_profile.py, read/written through the tenant-scoped client) works
--   under RLS, and to establish the intended posture ahead of the JWT dry-run.
--
-- SAFE + ADDITIVE + IDEMPOTENT. No data is touched.
-- =========================================================================

-- Optional pre-flight (run this SELECT on its own first to see current state):
--   select relname, relrowsecurity
--     from pg_class where relname in ('tenants','tenant_memberships');
--   select tablename, policyname, cmd, roles, qual, with_check
--     from pg_policies
--    where tablename in ('tenants','tenant_memberships')
--    order by tablename, policyname;

begin;

-- ── tenants ──────────────────────────────────────────────────────────────
alter table tenants enable row level security;
drop policy if exists tenants_read  on tenants;
drop policy if exists tenants_write on tenants;
create policy tenants_read  on tenants for select using (true);
create policy tenants_write on tenants for all to authenticated using (true) with check (true);

-- ── tenant_memberships ────────────────────────────────────────────────────
alter table tenant_memberships enable row level security;
drop policy if exists memberships_read  on tenant_memberships;
drop policy if exists memberships_write on tenant_memberships;
create policy memberships_read  on tenant_memberships for select using (true);
create policy memberships_write on tenant_memberships for all to authenticated using (true) with check (true);

commit;

-- =========================================================================
-- ROLLBACK — re-open these two tables (drop the policies + turn RLS off):
--   begin;
--     drop policy if exists tenants_read      on tenants;
--     drop policy if exists tenants_write     on tenants;
--     drop policy if exists memberships_read  on tenant_memberships;
--     drop policy if exists memberships_write on tenant_memberships;
--     alter table tenants            disable row level security;
--     alter table tenant_memberships disable row level security;
--   commit;
-- (Service-role access — the app's onboarding/admin paths — is unaffected either
--  way, since service-role bypasses RLS.)
-- =========================================================================
