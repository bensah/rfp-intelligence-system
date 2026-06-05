-- Migration 010 — add lead_applicant + sub_applicant columns to
-- rfp_submissions to mirror the two new columns added to Form1 in the
-- Excel workbook. These drive the per-grant detail display on the Grants
-- page: the deploying-org short name is auto-filled when Role=Prime
-- and Lead is empty, etc.

alter table rfp_submissions
    add column if not exists lead_applicant text,
    add column if not exists sub_applicant text;
