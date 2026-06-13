"""Schedule view — weekly Monday check-in calls (note-taker / presenter /
chair) with Day / Week / Month / Year list views + an add/update form.

Rendered inside the Actions → Schedule tab via render_view("schedule").
Data lives in app_settings (core.schedule). Deliberately list-based (not a
wide grid) so it stays readable on phones.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import streamlit as st

from core import schedule as _sched
from core import settings as _settings

st.subheader("📅 Check-in schedule")

_user = st.session_state.get("app_user") or {}
_today = date.today()
_sched_items = _sched.get_schedule()
_ROLES = [("Note-taker", "note_taker"), ("Presenter", "presenter"),
          ("Chair", "chair")]


def _d(s):
    return date.fromisoformat(s["date"])


def _roles_line(it: dict) -> str:
    return " · ".join(f"**{lbl}:** {(it.get(k) or '—')}" for lbl, k in _ROLES)


# ── Next meeting banner ─────────────────────────────────────────────────────
_next = _sched.next_meeting(_today)
if _next:
    _nd = _d(_next)
    _when = ("Today" if _nd == _today
             else f"in {(_nd - _today).days} day(s)")
    st.success(f"**Next call · {_nd.strftime('%a %d %b %Y')}** ({_when})  \n"
               f"{_roles_line(_next)}")
else:
    st.caption("No upcoming calls scheduled.")

# ── Add / update ────────────────────────────────────────────────────────────
with st.expander("➕ Add or update a call"):
    _opts = ["—"] + sorted(set((_settings.get_team_members() or [])
                               + _sched.roster_from_schedule()))
    with st.form("schedule_add_form"):
        a1, a2 = st.columns([1, 1])
        _ad = a1.date_input("Call date", value=_today)
        _ant = a2.selectbox("Note-taker", _opts)
        a3, a4 = st.columns(2)
        _apr = a3.selectbox("RFP presenter", _opts)
        _ach = a4.selectbox("Meeting chair", _opts)
        _save = st.form_submit_button("💾 Save call", type="primary")
    if _save:
        _sched.add_meeting(
            _ad,
            "" if _ant == "—" else _ant,
            "" if _apr == "—" else _apr,
            "" if _ach == "—" else _ach,
            updated_by=_user.get("email"))
        st.toast(f"Saved {_ad.isoformat()}", icon="✅")
        st.rerun()

st.divider()

# ── View selector + navigation ──────────────────────────────────────────────
_view = st.radio("View", ["Week", "Month", "Year", "Day"],
                 horizontal=True, index=0)
_ANCHOR = "_sched_anchor"
if _ANCHOR not in st.session_state:
    st.session_state[_ANCHOR] = _today.isoformat()
_anchor = date.fromisoformat(st.session_state[_ANCHOR])


def _shift(d: date, direction: int) -> date:
    if _view == "Day":
        return d + timedelta(days=direction)
    if _view == "Week":
        return d + timedelta(days=7 * direction)
    if _view == "Year":
        try:
            return d.replace(year=d.year + direction)
        except ValueError:
            return d.replace(year=d.year + direction, day=28)
    # Month
    m = d.month - 1 + direction
    y = d.year + m // 12
    return date(y, m % 12 + 1, 1)


_n1, _n2, _n3 = st.columns(3)
if _n1.button("◀ Prev", width='stretch'):
    st.session_state[_ANCHOR] = _shift(_anchor, -1).isoformat()
    st.rerun()
if _n2.button("● Today", width='stretch'):
    st.session_state[_ANCHOR] = _today.isoformat()
    st.rerun()
if _n3.button("Next ▶", width='stretch'):
    st.session_state[_ANCHOR] = _shift(_anchor, 1).isoformat()
    st.rerun()


# ── Period window + header ──────────────────────────────────────────────────
if _view == "Day":
    _start = _end = _anchor
    _label = _anchor.strftime("%A, %d %b %Y")
elif _view == "Week":
    _start = _anchor - timedelta(days=_anchor.weekday())  # Monday
    _end = _start + timedelta(days=6)
    _label = f"Week of {_start:%d %b} – {_end:%d %b %Y}"
elif _view == "Year":
    _start, _end = date(_anchor.year, 1, 1), date(_anchor.year, 12, 31)
    _label = str(_anchor.year)
else:  # Month
    _start = _anchor.replace(day=1)
    _nm = _start.replace(year=_start.year + (_start.month == 12),
                         month=_start.month % 12 + 1)
    _end = _nm - timedelta(days=1)
    _label = _anchor.strftime("%B %Y")

st.markdown(f"#### {_label}")

_period = [it for it in _sched_items if _start <= _d(it) <= _end]
_next_date = _d(_next) if _next else None

if not _period:
    st.caption("No calls in this period. Use **◀ / ▶** to browse, or "
               "**➕ Add or update a call** above.")
else:
    _cur_month = None
    for it in _period:
        di = _d(it)
        if _view == "Year" and di.month != _cur_month:
            _cur_month = di.month
            st.markdown(f"**{di.strftime('%B')}**")
        _is_next = (di == _next_date)
        _is_past = di < _today
        with st.container(border=True):
            _tag = (" &nbsp;🟢 next" if _is_next
                    else " &nbsp;✓ done" if _is_past else "")
            st.markdown(
                f"**{di.strftime('%a %d %b %Y')}**{_tag}", unsafe_allow_html=True)
            st.markdown(_roles_line(it))
