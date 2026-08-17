"""The password rule, stated once.

Four screens had their own copy of the check and their own wording — the set-password
screen, the forced-change gate, the self-service change form and the sign-up form — so the
rule a person was TOLD could differ from the rule they were JUDGED by. Someone would be
rejected for a password that satisfied the help text they had just read.

Deliberately free of Streamlit: a policy is not a widget. It also means the rule can be
tested without importing the UI, which matters here because another test module installs a
fake `streamlit` in sys.modules and anything importing the real one breaks under the full
suite.
"""
from __future__ import annotations

PASSWORD_MIN_LEN = 8

# What the person is told BEFORE they type. Shown next to the field, not only after a
# rejection: making someone guess, fail, and guess again is the thing being fixed.
PASSWORD_RULES_TEXT = (
    "At least 8 characters, including at least one letter and one number. "
    "Spaces are allowed."
)


def password_problems(pw: str | None) -> list[str]:
    """Everything wrong with `pw`, each naming what is MISSING rather than what failed.

    Returns [] for an acceptable password. Every problem is reported together on purpose:
    telling someone their password is too short, and only once they have fixed that telling
    them it also needs a digit, is two rounds of guessing for one rule.

    Each message says what to do ("Add at least one number.") rather than what went wrong
    ("Password must include letters AND digits."), and the length one quotes the length
    actually supplied, because "too short" without a target is the complaint being fixed.
    """
    pw = pw or ""
    out: list[str] = []
    if len(pw) < PASSWORD_MIN_LEN:
        out.append(f"Too short — use at least {PASSWORD_MIN_LEN} characters "
                   f"(this one has {len(pw)}).")
    if not any(c.isalpha() for c in pw):
        out.append("Add at least one letter.")
    if not any(c.isdigit() for c in pw):
        out.append("Add at least one number.")
    return out


__all__ = ["PASSWORD_MIN_LEN", "PASSWORD_RULES_TEXT", "password_problems"]
