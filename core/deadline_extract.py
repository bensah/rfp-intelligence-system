"""Confidence-gated submission-deadline extraction (DATA_SCHEMA_ETL.md §6).

Different donors phrase deadlines wildly differently, so a single regex is brittle.
This module runs a labelled-context date bank with a multi-format parser, classifies
each candidate (submission vs posted/award/info-session), and returns ONE deadline
plus a confidence and the evidence snippet. Three outcomes, not two:

  * date found      -> {deadline, confidence high|medium, method 'regex'}
  * certainly none  -> rolling/year-round wording, high confidence -> caller applies
                       the Dec-31-{scan_year} default (method 'default-rolling')
  * uncertain       -> 0 candidates or conflicting -> low confidence, needs_review
                       (caller may invoke an LLM arbiter to resolve)

Pure-python (re + datetime), no external deps. An optional `llm_arbiter` callable
lets the caller resolve low-confidence cases without this module importing any SDK.
"""
from __future__ import annotations

import re
from datetime import date

# Context that marks a SUBMISSION/closing date (what we want).
_SUBMIT_LABELS = (
    "submission deadline", "application deadline", "deadline for applications",
    "deadline for submission", "deadline", "closing date", "applications close",
    "application closes", "closes on", "close on", "apply by", "due by",
    "due date", "last date", "final date", "submit by", "must be received by",
    "expressions of interest due", "eoi deadline", "proposals due",
    "date limite",                       # FR — francophone donors
    # "Open until <date>" is how several donor-catalogue sites state their window, and its
    # absence here is why an expired call kept coming back: the date fell into the
    # unlabelled `other` bucket, came back confidence='low', and the scan pipeline
    # discards anything below medium. The page said "Open until 30 December 2017" in plain
    # words the whole time.
    "open until", "open till", "open through", "accepting applications until",
    "accepting submissions until", "submissions until", "applications until",
    "ouvert jusqu'au", "jusqu'au",        # FR
)
# Context that marks a NON-submission date (must NOT be taken as the deadline).
_NEGATIVE_LABELS = (
    "posted", "published", "date posted", "release date", "issued",
    "award date", "expected award", "start date", "project start", "kick-off",
    "kickoff", "information session", "info session", "webinar", "q&a", "q & a",
    "briefing", "pre-bid", "clarification", "anticipated award", "notification",
    "results announced", "decision date", "interview",
)
# Wording that means there is genuinely no fixed deadline (-> rolling default).
_ROLLING_RE = re.compile(
    r"\b(rolling|year[- ]?round|no\s+(?:fixed\s+)?deadline|ongoing basis|"
    r"open\s+call|accepted\s+(?:on\s+a\s+)?(?:rolling|continuous|ongoing)|"
    r"continuous(?:ly)?\s+open|applications?\s+(?:are\s+)?(?:accepted|open)\s+"
    r"(?:all\s+year|year[- ]?round|on\s+an?\s+ongoing))\b", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|" \
            r"jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# Date shapes, each tagged so the parser knows how to read the groups.
_DATE_PATTERNS = [
    # 22 July 2026 / 22nd July, 2026
    ("dmy_text", re.compile(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE + r"\.?,?\s+(\d{4})\b", re.I)),
    # July 22, 2026 / July 22 2026
    ("mdy_text", re.compile(
        r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)),
    # 2026-07-22
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    # 22/07/2026 or 07/22/2026 or 22-07-2026  (ambiguous order -> resolved below)
    ("numeric", re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")),
    # July 2026  (month-year only -> end of month, lower confidence)
    ("my_text", re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{4})\b", re.I)),
]


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _eom(y: int, m: int) -> date | None:
    from datetime import timedelta
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


def _parse(kind: str, groups: tuple) -> tuple[date | None, bool]:
    """(date, month_only?) for a matched pattern. month_only lowers confidence."""
    try:
        if kind == "dmy_text":
            d, mon, y = int(groups[0]), _MONTHS[groups[1][:3].lower()], int(groups[2])
            return _mk(y, mon, d), False
        if kind == "mdy_text":
            mon, d, y = _MONTHS[groups[0][:3].lower()], int(groups[1]), int(groups[2])
            return _mk(y, mon, d), False
        if kind == "iso":
            y, mon, d = int(groups[0]), int(groups[1]), int(groups[2])
            return _mk(y, mon, d), False
        if kind == "numeric":
            a, b, y = int(groups[0]), int(groups[1]), int(groups[2])
            # Disambiguate D/M vs M/D: if one >12 it's the day; else assume D/M
            # (international / francophone default) at slightly lower confidence.
            if a > 12 and b <= 12:
                return _mk(y, b, a), False
            if b > 12 and a <= 12:
                return _mk(y, a, b), False
            return _mk(y, b, a), False          # default day/month
        if kind == "my_text":
            mon, y = _MONTHS[groups[0][:3].lower()], int(groups[1])
            return _eom(y, mon), True
    except (KeyError, ValueError, IndexError):
        return None, False
    return None, False


def _label_near(text: str, start: int, window: int = 70) -> tuple[bool, bool, str]:
    """Classify a date by the label NEAREST before it (not any in the window) —
    so 'Posted 1 Mar. Submission deadline: 16 Aug' tags 16 Aug as submit, not the
    stray earlier 'Posted'. Returns (is_submit, is_negative, snippet)."""
    ctx = text[max(0, start - window):start].lower()
    best_pos, best_sub = -1, None       # None = no label found
    for lbl in _SUBMIT_LABELS:
        p = ctx.rfind(lbl)
        if p > best_pos:
            best_pos, best_sub = p, True
    for lbl in _NEGATIVE_LABELS:
        p = ctx.rfind(lbl)
        if p > best_pos:
            best_pos, best_sub = p, False
    snip = text[max(0, start - window):start + 24].strip().replace("\n", " ")
    return (best_sub is True), (best_sub is False), snip


def extract_deadline(text: str, *, scan_year: int | None = None, title: str = "",
                     llm_arbiter=None) -> dict:
    """Return {deadline, funding_window, confidence, method, evidence, needs_review}.

    `scan_year` is required to apply the rolling default (Dec 31, {scan_year}).
    `llm_arbiter(text, title) -> {'deadline': 'YYYY-MM-DD'|None}` is called ONLY for
    low-confidence cases; omit it to stay pure-regex (caller can fall back later).
    """
    blob = f"{title}\n{text or ''}"
    scan_year = scan_year or date.today().year

    # 1) collect candidates with context classification
    seen: set[tuple] = set()
    submit, other = [], []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(blob):
            dt, month_only = _parse(kind, m.groups())
            if not dt:
                continue
            key = (dt.isoformat(), m.start())
            if key in seen:
                continue
            seen.add(key)
            is_sub, is_neg, snip = _label_near(blob, m.start())
            rec = {"date": dt, "month_only": month_only, "is_sub": is_sub,
                   "is_neg": is_neg, "snippet": snip}
            (submit if (is_sub and not is_neg) else other).append(rec)

    def _out(dt, conf, method, ev, needs=False, window=None):
        return {"deadline": dt.isoformat() if dt else None,
                "funding_window": window, "confidence": conf, "method": method,
                "evidence": ev, "needs_review": needs}

    # 2) explicit submission-labelled date(s) — strongest signal
    if submit:
        # prefer non-month-only; among those take the latest (final closing date)
        precise = [r for r in submit if not r["month_only"]] or submit
        best = max(precise, key=lambda r: r["date"])
        conf = "high" if not best["month_only"] and len(
            {r["date"] for r in precise}) == 1 else "medium"
        return _out(best["date"], conf, "regex", best["snippet"], window="One-off")

    # 3) rolling / no-deadline wording -> apply the default
    if _ROLLING_RE.search(blob):
        return _out(date(scan_year, 12, 31), "high", "default-rolling",
                    "no fixed deadline (rolling)", window="Rolling")

    # 4) unlabelled date(s) present but no submit label -> ambiguous
    if other:
        if llm_arbiter:
            try:
                v = llm_arbiter(text, title) or {}
                iso = (v.get("deadline") or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
                    return _out(date.fromisoformat(iso), "medium", "regex+llm",
                                "LLM-resolved among unlabelled dates")
            except Exception:
                pass
        # fall back to the latest unlabelled date (prefer precise over month-only),
        # low confidence
        precise = [r for r in other if not r["month_only"]] or other
        best = max(precise, key=lambda r: r["date"])
        return _out(best["date"], "low", "regex", best["snippet"], needs=True)

    # 5) nothing found -> needs review (do NOT default unless caller is certain)
    if llm_arbiter:
        try:
            v = llm_arbiter(text, title) or {}
            iso = (v.get("deadline") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
                return _out(date.fromisoformat(iso), "medium", "regex+llm",
                            "LLM-extracted (no regex match)")
        except Exception:
            pass
    return _out(None, "low", "none", "no date found", needs=True)


if __name__ == "__main__":  # quick self-test
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cases = [
        ("Apply by July 22, 2026, at 2:00 p.m. ET (6:00 p.m. UTC). View full details", 2026),
        ("Posted 1 March 2026. Submission deadline: 16 August 2026. Expected award 31 Jan 2027.", 2026),
        ("Date limite de soumission : 30/09/2026. Publié le 02/06/2026.", 2026),
        ("Applications are accepted on a rolling basis throughout the year.", 2026),
        ("This grant supports health research. Information session on 5 May 2026.", 2026),
        ("Closing date for applications is 2026-07-31.", 2026),
        ("No date anywhere in this body of text about health systems.", 2026),
    ]
    for txt, yr in cases:
        r = extract_deadline(txt, scan_year=yr)
        print(f"{r['deadline']!s:12} {r['confidence']:7} {r['method']:15} "
              f"win={r['funding_window'] or '-':8} review={r['needs_review']}  | {txt[:55]}")
