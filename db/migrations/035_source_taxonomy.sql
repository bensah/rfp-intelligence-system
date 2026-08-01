-- Migration 035 — source taxonomy + canonicalisation.
-- (a) Enrich source_registry with the source-catalogue taxonomy so each host
--     carries how to treat + ingest it (serves the the source catalogue).
-- (b) Canonicalisation rule: rfp_submissions keeps the PRIMARY url in
--     opportunity_link AND the aggregator url it was discovered through, so we
--     can show provenance + always re-verify against the primary.

alter table source_registry
    add column if not exists source_class      text,   -- Primary source | Aggregator |
                                                        -- Intelligence platform | Grant database |
                                                        -- Tender database | Job aggregator |
                                                        -- ATS feed | API provider
    add column if not exists access_model       text,  -- Free | Freemium | Paid | API |
                                                        -- RSS/feed | Login required | Unknown
    add column if not exists ingestion_method   text,  -- API | RSS | sitemap crawl |
                                                        -- page crawl | email/newsletter parsing |
                                                        -- manual review
    add column if not exists has_api            boolean not null default false;

alter table rfp_submissions
    add column if not exists aggregator_url     text;   -- discovery url if resolved from an aggregator
