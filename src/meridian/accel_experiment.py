"""Compare gyro, complementary, and Kalman roll estimates from simulated IMU data."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import platform

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian.integration import integrate_gyro
from meridian.kalman import AngleBiasKalman
from meridian.simulation import sample_accelerometer, sample_gyro, simulate_roll
from meridian.tilt import ComplementaryRoll, accel_roll, wrap_angle


@dataclass(frozen=True)
class FusionConfig:
    """Experiment settings in seconds, SI rates, and explicit degree units."""

    duration_s: float = 30.0
    sample_rate_hz: float = 100.0
    amplitude_deg: float = 20.0
    frequency_hz: float = 0.1
    bias_deg_s: float = 0.5
    gyro_noise_std_deg_s: float = 1.0
    accel_noise_std_m_s2: float = 0.2
    observation_every: int = 10
    complementary_tau_s: float = 1.0
    pulse_start_s: float = 12.0
    pulse_end_s: float = 17.0
    pulse_y_m_s2: float = 2.0


GRAVITY_M_S2 = 9.80665
# Fixed before evaluating the experiment; only the nominal default case applies.
ACCEPTANCE_LIMITS = {
    "kalman_angle_rmse_deg": 0.75,
    "complementary_angle_rmse_deg": 1.0,
    "final_bias_abs_error_deg_s": 0.15,
}


def _error_metrics(error_deg: np.ndarray, times: np.ndarray, config: FusionConfig) -> dict:
    windows = {
        "before": times < config.pulse_start_s,
        "during": (times >= config.pulse_start_s) & (times < config.pulse_end_s),
        "after": times >= config.pulse_end_s,
    }
    return {
        "rmse_deg": float(np.sqrt(np.mean(error_deg**2))),
        "max_abs_error_deg": float(np.max(np.abs(error_deg))),
        "final_error_deg": float(error_deg[-1]),
        "window_rmse_deg": {
            name: float(np.sqrt(np.mean(error_deg[mask]**2))) if np.any(mask) else None
            for name, mask in windows.items()
        },
    }


def run_trial(config: FusionConfig, seed: int, *, disturbed: bool = False) -> dict:
    """Simulate and evaluate one case, with no truth supplied to corrections."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    every = config.observation_every
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise ValueError("observation_every must be a positive integer")
    if not (0 < config.pulse_start_s < config.pulse_end_s <= config.duration_s):
        raise ValueError("pulse times must satisfy 0 < start < end <= duration")
    if not math.isfinite(config.pulse_y_m_s2):
        raise ValueError("pulse_y_m_s2 must be finite")
    truth = simulate_roll(
        duration_s=config.duration_s, sample_rate_hz=config.sample_rate_hz,
        amplitude_rad=np.deg2rad(config.amplitude_deg), frequency_hz=config.frequency_hz,
    )
    indices = np.arange(every, len(truth.time_s), every)
    if not len(indices):
        raise ValueError("the duration must include an accelerometer observation")
    observation_time = truth.time_s[indices]
    gyro_seed, accel_seed = [
        int(child.generate_state(1, dtype=np.uint64)[0])
        for child in np.random.SeedSequence(seed).spawn(2)
    ]
    gyro = sample_gyro(
        truth.interval_rate_rad_s, bias_rad_s=np.deg2rad(config.bias_deg_s),
        noise_std_rad_s=np.deg2rad(config.gyro_noise_std_deg_s), seed=gyro_seed,
    )
    translation = np.zeros((len(indices), 2))
    pulse_mask = (observation_time >= config.pulse_start_s) & (observation_time < config.pulse_end_s)
    if disturbed:
        translation[pulse_mask, 0] = config.pulse_y_m_s2
    force = sample_accelerometer(
        truth.angle_rad[indices], noise_std_m_s2=config.accel_noise_std_m_s2,
        seed=accel_seed, gravity_m_s2=GRAVITY_M_S2, translation_yz_m_s2=translation,
    )
    tilt = accel_roll(force)
    initial_angle = float(truth.angle_rad[0])
    kalman = AngleBiasKalman(
        initial_angle_rad=initial_angle, initial_bias_rad_s=0.0,
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=np.deg2rad(1.0),
        gyro_noise_std_rad_s=np.deg2rad(config.gyro_noise_std_deg_s),
        angle_noise_std_rad=config.accel_noise_std_m_s2 / GRAVITY_M_S2,
    )
    complementary = ComplementaryRoll(initial_angle_rad=initial_angle, tau_s=config.complementary_tau_s)
    gyro_roll = integrate_gyro(truth.time_s, gyro, initial_angle_rad=initial_angle)
    states = np.empty((len(truth.time_s), 2))
    covariance = np.empty((len(truth.time_s), 2, 2))
    complementary_roll = np.empty(len(truth.time_s))
    innovations = np.empty((len(indices), 2))
    states[0], covariance[0], complementary_roll[0] = kalman.state, kalman.covariance, initial_angle
    j, previous_observation_time = 0, truth.time_s[0]
    for k, dt in enumerate(np.diff(truth.time_s), start=1):
        kalman.predict(gyro[k - 1], float(dt))
        complementary.predict(gyro[k - 1], float(dt))
        if k % every == 0:
            elapsed = float(truth.time_s[k] - previous_observation_time)
            complementary.update(float(tilt[j]), elapsed)
            innovations[j] = kalman.update_wrapped_angle(float(tilt[j]))
            previous_observation_time = truth.time_s[k]
            j += 1
        states[k], covariance[k] = kalman.state, kalman.covariance
        complementary_roll[k] = complementary.angle_rad
    estimates = {"gyro": gyro_roll, "complementary": complementary_roll, "kalman": states[:, 0]}
    metrics = {}
    for name, angle in estimates.items():
        error = np.rad2deg(angle - truth.angle_rad)
        metrics[name] = _error_metrics(error, truth.time_s, config)
        metrics[name]["observation_epoch_rmse_deg"] = float(np.sqrt(np.mean(error[indices]**2)))
    metrics["accel_tilt"] = _error_metrics(
        np.rad2deg(wrap_angle(tilt - truth.angle_rad[indices])), observation_time, config
    )
    metrics["final_bias_estimate_deg_s"] = float(np.rad2deg(states[-1, 1]))
    metrics["final_bias_error_deg_s"] = metrics["final_bias_estimate_deg_s"] - config.bias_deg_s
    metrics["mean_normalized_innovation_squared"] = float(
        np.mean(innovations[:, 0]**2 / innovations[:, 1])
    )
    return {
        "truth": truth, "gyro": gyro, "indices": indices, "force": force, "tilt": tilt,
        "translation": translation, "estimates": estimates, "states": states,
        "covariance": covariance, "innovations": innovations, "metrics": metrics,
        "stream_seeds": {"gyro": gyro_seed, "accelerometer": accel_seed},
    }


def _compact(metrics: dict) -> dict:
    return {
        "gyro_angle_rmse_deg": metrics["gyro"]["rmse_deg"],
        "complementary_angle_rmse_deg": metrics["complementary"]["rmse_deg"],
        "kalman_angle_rmse_deg": metrics["kalman"]["rmse_deg"],
        "final_bias_abs_error_deg_s": abs(metrics["final_bias_error_deg_s"]),
    }


def _passes(metrics: dict) -> bool:
    return all(metrics[name] < limit for name, limit in ACCEPTANCE_LIMITS.items())


def _plot(path: Path, scenarios: dict, config: FusionConfig) -> None:
    fig = Figure(figsize=(13, 10), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 2, sharex=True)
    colors = {"gyro": "#d8821c", "complementary": "#6d5b9b", "kalman": "#29916b"}
    for col, (name, trial) in enumerate(scenarios.items()):
        truth, states = trial["truth"], trial["states"]
        times, observation_time = truth.time_s, truth.time_s[trial["indices"]]
        axes[0, col].set_title("Gravity dominated" if name == "nominal" else "Lateral acceleration pulse")
        axes[0, col].plot(times, np.rad2deg(truth.angle_rad), "--", color="#212936", label="Truth")
        axes[0, col].scatter(observation_time, np.rad2deg(trial["tilt"]), s=6,
                             color="#8794a8", alpha=0.4, label="Accel tilt")
        for method, angle in trial["estimates"].items():
            axes[0, col].plot(times, np.rad2deg(angle), color=colors[method],
                              linewidth=1.1, label=method.capitalize())
            if method != "gyro":
                axes[1, col].plot(times, np.rad2deg(angle - truth.angle_rad),
                                  color=colors[method], label=method.capitalize())
        axes[0, col].legend(ncols=3, fontsize=8, loc="upper right")
        axes[1, col].scatter(
            observation_time, np.rad2deg(wrap_angle(trial["tilt"] - truth.angle_rad[trial["indices"]])),
            color="#8794a8", alpha=0.35, s=6, label="Accel tilt",
        )
        axes[1, col].legend(fontsize=8, loc="lower left")
        bias = np.rad2deg(states[:, 1])
        std_bias = np.rad2deg(np.sqrt(trial["covariance"][:, 1, 1]))
        axes[2, col].plot(times, bias, color="#29916b", label="Kalman bias")
        axes[2, col].axhline(config.bias_deg_s, color="#212936", linestyle="--", label="True bias")
        axes[2, col].fill_between(times, bias - 2 * std_bias, bias + 2 * std_bias,
                                  color="#29916b", alpha=0.15, label="±2 model std")
        axes[2, col].legend(ncols=3, fontsize=8, loc="upper right")
        axes[3, col].plot(observation_time, np.hypot(trial["force"][:, 0], trial["force"][:, 1]) / GRAVITY_M_S2,
                          color="#326cb2", linewidth=0.8)
        axes[3, col].axhline(1.0, color="#212936", linestyle="--")
        for row, label in enumerate(("Roll (deg)", "Fusion / tilt error (deg)", "Bias (deg/s)", "y/z force magnitude / g")):
            axes[row, col].set_ylabel(label)
            axes[row, col].grid(alpha=0.2)
            axes[row, col].spines[["top", "right"]].set_visible(False)
            axes[row, col].set_xlim(times[0], times[-1])
            if name != "nominal":
                axes[row, col].axvspan(config.pulse_start_s, config.pulse_end_s,
                                       color="#d8821c", alpha=0.1)
        axes[3, col].set_xlabel("Time (s)")
    fig.suptitle("Gyro–accelerometer fusion | paired simulated measurements", fontsize=14)
    fig.savefig(path, dpi=160)


def _export_trial(directory: Path, trial: dict, config: FusionConfig) -> None:
    directory.mkdir()
    truth, states, cov = trial["truth"], trial["states"], trial["covariance"]
    times, observation_time = truth.time_s, truth.time_s[trial["indices"]]
    columns = {
        "gyro_measurements.csv": ("t_start_s,t_end_s,rate_rad_s", (times[:-1], times[1:], trial["gyro"])),
        "accel_measurements.csv": ("time_s,force_y_m_s2,force_z_m_s2", (observation_time, *trial["force"].T)),
        "derived_tilt.csv": ("time_s,tilt_rad,yz_magnitude_m_s2",
                            (observation_time, trial["tilt"], np.hypot(*trial["force"].T))),
        "truth.csv": ("time_s,roll_rad,bias_rad_s",
                      (times, truth.angle_rad, np.full(len(times), np.deg2rad(config.bias_deg_s)))),
        "disturbance_truth.csv": ("time_s,translation_y_m_s2,translation_z_m_s2",
                                  (observation_time, *trial["translation"].T)),
        "estimates.csv": (
            "time_s,gyro_roll_rad,complementary_roll_rad,kalman_roll_rad,kalman_bias_rad_s,p_angle_rad2,p_angle_bias_rad2_s,p_bias_rad2_s2",
            (times, *trial["estimates"].values(), states[:, 1], cov[:, 0, 0], cov[:, 0, 1], cov[:, 1, 1]),
        ),
        "innovations.csv": ("time_s,innovation_rad,innovation_variance_rad2",
                            (observation_time, *trial["innovations"].T)),
    }
    for name, (header, values) in columns.items():
        np.savetxt(directory / name, np.column_stack(values), delimiter=",", header=header,
                   comments="", fmt="%.17g")


def run_experiment(
    output_dir: Path, *, config: FusionConfig = FusionConfig(), seed: int = 42,
    validation_seeds: int = 20,
) -> dict:
    """Export paired scenarios and repeat metrics; preserve existing directories."""
    if isinstance(validation_seeds, bool) or not isinstance(validation_seeds, int) or validation_seeds < 1:
        raise ValueError("validation_seeds must be a positive integer")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    scenarios = {name: run_trial(config, seed, disturbed=disturbed)
                 for name, disturbed in (("nominal", False), ("translation_pulse", True))}
    repeats = [
        {"seed": value,
         "nominal": _compact(run_trial(config, value)["metrics"]),
         "translation_pulse": _compact(run_trial(config, value, disturbed=True)["metrics"])}
        for value in range(validation_seeds)
    ]
    default_config = config == FusionConfig()
    aggregates = {
        scenario: {
            metric: {"mean": float(np.mean([run[scenario][metric] for run in repeats])),
                     "worst": max(run[scenario][metric] for run in repeats)}
            for metric in repeats[0][scenario]
        }
        for scenario in scenarios
    }
    summary = {
        "schema_version": 1, "experiment": "accelerometer_fusion", "data_source": "simulation",
        "config": asdict(config), "seed": seed, "stream_seeds": scenarios["nominal"]["stream_seeds"],
        "random_generator": "SeedSequence children -> uint64 seeds -> Generator(PCG64)",
        "gravity_m_s2": GRAVITY_M_S2,
        "filter_angle_std_rad": config.accel_noise_std_m_s2 / GRAVITY_M_S2,
        "initialization": {
            "angle_rad": 0.0, "angle_std_rad": 0.0, "bias_rad_s": 0.0,
            "bias_std_rad_s": float(np.deg2rad(1.0)), "policy": "known angle shared by all methods",
        },
        "model": "FRD pure roll; endpoint specific force; fixed nominal tilt R; constant gyro bias",
        "timing": "interval-mean gyro; predict to endpoint then apply wrapped accel-tilt innovation",
        "paired_noise": "same gyro and accelerometer noise in nominal and pulse scenarios",
        "interval_count": len(scenarios["nominal"]["gyro"]),
        "observation_count": len(scenarios["nominal"]["indices"]),
        "scenarios": {name: trial["metrics"] for name, trial in scenarios.items()},
        "validation": {"seeds": list(range(validation_seeds)), "trials": repeats, "aggregates": aggregates},
        "acceptance": {
            "scope": "nominal default configuration only; strict per-run limits",
            "limits": ACCEPTANCE_LIMITS, "applied": default_config,
            "selected_passed": _passes(_compact(scenarios["nominal"]["metrics"])) if default_config else None,
            "failed_validation_seeds": [run["seed"] for run in repeats if not _passes(run["nominal"])] if default_config else None,
            "translation_pulse": "characterization of model violation; no nominal accuracy threshold",
        },
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "matplotlib": matplotlib.__version__},
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, trial in scenarios.items():
        _export_trial(output_dir / name, trial, config)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _plot(output_dir / "overview.png", scenarios, config)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/accelerometer-fusion"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-seeds", type=int, default=20)
    parser.add_argument("--accel-noise-std-m-s2", type=float, default=0.2)
    parser.add_argument("--complementary-tau-s", type=float, default=1.0)
    args = vars(parser.parse_args(argv))
    output, seed, count = args.pop("output"), args.pop("seed"), args.pop("validation_seeds")
    try:
        summary = run_experiment(output, config=FusionConfig(**args), seed=seed, validation_seeds=count)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for name, metrics in summary["scenarios"].items():
        print(f"{name}: angle RMSE gyro={metrics['gyro']['rmse_deg']:.6f}, "
              f"complementary={metrics['complementary']['rmse_deg']:.6f}, "
              f"Kalman={metrics['kalman']['rmse_deg']:.6f} deg; "
              f"final bias={metrics['final_bias_estimate_deg_s']:.6f} deg/s")
    print(f"Results written to {output}")
    acceptance = summary["acceptance"]
    if acceptance["applied"] and (not acceptance["selected_passed"] or acceptance["failed_validation_seeds"]):
        print("Nominal acceptance checks failed; inspect summary.json.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
