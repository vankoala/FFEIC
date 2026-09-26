"""The dashboard's look (PLAN.md §8): a neutral palette with one data hue, figure styling
and the caption every chart carries.

Colors are the dataviz reference palette's documented hexes. The blue ramps were checked with
its validator as ordinal ramps (monotone lightness, adjacent steps at least 0.06 apart, the
lightest step at 2:1 or more against the surface) in both modes. Six steps of the ramp can't
pass in light mode, so no chart stacks more than five ordered series.
"""

from __future__ import annotations

from dataclasses import dataclass

import plotly.graph_objects as go
import streamlit as st

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


@dataclass(frozen=True)
class Tokens:
    surface: str
    text: str
    secondary: str
    muted: str  # axis labels, and the de-emphasis gray for marks out of focus
    grid: str
    baseline: str
    accent: str  # the one data hue: single series, and resets inside the selected window
    ramp: tuple[str, ...]  # five ordered steps, the most important first
    sequential: tuple[str, ...]  # light to dark, for heatmaps


LIGHT = Tokens(
    surface="#fcfcfb",
    text="#0b0b0b",
    secondary="#52514e",
    muted="#898781",
    grid="#e1e0d9",
    baseline="#c3c2b7",
    accent="#2a78d6",
    ramp=("#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef"),
    sequential=("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"),
)
# Dark mode is its own steps of the same ramp: the most important series is the lightest,
# and no step is darker than 600 so every one clears the dark surface.
DARK = Tokens(
    surface="#1a1a19",
    text="#ffffff",
    secondary="#c3c2b7",
    muted="#898781",
    grid="#2c2c2a",
    baseline="#383835",
    accent="#3987e5",
    ramp=("#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"),
    sequential=("#184f95", "#1c5cab", "#256abf", "#2a78d6", "#5598e7", "#86b6ef", "#b7d3f6"),
)


def tokens() -> Tokens:
    """The palette for the theme the viewer is using (light when Streamlit can't tell)."""
    theme = getattr(st.context, "theme", None)
    return DARK if getattr(theme, "type", None) == "dark" else LIGHT


def style(fig: go.Figure, t: Tokens, *, height: int = 360, y_title: str | None = None) -> None:
    """Recessive axes and hairline grid, legend on top, one tooltip per x position."""
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 36, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": FONT, "size": 13, "color": t.secondary},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "title": None,
            "font": {"color": t.secondary},
            "traceorder": "normal",  # the first series, the bottom of a stack, leads
        },
        hoverlabel={"bgcolor": t.surface, "bordercolor": t.baseline, "font": {"color": t.text}},
        hovermode="x unified",
    )
    # automargin makes room for tick labels and titles, which the tight margins would clip.
    fig.update_xaxes(
        showgrid=False, linecolor=t.baseline, tickfont={"color": t.muted}, ticks="", automargin=True
    )
    fig.update_yaxes(
        gridcolor=t.grid,
        gridwidth=1,
        zeroline=False,
        showline=False,
        tickfont={"color": t.muted},
        title={"text": y_title, "font": {"color": t.muted}} if y_title else None,
        automargin=True,
    )


def show(fig: go.Figure) -> None:
    st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False})


def caption(source: str, as_of: str, caveat: str) -> None:
    """PLAN.md §8: every chart names its source, its as-of date and one caveat."""
    st.caption(f"**Source:** {source}. **As of** {as_of}. **Caveat:** {caveat}")


def pct(value: float) -> str:
    return f"{value * 100:g}%"


def bn(value: float | None) -> str:
    return "n/a" if value is None else f"${value / 1e9:,.1f}bn"


def mm(value: float | None) -> str:
    return "n/a" if value is None else f"${value / 1e6:,.1f}mm"


def money(value: float | None) -> str:
    """A stat-tile amount, compacted: $306.0bn, $45.2mm."""
    if value is None:
        return "n/a"
    return bn(value) if abs(value) >= 1e9 else mm(value)


# Display formats for st.column_config.NumberColumn.
AMOUNT = "localized"
PERCENT = "%.1f%%"  # for values already in percentage points
