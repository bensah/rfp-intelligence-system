-- Migration 069 — user profile location fields.
-- Additive + idempotent; safe to run anytime (no code reads them until the profile
-- form update ships). Adds Address / City-Town / State-Province-Region / Country to
-- the (global) users table. Country is stored as free text here; the UI presents it
-- as a dropdown (core.geographies.COUNTRIES) so values stay canonical.
alter table users add column if not exists address       text;
alter table users add column if not exists city          text;
alter table users add column if not exists state_region  text;
alter table users add column if not exists country       text;
