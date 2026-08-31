"""Applicant graph — the profiles that legitimately back one scoring decision.

The scorer reads ONE tenant's profile. But a Sub inherits the Prime's prime-facing
eligibility, a child inherits its parent org's standing, and consortium members share
relationships. This module resolves, for one RFP + active tenant, the ordered set of
profiles each transfer-class of criteria may consult — self first (own standing is
strongest), then parent / prime / co-subs, per docs/TENANT_GRAPH_SCORING_DESIGN.md §4.

Two rules govern it:
  * Consulting another profile can only RAISE a score, never lower it. A named applicant
    that is not a resolvable, ACTIVE, CONSENTED tenant contributes nothing — the
    Sub-registration soft floor in criteria_derive handles the resulting "unclear" case.
  * Parent<->child is authorized by the ownership link (`tenants.parent_tenant_id`);
    a co-applicant is consulted ONLY if it set `share_for_consortium_scoring = true`.

The projection to `_WHITELIST` is the ONE boundary where another tenant's data enters a
score: transfer-eligible standing only, never strategy / capacity / co-financing / bid
effort / identity / contacts. The cross-tenant READ lives in `resolve()` alone; all the
LOGIC lives in the pure, DB-free `build_graph()` so it is testable without a database.

Fail-closed: any resolution error yields a SELF-ONLY graph — scoring proceeds on the
active tenant alone and no cross-tenant data leaks.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Fields a CONSULTED (parent / co-applicant) profile may expose to scoring. Nothing else
# crosses the tenant boundary. Kept in sync with the §4 transfer classes.
_WHITELIST = (
    "org_registered_countries", "org_operating_countries",
    "org_authorized_signatory_donors", "org_donor_registrations",
    "org_funder_history", "org_active_donors", "org_engaged_donors",
    "trusted_partners", "partners",
    "org_domain_expertise", "org_domain_ratings", "org_founding_year",
)


def _project(profile: dict | None) -> dict:
    """A consulted profile reduced to the whitelist (empty values dropped)."""
    p = profile or {}
    return {k: p.get(k) for k in _WHITELIST if p.get(k) not in (None, [], {}, "")}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


@dataclass(frozen=True)
class ApplicantGraph:
    """Resolved profiles for one (rfp, active tenant). `self_` is always the full active
    profile; parent/prime/cosubs are WHITELISTED projections (or None/empty)."""
    self_: dict
    parent: dict | None = None
    prime: dict | None = None
    cosubs: tuple[dict, ...] = ()
    role: str = ""                       # rfp.applicant_role, lowercased
    unresolved_prime: bool = False       # Sub with a Prime named but not a consented tenant

    @staticmethod
    def _chain(*nodes) -> list[dict]:
        out: list[dict] = []
        for n in nodes:
            if isinstance(n, (list, tuple)):
                out.extend(x for x in n if x)
            elif n:
                out.append(n)
        return out

    # Ordered profile lists per §4 (self first — own standing is strongest). Consumers
    # in criteria_derive OR a predicate across the list; the first satisfying wins.
    def for_registration(self) -> list[dict]:
        return self._chain(self.self_, self.parent, self.prime)

    def for_hq(self) -> list[dict]:
        return self._chain(self.self_, self.prime)

    def for_relationships(self) -> list[dict]:
        return self._chain(self.self_, self.parent, self.prime, self.cosubs)

    def for_signatory(self) -> list[dict]:
        return self.for_relationships()

    def for_competitiveness(self) -> list[dict]:
        return self._chain(self.self_, self.parent)

    def for_geographic(self) -> list[dict]:
        return self._chain(self.self_, self.prime, self.cosubs, self.parent)


def _name_index(rows: list[dict], exclude_id: str | None) -> list[tuple[set[str], dict]]:
    """(normalised-name-set, row) per tenant, excluding the active tenant."""
    idx: list[tuple[set[str], dict]] = []
    for r in rows:
        if exclude_id and str(r.get("id")) == exclude_id:
            continue
        ident = r.get("org_identity") if isinstance(r.get("org_identity"), dict) else {}
        names = [r.get("name"), r.get("slug"),
                 ident.get("org_name"), ident.get("org_short"), ident.get("name")]
        norms = {_norm(n) for n in names if n and _norm(n)}
        if norms:
            idx.append((norms, r))
    return idx


def _match(piece: str, idx: list[tuple[set[str], dict]]) -> dict | None:
    """Resolve one applicant-name piece to a tenant row. Exact normalised match first;
    an acronym ↔ full-name match, or ≥4-char containment, as fallback."""
    q = _norm(piece)
    if not q:
        return None
    for norms, r in idx:                       # 1) exact normalised name / slug / short
        if q in norms:
            return r
    try:
        from core.partner_names import is_acronym_of
    except Exception:
        is_acronym_of = None
    for norms, r in idx:                       # 2) acronym / substring (both ≥4 chars)
        for n in norms:
            if not n:
                continue
            if (is_acronym_of and is_acronym_of(piece, n)) \
                    or (len(q) >= 4 and len(n) >= 4 and (q in n or n in q)):
                return r
    return None


def _resolve_cell(cell, idx: list[tuple[set[str], dict]]) -> list[tuple[str, str, dict | None]]:
    """Each applicant-name piece → (status, piece, projected_profile|None).
    status ∈ ok · no_consent · inactive · unresolved."""
    try:
        from core.partner_names import split_pieces
        pieces = split_pieces(cell)
    except Exception:
        pieces = [p.strip() for p in str(cell or "").split(",") if p.strip()]
    out: list[tuple[str, str, dict | None]] = []
    for piece in pieces:
        row = _match(piece, idx)
        if row is None:
            out.append(("unresolved", piece, None))
        elif str(row.get("status") or "active").strip().lower() != "active":
            out.append(("inactive", piece, None))
        elif not row.get("share_for_consortium_scoring"):
            out.append(("no_consent", piece, None))
        else:
            out.append(("ok", piece, _project(row.get("org_profile"))))
    return out


def build_graph(rfp: dict, org: dict, self_tenant_id: str | None,
                rows: list[dict]) -> ApplicantGraph:
    """PURE (no DB): assemble the graph from the active profile, the active tenant id,
    and the full active-tenant row set. `rows` items carry id, name, slug, status,
    parent_tenant_id, share_for_consortium_scoring, org_profile (dict), org_identity."""
    org = org or {}
    role = str((rfp or {}).get("applicant_role") or "").strip().lower()
    by_id = {str(r["id"]): r for r in rows if r.get("id")}
    self_id = str(self_tenant_id) if self_tenant_id else None

    # Parent — the ownership link, no consent required.
    parent = None
    if self_id and self_id in by_id:
        pid = by_id[self_id].get("parent_tenant_id")
        if pid and str(pid) in by_id:
            parent = _project(by_id[str(pid)].get("org_profile")) or None

    idx = _name_index(rows, exclude_id=self_id)   # self never resolves as its own co-applicant

    lead = _resolve_cell((rfp or {}).get("lead_applicant"), idx)
    ok_leads = [p for st, _, p in lead if st == "ok" and p]
    prime = ok_leads[0] if ok_leads else None
    named_lead = bool(lead)
    unresolved_prime = (role == "sub" and named_lead and not ok_leads)

    cosubs = tuple(p for st, _, p in _resolve_cell((rfp or {}).get("sub_applicant"), idx)
                   if st == "ok" and p)

    return ApplicantGraph(self_=org, parent=parent, prime=prime, cosubs=cosubs,
                          role=role, unresolved_prime=unresolved_prime)


# --- DB-backed resolution (the one place another tenant is read) ---------------------
_ROWS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ROWS_TTL = 30.0        # seconds — the tenant set changes rarely; a crawl scores many RFPs


def _normalize_overlay(v) -> dict:
    import json
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            v = {}
    if not isinstance(v, dict):
        return {}
    try:
        from core.org_profile import _migrate_keys
        return _migrate_keys(v)
    except Exception:
        return v


def _load_active_tenant_rows() -> list[dict]:
    """Every non-blacklisted tenant, normalised. Cached briefly (crawl scores in bulk)."""
    hit = _ROWS_CACHE.get("rows")
    if hit is not None and (time.monotonic() - hit[0]) < _ROWS_TTL:
        return hit[1]
    from db.supabase_client import service_client
    raw = (service_client().table("tenants")
           .select("id,name,slug,status,parent_tenant_id,"
                   "share_for_consortium_scoring,org_profile,org_identity")
           .neq("status", "blacklisted").execute().data or [])
    out = []
    for r in raw:
        r = dict(r)
        r["org_profile"] = _normalize_overlay(r.get("org_profile"))
        r["org_identity"] = _normalize_overlay(r.get("org_identity"))
        out.append(r)
    _ROWS_CACHE["rows"] = (time.monotonic(), out)
    return out


def _current_tenant_id() -> str | None:
    try:
        from auth import tenant_context as tc
        store = tc.tenant_store(None)
        return store[1] if store else None
    except Exception:
        return None


def resolve(rfp: dict, org: dict, tenant_id: str | None = None) -> ApplicantGraph:
    """Build the applicant graph for one RFP + the active tenant. Fail-closed: any error
    (no DB, resolution failure) returns a SELF-ONLY graph — never raises, never leaks."""
    self_node = org or {}
    role = str((rfp or {}).get("applicant_role") or "").strip().lower()
    try:
        tid = tenant_id or _current_tenant_id()
        rows = _load_active_tenant_rows()
        return build_graph(rfp or {}, self_node, tid, rows)
    except Exception:
        return ApplicantGraph(self_=self_node, role=role)


def clear_cache() -> None:
    """Drop the cached tenant rows (call after a tenant is created / linked / consented)."""
    _ROWS_CACHE.clear()
