-- 078_tenant_kind_individual.sql
-- Tenant KIND: an org tenant (the default) vs an "individual" tenant that represents
-- a single person, not an organization. Individual tenants are PUBLIC — their activity
-- (pipeline, meetings, engagements, grants, narratives, donor contacts) is merged into
-- every user's read scope by the app-layer scoping wrapper (db/supabase_client.py:
-- _PUBLIC_VISIBLE_TABLES + auth/tenant_context.public_tenant_ids). Writes stay private
-- to the owning tenant; only READS are broadened.
--
-- Re-run safe: additive, IF NOT EXISTS / DROP-then-ADD constraint. No data modified.
-- =============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'organization';

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_kind_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_kind_check
    CHECK (kind IN ('organization', 'individual'));

-- Partial index — public_tenant_ids() lists only the individual tenants.
CREATE INDEX IF NOT EXISTS idx_tenants_kind_individual
    ON tenants (kind) WHERE kind = 'individual';
