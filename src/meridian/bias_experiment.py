"""Compare fixed bias diffusion intensities and verify every Python/C++ operation."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from meridian.cpp_parity import (
    TOLERANCES, _write_trace, compare_traces, python_trace, read_cpp_trace,
    replay_command, serialize_events,
)
from meridian.stress_evaluation import summarize_errors
from meridian.stress_scenarios import SCENARIOS, Scenario, generate_scenario


STUDY_SCENARIOS = tuple(s for s in SCENARIOS if s.name in ("nominal", "bias_ramp"))
BIAS_STDS_DEG_S_PER_SQRT_S = (0.0, 0.03, 0.1)
PHASE_SEEDS = {"exploration": tuple(range(20)), "evaluation": tuple(range(2000, 2020))}
BIAS_PARAMETER = "bias_random_walk_std_rad_s_per_sqrt_s"


def _source_hashes() -> dict:
    names = ("bias_experiment.py", "ekf.py", "cpp_parity.py", "stress_scenarios.py", "simulation.py", "stress_evaluation.py")
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}


def protocol() -> dict:
    return {
        "version": 1, "declared_date": "2026-09-15",
        "scenarios": [asdict(s) for s in STUDY_SCENARIOS],
        "bias_std_deg_s_per_sqrt_s": list(BIAS_STDS_DEG_S_PER_SQRT_S),
        "phase_seeds": {k: list(v) for k, v in PHASE_SEEDS.items()},
        "reference_setting": "0", "selection": "none; retain all settings",
        "bias_model": "db = sigma_b dW; predicted mean bias unchanged",
        "q_bias": "sigma_b^2 * [[dt^3/3, -dt^2/2], [-dt^2/2, dt]]",
        "q_bias_density_units": "rad^2/s^3",
        "q_gyro": "diag(sigma_g^2 * dt^2, 0); interval-mean noise",
        "assumed_gyro_std_deg_s": 1.0, "assumed_accel_std_m_s2": 0.2,
        "initial_state_rad_rad_s": [0.0, 0.0],
        "initial_std_deg_deg_s": [0.0, 1.0], "initial_cross_covariance": 0.0,
        "duration_s": 30.0, "interval_count": 3000, "observation_count": 300,
        "roll_amplitude_deg": 20.0, "roll_frequency_hz": 0.1, "gravity_m_s2": 9.80665,
        "frame": "forward-right-down; positive right-hand roll",
        "timing": "interval-mean gyro; predict then correct at endpoint",
        "rmse": "unwrapped endpoint error including initialization",
        "closed_windows_s": {"ramp": [10.0, 20.0], "late": [25.0, 30.0]},
        "late_bias_fluctuation": "temporal error std around its own mean, ddof=0; includes settling",
        "nis_dimension": 2, "nis": "joint prior innovation, all corrections",
        "aggregation": "per-seed metrics and paired setting-minus-zero differences; mean/min/max",
        "covariance_tolerance_si": 1e-12, "parity_tolerances": TOLERANCES,
        "accuracy_thresholds": None,
    }


def _metrics(data, states: np.ndarray, nis: np.ndarray) -> dict:
    times = data.truth.time_s
    angle = np.rad2deg(states[:, 0] - data.truth.angle_rad)
    bias = np.rad2deg(states[:, 1] - data.bias_rad_s)
    late, ramp = (times >= 25.0) & (times <= 30.0), (times >= 10.0) & (times <= 20.0)
    return {
        **summarize_errors(angle, times),
        "bias_rmse_deg_s": float(np.sqrt(np.mean(bias**2))),
        "ramp_angle_rmse_deg": float(np.sqrt(np.mean(angle[ramp]**2))),
        "ramp_bias_rmse_deg_s": float(np.sqrt(np.mean(bias[ramp]**2))),
        "late_bias_rmse_deg_s": float(np.sqrt(np.mean(bias[late]**2))),
        "late_bias_mean_error_deg_s": float(np.mean(bias[late])),
        "late_bias_temporal_std_deg_s": float(np.std(bias[late], ddof=0)),
        "max_abs_bias_error_deg_s": float(np.max(np.abs(bias))),
        "final_bias_error_deg_s": float(bias[-1]), "mean_nis": float(np.mean(nis)),
    }


def evaluate_trial(scenario: Scenario, seed: int, binary: Path) -> dict:
    """Apply all settings to the same serialized data; truth only enters metrics."""
    if scenario not in STUDY_SCENARIOS:
        raise ValueError("scenario is outside this fixed study")
    data = generate_scenario(scenario, seed)
    events, endpoint_rows, observation = [], [0], 0
    for endpoint, dt in enumerate(np.diff(data.truth.time_s), start=1):
        events.append(("predict", float(data.gyro_rate_rad_s[endpoint - 1]), float(dt)))
        if observation < len(data.observation_indices) and endpoint == data.observation_indices[observation]:
            events.append(("update", *map(float, data.force_yz_m_s2[observation])))
            observation += 1
        endpoint_rows.append(len(events))
    serialized = serialize_events(events)
    settings = {}
    with tempfile.TemporaryDirectory(prefix="meridian-bias-") as temp:
        source = Path(temp) / "events.csv"
        source.write_text(serialized, encoding="utf-8")
        for std in BIAS_STDS_DEG_S_PER_SQRT_S:
            key = format(std, "g")
            config = {
                "initial_angle_rad": 0.0, "initial_bias_rad_s": 0.0,
                "initial_angle_std_rad": 0.0,
                "initial_bias_std_rad_s": float(np.deg2rad(1.0)),
                "gyro_noise_std_rad_s": float(np.deg2rad(1.0)),
                "accel_noise_std_m_s2": 0.2, "gravity_m_s2": 9.80665,
                BIAS_PARAMETER: float(np.deg2rad(std)),
            }
            operations, expected = python_trace(config, serialized)
            output = Path(temp) / f"cpp-{key}.csv"
            process = subprocess.run(replay_command(binary, config, source, output),
                                     capture_output=True, text=True, timeout=30, check=False)
            if process.returncode:
                raise ValueError(f"C++ replay failed: {process.stderr.strip()}")
            actual = read_cpp_trace(output, operations)
            comparison = compare_traces(expected, actual)
            if not comparison["passed"]:
                raise ValueError(f"Python/C++ disagreement: {scenario.name}, seed {seed}, setting {key}")
            for trace in (expected, actual):
                p = trace[:, 2:6].reshape(-1, 2, 2)
                innovation_p = trace[np.array(operations) == "update", 8:12].reshape(-1, 2, 2)
                if (np.max(np.abs(p - p.swapaxes(1, 2))) > 1e-12
                        or np.min(np.linalg.eigvalsh(p)) < -1e-12
                        or np.max(np.abs(innovation_p - innovation_p.swapaxes(1, 2))) > 1e-12
                        or np.min(np.linalg.eigvalsh(innovation_p)) <= 0):
                    raise ValueError("invalid Python or C++ trace covariance")
            # Keep one posterior per physical endpoint, not both predict/update rows.
            endpoints = expected[endpoint_rows]
            states, covariance = endpoints[:, :2], endpoints[:, 2:6].reshape(-1, 2, 2)
            corrections = expected[np.array(operations) == "update"]
            v, s = corrections[:, 6:8], corrections[:, 8:12].reshape(-1, 2, 2)
            nis = np.array([residual @ np.linalg.solve(matrix, residual) for residual, matrix in zip(v, s)])
            if not np.all(np.isfinite(nis)) or np.any(nis < 0):
                raise ValueError("invalid normalized innovation")
            settings[key] = {
                "configuration": config, "states": states, "covariance": covariance,
                "nis": nis, "metrics": _metrics(data, states, nis), "comparison": comparison,
                "python_trace": expected, "cpp_trace": actual,
            }
    return {"data": data, "settings": settings, "events": serialized,
            "operations": operations, "endpoint_rows": endpoint_rows}


def _compact(trial: dict, seed: int) -> dict:
    default = trial["settings"]["0"]["metrics"]
    return {
        "seed": seed, "stream_seeds": trial["data"].stream_seeds,
        "input_sha256": hashlib.sha256(trial["events"].encode()).hexdigest(),
        "settings": {name: {
            "configuration": value["configuration"], "metrics": value["metrics"],
            "delta_from_constant": {k: v - default[k] for k, v in value["metrics"].items()},
            "comparison": value["comparison"],
        } for name, value in trial["settings"].items()},
    }


def _aggregates(trials: list[dict]) -> dict:
    result = {}
    for name in trials[0]["settings"]:
        result[name] = {}
        for group in ("metrics", "delta_from_constant"):
            records = [trial["settings"][name][group] for trial in trials]
            result[name][group] = {
                metric: {"mean": float(np.mean([r[metric] for r in records])),
                         "min": min(r[metric] for r in records), "max": max(r[metric] for r in records)}
                for metric in records[0]
            }
        result[name]["rmse_delta_counts"] = {
            metric: {"negative": sum(t["settings"][name]["delta_from_constant"][metric] < 0 for t in trials),
                     "zero": sum(t["settings"][name]["delta_from_constant"][metric] == 0 for t in trials),
                     "positive": sum(t["settings"][name]["delta_from_constant"][metric] > 0 for t in trials)}
            for metric in records[0] if "rmse" in metric
        }
    return result


def _csv(path: Path, names: str, columns) -> None:
    np.savetxt(path, np.column_stack(columns), delimiter=",", comments="", fmt="%.17g", header=names)


def _export(directory: Path, trial: dict) -> None:
    directory.mkdir()
    data = trial["data"]
    times = data.truth.time_s
    (directory / "events.csv").write_text(trial["events"], encoding="utf-8")
    _csv(directory / "truth.csv", "time_s,roll_rad,bias_rad_s", [times, data.truth.angle_rad, data.bias_rad_s])
    _csv(directory / "gyro_measurements.csv", "t_start_s,t_end_s,rate_rad_s",
         [times[:-1], times[1:], data.gyro_rate_rad_s])
    _csv(directory / "accel_measurements.csv", "time_s,force_y_m_s2,force_z_m_s2",
         [times[data.observation_indices], *data.force_yz_m_s2.T])
    for name, value in trial["settings"].items():
        _write_trace(directory / f"python-{name}.csv", trial["operations"], value["python_trace"])
        _write_trace(directory / f"cpp-{name}.csv", trial["operations"], value["cpp_trace"])


def _plot(path: Path, summary: dict, representatives: dict) -> None:
    figure = Figure(figsize=(12, 11), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(4, 2)
    styles = (("0", "0.55", "--"), ("0.03", "0.30", "-."), ("0.1", "0.05", "-"))
    for column, (name, trial) in enumerate(representatives.items()):
        times = trial["data"].truth.time_s
        axes[0, column].set_title("Constant true bias" if name == "nominal" else "Imposed bias ramp")
        axes[0, column].plot(times, np.rad2deg(trial["data"].bias_rad_s), color="0.1", linestyle=":", label="Truth")
        for key, color, style in styles:
            state = trial["settings"][key]["states"]
            axes[0, column].plot(times, np.rad2deg(state[:, 1]), color=color, linestyle=style,
                                 label=f"σb = {key}", linewidth=1)
            axes[1, column].plot(times, np.rad2deg(state[:, 0] - trial["data"].truth.angle_rad),
                                 color=color, linestyle=style, linewidth=1)
        axes[0, column].legend(fontsize=8, ncols=2)
        for row in (0, 1):
            axes[row, column].set_xlim(0, 30)
            axes[row, column].axvspan(10, 20, color="0.5", alpha=0.09)
            axes[row, column].set_xlabel("Time (s)")
        axes[0, column].set_ylabel("Bias (deg/s)")
        axes[1, column].set_ylabel("Angle error (deg)")
        for row, metric, label in ((2, "ramp_bias_rmse_deg_s", "10–20 s bias RMSE (deg/s)"),
                                   (3, "late_bias_temporal_std_deg_s", "25–30 s bias fluctuation (deg/s)")):
            runs = summary["scenarios"][name]["trials"]
            for pos, (key, _, _) in enumerate(styles):
                values = [run["settings"][key]["metrics"][metric] for run in runs]
                axes[row, column].scatter(pos + np.linspace(-0.15, 0.15, len(values)), values, s=13, color="0.55")
                axes[row, column].vlines(pos, min(values), max(values), color="0.3")
                axes[row, column].scatter(pos, np.mean(values), marker="D", s=32, color="0.05")
            axes[row, column].set_xticks([0, 1, 2], ["0 (constant)", "0.03", "0.1"])
            axes[row, column].set_xlabel("σb ((deg/s)/√s)")
            axes[row, column].set_ylabel(label)
            axes[row, column].set_ylim(bottom=0)
        for row in range(4):
            axes[row, column].grid(axis="y", alpha=0.18)
    figure.suptitle(f"Bias random walk | {summary['phase']}\n"
                   f"Top: preselected seed {summary['representative_seed']}. Bottom: {len(summary['seeds'])} seeds, "
                   "dots = runs, diamonds = means, bars = observed ranges.\n"
                   "Independent vertical scales. Ranges are not confidence intervals; fluctuation includes settling.", fontsize=11)
    figure.savefig(path, dpi=150, metadata={"Software": "Meridian"})


def run_experiment(binary: Path, output_dir: Path, *, phase: str) -> dict:
    if phase not in PHASE_SEEDS:
        raise ValueError("phase must be exploration or evaluation")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    binary = Path(binary).resolve(strict=True)
    source_hashes = _source_hashes()
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    version = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True, timeout=30)
    metadata = json.loads(version.stdout)
    if metadata.get("protocol_version") != 1:
        raise ValueError("C++ binary must support event protocol version 1")
    seeds = PHASE_SEEDS[phase]
    scenarios, representatives = {}, {}
    for scenario in STUDY_SCENARIOS:
        trials = []
        for seed in seeds:
            trial = evaluate_trial(scenario, seed, binary)
            trials.append(_compact(trial, seed))
            if seed == seeds[0]:
                representatives[scenario.name] = trial
        scenarios[scenario.name] = {"trials": trials, "aggregates": _aggregates(trials)}
    spec = protocol()
    if source_hashes != _source_hashes() or binary_hash != hashlib.sha256(binary.read_bytes()).hexdigest():
        raise ValueError("source files or C++ binary changed during the experiment")
    summary = {
        "schema_version": 1, "experiment": "gyro_bias_random_walk", "data_source": "simulation",
        "phase": phase, "seeds": list(seeds), "representative_seed": seeds[0],
        "protocol": spec,
        "protocol_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        "source_sha256": source_hashes,
        "cpp_binary": {"sha256": binary_hash, **metadata},
        "environment": {"python": platform.python_version(), "numpy": np.__version__, "matplotlib": matplotlib.__version__},
        "scenarios": scenarios,
        "limitations": ["Diffusion is a prior; the deterministic ramp does not calibrate a physical random walk.",
                        "Temporal fluctuation includes settling; NIS and seed ranges are descriptive.",
                        "All settings retained; no default retuning or universal ranking.",
                        "Pure-roll simulation and implementation agreement, not new bench or flight validation.",
                        "Final seeds are consumed after inspection; do not reuse them as an untouched reserve."],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, trial in representatives.items():
        _export(output_dir / name, trial)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _plot(output_dir / "overview.png", summary, representatives)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=tuple(PHASE_SEEDS))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.binary, args.output, phase=args.phase)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(f"{summary['phase']}: {len(summary['seeds'])} seeds per case; every Python/C++ comparison passed.")
    print(f"Results: {args.output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
