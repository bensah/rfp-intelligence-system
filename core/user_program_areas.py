"""Program areas the tenant's own users have declared about themselves.

WHY THIS IS EVIDENCE, AND NOT JUST ANOTHER PREFERENCE (owner, 2026-08-17).

The org profile is edited by one person, occasionally. It goes stale, it reflects that one
person's view of the organisation, and a low rating there is as likely to mean "nobody has
updated this" as "we are weak here". A colleague naming a programme area on their own
account is a different kind of statement: it is a person saying *this is what I work on*,
recorded per user, and accumulating as the team grows.

So a declared area is treated as HARD EVIDENCE of expertise and rated 5, overriding a lower
tenant rating for the same area. It can only raise a rating, never lower one - the profile
remains the floor, and a colleague's declaration lifts it.

Read from `users.program` and canonicalised through the shared taxonomy, so a declaration
compares against call and donor themes on the same vocabulary as everything else.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# The rating a declared area is worth. The top of the 0-5 band: somebody in this
# organisation does this work, which is the strongest claim the profile can carry.
DECLARED_RATING = 5.0

_TTL = 60.0
_CACHE: dict[str, tuple[float, list[str]]] = {}


def _split(raw: Any) -> list[str]:
    """A stored `users.program` value as a list of terms.

    The column is free text and has always been - historically "Vaccines, MCH, Malaria"
    typed by hand, now canonical keys chosen from the picker. Both are split the same way
    and handed to the classifier, which resolves a canonical key, a bare sub-area label or
    a whole category alike, so legacy typing keeps working.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [p for p in str(raw).replace(";", ",").split(",")]
    return [str(p).strip() for p in items if str(p).strip()]


def declared_keys(tenant_id: str | None = None) -> list[str]:
    """Canonical program-area keys declared by the tenant's ACTIVE users.

    Best-effort and cached for a minute: this runs inside scoring, and a declaration is
    not worth a query per call. Any failure returns [] - the profile alone then decides,
    which is the behaviour that existed before this signal was added.
    """
    key = str(tenant_id or "_session_")
    hit = _CACHE.get(key)
    if hit is not None and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]
    try:
        from db.supabase_client import get_client
        q = get_client().table("users").select("program").eq("is_active", True)
        rows = q.execute().data or []
    except Exception as exc:
        log.debug("user_program_areas: unavailable (%s)", exc)
        return []
    terms: list[str] = []
    for r in rows:
        terms.extend(_split(r.get("program")))
    keys: list[str] = []
    if terms:
        try:
            from core import program_area_classifier as _pa
            keys = sorted(_pa.expand(terms))
        except Exception as exc:                       # pragma: no cover
            log.debug("user_program_areas: could not canonicalise (%s)", exc)
            keys = []
    _CACHE[key] = (time.monotonic(), keys)
    return keys


def declared_scores(tenant_id: str | None = None) -> dict[str, float]:
    """{canonical key: DECLARED_RATING} for every area a user has declared."""
    return {k: DECLARED_RATING for k in declared_keys(tenant_id)}


def clear_cache() -> None:
    """Forget the cached declarations — call after editing a user's program areas."""
    _CACHE.clear()


__all__ = ["DECLARED_RATING", "declared_keys", "declared_scores", "clear_cache"]
