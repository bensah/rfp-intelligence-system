"""Where an account is in its life: pending, active, inactive.

An invitation SENT is not an account in USE. `users.is_active` is true from the moment an
admin creates the row - it has to be, or the person could not sign in once they activate -
so a status derived from that column alone reads "Active" for somebody who has never
received, let alone opened, their invitation. An admin then has no way to see who still owes
them an activation.

Kept out of the UI module so it can be tested directly: the first version of this lived
inline in the users table and looked right, but `None` arrives from pandas as the float
`nan`, whose str() is "nan" - truthy - so the pending branch never fired. A structural test
that greps the source for "Pending" would have passed happily.
"""
from __future__ import annotations

from typing import Any

STATUS_PENDING = "🕓 Pending"
STATUS_ACTIVE = "🟢 Active"
STATUS_INACTIVE = "⏸ Inactive"

# Values that mean "nothing here" once a column has been through pandas or a JSON round
# trip. Same guard as core.program_area_select._as_selection, for the same reason.
_BLANKS = {"", "nan", "none", "nat", "null", "<na>"}


def _is_blank(v: Any) -> bool:
    return str(v or "").strip().lower() in _BLANKS


def account_status(is_active: Any, must_change_password: Any,
                   last_login_at: Any) -> str:
    """The label for one account.

    PENDING requires BOTH halves to be outstanding: the invitation has not been redeemed
    (must_change_password is still set) AND the account has never been signed into. Either
    one alone is not enough - somebody who has signed in and been asked to rotate their
    password is an active user with a chore, and an account whose flag was cleared by hand
    but which has never been used is a different anomaly, not a pending invitation.
    """
    if not bool(is_active):
        return STATUS_INACTIVE
    never_signed_in = _is_blank(last_login_at)
    if bool(must_change_password) and never_signed_in:
        return STATUS_PENDING
    return STATUS_ACTIVE


__all__ = ["STATUS_PENDING", "STATUS_ACTIVE", "STATUS_INACTIVE", "account_status"]
