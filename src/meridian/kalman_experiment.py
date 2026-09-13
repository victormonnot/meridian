"""Compare gyro integration with a linear angle/bias Kalman reference."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import platform
from pathlib import Path

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian.integration import integrate_gyro
from meridian.kalman import AngleBiasKalman
from meridian.simulation import sample_angle, sample_gyro, simulate_roll


@dataclass(frozen=True)
class ExperimentConfig:
    """Illustrative simulation parameters; angles here are in display units."""

    duration_s: float = 30.0
    sample_rate_hz: float = 100.0
    amplitude_deg: float = 20.0
    frequency_hz: float = 0.1
    bias_deg_s: float = 0.5
    gyro_noise_std_deg_s: float = 1.0
    angle_noise_std_deg: float = 2.0
    observation_every: int = 10


# Declared in docs/linear-kalman.md before evaluating the new experiment.
ACCEPTANCE_LIMITS = {"angle_rmse_deg": 0.75, "final_bias_abs_error_deg_s": 0.15}


def _rmse(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(values**2)))


def run_trial(config: ExperimentConfig, seed: int) -> dict:
    """Run one seeded simulation; only this experiment layer uses truth."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    every = config.observation_every
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise ValueError("observation_every must be a positive integer")
    truth = simulate_roll(
        duration_s=config.duration_s, sample_rate_hz=config.sample_rate_hz,
        amplitude_rad=np.deg2rad(config.amplitude_deg), frequency_hz=config.frequency_hz,
    )
    observation_indices = np.arange(every, len(truth.time_s), every)
    if not len(observation_indices):
        raise ValueError("the duration must include at least one angle observation")
    children = np.random.SeedSequence(seed).spawn(2)
    gyro_seed, angle_seed = [
        int(child.generate_state(1, dtype=np.uint64)[0]) for child in children
    ]
    gyro = sample_gyro(
        truth.interval_rate_rad_s, bias_rad_s=np.deg2rad(config.bias_deg_s),
        noise_std_rad_s=np.deg2rad(config.gyro_noise_std_deg_s), seed=gyro_seed,
    )
    observations = sample_angle(
        truth.angle_rad[observation_indices],
        noise_std_rad=np.deg2rad(config.angle_noise_std_deg), seed=angle_seed,
    )
    initial_angle = float(truth.angle_rad[0])
    gyro_roll = integrate_gyro(truth.time_s, gyro, initial_angle_rad=initial_angle)
    estimator = AngleBiasKalman(
        initial_angle_rad=initial_angle, initial_bias_rad_s=0.0,
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=np.deg2rad(1.0),
        gyro_noise_std_rad_s=np.deg2rad(config.gyro_noise_std_deg_s),
        angle_noise_std_rad=np.deg2rad(config.angle_noise_std_deg),
    )
    states = np.empty((len(truth.time_s), 2))
    covariances = np.empty((len(truth.time_s), 2, 2))
    innovations = np.empty(len(observations))
    innovation_variances = np.empty(len(observations))
    states[0], covariances[0] = estimator.state, estimator.covariance
    j = 0
    for k, dt in enumerate(np.diff(truth.time_s), start=1):
        estimator.predict(gyro[k - 1], float(dt))
        if k % every == 0:
            innovations[j], innovation_variances[j] = estimator.update(observations[j])
            j += 1
        states[k], covariances[k] = estimator.state, estimator.covariance

    gyro_error = np.rad2deg(gyro_roll - truth.angle_rad)
    kalman_error = np.rad2deg(states[:, 0] - truth.angle_rad)
    bias_error = np.rad2deg(states[:, 1]) - config.bias_deg_s
    metrics = {
        "gyro_angle_rmse_deg": _rmse(gyro_error),
        "kalman_angle_rmse_deg": _rmse(kalman_error),
        "gyro_final_error_deg": float(gyro_error[-1]),
        "kalman_final_error_deg": float(kalman_error[-1]),
        "gyro_observation_epoch_rmse_deg": _rmse(gyro_error[observation_indices]),
        "kalman_observation_epoch_rmse_deg": _rmse(kalman_error[observation_indices]),
        "raw_angle_observation_rmse_deg": _rmse(
            np.rad2deg(observations - truth.angle_rad[observation_indices])
        ),
        "final_bias_estimate_deg_s": float(np.rad2deg(states[-1, 1])),
        "final_bias_error_deg_s": float(bias_error[-1]),
        "bias_rmse_deg_s": _rmse(bias_error),
        "mean_normalized_innovation_squared": float(
            np.mean(innovations**2 / innovation_variances)
        ),
    }
    return {
        "truth": truth, "gyro": gyro, "observation_indices": observation_indices,
        "observations": observations, "gyro_roll": gyro_roll, "states": states,
        "covariances": covariances, "innovations": innovations,
        "innovation_variances": innovation_variances, "metrics": metrics,
        "stream_seeds": {"gyro": gyro_seed, "angle": angle_seed},
    }


def _plot(path: Path, trial: dict, config: ExperimentConfig) -> None:
    fig = Figure(figsize=(11, 11), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 1, sharex=True)
    truth, states = trial["truth"], trial["states"]
    time_s = truth.time_s
    observation_time = time_s[trial["observation_indices"]]
    std = np.rad2deg(np.sqrt(np.diagonal(trial["covariances"], axis1=1, axis2=2)))
    axes[0].scatter(observation_time, np.rad2deg(trial["observations"]),
                    s=9, color="#8794a8", alpha=0.4, label="Noisy angle")
    axes[0].plot(time_s, np.rad2deg(trial["gyro_roll"]), color="#d8821c", label="Gyro only")
    axes[0].plot(time_s, np.rad2deg(states[:, 0]), color="#29916b", label="Kalman")
    axes[0].plot(time_s, np.rad2deg(truth.angle_rad), "--", color="#212936", label="Truth")
    axes[0].set_ylabel("Roll (deg)")
    axes[0].legend(ncols=4, fontsize=9, loc="upper right")
    for values, color, label in (
        (trial["gyro_roll"], "#d8821c", "Gyro only"),
        (states[:, 0], "#29916b", "Kalman"),
    ):
        axes[1].plot(time_s, np.rad2deg(values - truth.angle_rad), color=color, label=label)
    axes[1].fill_between(time_s, -2 * std[:, 0], 2 * std[:, 0],
                          color="#29916b", alpha=0.18, label="Kalman ±2 model std")
    axes[1].set_ylabel("Angle error (deg)")
    axes[1].legend(fontsize=9, loc="upper left")
    bias = np.rad2deg(states[:, 1])
    axes[2].plot(time_s, bias, color="#326cb2", label="Estimated bias")
    axes[2].axhline(config.bias_deg_s, color="#212936", linestyle="--", label="True bias")
    axes[2].fill_between(time_s, bias - 2 * std[:, 1], bias + 2 * std[:, 1],
                          color="#326cb2", alpha=0.18, label="±2 model std")
    axes[2].set_ylabel("Bias (deg/s)")
    axes[2].legend(ncols=3, fontsize=9, loc="upper right")
    axes[3].plot(observation_time, trial["innovations"] / np.sqrt(trial["innovation_variances"]),
                 color="#6d5b9b", linewidth=0.7)
    axes[3].axhline(2, color="#8794a8", linestyle="--", linewidth=0.8)
    axes[3].axhline(-2, color="#8794a8", linestyle="--", linewidth=0.8)
    axes[3].set_ylabel("Innovation / sqrt(S)")
    axes[3].set_xlabel("Time (s)")
    for axis in axes:
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_xlim(time_s[0], time_s[-1])
    fig.suptitle("Linear Kalman reference | simulated roll and constant gyro bias", fontsize=14)
    fig.savefig(path, dpi=160)


def _passes(metrics: dict) -> bool:
    return (
        metrics["kalman_angle_rmse_deg"] < ACCEPTANCE_LIMITS["angle_rmse_deg"]
        and abs(metrics["final_bias_error_deg_s"]) < ACCEPTANCE_LIMITS["final_bias_abs_error_deg_s"]
    )


def run_experiment(
    output_dir: Path, *, config: ExperimentConfig = ExperimentConfig(),
    seed: int = 42, validation_seeds: int = 20,
) -> dict:
    """Export one trial and metrics for seeds 0 through validation_seeds - 1."""
    if isinstance(validation_seeds, bool) or not isinstance(validation_seeds, int) or validation_seeds < 1:
        raise ValueError("validation_seeds must be a positive integer")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    selected = run_trial(config, seed)
    trials = [
        {"seed": value, **run_trial(config, value)["metrics"]}
        for value in range(validation_seeds)
    ]
    default_config = config == ExperimentConfig()
    summary = {
        "schema_version": 1, "experiment": "linear_kalman", "data_source": "simulation",
        "config": asdict(config), "seed": seed, "stream_seeds": selected["stream_seeds"],
        "random_generator": "SeedSequence children -> uint64 seeds -> Generator(PCG64)",
        "initialization": {
            "angle_rad": 0.0, "angle_std_rad": 0.0,
            "bias_rad_s": 0.0, "bias_std_rad_s": float(np.deg2rad(1.0)),
            "policy": "known initial angle shared by both methods; unknown constant bias",
        },
        "noise_model": "independent per-sample gyro and direct-angle noise; matching filter Q/R",
        "timing": "interval-mean gyro; predict to endpoint before any angle correction",
        "interval_count": len(selected["gyro"]),
        "observation_count": len(selected["observations"]),
        "metrics": selected["metrics"],
        "validation": {
            "seeds": list(range(validation_seeds)), "trials": trials,
            "mean_kalman_angle_rmse_deg": float(np.mean([m["kalman_angle_rmse_deg"] for m in trials])),
            "worst_kalman_angle_rmse_deg": max(m["kalman_angle_rmse_deg"] for m in trials),
            "mean_final_bias_abs_error_deg_s": float(np.mean([abs(m["final_bias_error_deg_s"]) for m in trials])),
            "worst_final_bias_abs_error_deg_s": max(abs(m["final_bias_error_deg_s"]) for m in trials),
        },
        "acceptance": {
            "scope": "default configuration; strict limits applied per trial",
            "limits": ACCEPTANCE_LIMITS, "applied": default_config,
            "selected_passed": _passes(selected["metrics"]) if default_config else None,
            "failed_validation_seeds": [m["seed"] for m in trials if not _passes(m)] if default_config else None,
        },
        "environment": {
            "python": platform.python_version(), "numpy": np.__version__,
            "matplotlib": matplotlib.__version__,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    truth, states, covariance = selected["truth"], selected["states"], selected["covariances"]
    exports = {
        "gyro_measurements.csv": (
            "t_start_s,t_end_s,rate_rad_s",
            (truth.time_s[:-1], truth.time_s[1:], selected["gyro"]),
        ),
        "angle_measurements.csv": (
            "time_s,angle_rad",
            (truth.time_s[selected["observation_indices"]], selected["observations"]),
        ),
        "truth.csv": (
            "time_s,roll_rad,bias_rad_s",
            (truth.time_s, truth.angle_rad, np.full(len(truth.time_s), np.deg2rad(config.bias_deg_s))),
        ),
        "estimates.csv": (
            "time_s,gyro_roll_rad,kalman_roll_rad,kalman_bias_rad_s,p_angle_rad2,p_angle_bias_rad2_s,p_bias_rad2_s2",
            (truth.time_s, selected["gyro_roll"], states[:, 0], states[:, 1],
             covariance[:, 0, 0], covariance[:, 0, 1], covariance[:, 1, 1]),
        ),
        "innovations.csv": (
            "time_s,innovation_rad,innovation_variance_rad2",
            (truth.time_s[selected["observation_indices"]],
             selected["innovations"], selected["innovation_variances"]),
        ),
    }
    for name, (header, columns) in exports.items():
        np.savetxt(output_dir / name, np.column_stack(columns), delimiter=",",
                   header=header, comments="", fmt="%.17g")
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    _plot(output_dir / "overview.png", selected, config)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/linear-kalman"),
                        help="new output directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-seeds", type=int, default=20,
                        help="also evaluate root seeds 0 through this count minus one")
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--amplitude-deg", type=float, default=20.0)
    parser.add_argument("--angle-noise-std-deg", type=float, default=2.0)
    parser.add_argument("--observation-every", type=int, default=10,
                        help="one angle observation every N gyro intervals")
    args = vars(parser.parse_args(argv))
    output, seed, count = args.pop("output"), args.pop("seed"), args.pop("validation_seeds")
    try:
        summary = run_experiment(output, config=ExperimentConfig(**args), seed=seed,
                                 validation_seeds=count)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    metrics = summary["metrics"]
    print(f"Angle RMSE: gyro = {metrics['gyro_angle_rmse_deg']:.6f} deg, "
          f"Kalman = {metrics['kalman_angle_rmse_deg']:.6f} deg")
    print(f"Final bias estimate = {metrics['final_bias_estimate_deg_s']:.6f} deg/s")
    print(f"Results written to {output}")
    acceptance = summary["acceptance"]
    if acceptance["applied"] and (
        not acceptance["selected_passed"] or acceptance["failed_validation_seeds"]
    ):
        print("Default-configuration acceptance checks failed; inspect summary.json.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
