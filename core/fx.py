"""Live FX conversion + money extraction for non-USD funding amounts.

Donor calls quote amounts in many currencies (DKK, EUR, GBP, NOK, XAF, …). The
static rates in config/dropdowns.yaml cover only a handful and drift over time,
so a DKK 1.7M prize rendered as "$nan". This module fixes both halves:

  * extract_amount(text) → pulls "DKK 1.7 million" / "€500,000" / "up to $5
    million" / "100 million NOK" out of free text → (value, ISO-currency).
  * to_usd(amount, currency) → converts using a LIVE rate (free, no key) so any
    currency works, returning the USD value + the rate + the rate date so we
    can display "≈ $X USD (DKK 1,700,000 @ 2026-06-13)".

Rate source: open.er-api.com (free, no key, ~160 currencies incl. XAF / NGN /
KES). It serves the LATEST rate; since we convert at scan/discovery time and
STORE the result, "latest" == the search-date rate for a newly-found call —
which is what the "date of search" requirement means in practice. Falls back to
the static config/dropdowns.yaml rate, then gives up (usd=None) rather than
guessing.
"""
from __future__ import annotations

import re
from datetime import date

_ERAPI = "https://open.er-api.com/v6/latest/{base}"


# ---------------------------------------------------------------------------
# Live rate
# ---------------------------------------------------------------------------
# Rate cache — deliberately a MODULE-level TTL dict, NOT @st.cache_data.
#
# This is a blocking OUTBOUND HTTP call that sits inside the per-row scoring path
# (criteria_derive._usd → dropdowns.usd_rate → here), so it is charged once per distinct
# currency on every screen that scores rows. It used to be an @st.cache_data(ttl=6h), but
# the app calls st.cache_data.clear() in ~37 places (after any write, registry edit, sync,
# …) — and EVERY one of those wiped the FX cache too, so the next page render re-fetched
# every currency over the network: measured ~1.7s each, up to the 12s timeout if the API
# stalls, which is minutes on a data-heavy page. A module cache is immune to those clears
# and is shared by every session in the process.
_RATE_CACHE: dict[str, tuple[float, tuple[float | None, str | None]]] = {}
_RATE_TTL = 21600.0        # 6h — intraday drift is immaterial
_FAIL_TTL = 900.0          # 15min — don't hammer the API for a currency it doesn't know
_HTTP_TIMEOUT = 4.0        # a page render must never hang on a third-party API

# Free-typed currency cells ("Euro €", "USD $", "GBP £") normalise to a bare first token,
# which can be a non-ISO word. Map the common ones so we don't burn a network round-trip
# (and a failure) on a code the API will never recognise.
_ALIAS = {"EURO": "EUR", "EUROS": "EUR", "POUND": "GBP", "POUNDS": "GBP",
          "STERLING": "GBP", "DOLLAR": "USD", "DOLLARS": "USD", "US": "USD",
          "USDOLLAR": "USD", "FCFA": "XAF", "CFA": "XAF", "RAND": "ZAR",
          "NAIRA": "NGN", "SHILLING": "KES", "KRONE": "DKK", "KRONER": "DKK"}


def _erapi_rate(currency: str) -> tuple[float | None, str | None]:
    """USD per 1 unit of `currency`, plus the rate's date. (None, None) on fail.

    Cached 6h in-process (failures 15min) and hard-timeboxed, so scoring a page of rows
    never blocks on the FX API."""
    import time as _time
    cur = (currency or "").strip().upper()
    cur = _ALIAS.get(cur, cur)
    if not cur or cur == "USD":
        return (1.0, date.today().isoformat())
    if not cur.isalpha() or len(cur) != 3:
        return (None, None)          # not an ISO code — never worth a network call
    hit = _RATE_CACHE.get(cur)
    if hit is not None:
        _ttl = _RATE_TTL if hit[1][0] is not None else _FAIL_TTL
        if (_time.monotonic() - hit[0]) < _ttl:
            return hit[1]
    out: tuple[float | None, str | None] = (None, None)
    try:
        import httpx
        r = httpx.get(_ERAPI.format(base=cur), timeout=_HTTP_TIMEOUT)
        if r.status_code == 200:
            j = r.json()
            if j.get("result") == "success":
                usd = (j.get("rates") or {}).get("USD")
                if usd:
                    rdate = ((j.get("time_last_update_utc") or "")[:16]
                             or date.today().isoformat())
                    out = (float(usd), rdate)
    except Exception:
        out = (None, None)           # network error → negative-cached briefly, then retried
    _RATE_CACHE[cur] = (_time.monotonic(), out)
    return out


def to_usd(amount, currency, on_date: str | None = None) -> dict:
    """Convert (amount, currency) → USD.

    Returns {usd, rate, rate_date, currency, source}; usd is None only when the
    currency is unknown to BOTH the live API and the static table.
    """
    out: dict = {"usd": None, "rate": None, "rate_date": None,
                 "currency": ((currency or "").strip().upper() or None),
                 "source": None}
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return out
    if amt == 0:
        out["usd"] = 0.0
        return out
    cur = out["currency"]
    if not cur or cur == "USD":
        out.update(usd=amt, rate=1.0, currency="USD", source="usd",
                   rate_date=(on_date or date.today().isoformat()))
        return out
    rate, rdate = _erapi_rate(cur)
    if rate:
        out.update(usd=amt * rate, rate=rate, rate_date=rdate,
                   source="open.er-api.com")
        return out
    # Fallback: static config rate (1.0 means "unknown" → don't trust it).
    try:
        from core import dropdowns
        srate = dropdowns.usd_rate(cur)
        if srate and srate != 1.0:
            out.update(usd=amt * srate, rate=srate, rate_date="static",
                       source="dropdowns.yaml")
            return out
    except Exception:
        pass
    return out  # usd stays None — genuinely unknown currency


# ---------------------------------------------------------------------------
# Money extraction
# ---------------------------------------------------------------------------
_SYMBOL_TO_ISO = {"$": "USD", "€": "EUR", "£": "GBP", "₦": "NGN", "¥": "JPY"}
# Currencies we recognise as ISO codes in text (extend freely).
_KNOWN_ISO = {
    "USD", "EUR", "GBP", "CAD", "AUD", "NZD", "DKK", "NOK", "SEK", "CHF",
    "JPY", "CNY", "INR", "ZAR", "XAF", "XOF", "NGN", "KES", "GHS", "UGX",
    "TZS", "RWF", "ETB", "BRL", "MXN", "SGD", "HKD", "AED", "SAR", "ILS",
}
_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6,
         "billion": 1e9, "bn": 1e9, "b": 1e9}

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULTGRP = r"(?:\s*(million|billion|thousand|mn|bn|[kmb]))?"
# currency BEFORE the number: "$5m", "€500,000", "DKK 1.7 million", "USD 5m"
_PAT_PRE = re.compile(r"([$€£₦¥]|\b[A-Za-z]{3})\s*(" + _NUM + r")" + _MULTGRP, re.I)
# number BEFORE the currency: "1,200,000 CAD", "100 million NOK"
_PAT_POST = re.compile(r"(" + _NUM + r")" + _MULTGRP + r"\s*(\b[A-Za-z]{3})", re.I)


def _to_float(num: str, mult: str | None) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    if mult:
        v *= _MULT.get(mult.lower(), 1.0)
    return v if v > 0 else None


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw in _SYMBOL_TO_ISO:
        return _SYMBOL_TO_ISO[raw]
    up = raw.upper()
    return up if up in _KNOWN_ISO else None


def extract_amount(text: str) -> tuple[float | None, str | None]:
    """Largest (amount, ISO-currency) found in `text`, or (None, None).

    Picks the largest because donor pages mention the headline ceiling plus
    smaller per-award figures; the headline is what we want to show.
    """
    if not text:
        return (None, None)
    best: tuple[float | None, str | None] = (None, None)
    best_v = -1.0
    for rx, cur_first in ((_PAT_PRE, True), (_PAT_POST, False)):
        for m in rx.finditer(text):
            cur = _iso(m.group(1) if cur_first else m.group(3))
            num = m.group(2) if cur_first else m.group(1)
            mult = m.group(3) if cur_first else m.group(2)
            if not cur:
                continue
            v = _to_float(num, mult)
            if v is not None and v > best_v:
                best_v, best = v, (v, cur)
    return best
