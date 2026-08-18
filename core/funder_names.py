"""One spelling rule for funder names, so the same donor is one donor everywhere.

Funder strings reach this app by three routes — a scan, a manual add, and the Excel
workbook — and the workbook is where the trouble starts: Word and Excel autocorrect
`ACRONYM - Name` into `ACRONYM – Name` (EN DASH, U+2013) the moment somebody edits the
cell. The two spellings are indistinguishable at a glance and identical in meaning, but
they are different strings, so anything that groups on the literal value splits the donor
in half. That is exactly what the report's "Which funders our calls come from" chart did:
`BMGF - Gates Foundation` with 5 calls and `BMGF – Gates Foundation` with 4, side by side,
each understating a funder the organisation actually has 9 calls from.

The matcher (`core.donor_intel`) already learned this lesson — its separator pattern
accepts the whole dash family. This module puts the same rule where the VALUE is written
and where it is grouped, so the fix does not depend on a reader remembering to normalise.

Two functions, deliberately distinct:

  `canonical_funder`  — the DISPLAY form to store. Conservative: it repairs the dash
                        family and whitespace and nothing else. Case, punctuation and the
                        organisation's own wording are left exactly as typed, because a
                        stored name is shown to people and "normalising" it further would
                        be rewriting their words.
  `funder_key`        — the IDENTITY form to group and compare on: case, punctuation and
                        spacing all collapse, so every spelling of one donor lands on one
                        key. Never displayed, and deliberately the same rule the donor
                        matcher already uses (see the function).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# Every dash Unicode offers that a human means as "-": hyphen, non-breaking hyphen, figure
# dash, en dash, em dash, horizontal bar, minus sign, and the soft hyphen that pastes in
# invisibly from PDFs and web pages.
_DASHES = "‐‑‒–—―−­"
_DASH_RE = re.compile(f"[{_DASHES}]")
_WS_RE = re.compile(r"\s+")


def canonical_funder(value: Any) -> str:
    """The funder name as it should be STORED — same words, one spelling.

    NFKC first (so a full-width or ligature character is the ordinary one), then the dash
    family folds to an ASCII hyphen and runs of whitespace collapse to single spaces. A
    soft hyphen is invisible, so it disappears rather than becoming a visible '-'.
    Idempotent: canonicalising an already-canonical name changes nothing."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.replace("­", "")                  # soft hyphen: invisible, not a dash
    s = _DASH_RE.sub("-", s)
    return _WS_RE.sub(" ", s).strip()


def funder_key(value: Any) -> str:
    """The IDENTITY of a funder name — what makes two spellings the same donor.

    Deliberately the SAME rule `core.donor_intel._norm` uses to match funders against donor
    records, applied to the canonical spelling: lowercase, and every run of non-alphanumeric
    characters becomes one space. A test holds the two together, because a chart that groups
    funders one way while the matcher identifies them another is a worse bug than the one
    this module fixes — the numbers would stop agreeing with the donor pages.

    That rule is deliberately literal about letters: "Française" and "Francaise" get
    different keys, as do "Bill & Melinda" and "Bill and Melinda". Neither variation exists
    in the data today, and widening the rule here alone would break the agreement above —
    it belongs in `_norm`, for both callers at once, on the day it is actually needed."""
    return re.sub(r"[^a-z0-9]+", " ", canonical_funder(value).lower()).strip()


def dominant_spelling(values: Iterable[Any]) -> str:
    """The spelling to SHOW for a group of variants: the most common one, ties broken by
    the alphabetically first so the label never flickers between renders. Showing the
    majority form keeps a chart in the organisation's own words rather than imposing a
    machine-normalised version of their donor's name."""
    counts: dict[str, int] = {}
    for value in values:
        name = canonical_funder(value)
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def group_by_funder(values: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """`{key: {"label": <dominant spelling>, "count": n, "variants": {raw: n}}}` — the
    whole reconciliation picture for a column of funder strings, used by both the report
    grouping and the backfill script so they can never disagree about what is a duplicate."""
    out: dict[str, dict[str, Any]] = {}
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        key = funder_key(raw)
        if not key:
            continue
        entry = out.setdefault(key, {"label": "", "count": 0, "variants": {}})
        entry["count"] += 1
        entry["variants"][raw] = entry["variants"].get(raw, 0) + 1
    for entry in out.values():
        entry["label"] = dominant_spelling(
            [v for v, n in entry["variants"].items() for _ in range(n)])
    return out
