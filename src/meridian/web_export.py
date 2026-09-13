"""Export paired EKF experiment records for static display, without rerunning filters."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np


COLUMNS = [
    "time_s", "truth_roll_deg", "gyro_roll_deg", "complementary_roll_deg",
    "kalman_roll_deg", "ekf_roll_deg", "truth_bias_deg_s",
    "kalman_bias_deg_s", "ekf_bias_deg_s",
]
FIELDS = {
    "truth.csv": ["time_s", "roll_rad", "bias_rad_s"],
    "estimates.csv": ["time_s", "gyro_roll_rad", "complementary_roll_rad",
                      "kalman_roll_rad", "kalman_bias_rad_s"],
    "ekf_estimates.csv": ["time_s", "ekf_roll_rad", "ekf_bias_rad_s"],
}


def _read_columns(path: Path, fields: list[str]) -> np.ndarray:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):
            raise ValueError(f"missing required columns in {path.name}")
        values = np.array([[float(row[field]) for field in fields] for row in reader])
    if values.ndim != 2 or values.shape[0] < 2 or not np.all(np.isfinite(values)):
        raise ValueError(f"empty or non-finite data in {path.name}")
    if not np.all(np.diff(values[:, 0]) > 0):
        raise ValueError(f"timestamps must increase in {path.name}")
    return values


def build_comparison(source: Path, *, stride: int = 5) -> dict:
    """Validate original endpoints and summary metrics before display decimation.

    This adapter supports the vector_ekf_comparison experiment schema, not arbitrary
    sensor logs. Hash keys are relative to the run; no machine paths are exported.
    """
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise ValueError("stride must be a positive integer")
    source = Path(source)
    summary_path = source / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if (summary.get("schema_version") != 1
            or summary.get("experiment") != "vector_ekf_comparison"
            or summary.get("data_source") != "simulation"):
        raise ValueError("expected a schema-1 vector_ekf_comparison simulation")
    config = summary["config"]
    required_config = ["duration_s", "sample_rate_hz", "amplitude_deg", "frequency_hz",
                       "bias_deg_s", "gyro_noise_std_deg_s", "accel_noise_std_m_s2",
                       "observation_every", "complementary_tau_s", "pulse_start_s",
                       "pulse_end_s", "pulse_y_m_s2"]
    if any(isinstance(config.get(key), bool)
           or not isinstance(config.get(key), (int, float))
           or not math.isfinite(config[key]) for key in required_config):
        raise ValueError("missing or non-finite experiment configuration")
    if (config["duration_s"] <= 0 or config["sample_rate_hz"] <= 0
            or config["observation_every"] <= 0
            or not 0 <= config["pulse_start_s"] < config["pulse_end_s"] <= config["duration_s"]):
        raise ValueError("invalid experiment timing")
    seed = summary["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**53 - 1:
        raise ValueError("display seed must be a nonnegative JavaScript-safe integer")
    hashes = {"summary.json": hashlib.sha256(summary_path.read_bytes()).hexdigest()}
    scenarios = {}
    full_rows = []
    for name in ("nominal", "translation_pulse"):
        tables = []
        for filename, fields in FIELDS.items():
            path = source / name / filename
            tables.append(_read_columns(path, fields))
            hashes[f"{name}/{filename}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        truth, baselines, ekf = tables
        times = truth[:, 0]
        if not np.allclose(truth[:, 2], np.deg2rad(config["bias_deg_s"]), atol=1e-12, rtol=0):
            raise ValueError(f"true bias does not match configuration in {name}")
        expected_roll = np.deg2rad(config["amplitude_deg"]) * np.sin(2 * np.pi * config["frequency_hz"] * times)
        if not np.allclose(truth[:, 1], expected_roll, atol=1e-12, rtol=0):
            raise ValueError(f"roll truth does not match motion configuration in {name}")
        if any(table.shape[0] != len(times)
               or not np.array_equal(table[:, 0], times) for table in tables[1:]):
            raise ValueError(f"misaligned endpoint timestamps in {name}")
        if (times[0] != 0 or not math.isclose(times[-1], config["duration_s"], abs_tol=1e-10, rel_tol=0)
                or len(times) != summary["interval_count"] + 1
                or not np.allclose(np.diff(times), 1 / config["sample_rate_hz"], atol=1e-10, rtol=0)):
            raise ValueError(f"endpoint schedule does not match summary in {name}")
        rows = np.column_stack([times, np.rad2deg(truth[:, 1]),
                                np.rad2deg(baselines[:, 1:4]), np.rad2deg(ekf[:, 1]),
                                np.rad2deg(truth[:, 2]), np.rad2deg(baselines[:, 4]),
                                np.rad2deg(ekf[:, 2])])
        initial = summary["initialization"]
        if (not math.isclose(truth[0, 1], initial["angle_rad"], abs_tol=1e-12, rel_tol=0)
                or not np.allclose(baselines[0, 1:4], truth[0, 1], atol=1e-12, rtol=0)
                or not math.isclose(ekf[0, 1], truth[0, 1], abs_tol=1e-12, rel_tol=0)
                or not np.allclose([baselines[0, 4], ekf[0, 2]], initial["bias_rad_s"], atol=1e-12, rtol=0)):
            raise ValueError(f"initial states do not match summary in {name}")
        metrics = summary["scenarios"][name]
        rmse = {method: metrics["baselines"][method]["rmse_deg"]
                for method in ("gyro", "complementary", "kalman")}
        rmse["ekf"] = metrics["vector_ekf"]["rmse_deg"]
        for index, method in enumerate(rmse, start=2):
            # Match the experiment's unwrapped endpoint error before rounding.
            computed = float(np.sqrt(np.mean((rows[:, index] - rows[:, 1]) ** 2)))
            if not math.isclose(computed, rmse[method], rel_tol=1e-10, abs_tol=1e-10):
                raise ValueError(f"full-run RMSE mismatch for {name}/{method}")
        bias_error = {"kalman": metrics["baselines"]["final_bias_error_deg_s"],
                      "ekf": metrics["vector_ekf"]["final_bias_error_deg_s"]}
        for column, method in ((7, "kalman"), (8, "ekf")):
            if not math.isclose(rows[-1, column] - rows[-1, 6], bias_error[method],
                                rel_tol=1e-10, abs_tol=1e-10):
                raise ValueError(f"final bias metric mismatch for {name}/{method}")
        indices = set(range(0, len(times), stride)) | {len(times) - 1}
        # Retain the first endpoint at/after each change, even for other strides.
        for boundary in (config["pulse_start_s"], config["pulse_end_s"]):
            index = int(np.searchsorted(times, boundary))
            if index < len(times):
                indices.add(index)
        display = rows[sorted(indices)].copy()
        display[:, 1:] = np.round(display[:, 1:], 6)
        scenarios[name] = {
            "source_sample_count": len(times),
            "disturbance": None if name == "nominal" else {
                "start_s": config["pulse_start_s"], "end_s": config["pulse_end_s"],
                "axis": "body_y", "value_m_s2": config["pulse_y_m_s2"],
            },
            "metrics": {"rmse_deg": rmse, "final_bias_error_deg_s": bias_error},
            "rows": display.tolist(),
        }
        full_rows.append(rows)
    if not np.array_equal(full_rows[0][:, [0, 1, 2, 6]], full_rows[1][:, [0, 1, 2, 6]]):
        raise ValueError("paired scenarios must share timing, truth, and gyro estimates")
    all_rows = np.concatenate(full_rows)
    return {
        "schema_version": 1,
        "experiment": "vector_ekf_comparison",
        "data_source": "simulation",
        "seed": seed,
        "config": {key: config[key] for key in required_config},
        "columns": COLUMNS,
        "display_stride": stride,
        "display_decimal_places": 6,
        "domains": {
            "roll_deg": [float(all_rows[:, 1:6].min()), float(all_rows[:, 1:6].max())],
            "bias_deg_s": [float(all_rows[:, 6:9].min()), float(all_rows[:, 6:9].max())],
        },
        "provenance": {
            "exporter": "meridian.web_export",
            "source_sha256": hashes,
            "timing": summary["timing"],
            "initialization": summary["initialization"],
            "paired_noise": summary["paired_noise"],
            # uint64 seeds would lose integer precision when parsed by JavaScript.
            "stream_seeds": {key: str(value) for key, value in summary["stream_seeds"].items()},
            "gravity_m_s2": summary["gravity_m_s2"],
            "metric_basis": "all original endpoints, including initialization; unwrapped angle error",
        },
        "scenarios": scenarios,
    }


def export_comparison(source: Path, output: Path, *, stride: int = 5) -> dict:
    """Write one deterministic artifact; preserve any existing output file."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output file already exists: {output}")
    data = build_comparison(source, stride=stride)
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="existing vector EKF experiment output directory")
    parser.add_argument("--output", type=Path, required=True, help="new display JSON file")
    parser.add_argument("--stride", type=int, default=5, help="display every Nth endpoint (default: 5)")
    args = parser.parse_args(argv)
    try:
        data = export_comparison(args.source, args.output, stride=args.stride)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.exit(2, f"Export failed: {error}\n")
    print(f"Exported {len(data['scenarios'])} simulation scenarios to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
