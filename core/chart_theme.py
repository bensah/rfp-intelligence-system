"""One chart palette for the whole report.

The report had accumulated a different palette per chart — emerald, amber, red, slate, grey,
yellow, two greens that were nearly the same — so nothing read as belonging to one document and
colour had stopped carrying meaning.

This replaces all of it with ONE hue at varying opacity. The house green is the base; the deep
blue is deliberately not used here.

WHY OPACITY RATHER THAN DIFFERENT COLOURS. On the charts that matter the categories are ORDERED —
Proceed → Park → Decline, Not Started → Completed, Approved → Not Approved — so a single hue that
fades along that order encodes the ranking in the ink itself. Distinct hues encode difference
without ranking, which is what made the old red/amber/green look busy while telling the reader
less than the axis label already did.

That is also the tradeoff worth knowing: the old charts made a Decline bar recognisable by colour
alone, and these do not. Read against the category order they carry more, not less; glanced at
from across a room they carry less.
"""
from __future__ import annotations

# House green. Kept as RGB so opacity can vary without hand-writing rgba strings.
BRAND_RGB = (0, 112, 60)          # #00703C
BRAND = "#00703C"

# Ink for text/axes/rules — a neutral that does not compete with the hue.
INK = "#334155"
MUTED = "#64748b"
GRID = "#e2e8e5"

# The most and least emphatic steps of the ramp. The floor stays well above invisible: a bar at
# 0.2 on white reads as a rendering fault rather than a small value.
_MAX_ALPHA = 0.95
_MIN_ALPHA = 0.38


def rgba(alpha: float) -> str:
    r, g, b = BRAND_RGB
    return f"rgba({r},{g},{b},{round(max(0.0, min(1.0, alpha)), 3)})"


def ramp(n: int, *, reverse: bool = False) -> list[str]:
    """`n` shades of the house green, most emphatic first.

    Pass `reverse=True` when the FIRST category is the one to de-emphasise (a chart ordered
    worst-to-best). One category gets full strength rather than the midpoint, so a single-bar
    chart does not render washed out.
    """
    if n <= 0:
        return []
    if n == 1:
        return [rgba(_MAX_ALPHA)]
    step = (_MAX_ALPHA - _MIN_ALPHA) / (n - 1)
    out = [rgba(_MAX_ALPHA - step * i) for i in range(n)]
    return list(reversed(out)) if reverse else out


def sequence_for(categories: list[str], order: list[str] | None = None) -> dict[str, str]:
    """`{category: rgba}` following `order` where given, else the order supplied.

    Charts pass their own semantic ordering (best outcome first), so the darkest shade lands on
    the same category every time rather than on whichever happens to be most frequent.
    """
    if order:
        ranked = [c for c in order if c in categories]
        ranked += [c for c in categories if c not in ranked]
    else:
        ranked = list(categories)
    return dict(zip(ranked, ramp(len(ranked))))


# Continuous scale, light to full strength — for heat-style or single-series fills.
SCALE = [[0.0, rgba(0.15)], [0.5, rgba(0.55)], [1.0, rgba(_MAX_ALPHA)]]

# Ordered category lists, so every chart of the same thing shades it the same way.
DECISION_ORDER = ["Proceed", "Park", "Decline", "No decision"]
PROGRESS_ORDER = ["Completed", "In Progress", "Not Started", "Missed", "Discontinued"]
DONOR_DECISION_ORDER = ["Approved", "Under Review", "Submitted", "Not Approved"]
SUBMITTED_ORDER = ["Submitted", "Unsubmitted"]


def style(fig, *, height: int | None = None, showlegend: bool | None = None):
    """The shared look: transparent ground, one font, restrained gridlines.

    Applied to every figure so the frames around them are the only visual boundary — Plotly's
    default grey plot area inside a bordered container reads as a box inside a box.
    """
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, size=12),
        title=dict(font=dict(size=13, color=INK)),
        margin=dict(t=38, b=8, l=8, r=8),
        colorway=ramp(8),
    )
    if height is not None:
        fig.update_layout(height=height)
    if showlegend is not None:
        fig.update_layout(showlegend=showlegend)
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID,
                     tickfont=dict(color=MUTED, size=11))
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID,
                     tickfont=dict(color=MUTED, size=11))
    return fig
