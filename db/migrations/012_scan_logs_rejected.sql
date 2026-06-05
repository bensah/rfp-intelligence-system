-- Migration 012 — track candidates rejected by the eligibility gate.
--
-- Before this column, scan_logs only stored rfps_found / rfps_new /
-- rfps_duplicate. "Rejected" (country / theme / deadline / feasibility
-- gate misses) was logged to stdout but discarded. Adding the column
-- so the Admin → Manual Scan tab can show why a scan returned 0 new
-- ("we found 30 candidates but the policy rejected all of them") vs
-- ("the scrapers came back empty").

alter table scan_logs
    add column if not exists rfps_rejected integer not null default 0;
