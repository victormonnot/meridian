"""Evaluate fixed initialization, bias, noise, availability, and timing scenarios."""

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

from meridian.stress_evaluation import evaluate_scenario
from meridian.stress_scenarios import SCENARIOS


# Declared in docs/controlled-scenarios.md before evaluating this suite.
NOMINAL_LIMITS = {"angle_rmse_deg": 0.75, "final_bias_abs_error_deg_s": 0.15}
METHODS = ("gyro", "complementary", "kalman", "ekf")
LABELS = {"gyro": "Gyro", "complementary": "Complementary", "kalman": "Angle KF", "ekf": "Vector EKF"}
COLORS = {"gyro": "#d8821c", "complementary": "#6d5b9b", "kalman": "#29916b", "ekf": "#326cb2"}


def _write_csv(path: Path, names: list[str], columns: list[np.ndarray]) -> None:
    np.savetxt(path, np.column_stack(columns), delimiter=",", comments="", fmt="%.17g",
               header=",".join(names))


def _export_trial(path: Path, trial: dict) -> None:
    path.mkdir()
    data = trial["data"]
    times, used = data.truth.time_s, data.available
    arrivals = times[data.observation_indices]
    _write_csv(path / "gyro_measurements.csv", ["t_start_s", "t_end_s", "rate_rad_s"],
               [times[:-1], times[1:], data.gyro_rate_rad_s])
    _write_csv(path / "accel_measurements.csv", ["arrival_time_s", "force_y_m_s2", "force_z_m_s2"],
               [arrivals[used], *data.force_yz_m_s2[used].T])
    # Actual source time is hidden from estimators; omitted samples have no input row.
    _write_csv(path / "observation_schedule_truth.csv", ["arrival_time_s", "sample_time_s", "available"],
               [arrivals, data.sample_time_s, used.astype(int)])
    _write_csv(path / "truth.csv", ["time_s", "roll_rad", "bias_rad_s"],
               [times, data.truth.angle_rad, data.bias_rad_s])
    _write_csv(path / "gyro_interval_truth.csv", ["t_start_s", "t_end_s", "mean_rate_rad_s", "mean_bias_rad_s"],
               [times[:-1], times[1:], data.truth.interval_rate_rad_s, data.interval_bias_rad_s])
    names = ["time_s"] + [f"{method}_roll_rad" for method in METHODS]
    columns = [times] + [trial["estimates"][method] for method in METHODS]
    for method in ("kalman", "ekf"):
        p = trial["covariance"][method]
        names += [f"{method}_bias_rad_s", f"{method}_p_angle_rad2", f"{method}_p_angle_bias_rad2_s", f"{method}_p_bias_rad2_s2"]
        columns += [trial["states"][method][:, 1], p[:, 0, 0], p[:, 0, 1], p[:, 1, 1]]
    _write_csv(path / "estimates.csv", names, columns)
    for method in ("kalman", "ekf"):
        v, s = trial["innovations"][method], trial["innovation_covariance"][method]
        if method == "kalman":
            names = ["arrival_time_s", "innovation_rad", "s_rad2", "nis"]
            columns = [times[trial["observation_indices"]], v[:, 0], s[:, 0, 0], trial["nis"][method]]
        else:
            names = ["arrival_time_s", "innovation_y_m_s2", "innovation_z_m_s2", "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4", "nis"]
            columns = [times[trial["observation_indices"]], *v.T, s[:, 0, 0], s[:, 0, 1], s[:, 1, 1], trial["nis"][method]]
        _write_csv(path / f"{method}_innovations.csv", names, columns)


def _plot(path: Path, trials: dict, *, bias: bool = False) -> None:
    figure = Figure(figsize=(16, 7.5), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(2, 4, sharex=True)
    titles = ("Nominal", "Initial +60°, std 30°", "Initial +60°, std 0°", "Bias ramp",
              "Accel dropout", "Irregular intervals", "Accel noise ×3", "Unmodeled accel delay")
    for axis, (name, trial), title in zip(axes.flat, trials.items(), titles):
        times = trial["data"].truth.time_s
        axis.set_title(title, fontsize=11)
        methods = ("kalman", "ekf") if bias else ("complementary", "kalman", "ekf")
        for method in methods:
            error = (trial["states"][method][:, 1] - trial["data"].bias_rad_s if bias
                     else trial["estimates"][method] - trial["data"].truth.angle_rad)
            axis.plot(times, np.rad2deg(error), "--" if method == "ekf" else "-",
                      color=COLORS[method], linewidth=1, label=LABELS[method])
        if bias:
            std = np.rad2deg(np.sqrt(np.maximum(trial["covariance"]["ekf"][:, 1, 1], 0)))
            axis.fill_between(times, -2 * std, 2 * std, color=COLORS["ekf"], alpha=0.12,
                              label="±2 model std about zero")
        if name == "bias_ramp":
            axis.axvspan(10, 20, color="#d8821c", alpha=0.12)
        elif name == "accel_dropout":
            axis.axvspan(12, 17, color="#d8821c", alpha=0.12)
        axis.axhline(0, color="0.3", linewidth=0.6)
        axis.set_xlim(0, 30)
        axis.grid(alpha=0.2)
        axis.set_ylabel("Bias error (deg/s)" if bias else "Roll error (deg)")
        axis.set_xlabel("Reported time (s)")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="outside lower center", ncols=len(labels), fontsize=10)
    description = "Bias errors relative to instantaneous truth" if bias else "Roll errors relative to truth | gyro baseline retained in CSV/metrics"
    figure.suptitle(f"Controlled scenarios | {description}\nIndependent vertical scales; unchanged estimator tuning", fontsize=13)
    figure.savefig(path, dpi=150, metadata={"Software": "Meridian"})


def _nominal_failures(metrics: dict) -> list[str]:
    return [method for method in ("kalman", "ekf") if not (
        metrics[method]["angle_rmse_deg"] < NOMINAL_LIMITS["angle_rmse_deg"]
        and abs(metrics[method]["final_bias_error_deg_s"]) < NOMINAL_LIMITS["final_bias_abs_error_deg_s"]
    )]


def _numerical_contracts(trial: dict) -> dict:
    """Descriptive numerical health, not physical accuracy acceptance."""
    covariances = list(trial["covariance"].values())
    arrays = list(trial["estimates"].values()) + list(trial["states"].values()) + covariances
    arrays += list(trial["innovations"].values()) + list(trial["innovation_covariance"].values()) + list(trial["nis"].values())
    minimum_eigenvalue = min(float(np.min(np.linalg.eigvalsh(p))) for p in covariances)
    asymmetry = max(float(np.max(np.abs(p - p.transpose(0, 2, 1)))) for p in covariances)
    parity = float(np.max(np.abs(covariances[0] - covariances[1])))
    finite = all(bool(np.all(np.isfinite(array))) for array in arrays)
    # State units are rad and rad/s; tolerances bound roundoff in stored SI entries.
    passed = finite and minimum_eigenvalue >= -1e-12 and asymmetry < 1e-12 and parity < 1e-12
    return {"finite": finite, "minimum_covariance_eigenvalue": minimum_eigenvalue,
            "maximum_covariance_asymmetry": asymmetry, "maximum_kf_ekf_covariance_difference": parity,
            "roundoff_tolerance": 1e-12, "passed": passed}


def run_experiment(output_dir: Path, *, seed: int = 42, validation_seeds: int = 20) -> dict:
    if isinstance(validation_seeds, bool) or not isinstance(validation_seeds, int) or validation_seeds < 1:
        raise ValueError("validation_seeds must be a positive integer")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    selected = {scenario.name: evaluate_scenario(scenario, seed) for scenario in SCENARIOS}
    repeats = []
    failed_contracts = []
    for repeat_seed in range(validation_seeds):
        cases = {}
        for scenario in SCENARIOS:
            trial = evaluate_scenario(scenario, repeat_seed)
            health = _numerical_contracts(trial)
            if not health["passed"]:
                failed_contracts.append({"seed": repeat_seed, "scenario": scenario.name})
            cases[scenario.name] = {"metrics": trial["metrics"], "numerical_contracts": health}
        repeats.append({"seed": repeat_seed, "scenarios": cases})
    # Signed final errors are retained individually; aggregate only nonnegative errors.
    aggregate_metrics = ("angle_rmse_deg", "angle_time_weighted_rmse_deg", "late_angle_rmse_deg")
    aggregates = {scenario.name: {
        method: {metric: {
            "mean": float(np.mean([run["scenarios"][scenario.name]["metrics"][method][metric] for run in repeats])),
            "worst": max(run["scenarios"][scenario.name]["metrics"][method][metric] for run in repeats),
        } for metric in aggregate_metrics} for method in METHODS} for scenario in SCENARIOS}
    summary = {
        "schema_version": 1, "experiment": "controlled_scenarios", "data_source": "simulation",
        "seed": seed, "scenario_definitions": [asdict(scenario) for scenario in SCENARIOS],
        "shared_settings": {"duration_s": 30, "gyro_interval_count": 3000, "scheduled_accel_count": 300,
                            "amplitude_deg": 20, "frequency_hz": 0.1, "gravity_m_s2": 9.80665,
                            "gyro_noise_std_deg_s_per_interval": 1, "assumed_accel_noise_std_m_s2": 0.2,
                            "initial_bias_deg_s": 0, "initial_bias_std_deg_s": 1, "bias_random_walk": False,
                            "complementary_tau_s": 1, "late_window_start_s": 25},
        "random_generator": "SeedSequence children gyro/accel/jitter -> uint64 seeds -> Generator(PCG64)",
        "innovation_dimensions": {"kalman": 1, "ekf": 2},
        "scenarios": {}, "validation": {"seeds": list(range(validation_seeds)), "trials": repeats, "aggregates": aggregates},
        "acceptance": {"scope": "nominal accuracy only; numerical contracts on every case",
                       "nominal_limits": NOMINAL_LIMITS,
                       "selected_failed_methods": _nominal_failures(selected["nominal"]["metrics"]),
                       "failed_validation": [{"seed": run["seed"], "methods": failures} for run in repeats
                                             if (failures := _nominal_failures(run["scenarios"]["nominal"]["metrics"]))],
                       "failed_numerical_validation": failed_contracts,
                       "stress_accuracy_thresholds": None},
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "matplotlib": matplotlib.__version__},
        "limitations": ["Fixed cases and seeds do not establish general robustness or estimator ranking.",
                        "Bias is fixed in every filter, including when the true bias ramps.",
                        "Delay source times are known only to simulation; estimators apply observations at arrival.",
                        "Gyro samples are exact interval means, not logged snapshots or a continuous noise density.",
                        "Covariance and NIS are model diagnostics, not calibrated accuracy guarantees.",
                        "No new physical acquisition, C++ parity, embedded, or flight validation is demonstrated."],
    }
    for name, trial in selected.items():
        data = trial["data"]
        times = data.truth.time_s
        summary["scenarios"][name] = {
            "metrics": trial["metrics"], "numerical_contracts": _numerical_contracts(trial),
            "stream_seeds": data.stream_seeds, "used_accel_count": int(np.count_nonzero(data.available)),
            "gyro_dt_min_s": float(np.min(np.diff(times))), "gyro_dt_max_s": float(np.max(np.diff(times))),
            "maximum_correction_gap_s": float(np.max(np.diff(np.r_[times[0], times[trial["observation_indices"]]]))),
        }
    summary["acceptance"]["selected_failed_numerical_scenarios"] = [
        name for name, scenario in summary["scenarios"].items() if not scenario["numerical_contracts"]["passed"]]
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, trial in selected.items():
        _export_trial(output_dir / name, trial)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _plot(output_dir / "overview.png", selected)
    _plot(output_dir / "bias.png", selected, bias=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/controlled-scenarios"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-seeds", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.output, seed=args.seed, validation_seeds=args.validation_seeds)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for name, scenario in summary["scenarios"].items():
        metrics = scenario["metrics"]["ekf"]
        print(f"{name}: EKF roll RMSE={metrics['angle_rmse_deg']:.6f} deg, "
              f"last 5 s={metrics['late_angle_rmse_deg']:.6f} deg, "
              f"final bias error={metrics['final_bias_error_deg_s']:+.6f} deg/s")
    print(f"Results written to {args.output}; stress cases have no accuracy pass/fail threshold.")
    acceptance = summary["acceptance"]
    return int(any(acceptance[key] for key in ("selected_failed_methods", "failed_validation",
                                              "failed_numerical_validation", "selected_failed_numerical_scenarios")))


if __name__ == "__main__":
    raise SystemExit(main())
