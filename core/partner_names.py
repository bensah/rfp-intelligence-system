"""Splitting an applicant cell into the organisations it names.

A `lead_applicant` / `sub_applicant` cell can name several organisations that applied jointly on
one grant, separated by ";" or ",". Counting partners means splitting on those separators — but a
single organisation's own name can CONTAIN one, and then the split invents a partner that does
not exist.

Two such shapes are known:

  * a legal-entity suffix after a comma — "Westvale Media Labs, Inc."
  * an abbreviation after a separator — "Northline Statistics Group Inc.; (NSG)", which split
    into two bars, one of them a bare "(NSG)". The same name written WITHOUT a separator,
    "Northline Statistics Group (NSG)", was never affected, which is what makes the separator
    the culprit rather than the parentheses.

Both are handled by re-attaching rather than by weakening the separator rule: the split still
happens on every ";" and ",", and a piece is only glued back on when it is recognisably part of
the name before it. Anything unrecognised stays a separate organisation, so an unfamiliar shape
is over-counted (visible, correctable) rather than silently merged into its neighbour.
"""
from __future__ import annotations

import re

# Blank-equivalents. "N/A" is a real, distinct value a person typed — it is still not an
# applicant, so it is dropped rather than charted.
NA_VALUES = frozenset({"n/a", "na", "not applicable", "none", "nil", "tbd", "-", "—", "nan"})

# Legal-entity suffixes that follow a COMMA inside ONE org name ("Westvale Media Labs, Inc.").
LEGAL_SUFFIX = re.compile(
    r"^(inc|llc|l\.l\.c|ltd|co|corp|pbc|gmbh|s\.?a|plc|llp|pte|bv|ag|nv|"
    r"limited|incorporated|company)\.?$", re.I)

# A parenthesised abbreviation standing alone as a split piece: "(NSG)", "( NSG )".
_PAREN_ACRONYM = re.compile(r"^\(\s*([A-Za-z0-9&.\-]{2,12})\s*\)$")

# A trailing parenthesised abbreviation still attached to its name, for canonicalisation:
# "Northline Statistics Group (NSG)" -> "Northline Statistics Group".
_TRAILING_ACRONYM = re.compile(r"\s*\(\s*([A-Za-z0-9&.\-]{2,12})\s*\)\s*$")

# Words an abbreviation skips over. "Institute OF Statistics" abbreviates to IS, not IOS.
_ACRONYM_SKIP = frozenset({"of", "and", "the", "for", "in", "on", "at", "to", "a", "an", "&",
                           "de", "du", "des", "la", "le", "les", "el", "y"})


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^A-Za-z0-9&]+", str(name or "")) if w]


def _initial_variants(name: str) -> set[str]:
    """The abbreviations `name` could reasonably produce.

    A legal suffix may or may not be counted — "Northline Statistics Group Inc." is written both
    NSG and NSGI — so both readings are accepted. Nothing else is guessed: an abbreviation that
    matches neither is treated as a different organisation.
    """
    words = _words(name)
    if not words:
        return set()
    out = set()
    for drop_suffix in (True, False):
        picked = []
        for w in words:
            if w.lower() in _ACRONYM_SKIP:
                continue
            if drop_suffix and LEGAL_SUFFIX.match(w):
                continue
            picked.append(w[0].upper())
        if len(picked) >= 2:
            out.add("".join(picked))
    return out


def _letters(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(token or "")).upper()


def is_acronym_of(token: str, name: str) -> bool:
    """True when `token` is an abbreviation of `name`.

    This is the test that lets a split piece be re-attached, so it is deliberately strict —
    initials must match exactly. A piece that merely looks short and shouty stays its own
    organisation.
    """
    letters = _letters(token)
    return bool(letters) and letters in _initial_variants(name)


def strip_trailing_acronym(name: str) -> str:
    """Drop a trailing "(ABC)" when it abbreviates the rest of the name, else leave it.

    Needed for identity, not display: once "Org Full Name; (OFN)" is re-attached, the result must
    still be recognisable as the deploying org's own name, or canonicalisation would miss it and
    the org would get a second bar of its own.
    """
    s = str(name or "").strip()
    m = _TRAILING_ACRONYM.search(s)
    if not m:
        return s
    base = s[:m.start()].strip()
    return base if (base and is_acronym_of(m.group(1), base)) else s


def split_pieces(value) -> list[str]:
    """Split on ";" / "," and re-attach the pieces that belong to the name before them."""
    merged: list[str] = []
    for piece in re.split(r"[;,]", str(value or "")):
        piece = piece.strip()
        if not piece:
            continue
        if merged and LEGAL_SUFFIX.match(piece):
            merged[-1] = f"{merged[-1]}, {piece}"          # "Westvale Media Labs" + "Inc."
            continue
        m = _PAREN_ACRONYM.match(piece)
        if merged and m and is_acronym_of(m.group(1), merged[-1]):
            # Re-attached with a SPACE, not the original separator, so it reads the way the
            # same organisation reads when it was never split: "Name (ABC)".
            merged[-1] = f"{merged[-1]} ({m.group(1)})"
            continue
        merged.append(piece)
    return merged
