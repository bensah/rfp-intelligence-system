"""Resolving a person's name to one canonical form, and to a chart label.

Names arrive typed by hand across years of forms and spreadsheet imports: nicknames, ALL CAPS,
first-name-only, honorifics. Charted raw, one person lands on several bars.

PERSON NAMES ARE DATA, and nothing in this file contains one. The nickname and alias maps are
read from the database (app_settings, behind the same access controls as the roster) and are
EMPTY by default — the same arrangement `settings.get_team_members` already uses, where the YAML
holds placeholders only.

This logic used to sit inline in the report page, where it could not be imported or tested, and
it carried two hard-coded real names. One mapped a real first name to a surname INVENTED during
an anonymisation pass, so the report charted a person under a name that does not exist — and,
because that invented name collided with the person's real record, it split them across two bars.

RESOLUTION ORDER in `normalize_member_name`:

  1. Strip honorifics, trim, collapse whitespace, title-case (handles ALL CAPS). A value that is
     nothing but a title is not a person and resolves to "(unknown)".
  2. Whole-string alias from the database, if any.
  3. Exact match against the roster.
  4. Token subset: after nickname expansion, input tokens contained in a roster name's tokens.
  5. Tie-break on the longest roster match, so "First Last" beats "First".
  6. Otherwise return the tidied input unchanged.

Step 4 assumes no two people share a first OR last name, which holds for a small team. Where two
do, `first_name_display_map` is what keeps them apart on the chart.
"""
from __future__ import annotations

import pandas as pd

from core import dropdowns, settings


# ---------------------------------------------------------------------------
# Person names are DATA and live in the database, behind the same access controls as the
# roster — never in this file. Both maps are configured per deployment
# (app_settings.member_nicknames_json / member_name_aliases_json) and are EMPTY by default,
# so an unconfigured deployment simply leaves names as they were typed.
#
# This file used to hard-code them, and the cost was not only the leak: one alias mapped a real
# first name to a surname that had been INVENTED during an anonymisation pass, so the report
# charted a person under a name that does not exist, and split one real person into two bars.


def _nickname_map() -> dict[str, str]:
    """{"nickname": "formal"} token map. Never raises — name display is not worth a page."""
    try:
        return settings.get_member_nicknames()
    except Exception:
        return {}


def _fullname_aliases() -> dict[str, str]:
    """{"shortform": "Canonical Full Name"} whole-string map."""
    try:
        return settings.get_member_name_aliases()
    except Exception:
        return {}


# Honorifics are TITLES, not names. Left in place they became people: a value like
# "Prof <name>" charted as a bar labelled "Prof", because the display label is the first
# token. Stripped for both matching and display, so the person is charted under their name.
_HONORIFICS = frozenset({
    "prof", "professor", "dr", "doc", "mr", "mrs", "ms", "miss", "mx", "sir", "madam", "mme",
    "eng", "ing", "hon", "rev", "fr", "pr", "pastor", "amb", "sen", "capt", "col", "gen",
})


def _strip_honorifics(name: str) -> str:
    """Drop leading honorifics. Returns "" when the value is ONLY a title.

    A bare title is not a person, and charting it invents a team member out of a courtesy word.
    """
    toks = str(name or "").replace(".", " ").split()
    while toks and toks[0].lower() in _HONORIFICS:
        toks.pop(0)
    return " ".join(toks)

def _title_case(s: str) -> str:
    """ALL CAPS / lower / Mixed → Sentence Case per word. Keeps hyphens and
    apostrophes intact (so 'O'Brien' stays 'O'Brien' rather than 'O'brien')."""
    if not s:
        return ""
    parts = []
    for word in str(s).strip().split():
        # Preserve apostrophe casing: O'BRIEN → O'Brien
        if "'" in word:
            chunks = word.split("'")
            parts.append("'".join(c.capitalize() for c in chunks))
        else:
            parts.append(word.capitalize())
    return " ".join(parts)


def _tokenize_name(s: str) -> set[str]:
    """Lowercase token set: honorifics dropped, nicknames expanded to their formal form."""
    nick = _nickname_map()
    out: set[str] = set()
    for tok in _strip_honorifics(s).lower().replace("-", " ").split():
        out.add(nick.get(tok, tok))
    return out


def normalize_member_name(raw: str | None) -> str:
    """Map a raw name string to the canonical team-member name.

    None / empty → "(unknown)" so it still buckets cleanly.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "(unknown)"
    s = _strip_honorifics(str(raw).strip())
    if not s:
        # Blank, or a value that was nothing but a title ("Prof").
        return "(unknown)"
    tidy = _title_case(s)
    canonical_list = tuple(dropdowns.get("team_members") or [])
    if not canonical_list:
        return tidy

    # Whole-string alias check, BEFORE the exact-match shortcut: a first-name-only mention
    # must be able to roll up to a specific person even when that first name is also a roster
    # entry in its own right. Configured in the database, empty by default.
    alias_target = _fullname_aliases().get(tidy.lower())
    if alias_target:
        for c in canonical_list:
            if c.lower() == alias_target.lower():
                return c
        # Alias target isn't in the dropdown — return it anyway so the
        # rollup happens; downstream chart code doesn't require canonical
        # membership.
        return alias_target

    # Exact match on the cleaned-up form
    for c in canonical_list:
        if c.lower() == tidy.lower():
            return c

    # Subset / nickname match
    input_tokens = _tokenize_name(tidy)
    if not input_tokens:
        return tidy
    matches: list[str] = []
    for c in canonical_list:
        c_tokens = _tokenize_name(c)
        if not c_tokens:
            continue
        # Input ⊆ canonical (e.g. single-name ⊆ full-name) OR
        # canonical ⊆ input (rare — when a fuller form is submitted)
        if input_tokens <= c_tokens or c_tokens <= input_tokens:
            matches.append(c)
    if matches:
        # Tie-break: prefer the canonical name with MORE tokens (the
        # fully-specified form). "First Last" beats "First".
        matches.sort(key=lambda x: (-len(_tokenize_name(x)), x))
        return matches[0]

    return tidy


def split_and_normalize_names(value) -> list[str]:
    """Split a comma-separated name (or list-of-strings) into a flat
    list of canonical names.

    Cases handled:
      * None / NaN / empty                  → []
      * "Jane Doe"                       → ["Jane Doe"]
      * "Alex Kim, Jane Doe"        → ["Alex Kim", "Jane Doe"]
      * ["Alex Kim", "Jane Doe"]    → ["Alex Kim", "Jane Doe"]
        (Postgres text[] arrays — contributors column)
      * ["Alex Kim, Jane Doe"]      → ["Alex Kim", "Jane Doe"]
        (one list element that ITSELF contains commas — common when a
        sloppy form submission packed two names into one entry)

    Each split piece is run through `normalize_member_name()` so
    nickname / case / partial variants collapse to the canonical form.
    "(unknown)" results are filtered out — they only appear when the
    input was empty/None.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend(split_and_normalize_names(v))
        return out
    parts = [p.strip() for p in str(value).split(",")]
    normalized = [normalize_member_name(p) for p in parts if p]
    return [n for n in normalized if n and n != "(unknown)"]


def first_name_display_map(canonical_names) -> dict[str, str]:
    """{canonical_name -> chart label}: first names, full names only where truly ambiguous.

      ["Jane Doe", "Drew Hall"]        -> {"Jane Doe": "Jane", "Drew Hall": "Drew"}
      ["Ada", "Ada Nwosu"]             -> both "Ada"        (one person, two spellings)
      ["Ada Nwosu", "Ada Okafor"]      -> both full          (two people)
      ["Prof Ada Nwosu"]               -> "Ada"              (a title is not a name)

    Applied before the group-by, so names mapping to the same label become one bar.

    Shorter labels mean narrower charts — which matters most in print, where a wide label
    column squeezes the plot.
    """
    canonical = [n for n in set(canonical_names) if n and n != "(unknown)"]
    by_first: dict[str, list[str]] = {}
    for name in canonical:
        # Honorific-aware: `"Prof <name>".split()[0]` is "Prof", so a titled name was labelled
        # with the courtesy word instead of the person.
        bare = _strip_honorifics(name) or name
        by_first.setdefault(bare.split()[0], []).append(name)

    display: dict[str, str] = {}
    for first, names in by_first.items():
        # Sharing a first name is not the same as being different people. The data holds the
        # same person written both ways — "Ada" on one row, "Ada Nwosu" on another — and the
        # old rule read that as a collision and printed BOTH in full, which is what put one
        # two-word label among a column of first names.
        #
        # So: how many DISTINCT fuller forms are there? One means one person written at
        # different lengths — label the whole group with the first name, and because this map
        # is applied before the group-by, their bars merge into the single person they are.
        # Two or more means genuinely different people, and only then is the full name needed
        # to tell them apart.
        #
        # This is also what retires the hard-coded alias that used to force this rollup: the
        # relationship is derived from the names actually present, so it needs no roster entry,
        # invents nothing, and keeps person names out of this file.
        fuller = {(_strip_honorifics(n) or n) for n in names
                  if len((_strip_honorifics(n) or n).split()) > 1}
        if len(fuller) <= 1:
            for n in names:
                display[n] = first
        else:
            for n in names:
                # Titles dropped here too, so a disambiguated label is still a name.
                display[n] = _strip_honorifics(n) or n
    # Pass through "(unknown)" so it still groups
    display["(unknown)"] = "(unknown)"
    return display
