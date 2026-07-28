-- 077_blacklist_users_tenants.sql
-- Dedicated BLACKLIST (hard block) for users and tenants — distinct from the soft
-- "deactivate" (users.is_active = false) and "suspend" (tenants.status = 'suspended')
-- states. A blacklisted user is blocked at the login gate even if is_active is true,
-- and cannot be re-approved without first being removed from the blacklist. A
-- blacklisted tenant (status = 'blacklisted') is dropped from its members' active
-- memberships, so its users lose all tenant context until it is un-blacklisted.
--
-- Powers Settings → Accounts → Blacklisted (list + remove-from-blacklist).
--
-- Re-run safe: every statement is IF NOT EXISTS / additive. No data is modified.
-- =============================================================================

-- ---- Users: orthogonal hard-block flag + audit trail ------------------------
ALTER TABLE users   ADD COLUMN IF NOT EXISTS is_blacklisted   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users   ADD COLUMN IF NOT EXISTS blacklisted_at   TIMESTAMPTZ;
ALTER TABLE users   ADD COLUMN IF NOT EXISTS blacklisted_by   TEXT;
ALTER TABLE users   ADD COLUMN IF NOT EXISTS blacklist_reason TEXT;

-- Partial index — the Blacklisted list queries only the blacklisted rows.
CREATE INDEX IF NOT EXISTS idx_users_blacklisted
    ON users (is_blacklisted) WHERE is_blacklisted;

-- ---- Tenants: 'blacklisted' becomes a third status value --------------------
-- tenants.status already exists with a CHECK constraint in ('active','suspended')
-- (migration 067). Widen that constraint to admit 'blacklisted', then add an audit
-- trail. Members of a blacklisted tenant are excluded from active_memberships()
-- (auth/tenant_context.py), which is the runtime enforcement point.
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_status_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_status_check
    CHECK (status IN ('active', 'suspended', 'blacklisted'));

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS blacklisted_at   TIMESTAMPTZ;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS blacklisted_by   TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS blacklist_reason TEXT;
