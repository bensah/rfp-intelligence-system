-- Migration 053 — donor_intel: PREFER-8 (Competitiveness) multi-country signal.
-- The call/donor EXPLICITLY encourages multi-country / multi-geography proposals.
-- Matched against the org's Entity type = Multi-country Organization (org_is_multi_country):
-- encouraged + MCO → 1, mismatch → 0. TEXT ("yes"/null flag convention). Idempotent.
alter table donor_intel add column if not exists multi_country_encouraged text;
