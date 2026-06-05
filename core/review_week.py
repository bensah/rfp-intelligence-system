"""Review-week label helper.

Matches the Excel screener's week label format:
    "Week 22 (25 May - 31 May)"
ISO week numbering, Monday-anchored. The screener's review week is the
upcoming Monday's week (i.e. the week the Friday scan feeds into).
"""
from __future__ import annotations

from datetime import date, timedelta


def week_bounds(d: date) -> tuple[date, date]:
    """(Monday, Sunday) of the ISO week containing `d`."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _fmt_day(d: date) -> str:
    return d.strftime("%d %b").lstrip("0")


def review_week_label(d: date | None = None) -> str:
    """Return 'Week N (DD Mon - DD Mon)' for the week containing `d` (default today)."""
    d = d or date.today()
    mon, sun = week_bounds(d)
    return f"Week {mon.isocalendar().week} ({_fmt_day(mon)} - {_fmt_day(sun)})"


def upcoming_review_week_label(d: date | None = None) -> str:
    """Label for the *upcoming* review week.

    A Friday scan (or weekend submission) feeds the following Monday's review,
    so we advance to next Monday when called on Friday/Saturday/Sunday.
    """
    d = d or date.today()
    if d.weekday() >= 4:  # Fri/Sat/Sun -> next week
        d = d + timedelta(days=(7 - d.weekday()))
    return review_week_label(d)


def all_weeks_for_year(year: int) -> list[str]:
    """All 52 (or 53) ISO-week labels for the given year. Used in selectors."""
    labels: list[str] = []
    d = date(year, 1, 4)  # Jan 4 is always in week 1
    mon, _ = week_bounds(d)
    while mon.year <= year:
        labels.append(review_week_label(mon))
        mon = mon + timedelta(days=7)
        if mon.year > year and mon.isocalendar().week == 1:
            break
    return labels


def monday_from_week_label(label: str, year: int) -> date:
    """Parse 'Week 23 (1 Jun - 7 Jun)' → date(2026, 6, 1) for year=2026."""
    week_num = int(label.split()[1])
    jan4 = date(year, 1, 4)
    base_mon, _ = week_bounds(jan4)
    return base_mon + timedelta(days=(week_num - base_mon.isocalendar().week) * 7)
