"""Source registry — the learning log of hosts the scanner meets.

One row per HOST (normalized netloc), classified aggregator / primary / blog /
listing / unknown (migration 034). The detector (core.aggregators) reads
CONFIRMED rows as authoritative; the scanner records every encounter so a human
can review new hosts once and confirm the classification.

Reads are cached (process-level TTL — the scanner is a plain subprocess, no
Streamlit). Writes are batched at end of scan and fully best-effort: if the table
is missing (migration 034 not run) or the DB blips, everything no-ops and the
scan is unaffected.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from db.supabase_client import get_client

log = logging.getLogger(__name__)

_TABLE = "source_registry"
_TTL = 300.0
_CACHE: dict = {"t": 0.0, "rows": None}

VALID_CLASS = ("aggregator", "primary", "blog", "listing", "unknown")


def normalize_host(url: str | None) -> str:
    """Lowercased netloc with a leading 'www.' stripped. Subdomains kept (so
    'grants-gov.blogspot.com' stays distinct and blog-platform suffix matching
    still works)."""
    if not url:
        return ""
    try:
        net = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    except Exception:
        return ""
    return net[4:] if net.startswith("www.") else net


def get_all(force: bool = False) -> dict[str, dict]:
    """{host: row} for every registry entry. Cached; [] on any error."""
    now = time.time()
    if not force and _CACHE["rows"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["rows"]
    rows: dict[str, dict] = {}
    try:
        data = (get_client().table(_TABLE)
                .select("host,classification,status,hits").execute().data or [])
        rows = {(r.get("host") or ""): r for r in data if r.get("host")}
    except Exception as exc:
        log.debug("source_registry.get_all unavailable: %s", exc)
        rows = {}
    _CACHE.update(t=now, rows=rows)
    return rows


def confirmed_class(host: str) -> str | None:
    """The human-confirmed classification for a host, or None if not confirmed.
    Confirmed rows are authoritative — the detector defers to them."""
    r = get_all().get(host)
    if r and (r.get("status") or "").lower() == "confirmed":
        c = (r.get("classification") or "").lower()
        return c if c in VALID_CLASS else None
    return None


def clear_cache() -> None:
    _CACHE.update(t=0.0, rows=None)


# Pre-037 columns (always present) + the 037 additions (opportunity_types /
# donor_name / donor_code). list_rows tries the full set, falls back to core so
# the registry stays viewable before migration 037 runs.
_CORE_COLS = ("source_uid,host,classification,status,source_class,access_model,"
              "ingestion_method,has_api,detected_as,hits,"
              "sample_url,sample_title,last_seen,verified_by")
_EXTRA_COLS = (",opportunity_types,solicitation_types,instrument_types,"
               "donor_name,donor_code,in_catalogue,listings_url,notes,"
               "verified_at")


def list_rows() -> list[dict]:
    """Full registry rows (for the admin review UI). [] on any error. Falls back
    to the core columns if the migration-037 columns aren't present yet, so the
    registry stays viewable in the meantime."""
    sb = get_client()
    # Degrade gracefully if a column isn't migrated yet: full → drop listings_url
    # (migration 061) but keep the 037 extras → bare core. Keeps the table usable
    # during a migration lag.
    for cols in (_CORE_COLS + _EXTRA_COLS,
                 _CORE_COLS + _EXTRA_COLS.replace(",listings_url", ""),
                 _CORE_COLS):
        try:
            return (sb.table(_TABLE).select(cols)
                    .order("hits", desc=True).limit(5000).execute().data or [])
        except Exception:
            continue
    log.debug("source_registry.list_rows failed for all column sets")
    return []


_EDITABLE = ("classification", "status", "source_class", "access_model",
             "ingestion_method", "has_api", "opportunity_types",
             "solicitation_types", "instrument_types", "sample_url",
             "listings_url", "notes", "donor_name", "donor_code")


def update_row(host: str, fields: dict, by: str | None = None) -> bool:
    """Human edit of any taxonomy field(s) on a host (registry review UI).
    Whitelisted columns only; stamps verifier. Best-effort."""
    upd = {k: v for k, v in (fields or {}).items() if k in _EDITABLE}
    if not host or not upd:
        return False
    try:
        import datetime as _dt
        upd["verified_by"] = by
        upd["verified_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        get_client().table(_TABLE).update(upd).eq("host", host).execute()
        clear_cache()
        return True
    except Exception as exc:
        log.debug("source_registry.update_row failed: %s", exc)
        return False


def add_row(host_or_url: str, fields: dict, by: str | None = None
            ) -> tuple[bool, str]:
    """Manually add (or update) a host — the registry 'Add source' button.
    Accepts a URL or bare host and normalises to host. Upserts (re-adding an
    existing host updates it). Manual adds default to status='confirmed'
    (human-authoritative). Returns (ok, message). Best-effort."""
    host = normalize_host(host_or_url) or (host_or_url or "").strip().lower()
    if not host:
        return False, "Enter a host or URL."
    allowed = set(_EDITABLE) | {"notes", "sample_url", "sample_title"}
    row = {k: v for k, v in (fields or {}).items() if k in allowed and v not in (None, "")}
    row["host"] = host
    row.setdefault("status", "confirmed")
    try:
        import datetime as _dt
        row["verified_by"] = by
        row["verified_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        get_client().table(_TABLE).upsert(row, on_conflict="host").execute()
        clear_cache()
        return True, f"Saved {host}."
    except Exception as exc:
        log.debug("source_registry.add_row failed: %s", exc)
        return False, str(exc)[:160]


def set_classification(host: str, classification: str, *,
                       status: str = "confirmed", by: str | None = None) -> bool:
    """Human edit: set a host's classification + status (status defaults to
    'confirmed' = authoritative). Best-effort."""
    classification = (classification or "").lower().strip()
    if not host or classification not in VALID_CLASS:
        return False
    try:
        import datetime as _dt
        get_client().table(_TABLE).update({
            "classification": classification, "status": status,
            "verified_by": by, "verified_at":
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }).eq("host", host).execute()
        clear_cache()
        return True
    except Exception as exc:
        log.debug("source_registry.set_classification failed: %s", exc)
        return False


def delete_hosts(hosts: list[str]) -> int:
    """Delete registry rows by host. Returns count attempted; 0 on error."""
    hosts = [h for h in (hosts or []) if h]
    if not hosts:
        return 0
    try:
        get_client().table(_TABLE).delete().in_("host", hosts).execute()
        clear_cache()
        return len(hosts)
    except Exception as exc:
        log.debug("source_registry.delete_hosts failed: %s", exc)
        return 0


def _method_for(ingestion_method: str | None) -> str:
    """Map registry 'Best ingestion method' → donor_sources.scrape_method (the
    technical value the scan dispatches on; CHECK = html|html_js|rss|rest_json|manual)."""
    im = (ingestion_method or "").lower()
    if "api" in im:
        return "rest_json"
    if "rss" in im or "feed" in im or "newsletter" in im:
        return "rss"
    if "js" in im or "dynamic" in im or "playwright" in im:
        return "html_js"
    if "manual" in im or "licensed" in im or "linked" in im:
        return "manual"
    return "html"


def push_primaries(hosts: list[str], by: str | None = None) -> dict:
    """UPSERT confirmed-primary registry hosts into the donor_sources catalogue
    (the scan list). Deduped by HOST: an existing catalogue row for the same host
    is UPDATED in place with the registry's current values (listing URL, method,
    opportunity_types, access, source class, donor/code); a new host is INSERTED.
    Returns {added:[...], updated:[...], skipped:[(host,reason)], error}."""
    hosts = [h for h in (hosts or []) if h]
    if not hosts:
        return {"added": [], "updated": [], "skipped": [], "error": None}
    try:
        sb = get_client()
        reg = {r.get("host"): r for r in list_rows()}
        existing = (sb.table("donor_sources")
                    .select("id,rfp_listing_url,base_url").execute().data or [])
        # host -> existing catalogue row id (first match wins)
        by_host: dict[str, str] = {}
        for e in existing:
            for u in (e.get("rfp_listing_url"), e.get("base_url")):
                h = normalize_host(u)
                if h and h not in by_host:
                    by_host[h] = e.get("id")
        inserts, added, updated, skipped = [], [], [], []
        for host in hosts:
            r = reg.get(host)
            if not r or r.get("classification") != "primary" \
                    or (r.get("status") or "").lower() != "confirmed":
                skipped.append((host, "not a confirmed primary"))
                continue
            fields = {
                # Harmonised registry → catalogue (single source of truth).
                "host": host,                        # required — insert fails without it
                "donor_name": r.get("donor_name") or host,
                "donor_code": r.get("donor_code"),
                "base_url": f"https://{host}/",
                "rfp_listing_url": (r.get("listings_url") or r.get("sample_url")
                                    or f"https://{host}/"),
                "scrape_method": _method_for(r.get("ingestion_method")),
                "access_model": r.get("access_model"),
                "source_class": r.get("source_class"),
                "opportunity_types": r.get("opportunity_types") or None,
                "solicitation_types": r.get("solicitation_types") or None,
                "instrument_types": r.get("instrument_types") or None,
                "is_active": True,
            }
            if host in by_host:                      # UPDATE existing in place
                try:
                    sb.table("donor_sources").update(fields).eq(
                        "id", by_host[host]).execute()
                    updated.append(host)
                except Exception as exc:
                    skipped.append((host, f"update failed: {str(exc)[:60]}"))
            else:                                    # INSERT new
                fields["notes"] = (f"seeded from source_registry — "
                                   f"{r.get('sample_title') or ''}")[:300]
                fields["created_by"] = by or "source_registry"
                inserts.append(fields)
                added.append(host)
                by_host[host] = "pending"            # avoid dup-insert within batch
        if inserts:
            sb.table("donor_sources").insert(inserts).execute()
        # Mark pushed hosts as present in the catalogue so future pushes skip
        # them (no duplicates across registry ↔ catalogue). Best-effort — needs
        # migration 040; silently ignored if the column isn't there yet.
        pushed = added + updated
        if pushed:
            try:
                sb.table(_TABLE).update({"in_catalogue": True}).in_(
                    "host", pushed).execute()
                clear_cache()
            except Exception:
                pass
        return {"added": added, "updated": updated, "skipped": skipped,
                "error": None}
    except Exception as exc:
        log.debug("source_registry.push_primaries failed: %s", exc)
        return {"added": [], "updated": [], "skipped": [], "error": str(exc)[:200]}


def reconcile_in_catalogue() -> dict:
    """Recompute the `in_catalogue` flag for every registry row by matching its
    host (and sample_url host) BASE DOMAIN against the live donor_sources
    catalogue. Sets True for hosts already in the catalogue, False otherwise, so
    the registry's "not yet pushed" view and future pushes stay accurate.

    Returns {marked, cleared, total, error}. Best-effort; needs migration 040."""
    def _base(h: str | None) -> str:
        p = (h or "").split(".")
        return ".".join(p[-2:]) if len(p) >= 2 else (h or "")

    try:
        sb = get_client()
        cat = (sb.table("donor_sources")
               .select("rfp_listing_url,base_url").execute().data or [])
        cat_bases = set()
        for c in cat:
            for u in (c.get("rfp_listing_url"), c.get("base_url")):
                h = normalize_host(u)
                if h:
                    cat_bases.add(_base(h))
        marked = cleared = 0
        for r in list_rows():
            host = r.get("host")
            present = (_base(host) in cat_bases
                       or _base(normalize_host(r.get("listings_url"))) in cat_bases
                       or _base(normalize_host(r.get("sample_url"))) in cat_bases)
            current = bool(r.get("in_catalogue"))
            if present == current:
                continue
            try:
                sb.table(_TABLE).update({"in_catalogue": present}).eq(
                    "host", host).execute()
                marked += int(present)
                cleared += int(not present)
            except Exception:
                pass
        clear_cache()
        return {"marked": marked, "cleared": cleared,
                "total": marked + cleared, "error": None}
    except Exception as exc:
        log.debug("source_registry.reconcile_in_catalogue failed: %s", exc)
        return {"marked": 0, "cleared": 0, "total": 0, "error": str(exc)[:200]}


def record_encounters(encounters: list[dict]) -> int:
    """Batch-log host encounters from one scan. Each item:
        {url, title, detected, accepted}
    New hosts are inserted (status='pending'); existing ones get hits bumped +
    last_seen refreshed (pending rows also adopt the latest detector guess).
    Best-effort: returns rows written, 0 on any error. Never raises."""
    if not encounters:
        return 0
    # Aggregate this scan's encounters per host first (one write per host).
    agg: dict[str, dict] = {}
    for e in encounters:
        host = normalize_host(e.get("url"))
        if not host:
            continue
        a = agg.setdefault(host, {"count": 0, "detected": "unknown",
                                  "url": e.get("url"), "title": e.get("title"),
                                  "sols": set(), "instrs": set()})
        a["count"] += 1
        if e.get("solicitation_type"):
            a["sols"].add(e["solicitation_type"])
        if e.get("instrument_type"):
            a["instrs"].add(e["instrument_type"])
        det = (e.get("detected") or "unknown").lower()
        # Precedence: a detected aggregator/blog/listing STICKS (even if the
        # candidate was accepted — that means its resolved primary passed, not
        # that THIS host is primary). Otherwise an accepted candidate is strong
        # evidence the host itself is a primary source.
        if det in ("aggregator", "blog", "listing"):
            a["detected"] = det
        elif a["detected"] not in ("aggregator", "blog", "listing"):
            if e.get("accepted"):
                a["detected"] = "primary"
            elif a["detected"] == "unknown" and det in VALID_CLASS:
                a["detected"] = det
    if not agg:
        return 0
    try:
        sb = get_client()
        existing = get_all(force=True)
        now_iso = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat()
        try:
            from core import aggregators as _aggr   # lazy — avoids import cycle
        except Exception:
            _aggr = None
        new_rows, written = [], 0
        for host, a in agg.items():
            m = (_aggr.meta(a["url"]) if _aggr else {})  # taxonomy from catalogue
            row = existing.get(host)
            if not row:
                new_rows.append({
                    "host": host, "classification": a["detected"],
                    "status": "pending", "detected_as": a["detected"],
                    "sample_url": (a["url"] or "")[:600],
                    "sample_title": (a["title"] or "")[:300] or None,
                    "hits": a["count"], "last_seen": now_iso,
                    "source_class": m.get("source_class"),
                    "access_model": m.get("access_model"),
                    "ingestion_method": m.get("ingestion_method"),
                    "has_api": bool(m.get("has_api")),
                    "solicitation_types": sorted(a["sols"]) or None,
                    "instrument_types": sorted(a["instrs"]) or None,
                })
            else:
                upd = {"hits": int(row.get("hits") or 0) + a["count"],
                       "last_seen": now_iso}
                # Refresh the guess only while still pending (never overwrite a
                # human-confirmed classification).
                if (row.get("status") or "").lower() != "confirmed":
                    upd["detected_as"] = a["detected"]
                    if a["detected"] != "unknown":
                        upd["classification"] = a["detected"]
                    if m.get("source_class"):
                        upd["source_class"] = m["source_class"]
                        upd["access_model"] = m.get("access_model")
                        upd["ingestion_method"] = m.get("ingestion_method")
                        upd["has_api"] = bool(m.get("has_api"))
                try:
                    sb.table(_TABLE).update(upd).eq("host", host).execute()
                    written += 1
                except Exception:
                    pass
        for i in range(0, len(new_rows), 200):
            try:
                sb.table(_TABLE).insert(new_rows[i:i + 200]).execute()
                written += len(new_rows[i:i + 200])
            except Exception:
                pass
        if written:
            clear_cache()
            log.info("source_registry: recorded %d host encounters "
                     "(%d new)", len(agg), len(new_rows))
        return written
    except Exception as exc:
        log.debug("source_registry.record_encounters failed: %s", exc)
        return 0
