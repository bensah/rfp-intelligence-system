"""E3 — donor-intel auto-enrichment from calls (conservative + provenance-tracked).

The platform learns donors FROM calls. This module is the foundation:

  ensure_donor(funder) → resolve a call's funder to a donor_intel canonical_key,
  auto-creating a STUB row when the funder is a clearly-named, on-theme, NON-generic
  funder not yet in the matrix — so contacts/signals from the call have a donor to
  attach to (the missing piece behind E1's contacts no-op). CONSERVATIVE by choice
  (owner 2026-06-30): generic strings like "UN (UNGM)" are never auto-created.

Provenance (donor_intel.field_provenance jsonb, migration 064) keeps auto-derived
values distinct from human-verified ones:
  PROV_HUMAN  a human set/confirmed it (source of truth — never auto-overwritten)
  PROV_CALL   auto-filled from a call's extracted signal (shown as "suggested")
  PROV_AUTO   the donor stub itself was auto-created

Everything is best-effort: never overwrites an existing donor, never raises into a scan.
The WIRING (contacts-stub on ensure_donor, requirement enrichment from call compliance
signals, form 'suggested' display + human-verified marking on Save) builds on top of
this once E1/E2 land.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

PROV_HUMAN = "human_verified"
PROV_CALL = "from_call"
PROV_AUTO = "auto_created"

# Generic / non-identifying funder strings we must NOT turn into a donor record.
_GENERIC_FUNDER_RE = re.compile(
    r"^(?:un|u\.n\.|unknown|n/?a|tbd|tba|various|multiple|misc\.?|donor|funder|agency|"
    r"government|other|the\s+government|un\s*\(ungm\)|un\s*[—-]\s*ungm|"
    r"united\s+nations\s*\(ungm\))$", re.I)


def _split_funder(funder: Any) -> tuple[str, str | None]:
    """('BMGF - Gates Foundation') → ('Gates Foundation', 'BMGF'); ('UN — UNICEF') →
    ('UNICEF', None); ('IDRC') → ('IDRC', None). Returns (donor_name, acronym|None)."""
    raw = re.sub(r"\s+", " ", str(funder or "").strip())
    acronym = None
    for sep in (" - ", " — ", " – "):
        if sep in raw:
            a, b = (x.strip() for x in raw.split(sep, 1))
            # The acronym is the SHORT, mostly-caps side; the name is the rest.
            if a and len(re.sub(r"[^A-Za-z]", "", a)) <= 6 and a.upper() == a:
                acronym, raw = a, (b or a)
            else:
                raw = b or a
            break
    return raw.strip(), acronym


def _is_namable(funder: Any) -> bool:
    """Conservative gate: a real, identifying donor name (not a generic placeholder)."""
    name, _ = _split_funder(funder)
    if len(name) < 4 or not re.search(r"[A-Za-z]", name):
        return False
    return not _GENERIC_FUNDER_RE.match(name.strip())


def _slug(name: str) -> str:
    """canonical_key slug — MUST match app_pages/donors.py (the add-donor path)."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_") or "donor"


def ensure_donor(funder: Any, *, on_theme: bool = True) -> str | None:
    """canonical_key for a call's funder, auto-creating a conservative STUB if it's a
    namable, on-theme funder not yet in the matrix. Returns None when not creatable
    (generic funder, off-theme, or the DB is unavailable). Never overwrites an existing
    donor; never raises."""
    try:
        from core.donor_intel import match_donor
        d = match_donor(funder)
        if d:
            return d.get("canonical_key")
    except Exception:
        return None
    if not on_theme or not _is_namable(funder):
        return None
    name, acronym = _split_funder(funder)
    try:
        from db.supabase_client import get_client, safe_execute
        sb = get_client()
        existing = {r.get("canonical_key") for r in (safe_execute(
            sb.table("donor_intel").select("canonical_key")) or [])}
        base = _slug(name)
        key, i = base, 2
        while key in existing:
            key, i = f"{base}_{i}", i + 1
        aliases = "; ".join(dict.fromkeys(x for x in (acronym, name) if x))
        sb.table("donor_intel").insert({
            "canonical_key": key,
            "donor": name,
            "donor_short": acronym or None,
            "donor_aliases": aliases,
            "field_provenance": json.dumps({"_meta": PROV_AUTO}),
        }).execute()
        try:
            from core.donor_intel import clear_cache
            clear_cache()
        except Exception:
            pass
        log.info("E3 auto-created donor stub %r (key=%s) from call funder %r", name, key, funder)
        return key
    except Exception as exc:
        log.debug("ensure_donor failed for %r: %s", funder, exc)
        return None


def mark_human_verified(field_provenance: Any, fields) -> dict:
    """Mark the given fields as human-verified in a field_provenance dict (called when a
    human Saves the donor form). Fields they didn't touch keep their from_call/auto tag."""
    fp = dict(field_provenance) if isinstance(field_provenance, dict) else {}
    for f in fields:
        if f:
            fp[str(f)] = PROV_HUMAN
    return fp


def _requirement_updates(candidate: dict) -> dict:
    """BLANK-fill updates for a donor's '*_required' compliance columns, derived from a
    call's call_compliance_flags. Pure (no DB). A flag the call EXPLICITLY imposes → the
    matching donor requirement column set 'yes'. Silence never writes 'Not Required'
    (absence of a signal is not evidence). {} when there's nothing to suggest."""
    flags = candidate.get("call_compliance_flags")
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except Exception:
            flags = None
    if not isinstance(flags, dict) or not flags:
        return {}
    try:
        from core.criteria_derive import _eff_column
    except Exception:
        return {}
    updates: dict[str, str] = {}
    for k, v in flags.items():
        if not v:
            continue
        col = _eff_column(k)
        if col.endswith("_required"):
            updates[col] = "yes"
    return updates


def _profile_updates(candidate: dict) -> dict:
    """BLANK-fill updates for a donor's PROFILE columns (geographic scope, LMIC/Africa
    focus, global/multi-country scope, priority program areas, source URL), derived from
    a call. Pure (no DB). Award range is intentionally NOT written — one call's budget is
    a poor proxy for a donor's typical award tier and would mislead PREFER-6."""
    updates: dict[str, str] = {}
    scope = candidate.get("call_geographic_scope")
    scope_list = [str(s).strip() for s in
                  (scope if isinstance(scope, (list, tuple)) else [scope] if scope else [])
                  if str(s).strip()]
    if scope_list:
        updates["donor_geographic_scope"] = "; ".join(dict.fromkeys(scope_list))
        blob = " ".join(scope_list).lower()
        if re.search(r"africa|sub-?saharan|\bssa\b|lmic|low-\s*and\s*middle|"
                     r"least\s+developed|\bldc\b|global\s+south", blob):
            updates["donor_lmic_africa_focus"] = "yes"
        if len(scope_list) > 1 or re.search(
                r"global|worldwide|multi-?country|lmic|global\s+south", blob):
            updates["donor_global_multi_country_scope"] = "yes"
    text = " ".join(str(candidate.get(k) or "") for k in
                    ("opportunity_title", "brief_description", "raw_text", "_page_text"))
    try:
        from core.program_area_classifier import classify_program_areas, UNSPECIFIED
        areas = [a for a in classify_program_areas(text) if a != UNSPECIFIED]
    except Exception:
        areas = []
    if areas:
        updates["donor_priority_areas"] = "; ".join(dict.fromkeys(areas))
    link = candidate.get("opportunity_link")
    if link:
        updates["donor_source_urls"] = str(link)
    return updates


def enrich_donor_from_call(candidate: dict) -> int:
    """Fill a donor's BLANK requirement + profile fields from a call in ONE ensure_donor
    lookup + ONE persist (round-trip-efficient — preferred over calling the two enrichers
    back-to-back). from_call provenance, blank-only, never overwrites human/non-blank.
    Auto-creates the donor conservatively if needed. Returns #fields filled. Best-effort."""
    key = ensure_donor(candidate.get("funding_agency"))
    if not key:
        return 0
    updates = {**_requirement_updates(candidate), **_profile_updates(candidate)}
    if not updates:
        return 0
    n = _persist_call_fill(key, updates)
    if n:
        log.info("E3 enriched donor %s with %d call-derived field(s)", key, n)
    return n


def enrich_donor_requirements_from_call(candidate: dict) -> int:
    """Compliance-requirement-only enricher (see _requirement_updates). Kept for callers
    that only have compliance flags; enrich_donor_from_call is preferred otherwise."""
    updates = _requirement_updates(candidate)
    if not updates:
        return 0
    key = ensure_donor(candidate.get("funding_agency"))
    if not key:
        return 0
    n = _persist_call_fill(key, updates)
    if n:
        log.info("E3 enriched donor %s with %d call-derived requirement(s)", key, n)
    return n


def enrich_donor_profile_from_call(candidate: dict) -> int:
    """Profile-only enricher (see _profile_updates). enrich_donor_from_call is preferred
    when both requirement + profile signals may be present (single round-trip)."""
    updates = _profile_updates(candidate)
    if not updates:
        return 0
    key = ensure_donor(candidate.get("funding_agency"))
    if not key:
        return 0
    n = _persist_call_fill(key, updates)
    if n:
        log.info("E3 enriched donor %s profile with %d call-derived field(s)", key, n)
    return n


def _persist_call_fill(key: str, updates: dict) -> int:
    """Fill-BLANK-only upsert of call-derived `updates` onto donor `key`, tagging each
    filled field PROV_CALL in field_provenance. Returns #fields filled. Shared by the
    requirement + profile enrichers. Best-effort; never raises."""
    try:
        from db.supabase_client import get_client, safe_execute
        sb = get_client()
        rows = safe_execute(
            sb.table("donor_intel").select("*").eq("canonical_key", key)) or []
        donor = rows[0] if rows else {}
        patch, prov = fill_blank_from_call(donor, updates)
        if not patch:
            return 0
        fp = donor.get("field_provenance")
        fp = dict(fp) if isinstance(fp, dict) else {}
        fp.update(prov)
        patch["canonical_key"] = key
        patch["field_provenance"] = json.dumps(fp)
        sb.table("donor_intel").upsert(patch, on_conflict="canonical_key").execute()
        try:
            from core.donor_intel import clear_cache
            clear_cache()
        except Exception:
            pass
        return len(prov)
    except Exception as exc:
        log.debug("_persist_call_fill failed for %s: %s", key, exc)
        return 0


def fill_blank_from_call(donor: dict, updates: dict) -> tuple[dict, dict]:
    """Compute a fill-BLANK-only patch + provenance bumps for a donor from call-derived
    `updates`. NEVER overwrites a field a human verified or any non-blank value; only
    fills blanks, tagging them PROV_CALL. Returns (patch, provenance_patch). Pure — the
    caller decides whether/how to persist (wired in a later E3 step)."""
    fp = donor.get("field_provenance")
    fp = fp if isinstance(fp, dict) else {}
    patch, prov = {}, {}
    for field, val in (updates or {}).items():
        if val in (None, "", []):
            continue
        if fp.get(field) == PROV_HUMAN:
            continue                                   # human is the source of truth
        cur = donor.get(field)
        if cur in (None, "", []):                      # fill blanks only
            patch[field] = val
            prov[field] = PROV_CALL
    return patch, prov
