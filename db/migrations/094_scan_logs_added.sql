-- 094 — scan_logs learns the difference between "eligible" and "new".
--
-- `rfps_new` never meant what its name says. It is the first return value of
-- ingest_candidates, whose own docstring defines it as "inserted + merge-updates":
-- everything that PASSED THE ELIGIBILITY GATE, including calls that were already in
-- the pipeline and merely got refreshed. In extract-only discovery runs it means a
-- third thing again — rows written to the shared extracted store, where nothing
-- enters rfp_submissions at all.
--
-- So a screening run where all twelve eligible calls were already tracked logged
-- "12 new", the notification bell repeated it, and the Screen tab showed nothing.
-- That is the discrepancy a reader reported, and they were right: the number was
-- never a count of things they could go and look at. The app already knew the honest
-- figure — core/scan_runner.py says "(N newly added, M already in your pipeline)"
-- straight after a manual run — it just was not persisted.
--
-- rfps_added records rows this run actually CREATED. Deliberately nullable with no
-- default: existing rows predate the counter and 0 would assert "this run added
-- nothing", which is a claim the data cannot support. NULL means not recorded, and
-- the UI shows it as such. Extract-only runs also write NULL — they insert nothing
-- into rfp_submissions, and telling apart a new store row from a refreshed one would
-- cost a lookup per candidate.
--
-- Self-contained and re-runnable: `if not exists` so applying it twice is a no-op,
-- and it asserts nothing about which earlier migrations have run.

alter table scan_logs
    add column if not exists rfps_added integer;

comment on column scan_logs.rfps_added is
    'Rows this run CREATED (inserted into rfp_submissions). NULL = not recorded: a '
    'run from before this column existed, or an extract-only discovery run that '
    'writes to the shared extracted store rather than to rfp_submissions. Compare '
    'with rfps_new, which counts everything that passed the gate including '
    'merge-updates of calls already tracked.';
