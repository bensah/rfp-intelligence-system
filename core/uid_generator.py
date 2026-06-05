"""UID generator.

Format mirrors the original Excel screener: <INITIALS>-<YYMMDD>-<HHMM>
e.g. BE-260202-1220 (Bernard, 2 Feb 2026 12:20).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "XX"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def generate_uid(submitter_name: str, ts: Optional[datetime] = None) -> str:
    ts = ts or datetime.now()
    return f"{_initials(submitter_name)}-{ts.strftime('%y%m%d')}-{ts.strftime('%H%M')}"
