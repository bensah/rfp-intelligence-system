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
shows the raw extraction too rather than only the fields matching kept. 192 of 257 live
pipeline rows are reachable this way; the remainder predate the extraction store.

That join was BROKEN until now, and it is most of why a screened call looked empty: the key
is lowercased by `normalise_link` while the column stores the URL as published, and the
lookup compared the two with `=`. Only 58 of 257 rows were finding their extraction. See
`link_query_patterns`.

WHY WE RESTATE EVERY CALL IN ONE STRUCTURE
------------------------------------------
Primary sources are all different — grants.gov, a UN portal, a foundation's own page and a
tender board publish the same facts under different names, in different orders, with
different things missing. `standard_view` maps whichever store we have onto ONE canonical
field set, and `sections` lays that out. The result reads like the published call, but every
call reads the same way, which is the only way a reviewer can compare two of them.

The layout is ordered by the QUESTIONS a reviewer asks — how much, by when, can we apply, is
it our kind of work, how do we submit — not by the schema's storage order, and each fact
appears exactly ONCE (see the note above `_SECTIONS`). Internal bookkeeping is in
`technical_sections`, for a super_user only: a reviewer does not act on a crawl timestamp.

WHY `full_description` IS EMPTY ON EVERY ROW
-------------------------------------------
It has NO WRITER. The name occurs in exactly two places in the codebase — the column
allow-list in `core/extracted_store.py`, and the read here. §4.3 specifies it (150–300 words,
original prose) and `core/extract.py` says the narrative fields are "populated by a later
shadow-mode pass — left None here", but the pass that was eventually built
(`core.llm_synthesis.synthesize_store`) produces `brief_description` and does not emit
`full_description` at all. So this is not an extraction that fails; it is a field nobody ever
fills. Same story for what_is_funded / what_is_not_funded / eligibility_countries /
eligibility_other / applicant_fit_profile / project_stages / attachments / resource_links.

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

import re
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


def usd_reference(value: Any, currency: Any) -> str:
    """The USD figure for an award, ALWAYS — "≈US $38,108,565" for a converted amount, and
    "=US $1,500,000" when the call is already in USD.

    Two reasons it is unconditional. The award card was a line shorter than the deadline and
    duration cards whenever the call was in USD, so the row of three sat unevenly. And a
    reader comparing two calls wants the dollar figure in the same place on both, rather than
    having to notice that its absence means "this one already was in dollars".
    """
    amt = _amount(value)
    if amt is None:
        return ""
    if currency_code(currency) == "USD":
        return f"=US ${amt:,.0f}"
    return usd_equivalent(value, currency)


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


def link_query_patterns(link: str) -> tuple[str, ...]:
    """Case-insensitive LIKE patterns that match a stored `opportunity_url` equal to `link`.

    THE BUG THIS EXISTS FOR. A screened row is joined back to its extraction by call URL.
    The join key is `normalise_link`, which LOWERCASES — but the lookup compared it with `=`
    against a column that stores the URL as published, and half of those carry uppercase
    (344 of 686 rows; a topic code like `PROG-…-RIA-03` sits right in the path). So the
    equality could not hold: 134 of 257 pipeline rows found no extraction and rendered as
    though none existed, which is most of why a screened call looked empty. Only 58 rows
    were joining, against 192 that were actually reachable.

    `ilike` with no wildcards is case-insensitive equality, so that is what these patterns
    are for. `%` `_` and `\\` ARE wildcards to LIKE and appear in real URLs (a query string,
    a slug), so they are escaped — an unescaped `_` would quietly match a different call.
    Callers must still confirm `normalise_link(row) == link`: escaping makes over-matching
    unlikely, not impossible, and a wrong extraction is worse than none.
    """
    link = str(link or "").strip()
    if not link:
        return ()
    esc = link.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return (esc, esc + "/")          # the stored URL may keep a trailing slash


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
# Rows whose fields were typed by a person rather than read off the call.
_HAND_ENTERED = frozenset({"migration", "manual", "form"})

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

    # GEOGRAPHY MUST BE THE CALL'S OWN. A migrated or hand-entered row carries the countries
    # the SUBMITTER had in mind, not the scope the funder published: 34 of 63 migrated rows
    # and both manual rows name one of the tenant's own countries in this column, against 3
    # of 192 auto-scanned ones. Displaying that as "Geographic scope" told a reviewer the
    # funder had restricted the call to their countries when it may well have said "Global".
    # With no extraction to defer to, the honest move is to withhold it from the call view and
    # keep it as what it is — the submitter's note, for the decision aid.
    if kind == KIND_PIPELINE and not extraction             and str((row or {}).get("source") or "").strip().lower() in _HAND_ENTERED:
        if not _blank(out.get("call_geographic_scope")):
            out["_submitter_geographic_scope"] = out.pop("call_geographic_scope")
    return out


# (section title, [(label, field, kind)]).
# kind: "text" · "money" · "money_range" · "date" · "list" · "bullets" · "duration"
#
# WHAT IS AND ISN'T HERE
# ---------------------
# The page is a reviewer's read of the call, so the layout is organised by the QUESTIONS a
# reviewer asks — how much, by when, can we apply, is it our kind of work, how do we submit
# — rather than by the storage order of the schema.
#
# Nothing is shown twice. Three facts are owned by the header and the glance metrics, and
# are therefore absent from the cards below:
#
#   funder_name / grantmaking_entity  the header line under the title
#   opportunity_id                    the header reference, beside the funder
#   solicitation_type                 appended to the title ("… : Request for Proposals")
#   grant_amount / deadline /         the three glance metrics, which state them far more
#   project_duration                  prominently than a table row could
#
# Anything a reviewer does not act on — internal keys, per-field confidence, crawl
# timestamps — is in _TECHNICAL_SECTIONS and shown to a super_user only.
_SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Funding & awards", (
        ("Award range (per award)", "_award_range", "money_range"),
        ("Total programme funding", "total_program_funding", "money"),
        ("Expected number of awards", "expected_awards", "text"),
        ("Funding tiers", "funding_tiers", "list"),
    )),
    ("Timeline", (
        ("Posted", "date_posted", "date"),
        ("Status", "funding_status", "text"),
        ("Window", "funding_window", "text"),
        ("Expected award date", "expected_award_date", "date"),
        ("Time to award", "time_to_award", "text"),
    )),
    ("Eligibility requirements", (
        # WHAT THE CALL ITSELF PUBLISHES about who qualifies — nothing derived from the
        # donor profile and nothing from our own screening criteria. A reviewer uses this to
        # decide whether they qualify at all, so a condition we inferred rather than read
        # would be actively misleading here. Our criteria live in the decision aid (§2).
        ("Institution types accepted", "eligibility_applicant_types", "list"),
        ("Eligible countries (applicants)", "eligibility_countries", "list"),
        ("Other conditions", "eligibility_other", "bullets"),
        # compliance_requirements is NOT a row here. It is long prose and it already renders
        # as its own block below this section; carrying it in both places printed the same
        # text twice, once squeezed into a table cell.
    )),
    ("Who can apply", (
        ("Ideal applicant", "applicant_fit_profile", "text"),
        # "Our role" is NOT here. It is what THIS tenant would be on a bid (prime / sub),
        # which is a decision the tenant took — not something the call says about who may
        # apply. Part 1 restates the CALL, identically for every tenant that opens it, so a
        # tenant-specific fact in a card headed "Who can apply" read as an eligibility rule
        # published by the funder. It lives in the decision aid (§2) instead.
    )),
    ("Scope & focus", (
        ("Geographic scope", "call_geographic_scope", "list"),
        ("Sector", "focus_themes", "list"),
        ("Programme areas", "call_domain_areas", "list"),
        ("Project stages", "project_stages", "list"),
    )),
    ("Type of opportunity", (
        # ONE reconciled line, not two labels the reader has to reconcile themselves.
        # "Instrument: Contract" sitting above "Opportunity type: grant" read as the
        # extraction contradicting itself, when the two answer different questions: what
        # pursuing this IS before the award, and the vehicle it becomes after one. A grant
        # is contracted once awarded, so that pair is ordinary — 30 live rows are it.
        # `core.award_type` also fills a missing half from the half present (187 rows) and
        # flags only the combinations that are genuinely hard to explain (7 rows).
        ("Award type", "_award_type", "text"),
    )),
    ("How to apply", (
        ("Submission format", "submission_format", "text"),
        # A funder often REQUIRES the application in a named language — usually the one the
        # call is published in, sometimes stated separately. It is a requirement, not a
        # property of our record, so it is labelled as the applicant's obligation.
        ("Application language", "solicitation_language", "text"),
        ("Application steps", "application_checklist", "bullets"),
        # Only when the funder publishes a SECOND reference distinct from the header id.
        ("Opportunity number", "_second_reference", "text"),
    )),
)

# Shown to a super_user only. None of it changes a bid decision: it is the internal key, the
# crawl's own bookkeeping, and the per-field confidence the training loop uses. On the page
# it competed for attention with the call's actual terms.
_TECHNICAL_SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Record & provenance (super_user)", (
        ("RFPIS uid", "uid", "text"),
        ("Opportunity ID", "opportunity_id", "text"),
        ("Opportunity number", "funding_opportunity_number", "text"),
        ("Agency code", "agency_code", "text"),
        ("Source", "source", "text"),
        ("Source uid", "source_uid", "text"),
        ("Solicitation type (raw)", "solicitation_type", "text"),
        ("Extraction confidence", "extraction_confidence", "text"),
        ("Deadline confidence", "deadline_confidence", "text"),
        ("First seen", "scraped_at", "date"),
        ("Last updated", "updated_at", "date"),
    )),
)

# ---------------------------------------------------------------------------
# solicitation type, spelled out beside the title
# ---------------------------------------------------------------------------
# The column stores the trade abbreviation (NOFO 135 rows, Tender 117, CFP 74, RFP 28 …) and
# is blank on 244 of 686. A reviewer opening a page should not have to know that CfCN means
# a concept-note round, so the abbreviation is expanded, and a blank one falls back to the
# broader `opportunity_type` — which is populated on 99% of rows.
_SOLICITATION_LABELS = {
    "NOFO": "Notice of Funding Opportunity",
    "RFP": "Request for Proposals",
    "RFA": "Request for Applications",
    "RFQ": "Request for Quotation",
    "RFI": "Request for Information",
    "CFP": "Call for Proposals",
    "CFA": "Call for Applications",
    "CFCN": "Call for Concept Notes",
    "EOI": "Expression of Interest",
    "LOI": "Letter of Intent",
    "ITB": "Invitation to Bid",
    "TENDER": "Tender",
    "BID": "Bid",
    "PRIZE": "Prize",
    "CHALLENGE": "Challenge",
    "GRANT": "Grant",
}


def solicitation_label(view: dict) -> str:
    """"Request for Proposals" — the kind of solicitation, spelled out, or "" if unknown."""
    raw = (view or {}).get("solicitation_type")
    if not _blank(raw):
        s = display_value(raw).strip()
        hit = _SOLICITATION_LABELS.get(s.replace(" ", "").replace("-", "").upper())
        if hit:
            return hit
        return s if len(s) > 5 else s.upper()   # unknown long form kept; short = acronym
    # Fall back to the classifier's broader type ("Grant/funding call", "Procurement").
    v = (view or {}).get("opportunity_type")
    if _blank(v):
        return ""
    s = display_value(v).strip()
    return s[:1].upper() + s[1:]


def title_line(view: dict) -> tuple[str, str]:
    """``(title, solicitation label)``.

    The label used to be appended to the title after a colon, which produced "DIV Fund –
    Request for Proposals: Request for Proposals" whenever the funder had already named the
    kind in their own title — which they usually have. The label now goes in the chip row
    instead, and is returned as "" when the title already says it.
    """
    title = title_of(view)
    label = solicitation_label(view)
    if label and _title_already_names(title, label):
        return title, ""
    return title, label


def _title_already_names(title: str, label: str) -> bool:
    """Does the title already say what kind of thing this is? Compared on words rather than
    substrings, so "Request for Proposals" matches "Request for Proposal" and "RFP"."""
    t = " ".join(re.findall(r"[a-z]+", (title or "").lower()))
    words = [w.rstrip("s") for w in (label or "").lower().split() if len(w) > 2]
    if not words:
        return False
    if all(w in t for w in words):
        return True
    acronym = "".join(w[0] for w in (label or "").split() if w[:1].isalpha())
    return len(acronym) >= 3 and acronym.lower() in t.split()

# The narrative + bullet fields rendered as prose blocks rather than label/value rows.
# All of these describe the CALL, so they are the same for every tenant that opens the page.
# `key_risks` is deliberately absent — see DECISION_AID_FIELDS.
NARRATIVE_FIELDS = (
    ("Project overview", "full_description"),
    ("How to apply", "how_to_apply"),
    ("What is funded", "what_is_funded"),
    ("What is NOT funded", "what_is_not_funded"),
    # "Compliance requirements" — the phrase the call itself uses. "Hard gates" is our
    # screening vocabulary and meant nothing to a reader looking at the funder's terms.
    ("Compliance requirements", "compliance_requirements"),
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
# Nothing here any more. "Our role on a bid" was the only row, and a one-row card carrying a
# single word ("Prime") is more furniture than information — the same fact is stated by the
# criteria below, which is where a reviewer is already looking. The field is still resolved on
# the view for anything else that wants it; it just is not given a card of its own.
DECISION_AID_FIELDS: tuple[tuple[str, str, str], ...] = ()
DECISION_AID_NARRATIVE = (
    ("Key risks for this entity", "key_risks"),
)


def award_pairing(view: dict) -> dict:
    """The two award axes reconciled into one answer — see `core.award_type`.

    Imported lazily so this module keeps rendering if the type vocabularies move; a page
    that loses one card row is better than a page that will not load.
    """
    try:
        from core import award_type
        return award_type.pairing(view.get("opportunity_type"),
                                  view.get("instrument_type"))
    except Exception:
        return {"text": "", "verdict": "unknown", "note": "", "inferred": []}


def _second_reference(view: dict) -> str:
    """`funding_opportunity_number` only when it is genuinely a SECOND reference.

    Many sources put the same string in both columns, which printed the identifier twice —
    once in the header, once as "Opportunity number" — and invited the reader to look for a
    difference that wasn't there.
    """
    num = view.get("funding_opportunity_number")
    if _blank(num):
        return ""
    num_s = display_value(num).strip()
    oid = "" if _blank(view.get("opportunity_id")) else display_value(
        view.get("opportunity_id")).strip()
    return "" if num_s.lower() == oid.lower() else num_s


def _render(view: dict, field: str, kind: str) -> str:
    if field == "_award_range":
        rng = format_money_range(view.get("call_award_floor"),
                                 view.get("call_award_ceiling"), view.get("currency"))
        # The glance metric already states a single award value; a "range" that repeats it
        # is noise, so only a real span shows here.
        return "" if rng == format_money(view.get("grant_amount"),
                                         view.get("currency")) else rng
    if field == "_second_reference":
        return _second_reference(view)
    if field == "_award_type":
        return award_pairing(view).get("text") or ""
    v = view.get(field)
    if _blank(v):
        return ""
    if kind == "money":
        return format_money(v, view.get("currency"))
    if kind == "date":
        return str(v)[:10]
    if kind == "duration":
        return format_duration(v)
    if kind == "sentence":
        s = display_value(v)
        return s[:1].upper() + s[1:] if s else s
    if kind in ("list", "bullets"):
        return display_value(v)
    return display_value(v)


# The only rows that vanish rather than showing a dash. Both are layout pseudo-fields whose
# emptiness means "this repeats something already on the page", not "this is unknown":
#   _award_range       an award range identical to the single award value in the metrics
#   _second_reference  a funder reference identical to the one beside the title
# `_award_type` is deliberately NOT here: it stands for two real schema columns, so when both
# are missing that IS a gap and must read as one.
_SUPPRESSED_WHEN_REDUNDANT = frozenset({"_award_range", "_second_reference"})


def _lay_out(view: dict, spec, *, show_missing: bool = False
             ) -> list[tuple[str, list[tuple[str, str]]]]:
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for title, fields in spec:
        rows: list[tuple[str, str]] = []
        for label, field, kind in fields:
            value = _render(view, field, kind)
            if value:
                rows.append((label, value))
            elif show_missing and field not in _SUPPRESSED_WHEN_REDUNDANT:
                # A field with nothing in it — say so. Only the two rows below are exempt,
                # and they are exempt because they were suppressed as DUPLICATES rather than
                # being absent; a dash there would invent a gap.
                rows.append((label, MISSING))
        if rows:
            out.append((title, rows))
    return out


MISSING = "—"


def sections(view: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section title, [(label, value or "—")])] — the reviewer's read of the call.

    THE WHOLE SKELETON IS ALWAYS RENDERED, including fields this call has nothing for.
    Blank rows used to be dropped and an empty section dropped whole, which read better on
    one call but was wrong across a set of them: the page changed SHAPE from call to call, so
    a reader could not tell "this funder did not state a project duration" from "this app does
    not track project duration", and could not compare two calls by eye because the rows were
    in different places. A dash is a statement — the field is tracked, this call is silent on
    it — and that is worth more than a shorter card. (Owner's call, 2026-08-11.)

    The exception is a row suppressed for REDUNDANCY rather than for absence: the layout
    pseudo-fields (`_award_range` when it merely repeats the single award value,
    `_second_reference` when it repeats the header identifier) are dropped entirely, because
    those are duplicates rather than gaps and printing "—" for them would invent a gap.
    """
    return _lay_out(view, _SECTIONS, show_missing=True)


def technical_sections(view: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """The internal bookkeeping — super_user only. Same shape as `sections`."""
    return _lay_out(view, _TECHNICAL_SECTIONS)


def narrative_blocks(view: dict) -> list[tuple[str, list[str]]]:
    """[(heading, [lines])] for the prose sections — bullets split into lines.

    Only the sections that HAVE content. `narrative_sections` is what the page renders.
    """
    return _blocks(view, NARRATIVE_FIELDS)


# What a narrative heading says when the call has nothing under it. Distinct from a plain
# dash because these are the LLM-synthesis fields: "not extracted" is the honest word, and it
# tells the reader the gap is ours rather than the funder's silence.
# One mark for "nothing here", used by the cards AND the prose blocks. A sentence explaining
# our pipeline ("Not extracted for this call yet") put our internal state in front of a
# reviewer who only wants to know whether the funder said anything.
NOT_EXTRACTED = MISSING


# ---------------------------------------------------------------------------
# the reading order of the page
# ---------------------------------------------------------------------------
# A reviewer should be walked through the opportunity from the top: what it IS, then how much
# and by when, then whether they qualify, then what it pays for, then how to submit. The prose
# and the cards therefore have to INTERLEAVE — the narrative blocks used to be rendered as one
# run before every card, so "What is funded" arrived long before "Who can apply" and the page
# read as two unrelated halves. Keeping the order here rather than in the page keeps it
# testable.
# The overview sits BELOW the headline metrics, not above them (owner reverted this on
# 2026-08-11 having seen it in place): the three numbers are the fastest read on the page, and
# a 500-word summary above them buries what a reviewer looks at first.
#
# It is also a different KIND of text from `brief_description`. This is the publisher's own
# account of what they aim to fund — purpose, objectives, scope, focus areas — kept close to
# their wording rather than rewritten, and capped so a long one does not swallow the page. See
# `overview_is_truncated`, which is what puts a "Learn more" link on the end.
OVERVIEW_FIELDS = (
    ("Project overview", "full_description"),
)
OVERVIEW_MAX_CHARS = 3500          # ~500 words


def overview_is_truncated(view: dict) -> bool:
    """True when `full_description` was cut to fit, so the page can offer the source."""
    v = view.get("full_description")
    return (not _blank(v)) and len(str(v).strip()) > OVERVIEW_MAX_CHARS


def overview_text(view: dict) -> str:
    """The publisher's summary, clipped at a sentence boundary near the cap."""
    v = view.get("full_description")
    if _blank(v):
        return ""
    t = str(v).strip()
    if len(t) <= OVERVIEW_MAX_CHARS:
        return t
    cut = t[:OVERVIEW_MAX_CHARS]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:stop + 1] if stop > OVERVIEW_MAX_CHARS // 2 else cut).rstrip() + " …"
# Prose that belongs INSIDE a subsection, under its fact rows and with no heading of its own.
# "How to apply" followed by "How to apply in detail" was two headings for one subject; the
# narrative is simply the detail of the thing above it.
INLINE_PROSE = {
    "How to apply": ("how_to_apply",),
    "Eligibility requirements": ("compliance_requirements",),
}

# Prose that gets its own full-width row, because it is long enough to deserve the measure.
# (heading, field) pairs; a row holding two of them renders them side by side.
PROSE_ROWS = (
    (("What is funded", "what_is_funded"), ("What is NOT funded", "what_is_not_funded")),
)

# THE VISUAL ROWS OF SECTION 1. Stated explicitly because the previous approach — stream the
# sections into a two-column run and reset the run whenever prose appeared — left holes all
# down the page: a card on the left with nothing beside it, then a heading, then another lone
# card. Pairing is a layout decision, so it is made here where it can be read and tested.
# Only these render with card chrome. Seven cards turned the page into a wall of boxes, and a
# box is only worth it for a tight column of numbers and dates a reader scans rather than
# reads. Everything else reads better as labelled lines in open text.
AS_CARDS = frozenset({"Funding & awards", "Timeline", "Type of opportunity",
                      "Eligibility requirements", "Scope & focus"})

# THE LAYOUT, as rows of COLUMN STACKS. A row is one or more columns; a column is a stack of
# section titles rendered one under the other.
#
# Rows alone were not enough. "Funding & awards" (3 rows) beside "Timeline" (5 rows) leaves
# dead space under the shorter one, and the next row starts below BOTH — so the page grew a
# ladder of holes. Stacking lets the short card be followed immediately by the next one in the
# same column, which is what closes the gap.
LAYOUT: tuple[tuple[tuple[str, ...], ...], ...] = (
    # left column stacks under itself; right column does the same, so the two even out
    (("Funding & awards", "Eligibility requirements"),
     ("Timeline", "Type of opportunity")),
    (("Who can apply",), ("How to apply",)),
    (("Scope & focus",),),                 # one column = full width
)


def overview_blocks(view: dict) -> list[tuple[str, list[str], bool]]:
    """The prose that belongs above the headline metrics — what this opportunity IS."""
    return _named_blocks(view, OVERVIEW_FIELDS)


def page_rows(view: dict) -> list[list[list[dict]]]:
    """Section 1 as ``[row][column][block]``.

    A row is rendered as N side-by-side columns; each column stacks its blocks vertically, so
    a short card is followed immediately by the next one rather than leaving dead space until
    the tallest block in the row ends.

    A block is either a fact block —
        ``{"kind": "cards"|"facts", "title", "rows": [(label, value)], "prose": [lines]}``
    — where `prose` is the subsection's own detail, shown under its rows without a second
    heading; or a prose block —
        ``{"kind": "prose", "title", "lines", "missing": bool}``.

    Sections absent from LAYOUT are appended in their own row, so adding one can never make it
    silently vanish.
    """
    by_title = {t: rows for t, rows in sections(view)}

    def _block(title: str) -> dict:
        prose: list[str] = []
        for field in INLINE_PROSE.get(title, ()):
            prose.extend(as_bullets(view.get(field)))
        return {"kind": "cards" if title in AS_CARDS else "facts", "title": title,
                "rows": by_title[title], "prose": prose}

    out: list[list[list[dict]]] = []
    for row in LAYOUT:
        cols = [[_block(t) for t in stack if t in by_title] for stack in row]
        cols = [c for c in cols if c]
        if cols:
            out.append(cols)
    planned = {t for row in LAYOUT for stack in row for t in stack}
    extra = [t for t in by_title if t not in planned]
    if extra:
        out.append([[_block(t)] for t in extra])
    for prose_row in PROSE_ROWS:
        cols = []
        for heading, field in prose_row:
            lines = as_bullets(view.get(field))
            cols.append([{"kind": "prose", "title": heading,
                          "lines": lines or [MISSING], "missing": not lines}])
        out.append(cols)
    return out


def _named_blocks(view: dict, spec) -> list[tuple[str, list[str], bool]]:
    out = []
    for heading, field in spec:
        lines = as_bullets(view.get(field))
        out.append((heading, lines or [NOT_EXTRACTED], not lines))
    return out


def narrative_sections(view: dict) -> list[tuple[str, list[str], bool]]:
    """``[(heading, lines, is_missing)]`` for EVERY narrative section in the schema.

    Same reasoning as `sections`: the shape of the page stays constant across calls, so a
    reader can see that the app tracks "What is NOT funded" and that this call is silent on
    it, rather than wondering whether the section exists at all.
    """
    out: list[tuple[str, list[str], bool]] = []
    for heading, field in NARRATIVE_FIELDS:
        lines = as_bullets(view.get(field))
        out.append((heading, lines or [NOT_EXTRACTED], not lines))
    return out


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

    SUPER_USER ONLY, and no longer part of the reviewer's page. The point of this app is that
    every call reads the same way; dropping the primary source's own text into the page put a
    different publisher's structure on screen beside ours and undid that. It stays available
    for audit — checking what the extraction had to work with is exactly a development and
    validation task — but a reviewer reads OUR schema. (Owner's call, 2026-08-11.)

    Now that the synthesis writer fills the narrative fields, the reason it was in the user
    view — being the only place to read the whole call — has gone.
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


# Every field of DATA_SCHEMA_ETL.md §4 that carries CALL CONTENT — what the extraction is
# supposed to come back with. Stated explicitly rather than derived from the layout above:
# coverage measures EXTRACTION, and while it was derived from the sections, rearranging a
# card silently moved the score. Excluded are the columns that are system-set and therefore
# always populated (uid, source, content_hash, timestamps) or derived rather than extracted
# (funding_status, the *_confidence pair, field_provenance).
_COVERAGE_FIELDS = (
    # §4.1 identity & links
    "opportunity_name", "opportunity_id", "opportunity_url", "apply_url",
    "funding_opportunity_number",
    # §4.2 funder
    "funder_name", "agency_code", "grantmaking_entity",
    # §4.3 narrative
    "brief_description", "full_description", "applicant_fit_profile", "project_stages",
    # §4.4 eligibility
    "what_is_funded", "what_is_not_funded", "eligibility_applicant_types",
    "eligibility_countries", "eligibility_other",
    # §4.5 money
    "grant_amount", "currency", "call_award_floor", "call_award_ceiling",
    "total_program_funding", "expected_awards", "funding_tiers",
    # §4.6 dates & window
    "date_posted", "deadline", "funding_window", "expected_award_date", "time_to_award",
    "project_duration", "submission_format",
    # §4.7 classification
    "solicitation_type", "instrument_type", "opportunity_type", "focus_themes",
    "call_domain_areas", "call_geographic_scope", "solicitation_language",
    # §4.8 documents
    "attachments", "resource_links",
    # §4.9 the audit copy — the fallback full read
    "raw_text",
)


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
    """Where to go to apply — the extracted apply link, FALLING BACK to the call page.

    `apply_url` is specified as required but is populated on no row at all: the button is a
    different shape on every portal (a form POST, a JS modal, a login wall), so pinning it
    reliably per source is its own project. Returning "" meant the page offered no way to
    act at all, which is worse than sending someone to the call page — where the real button
    is, one click further on. The caller still distinguishes the two: an "Apply" link is only
    rendered separately when it differs from the call URL.
    """
    v = (view or {}).get("apply_url")
    return call_url(view) if _blank(v) else str(v).strip()


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
