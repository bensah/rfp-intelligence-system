-- 040: flag registry rows that already exist in the scan catalogue (donor_sources),
-- so future "push confirmed primaries" excludes them (no duplicates across the two).
alter table source_registry add column if not exists in_catalogue boolean default false;

-- Helps the admin UI filter "not yet pushed" quickly.
create index if not exists source_registry_in_catalogue_idx
  on source_registry (in_catalogue);
