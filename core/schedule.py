"""Monday check-in call schedule (note-taker / RFP presenter / chair).

Stored as JSON in `app_settings` (key 'schedule_json') — same pattern as the
team roster, so there's no DB migration to run. Seeded from the Excel
'Schedule' sheet via scripts/import_schedule.py and edited in Actions →
Schedule.

Each entry: {"date": "YYYY-MM-DD", "note_taker": str, "presenter": str,
             "chair": str}.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from core.settings import get_setting, set_setting

_KEY = "schedule_json"


def _parse(d) -> Optional[date]:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except Exception:
        return None


def _clean(items) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for it in (items or []):
        d = _parse(it.get("date"))
        if not d or d.isoformat() in seen:
            continue
        seen.add(d.isoformat())
        out.append({
            "date": d.isoformat(),
            "cid": ("" if it.get("cid") in (None, "")
                    else str(it.get("cid")).strip()),
            "note_taker": (it.get("note_taker") or "").strip(),
            "presenter": (it.get("presenter") or "").strip(),
            "chair": (it.get("chair") or "").strip(),
        })
    out.sort(key=lambda x: x["date"])
    return out


def get_schedule() -> list[dict]:
    """All schedule entries, ascending by date. Empty list if unset."""
    raw = get_setting(_KEY)
    if not raw:
        return []
    try:
        return _clean(json.loads(raw))
    except Exception:
        return []


def set_schedule(items: list[dict], updated_by: str | None = None) -> None:
    set_setting(_KEY, json.dumps(_clean(items)), updated_by=updated_by)


def add_meeting(d, note_taker: str = "", presenter: str = "",
                chair: str = "", updated_by: str | None = None) -> bool:
    """Add (or replace, by date) one meeting. Returns False on a bad date."""
    dd = _parse(d)
    if not dd:
        return False
    items = [x for x in get_schedule() if x["date"] != dd.isoformat()]
    items.append({"date": dd.isoformat(),
                  "note_taker": (note_taker or "").strip(),
                  "presenter": (presenter or "").strip(),
                  "chair": (chair or "").strip()})
    set_schedule(items, updated_by=updated_by)
    return True


def delete_meeting(d, updated_by: str | None = None) -> None:
    dd = _parse(d)
    if not dd:
        return
    set_schedule([x for x in get_schedule() if x["date"] != dd.isoformat()],
                 updated_by=updated_by)


def next_meeting(today: date | None = None) -> Optional[dict]:
    """The next meeting on or after `today` (schedule is sorted ascending)."""
    today = today or date.today()
    for it in get_schedule():
        d = _parse(it["date"])
        if d and d >= today:
            return it
    return None


def roster_from_schedule() -> list[str]:
    """Distinct people already named in the schedule (note-takers / presenters
    / chairs) — used to populate the add-meeting dropdowns alongside the team
    roster, so existing full names stay selectable."""
    names: set[str] = set()
    for it in get_schedule():
        for k in ("note_taker", "presenter", "chair"):
            v = (it.get(k) or "").strip()
            if v:
                names.add(v)
    return sorted(names)
