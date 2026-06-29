"""Currency display + conversion helpers.

`format_money(value, code)` returns a consistent native-currency string:

  $1,000,000 USD      (symbol-prefixed when defined)
  £5,000,000 GBP
  €500,000 EUR
  C$2,000,000 CAD
  XAF 500,000 FCFA    (currencies without a symbol use "{code} {value} {label}")

Resolution order for the second argument:
  1. exact match against `code`
  2. exact match against `label`
  3. exact match against any value in `aliases`
  4. first-whitespace-token match against `code`  (handles "GBP £" → "GBP")
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from core import dropdowns


def _is_null(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _resolve_currency(code_str: str) -> dict | None:
    currencies = dropdowns.load().get("currencies", []) or []
    for entry in currencies:
        if entry.get("code") == code_str or entry.get("label") == code_str:
            return entry
        if code_str in (entry.get("donor_aliases") or []):
            return entry
    # Fallback: first whitespace token
    first = code_str.split()[0] if code_str else ""
    for entry in currencies:
        if entry.get("code") == first:
            return entry
    return None


def format_money(value: Any, code: Any) -> str:
    if _is_null(value):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    code_str = str(code or "").strip()
    if not code_str:
        return f"{v:,.0f}"
    entry = _resolve_currency(code_str)
    if entry:
        symbol = entry.get("symbol")
        actual_code = entry.get("code") or code_str
        label = entry.get("label") or actual_code
        if symbol:
            return f"{symbol}{v:,.0f} {actual_code}"
        return f"{actual_code} {v:,.0f} {label}"
    # Unknown currency — render as-is
    return f"{v:,.0f} {code_str}"


def format_usd(value: Any) -> str:
    if _is_null(value):
        return "—"
    try:
        return f"${float(value):,.0f} USD"
    except (TypeError, ValueError):
        return "—"
