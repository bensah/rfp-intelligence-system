-- Migration 061 — source_registry: dedicated listings_url (the page/API that LISTS a
-- source's opportunities — what the scanner ingests and what the registry UI edits).
-- `sample_url` had been misused as the listings page; move it to listings_url and blank
-- sample_url (sample_url is now an OPTIONAL single-example-opportunity link, not the
-- listings page). Idempotent: the move runs only for rows not yet migrated, so any
-- future sample_url set deliberately is preserved.

alter table source_registry add column if not exists listings_url text;

update source_registry
   set listings_url = sample_url,
       sample_url   = null
 where listings_url is null
   and sample_url is not null;
