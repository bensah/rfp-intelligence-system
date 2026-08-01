-- Migration 070 — seed the platform-owner "RFPIS" tenant + the super_user.
-- REQUIRES migration 067 (tenants / tenant_memberships) applied first. Idempotent.
--
-- RFPIS is the super_user's home tenant with a DEFAULT/empty org profile (NOT the organisation's
-- details). The super_user manages all tenants from Settings → Tenants and (once
-- Phase-3 RLS carries a super_user bypass) can see across tenants. This seed only
-- creates the tenant + membership + role; cross-tenant DATA visibility is enforced in
-- the app (admin views) now and by the RLS super_user bypass when Phase 3 is applied.
do $$
declare _tid uuid; _uid uuid;
begin
  -- 1. RFPIS tenant with an empty org profile.
  insert into tenants (name, slug, status, org_profile)
  values ('RFPIS', 'rfpis', 'active', '{}'::jsonb)
  on conflict (name) do nothing;
  select id into _tid from tenants where name = 'RFPIS';

  -- 2. The super_user account.
  select id into _uid from users where lower(email) = lower('nsah.ben03@gmail.com') limit 1;
  if _uid is null then
    raise notice 'user nsah.ben03@gmail.com not found — create that account first, then re-run this migration.';
    return;
  end if;

  update users set role = 'super_user' where id = _uid;

  insert into tenant_memberships (tenant_id, user_id, role, status, decided_at)
  values (_tid, _uid, 'super_user', 'active', now())
  on conflict (tenant_id, user_id) do update
    set role = 'super_user', status = 'active';
end $$;
