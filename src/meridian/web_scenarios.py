"""Export nine simulation cases with their recorded timing for the results explorer."""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from meridian.stress_scenarios import SCENARIOS
from meridian.web_export import _read_columns, build_comparison
from meridian.web_diagnostics import build_diagnostics


METHODS = ("gyro", "complementary", "kalman", "ekf")
CONTROLLED_CASES = ("initial_offset", "accel_dropout", "bias_ramp",
                    "initial_overconfident", "accel_noise_mismatch", "timing_jitter", "accel_delay")
ESTIMATE_FIELDS = ["time_s", *[f"{method}_roll_rad" for method in METHODS],
                   "kalman_bias_rad_s", "ekf_bias_rad_s"]
INITIAL_COVARIANCE_FIELDS = [f"{method}_{field}" for method in ("kalman", "ekf")
                           for field in ("p_angle_rad2", "p_angle_bias_rad2_s", "p_bias_rad2_s2")]


def _equal(actual, expected, label, *, tolerance=1e-12):
    actual, expected = np.asarray(actual), np.asarray(expected)
    if actual.shape != expected.shape or not np.allclose(actual, expected, atol=tolerance, rtol=0):
        raise ValueError(f"inconsistent {label}")


def _display(rows, arrivals, boundaries, stride):
    """Keep every correction and its preceding endpoint, plus event boundaries."""
    times = rows[:, 0]
    indices = set(range(0, len(times), stride)) | {len(times)-1}
    correction_indices = set()
    for instant in [*arrivals, *boundaries]:
        index = int(np.argmin(np.abs(times-instant)))
        if not math.isclose(times[index], instant, abs_tol=1e-10, rel_tol=0):
            raise ValueError("correction or event is not aligned with an endpoint")
        indices.update((max(0, index-1), index))
    for instant in arrivals:
        correction_indices.add(int(np.argmin(np.abs(times-instant))))
    changed_bias = np.flatnonzero(np.any(np.diff(rows[:, 7:9], axis=0) != 0, axis=1))+1
    if any(index not in correction_indices for index in changed_bias):
        raise ValueError("bias changes outside a recorded correction")
    reduced = rows[sorted(indices)].copy()
    reduced[:, 1:] = np.round(reduced[:, 1:], 6)
    return reduced.tolist()


def _domains(rows):
    return {"roll_deg": [float(rows[:, 1:6].min()), float(rows[:, 1:6].max())],
            "bias_deg_s": [float(rows[:, 6:9].min()), float(rows[:, 6:9].max())]}


def _timing(times, indices, sample_times, *, irregular=False, delay_s=0.):
    intervals = np.diff(times)
    return {"kind": "irregular" if irregular else "uniform", "accel_delay_s": delay_s,
            "gyro_interval_min_s": float(intervals.min()), "gyro_interval_max_s": float(intervals.max()),
            "correction_predecessor_times_s": times[indices-1].tolist(),
            "correction_sample_times_s": sample_times.tolist()}


def _time_weighted_rmse(errors, times):
    return float(np.sqrt(np.trapezoid(errors**2, times)/(times[-1]-times[0])))


def build_scenarios(paired_source: Path, controlled_source: Path, *, stride: int = 5) -> dict:
    """Validate two experiment schemas; no simulation or estimator is executed.

    Source timestamps and acquisition times are retained, including irregular
    intervals and deliberately uncompensated accelerometer delay.
    """
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise ValueError("stride must be a positive integer")
    paired_source, controlled_source = Path(paired_source), Path(controlled_source)
    data = build_comparison(paired_source, stride=1)
    config = data["config"]
    expected_config = {"duration_s": 30, "sample_rate_hz": 100, "amplitude_deg": 20,
                       "frequency_hz": .1, "bias_deg_s": .5, "gyro_noise_std_deg_s": 1,
                       "accel_noise_std_m_s2": .2, "observation_every": 10, "complementary_tau_s": 1}
    if any(config.get(key) != value for key, value in expected_config.items()):
        raise ValueError("paired configuration does not match the selected controlled suite")
    data.update(schema_version=4, experiment="roll_scenario_explorer", config=expected_config,
                display_stride=stride)
    paired_provenance = data.pop("provenance")
    initial = paired_provenance["initialization"]
    _equal([initial["angle_rad"], initial["angle_std_rad"], initial["bias_rad_s"], initial["bias_std_rad_s"]],
           [0, 0, 0, np.deg2rad(1)], "paired initialization")
    data["sources"] = {"paired": {"experiment": "vector_ekf_comparison", **paired_provenance}}
    for name, case in data["scenarios"].items():
        corrections_path = paired_source/name/"ekf_innovations.csv"
        arrivals = _read_columns(corrections_path, ["time_s"])[:, 0]
        expected_arrivals = np.arange(1, 301)/10
        _equal(arrivals, expected_arrivals, f"{name} correction schedule")
        data["sources"]["paired"]["source_sha256"][f"{name}/ekf_innovations.csv"] = hashlib.sha256(corrections_path.read_bytes()).hexdigest()
        event = case.pop("disturbance")
        if event is not None:
            event = {"kind": "translation", **event}
        full = np.asarray(case["rows"])
        boundaries = [] if event is None else [event["start_s"], event["end_s"]]
        case.update(source="paired", event=event, domains=data["domains"],
                    initialization={"roll_deg": 0., "angle_std_deg": 0., "bias_deg_s": 0., "bias_std_deg_s": 1.},
                    accelerometer_noise={"actual_std_m_s2": .2, "assumed_std_m_s2": .2},
                    scheduled_accel_count=300, correction_times_s=arrivals.tolist(),
                    timing=_timing(full[:, 0], np.arange(10, 3001, 10), arrivals),
                    rows=_display(full, arrivals, boundaries, stride))
        # The pair's full-resolution metrics and shared domains are retained.
        # Additional duration weighting must use the original, unrounded CSVs.
        raw_truth = _read_columns(paired_source/name/"truth.csv", ["time_s", "roll_rad"])
        raw_angles = _read_columns(paired_source/name/"estimates.csv", ["time_s", *[f"{method}_roll_rad" for method in METHODS[:3]]])
        raw_ekf = _read_columns(paired_source/name/"ekf_estimates.csv", ["time_s", "ekf_roll_rad"])
        _equal(raw_angles[:, 0], raw_truth[:, 0], f"{name} weighted metric timing")
        _equal(raw_ekf[:, 0], raw_truth[:, 0], f"{name} weighted EKF timing")
        errors = np.rad2deg(np.column_stack([raw_angles[:, 1:], raw_ekf[:, 1]])-raw_truth[:, 1, None])
        case["metrics"]["time_weighted_rmse_deg"] = {
            method: _time_weighted_rmse(errors[:, column], raw_truth[:, 0]) for column, method in enumerate(METHODS)}

    summary_path = controlled_source/"summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if (summary.get("schema_version") != 1 or summary.get("experiment") != "controlled_scenarios"
            or summary.get("data_source") != "simulation" or summary.get("seed") != data["seed"]):
        raise ValueError("expected controlled simulation with the same selected seed")
    shared = {"duration_s": 30, "gyro_interval_count": 3000, "scheduled_accel_count": 300,
              "amplitude_deg": 20, "frequency_hz": .1, "gravity_m_s2": 9.80665,
              "gyro_noise_std_deg_s_per_interval": 1, "assumed_accel_noise_std_m_s2": .2,
              "initial_bias_deg_s": 0, "initial_bias_std_deg_s": 1, "bias_random_walk": False,
              "complementary_tau_s": 1, "late_window_start_s": 25}
    if summary.get("shared_settings") != shared:
        raise ValueError("unsupported controlled settings")
    definitions = {item["name"]: item for item in summary["scenario_definitions"]}
    expected_definitions = {item.name: asdict(item) for item in SCENARIOS if item.name in CONTROLLED_CASES}
    if any(definitions.get(name) != definition for name, definition in expected_definitions.items()):
        raise ValueError("unsupported controlled scenario definition")
    hashes = {"summary.json": hashlib.sha256(summary_path.read_bytes()).hexdigest()}
    streams = {}
    paired_inputs = {}
    for name in CONTROLLED_CASES:
        def read(filename, fields):
            path = controlled_source/name/filename
            hashes[f"{name}/{filename}"] = hashlib.sha256(path.read_bytes()).hexdigest()
            return _read_columns(path, fields)

        truth = read("truth.csv", ["time_s", "roll_rad", "bias_rad_s"])
        estimates = read("estimates.csv", ESTIMATE_FIELDS+INITIAL_COVARIANCE_FIELDS)
        times = truth[:, 0]
        definition = definitions[name]
        if definition["timing_jitter"]:
            # Bounds implied by 3000 positive U(0.5, 1.5) weights normalized to 30 s.
            dt = np.diff(times)
            if (len(times) != 3001 or times[0] != 0 or times[-1] != 30
                    or dt.min() < 30/(3*3000) or dt.max() > 3*30/3000
                    or dt.max()/dt.min() > 3 or np.allclose(dt, .01, atol=1e-12, rtol=0)):
                raise ValueError("inconsistent timing_jitter endpoints")
        else:
            _equal(times, np.arange(3001)/100, f"{name} endpoints")
        _equal(estimates[:, 0], times, f"{name} aligned estimates")
        _equal(truth[:, 1], np.deg2rad(20)*np.sin(2*np.pi*.1*times), f"{name} roll truth")
        expected_bias = np.full(len(times), .5)
        if name == "bias_ramp":
            expected_bias += .1*np.clip(times-10, 0, 10)
        _equal(truth[:, 2], np.deg2rad(expected_bias), f"{name} bias truth")
        _equal(estimates[0, 1:5], np.full(4, np.deg2rad(definition["initial_angle_deg"])), f"{name} initial roll")
        _equal(estimates[0, 5:7], [0, 0], f"{name} initial bias")
        _equal(estimates[0, 7:], [np.deg2rad(definition["initial_angle_std_deg"])**2, 0, np.deg2rad(1)**2]*2,
               f"{name} initial covariance")
        schedule = read("observation_schedule_truth.csv", ["arrival_time_s", "sample_time_s", "available"])
        scheduled_indices = np.arange(10, 3001, 10)
        _equal(schedule[:, 0], times[scheduled_indices], f"{name} scheduled arrivals")
        _equal(schedule[:, 1], schedule[:, 0]-definition["accel_delay_s"], f"{name} sample times")
        if np.any(schedule[:, 1] < 0):
            raise ValueError(f"inconsistent {name} negative sample time")
        available = np.ones(300, dtype=bool)
        if name == "accel_dropout":
            available = ~((schedule[:, 0] >= 12) & (schedule[:, 0] < 17))
        _equal(schedule[:, 2], available.astype(int), f"{name} availability")
        arrivals = schedule[available, 0]
        accel = read("accel_measurements.csv", ["arrival_time_s", "force_y_m_s2", "force_z_m_s2"])
        _equal(accel[:, 0], arrivals, f"{name} available measurements")
        # Acquisition time is provenance only: delayed measurements were fused
        # at arrival. Noise draws pair by scheduled order, even when times differ.
        angle = np.deg2rad(20)*np.sin(2*np.pi*.1*schedule[available, 1])
        gravity = -shared["gravity_m_s2"]*np.column_stack([np.sin(angle), np.cos(angle)])
        accel_noise = (accel[:, 1:]-gravity)/definition["accel_noise_std_m_s2"]
        for filename in ("kalman_innovations.csv", "ekf_innovations.csv"):
            _equal(read(filename, ["arrival_time_s"])[:, 0], arrivals, f"{name} actual corrections")
        gyro = read("gyro_measurements.csv", ["t_start_s", "t_end_s", "rate_rad_s"])
        _equal(gyro[:, :2], np.column_stack([times[:-1], times[1:]]), f"{name} gyro intervals")
        interval_truth = read("gyro_interval_truth.csv", ["t_start_s", "t_end_s", "mean_rate_rad_s", "mean_bias_rad_s"])
        _equal(interval_truth[:, :2], gyro[:, :2], f"{name} interval truth times")
        _equal(interval_truth[:, 2], np.diff(truth[:, 1])/np.diff(times), f"{name} interval rate truth")
        # Ramp knots lie on its uniform grid; other cases have constant bias.
        # Trapezoid means are exact, independently of the simulator's integral code.
        mean_bias = np.deg2rad((expected_bias[:-1]+expected_bias[1:])/2)
        _equal(interval_truth[:, 3], mean_bias, f"{name} interval bias truth")
        expected_gyro = np.deg2rad(definition["initial_angle_deg"])+np.r_[0, np.cumsum(gyro[:, 2]*np.diff(times))]
        _equal(estimates[:, 1], expected_gyro, f"{name} gyro integration", tolerance=1e-10)
        record = summary["scenarios"][name]
        _equal([record["gyro_dt_min_s"], record["gyro_dt_max_s"]],
               [np.diff(times).min(), np.diff(times).max()], f"{name} gyro interval range")
        if record["used_accel_count"] != len(arrivals) or not math.isclose(
                record["maximum_correction_gap_s"], float(np.diff(np.r_[0, arrivals]).max()), abs_tol=1e-10, rel_tol=0):
            raise ValueError(f"inconsistent {name} correction count or gap")
        rows = np.column_stack([times, np.rad2deg(truth[:, 1]), np.rad2deg(estimates[:, 1:5]),
                                np.rad2deg(truth[:, 2]), np.rad2deg(estimates[:, 5:7])])
        metrics = {"rmse_deg": {}, "final_bias_error_deg_s": {}, "time_weighted_rmse_deg": {}}
        for column, method in enumerate(METHODS, start=2):
            rmse = record["metrics"][method]["angle_rmse_deg"]
            computed = float(np.sqrt(np.mean((rows[:, column]-rows[:, 1])**2)))
            if not math.isclose(rmse, computed, abs_tol=1e-10, rel_tol=1e-10):
                raise ValueError(f"inconsistent {name}/{method} RMSE")
            metrics["rmse_deg"][method] = rmse
            weighted = record["metrics"][method]["angle_time_weighted_rmse_deg"]
            if not math.isclose(weighted, _time_weighted_rmse(rows[:, column]-rows[:, 1], times),
                                abs_tol=1e-10, rel_tol=1e-10):
                raise ValueError(f"inconsistent {name}/{method} weighted RMSE")
            metrics["time_weighted_rmse_deg"][method] = weighted
        for column, method in ((7, "kalman"), (8, "ekf")):
            error = record["metrics"][method]["final_bias_error_deg_s"]
            if not math.isclose(error, rows[-1, column]-rows[-1, 6], abs_tol=1e-10, rel_tol=1e-10):
                raise ValueError(f"inconsistent {name}/{method} final bias")
            metrics["final_bias_error_deg_s"][method] = error
        event = None
        if name == "accel_dropout":
            event = {
                "kind": "accel_dropout", "start_s": 12., "end_s": 17., "omitted_count": 50,
                "last_correction_before_s": float(arrivals[arrivals < 12][-1]),
                "first_correction_after_s": float(arrivals[arrivals >= 17][0]),
            }
        elif name == "bias_ramp":
            event = {"kind": "bias_ramp", "start_s": 10., "end_s": 20.,
                     "initial_bias_deg_s": .5, "final_bias_deg_s": 1.5, "slope_deg_s2": .1}
        boundaries = [] if event is None else [event["start_s"], event["end_s"]]
        data["scenarios"][name] = {
            "source": "controlled", "source_sample_count": len(times), "event": event,
            "initialization": {"roll_deg": definition["initial_angle_deg"], "angle_std_deg": definition["initial_angle_std_deg"],
                               "bias_deg_s": 0., "bias_std_deg_s": 1.},
            "accelerometer_noise": {"actual_std_m_s2": definition["accel_noise_std_m_s2"],
                                    "assumed_std_m_s2": shared["assumed_accel_noise_std_m_s2"]},
            "scheduled_accel_count": 300, "correction_times_s": arrivals.tolist(),
            "timing": _timing(times, scheduled_indices[available], schedule[available, 1],
                              irregular=definition["timing_jitter"], delay_s=definition["accel_delay_s"]),
            "metrics": metrics, "domains": _domains(rows),
            "rows": _display(rows, arrivals, boundaries, stride),
        }
        streams[name] = {key: str(value) for key, value in record["stream_seeds"].items()}
        paired_inputs[name] = (truth, gyro, gyro[:, 2]-interval_truth[:, 2]-mean_bias, accel_noise, available)
    reference_truth, reference_gyro, reference_noise, reference_accel_noise, _ = paired_inputs["initial_offset"]
    for name in CONTROLLED_CASES[1:]:
        truth, gyro, gyro_noise, accel_noise, available = paired_inputs[name]
        if name != "timing_jitter":
            _equal(reference_truth[:, :2], truth[:, :2], f"{name} paired motion truth")
            _equal(reference_gyro[:, :2], gyro[:, :2], f"{name} paired gyro timing")
        if streams[name] != streams["initial_offset"]:
            raise ValueError(f"inconsistent {name} paired stream seeds")
        _equal(reference_noise, gyro_noise, f"{name} paired gyro noise")
        _equal(reference_accel_noise[available], accel_noise, f"{name} paired accelerometer noise")
    data["sources"]["controlled"] = {
        "experiment": "controlled_scenarios", "source_sha256": hashes, "stream_seeds": streams,
        "metric_basis": "all original endpoints, including initialization; unwrapped angle error",
    }
    for source, provenance in data["sources"].items():
        provenance["time_weighted_metric_basis"] = (
            "trapezoidal squared endpoint errors over duration, from original unrounded CSVs; "
            + ("computed by display adapter" if source == "paired" else "checked against controlled summary"))
    for name, case in data["scenarios"].items():
        case["diagnostics"], diagnostic_hashes = build_diagnostics(
            paired_source if case["source"] == "paired" else controlled_source, name, case)
        data["sources"][case["source"]]["source_sha256"].update(diagnostic_hashes)
    data.pop("domains")
    return data


def export_scenarios(paired_source: Path, controlled_source: Path, output: Path, *, stride: int = 5) -> dict:
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output file already exists: {output}")
    data = build_scenarios(paired_source, controlled_source, stride=stride)
    payload = json.dumps(data, allow_nan=False, separators=(",", ":"))+"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paired_source", type=Path)
    parser.add_argument("controlled_source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stride", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        data = export_scenarios(args.paired_source, args.controlled_source, args.output, stride=args.stride)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.exit(2, f"Export failed: {error}\n")
    print(f"Exported {len(data['scenarios'])} recorded simulation cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
