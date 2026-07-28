-- Migration 074 — per-tenant scan_logs (notification scoping).
-- =========================================================================
-- Adds a nullable tenant_id to scan_logs so the notification feed can tell SYSTEM-WIDE
-- runs from TENANT-SPECIFIC ones:
--   * tenant_id IS NULL  → system-wide (the Friday auto-scan / discovery crawl) — shown
--                          to EVERYONE (owner rule: auto-scan is a system action).
--   * tenant_id = <id>   → a tenant's own run (eligibility / "Find my matches"
--                          screening) — shown only to that tenant's members (+ super_user).
-- Existing rows stay NULL = system-wide (shown to all), which is correct for past
-- discovery scans. Additive + idempotent; no code path breaks if applied late.
-- =========================================================================

begin;
alter table scan_logs add column if not exists tenant_id uuid references tenants(id);
create index if not exists idx_scan_logs_tenant on scan_logs(tenant_id);
commit;

-- Rollback:
--   alter table scan_logs drop column if exists tenant_id;
