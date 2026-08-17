"""What counts as "the funder has not answered yet".

Three places had their own copy of this test — the RFP editor, the Grants page and the Excel
sync — and they have to agree, because one of them SUGGESTS a value, one DISPLAYS a derived
one, and one decides whether a spreadsheet may overwrite what is stored. When they drift, the
app shows one thing and stores another.

The subtlety worth naming: "Not submitted" is the DEFAULT every row carries, not a judgement
anybody made, so it counts as no answer. "Under Review" is different — somebody (or something)
put it there deliberately, so it is an answer for the purpose of not being overwritten, even
though it is not an outcome.
"""
from __future__ import annotations

from typing import Any

# The funder's real answers. Anything else is either the default or a pending state.
OUTCOMES = ("Approved", "Not Approved")

# Values that mean nobody has recorded an answer. Includes the shapes a NULL takes after a
# pandas round trip, because these values reach here from DataFrames as often as from dicts.
_NO_ANSWER = {"", "none", "nan", "nat", "null", "not submitted"}


def is_no_answer(value: Any) -> bool:
    """True when nothing has been recorded about the funder's response.

    Used to decide whether to SUGGEST "Under Review" (editor), whether to DISPLAY a Completed
    row as pending (Grants), and whether a spreadsheet value may fill a stored blank (Excel
    sync). One definition so those three cannot disagree.
    """
    return str(value or "").strip().lower() in _NO_ANSWER


def is_outcome(value: Any) -> bool:
    """True when the funder has actually decided — approved or not.

    "Under Review" is deliberately NOT an outcome: it records that we are waiting, which is
    why a row carrying it still belongs in Applied Funding but not in a win/loss count.
    """
    return str(value or "").strip().lower() in {o.lower() for o in OUTCOMES}


__all__ = ["OUTCOMES", "is_no_answer", "is_outcome"]
