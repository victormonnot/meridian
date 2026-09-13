"""Run and export a controlled experiment on gyroscope integration drift."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

import matplotlib
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from meridian.integration import integrate_gyro
from meridian.simulation import RollTruth, sample_gyro, simulate_roll


def _plot_results(
    path: Path,
    truth: RollTruth,
    measurements: dict[str, np.ndarray],
    estimates: dict[str, np.ndarray],
) -> None:
    """Plot angles, interval-average rates, and errors without a display server."""
    fig = Figure(figsize=(11, 9), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(3, 1, sharex=True)
    colors = {"ideal": "#29916b", "bias_only": "#d8821c", "bias_and_noise": "#326cb2"}
    labels = {"ideal": "Ideal gyro", "bias_only": "Bias only", "bias_and_noise": "Bias + noise"}
    midpoint_s = 0.5 * (truth.time_s[:-1] + truth.time_s[1:])
    for name in measurements:
        style = {"color": colors[name], "label": labels[name], "linewidth": 1.6}
        axes[0].plot(truth.time_s, np.rad2deg(estimates[name]), **style)
        axes[1].plot(
            midpoint_s,
            np.rad2deg(measurements[name]),
            color=colors[name],
            linewidth=0.7 if name == "bias_and_noise" else 1.5,
            alpha=0.6 if name == "bias_and_noise" else 1.0,
        )
        axes[2].plot(
            truth.time_s, np.rad2deg(estimates[name] - truth.angle_rad), **style
        )
    axes[0].plot(
        truth.time_s,
        np.rad2deg(truth.angle_rad),
        color="#212936",
        linestyle="--",
        linewidth=1.2,
        label="Ground truth",
    )
    axes[0].set_ylabel("Roll angle (deg)")
    axes[1].set_ylabel("Gyro rate (deg/s)")
    axes[2].set_ylabel("Angle error (deg)")
    axes[2].set_xlabel("Time (s)")
    axes[0].legend(loc="upper right", ncols=4, fontsize=9)
    for axis in axes:
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_xlim(truth.time_s[0], truth.time_s[-1])
    fig.suptitle("Gyroscope integration | simulated roll", fontsize=15)
    fig.savefig(path, dpi=160)


def run_experiment(
    output_dir: Path,
    *,
    duration_s: float = 30.0,
    sample_rate_hz: float = 100.0,
    amplitude_deg: float = 20.0,
    frequency_hz: float = 0.1,
    bias_deg_s: float = 0.5,
    noise_std_deg_s: float = 1.0,
    seed: int = 42,
) -> dict:
    """Compare three gyro conditions, using a known initial angle in every case.

    The output directory must not already exist. Input rates are interval means
    in rad/s; truth is retained separately for evaluation. Noise is independent
    Gaussian rate noise with a standard deviation specified per interval sample.
    """
    truth = simulate_roll(
        duration_s=duration_s,
        sample_rate_hz=sample_rate_hz,
        amplitude_rad=np.deg2rad(amplitude_deg),
        frequency_hz=frequency_hz,
    )
    bias_rad_s = np.deg2rad(bias_deg_s)
    measurements = {
        "ideal": sample_gyro(truth.interval_rate_rad_s),
        "bias_only": sample_gyro(truth.interval_rate_rad_s, bias_rad_s=bias_rad_s),
        "bias_and_noise": sample_gyro(
            truth.interval_rate_rad_s,
            bias_rad_s=bias_rad_s,
            noise_std_rad_s=np.deg2rad(noise_std_deg_s),
            seed=seed,
        ),
    }
    initial_angle_rad = float(truth.angle_rad[0])
    estimates = {
        name: integrate_gyro(truth.time_s, rates, initial_angle_rad=initial_angle_rad)
        for name, rates in measurements.items()
    }
    metrics = {}
    for name, angles in estimates.items():
        error_deg = np.rad2deg(angles - truth.angle_rad)
        metrics[name] = {
            "angle_rmse_deg": float(np.sqrt(np.mean(error_deg**2))),
            "max_abs_error_deg": float(np.max(np.abs(error_deg))),
            "final_error_deg": float(error_deg[-1]),
        }
    summary = {
        "schema_version": 1,
        "experiment": "gyro_drift",
        "data_source": "simulation",
        "config": {
            "duration_s": duration_s,
            "sample_rate_hz": sample_rate_hz,
            "amplitude_deg": amplitude_deg,
            "frequency_hz": frequency_hz,
            "bias_deg_s": bias_deg_s,
            "noise_std_deg_s": noise_std_deg_s,
            "seed": seed,
        },
        "interval_count": len(truth.interval_rate_rad_s),
        "measurement_semantics": "mean angular rate over [t_start_s, t_end_s]",
        "noise_model": "independent Gaussian noise per interval-mean rate sample",
        "random_generator": "NumPy Generator(PCG64)",
        "initialization": "known initial angle, identical for all conditions",
        "initial_angle_rad": initial_angle_rad,
        "metrics": metrics,
        "analytic_predictions": {
            "bias_only_final_error_deg": bias_deg_s * duration_s,
            "noise_only_final_error_std_deg": float(
                noise_std_deg_s * np.sqrt(np.sum(np.diff(truth.time_s) ** 2))
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
    }

    # Refuse an existing directory to preserve earlier runs and unrelated files.
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    np.savetxt(
        output_dir / "measurements.csv",
        np.column_stack((truth.time_s[:-1], truth.time_s[1:], *measurements.values())),
        delimiter=",",
        header="t_start_s,t_end_s,ideal_rad_s,bias_only_rad_s,bias_and_noise_rad_s",
        comments="",
        fmt="%.17g",
    )
    np.savetxt(
        output_dir / "truth.csv",
        np.column_stack((truth.time_s, truth.angle_rad)),
        delimiter=",",
        header="time_s,roll_rad",
        comments="",
        fmt="%.17g",
    )
    np.savetxt(
        output_dir / "estimates.csv",
        np.column_stack((truth.time_s, *estimates.values())),
        delimiter=",",
        header="time_s,ideal_rad,bias_only_rad,bias_and_noise_rad",
        comments="",
        fmt="%.17g",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    _plot_results(output_dir / "overview.png", truth, measurements, estimates)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/gyro-drift"),
                        help="new output directory (default: outputs/gyro-drift)")
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--sample-rate-hz", type=float, default=100.0)
    parser.add_argument("--amplitude-deg", type=float, default=20.0,
                        help="roll amplitude; use 0 for a stationary experiment")
    parser.add_argument("--frequency-hz", type=float, default=0.1)
    parser.add_argument("--bias-deg-s", type=float, default=0.5)
    parser.add_argument("--noise-std-deg-s", type=float, default=1.0,
                        help="rate noise standard deviation per interval sample")
    parser.add_argument("--seed", type=int, default=42)
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    try:
        summary = run_experiment(output, **args)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for name, values in summary["metrics"].items():
        print(f"{name}: final error = {values['final_error_deg']:.6f} deg, "
              f"RMSE = {values['angle_rmse_deg']:.6f} deg")
    print(f"Results written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
