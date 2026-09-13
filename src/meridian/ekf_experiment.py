"""Compare vector-accelerometer EKF and existing baselines on shared simulations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian import accel_experiment as baseline
from meridian.accel_experiment import FusionConfig, GRAVITY_M_S2
from meridian.ekf import AngleBiasEKF


# Declared before the experiment, for the nominal default configuration only.
# Match the existing reference's limits; no requirement to outperform it.
ACCEPTANCE_LIMITS = {
    "ekf_angle_rmse_deg": 0.75,
    "ekf_final_bias_abs_error_deg_s": 0.15,
}


def run_trial(config: FusionConfig, seed: int, *, disturbed: bool = False) -> dict:
    """Run EKF on exactly the measurements already used by the baseline trial."""
    trial = baseline.run_trial(config, seed, disturbed=disturbed)
    times = trial["truth"].time_s
    estimator = AngleBiasEKF(
        initial_angle_rad=float(trial["estimates"]["gyro"][0]), initial_bias_rad_s=0.0,
        initial_angle_std_rad=0.0, initial_bias_std_rad_s=float(np.deg2rad(1.0)),
        gyro_noise_std_rad_s=float(np.deg2rad(config.gyro_noise_std_deg_s)),
        accel_noise_std_m_s2=config.accel_noise_std_m_s2, gravity_m_s2=GRAVITY_M_S2,
    )
    states = np.empty((len(times), 2))
    covariance = np.empty((len(times), 2, 2))
    innovations = np.empty((len(trial["indices"]), 2))
    innovation_covariance = np.empty((len(trial["indices"]), 2, 2))
    states[0], covariance[0] = estimator.state, estimator.covariance
    j = 0
    for k, dt in enumerate(np.diff(times), start=1):
        estimator.predict(float(trial["gyro"][k - 1]), float(dt))
        if j < len(trial["indices"]) and k == trial["indices"][j]:
            innovations[j], innovation_covariance[j] = estimator.update(trial["force"][j])
            j += 1
        states[k], covariance[k] = estimator.state, estimator.covariance
    normalized = np.array([float(v @ np.linalg.solve(s, v))
                           for v, s in zip(innovations, innovation_covariance)])
    angle_error = np.rad2deg(states[:, 0] - trial["truth"].angle_rad)
    metrics = baseline._error_metrics(angle_error, times, config)
    metrics["observation_epoch_rmse_deg"] = float(np.sqrt(np.mean(angle_error[trial["indices"]]**2)))
    metrics["final_bias_estimate_deg_s"] = float(np.rad2deg(states[-1, 1]))
    metrics["final_bias_error_deg_s"] = metrics["final_bias_estimate_deg_s"] - config.bias_deg_s
    pulse = (times >= config.pulse_start_s) & (times < config.pulse_end_s)
    metrics["max_bias_estimate_during_pulse_deg_s"] = float(np.max(np.rad2deg(states[pulse, 1]))) if np.any(pulse) else None
    metrics["mean_normalized_innovation_squared"] = float(np.mean(normalized))
    metrics["max_abs_covariance_difference_to_angle_kalman"] = float(np.max(np.abs(covariance - trial["covariance"])))
    trial["ekf"] = {"states": states, "covariance": covariance, "innovations": innovations,
                    "innovation_covariance": innovation_covariance,
                    "normalized_innovation_squared": normalized, "metrics": metrics}
    return trial


def _compact(trial: dict) -> dict:
    metrics, ekf = trial["metrics"], trial["ekf"]["metrics"]
    return {
        "gyro_angle_rmse_deg": metrics["gyro"]["rmse_deg"],
        "complementary_angle_rmse_deg": metrics["complementary"]["rmse_deg"],
        "angle_kalman_angle_rmse_deg": metrics["kalman"]["rmse_deg"],
        "ekf_angle_rmse_deg": ekf["rmse_deg"],
        "angle_kalman_final_bias_abs_error_deg_s": abs(metrics["final_bias_error_deg_s"]),
        "ekf_final_bias_abs_error_deg_s": abs(ekf["final_bias_error_deg_s"]),
    }


def _passes(metrics: dict) -> bool:
    return all(metrics[name] < limit for name, limit in ACCEPTANCE_LIMITS.items())


def _export_trial(directory: Path, trial: dict, config: FusionConfig) -> None:
    # Preserve baseline exports; vector innovations have different units/dimension.
    baseline._export_trial(directory, trial, config)
    ekf = trial["ekf"]
    times = trial["truth"].time_s
    cov, states, innovation_cov = ekf["covariance"], ekf["states"], ekf["innovation_covariance"]
    np.savetxt(directory / "ekf_estimates.csv", np.column_stack([
        times, states[:, 0], states[:, 1], cov[:, 0, 0], cov[:, 0, 1], cov[:, 1, 1],
    ]), delimiter=",", comments="", fmt="%.17g",
        header="time_s,ekf_roll_rad,ekf_bias_rad_s,p_angle_rad2,p_angle_bias_rad2_s,p_bias_rad2_s2")
    np.savetxt(directory / "ekf_innovations.csv", np.column_stack([
        times[trial["indices"]], *ekf["innovations"].T, innovation_cov[:, 0, 0],
        innovation_cov[:, 0, 1], innovation_cov[:, 1, 1], ekf["normalized_innovation_squared"],
    ]), delimiter=",", comments="", fmt="%.17g",
        header="time_s,innovation_y_m_s2,innovation_z_m_s2,s_yy_m2_s4,s_yz_m2_s4,s_zz_m2_s4,normalized_innovation_squared")


def _plot(output: Path, scenarios: dict, config: FusionConfig) -> None:
    figure = Figure(figsize=(13, 11), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(4, 2, sharex=True)
    colors = {"gyro": "#d8821c", "complementary": "#6d5b9b", "kalman": "#29916b", "ekf": "#326cb2"}
    labels = {"gyro": "Gyro", "complementary": "Complementary", "kalman": "Angle KF", "ekf": "Vector EKF"}
    for column, (name, trial) in enumerate(scenarios.items()):
        times, truth, ekf = trial["truth"].time_s, trial["truth"].angle_rad, trial["ekf"]
        observation_time = times[trial["indices"]]
        angles = {**trial["estimates"], "ekf": ekf["states"][:, 0]}
        axes[0, column].set_title("Nominal gravity model" if name == "nominal" else "Lateral acceleration pulse")
        axes[0, column].plot(times, np.rad2deg(truth), "--", color="0.2", label="Truth")
        for method, angle in angles.items():
            style = "--" if method == "ekf" else "-"
            axes[0, column].plot(times, np.rad2deg(angle), style, color=colors[method], linewidth=1, label=labels[method])
            if method != "gyro":
                axes[1, column].plot(times, np.rad2deg(angle - truth), style,
                                     color=colors[method], linewidth=1, label=labels[method])
        axes[0, column].legend(ncols=3, fontsize=8, loc="upper right")
        axes[1, column].legend(fontsize=8, loc="lower left")
        for method, state in (("kalman", trial["states"]), ("ekf", ekf["states"])):
            axes[2, column].plot(times, np.rad2deg(state[:, 1]), "--" if method == "ekf" else "-",
                                 color=colors[method], label=labels[method])
        bias = np.rad2deg(ekf["states"][:, 1])
        std = np.rad2deg(np.sqrt(ekf["covariance"][:, 1, 1]))
        axes[2, column].fill_between(times, bias - 2 * std, bias + 2 * std,
                                     color=colors["ekf"], alpha=0.13, label="EKF ±2 model std")
        axes[2, column].axhline(config.bias_deg_s, color="0.2", linestyle=":", label="True bias")
        axes[2, column].legend(ncols=2, fontsize=8, loc="upper right")
        scalar_nis = trial["innovations"][:, 0]**2 / trial["innovations"][:, 1]
        axes[3, column].plot(observation_time, scalar_nis, color=colors["kalman"], linewidth=0.7, label="Angle KF: NIS / 1")
        axes[3, column].plot(observation_time, ekf["normalized_innovation_squared"] / 2,
                             color=colors["ekf"], linewidth=0.7, label="Vector EKF: NIS / 2")
        axes[3, column].axhline(1, color="0.2", linestyle=":", label="Local model reference")
        axes[3, column].legend(fontsize=8, loc="upper right")
        for row, label in enumerate(("Roll (deg)", "Fusion error (deg)", "Bias (deg/s)", "NIS / dimension")):
            axis = axes[row, column]
            axis.set_ylabel(label)
            axis.grid(alpha=0.2)
            axis.set_xlim(times[0], times[-1])
            if name != "nominal":
                axis.axvspan(config.pulse_start_s, config.pulse_end_s, color="#d8821c", alpha=0.1)
        axes[-1, column].set_xlabel("Time (s)")
    figure.suptitle("Accelerometer-vector EKF | identical simulated inputs and initialization\n"
                    "Independent vertical scales; NIS is a model diagnostic, not an estimator ranking", fontsize=13)
    figure.savefig(output, dpi=150, metadata={"Software": "Meridian"})


def run_experiment(output_dir: Path, *, config: FusionConfig = FusionConfig(), seed: int = 42,
                   validation_seeds: int = 20) -> dict:
    """Export nominal/disturbed comparisons and repeated seeded evaluations."""
    if isinstance(validation_seeds, bool) or not isinstance(validation_seeds, int) or validation_seeds < 1:
        raise ValueError("validation_seeds must be a positive integer")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    scenarios = {name: run_trial(config, seed, disturbed=disturbed)
                 for name, disturbed in (("nominal", False), ("translation_pulse", True))}
    repeats = [{"seed": value, "nominal": _compact(run_trial(config, value)),
                "translation_pulse": _compact(run_trial(config, value, disturbed=True))}
               for value in range(validation_seeds)]
    aggregates = {name: {
        metric: {"mean": float(np.mean([run[name][metric] for run in repeats])),
                 "worst": max(run[name][metric] for run in repeats)}
        for metric in repeats[0][name]} for name in scenarios}
    applied = config == FusionConfig()
    summary = {
        "schema_version": 1, "experiment": "vector_ekf_comparison", "data_source": "simulation",
        "config": asdict(config), "seed": seed, "stream_seeds": scenarios["nominal"]["stream_seeds"],
        "random_generator": "SeedSequence children -> uint64 seeds -> Generator(PCG64)",
        "gravity_m_s2": GRAVITY_M_S2,
        "ekf_accel_covariance_m2_s4": (np.eye(2) * config.accel_noise_std_m_s2**2).tolist(),
        "angle_kalman_variance_rad2": (config.accel_noise_std_m_s2 / GRAVITY_M_S2)**2,
        "initialization": {"angle_rad": 0.0, "angle_std_rad": 0.0, "bias_rad_s": 0.0,
                           "bias_std_rad_s": float(np.deg2rad(1.0)), "policy": "known angle shared by all methods"},
        "interval_count": len(scenarios["nominal"]["gyro"]),
        "observation_count": len(scenarios["nominal"]["indices"]),
        "timing": "interval-mean gyro; predict to endpoint, then update from the current force vector",
        "paired_noise": "identical gyro and accelerometer noise for nominal/pulse, shared by every estimator",
        "innovation_dimensions": {"angle_kalman": 1, "vector_ekf": 2},
        "scenarios": {name: {"baselines": trial["metrics"], "vector_ekf": trial["ekf"]["metrics"]}
                      for name, trial in scenarios.items()},
        "validation": {"seeds": list(range(validation_seeds)), "trials": repeats, "aggregates": aggregates},
        "acceptance": {"scope": "nominal default configuration only; no superiority requirement",
                       "limits": ACCEPTANCE_LIMITS, "applied": applied,
                       "selected_passed": _passes(_compact(scenarios["nominal"])) if applied else None,
                       "failed_validation_seeds": [run["seed"] for run in repeats if not _passes(run["nominal"])] if applied else None,
                       "translation_pulse": "characterize the model violation; no nominal accuracy threshold"},
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "matplotlib": matplotlib.__version__},
        "limitations": [
            "Pure roll, zero pitch and gravity-only observation; imposed translation violates the model.",
            "Local EKF correction with constant bias, isotropic component noise and no physical rejection gate.",
            "Known initial angle and simulated interval-mean rates do not establish global convergence or hardware timing.",
            "Identical covariance to the nominal angle KF is expected here; small covariance does not certify low error.",
            "No real-log vector EKF replay, new bench acquisition, C++ or flight validation is demonstrated.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, trial in scenarios.items():
        _export_trial(output_dir / name, trial, config)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _plot(output_dir / "overview.png", scenarios, config)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/ekf-comparison"))
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
        print(f"{name}: angle KF RMSE={metrics['baselines']['kalman']['rmse_deg']:.6f}, "
              f"vector EKF RMSE={metrics['vector_ekf']['rmse_deg']:.6f} deg; "
              f"EKF final bias={metrics['vector_ekf']['final_bias_estimate_deg_s']:.6f} deg/s")
    print(f"Results written to {output}")
    acceptance = summary["acceptance"]
    if acceptance["applied"] and (not acceptance["selected_passed"] or acceptance["failed_validation_seeds"]):
        print("Nominal EKF acceptance checks failed; inspect summary.json.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
