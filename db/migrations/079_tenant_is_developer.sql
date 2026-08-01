-- Migration 079 — developer vs client tenant category (access-control layer).
-- =========================================================================
-- Adds ONE additive column to `tenants`:
--   * is_developer boolean — marks a DEVELOPER / SYSTEM tenant (e.g. "RFPIS Inc."
--                   and "Example Tenant"). Members of a developer tenant may
--                   perform cross-tenant DEVELOPER tasks that touch the shared,
--                   platform-wide resources — donor mapping, the Sources catalog,
--                   Blocked tokens, Run Extraction, Records → Verify/Reset, and the
--                   Learning-data view. A CLIENT tenant (any normal org / individual)
--                   is confined to its OWN tenant-scoped data + settings, no matter
--                   how privileged its own admins/super_user are.
--
-- WHY a distinct column (not is_platform): is_platform marks the single super_user
-- HOME tenant (auto-selected, never onboarded). "Developer" is a CATEGORY that can
-- cover MORE than one tenant (RFPIS Inc + a second developer tenant), so it needs its own flag. The
-- platform tenant is ALWAYS a developer tenant, so we seed is_developer=true from it.
--
-- The app reads this via auth.tenant_context.developer_tenant_ids() (60s-cached on
-- the service client) + active_tenant_is_developer(); permissions.is_developer_super/
-- _admin/_member combine it with the role check. Single-tenant (multi-tenant OFF)
-- treats the sole deployment as its own developer, so nothing is locked out there.
--
-- SAFE + ADDITIVE + RE-RUN-SAFE. No data is lost; re-running only re-asserts the
-- platform → developer seed. Flag the SECOND developer tenant (a second tenant) from the UI
-- (Settings → Accounts → Tenants → "Developer tenant" toggle) or below by name.
-- =========================================================================

begin;

-- 0. Column ----------------------------------------------------------------
alter table tenants
  add column if not exists is_developer boolean not null default false;

-- 1. The platform/home tenant (RFPIS Inc.) is ALWAYS a developer tenant. -----
update tenants set is_developer = true where is_platform;

-- 2. Best-effort: also flag a tenant literally named like the second developer tenant if one
--    already exists (harmless no-op otherwise). The UI toggle is the durable
--    mechanism; this just spares a manual step when the tenant is present.
update tenants set is_developer = true
 where is_developer = false
   and (name ilike '%example%' or slug ilike '%example%'
        or name ilike 'RFP Intelligence System App%');

commit;

-- =========================================================================
-- ROLLBACK (additive column):
--   begin;
--     alter table tenants drop column if exists is_developer;
--   commit;
-- =========================================================================
