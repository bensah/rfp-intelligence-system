-- Migration 082 — tenant 'pending' status + approval-audit columns.
-- =========================================================================
-- Admins can now REQUEST a new tenant, which lands in status='pending' for a super_user
-- to approve (due-diligence gate against duplicate tenants). The super_user's own adds go
-- straight to 'active'. A pending tenant grants NO runtime access to anyone (its members
-- are dropped by auth.tenant_context.active_memberships until approved) — the creator sees
-- it only as their pending request in Settings → Tenants.
--
-- SAFE + ADDITIVE + RE-RUN-SAFE.
-- =========================================================================

begin;

-- Widen the status CHECK to allow 'pending' (created by mig 067, extended by 077).
alter table tenants drop constraint if exists tenants_status_check;
alter table tenants add constraint tenants_status_check
    check (status in ('active','suspended','blacklisted','pending'));

-- Approval audit trail (parallel to the blacklist trio).
alter table tenants add column if not exists requested_by uuid references users(id);
alter table tenants add column if not exists approved_by   text;
alter table tenants add column if not exists approved_at   timestamptz;

commit;

-- =========================================================================
-- ROLLBACK (drop pending rows first, then narrow the constraint back):
--   begin;
--     delete from tenants where status = 'pending';
--     alter table tenants drop constraint if exists tenants_status_check;
--     alter table tenants add constraint tenants_status_check
--         check (status in ('active','suspended','blacklisted'));
--     alter table tenants drop column if exists requested_by;
--     alter table tenants drop column if exists approved_by;
--     alter table tenants drop column if exists approved_at;
--   commit;
-- =========================================================================
