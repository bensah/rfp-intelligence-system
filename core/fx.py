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

import streamlit as st

_ERAPI = "https://open.er-api.com/v6/latest/{base}"


# ---------------------------------------------------------------------------
# Live rate
# ---------------------------------------------------------------------------
@st.cache_data(ttl=21600, show_spinner=False)  # 6h — intraday drift is immaterial
def _erapi_rate(currency: str) -> tuple[float | None, str | None]:
    """USD per 1 unit of `currency`, plus the rate's date. (None, None) on fail."""
    cur = (currency or "").strip().upper()
    if not cur or cur == "USD":
        return (1.0, date.today().isoformat())
    try:
        import httpx
        r = httpx.get(_ERAPI.format(base=cur), timeout=12.0)
        if r.status_code != 200:
            return (None, None)
        j = r.json()
        if j.get("result") != "success":
            return (None, None)
        usd = (j.get("rates") or {}).get("USD")
        if not usd:
            return (None, None)
        rdate = (j.get("time_last_update_utc") or "")[:16] or date.today().isoformat()
        return (float(usd), rdate)
    except Exception:
        return (None, None)


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
