"""One opportunity, resolved and laid out for its own page.

The Live Opportunity Feed links every title, and until now every one of those links went
to the bare `/pipelines` page — the same destination for all of them, so the click told
you nothing and you still had to find the row by hand. Worse, the FEATURED card ranks the
SHARED catalog (`extracted_solicitations`), and those calls are not in `rfp_submissions` at
all: there was no page in the app that could show one.

So a uid can name a row in either store, and this module resolves both:

  pipeline — `rfp_submissions`, tenant-scoped. Already screened: it has criteria, an
             alignment score and a recommendation, and can be opened in Review.
  catalog  — `extracted_solicitations`, deliberately NOT tenant-scoped (it is the shared
             pool the Featured card ranks, which is what makes a screening miss
             recoverable). Not screened for anyone: it has extraction detail but no score,
             which is what "Track this opportunity" is for.

Kept free of Streamlit so the resolution and the field layout can be tested.
"""
from __future__ import annotations

from typing import Any

KIND_PIPELINE = "pipeline"
KIND_CATALOG = "catalog"

# The catalog carries far more of the call than the pipeline row keeps — full description,
# what is and is not funded, eligibility, attachments. That detail is the point of giving an
# opportunity its own page, so it is all selected.
CATALOG_FIELDS = "*"
PIPELINE_FIELDS = "*"


def _blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    s = str(v).strip()
    # "[]" / "{}" are the SERIALISED empty collections: several of these columns are jsonb
    # and come back as strings, so an empty one reached the page as a literal "[]".
    return s in ("", "[]", "{}") or s.lower() in (
        "nan", "none", "null", "n/a", "not stated", "not specified", "unknown")


def display_value(v: Any) -> str:
    """One field's value as text.

    jsonb columns arrive inconsistently — sometimes a real list, sometimes a
    JSON-encoded string, occasionally a list whose single element is itself an encoded
    list. `_as_list` already untangles all three for the Review card, so it is reused
    here rather than letting a raw Python repr (`['Ukraine', 'United States']`) reach the
    page.
    """
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        items = list(v)
    elif isinstance(v, str) and v.strip().startswith(("[", "{")):
        try:
            from core.criteria_derive import _as_list
            items = _as_list(v)
        except Exception:
            return v.strip()
        if not items:
            return ""
    else:
        return str(v).strip()
    try:
        from core.criteria_derive import _as_list
        items = _as_list(items)
    except Exception:
        items = [str(x) for x in items]
    return ", ".join(str(x) for x in items if str(x).strip())


def load(uid: str, *, pipeline_reader=None, catalog_reader=None) -> dict:
    """Resolve `uid` to one opportunity.

    Returns ``{"kind": KIND_PIPELINE|KIND_CATALOG|None, "row": dict|None}``. The pipeline
    is tried FIRST: once a catalog call has been tracked, the tenant's own screened row is
    the more informative answer for the same uid.

    Readers are injected so this is testable without a database. Each takes a uid and
    returns a row dict or None.
    """
    uid = (uid or "").strip()
    if not uid:
        return {"kind": None, "row": None}
    for kind, reader in ((KIND_PIPELINE, pipeline_reader),
                         (KIND_CATALOG, catalog_reader)):
        if reader is None:
            continue
        try:
            row = reader(uid)
        except Exception:
            row = None
        if row:
            return {"kind": kind, "row": row}
    return {"kind": None, "row": None}


def to_candidate(row: dict) -> dict:
    """A catalog row → the candidate dict `core.found_loader.load_candidate` scores.

    Maps the catalog's own column names onto the pipeline's (`opportunity_name` →
    `opportunity_title`, `funder_name` → `funding_agency`, `deadline` →
    `call_submission_deadline`, `grant_amount` → `call_award_value`) and carries the
    extraction the crawl already paid for, so tracking does not throw away geography,
    domains or duration and re-derive them from a title.
    """
    row = row or {}
    cand = {
        "opportunity_title": (str(row.get("opportunity_name") or "").strip()[:300]
                              or None),
        "opportunity_link": row.get("opportunity_url") or row.get("apply_url"),
        "funding_agency": (str(row.get("funder_name") or "").strip() or None),
        "brief_description": row.get("brief_description") or row.get("full_description"),
        "call_submission_deadline": (str(row.get("deadline"))[:10]
                                     if row.get("deadline") else None),
        "date_posted": (str(row.get("date_posted"))[:10]
                        if row.get("date_posted") else None),
        "call_award_value": row.get("grant_amount"),
        "currency": row.get("currency"),
        "call_geographic_scope": row.get("call_geographic_scope"),
        "call_domain_areas": row.get("call_domain_areas") or row.get("focus_themes"),
        "instrument_type": row.get("instrument_type") or row.get("solicitation_type"),
        "project_duration": row.get("project_duration"),
        "expected_award_date": row.get("expected_award_date"),
        "funding_window": row.get("funding_window"),
        "raw_text": row.get("raw_text"),
    }
    return {k: v for k, v in cand.items() if not _blank(v)}


# (label, column) per section. Rendered in order, blanks dropped — a page of "—" tells the
# reviewer nothing, and the point of this page is the detail that IS there.
_CATALOG_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("The award", (
        ("Value", "grant_amount"),
        ("Award floor", "call_award_floor"),
        ("Award ceiling", "call_award_ceiling"),
        ("Total programme funding", "total_program_funding"),
        ("Expected number of awards", "expected_awards"),
        ("Funding tiers", "funding_tiers"),
        ("Currency", "currency"),
    )),
    ("Timing", (
        ("Deadline", "deadline"),
        ("Deadline confidence", "deadline_confidence"),
        ("Posted", "date_posted"),
        ("Expected award date", "expected_award_date"),
        ("Time to award", "time_to_award"),
        ("Project duration", "project_duration"),
        ("Funding window", "funding_window"),
    )),
    ("Scope", (
        ("Geographic scope", "call_geographic_scope"),
        ("Domain areas", "call_domain_areas"),
        ("Focus themes", "focus_themes"),
        ("Project stages", "project_stages"),
        ("What is funded", "what_is_funded"),
        ("What is NOT funded", "what_is_not_funded"),
    )),
    ("Eligibility", (
        ("Applicant types", "eligibility_applicant_types"),
        ("Countries", "eligibility_countries"),
        ("Other requirements", "eligibility_other"),
        ("Applicant fit profile", "applicant_fit_profile"),
    )),
    ("The call", (
        ("Funder", "funder_name"),
        ("Grantmaking entity", "grantmaking_entity"),
        ("Instrument", "instrument_type"),
        ("Solicitation type", "solicitation_type"),
        ("Opportunity type", "opportunity_type"),
        ("Opportunity number", "funding_opportunity_number"),
        ("Agency code", "agency_code"),
        ("Funding status", "funding_status"),
        ("Submission format", "submission_format"),
        ("Language", "solicitation_language"),
    )),
    ("Provenance", (
        ("Source", "source"),
        ("Extraction confidence", "extraction_confidence"),
        ("First seen", "scraped_at"),
        ("Last updated", "updated_at"),
    )),
)

_PIPELINE_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("The award", (
        ("Value", "call_award_value"),
        ("Currency", "currency"),
        ("Applicant role", "applicant_role"),
    )),
    ("Timing", (
        ("Deadline", "call_submission_deadline"),
        ("Posted", "date_posted"),
        ("Expected award date", "expected_award_date"),
        ("Project duration", "project_duration"),
        ("Funding window", "funding_window"),
    )),
    ("Scope", (
        ("Geographic scope", "call_geographic_scope"),
        ("Domain areas", "call_domain_areas"),
        ("Focus theme", "focus_theme"),
    )),
    ("Eligibility & compliance", (
        ("Eligibility specifics", "eligibility_specifics"),
        ("Compliance requirements", "compliance_requirements"),
        ("Key risks", "key_risks"),
    )),
    ("The call", (
        ("Funder", "funding_agency"),
        ("Instrument", "instrument_type"),
    )),
    ("Screening", (
        ("Bid Strength", "alignment_score"),
        ("System recommendation", "auto_recommendation"),
        ("Team decision", "decision"),
        ("Decision rationale", "decision_note"),
        ("Reviewed on", "decision_date"),
        ("Review week", "review_week"),
    )),
    ("Provenance", (
        ("Source", "source"),
        ("Submitted by", "submitted_by"),
        ("Submitted at", "submitted_at"),
    )),
)


def sections(kind: str, row: dict) -> list[tuple[str, list[tuple[str, Any]]]]:
    """[(section title, [(label, value), ...]), ...] with blanks and empty sections
    dropped."""
    spec = _CATALOG_SECTIONS if kind == KIND_CATALOG else _PIPELINE_SECTIONS
    out: list[tuple[str, list[tuple[str, Any]]]] = []
    for title, fields in spec:
        rows = [(label, row.get(col)) for label, col in fields
                if not _blank(row.get(col))]
        if rows:
            out.append((title, rows))
    return out


def title_of(kind: str, row: dict) -> str:
    row = row or {}
    if kind == KIND_CATALOG:
        return str(row.get("opportunity_name") or "").strip() or "(untitled opportunity)"
    return str(row.get("opportunity_title") or "").strip() or "(untitled opportunity)"


def link_of(kind: str, row: dict) -> str:
    row = row or {}
    if kind == KIND_CATALOG:
        return str(row.get("opportunity_url") or row.get("apply_url") or "").strip()
    return str(row.get("opportunity_link") or "").strip()


def narrative_of(kind: str, row: dict) -> str:
    """The longest human-readable description available, preferring the fuller text."""
    row = row or {}
    if kind == KIND_CATALOG:
        for f in ("full_description", "brief_description"):
            if not _blank(row.get(f)):
                return str(row[f]).strip()
        return ""
    return "" if _blank(row.get("brief_description")) else str(row["brief_description"]).strip()


def normalise_link(v: Any) -> str:
    """The comparison form of a call URL — lowercased, trailing slash dropped. Mirrors
    what views.opportunity_rail already uses to keep Featured from repeating a call the
    tenant can see in Review."""
    return str(v or "").strip().lower().rstrip("/")


def tracked_uid(catalog_row: dict, pipeline_rows: list[dict] | None) -> str | None:
    """The tenant's OWN pipeline uid for this catalogue call, if they have already tracked
    it, else None.

    Tracking mints a NEW uid (the catalogue uid names a row in a different table), so
    /opportunity?uid=<catalogue uid> keeps resolving to the catalogue row afterwards.
    Without this the page would offer "Track this opportunity" again on the next visit, and
    the reviewer would only discover it was already tracked by pressing it. Matched on the
    call URL, which is the identity both stores share.
    """
    link = normalise_link((catalog_row or {}).get("opportunity_url")
                          or (catalog_row or {}).get("apply_url"))
    if not link:
        return None
    for r in pipeline_rows or []:
        if normalise_link(r.get("opportunity_link")) == link:
            return str(r.get("uid") or "").strip() or None
    return None


def is_screened(kind: str, row: dict) -> bool:
    """Has this opportunity been through the scoring pipeline for this tenant? Only a
    pipeline row can have been, and only if it actually carries a score."""
    if kind != KIND_PIPELINE:
        return False
    return not _blank((row or {}).get("alignment_score"))
