-- Migration 013 — auto-promote `auto_recommendation` into `decision` for
-- auto-scanned rows that don't have a human-set decision yet.
--
-- Before this, the scan pipeline left `decision = NULL` for every row it
-- inserted, with the intent that humans would explicitly Proceed / Park /
-- Decline each via the Review tab. In practice this meant Park / Decline
-- candidates remained on the Tracking pipeline view (which filters by
-- `decision IN ('Proceed', 'Proceed as sub')` — NULL is neither, but the
-- screen counts treat them ambiguously) and forced manual review even
-- when the auto-rec was clearly Decline.
--
-- Going forward `core/auto_scorer.py` writes `decision = auto_recommendation`
-- at insert time. This migration backfills the same logic for rows
-- already inserted.

update rfp_submissions
   set decision = auto_recommendation
 where source = 'auto'
   and decision is null
   and auto_recommendation is not null
   and auto_recommendation <> '';
