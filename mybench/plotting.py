"""Plotly figure for one denoising trajectory.

Three stacked panels sharing the denoising-step axis: formation energy, energy
above hull, and mean force magnitude. Frames that are not `valid` are marked with
a vertical line rather than a fake y=0 point -- zero is a meaningful energy, and
on trajectories where most frames are invalid a row of markers at zero swamps the
real data.

Every trace carries a ``legendgroup`` so that one legend click toggles that series
across all three panels; plotly only ties traces together that way when the group
is set explicitly. The status markers are real Scatter traces rather than
``add_vline`` shapes for the same reason -- shapes cannot be toggled from the
legend at all.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from plotly import graph_objects as go
from plotly.subplots import make_subplots

# tab10, kept mutually distinct: `invalid` used to reuse mace's orange, which made
# the two indistinguishable on the same panel.
MLIP_COLORS = {"orb": "#1f77b4", "mace": "#ff7f0e", "uma": "#2ca02c"}
MEAN_COLOR = "#9467bd"
RIBBON_COLOR = "rgba(148, 103, 189, 0.15)"
RELAXED_COLOR = "#17becf"

STATUS_STYLES = {
    "invalid": {"color": "#d62728", "dash": "dash", "label": "invalid"},
    "undetermined": {"color": "#7f7f7f", "dash": "dot", "label": "undetermined"},
}

PANELS = [
    ("Formation energy Ef (eV/atom)", "Ef"),
    ("Energy above hull E_hull (eV/atom)", "E_hull"),
    # Forces are always computed on the input structure, before any relaxation
    # (multi_mlip_preprocess.py:458-461), so this panel has no relaxed counterpart.
    ("Mean force magnitude (eV/A, single-point only)", "forces"),
]

# Panels that gain a "relaxed" overlay when the run was done with --relax
RELAXABLE_PREFIXES = {"Ef", "E_hull"}


def _panel_span(series_list: List[pd.Series]) -> Tuple[float, float]:
    """Y span covering every finite value plotted in one panel, plus 5% padding.

    Used to size the status marker lines. Two degenerate cases matter in practice:
    a panel where every value is NaN (a trajectory whose frames all contain an
    element the MLIPs reject), and a panel with a single finite point (only the
    final frame survived), where min == max would give a zero-height line.
    """
    values = np.concatenate(
        [pd.to_numeric(s, errors="coerce").to_numpy(dtype=float) for s in series_list]
    )
    finite = values[np.isfinite(values)]

    if finite.size == 0:
        return 0.0, 1.0

    low, high = float(finite.min()), float(finite.max())
    if low == high:
        pad = max(abs(low) * 0.1, 0.1)
    else:
        pad = (high - low) * 0.05
    return low - pad, high + pad


def _status_line_coords(
    steps: List[float], low: float, high: float
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """One trace's worth of disjoint vertical segments, separated by None."""
    xs: List[Optional[float]] = []
    ys: List[Optional[float]] = []
    for step in steps:
        xs.extend([step, step, None])
        ys.extend([low, high, None])
    return xs, ys


def build_figure(df: pd.DataFrame, batch_id: int, run_name: str) -> go.Figure:
    """Build the three-panel trajectory figure.

    When the DataFrame carries ``relaxed_*`` columns (a ``--relax`` run) the
    Ef and E_hull panels gain the relaxed ensemble mean as a dotted overlay.
    """
    has_relaxed = "relaxed_Ef_mean" in df.columns

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        subplot_titles=[title for title, _ in PANELS],
        vertical_spacing=0.08,
    )

    steps = df["step"]
    panel_spans: Dict[int, Tuple[float, float]] = {}

    for row_idx, (_, prefix) in enumerate(PANELS, start=1):
        mean_values = df[f"{prefix}_mean"]
        std_values = df[f"{prefix}_std"]
        upper, lower = mean_values + std_values, mean_values - std_values

        plotted = [upper, lower, mean_values] + [
            df[f"{prefix}_{m}"] for m in MLIP_COLORS
        ]

        show_relaxed = has_relaxed and prefix in RELAXABLE_PREFIXES
        if show_relaxed:
            plotted.append(df[f"relaxed_{prefix}_mean"])

        panel_spans[row_idx] = _panel_span(plotted)

        # +/- std ribbon: an invisible upper bound, then a lower bound filled to it.
        # Both halves share a legendgroup so one click hides the whole band.
        fig.add_trace(
            go.Scatter(
                x=steps,
                y=upper,
                mode="lines",
                line=dict(width=0),
                legendgroup="std",
                showlegend=False,
                hoverinfo="skip",
            ),
            row=row_idx,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=steps,
                y=lower,
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor=RIBBON_COLOR,
                name="±std",
                legendgroup="std",
                showlegend=row_idx == 1,
                hoverinfo="skip",
            ),
            row=row_idx,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=steps,
                y=mean_values,
                mode="lines+markers",
                line=dict(color=MEAN_COLOR, width=2, dash="dash"),
                marker=dict(size=6),
                name="mean",
                legendgroup="mean",
                showlegend=row_idx == 1,
            ),
            row=row_idx,
            col=1,
        )

        if show_relaxed:
            fig.add_trace(
                go.Scatter(
                    x=steps,
                    y=df[f"relaxed_{prefix}_mean"],
                    mode="lines+markers",
                    line=dict(color=RELAXED_COLOR, width=2, dash="dot"),
                    marker=dict(size=6, symbol="diamond"),
                    name="relaxed mean",
                    legendgroup="relaxed",
                    showlegend=row_idx == 1,
                ),
                row=row_idx,
                col=1,
            )

        for mlip, color in MLIP_COLORS.items():
            fig.add_trace(
                go.Scatter(
                    x=steps,
                    y=df[f"{prefix}_{mlip}"],
                    mode="lines+markers",
                    line=dict(color=color, width=1.5),
                    marker=dict(size=5),
                    name=mlip,
                    legendgroup=mlip,
                    showlegend=row_idx == 1,
                ),
                row=row_idx,
                col=1,
            )

    # Status markers: real traces (not add_vline shapes) so the legend can toggle them
    for status, style in STATUS_STYLES.items():
        flagged = df.loc[df["status"] == status, "step"].tolist()
        if not flagged:
            continue
        for row_idx in (1, 2, 3):
            low, high = panel_spans[row_idx]
            xs, ys = _status_line_coords(flagged, low, high)
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="lines",
                    line=dict(color=style["color"], width=1.5, dash=style["dash"]),
                    name=f"{style['label']} ({len(flagged)})",
                    legendgroup=status,
                    showlegend=row_idx == 1,
                    hoverinfo="skip",
                ),
                row=row_idx,
                col=1,
            )

    fig.update_xaxes(
        tickmode="array",
        tickvals=steps.tolist(),
        ticktext=[str(s) for s in steps],
    )
    fig.update_xaxes(title_text="Denoising step", row=3, col=1)

    title = f"Denoising trajectory · gen_{batch_id} · {run_name}"
    if has_relaxed:
        title += " · single-point vs relaxed"

    fig.update_layout(
        title=title,
        height=900,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    return fig
