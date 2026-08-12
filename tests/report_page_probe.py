"""Runs the report page under AppTest and prints what it rendered, as JSON.

Driven as a SUBPROCESS by tests/test_report_sections.py. It has to be a subprocess: the report
page is a script-scope Streamlit module, and running it in the same interpreter as the other
AppTest module in this suite makes it render nothing — Streamlit keeps global runtime state, and
our own modules stay bound in sys.modules to whichever `streamlit` was live when first imported.
Purging and re-importing both was not enough. A fresh interpreter is the one isolation that
holds, and it costs a few seconds once.

Not a test module itself — it has no test_* functions, so unittest discovery ignores it.
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")


# Synthetic rows, shaped like the real tables. All names, links and figures are invented.
#
# The page must be driven with NON-EMPTY data. Every chart is built inside an `if not
# frame.empty` branch, so an empty database renders the headings and skips every figure — which
# is precisely where the colour arguments, category orders and column references live. A run on
# empty tables passed while `dec_df` was being referenced by the wrong name.
_TEAM = ["avery", "blake", "casey"]
_DONORS = ["Northwind Trust", "Meridian Fund", "Southbank Foundation"]
_DECISIONS = ["Proceed", "Proceed", "Proceed", "Park", "Decline", None]
_PROGRESS = ["Completed", "In Progress", "Not Started", "Discontinued", "Completed", None]
_DONOR_DEC = ["Approved", "Not Approved", "Under Review", "Not submitted", "Submitted", None]


# EVERY column of the real table, defaulted to None. The page indexes columns directly,
# so a fixture missing one crashes with a KeyError that has nothing to do with the code
# under test — which is exactly what happened on the first attempt ('date_completed').
# Column NAMES are schema, not tenant data.
_SUBMISSION_COLUMNS = [
    "action_deadline", "agency_code", "aggregator_url", "alignment_score", "amount_requested",
    "amount_secured", "applicant_role", "application_checklist", "apply_url", "assigned_to",
    "auto_recommendation", "bid_effort", "brief_description", "call_award_ceiling",
    "call_award_floor", "call_award_value", "call_compliance_flags", "call_domain_areas",
    "call_geographic_scope", "call_submission_deadline", "capacity", "cofinancing",
    "compliance_requirements", "contributors", "created_at", "criteria_component_overrides",
    "currency", "currency_secured", "date_completed", "date_of_approval", "date_posted",
    "decision", "decision_date", "decision_note", "decision_overridden_at",
    "decision_overridden_by", "decline_flags_present", "donor_decision", "donor_engaged",
    "donor_program_officer", "duplicate_of_uid", "eligibility_specifics",
    "expected_award_date", "expected_awards", "extraction_uid", "feasibility", "focus_theme",
    "form_end_date", "form_id", "form_start_date", "funder_relationship", "funding_agency",
    "funding_opportunity_number", "funding_quality", "funding_tiers", "funding_window",
    "geographic_fit", "how_to_apply", "id", "instrument_type", "is_duplicate", "key_risks",
    "kickoff_date", "last_seen_at", "last_update", "lead_applicant", "merge_conflicts",
    "next_action", "next_step", "notes", "opportunity_id", "opportunity_link",
    "opportunity_title", "opportunity_type", "progress_status", "project_duration",
    "proposal_lead", "qualification", "remarks", "review_week", "reviewers", "search_date",
    "solicitation_type", "source", "stage", "strategic_fit", "sub_applicant",
    "submission_format", "submissions", "submitted_at", "submitted_by", "submitted_by_email",
    "support_roles", "tenant_id", "time_to_award", "total_program_funding", "uid",
    "updated_at",
]


def _submissions() -> list[dict]:
    rows = []
    for i in range(6):
        row = {c: None for c in _SUBMISSION_COLUMNS}
        row.update({
            "id": i + 1, "uid": f"rfp_{i:03d}", "tenant_id": "t_probe",
            # Titles carry words from the curated niche vocabulary, or the keyword cloud
            # extracts nothing and the card is legitimately absent — which would make a test
            # asserting its presence unfalsifiable.
            "opportunity_title": ["Malaria diagnostics scale-up",
                                  "Immunization supply chain strengthening",
                                  "Maternal and newborn health financing",
                                  "Digital health information systems",
                                  "Tuberculosis case finding",
                                  "Nutrition and food security programme"][i % 6],
            "opportunity_link": f"https://funder.example/call/{i}",
            "opportunity_id": f"OPP-{i:04d}",
            "funding_agency": _DONORS[i % len(_DONORS)],
            "source": "funder.example",
            "search_date": f"2026-0{(i % 6) + 1}-05T00:00:00",
            "decision": _DECISIONS[i], "decision_date": f"2026-0{(i % 6) + 1}-20",
            "decision_overridden_by": (_TEAM[0] if i == 1 else None),
            "auto_recommendation": ("Proceed" if i % 2 == 0 else "Park"),
            "progress_status": _PROGRESS[i], "donor_decision": _DONOR_DEC[i],
            "submissions": (1 if i < 4 else 0),
            "submitted_by": _TEAM[i % len(_TEAM)],
            "proposal_lead": _TEAM[i % len(_TEAM)],
            "contributors": ", ".join(_TEAM[: (i % 3) + 1]),
            "lead_applicant": "Northline Statistics Group Inc.; (NSG)" if i == 0 else "Org North",
            "sub_applicant": "Org South, Inc.; Org East" if i == 1 else "Org South",
            "amount_requested": 250000 + i * 50000, "currency": "USD",
            "amount_secured": (120000 if i == 0 else 0), "currency_secured": "USD",
            "date_of_approval": ("2026-05-01" if i == 0 else None),
            "is_duplicate": False, "duplicate_of_uid": None,
            "alignment_score": 3, "qualification": 3, "capacity": 2, "geographic_fit": 3,
            "strategic_fit": 3, "competitiveness": 2, "funding_quality": 3, "feasibility": 2,
            "cofinancing": 3,
            "call_submission_deadline": "2026-12-01",
            "solicitation_type": "RFP", "instrument_type": "Grant",
            "focus_theme": "Health Systems", "stage": "Screened",
            # Programme areas, in the SHAPES the live column really holds: a real list, the
            # internal category prefix, and the extractor's placeholder — so the focus-area
            # cloud's merging and filtering are exercised rather than assumed.
            "call_domain_areas": [
                ["MNCH", "Cross-cutting - Digital Health (+AI)"],
                ["IDs - Malaria & NTDs", "Digital Health"],
                ["WCH - Vaccines", "Unspecified Program Areas"],
                ["Cross-cutting - Research"],
                ["WCH - Nutrition"],
                None,
            ][i % 6],
            "assigned_to": _TEAM[i % len(_TEAM)],
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-06-01T00:00:00",
            "date_completed": ("2026-04-01" if i < 4 else None),
            "submitted_at": ("2026-03-15T00:00:00" if i < 4 else None),
        })
        rows.append(row)
    return rows


def _meetings() -> list[dict]:
    return [{"id": i + 1, "tenant_id": "t_probe", "rfp_uid": f"rfp_{i:03d}",
             "meeting_date": f"2026-0{i + 1}-10", "owner": _TEAM[i % len(_TEAM)],
             "donor_title": _DONORS[i % len(_DONORS)],
             "actions": "Follow up on budget annex.", "remarks": "Discussed scope.",
             "is_resolved": (i % 2 == 0), "deadline": f"2026-0{i + 2}-01",
             "source": "form", "created_by": _TEAM[0], "external_id": None,
             "created_at": "2026-01-01T00:00:00"} for i in range(4)]


def _engagements() -> list[dict]:
    return [{"id": i + 1, "tenant_id": "t_probe", "donor_title": _DONORS[i % len(_DONORS)],
             "engagement_date": f"2026-0{i + 1}-15", "owner": _TEAM[i % len(_TEAM)],
             "purpose": "Introductory call", "outcome": "Positive",
             "linked_rfp_uid": f"rfp_{i:03d}", "donor_contacts": "programme officer",
             "created_at": "2026-01-01T00:00:00"} for i in range(4)]


def _funding() -> list[dict]:
    return [{"id": i + 1, "tenant_id": "t_probe", "funding_id": f"AF-{i:03d}",
             "donor_title": _DONORS[i % len(_DONORS)], "status": ("Approved" if i == 0 else "Submitted"),
             "submitted_date": f"2026-0{i + 1}-01", "award_date": ("2026-05-01" if i == 0 else None),
             "end_date": "2027-01-01", "report_due_date": "2027-02-01", "report_type": "Annual",
             "owner": _TEAM[i % len(_TEAM)], "remarks": "", "form_id_link": None,
             "source": "form", "created_at": "2026-01-01T00:00:00",
             "updated_at": "2026-06-01T00:00:00"} for i in range(3)]


def _scans() -> list[dict]:
    return [{"id": i + 1, "tenant_id": "t_probe", "run_id": f"run_{i}",
             "scan_date": f"2026-0{i + 1}-02T03:00:00", "source": "funder.example",
             "rfps_found": 40, "rfps_new": 6, "rfps_duplicate": 4, "rfps_rejected": 30,
             "duration_sec": 120, "errors": None, "triggered_by": "cron"} for i in range(3)]


# Set RFPIS_PROBE_EMPTY to a comma-separated list of tables to force empty, so a caller can
# reproduce "this tenant has no scan runs in the period" while RFP data still exists.
_FORCE_EMPTY = {t.strip() for t in os.environ.get("RFPIS_PROBE_EMPTY", "").split(",") if t.strip()}

_TABLES = {
    "rfp_submissions": _submissions,
    "meeting_logs": _meetings,
    "engagement_logs": _engagements,
    "applied_funding": _funding,
    "scan_logs": _scans,
}


class _Query:
    """Returns synthetic rows for the tables the report reads, empty for anything else."""

    def __init__(self, table: str):
        self._table = table

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def execute(self):
        if self._table in _FORCE_EMPTY:
            return mock.Mock(data=[])
        rows = _TABLES.get(self._table, lambda: [])()
        return mock.Mock(data=rows)


class _Client:
    def table(self, name, *a, **k): return _Query(name)


def main() -> int:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("app_pages/report.py", default_timeout=180)
    at.session_state["app_user"] = {"email": "dev@example.com", "role": "super_user"}
    # Everything below the advanced filter sits behind a Generate gate that ends the script
    # with st.stop(), so without this the page renders the filter and nothing else.
    at.session_state["report_generated"] = True

    with mock.patch("db.supabase_client.get_client", return_value=_Client()), \
         mock.patch("db.supabase_client.service_client", return_value=_Client()):
        at.run()

    print("---PROBE---")
    print(json.dumps({
        # render_view CATCHES exceptions and renders them as page elements, so a crash never
        # reaches at.exception — collecting only that made the probe blind to real failures.
        "exceptions": ([str(e.value)[:400] for e in at.exception]
                       + [str(e.value)[:400] for e in at.error]),
        "subheaders": [s.value for s in at.subheader],
        "markdown": "\n".join(str(m.value) for m in at.markdown),
        "n_charts": len(at.get("plotly_chart")),
        "captions": [str(c.value)[:120] for c in at.caption],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
