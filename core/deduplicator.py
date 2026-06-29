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


def _norm_title(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


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

        # 3. Title similarity
        sim = _title_similarity(cand_title, _norm_title(row.get("opportunity_title")))
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
            and (row.get("funding_agency") or "").strip().lower() == cand_agency
            and str(row.get("call_submission_deadline") or "").strip() == cand_deadline
        ):
            shared = (_distinctive_tokens(cand_title)
                      & _distinctive_tokens(_norm_title(row.get("opportunity_title"))))
            if sim >= 0.55 or len(shared) >= 2:
                matches.append({**row, "_reason":
                                f"funder + deadline + title overlap "
                                f"(sim={sim:.0%}, shared={sorted(shared)[:4]})"})
                continue

        # 5. Funder + deadline + value triple (kept: exact-value corroboration).
        if (
            cand_agency
            and cand_deadline
            and cand_value is not None
            and (row.get("funding_agency") or "").strip().lower() == cand_agency
            and str(row.get("call_submission_deadline") or "").strip() == cand_deadline
            and row.get("call_award_value") == cand_value
        ):
            matches.append({**row, "_reason": "funder + deadline + value match"})

    return matches


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
