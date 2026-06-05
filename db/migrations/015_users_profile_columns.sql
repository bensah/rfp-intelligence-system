-- Migration 015 — extend `users` with profile columns + password lifecycle.
--
-- Backs the new top-level User page (replaces the Submit nav entry on
-- 2026-06-05). Every logged-in user can self-edit phone / job_title /
-- department / program; admins see the same form for any user, plus
-- role + is_active + reset password.
--
-- Password-lifecycle additions:
--   * must_change_password — flips true when admin creates a user or
--     force-resets the password; the next login redirects to the
--     Change Password screen until flipped back to false.
--   * password_changed_at — set by the Change Password handler; shown
--     in the admin Manage Users table so stale passwords are visible
--     at a glance.

alter table users
  add column if not exists phone                 text,
  add column if not exists job_title             text,
  add column if not exists department            text,
  add column if not exists program               text,
  add column if not exists must_change_password  boolean not null default false,
  add column if not exists password_changed_at   timestamptz;

-- Backfill: any existing user whose hash is set has implicitly
-- "changed it" at account creation time. Without this, the password-
-- age column would show NULL for every legacy account.
update users
   set password_changed_at = coalesce(password_changed_at, created_at)
 where password_hash is not null
   and password_changed_at is null;
