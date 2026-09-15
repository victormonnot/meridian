"""Compare a fixed accelerometer R grid on paired exploratory and evaluation seeds."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform

import matplotlib
import numpy as np

from meridian.ekf import AngleBiasEKF
from meridian.integration import integrate_gyro
from meridian.r_sensitivity_plot import plot_summary
from meridian.stress_evaluation import summarize_errors
from meridian.stress_scenarios import SCENARIOS, Scenario, ScenarioData, generate_scenario
from meridian.tilt import ComplementaryRoll, accel_roll


# Frozen before execution; see docs/r-sensitivity.md. Never select a winner here.
STUDY_SCENARIOS = tuple(s for s in SCENARIOS if s.name in ("nominal", "accel_noise_mismatch"))
ASSUMED_STDS = (0.1, 0.2, 0.6)
DEFAULT_SETTING = "0.2"
PHASE_SEEDS = {"exploration": tuple(range(20)), "evaluation": tuple(range(1000, 1020))}
GRAVITY_M_S2 = 9.80665
GYRO_STD_RAD_S = float(np.deg2rad(1.0))
COVARIANCE_TOLERANCE = 1e-12


def protocol() -> dict:
    """Return the fixed protocol in serializable units, independent of outcomes."""
    return {
        "version": 1, "declared_date": "2026-09-15",
        "scenarios": [asdict(s) for s in STUDY_SCENARIOS],
        "phase_seeds": {name: list(seeds) for name, seeds in PHASE_SEEDS.items()},
        "assumed_accel_std_m_s2": list(ASSUMED_STDS),
        "r_diagonal_m2_s4": {str(s): s * s for s in ASSUMED_STDS},
        "default_setting": DEFAULT_SETTING, "selection_rule": "none; report every setting",
        "duration_s": 30.0, "gyro_intervals": 3000, "accel_observations": 300,
        "roll_amplitude_deg": 20.0, "roll_frequency_hz": 0.1,
        "true_bias_deg_s": 0.5, "gravity_m_s2": GRAVITY_M_S2,
        "initial_state_rad_rad_s": [0.0, 0.0],
        "initial_covariance_si": [[0.0, 0.0], [0.0, GYRO_STD_RAD_S**2]],
        "assumed_gyro_std_rad_s": GYRO_STD_RAD_S,
        "q_model": "diag(sigma_gyro^2 * dt^2, 0); independent interval-mean noise",
        "frame": "forward-right-down; positive right-hand roll about x",
        "observation": "unnormalized [f_y, f_z] = [-g*sin(roll), -g*cos(roll)] + noise",
        "timing": "predict using interval mean, then correct at endpoint; no t=0 observation",
        "complementary_tau_s": 1.0, "late_window_s_closed": [25.0, 30.0],
        "rmse_weighting": "all endpoints including initialization; no window restart",
        "nis_dimension": 2, "nis_epochs": "all corrections, using the prior innovation and full S",
        "pairing": "same arrays within case; same gyro and standardized force draws across cases",
        "delta": "per-seed setting metric minus default-setting metric",
        "aggregation": "mean and observed min/max across seeds, separately by phase/case",
        "numerical_covariance_tolerance_si": COVARIANCE_TOLERANCE,
        "accuracy_thresholds": None,
    }


def _check_numerics(result: dict) -> None:
    """Fail on invalid numerical output without imposing an accuracy ranking."""
    for name in ("states", "covariance", "innovations", "innovation_covariance", "nis"):
        if not np.all(np.isfinite(result[name])):
            raise ValueError(f"nonfinite {name}")
    p = result["covariance"]
    s = result["innovation_covariance"]
    if np.max(np.abs(p - p.swapaxes(1, 2))) > COVARIANCE_TOLERANCE:
        raise ValueError("state covariance is not symmetric")
    if np.min(np.linalg.eigvalsh(p)) < -COVARIANCE_TOLERANCE:
        raise ValueError("state covariance is not positive semidefinite")
    if np.max(np.abs(s - s.swapaxes(1, 2))) > COVARIANCE_TOLERANCE:
        raise ValueError("innovation covariance is not symmetric")
    if np.min(np.linalg.eigvalsh(s)) <= 0.0 or np.min(result["nis"]) < 0.0:
        raise ValueError("innovation covariance or NIS is invalid")


def evaluate_trial(scenario: Scenario, seed: int) -> dict:
    """Generate once and apply every R to identical inputs with fixed P0 and Q.

    Truth is used only for scoring. State/innovation arrays retain every endpoint
    or correction; the generated data are never changed by the filters.
    """
    if scenario not in STUDY_SCENARIOS:
        raise ValueError("scenario is outside this fixed study")
    data = generate_scenario(scenario, seed)
    times, indices = data.truth.time_s, data.observation_indices
    estimators = {
        str(std): AngleBiasEKF(
            initial_angle_rad=0.0, initial_bias_rad_s=0.0,
            initial_angle_std_rad=0.0, initial_bias_std_rad_s=GYRO_STD_RAD_S,
            gyro_noise_std_rad_s=GYRO_STD_RAD_S,
            accel_noise_std_m_s2=std, gravity_m_s2=GRAVITY_M_S2,
        ) for std in ASSUMED_STDS
    }
    settings = {
        name: {"states": np.empty((len(times), 2)),
               "covariance": np.empty((len(times), 2, 2)),
               "innovations": np.empty((len(indices), 2)),
               "innovation_covariance": np.empty((len(indices), 2, 2))}
        for name in estimators
    }
    for name, estimator in estimators.items():
        settings[name]["states"][0] = estimator.state
        settings[name]["covariance"][0] = estimator.covariance
    complementary = ComplementaryRoll(initial_angle_rad=0.0, tau_s=1.0)
    complementary_angles = np.empty(len(times))
    complementary_angles[0] = 0.0
    tilt = accel_roll(data.force_yz_m_s2)
    observation, previous_time = 0, times[0]
    for endpoint, dt in enumerate(np.diff(times), start=1):
        rate = float(data.gyro_rate_rad_s[endpoint - 1])
        for estimator in estimators.values():
            estimator.predict(rate, float(dt))
        complementary.predict(rate, float(dt))
        if observation < len(indices) and endpoint == indices[observation]:
            for name, estimator in estimators.items():
                innovation, s = estimator.update(data.force_yz_m_s2[observation])
                settings[name]["innovations"][observation] = innovation
                settings[name]["innovation_covariance"][observation] = s
            complementary.update(float(tilt[observation]), float(times[endpoint] - previous_time))
            previous_time = times[endpoint]
            observation += 1
        for name, estimator in estimators.items():
            settings[name]["states"][endpoint] = estimator.state
            settings[name]["covariance"][endpoint] = estimator.covariance
        complementary_angles[endpoint] = complementary.angle_rad
    if observation != len(indices):
        raise ValueError("not every scheduled observation was consumed")

    for result in settings.values():
        v, s = result["innovations"], result["innovation_covariance"]
        result["nis"] = np.array([innovation @ np.linalg.solve(covariance, innovation)
                                  for innovation, covariance in zip(v, s)])
        _check_numerics(result)
        angle_error = np.rad2deg(result["states"][:, 0] - data.truth.angle_rad)
        bias_error = np.rad2deg(result["states"][:, 1] - data.bias_rad_s)
        result["metrics"] = {
            **summarize_errors(angle_error, times),
            "bias_rmse_deg_s": float(np.sqrt(np.mean(bias_error**2))),
            "late_bias_rmse_deg_s": float(np.sqrt(np.mean(bias_error[times >= 25.0]**2))),
            "max_abs_bias_error_deg_s": float(np.max(np.abs(bias_error))),
            "final_bias_error_deg_s": float(bias_error[-1]),
            "mean_nis": float(np.mean(result["nis"])),
        }
    angles = {"gyro": integrate_gyro(times, data.gyro_rate_rad_s, initial_angle_rad=0.0),
              "complementary": complementary_angles}
    return {
        "data": data, "settings": settings,
        "baselines": {name: {"angles": angle,
                             "metrics": summarize_errors(np.rad2deg(angle - data.truth.angle_rad), times)}
                      for name, angle in angles.items()},
    }


def _measurement_fingerprint(data: ScenarioData) -> str:
    """Hash named, shaped little-endian input arrays, excluding truth."""
    digest = hashlib.sha256()
    inputs = {"time_s": data.truth.time_s, "gyro_rate_rad_s": data.gyro_rate_rad_s,
              "observation_indices": data.observation_indices, "force_yz_m_s2": data.force_yz_m_s2}
    for name, values in inputs.items():
        array = np.asarray(values, dtype="<i8" if name == "observation_indices" else "<f8")
        digest.update(f"{name}:{array.dtype.str}:{array.shape}\n".encode("ascii"))
        digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def _compact(trial: dict, seed: int) -> dict:
    default = trial["settings"][DEFAULT_SETTING]["metrics"]
    return {
        "seed": seed, "stream_seeds": trial["data"].stream_seeds,
        "measurement_sha256": _measurement_fingerprint(trial["data"]),
        "baselines": {name: result["metrics"] for name, result in trial["baselines"].items()},
        "settings": {
            name: {"metrics": result["metrics"],
                   "delta_from_default": {metric: value - default[metric]
                                          for metric, value in result["metrics"].items()},
                   "numerical_checks_passed": True}
            for name, result in trial["settings"].items()
        },
    }


def _aggregate(metrics: list[dict]) -> dict:
    return {name: {"mean": float(np.mean([m[name] for m in metrics])),
                   "min": float(min(m[name] for m in metrics)),
                   "max": float(max(m[name] for m in metrics))}
            for name in metrics[0]}


def _aggregates(trials: list[dict]) -> dict:
    settings = {}
    for std in ASSUMED_STDS:
        name = str(std)
        deltas = [t["settings"][name]["delta_from_default"] for t in trials]
        counts = {
            metric: {"negative": sum(d[metric] < 0 for d in deltas),
                     "zero": sum(d[metric] == 0 for d in deltas),
                     "positive": sum(d[metric] > 0 for d in deltas)}
            for metric in ("angle_rmse_deg", "angle_time_weighted_rmse_deg", "late_angle_rmse_deg",
                           "bias_rmse_deg_s", "late_bias_rmse_deg_s")
        }
        settings[name] = {
            "metrics": _aggregate([t["settings"][name]["metrics"] for t in trials]),
            "delta_from_default": _aggregate(deltas), "rmse_delta_counts": counts,
        }
    return {"settings": settings,
            "baselines": {name: _aggregate([t["baselines"][name] for t in trials])
                          for name in ("gyro", "complementary")}}


def _write_csv(path: Path, names: list[str], columns: list[np.ndarray]) -> None:
    np.savetxt(path, np.column_stack(columns), delimiter=",", comments="", fmt="%.17g",
               header=",".join(names))


def _export_trial(directory: Path, trial: dict) -> None:
    directory.mkdir()
    data = trial["data"]
    times = data.truth.time_s
    _write_csv(directory / "gyro_measurements.csv", ["t_start_s", "t_end_s", "rate_rad_s"],
               [times[:-1], times[1:], data.gyro_rate_rad_s])
    _write_csv(directory / "accel_measurements.csv", ["time_s", "force_y_m_s2", "force_z_m_s2"],
               [times[data.observation_indices], *data.force_yz_m_s2.T])
    _write_csv(directory / "truth.csv", ["time_s", "roll_rad", "bias_rad_s"],
               [times, data.truth.angle_rad, data.bias_rad_s])
    _write_csv(directory / "baseline_estimates.csv", ["time_s", "gyro_roll_rad", "complementary_roll_rad"],
               [times, trial["baselines"]["gyro"]["angles"], trial["baselines"]["complementary"]["angles"]])
    for name, result in trial["settings"].items():
        p, s = result["covariance"], result["innovation_covariance"]
        _write_csv(directory / f"ekf-{name}-estimates.csv",
                   ["time_s", "roll_rad", "bias_rad_s", "p_angle_rad2", "p_angle_bias_rad2_s", "p_bias_rad2_s2"],
                   [times, *result["states"].T, p[:, 0, 0], p[:, 0, 1], p[:, 1, 1]])
        _write_csv(directory / f"ekf-{name}-innovations.csv",
                   ["time_s", "innovation_y_m_s2", "innovation_z_m_s2", "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4", "nis"],
                   [times[data.observation_indices], *result["innovations"].T,
                    s[:, 0, 0], s[:, 0, 1], s[:, 1, 1], result["nis"]])


def run_experiment(output_dir: Path, *, phase: str) -> dict:
    """Run one fixed phase; refuse overwriting prior evidence."""
    if phase not in PHASE_SEEDS:
        raise ValueError("phase must be exploration or evaluation")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    seeds = PHASE_SEEDS[phase]
    scenarios, representatives = {}, {}
    for scenario in STUDY_SCENARIOS:
        trials = []
        for seed in seeds:
            trial = evaluate_trial(scenario, seed)
            trials.append(_compact(trial, seed))
            if seed == seeds[0]:
                representatives[scenario.name] = trial
        scenarios[scenario.name] = {
            "simulated_accel_std_m_s2": scenario.accel_noise_std_m_s2,
            "trials": trials, "aggregates": _aggregates(trials),
        }
    spec = protocol()
    sources = ("r_sensitivity.py", "r_sensitivity_plot.py", "stress_scenarios.py",
               "stress_evaluation.py", "simulation.py", "ekf.py", "integration.py", "tilt.py")
    summary = {
        "schema_version": 1, "experiment": "accelerometer_covariance_sensitivity",
        "data_source": "simulation", "phase": phase, "seeds": list(seeds),
        "representative_seed": seeds[0], "protocol": spec,
        "protocol_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                          for name in sources},
        "measurement_fingerprint_format": "SHA256 of named, shaped C-order little-endian arrays; see _measurement_fingerprint",
        "environment": {"python": platform.python_version(), "numpy": np.__version__,
                        "matplotlib": matplotlib.__version__},
        "scenarios": scenarios,
        "limitations": [
            "Fixed pure-roll simulation family; no hardware noise identification or new motion distribution.",
            "Shared seed draws across settings and cases; do not pool them as independent replications.",
            "Observed seed ranges are not confidence intervals; NIS does not establish consistency.",
            "No default retuning, adaptive R, changing-bias model, or new C++ implementation.",
            "Evaluation seeds are no longer untouched after this phase is inspected.",
            "No new bench acquisition, embedded timing, or flight validation.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, trial in representatives.items():
        _export_trial(output_dir / name, trial)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    plot_summary(output_dir / "overview.png", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=tuple(PHASE_SEEDS))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.output, phase=args.phase)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"{summary['phase']}: {len(summary['seeds'])} seeds per case; all R settings retained.")
    print(f"Results: {args.output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
