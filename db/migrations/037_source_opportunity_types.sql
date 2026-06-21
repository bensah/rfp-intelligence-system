-- 037: harmonise the source schemas (registry <-> donor catalogue) + tag types.
--
-- (A) opportunity_types: WHAT each source publishes (Grant / RFP / Tender / Job /
--     Fellowship / …, multi-valued). On BOTH layers so push carries it and the
--     scan can segregate by type. Untagged = donor-RFP (behaviour unchanged).
--
-- (B) donor_name / donor_code on source_registry — the "Donor" + "Code" columns
--     Bernard added so the registry matches the donor catalogue 1:1.
--
-- (C) access_model / source_class on donor_sources — so the catalogue can DISPLAY
--     Access + Source class (pushed from the registry).
--
-- (D) Single source of truth for ALL scan jobs: allow scrape_method='html_js'
--     (the old CHECK rejected it, forcing JS donors like NORAD into yaml).
--
-- NOTE: donor_sources.scrape_method == source_registry.ingestion_method (both are
-- "Method" / "Best ingestion method"); kept under their existing column names and
-- reconciled by push_primaries — no rename. Idempotent.

alter table source_registry add column if not exists opportunity_types text[];
alter table source_registry add column if not exists donor_name  text;
alter table source_registry add column if not exists donor_code  text;

alter table donor_sources   add column if not exists opportunity_types text[];
alter table donor_sources   add column if not exists access_model  text;
alter table donor_sources   add column if not exists source_class  text;

alter table donor_sources drop constraint if exists donor_sources_scrape_method_check;
alter table donor_sources add constraint donor_sources_scrape_method_check
    check (scrape_method in ('html', 'html_js', 'rss', 'rest_json', 'manual'));
