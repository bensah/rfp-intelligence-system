"""Duplicate detection.

Two RFPs are flagged as duplicates if ANY of:
  1. Identical `opportunity_id` (case-insensitive, whitespace-trimmed) —
     STRONGEST signal: catches the cross-source case where the same RFP
     was both manually imported from Excel (source='migration') AND later
     re-discovered by the scanner (source='auto'). The donor's own RFP
     number (Grants.gov's "Funding Opportunity Number" like
     HT942526PRMRPDA, EC's topic identifier, etc.) is globally unique
     within that portal, so equality here is dispositive.
  2. Identical `opportunity_link` (case/space-normalised, query/fragment stripped).
  3. Title similarity >= TITLE_THRESHOLD via difflib.SequenceMatcher on a
     normalised form (lowercased, punctuation stripped, collapsed whitespace).
  4. Same (funding_agency, submission_deadline, estimated_value) tuple,
     when all three are non-null.

The order matters — opportunity_id beats title similarity, which beats
the funder/deadline/value triple. Returns the list of existing UIDs that
match the candidate. The first match (oldest by submitted_at) is treated
as the canonical record; later ones are marked `is_duplicate=True` with
`duplicate_of_uid` pointing to it.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from db.supabase_client import get_client

TITLE_THRESHOLD = 0.90


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")

# Leading solicitation-type phrase ("Call for letters of interest: …", "Request for
# proposals — …", "EOI: …"). Stripped for MATCHING only (the stored opportunity_title
# keeps the full original) so two sources that wrap the same call differently
# ("Call for letters of interest: Addressing …" vs "Addressing … (Letters of …)") line up.
_SOLICIT_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"call\s+for\s+(?:letters?\s+of\s+interest|expressions?\s+of\s+interest|proposals?|"
    r"applications?|concept\s+notes?|tenders?)"
    r"|requests?\s+for\s+(?:proposals?|applications?|expressions?\s+of\s+interest|"
    r"information|quotations?)"
    r"|expressions?\s+of\s+interest|letters?\s+of\s+interest"
    r"|notice\s+of\s+funding\s+opportunity|funding\s+opportunity\s+announcement"
    r"|invitation\s+to\s+(?:tender|bid)"
    r"|eoi|loi|nofo|rfp|rfa|rfq|cfp|cfa"
    r")\b\s*[:\-–—]*\s*",
    re.IGNORECASE,
)


def _strip_solicit_prefix(title: str) -> str:
    out = _SOLICIT_PREFIX_RE.sub("", title, count=1)
    # Don't strip a title down to near-nothing (e.g. a bare "Call for proposals").
    return out if len(out.strip()) >= 8 else title


def _norm_title(s: str | None) -> str:
    if not s:
        return ""
    s = _strip_solicit_prefix(s).lower()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


# Funder equivalence — collapse an acronym with its full name ("IDRC" ⇄ "International
# Development Research Centre") and drop a leading "ACRONYM - Donor Name" migration
# prefix, so an Excel-imported funder and a re-scanned full-name funder line up. Used
# only inside the deadline-corroborated rules (4/5), so an initials coincidence can't
# merge two calls on its own — it still needs the same deadline + title/value overlap.
_FUNDER_INITIAL_SKIP = {"of", "the", "and", "for", "de", "du", "des", "la", "le", "et"}


def _funder_core(s: str | None) -> str:
    raw = (s or "")
    raw = raw.split(" - ", 1)[1] if " - " in raw else raw   # drop "ACRONYM - " prefix
    return _WS.sub(" ", _PUNCT.sub(" ", raw.lower())).strip()


def _funder_initials(s: str | None) -> str:
    raw = (s or "")
    raw = raw.split(" - ", 1)[1] if " - " in raw else raw
    words = re.findall(r"[A-Za-z][A-Za-z'&\-]*", raw)
    return "".join(w[0].upper() for w in words if w.lower() not in _FUNDER_INITIAL_SKIP)


def _funders_equivalent(a: str | None, b: str | None) -> bool:
    ca, cb = _funder_core(a), _funder_core(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if len(ca) >= 5 and len(cb) >= 5 and (ca in cb or cb in ca):
        return True
    # acronym ⇄ full-name initials (IDRC ⇄ International Development Research Centre)
    for short, full in ((a, b), (b, a)):
        s = re.sub(r"[^A-Za-z]", "", (short or "")).upper()
        if 2 <= len(s) <= 6 and s == _funder_initials(full):
            return True
    return False


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    try:
        p = urlsplit(u.strip())
        # Drop query/fragment + trailing slash; lowercase scheme/host.
        path = p.path.rstrip("/")
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))
    except Exception:
        return u.strip().lower()


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# Ultra-generic grant vocabulary + stopwords — shared across most calls, so they
# carry no disambiguating power. Excluded when counting "distinctive" overlap.
_GENERIC_TOKENS = {
    "call", "calls", "proposal", "proposals", "for", "the", "of", "a", "an",
    "and", "to", "in", "on", "is", "are", "now", "open", "until", "grant",
    "grants", "fund", "funds", "funding", "rfp", "rfa", "cfp", "eoi", "rfq",
    "application", "applications", "apply", "round", "programme", "program",
    "project", "projects", "award", "awards", "scheme", "new", "research",
}


def _title_countries(title: str) -> set[str]:
    """Canonical countries named in a title. Empty when the title names none.

    Deliberately COUNTRIES only, never regions or income tiers: "Sub-Saharan Africa" and
    "LMICs" are qualifiers that two genuinely identical calls may word differently, while
    a country is an identity.
    """
    try:
        from core.auto_scorer import _COUNTRY_PATTERN
        from core import geographies as geo
    except Exception:                                    # pragma: no cover - import cycle
        return set()
    return {geo.canonical_geo(m).lower()
            for m in _COUNTRY_PATTERN.findall(title or "") if m}


def _different_countries(cand_title: str, row_title: str) -> bool:
    """True when the two titles each name a country and the sets are DISJOINT.

    THE COUNTRY IS THE IDENTITY IN A SIBLING-PROGRAMME FAMILY. Funders publish families
    of calls whose titles are one boilerplate with the country swapped:

        "Modern Slavery Fund Albania Programme 2026 to 2029: call for proposals"
        "Modern Slavery Fund Viet Nam Programme 2026 to 2029: call for proposals"

    Those are 91% similar as characters, which cleared the 0.90 title threshold, so the
    two calls were declared the same one and merged. The country name — the single most
    disambiguating word in the title — was the only thing that differed, and a character
    ratio is structurally blind to WHICH characters differ. The same shape appears in
    development-bank procurement notices ("IFB - Cabo Verde - ...", "SPN - Zimbabwe -
    ...") and in EU topic families.

    A merge is not a tie-break: it keeps one row's title and link and gap-fills the rest
    from the other, so a wrong match does not lose a row, it CORRUPTS one — the surviving
    call ends up wearing a different country's description and award value. That is worse
    than keeping a duplicate, which a human can see and dismiss. So this vetoes.

    Nothing is vetoed when only one side names a country (a general call and a
    country-specific one may still be the same call reworded), or when the sets overlap.
    """
    a, b = _title_countries(cand_title), _title_countries(row_title)
    return bool(a and b and not (a & b))


def _distinctive_tokens(title: str) -> set[str]:
    """Meaningful, disambiguating tokens of a normalised title (drop generic
    grant vocabulary + bare years + 1-2 char noise)."""
    out = set()
    for tok in title.split():
        if tok in _GENERIC_TOKENS or len(tok) < 3:
            continue
        if tok.isdigit() and len(tok) == 4:      # bare year (2026) — not distinctive
            continue
        out.add(tok)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def find_duplicates(
    candidate: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the existing rows the candidate is a duplicate of (zero or more).

    `existing` is an iterable of dicts each with at least
    `uid, opportunity_title, opportunity_link, funding_agency,
    submission_deadline, estimated_value`. When omitted, it is fetched from
    Supabase (excluding rows already flagged as duplicates).
    """
    if existing is None:
        existing = _fetch_existing()

    cand_oppid = _norm_oppid(candidate.get("opportunity_id"))
    cand_link = _norm_url(candidate.get("opportunity_link"))
    cand_title = _norm_title(candidate.get("opportunity_title"))
    cand_agency = (candidate.get("funding_agency") or "").strip().lower()
    cand_deadline = str(candidate.get("call_submission_deadline") or "").strip()
    cand_value = candidate.get("call_award_value")

    matches: list[dict[str, Any]] = []
    for row in existing:
        if row.get("uid") and candidate.get("uid") == row.get("uid"):
            continue  # skip self

        # 1. Identical opportunity_id — strongest signal. A donor's own
        # RFP number is unique within their portal, so equality here is
        # dispositive. This is what catches "same RFP imported from
        # Excel + later scanned by the bot" duplicates that previously
        # slipped through.
        row_oppid = _norm_oppid(row.get("opportunity_id"))
        if cand_oppid and row_oppid and cand_oppid == row_oppid:
            matches.append({**row, "_reason": f"opportunity_id match: {cand_oppid}"})
            continue

        # 2. Identical link
        if cand_link and _norm_url(row.get("opportunity_link")) == cand_link:
            matches.append({**row, "_reason": "identical link"})
            continue

        # A country named on BOTH sides, disagreeing, settles it: these are different
        # calls, and no amount of shared boilerplate says otherwise. Checked before the
        # two similarity rules because both of them fired on real sibling programmes and
        # the merge that followed corrupted the surviving row. Rules 1 and 2 above are
        # left alone on purpose: an identical opportunity_id or an identical link is
        # dispositive evidence of the SAME call, and outranks a country in the title.
        _row_title_raw = row.get("opportunity_title")
        if _different_countries(candidate.get("opportunity_title") or "",
                                _row_title_raw or ""):
            continue

        # 3. Title similarity
        sim = _title_similarity(cand_title, _norm_title(_row_title_raw))
        if sim >= TITLE_THRESHOLD:
            matches.append({**row, "_reason": f"title similarity {sim:.0%}"})
            continue

        # 4. Same funder + same deadline + meaningful title overlap.
        # Value-INDEPENDENT (most calls carry no published amount). A single
        # funder rarely runs two DISTINCT calls closing the same day on the same
        # topic, so funder+deadline plus either a moderate title similarity OR
        # >=2 shared distinctive tokens collapses the "same call, different
        # title/URL from two sources" case (e.g. MMV's "9th African Call for
        # proposals … open until 29 Aug" vs "Malaria drug discovery: 9th African
        # call for proposals now open" — same funder, same 2026-08-29 deadline).
        if (
            cand_agency
            and cand_deadline
            and _funders_equivalent(candidate.get("funding_agency"), row.get("funding_agency"))
            and str(row.get("call_submission_deadline") or "").strip() == cand_deadline
        ):
            shared = (_distinctive_tokens(cand_title)
                      & _distinctive_tokens(_norm_title(row.get("opportunity_title"))))
            # STRICT threshold: sim >= 0.75 OR >= 3 shared DISTINCTIVE tokens. Funders
            # like Grand Challenges run MANY distinct calls closing the same day, so a
            # weak overlap (2 generic tokens like "cost"/"innovations" at 35% sim) is a
            # false positive — the IDRC true-dup has 6 shared tokens, the diarrheal one
            # 82% sim, so the real duplicates still clear this bar.
            if sim >= 0.75 or len(shared) >= 3:
                matches.append({**row, "_reason":
                                f"funder + deadline + title overlap "
                                f"(sim={sim:.0%}, shared={sorted(shared)[:4]})"})
                continue

        # 5. Funder + deadline + value triple (kept: exact-value corroboration).
        if (
            cand_agency
            and cand_deadline
            and cand_value is not None
            and _funders_equivalent(candidate.get("funding_agency"), row.get("funding_agency"))
            and str(row.get("call_submission_deadline") or "").strip() == cand_deadline
            and row.get("call_award_value") == cand_value
        ):
            matches.append({**row, "_reason": "funder + deadline + value match"})

    return matches


def _dup_blank(v: Any) -> bool:
    return v is None or v == "" or v == [] or str(v).strip().lower() == "nan"


_RECONCILE_KEYS = (
    "call_submission_deadline", "call_award_value", "call_domain_areas",
    "call_geographic_scope", "brief_description",
)
# Fields worth gap-filling onto the surviving canonical from a flagged duplicate,
# so consolidating two rows never loses data (e.g. a migration row's project_duration).
_RECONCILE_FILL = (
    "call_submission_deadline", "call_award_value", "currency", "project_duration",
    "call_domain_areas", "call_geographic_scope", "brief_description",
    "opportunity_id", "funding_opportunity_number",
)


def _completeness(r: Mapping[str, Any]) -> int:
    return sum(1 for k in _RECONCILE_KEYS if not _dup_blank(r.get(k)))


def reconcile_duplicates(dry_run: bool = True) -> dict[str, Any]:
    """Reconcile duplicates that are ALREADY stored (both rows inserted) — the case the
    ingest-time dedup can't fix, because it only compares a NEW candidate to existing
    rows. Clusters every non-duplicate row with find_duplicates; in each cluster the
    RICHEST row (human-reviewed > most complete > longest brief > oldest) stays canonical,
    the rest are flagged is_duplicate=True → duplicate_of_uid, and any field still blank
    on the canonical is gap-filled from the duplicate so no data is lost. Returns a report;
    writes only when dry_run is False."""
    sb = get_client()
    rows = (
        sb.table("rfp_submissions")
        .select(
            "uid,opportunity_id,opportunity_title,opportunity_link,funding_agency,"
            "call_submission_deadline,call_award_value,currency,project_duration,"
            "call_domain_areas,call_geographic_scope,brief_description,submitted_at,"
            "funding_opportunity_number,source,decision,decision_date"
        )
        .eq("is_duplicate", False)
        .execute()
        .data
        or []
    )
    # Richest first → becomes the cluster's canonical (stable, keeps the best data).
    rows_sorted = sorted(rows, key=lambda r: (
        -(1 if str(r.get("decision_date") or "").strip() else 0),
        -_completeness(r),
        -len(str(r.get("brief_description") or "")),
        str(r.get("submitted_at") or ""),          # ties → older canonical
    ))
    canon: list[dict[str, Any]] = []
    flagged: list[tuple[dict, dict]] = []
    for r in rows_sorted:
        m = find_duplicates(r, existing=canon)
        if m:
            flagged.append((r, m[0]))
        else:
            canon.append(r)

    report = {"total": len(rows), "canonical": len(canon), "flagged": len(flagged),
              "pairs": [], "filled": 0}
    for dup, can in flagged:
        patch = {f: dup.get(f) for f in _RECONCILE_FILL
                 if _dup_blank(can.get(f)) and not _dup_blank(dup.get(f))}
        report["pairs"].append({
            "duplicate": dup["uid"], "canonical": can["uid"],
            "reason": can.get("_reason"), "gap_filled": sorted(patch.keys())})
        if not dry_run:
            if patch:
                sb.table("rfp_submissions").update(patch).eq("uid", can["uid"]).execute()
                can.update(patch)          # keep in-memory canon current for later clusters
                report["filled"] += 1
            sb.table("rfp_submissions").update(
                {"is_duplicate": True, "duplicate_of_uid": can["uid"]}
            ).eq("uid", dup["uid"]).execute()
    return report


def _norm_oppid(s: str | None) -> str:
    """Normalize a donor RFP number for cross-source equality. Excel
    imports often store it with spaces or surrounding noise ("FOA
    HT942526PRMRPDA"), so we strip whitespace and uppercase. Empty /
    obviously non-identifier values return empty string."""
    if not s:
        return ""
    out = re.sub(r"\s+", "", str(s)).upper().strip()
    # Filter out obvious garbage that isn't an ID (very short, all digits
    # without letters, etc.) — these create false-positive matches.
    if len(out) < 4:
        return ""
    return out


def _fetch_existing() -> list[dict[str, Any]]:
    """Fetch every non-duplicate row across ALL sources (auto, manual,
    migration). Migration rows are intentionally included so a freshly-
    scanned Grants.gov RFP can be recognised as a duplicate of the
    matching Excel row that was imported during the initial migration."""
    sb = get_client()
    res = (
        sb.table("rfp_submissions")
        .select(
            "uid,opportunity_id,opportunity_title,opportunity_link,"
            "funding_agency,call_submission_deadline,call_award_value,"
            "submitted_at,source"
        )
        .eq("is_duplicate", False)
        .execute()
    )
    return res.data or []
