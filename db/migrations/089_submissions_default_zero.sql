-- Migration 089 — `submissions` counts donor-side submissions, so its floor is ZERO.
--
-- rfp_submissions.submissions records how many times an RFP was submitted to the funder's
-- portal: 0 while it has never been submitted, 1 once Progress = Completed, 2+ when the same
-- RFP was submitted more than once. Every submission-derived indicator (Total Submitted,
-- Approved, Under Review, Not Approved, win rate) multiplies by it.
--
-- TWO defects put a 1 on rows that were never submitted:
--   1. scripts/migrate_excel.py mapped it as `_int(get("Submissions")) or 1`. Python treats
--      0 as falsy, so every Excel 0 became 1 on import — the source workbook holds 0/1/2
--      correctly, the import corrupted it. Fixed in code (_submissions_value).
--   2. This column defaulted to 1, so any insert that omitted it (auto-scanned rows) got 1.
--
-- Result: 242 never-submitted rows carried submissions = 1, inflating every count that
-- multiplies by it. This migration fixes the default and repairs the stored data.
--
-- The repair keys off progress_status, which is the definition of "submitted": rows that are
-- Completed KEEP their value (10 rows at 1, one at 2 — that is the genuinely twice-submitted
-- RFP); everything else is reset to 0.
--
-- Idempotent.

alter table rfp_submissions alter column submissions set default 0;

comment on column rfp_submissions.submissions is
    'Donor-side submissions for this RFP: 0 until submitted, 1 once Progress = Completed, '
    '2+ when the same RFP was submitted to the funder more than once. Indicators multiply by '
    'it, so a never-submitted row MUST be 0.';

update rfp_submissions
   set submissions = 0
 where lower(coalesce(progress_status, '')) <> 'completed'
   and coalesce(submissions, 0) <> 0;

-- A Completed row must count at least once.
update rfp_submissions
   set submissions = 1
 where lower(coalesce(progress_status, '')) = 'completed'
   and coalesce(submissions, 0) = 0;

notify pgrst, 'reload schema';
