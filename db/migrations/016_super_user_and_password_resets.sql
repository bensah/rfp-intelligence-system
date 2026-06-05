-- Migration 016 — super_user role + self-service password-reset requests.
--
-- Two changes bundled because they ship together as part of the
-- Phase 5b user-management feature:
--
--   1. Add `super_user` as a fourth role above admin. Hierarchy:
--        super_user > admin > reviewer ≈ collaborator
--      Only super_user can promote / demote admins. Admins can manage
--      reviewers + collaborators but cannot touch other admins or the
--      super_user. This prevents a rogue / compromised admin from
--      locking out the founder.
--
--   2. New `password_reset_requests` table backs the "Forgot password?"
--      link on the login page. Self-service sends a row here (NOT an
--      email reset link — we don't have the token/SMTP infra for that
--      yet); admin picks up the request from the Manage Users tab and
--      issues a temp password via the existing reset action.
--
-- The role-constraint swap uses DROP + ADD because PostgreSQL doesn't
-- support ALTER CONSTRAINT for check expressions; the brief window
-- between drop + add is safe because the existing rows already satisfy
-- the new constraint (super_user is additive).

-- Drop the old check constraint. Name follows PostgreSQL's autogen
-- pattern but use IF EXISTS in case it was named differently on a
-- particular deployment.
alter table users
  drop constraint if exists users_role_check;

alter table users
  add constraint users_role_check
  check (role in ('super_user', 'admin', 'reviewer', 'collaborator'));

-- Seed: upgrade the founder account to super_user. Email matches the
-- one used during initial deployment; safe-guarded by WHERE clause so
-- this is a no-op on any environment with a different seed account.
update users
   set role = 'super_user'
 where email = 'nsah.ben03@gmail.com';

-- Password-reset request inbox. Admin / super_user can see pending
-- rows in the Manage Users tab and action them via the existing
-- "Reset password" button (which generates a temp + flips
-- must_change_password=true).
create table if not exists password_reset_requests (
    id            uuid primary key default gen_random_uuid(),
    email         text not null,
    requested_at  timestamptz not null default now(),
    status        text not null default 'pending'
                  check (status in ('pending', 'handled', 'dismissed')),
    handled_by    text,
    handled_at    timestamptz,
    notes         text
);

create index if not exists password_reset_requests_pending_idx
  on password_reset_requests (status, requested_at)
  where status = 'pending';
