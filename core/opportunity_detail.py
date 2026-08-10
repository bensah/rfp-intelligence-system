"""One opportunity, resolved and laid out as a FULL READ in the RFPIS standard format.

Two stores hold an opportunity, and a uid can name either:

  pipeline — `rfp_submissions`, tenant-scoped. This row is structured FOR MATCHING: it
             keeps the handful of `call_*` fields the nine criteria score against, and
             throws the rest of the call away.
  catalog  — `extracted_solicitations`, deliberately NOT tenant-scoped (the shared pool
             the Featured card ranks, which is what makes a screening miss recoverable).
             This is the RAW EXTRACTION — the regex + LLM + deep-read output against the
             schema in docs/DATA_SCHEMA_ETL.md §4, and far richer than the matching row.

A screened row is joined back to its extraction by call URL, so a pipeline opportunity
shows the raw extraction too rather than only the fields matching kept. 190 of 254 live
pipeline rows join this way (all 184 auto-scanned ones; migrated rows predate the store).

WHY WE RESTATE EVERY CALL IN ONE STRUCTURE
------------------------------------------
Primary sources are all different — grants.gov, a UN portal, a foundation's own page and a
tender board publish the same facts under different names, in different orders, with
different things missing. `standard_view` maps whichever store we have onto ONE canonical
field set, and `sections` lays that out in the schema's own §4 order (identity, funder,
narrative, eligibility, money, dates, classification, documents, provenance). The result
reads like the published call, but every call reads the same way, which is the only way a
reviewer can compare two of them.

WHAT IS ACTUALLY POPULATED TODAY (measured over 500 catalogue rows)
-------------------------------------------------------------------
The regex/handler stage fills: brief_description (390), raw_text (443, ~3k chars),
deadline + confidence, funding_status, funding_window, opportunity_id,
funding_opportunity_number, geographic scope, language, amount + currency, solicitation /
instrument / opportunity type, extraction confidence and field provenance.

The LLM-SYNTHESIS stage has never run, so these are empty on every row: full_description,
what_is_funded, what_is_not_funded, applicant_fit_profile, project_stages,
eligibility_countries, eligibility_other, attachments, resource_links, funding_tiers,
grantmaking_entity, apply_url, expected_awards, total_program_funding, submission_format,
time_to_award, agency_code, focus_themes, call_domain_areas, expected_award_date
(`eligibility_applicant_types` is partial, 133/500). See core/extract.py, which says so:
"populated by a later shadow-mode pass — left None here".

That is why a "full read" still leans on `brief_description` + the as-published `raw_text`.
The layout below is deliberately the WHOLE schema anyway, so those sections fill in on
their own the day the synthesis pass lands — and `coverage()` reports the gap instead of
hiding it behind a page of blanks.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

KIND_PIPELINE = "pipeline"
KIND_CATALOG = "catalog"

CATALOG_FIELDS = "*"
PIPELINE_FIELDS = "*"


# ---------------------------------------------------------------------------
# blank / value normalisation
# ---------------------------------------------------------------------------
_PLACEHOLDERS = {"nan", "none", "null", "n/a", "na", "not stated", "not specified",
                 "unknown", "not mentioned", "tbd", "tbc", "none stated"}


def _blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    s = str(v).strip()
    # "[]" / "{}" are the SERIALISED empty collections: several of these columns are jsonb
    # and come back as strings, so an empty one reached the page as a literal "[]".
    return s in ("", "[]", "{}") or s.lower() in _PLACEHOLDERS


def display_value(v: Any) -> str:
    """One field's value as text.

    jsonb columns arrive inconsistently — sometimes a real list, sometimes a JSON-encoded
    string, occasionally a list whose single element is itself an encoded list. `_as_list`
    already untangles all three for the Review card, so it is reused here rather than
    letting a raw Python repr (`['Ukraine', 'United States']`) reach the page.
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
    else:
        return str(v).strip()
    try:
        from core.criteria_derive import _as_list
        items = _as_list(items)
    except Exception:
        items = [str(x) for x in items]
    return ", ".join(str(x) for x in items if str(x).strip())


def as_bullets(v: Any) -> list[str]:
    """A bullets field (`what_is_funded` and friends) as a list of lines. Accepts a real
    list, a JSON-encoded list, or newline / semicolon / bullet-delimited prose."""
    if _blank(v):
        return []
    if isinstance(v, (list, tuple, set)):
        raw = [str(x) for x in v]
    else:
        s = str(v).strip()
        if s.startswith("["):
            try:
                from core.criteria_derive import _as_list
                raw = _as_list(s)
            except Exception:
                raw = [s]
        else:
            raw = [s]
    out: list[str] = []
    for chunk in raw:
        for line in str(chunk).replace("\r", "\n").split("\n"):
            for piece in line.split(";"):
                t = piece.strip().lstrip("-•*·").strip()
                if t and not _blank(t):
                    out.append(t)
    return out


# ---------------------------------------------------------------------------
# money — one formatted string, never an amount beside a bare currency code
# ---------------------------------------------------------------------------
# Region prefix + symbol per currency, so a value reads "US $244,000,000" rather than
# "244000000.0" in one row and "USD $" in another. Unlisted codes print the code itself.
_CCY = {"USD": ("US", "$"), "EUR": ("EU", "€"), "GBP": ("GB", "£"),
        "CAD": ("CA", "$"), "AUD": ("AU", "$"), "CHF": ("CH", "CHF "),
        "JPY": ("JP", "¥"), "INR": ("IN", "₹"), "ZAR": ("ZA", "R"),
        "NGN": ("NG", "₦"), "KES": ("KE", "KSh "), "XAF": ("XAF", ""),
        "XOF": ("XOF", ""), "DKK": ("DK", "kr "), "SEK": ("SE", "kr "),
        "NOK": ("NO", "kr ")}


def _amount(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f <= 0:            # NaN or non-positive
        return None
    return f


def currency_code(v: Any) -> str:
    """The ISO code out of a currency value. The column is free text in places, so
    "USD $" / "usd" / "USD - US Dollar" all normalise to "USD"."""
    s = str(v or "").strip()
    if not s:
        return "USD"
    tok = s.replace("-", " ").split()
    return (tok[0].upper() if tok else "USD")[:3] or "USD"


def format_money(value: Any, currency: Any = "USD") -> str:
    """"US $244,000,000" — the amount and its currency as ONE value.

    The two used to be separate rows ("Value 244000000.0" above "Currency USD $"), which
    made the reader do the formatting and the joining. Whole amounts print without
    decimals; a fractional amount keeps two.
    """
    amt = _amount(value)
    if amt is None:
        return ""
    code = currency_code(currency)
    pre, sym = _CCY.get(code, (code, ""))
    num = f"{amt:,.0f}" if float(amt).is_integer() else f"{amt:,.2f}"
    return f"{pre} {sym}{num}" if sym else f"{pre} {num}"


def format_money_range(floor: Any, ceiling: Any, currency: Any = "USD") -> str:
    """"US $15,000 – US $50,000", or a single bound when only one is known."""
    lo, hi = format_money(floor, currency), format_money(ceiling, currency)
    if lo and hi:
        return lo if lo == hi else f"{lo} – {hi}"
    return lo or hi


def usd_equivalent(value: Any, currency: Any) -> str:
    """"≈US $27,000" for a non-USD amount, else "". Best-effort: a missing FX table
    returns "" rather than a wrong number."""
    amt = _amount(value)
    code = currency_code(currency)
    if amt is None or code == "USD":
        return ""
    try:
        from core import dropdowns
        rate = float(dropdowns.usd_rate(currency))
    except Exception:
        return ""
    if not rate or rate == 1.0:
        return ""
    return f"≈US ${amt * rate:,.0f}"


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------
def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_until(deadline: Any, today: date | None = None) -> int | None:
    d = _as_date(deadline)
    if d is None:
        return None
    return (d - (today or date.today())).days


def deadline_status(deadline: Any, funding_status: Any = None,
                    today: date | None = None) -> tuple[str, str]:
    """(text, tone) for the deadline chip. tone ∈ closed | urgent | soon | open | unknown.

    A stated funding_status of Closed wins: the cron flips it when the deadline passes, and
    a funder can close a call early.
    """
    if str(funding_status or "").strip().lower() == "closed":
        return "Closed", "closed"
    d = days_until(deadline, today)
    if d is None:
        return "No deadline stated", "unknown"
    if d < 0:
        return f"Closed {_plural(abs(d), 'day')} ago", "closed"
    if d == 0:
        return "Due today", "urgent"
    left = f"{_plural(d, 'day')} left"
    if d <= 14:
        return left, "urgent"
    if d <= 45:
        return left, "soon"
    return left, "open"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def format_duration(v: Any) -> str:
    """`project_duration` is a count of MONTHS, so a bare "36" tells the reader nothing.
    Free-text durations that already name their unit are passed through untouched."""
    if _blank(v):
        return ""
    s = display_value(v)
    try:
        n = float(s)
    except (TypeError, ValueError):
        return s                        # e.g. "18-24 months", "2 years" — already explicit
    n_int = int(n) if float(n).is_integer() else n
    return _plural(n_int, "month") if isinstance(n_int, int) else f"{n_int} months"


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def normalise_link(v: Any) -> str:
    """The comparison form of a call URL — lowercased, trailing slash dropped."""
    return str(v or "").strip().lower().rstrip("/")


def load(uid: str, *, pipeline_reader=None, catalog_reader=None,
         catalog_by_link_reader=None) -> dict:
    """Resolve `uid` to one opportunity.

    Returns ``{"kind", "row", "extraction"}``:
      kind       — KIND_PIPELINE | KIND_CATALOG | None
      row        — the resolved row
      extraction — the RAW EXTRACTION for it. For a catalogue uid that is the row itself.
                   For a pipeline uid it is looked up by call URL, so a screened row shows
                   the full extraction instead of only the fields matching kept; None when
                   the row predates the extraction store (migrated rows).

    The pipeline is tried FIRST: once a catalogue call has been tracked, the tenant's own
    screened row is the more informative answer for the same uid.

    Readers are injected so this is testable without a database.
    """
    uid = (uid or "").strip()
    if not uid:
        return {"kind": None, "row": None, "extraction": None}
    row = None
    if pipeline_reader is not None:
        try:
            row = pipeline_reader(uid)
        except Exception:
            row = None
    if row:
        ext = None
        link = normalise_link(row.get("opportunity_link"))
        if link and catalog_by_link_reader is not None:
            try:
                ext = catalog_by_link_reader(link)
            except Exception:
                ext = None
        return {"kind": KIND_PIPELINE, "row": row, "extraction": ext}
    if catalog_reader is not None:
        try:
            row = catalog_reader(uid)
        except Exception:
            row = None
        if row:
            return {"kind": KIND_CATALOG, "row": row, "extraction": row}
    return {"kind": None, "row": None, "extraction": None}


def tracked_uid(catalog_row: dict, pipeline_rows: list[dict] | None) -> str | None:
    """The tenant's OWN pipeline uid for this catalogue call, if already tracked.

    Tracking mints a NEW uid (the catalogue uid names a row in another table), so
    /opportunity?uid=<catalogue uid> keeps resolving to the catalogue row afterwards.
    Without this the page would offer "Add to my pipeline" again on the next visit and the
    reviewer would only discover it was already tracked by pressing it.
    """
    link = normalise_link((catalog_row or {}).get("opportunity_url")
                          or (catalog_row or {}).get("apply_url"))
    if not link:
        return None
    for r in pipeline_rows or []:
        if normalise_link(r.get("opportunity_link")) == link:
            return str(r.get("uid") or "").strip() or None
    return None


# ---------------------------------------------------------------------------
# ONE canonical view over either store
# ---------------------------------------------------------------------------
# A screened row keeps the same facts under matching-oriented names. Mapping them onto the
# schema's names is what lets both stores render through one layout.
_PIPELINE_TO_SCHEMA = {
    "opportunity_title": "opportunity_name",
    "opportunity_link": "opportunity_url",
    "funding_agency": "funder_name",
    "call_award_value": "grant_amount",
    "call_submission_deadline": "deadline",
    "focus_theme": "focus_themes",
    "eligibility_specifics": "eligibility_other",
}
# Carried across unchanged when the extraction doesn't have them.
_PIPELINE_KEEP = (
    "currency", "date_posted", "call_geographic_scope", "call_domain_areas",
    "brief_description", "instrument_type", "project_duration", "expected_award_date",
    "funding_window", "funding_status", "source", "uid",
)


def standard_view(kind: str, row: dict, extraction: dict | None = None) -> dict:
    """One dict keyed by the canonical schema field names, whatever store it came from.

    The RAW EXTRACTION wins field by field — it is the fuller, less-processed read of the
    call — and the matching row fills what the extraction lacks (or predates). Blank values
    never overwrite a populated one in either direction.
    """
    out: dict[str, Any] = {}

    def _put(k, v):
        if not _blank(v) and _blank(out.get(k)):
            out[k] = v

    for src in (extraction or {},):
        for k, v in src.items():
            _put(k, v)
    if kind == KIND_PIPELINE or extraction is not row:
        for k, v in (row or {}).items():
            _put(_PIPELINE_TO_SCHEMA.get(k, k), v)
    # RFPIS-specific facts that have no schema slot but a reviewer needs.
    for k in ("applicant_role", "compliance_requirements", "key_risks", "aggregator_url"):
        _put(k, (row or {}).get(k))
    for k in _PIPELINE_KEEP:
        _put(k, (row or {}).get(k))
    return out


# (section title, [(label, field, kind)]) in the order of DATA_SCHEMA_ETL.md §4.
# kind: "text" · "money" · "money_range" · "date" · "list" · "bullets"
_SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Funding", (
        ("Award value", "grant_amount", "money"),
        ("Award range", "_award_range", "money_range"),
        ("Total programme funding", "total_program_funding", "money"),
        ("Expected number of awards", "expected_awards", "text"),
        ("Funding tiers", "funding_tiers", "list"),
    )),
    ("Dates & window", (
        ("Deadline", "deadline", "date"),
        ("Deadline confidence", "deadline_confidence", "text"),
        ("Posted", "date_posted", "date"),
        ("Status", "funding_status", "text"),
        ("Window", "funding_window", "text"),
        ("Expected award date", "expected_award_date", "date"),
        ("Time to award", "time_to_award", "text"),
        ("Project duration", "project_duration", "duration"),
        ("Submission format", "submission_format", "text"),
    )),
    ("Who can apply", (
        ("Applicant types", "eligibility_applicant_types", "list"),
        ("Eligible countries", "eligibility_countries", "list"),
        ("Other requirements", "eligibility_other", "bullets"),
        ("Ideal applicant", "applicant_fit_profile", "text"),
        # "Our role" is NOT here. It is what THIS tenant would be on a bid (prime / sub),
        # which is a decision the tenant took — not something the call says about who may
        # apply. Part 1 restates the CALL, identically for every tenant that opens it, so a
        # tenant-specific fact in a card headed "Who can apply" read as an eligibility rule
        # published by the funder. It lives in the decision aid (§2) instead.
    )),
    ("Classification", (
        ("Sector / focus themes", "focus_themes", "list"),
        ("Programme areas", "call_domain_areas", "list"),
        ("Geographic scope", "call_geographic_scope", "list"),
        ("Solicitation type", "solicitation_type", "text"),
        ("Instrument", "instrument_type", "text"),
        ("Opportunity type", "opportunity_type", "text"),
        ("Project stages", "project_stages", "list"),
        ("Language", "solicitation_language", "text"),
    )),
    ("Funder", (
        ("Funder", "funder_name", "text"),
        ("Grantmaking entity", "grantmaking_entity", "text"),
        ("Agency code", "agency_code", "text"),
    )),
    ("Identity", (
        ("Opportunity ID", "opportunity_id", "text"),
        ("Opportunity number", "funding_opportunity_number", "text"),
        ("RFPIS uid", "uid", "text"),
    )),
    ("Provenance", (
        ("Source", "source", "text"),
        ("Extraction confidence", "extraction_confidence", "text"),
        ("First seen", "scraped_at", "date"),
        ("Last updated", "updated_at", "date"),
    )),
)

# The narrative + bullet fields rendered as prose blocks rather than label/value rows.
# All of these describe the CALL, so they are the same for every tenant that opens the page.
# `key_risks` is deliberately absent — see DECISION_AID_FIELDS.
NARRATIVE_FIELDS = (
    ("Project overview", "full_description"),
    ("What is funded", "what_is_funded"),
    ("What is NOT funded", "what_is_not_funded"),
    ("Compliance & hard gates", "compliance_requirements"),
)

# ---------------------------------------------------------------------------
# THE TENANT-SPECIFIC SPLIT
# ---------------------------------------------------------------------------
# Part 1 of the page is a read of the CALL and must render identically for every tenant —
# that is what makes two calls comparable, and what lets the same page serve a call nobody
# has screened yet. These two fields are the opposite: they are statements about THIS
# tenant against this call.
#
#   applicant_role — prime or sub, a positioning decision the tenant made
#   key_risks      — risks relative to this tenant's profile ("the organization lacks a
#                    presence in the eligible regions"), which is a scoring output, not
#                    something the funder published
#
# Shown in Part 1 they read as facts about the call. They belong with the verdict they
# support, so both moved to the decision aid in §2.
DECISION_AID_FIELDS = (
    ("Our role on a bid", "applicant_role", "text"),
)
DECISION_AID_NARRATIVE = (
    ("Key risks for this entity", "key_risks"),
)


def _render(view: dict, field: str, kind: str) -> str:
    if field == "_award_range":
        return format_money_range(view.get("call_award_floor"),
                                  view.get("call_award_ceiling"), view.get("currency"))
    v = view.get(field)
    if _blank(v):
        return ""
    if kind == "money":
        return format_money(v, view.get("currency"))
    if kind == "date":
        return str(v)[:10]
    if kind == "duration":
        return format_duration(v)
    if kind in ("list", "bullets"):
        return display_value(v)
    return display_value(v)


def sections(view: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section title, [(label, formatted value)])] in schema order, blanks dropped.

    An empty section is dropped whole: a card of em dashes tells a reviewer nothing, and
    the point of this page is the detail that IS there.
    """
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for title, fields in _SECTIONS:
        rows = [(label, _render(view, f, k)) for label, f, k in fields]
        rows = [(lb, v) for lb, v in rows if v]
        if rows:
            out.append((title, rows))
    return out


def narrative_blocks(view: dict) -> list[tuple[str, list[str]]]:
    """[(heading, [lines])] for the prose sections — bullets split into lines."""
    return _blocks(view, NARRATIVE_FIELDS)


def _blocks(view: dict, spec) -> list[tuple[str, list[str]]]:
    out = []
    for heading, field in spec:
        lines = as_bullets(view.get(field))
        if lines:
            out.append((heading, lines))
    return out


def decision_aid(view: dict) -> tuple[list[tuple[str, str]], list[tuple[str, list[str]]]]:
    """The TENANT-SPECIFIC material for §2: ``(rows, narrative blocks)``.

    Kept out of Part 1 so that part is a read of the call alone. Empty when the page is
    showing a catalogue call nobody has screened — there is no tenant view of it yet.
    """
    rows = [(label, _render(view, f, k)) for label, f, k in DECISION_AID_FIELDS]
    return [(lb, v) for lb, v in rows if v], _blocks(view, DECISION_AID_NARRATIVE)


# ---------------------------------------------------------------------------
# is this actually in the tenant's pipeline?
# ---------------------------------------------------------------------------
# A row exists in `rfp_submissions` from the moment the scan touches it, long before anyone
# decides anything: 180 of 254 live rows carry NO decision, and 160 are marked not eligible.
# Badging all of them "In your pipeline" told a reviewer that three quarters of the store
# was work in progress. A pipeline is the three real dispositions and nothing else.
PIPELINE_DECISIONS = ("Proceed", "Park", "Decline")


def pipeline_decision(kind: str, row: dict) -> str | None:
    """The tenant's recorded decision when this call is genuinely IN their pipeline
    (Proceed / Park / Decline), else None — a scanned-but-undecided row, a row rejected at
    screening, and a catalogue call all return None."""
    if kind != KIND_PIPELINE:
        return None
    d = str((row or {}).get("decision") or "").strip()
    for known in PIPELINE_DECISIONS:
        if d.lower() == known.lower():
            return known
    return None


def in_pipeline(kind: str, row: dict) -> bool:
    return pipeline_decision(kind, row) is not None


def header_reference(view: dict) -> str:
    """The identifier shown beside the funder under the title: the FUNDER'S OWN id for the
    call, so it can be quoted back to them or searched on their portal. Blank when the call
    published none — the RFPIS uid is an internal key and belongs in Identity, where it
    already is."""
    v = (view or {}).get("opportunity_id")
    return "" if _blank(v) else display_value(v)


def summary_of(view: dict) -> str:
    """The 2–4 sentence house-style summary (schema §4.3 `brief_description`)."""
    v = view.get("brief_description")
    return "" if _blank(v) else str(v).strip()


def as_published(view: dict) -> str:
    """`raw_text` — the audit copy of the call as the primary source published it.

    Shown collapsed, and it matters more than it looks: with the LLM-synthesis stage of the
    schema still unpopulated, this is the only place a reviewer can read the WHOLE call
    without leaving the app. ~3,000 characters on the rows that have it (443 of 500).
    """
    v = view.get("raw_text")
    return "" if _blank(v) else str(v).strip()


def documents(view: dict) -> list[tuple[str, str, str]]:
    """[(label, url, doc_type)] over `attachments` + `resource_links` (schema §4.8).

    Both are JSONB arrays of {url, label, doc_type|type}. They also arrive as plain URL
    strings from older rows, so a bare string is accepted and labelled by its own URL.
    """
    out: list[tuple[str, str, str]] = []
    for field, default_kind in (("attachments", "attachment"),
                                ("resource_links", "resource")):
        raw = view.get(field)
        if _blank(raw):
            continue
        if isinstance(raw, str):
            try:
                import json
                raw = json.loads(raw)
            except Exception:
                raw = [raw]
        if isinstance(raw, dict):
            raw = [raw]
        for item in (raw if isinstance(raw, (list, tuple)) else []):
            if isinstance(item, dict):
                url = str(item.get("url") or item.get("href") or "").strip()
                label = str(item.get("label") or item.get("name") or "").strip()
                kind = str(item.get("doc_type") or item.get("type")
                           or default_kind).strip()
            else:
                url, label, kind = str(item).strip(), "", default_kind
            if not url:
                continue
            out.append((label or url, url, kind))
    return out


# Fields the schema defines for a full read. `coverage` reports how much of it this
# opportunity actually carries, so a thin page is visibly a DATA gap and not a layout that
# forgot to show something.
_COVERAGE_FIELDS = tuple(
    f for _t, fields in _SECTIONS for _lb, f, _k in fields
    if f not in ("_award_range", "uid", "source", "scraped_at", "updated_at",
                 "extraction_confidence")
) + ("brief_description", "full_description", "what_is_funded", "what_is_not_funded",
     "attachments", "resource_links", "raw_text")


def coverage(view: dict) -> tuple[int, int, list[str]]:
    """(filled, total, missing field names) over the schema's full-read fields."""
    missing = [f for f in _COVERAGE_FIELDS if _blank(view.get(f))]
    total = len(_COVERAGE_FIELDS)
    return total - len(missing), total, missing


def title_of(view: dict) -> str:
    v = (view or {}).get("opportunity_name")
    return str(v).strip() if not _blank(v) else "(untitled opportunity)"


def call_url(view: dict) -> str:
    return str((view or {}).get("opportunity_url") or "").strip()


def apply_url(view: dict) -> str:
    """The actual "Apply" link when the extraction found one, else "" — never the listing
    page, so an Apply button never quietly sends someone back to the summary."""
    v = (view or {}).get("apply_url")
    return "" if _blank(v) else str(v).strip()


def is_screened(kind: str, row: dict) -> bool:
    """Has this been through the scoring pipeline for this tenant? Only a pipeline row
    can have been, and only if it carries a score."""
    if kind != KIND_PIPELINE:
        return False
    return not _blank((row or {}).get("alignment_score"))


def to_candidate(row: dict) -> dict:
    """A catalogue row → the candidate dict `core.found_loader.load_candidate` scores.

    Maps the catalogue's column names onto the pipeline's and carries the extraction the
    crawl already paid for, so tracking does not throw away geography, domains or duration
    and re-derive them from a title.
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
