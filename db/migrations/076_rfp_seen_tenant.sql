-- Migration 076 — per-tenant seen-ledger (fixes cross-tenant screening suppression).
-- =========================================================================
-- The `rfp_seen` tombstone ledger (migration 033) is GLOBAL. In the new Option-C
-- per-tenant screening loop, the SUPPRESS path (scan_pipeline) matches a candidate against
-- the WHOLE ledger by link/title/deadline — so once the first tenant screened a call and
-- recorded its tombstone, every LATER tenant matched it and skipped inserting the call,
-- i.e. only the alphabetically-first tenant ever got populated.
--
-- Fix: scope the ledger by tenant. With tenant_id added AND `rfp_seen` added to the app's
-- _TENANT_SCOPED_TABLES (code change in db/supabase_client.py), the get_client() wrapper
-- auto-filters fetch_all() to the current tenant and stamps record() with it — so each
-- tenant's suppression only considers ITS OWN tombstones. Existing tombstones are
-- backfilled to the organisation Cameroon (they were the organisation's historically). Single-tenant/super_user
-- (unscoped) still see the full ledger.
--
-- SAFE + ADDITIVE + IDEMPOTENT.
-- =========================================================================

begin;

alter table rfp_seen add column if not exists tenant_id uuid references tenants(id);
create index if not exists idx_rfp_seen_tenant on rfp_seen(tenant_id);

-- Backfill the pre-existing global tombstones to the organisation Cameroon (their historical owner).
update rfp_seen
   set tenant_id = (select id from tenants where name = 'the organisation Cameroon' limit 1)
 where tenant_id is null
   and exists (select 1 from tenants where name = 'the organisation Cameroon');

commit;

-- Rollback:
--   alter table rfp_seen drop column if exists tenant_id;
