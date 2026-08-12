"""One chart palette for the whole report.

The report had accumulated a different palette per chart — emerald, amber, red, slate, grey,
yellow, two greens that were nearly the same — so nothing read as belonging to one document and
colour had stopped carrying meaning.

THE PALETTE. Turquoise is the primary chart colour and Light Blue the low-emphasis end; a series
is shaded by interpolating between them, with Teal available as a third step where a chart genuinely
needs one. The house dark blue is deliberately unused (it reads as chrome, not data), and Dark Red
is reserved — it appears ONLY where a category is genuinely negative (Decline, Not Approved,
Missed). That reservation is the point: if red appeared decoratively it would stop meaning "bad".

WHY A GRADIENT RATHER THAN DIFFERENT COLOURS. On the charts that matter the categories are ORDERED
— Proceed → Park → Decline, Not Started → Completed — so one ramp along that order encodes the
ranking in the ink itself. Distinct hues encode difference without ranking, which is what made the
old red/amber/green look busy while saying less than the axis label already did.

Tradeoff worth knowing: two adjacent steps of a gradient are harder to tell apart at a glance than
two unrelated hues. Charts here stay under ~6 categories, where that holds up.
"""
from __future__ import annotations

# House palette. Names are descriptive so nothing brand-specific lives in the code.
TURQUOISE = "#117996"          # primary — most emphatic end of every ramp
LIGHT_BLUE = "#D5E7EF"         # low-emphasis end
TEAL = "#6EDBCD"               # third step where a chart needs one
DARK_RED = "#7C1220"           # NEGATIVE categories only

# Kept for callers that want the primary by name.
BRAND = TURQUOISE

# Ink for text/axes/rules — neutral, so it never competes with the data.
INK = "#334155"
MUTED = "#64748b"
GRID = "#E3EAEE"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    """`a` at t=0 → `b` at t=1, interpolated in RGB."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    t = max(0.0, min(1.0, t))
    return "#{:02x}{:02x}{:02x}".format(round(ar + (br - ar) * t),
                                        round(ag + (bg - ag) * t),
                                        round(ab + (bb - ab) * t))


# The faint end stops short of pure Light Blue: a bar in the exact background tint reads as a
# rendering fault rather than a small value.
_FAINTEST = 0.82


def ramp(n: int, *, reverse: bool = False) -> list[str]:
    """`n` shades from Turquoise (most emphatic) toward Light Blue.

    One category gets the full primary rather than a midpoint, so a single-bar chart is not
    washed out.
    """
    if n <= 0:
        return []
    if n == 1:
        return [TURQUOISE]
    out = [_mix(TURQUOISE, LIGHT_BLUE, (i / (n - 1)) * _FAINTEST) for i in range(n)]
    return list(reversed(out)) if reverse else out


# Categories that mean something went wrong. These get Dark Red instead of a ramp step, on every
# chart, so the colour keeps one meaning across the report.
NEGATIVE_CATEGORIES = frozenset({
    "decline", "declined", "not approved", "rejected", "missed", "lost", "unsuccessful",
})


def is_negative(category: str) -> bool:
    return str(category or "").strip().lower() in NEGATIVE_CATEGORIES


def sequence_for(categories: list[str], order: list[str] | None = None) -> dict[str, str]:
    """`{category: colour}` following `order` where given, else the order supplied.

    Charts pass their own semantic ordering (best outcome first) so the emphatic end lands on the
    same category every time rather than on whichever happens to be most frequent. Negative
    categories are taken out of the ramp and given Dark Red.
    """
    if order:
        ranked = [c for c in order if c in categories]
        ranked += [c for c in categories if c not in ranked]
    else:
        ranked = list(categories)

    positives = [c for c in ranked if not is_negative(c)]
    shades = ramp(len(positives))
    out = {c: shades[i] for i, c in enumerate(positives)}
    for c in ranked:
        if is_negative(c):
            out[c] = DARK_RED
    return out


# Continuous scale, faint to full strength — for single-series fills.
SCALE = [[0.0, _mix(TURQUOISE, LIGHT_BLUE, _FAINTEST)],
         [0.5, _mix(TURQUOISE, LIGHT_BLUE, 0.4)],
         [1.0, TURQUOISE]]

# Ordered category lists, so every chart of the same thing shades it the same way.
DECISION_ORDER = ["Proceed", "Park", "Decline", "No decision"]
PROGRESS_ORDER = ["Completed", "In Progress", "Not Started", "Discontinued", "Missed"]
DONOR_DECISION_ORDER = ["Approved", "Under Review", "Submitted", "Not Approved"]
SUBMITTED_ORDER = ["Submitted", "Unsubmitted"]


# Accents for charts whose categories are UNRELATED rather than ordered — one series per team
# member, say. A single-hue ramp is the right choice for an ordered scale and the wrong one here:
# thirteen steps of the same turquoise are indistinguishable, which is exactly what the
# per-member stacked chart looked like.
#
# Dark Red is deliberately absent: it means "negative" everywhere else in the report, and spending
# it on whoever happens to be sixth in a legend would empty it of that meaning. The house dark
# blue is absent too — it reads as page chrome. Everything here is the palette's accents and
# their tints/shades, which the brand guidance allows for graphs.
_CATEGORICAL = [
    "#117996",   # turquoise (primary)
    "#1ED47F",   # green
    "#F4B71B",   # gold
    "#6EDBCD",   # teal
    "#0E5A70",   # deep turquoise
    "#A5C8D6",   # pale blue
    "#14A06B",   # deep green
    "#C9A227",   # deep gold
    "#4A7A96",   # slate teal
    "#9BD9C6",   # pale teal
    "#E3D08A",   # pale gold
    "#7FB2C4",   # mid blue
]


def categorical(n: int) -> list[str]:
    """`n` visually distinct colours for unordered categories.

    Beyond the accent list the colours are lightened progressively rather than repeated, so a
    long legend stays readable instead of pairing two series in the same colour.
    """
    if n <= 0:
        return []
    out = list(_CATEGORICAL[:n])
    round_no = 1
    while len(out) < n:
        for base in _CATEGORICAL:
            if len(out) >= n:
                break
            out.append(_mix(base, "#FFFFFF", min(0.62, 0.22 * round_no)))
        round_no += 1
    return out[:n]


def style(fig, *, height: int | None = None, showlegend: bool | None = None):
    """The shared look: transparent ground, one font, restrained gridlines.

    Font sizes are set here and NOT scaled down for print — printing re-lays each chart out at
    the page width through Plotly, so these sizes are what lands on paper. Shrinking the whole
    SVG instead took 11px axis labels to about 5pt, which is what "blurry" meant.
    """
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, size=12),
        title=dict(font=dict(size=13, color=INK)),
        margin=dict(t=38, b=8, l=8, r=8),
        colorway=ramp(6),
    )
    if height is not None:
        fig.update_layout(height=height)
    if showlegend is not None:
        fig.update_layout(showlegend=showlegend)
    # NO GRIDLINES. With a chart in a bordered frame and a value label on every bar, the grid
    # was a third layer of ink competing with both — it made a simple chart look busy without
    # helping anyone read a value. The axis line and the tick labels are enough; a bar chart is
    # read against its own labels, not against a ruler.
    for axis in (fig.update_xaxes, fig.update_yaxes):
        axis(showgrid=False, zeroline=False, linecolor=GRID, linewidth=1,
             tickfont=dict(color=MUTED, size=11))
    return fig
