"""Tiny loader for config/dropdowns.yaml — shared by Submit form + dashboards.

Cache keyed by file mtime so YAML edits take effect on next call without a
process restart.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


_PATH = Path(__file__).resolve().parent.parent / "config" / "dropdowns.yaml"
_cache: dict[str, Any] = {"mtime": None, "data": None}


def load() -> dict[str, Any]:
    mtime = _PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        with _PATH.open(encoding="utf-8") as f:
            _cache["data"] = yaml.safe_load(f) or {}
        _cache["mtime"] = mtime
    data = _cache["data"]

    # Currency overrides from app_settings take precedence over YAML
    try:
        from core.settings import get_currency_overrides
        overrides = get_currency_overrides()
        if overrides:
            data = {**data, "currencies": overrides}
    except Exception:
        pass
    return data


def get(key: str, default: list | None = None) -> list:
    return load().get(key, default or [])


def usd_rate(currency: str | None) -> float:
    """Look up FX rate; missing/unknown currency returns 1.0 (no conversion).

    Resolution order matches core.currency._resolve_currency:
    code → label → aliases → first whitespace token.
    """
    if not currency:
        return 1.0
    cur = str(currency).strip()
    if not cur:
        return 1.0
    currencies = load().get("currencies", []) or []

    def _rate(entry):
        try:
            return float(entry.get("usd_rate") or 1.0)
        except (TypeError, ValueError):
            return 1.0

    for entry in currencies:
        if (entry.get("code") == cur
                or entry.get("label") == cur
                or cur in (entry.get("aliases") or [])):
            return _rate(entry)
    # Fallback: first whitespace token (e.g. "GBP £" → "GBP")
    first = cur.split()[0] if cur else ""
    for entry in currencies:
        if entry.get("code") == first:
            return _rate(entry)
    return 1.0
