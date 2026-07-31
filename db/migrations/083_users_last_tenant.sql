-- Migration 083 — remember a user's active tenant across logins (R3 multi-tenant switcher).
-- =========================================================================
-- A user who belongs to MORE THAN ONE tenant now lands in their last-used tenant and can
-- switch from the header dropdown. This column persists that choice across logins (the
-- app select is `users.* `, so it flows into the app_user dict automatically). Nullable;
-- SET NULL if the tenant is removed. SAFE + ADDITIVE + RE-RUN-SAFE.
-- =========================================================================

begin;

alter table users
  add column if not exists last_tenant_id uuid references tenants(id) on delete set null;

commit;

-- ROLLBACK:  begin; alter table users drop column if exists last_tenant_id; commit;
