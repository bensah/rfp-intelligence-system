-- 065_scan_logs_run_id.sql
-- Add a per-run identifier to scan_logs so the Manual Scan summary cards can show the
-- EXACT last extraction (all its per-source rows share one run_id), instead of guessing
-- the run boundary from a timestamp gap — which merged two runs started <15 min apart
-- and made the cards read cumulatively (e.g. 702 + 680 = 1382).
ALTER TABLE scan_logs ADD COLUMN IF NOT EXISTS run_id text;

-- Newest-run lookups filter/order by run_id; index the (run_id, scan_date) pair.
CREATE INDEX IF NOT EXISTS idx_scan_logs_run_id ON scan_logs (run_id, scan_date DESC);
