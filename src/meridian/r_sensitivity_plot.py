"""Plot paired accelerometer covariance trials without inferring confidence bounds."""

from __future__ import annotations

from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np


_SETTINGS = ("0.1", "0.2", "0.6")
_CASES = (("nominal", "Nominal"),
          ("accel_noise_mismatch", "Higher accelerometer noise"))
_ROWS = (("metrics", "angle_rmse_deg", "Full-run angle RMSE (deg)"),
         ("metrics", "bias_rmse_deg_s", "Full-run bias RMSE (deg/s)"),
         ("metrics", "mean_nis", "Mean joint NIS"),
         ("delta_from_default", "angle_rmse_deg", "Paired angle RMSE difference (deg)"))


def plot_summary(path: Path, summary: dict) -> None:
    """Write a deterministic PNG of run scores, means, and observed ranges.

    Horizontal positions are categories, not a continuous parameter sweep. Each
    seed keeps the same small horizontal offset across settings and scenarios.
    Vertical scales are independent, and no interval is a confidence interval.
    """
    seeds = sorted(summary["seeds"])
    offsets = np.linspace(-0.16, 0.16, len(seeds)) if len(seeds) > 1 else np.zeros(1)
    figure = Figure(figsize=(12, 12.5), facecolor="white")
    FigureCanvasAgg(figure)
    axes = figure.subplots(len(_ROWS), len(_CASES), sharex="col")
    figure.subplots_adjust(left=0.10, right=0.97, bottom=0.14, top=0.86,
                           hspace=0.32, wspace=0.30)

    for column, (case, title) in enumerate(_CASES):
        scenario = summary["scenarios"][case]
        trials = {trial["seed"]: trial for trial in scenario["trials"]}
        for row, (group, metric, label) in enumerate(_ROWS):
            axis = axes[row, column]
            for position, setting in enumerate(_SETTINGS):
                values = np.array([trials[seed]["settings"][setting][group][metric]
                                   for seed in seeds], dtype=float)
                mean = float(np.mean(values))
                lower, upper = float(np.min(values)), float(np.max(values))
                axis.vlines(position, lower, upper, color="0.50", linewidth=1.3,
                            zorder=2)
                axis.hlines([lower, upper], position - 0.07, position + 0.07,
                            color="0.50", linewidth=1.3, zorder=2)
                axis.scatter(position + offsets, values, s=17, color="0.57",
                             edgecolors="none", zorder=3)
                axis.scatter(position, mean, s=40, marker="D", color="0.10",
                             edgecolors="white", linewidths=0.6, zorder=4)

            axis.set_xlim(-0.4, len(_SETTINGS) - 0.6)
            axis.set_xticks(range(len(_SETTINGS)), ("0.1", "0.2\n(default)", "0.6"))
            axis.set_ylabel(label, fontsize=10, color="0.15")
            axis.tick_params(labelsize=9, colors="0.25")
            axis.set_axisbelow(True)
            axis.grid(axis="y", color="0.90", linewidth=0.6)
            for side in ("top", "right"):
                axis.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                axis.spines[side].set_color("0.75")
                axis.spines[side].set_linewidth(0.6)

            if group == "metrics":
                axis.set_ylim(bottom=0)
            else:
                axis.axhline(0, color="0.25", linestyle=":", linewidth=1, zorder=1)
            if metric == "mean_nis":
                axis.axhline(2, color="0.25", linestyle=":", linewidth=1, zorder=1)
                axis.set_ylim(top=max(axis.get_ylim()[1], 2.6))
                axis.annotate("2: model reference", (0.98, 2),
                              xycoords=("axes fraction", "data"), xytext=(0, 4),
                              textcoords="offset points", ha="right", va="bottom",
                              fontsize=8, color="0.30",
                              bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
            if row == 0:
                noise = scenario["simulated_accel_std_m_s2"]
                axis.set_title(f"{title}\nSimulated component noise std: {noise:g} m/s²",
                               fontsize=11, color="0.10", pad=12, linespacing=1.5)
            if row == len(_ROWS) - 1:
                axis.set_xlabel("Assumed component noise std (m/s²)",
                                fontsize=10, labelpad=10, color="0.15")

    phase = summary["phase"].capitalize()
    seed_range = f"{seeds[0]}–{seeds[-1]}" if len(seeds) > 1 else str(seeds[0])
    figure.suptitle(f"Accelerometer covariance sensitivity | {phase}",
                   y=0.971, fontsize=16, color="0.10")
    figure.text(0.5, 0.934,
                f"{len(seeds)} paired noise seeds ({seed_range}) per case · "
                "Vector EKF · Independent vertical scales",
                ha="center", fontsize=10, color="0.35")
    figure.text(0.10, 0.035,
                "Dots: individual runs. Diamonds: arithmetic means. Bars: observed min–max, "
                "not confidence intervals.\n"
                "Paired difference: each setting minus the default on the same measurements; "
                "negative means smaller angle RMSE.\n"
                "R = σ² I₂. Cases share noise draws at different amplitudes; "
                "the NIS line is a model reference, not an acceptance threshold.",
                fontsize=9, color="0.30", linespacing=1.6)
    figure.savefig(path, dpi=160, facecolor="white", metadata={"Software": "Meridian"})
