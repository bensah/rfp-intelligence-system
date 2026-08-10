"""Donor intelligence → classifier signals.

Matches an RFP's funder to the `donor_intel` table (verified, researched
metadata) and turns that donor's record into MUST/PREFER overrides — the
single strongest real-world evidence for eligibility.

Design notes
------------
* DATA lives in Postgres (private IP). This module is the open-source
  programmable INTERFACE — fork it and source your own intel into the fields.
* No Streamlit dependency: the scanner runs in a plain subprocess
  (scripts/run_scan.py), so we cache with a process-level TTL, not
  st.cache_data.
* BLANK = "not documented" (unknown) — NEVER coerced to "no". Every derivation
  returns None when the donor record doesn't decisively say, so the caller
  defers to the keyword scorer / default instead of penalising.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from db.supabase_client import get_client

_TTL = 300.0
_CACHE: dict[str, Any] = {"t": 0.0, "index": None}


# ---------------------------------------------------------------------------
# Money parsing — donor award ranges are formatted strings ("$3.00M",
# "$0.150M", "$50.00M", "$8.6B", "150k"). Recognise K / M / B suffixes.
# ---------------------------------------------------------------------------
_MONEY_RE = re.compile(r"([\d][\d,]*\.?\d*)\s*([kmb])?", re.IGNORECASE)
_SUFFIX = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}


def parse_money(s: Any) -> Optional[float]:
    """'$0.150M' -> 150000.0 ; '$50M' -> 50000000 ; '150k' -> 150000.
    Returns None for blank / unparseable (so it reads as unknown)."""
    if s is None:
        return None
    txt = str(s).replace("$", "").replace(",", "").strip()
    if not txt or txt.lower() in ("nan", "none", "n/a", "-"):
        return None
    m = _MONEY_RE.search(txt)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except (TypeError, ValueError):
        return None
    return num * _SUFFIX.get((m.group(2) or "").lower(), 1.0)


# ---------------------------------------------------------------------------
# Funder → donor record matching
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# Funder strings are stored as "ACRONYM <dash> Donor Name" ("BMGF - Gates Foundation").
# The dash is NOT reliably an ASCII hyphen: values typed in Word/Excel, pasted from a
# funder's own web page, or auto-corrected arrive with an EN DASH (U+2013) or EM DASH
# (U+2014) instead. Splitting on the ASCII form alone silently dropped the donor join for
# those rows — "BMGF – Gates Foundation" (en dash) resolved to NO donor record at all,
# which zeroed the donor side of the confidence band AND left every donor-imposed MUST-1 /
# MUST-5 component inactive, so the criteria read "Not sure" on a funder the org knows
# well. Accept the whole dash family (core.donor_enrich already did; this brings the
# matcher into line).
_FUNDER_SEP_RE = re.compile(r"\s[-‐‑‒–—―−]\s")


def split_funder_prefix(funder: Any) -> Optional[tuple[str, str]]:
    """('BMGF', 'Gates Foundation') for "BMGF <dash> Gates Foundation", for ANY dash
    variant. None when the string carries no acronym prefix."""
    raw = str(funder or "")
    m = _FUNDER_SEP_RE.search(raw)
    if not m:
        return None
    return raw[:m.start()].strip(), raw[m.end():].strip()


def _keys_for(row: dict) -> set[str]:
    keys: set[str] = set()
    for f in ("canonical_key", "donor", "donor_short"):
        k = _norm(row.get(f))
        if k:
            keys.add(k)
    for a in re.split(r"[;|]", row.get("donor_aliases") or ""):
        k = _norm(a)
        if len(k) >= 4:                       # ignore 1-3 char alias noise
            keys.add(k)
    return keys


def _load_index() -> dict[str, dict]:
    now = time.time()
    if _CACHE["index"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["index"]
    idx: dict[str, dict] = {}
    try:
        from db.supabase_client import safe_execute
        rows = safe_execute(get_client().table("donor_intel").select("*")).data or []
    except Exception:
        rows = []
    for r in rows:
        for k in _keys_for(r):
            idx.setdefault(k, r)
    _CACHE.update(t=now, index=idx)
    return idx


def match_donor(funder: Any, *, fuzzy: bool = True) -> Optional[dict]:
    """Best-effort match of an RFP funder string to a donor_intel row.

    Excel-migrated funders are stored as "ACRONYM - Donor Name" (e.g. "BMGF - Gates
    Foundation"), while donor_intel holds just the name ("Gates Foundation" / "Bill &
    Melinda Gates Foundation"). The leading acronym blocks a plain match, so we also
    try the name AFTER the first separator (and the acronym before it). This is
    why HAPPI's MUST-5 / PREFER-7 came back unmatched. The separator is matched as ANY
    dash variant (see `split_funder_prefix`) — an en/em dash used to defeat the split
    entirely and return no donor.

    `fuzzy` (default True) enables the last-resort ≥5-char SUBSTRING-containment fallback.
    Callers on the HARD-GATE / auto-Decline scoring path MUST pass fuzzy=False: a wrong
    substring match there injects the wrong donor's HQ / ceiling / priorities and can force
    a FALSE auto-Decline. Exact-key matching (name / short / acronym / the "ACR - Name"
    split) stays on for everyone — it's a strict superset of the old exact `.ilike`."""
    raw = str(funder or "")
    cands = [_norm(raw)]
    _split = split_funder_prefix(raw)
    if _split:
        acr, name = _split
        cands += [_norm(name), _norm(acr)]      # prefer the donor name, then the acronym
    cands = [c for c in dict.fromkeys(cands) if c]   # de-dup, drop blanks, keep order
    if not cands:
        return None
    idx = _load_index()
    for c in cands:                              # exact key hit on any candidate
        if c in idx:
            return idx[c]
    if not fuzzy:
        return None                              # gate path: never a fuzzy wrong match
    # Containment either direction over any candidate; prefer the longest matching key.
    best, best_len = None, 0
    for c in cands:
        for k, r in idx.items():
            if len(k) >= 5 and (k in c or c in k) and len(k) > best_len:
                best, best_len = r, len(k)
    return best


def clear_cache() -> None:
    _CACHE.update(t=0.0, index=None)


# Donor-record fields that declare the donor's funded geography. Read in priority
# order; the first that yields terms wins. Used as the SILENT-CALL fallback for the
# geo eligibility gate (owner rule: a geo-silent call is gated on the donor's declared
# geography, and passes permissively only when the donor is ALSO geo-silent).
_DONOR_GEO_FIELDS = ("donor_geographic_scope", "donor_funded_geographies",
                     "donor_pi_country_scope", "donor_registration_region")
_GLOBAL_GEO_RE = re.compile(
    r"\b(global|globally|worldwide|world[-\s]?wide|any country|all countries|"
    r"international(?:ly)?|multi[-\s]?country|multiple countries)\b", re.I)


def declared_geo(funder: Any) -> Optional[dict]:
    """The donor's DECLARED funded geography for `funder`, or None when the donor is
    unknown or states no geography (geo-silent). Returns
    ``{"terms": set[str canonical], "global": bool, "raw": str}`` where `global` is
    True when the donor funds worldwide / any country (→ any tenant qualifies).

    Matches strictly (fuzzy=False) — a wrong donor's geography must never drive a
    false reject on the gate path. Best-effort: any error → None (defer to the call's
    own geography / permissive)."""
    try:
        from core import geographies as _geo
        row = match_donor(funder, fuzzy=False)
        if not row:
            return None
        raw_parts: list[str] = []
        for f in _DONOR_GEO_FIELDS:
            v = row.get(f)
            if isinstance(v, (list, tuple)):
                raw_parts += [str(x) for x in v if str(x).strip()]
            elif v not in (None, ""):
                raw_parts.append(str(v))
        # An explicit global flag on the donor record is authoritative for "worldwide".
        is_global = str(row.get("donor_global_multi_country_scope") or "").strip().lower() in (
            "yes", "true", "global", "worldwide")
        raw = " ; ".join(raw_parts).strip()
        if not raw and not is_global:
            return None                          # donor is geo-silent → caller stays permissive
        if _GLOBAL_GEO_RE.search(raw):
            is_global = True
        # Canonicalise each declared term (splits on common separators); keep only ones
        # the geo library recognises as a country/region so noise doesn't gate.
        terms: set[str] = set()
        for chunk in re.split(r"[;,/|]| and | & |\n", raw):
            c = _geo.canonical_geo(chunk.strip())
            if c and c.lower() not in ("", "global / worldwide"):
                terms.add(c)
        if _geo.canonical_geo(raw) == "Global / worldwide":
            is_global = True
        if not terms and not is_global:
            return None
        return {"terms": terms, "global": is_global, "raw": raw}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Derivations (each returns Yes / Partial / No / None; None = defer)
# ---------------------------------------------------------------------------
def _yes(row: dict, field: str) -> bool:
    return str(row.get(field) or "").strip().lower() == "yes"


def funding_quality(row: dict, *, false_below: float = 50_000,
                    partial_below: float = 100_000) -> Optional[str]:
    """Typical funding tier from the donor's award range — the PREFER 6
    fallback when the RFP itself publishes no amount. Uses the award ceiling
    (what the donor CAN fund); falls back to the floor."""
    hi = parse_money(row.get("donor_award_high")) or parse_money(row.get("donor_award_low"))
    if hi is None:
        return None
    if hi >= partial_below:
        return "Yes"
    if hi >= false_below:
        return "Partial"
    return "No"


def compliance(row: dict, *, org_has_local_board: Optional[str] = None) -> Optional[str]:
    """MUST 4 from donor requirement flags + the org's structural profile."""
    # Hard disqualifier: donor needs a local board, org doesn't have one.
    if _yes(row, "donor_local_board_required") and (org_has_local_board or "").lower() == "no":
        return "No"
    pf = str(row.get("donor_prefinance_required") or "").strip().lower()
    if pf == "reimbursement_only":
        return "Partial"      # we must pre-finance — cash-flow risk
    if pf in ("none", "partial"):
        return "Yes"          # advance available
    return None               # nothing decisive → defer


def partnership(row: dict) -> Optional[str]:
    """PREFER 8 from the donor's partnership requirement."""
    pm = str(row.get("donor_partnership_mandatory") or "").strip().lower()
    if pm == "yes":
        return "No"           # must partner / consortium required
    if pm == "no":
        return "Yes"          # can apply alone or partner optionally
    return None


# Health / program-area fit columns in the donor matrix (legacy flags — still
# honoured, but program areas are now primarily captured in priority_program_areas
# against the shared taxonomy in core.program_area_classifier).
_HEALTH_FIT_FIELDS = (
    "donor_infectious_diseases_fit", "donor_hiv_aids_fit", "donor_tb_fit", "donor_malaria_fit",
    "donor_immunization_vaccines_fit", "donor_mnch_fit", "donor_srhr_family_planning_fit",
    "donor_nutrition_fit", "donor_ncds_fit", "donor_hss_fit", "donor_digital_health_data_ai_fit",
)
# Taxonomy category PREFIXES that count as "health" for MUST-2 alignment.
_HEALTH_CATEGORY_PREFIXES = ("WCH", "NCDs", "IDs", "HSS", "Cross-cutting")
_HEALTH_CATEGORY_NAMES = (
    "Women & Children's Health", "Non-Communicable Diseases",
    "Infectious Diseases", "Health System Strengthening", "Cross-cutting (Health)",
)


def _has_health_program_area(row: dict) -> bool:
    """True if priority_program_areas contains any health-category key/name."""
    raw = row.get("donor_priority_areas")
    if not raw:
        return False
    txt = str(raw)
    if any(name in txt for name in _HEALTH_CATEGORY_NAMES):
        return True
    return any(re.search(rf"\b{p}\s*-", txt) for p in _HEALTH_CATEGORY_PREFIXES)


def program_alignment(row: dict) -> tuple[Optional[str], Optional[str]]:
    """Derive (MUST-1 govt alignment, MUST-2 strategic fit) from the matched
    donor's program areas + geographic focus. UPGRADE-ONLY: returns 'Yes' or
    None (never 'No'), so a verified, on-mission donor lifts these criteria but
    a thin keyword read is never penalised by the matrix. None = defer."""
    health_fit = (any(_yes(row, f) for f in _HEALTH_FIT_FIELDS)
                  or _has_health_program_area(row))
    lmic = _yes(row, "donor_lmic_africa_focus") or _yes(row, "donor_global_multi_country_scope")
    must1 = must2 = None
    if health_fit:
        must2 = "Yes"             # donor funds in our health program areas
        if lmic:
            must1 = "Yes"         # LMIC/global health funding aligns w/ national priorities
    return must1, must2


def apply_to_values(values: dict, candidate: dict, policies: dict) -> Optional[dict]:
    """ALL criterion overrides are now DISABLED — the dedicated active-only
    derivations in core.criteria_derive are authoritative for every criterion. This
    remains only to return the matched donor row for provenance (callers record it).

    History: the MUST-1 / MUST-2 overrides were disabled 2026-06-28; the MUST-4 /
    PREFER-8 / PREFER-6 overrides were disabled 2026-06-29 once those criteria were
    reworked under the active-only model (owner's standing rule: retire each donor_intel
    override as its criterion is rebuilt, since the heuristic would otherwise CONFLICT
    with the reworked derivation — e.g. force geographic_fit past the geo-scope/US-only/
    'Not sure' logic, or funding_quality past its 'Not sure' default)."""
    row = match_donor(candidate.get("funding_agency"))
    if not row:
        return None

    # geographic_fit (MUST-4), competitiveness (PREFER-8) and the funding_quality
    # (PREFER-6) no-amount fallback are NO LONGER overridden here — see docstring.
    # (compliance() / partnership() / funding_quality() helpers are kept for any
    # provenance/diagnostic readers but are not applied to `values`.)
    return row


def _rfp_amount(candidate: dict) -> float:
    raw = candidate.get("call_award_value")
    try:
        return float(raw) if raw not in (None, "", 0) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _tier_thresholds(tiers_conf: dict) -> tuple[float, float]:
    """Pull (false_below, partial_below) from the funding_quality_tiers
    config; falls back to 50k / 100k."""
    false_below, partial_below = 50_000.0, 100_000.0
    try:
        vals = sorted(float(t["threshold_usd"]) for t in tiers_conf.get("tiers", [])
                      if float(t.get("threshold_usd", 0)) > 0)
        if len(vals) >= 2:
            false_below, partial_below = vals[0], vals[1]
        elif len(vals) == 1:
            partial_below = vals[0]
    except (TypeError, ValueError, KeyError):
        pass
    return false_below, partial_below
