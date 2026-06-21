-- 038: per-opportunity TYPE captured at scan time (Grant / RFP / CFP / Tender /
-- Cooperative Agreement / …). Different donors label it differently (grants.gov =
-- "Funding Instrument Type", others in the title) — core.opportunity_type.detect()
-- normalises it. Stored on inserted RFPs (rfp_submissions) and on the labeled
-- reject rows (scan_decisions); the per-source aggregate stays in
-- source_registry.opportunity_types. Idempotent.

alter table rfp_submissions add column if not exists opportunity_type text;
alter table scan_decisions  add column if not exists opportunity_type text;
