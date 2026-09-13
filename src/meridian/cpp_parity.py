"""Compare the C++ EKF with Python after every serialized prediction and update."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import tempfile

import numpy as np

from meridian.accel_experiment import FusionConfig, run_trial
from meridian.ekf import AngleBiasEKF
from meridian.stress_scenarios import SCENARIOS, generate_scenario


CASE_NAMES = tuple(scenario.name for scenario in SCENARIOS) + ("translation_pulse",)
VALUE_NAMES = ("angle_rad", "bias_rad_s", "p00", "p01", "p10", "p11", "v0", "v1", "s00", "s01", "s10", "s11")
TRACE_HEADER = ("step", "operation", *VALUE_NAMES)
# Fixed before evaluation; absolute units follow each named scalar quantity.
TOLERANCES = {name: {"atol": 1e-12 if name.startswith("p") else 1e-10, "rtol": 1e-10}
              for name in VALUE_NAMES}


def scenario_events(name: str, seed: int) -> tuple[dict, list[tuple[str, float, float]]]:
    """Extract only configuration and input operations from existing simulations."""
    if name == "translation_pulse":
        trial = run_trial(FusionConfig(), seed, disturbed=True)
        times, rates, indices, force = trial["truth"].time_s, trial["gyro"], trial["indices"], trial["force"]
        angle_deg, angle_std_deg = 0.0, 0.0
    else:
        scenario = next((case for case in SCENARIOS if case.name == name), None)
        if scenario is None:
            raise ValueError(f"unknown scenario: {name}")
        data = generate_scenario(scenario, seed)
        times, rates = data.truth.time_s, data.gyro_rate_rad_s
        indices, force = data.observation_indices[data.available], data.force_yz_m_s2[data.available]
        angle_deg, angle_std_deg = scenario.initial_angle_deg, scenario.initial_angle_std_deg
    config = {"initial_angle_rad": float(np.deg2rad(angle_deg)), "initial_bias_rad_s": 0.0,
              "initial_angle_std_rad": float(np.deg2rad(angle_std_deg)),
              "initial_bias_std_rad_s": float(np.deg2rad(1.0)),
              "gyro_noise_std_rad_s": float(np.deg2rad(1.0)),
              "accel_noise_std_m_s2": 0.2, "gravity_m_s2": 9.80665}
    events = []
    observation = 0
    for endpoint, dt in enumerate(np.diff(times), start=1):
        events.append(("predict", float(rates[endpoint - 1]), float(dt)))
        if observation < len(indices) and endpoint == indices[observation]:
            events.append(("update", float(force[observation, 0]), float(force[observation, 1])))
            observation += 1
    return config, events


def serialize_events(events: list[tuple[str, float, float]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["operation", "value0", "value1"])
    for operation, first, second in events:
        writer.writerow([operation, format(first, ".17g"), format(second, ".17g")])
    return buffer.getvalue()


def python_trace(config: dict, serialized: str) -> tuple[list[str], np.ndarray]:
    """Decode serialized input numbers before running the Python reference."""
    reader = csv.reader(io.StringIO(serialized))
    if next(reader, None) != ["operation", "value0", "value1"]:
        raise ValueError("invalid event header")
    estimator = AngleBiasEKF(**config)
    operations = ["init"]
    values = [np.r_[estimator.state, estimator.covariance.ravel(), np.full(6, np.nan)]]
    for row in reader:
        if len(row) != 3 or row[0] not in ("predict", "update"):
            raise ValueError("invalid event row")
        first, second = float(row[1]), float(row[2])
        diagnostics = np.full(6, np.nan)
        if row[0] == "predict":
            estimator.predict(first, second)
        else:
            innovation, covariance = estimator.update([first, second])
            diagnostics = np.r_[innovation, covariance.ravel()]
        operations.append(row[0])
        values.append(np.r_[estimator.state, estimator.covariance.ravel(), diagnostics])
    return operations, np.array(values)


def _write_trace(path: Path, operations: list[str], values: np.ndarray) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(TRACE_HEADER)
        for step, (operation, row) in enumerate(zip(operations, values)):
            writer.writerow([step, operation, *("" if np.isnan(value) else format(value, ".17g") for value in row)])


def read_cpp_trace(path: Path, expected_operations: list[str]) -> np.ndarray:
    """Reject missing/reordered records and absent or spurious diagnostics."""
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or tuple(rows[0]) != TRACE_HEADER or len(rows) != len(expected_operations) + 1:
        raise ValueError("C++ output header or record count mismatch")
    values = np.full((len(expected_operations), len(VALUE_NAMES)), np.nan)
    for step, (row, operation) in enumerate(zip(rows[1:], expected_operations)):
        if len(row) != len(TRACE_HEADER) or row[:2] != [str(step), operation]:
            raise ValueError(f"C++ output operation mismatch at step {step}")
        observed_count = 12 if operation == "update" else 6
        observed = np.array([float(value) for value in row[2:2 + observed_count]])
        if not np.all(np.isfinite(observed)) or any(row[2 + observed_count:]):
            raise ValueError(f"C++ output invalid values or diagnostic availability at step {step}")
        values[step, :observed_count] = observed
    return values


def compare_traces(expected: np.ndarray, actual: np.ndarray) -> dict:
    """Compare every finite scalar with its predeclared tolerance and unit."""
    if expected.shape != actual.shape or expected.ndim != 2 or expected.shape[1] != len(VALUE_NAMES):
        raise ValueError("trace shape mismatch")
    if not np.array_equal(np.isnan(expected), np.isnan(actual)):
        raise ValueError("trace diagnostic mask mismatch")
    checks = {}
    for column, name in enumerate(VALUE_NAMES):
        mask = ~np.isnan(expected[:, column])
        reference, candidate = expected[mask, column], actual[mask, column]
        if not np.all(np.isfinite(reference)) or not np.all(np.isfinite(candidate)):
            raise ValueError("trace contains nonfinite values")
        tolerance = TOLERANCES[name]
        error = np.abs(candidate - reference)
        scaled_error = error / (tolerance["atol"] + tolerance["rtol"] * np.abs(reference))
        checks[name] = {"compared_values": int(len(error)),
                        "max_abs_difference": float(np.max(error)) if len(error) else 0.0,
                        "max_tolerance_ratio": float(np.max(scaled_error)) if len(error) else 0.0,
                        "passed": bool(np.all(scaled_error <= 1.0))}
    return {"record_count": len(expected), "quantities": checks,
            "passed": all(check["passed"] for check in checks.values())}


def replay_command(binary: Path, config: dict, source: Path, output: Path) -> list[str]:
    command = [str(binary), "--input", str(source), "--output", str(output)]
    for name, value in config.items():
        command += ["--" + name.replace("_", "-"), format(value, ".17g")]
    return command


def compare_case(binary: Path, name: str, seed: int, directory: Path) -> dict:
    """Retain the exact input and both traces for one selected case."""
    config, events = scenario_events(name, seed)
    serialized = serialize_events(events)
    operations, expected = python_trace(config, serialized)
    directory.mkdir(parents=True, exist_ok=False)
    source, output = directory / "events.csv", directory / "cpp_trace.csv"
    source.write_text(serialized, encoding="utf-8")
    process = subprocess.run(replay_command(binary, config, source, output), text=True,
                             capture_output=True, timeout=30, check=False)
    if process.returncode != 0:
        raise ValueError(f"C++ replay failed for {name}, seed {seed}: {process.stderr.strip()}")
    actual = read_cpp_trace(output, operations)
    comparison = compare_traces(expected, actual)
    _write_trace(directory / "python_trace.csv", operations, expected)
    return {"scenario": name, "seed": seed, "configuration": config,
            "input_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "prediction_count": operations.count("predict"), "correction_count": operations.count("update"),
            **comparison}


def run_experiment(binary: Path, output_dir: Path, *, seed: int = 42, validation_seeds: int = 20) -> dict:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if isinstance(validation_seeds, bool) or not isinstance(validation_seeds, int) or validation_seeds < 1:
        raise ValueError("validation_seeds must be a positive integer")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    binary = Path(binary).resolve(strict=True)
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30, check=True)
    metadata = json.loads(version.stdout)
    if metadata.get("protocol_version") != 1:
        raise ValueError("C++ binary must support event protocol version 1")
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=False)
    selected = [compare_case(binary, name, seed, output_dir / name) for name in CASE_NAMES]
    repeats = []
    # Temporary traces are never published; every repeat retains scalar maxima.
    for value in range(validation_seeds):
        for name in CASE_NAMES:
            with tempfile.TemporaryDirectory(prefix="parity-", dir=output_dir) as temporary:
                repeats.append(compare_case(binary, name, value, Path(temporary) / "trace"))
    all_results = selected + repeats
    summary = {
        "schema_version": 1, "experiment": "cpp_python_ekf_parity", "data_source": "simulation",
        "binary": {"name": binary.name, "sha256": binary_digest, **metadata},
        "python_environment": {"python": platform.python_version(), "numpy": np.__version__},
        "seed": seed, "validation_seeds": list(range(validation_seeds)), "scenario_names": list(CASE_NAMES),
        "tolerances": TOLERANCES,
        "comparison_rule": "abs(cpp-python) <= atol + rtol * abs(python); both use decoded serialized inputs",
        "quantity_units": {"angle_rad": "rad", "bias_rad_s": "rad/s", "p00": "rad^2",
                           "p01": "rad^2/s", "p10": "rad^2/s", "p11": "rad^2/s^2",
                           "v0": "m/s^2", "v1": "m/s^2", "s00": "m^2/s^4", "s01": "m^2/s^4",
                           "s10": "m^2/s^4", "s11": "m^2/s^4"},
        "selected": selected, "validation": repeats,
        "aggregate": {"trace_count": len(all_results),
                      "record_count": sum(result["record_count"] for result in all_results),
                      "quantities": {name: {
                          "compared_values": sum(result["quantities"][name]["compared_values"] for result in all_results),
                          "max_abs_difference": max(result["quantities"][name]["max_abs_difference"] for result in all_results),
                          "max_tolerance_ratio": max(result["quantities"][name]["max_tolerance_ratio"] for result in all_results),
                      } for name in VALUE_NAMES}},
        "passed": all(result["passed"] for result in all_results),
        "limitations": ["Agreement is numerical equivalence on stated cases, not independent physical accuracy.",
                        "Both implementations retain known initialization, translation, bias and delay limitations.",
                        "Different solvers may reject different extreme ill-conditioned inputs.",
                        "No timing/allocation guarantee, float32 assessment, embedded deployment or flight validation."],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/cpp-parity"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-seeds", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.binary, args.output, seed=args.seed, validation_seeds=args.validation_seeds)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(f"Compared {summary['aggregate']['trace_count']} traces and {summary['aggregate']['record_count']} operation records.")
    print(f"Numerical agreement: {'PASS' if summary['passed'] else 'FAIL'}; results written to {args.output}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
