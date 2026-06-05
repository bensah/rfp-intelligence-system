-- Migration 017 — per-user access overrides.
--
-- Previously `core/permissions.ACCESS_MATRIX` was the ONLY policy
-- source: every user got the capability mapped to their role
-- (admin or "user"). That was fine for the bulk case but blocked
-- per-individual exceptions like "give one collaborator edit access
-- to the Report" or "revoke a reviewer's Pipeline access".
--
-- This column stores per-user overrides as a JSON object:
--     { "Surface name": "capability", ... }
-- where Surface keys match `ACCESS_MATRIX` (e.g. "Admin → Sources")
-- and capability values are any of the strings in the matrix
-- ("edit", "view", "view+add", "trigger", "self", "all", "hidden").
--
-- `permissions.access()` checks this column FIRST, falling back to
-- the role default when the surface is absent. Empty / NULL behaves
-- as "no overrides — use role defaults".

alter table users
  add column if not exists access_overrides jsonb not null default '{}'::jsonb;

comment on column users.access_overrides is
  'Per-user access-matrix overrides. JSON object keyed by surface name, '
  'value is the capability string. Empty {} means use role defaults.';
