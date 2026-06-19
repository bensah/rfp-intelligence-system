-- Migration 031 - donor_intel: add funders_collaborators — the funders, pooled
-- funders, philanthropies and other partners BEHIND / alongside this donor
-- (e.g. the DIV Fund is backed by Coefficient Giving, GiveWell, Livelihood Impact
-- Fund, CRI Foundation, Global Development Incubator, Anonymous Donors). Blank for
-- most donors. Stored as a JSON array of partner names, drawn from the shared
-- partner vocabulary (core/partners.py ALL_PARTNERS) merged with the donor catalog
-- — the SAME list the org profile's "Trusted partners" pickers use (accept typed
-- additions for private firms / academic institutions). Idempotent.

alter table donor_intel add column if not exists funders_collaborators text;
