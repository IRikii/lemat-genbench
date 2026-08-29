"""Plotly figure for one denoising trajectory.

Three stacked panels sharing the denoising-step axis: formation energy, energy
above hull, and mean force magnitude. Frames that are not `valid` are marked with
a vertical line rather than a fake y=0 point -- zero is a meaningful energy, and
on trajectories where most frames are invalid a row of markers at zero swamps the
real data.
"""

import pandas as pd
from plotly import graph_objects as go
from plotly.subplots import make_subplots

MLIP_COLORS = {"orb": "#1f77b4", "mace": "#ff7f0e", "uma": "#2ca02c"}
MEAN_COLOR = "#9467bd"
RIBBON_COLOR = "rgba(148, 103, 189, 0.15)"

STATUS_STYLES = {
    "invalid": {"color": "#ff7f0e", "dash": "dash", "label": "invalid"},
    "undetermined": {"color": "#888888", "dash": "dot", "label": "undetermined"},
}

PANELS = [
    ("Formation energy Ef (eV/atom)", "Ef"),
    ("Energy above hull E_hull (eV/atom)", "E_hull"),
    ("Mean force magnitude (eV/A)", "forces"),
]


def build_figure(df: pd.DataFrame, batch_id: int, run_name: str) -> go.Figure:
    """Build the three-panel trajectory figure."""
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        subplot_titles=[title for title, _ in PANELS],
        vertical_spacing=0.08,
    )

    steps = df["step"]

    for row_idx, (_, prefix) in enumerate(PANELS, start=1):
        mean_values = df[f"{prefix}_mean"]
        std_values = df[f"{prefix}_std"]

        # +/- std ribbon: an invisible upper bound, then a lower bound filled to it
        fig.add_trace(
            go.Scatter(
                x=steps,
                y=mean_values + std_values,
                mode="lines",
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip",
            ),
            row=row_idx,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=steps,
                y=mean_values - std_values,
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor=RIBBON_COLOR,
                name="±std",
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
                    showlegend=row_idx == 1,
                ),
                row=row_idx,
                col=1,
            )

    # Vertical markers for frames that did not come out valid
    for status, style in STATUS_STYLES.items():
        flagged = df.loc[df["status"] == status, "step"]
        if flagged.empty:
            continue
        for step in flagged:
            for row_idx in (1, 2, 3):
                fig.add_vline(
                    x=step,
                    line=dict(color=style["color"], width=1.5, dash=style["dash"]),
                    row=row_idx,
                    col=1,
                )
        # Dummy trace purely to give the vertical lines a legend entry
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="lines",
                line=dict(color=style["color"], width=1.5, dash=style["dash"]),
                name=f"{style['label']} ({len(flagged)})",
                visible="legendonly",
            ),
            row=1,
            col=1,
        )

    fig.update_xaxes(
        tickmode="array",
        tickvals=steps.tolist(),
        ticktext=[str(s) for s in steps],
    )
    fig.update_xaxes(title_text="Denoising step", row=3, col=1)
    fig.update_layout(
        title=f"Denoising trajectory · gen_{batch_id} · {run_name}",
        height=900,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    return fig
