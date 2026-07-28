-- Migration 072 — per-tenant identity + platform-home tenant (Phase 6b).
-- =========================================================================
-- Adds two additive columns to `tenants` and wires up the super_user's home:
--   * is_platform  boolean — marks the platform-owner tenant (the super_user's blank
--                   home). Robust to renames; the app auto-selects it for the super_user
--                   and never onboards them.
--   * org_identity jsonb   — PER-TENANT org identity (name/short/country/team/logo/
--                   contacts + the scan-eligibility flags). Kept SEPARATE from
--                   org_profile so saving one never clobbers the other. When multi-tenant
--                   is on, core.settings.get_org()/set_org() read/write this blob; the
--                   tenant's display name falls back to tenants.name.
--
-- Then it:
--   A. makes the user-created "RFPIS Inc." the platform tenant (blank identity+profile,
--      slug 'rfpis');
--   B. makes the super_user an ACTIVE super_user member of RFPIS Inc.;
--   C. (optional) removes the super_user's memberships in OTHER tenants so RFPIS Inc. is
--      their sole home — comment this out to keep the super_user in the organisation Cameroon too;
--   D. SEEDS the organisation Cameroon's CURRENT global identity (from app_settings) into its own
--      org_identity, so the organisation users + the scan see the SAME identity/flags as today once
--      get_org() becomes tenant-aware. RFPIS Inc. stays blank.
--
-- SAFE + ADDITIVE + RE-RUN-SAFE. No data is lost (app_settings is left intact) and
-- re-running NEVER blanks a tenant's saved profile/identity (see step A note). Adjust
-- the RFPIS-Inc. matcher (name ilike 'RFPIS Inc%') if you named it differently.
-- =========================================================================

begin;

-- 0. Columns ---------------------------------------------------------------
alter table tenants add column if not exists is_platform  boolean not null default false;
alter table tenants add column if not exists org_identity jsonb   not null default '{}'::jsonb;

do $$
declare
  _plat uuid;
  _uid  uuid;
begin
  -- A. Resolve the platform tenant. Prefer the already-flagged one (survives a rename
  --    of "RFPIS Inc." via the Organization editor), else the user-created name.
  select id into _plat from tenants where is_platform order by created_at limit 1;
  if _plat is null then
    select id into _plat from tenants
     where name ilike 'RFPIS Inc%' order by created_at limit 1;
  end if;
  if _plat is null then
    raise exception 'No platform tenant (is_platform flag or name "RFPIS Inc%%") — create it first, then re-run.';
  end if;

  -- Exactly one platform tenant. Free the 'rfpis' slug from any other row first
  -- (the tenants.slug unique constraint), then stamp the platform tenant.
  -- IMPORTANT: do NOT reset org_profile / org_identity here — the columns already
  -- default to '{}' when first added (step 0), so a fresh platform tenant is blank
  -- anyway, and re-running this migration must NEVER wipe a profile/identity the
  -- super_user has since filled in for this tenant (that was the earlier data-loss bug).
  update tenants set is_platform = false where is_platform and id <> _plat;
  update tenants set slug = null       where slug = 'rfpis' and id <> _plat;
  update tenants
     set is_platform = true,
         status      = 'active',
         slug        = coalesce(slug, 'rfpis')
   where id = _plat;

  -- B. Super_user is an ACTIVE super_user member of RFPIS Inc.
  select id into _uid from users where lower(email) = lower('nsah.ben03@gmail.com') limit 1;
  if _uid is null then
    raise notice 'user nsah.ben03@gmail.com not found — create the account, then re-run.';
  else
    update users set role = 'super_user' where id = _uid;
    insert into tenant_memberships (tenant_id, user_id, role, status, decided_at)
    values (_plat, _uid, 'super_user', 'active', now())
    on conflict (tenant_id, user_id) do update
      set role = 'super_user', status = 'active';

    -- C. OPTIONAL — make RFPIS Inc. the super_user's ONLY tenant (drops the organisation Cameroon
    --    et al. from their memberships). Comment out to keep them in other tenants.
    delete from tenant_memberships where user_id = _uid and tenant_id <> _plat;
  end if;

  -- D. Seed the organisation Cameroon's CURRENT global identity into its own org_identity, so a
  --    the organisation user (and in-session scoring) sees the same values as today. RFPIS Inc.
  --    stays blank. Only runs if the organisation Cameroon exists and its org_identity is still {}.
  update tenants t
     set org_identity = coalesce((
           select jsonb_object_agg(key, to_jsonb(value))
             from app_settings
            where key in (
              'org_name','org_short','org_country','org_team','org_is_us_entity',
              'org_has_local_board','org_contact_email','org_logo_url','org_website',
              'org_has_bd_team','org_is_grassroot','org_is_multi_country','org_hq_country',
              'org_logo_b64','org_logo_mime')
         ), '{}'::jsonb)
   where t.name = 'the organisation Cameroon'
     and coalesce(t.org_identity, '{}'::jsonb) = '{}'::jsonb;
end $$;

commit;

-- =========================================================================
-- ROLLBACK (columns are additive; drop them + the memberships change is manual):
--   begin;
--     -- (re-add the super_user to the organisation Cameroon if step C removed them, if desired)
--     alter table tenants drop column if exists is_platform;
--     alter table tenants drop column if exists org_identity;
--   commit;
-- =========================================================================
