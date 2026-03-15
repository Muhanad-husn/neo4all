"""
ui/components/plotly_helpers.py — Shared Plotly figure factories for dashboard tabs.

Provides a consistent visual style across all dashboard charts.  Every function
returns a ``go.Figure`` rendered via ``st.plotly_chart(fig, use_container_width=True)``.

Architecture rules:
  - Pure figure factories — no business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any, Sequence

import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Colour palette — clean modern aesthetic
# ---------------------------------------------------------------------------

COLORS: list[str] = [
    "#636EFA",
    "#EF553B",
    "#00CC96",
    "#AB63FA",
    "#FFA15A",
    "#19D3F3",
    "#FF6692",
    "#B6E880",
]


def _base_layout(**overrides: Any) -> dict[str, Any]:
    """Shared layout defaults for all figures."""
    defaults: dict[str, Any] = {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "margin": dict(l=40, r=20, t=40, b=30),
        "font": dict(size=12),
    }
    defaults.update(overrides)
    return defaults


def bar_chart(
    labels: Sequence[str],
    values: Sequence[int | float],
    title: str,
    *,
    horizontal: bool = False,
    color: str | Sequence[str] | None = None,
) -> go.Figure:
    """Simple bar chart (vertical or horizontal)."""
    colours = color if color is not None else COLORS[: len(labels)]
    if horizontal:
        fig = go.Figure(go.Bar(y=list(labels), x=list(values), orientation="h", marker_color=colours))
    else:
        fig = go.Figure(go.Bar(x=list(labels), y=list(values), marker_color=colours))
    fig.update_layout(**_base_layout(title=title))
    return fig


def donut_chart(
    labels: Sequence[str],
    values: Sequence[int | float],
    title: str,
) -> go.Figure:
    """Donut (ring) chart."""
    fig = go.Figure(
        go.Pie(
            labels=list(labels),
            values=list(values),
            hole=0.45,
            marker=dict(colors=COLORS[: len(labels)]),
        )
    )
    fig.update_layout(**_base_layout(title=title))
    return fig


def gauge_chart(
    value: float,
    title: str,
    *,
    max_val: float = 1.0,
    suffix: str = "%",
) -> go.Figure:
    """Gauge / indicator chart for a single numeric value."""
    display = value * 100 if max_val <= 1.0 else value
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=display,
            number=dict(suffix=suffix),
            title=dict(text=title),
            gauge=dict(
                axis=dict(range=[0, 100 if max_val <= 1.0 else max_val]),
                bar=dict(color=COLORS[0]),
                bgcolor="rgba(200,200,200,0.2)",
            ),
        )
    )
    fig.update_layout(**_base_layout(height=250, margin=dict(l=30, r=30, t=60, b=10)))
    return fig


def stacked_bar(
    labels: Sequence[str],
    field_values: dict[str, Sequence[int | float]],
    title: str,
    *,
    horizontal: bool = False,
) -> go.Figure:
    """Stacked bar chart with multiple series."""
    fig = go.Figure()
    for idx, (name, vals) in enumerate(field_values.items()):
        colour = COLORS[idx % len(COLORS)]
        if horizontal:
            fig.add_trace(go.Bar(y=list(labels), x=list(vals), name=name, orientation="h", marker_color=colour))
        else:
            fig.add_trace(go.Bar(x=list(labels), y=list(vals), name=name, marker_color=colour))
    fig.update_layout(barmode="stack", **_base_layout(title=title))
    return fig
