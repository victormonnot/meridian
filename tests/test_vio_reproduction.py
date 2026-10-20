"""Numerical reproduction criteria use synthetic states with known changes."""

import csv
import json
import math
from pathlib import Path

import pytest

from meridian.vio_reproduction import compare_results


TIMES = [1_000_000_000, 2_000_000_000, 3_000_000_000, 4_000_000_000]
POSITIONS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 1, 1)]


def write_run(directory, *, offset=0, yaw_deg=0, sign=1, times=None, metric_delta=0):
    directory.mkdir()
    angle = math.radians(yaw_deg) / 2
    quaternion = [0, 0, sign * math.sin(angle), sign * math.cos(angle)]
    timestamps = TIMES if times is None else times
    with (directory / "trajectory.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp_ns", "px_m", "py_m", "pz_m", "qx", "qy", "qz", "qw"])
        for timestamp, position in zip(timestamps, POSITIONS):
            writer.writerow([timestamp, position[0] + offset, *position[1:], *quaternion])
    evaluation = {
        "schema_version": 1, "status": "ok", "alignment": {"scale": 1.0},
        "protocol": {"alignment": "global_SE3", "estimate_interpolation": False},
        "counts": {"expected_camera_samples": 4, "estimate_samples": len(timestamps),
                   "evaluated_camera_samples": len(timestamps)},
        "coverage_fraction": len(timestamps) / 4, "evaluated_fraction": len(timestamps) / 4,
        "reference_coverage_fraction": 1, "evaluated_reference_fraction": len(timestamps) / 4,
        "initialization_delay_s": 0,
        "missing_estimate_intervals": [], "unsupported_reference_intervals": [],
        "per_sample": {"timestamp_ns": timestamps},
        "metrics": {"ate_rmse_m": 0.06 + metric_delta, "orientation_rmse_deg": 1.0},
    }
    (directory / "evaluation.json").write_text(json.dumps(evaluation))
    return directory


def change_evaluation(directory, mutate):
    path = directory / "evaluation.json"
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))


def test_identical_states_pass_with_exact_support_and_bytes(tmp_path):
    first, second = write_run(tmp_path / "first"), write_run(tmp_path / "second")
    comparison = compare_results(first, second)
    assert comparison["passed"]
    assert comparison["trajectory_bytes_identical"]
    assert comparison["maximum_direct_position_difference_m"] == 0
    assert comparison["maximum_direct_orientation_difference_deg"] == 0


def test_quaternion_sign_is_the_same_orientation(tmp_path):
    comparison = compare_results(write_run(tmp_path / "first"),
                                 write_run(tmp_path / "second", sign=-1))
    assert comparison["passed"]
    assert not comparison["trajectory_bytes_identical"]
    assert comparison["maximum_direct_orientation_difference_deg"] == 0


def test_small_known_displacement_and_rotation_pass_without_refit(tmp_path):
    comparison = compare_results(write_run(tmp_path / "first"),
                                 write_run(tmp_path / "second", offset=0.0002, yaw_deg=0.0002))
    assert comparison["passed"]
    assert comparison["maximum_direct_position_difference_m"] == pytest.approx(0.0002)
    assert comparison["maximum_direct_orientation_difference_deg"] == pytest.approx(0.0002)


@pytest.mark.parametrize("change,failed", [
    ({"offset": 0.002}, "direct_position_within_tolerance"),
    ({"offset": 10}, "direct_position_within_tolerance"),
    ({"yaw_deg": 0.002}, "direct_orientation_within_tolerance"),
    ({"metric_delta": 0.002}, "position_rmse_within_tolerance"),
])
def test_wrong_poses_or_scores_fail_even_if_other_metrics_agree(tmp_path, change, failed):
    comparison = compare_results(write_run(tmp_path / "first"),
                                 write_run(tmp_path / "second", **change))
    assert not comparison["passed"]
    assert not comparison["checks"][failed]


def test_dropped_hard_epoch_cannot_pass_by_lowering_error(tmp_path):
    first = write_run(tmp_path / "first")
    second = write_run(tmp_path / "second", times=TIMES[:-1], metric_delta=-0.0001)
    comparison = compare_results(first, second)
    assert not comparison["passed"]
    assert not comparison["checks"]["identical_output_timestamps"]
    assert not comparison["checks"]["identical_evaluation_support"]
    assert comparison["maximum_direct_position_difference_m"] is None
    assert comparison["metrics"]["ate_rmse_m"]["passed"]


@pytest.mark.parametrize("mutate,failed", [
    (lambda value: value["protocol"].update(estimate_interpolation=True), "identical_evaluation_protocol"),
    (lambda value: value["alignment"].update(scale=1.01), "unit_scale_alignment"),
    (lambda value: value.update(unsupported_reference_intervals=[{"sample_count": 1}]),
     "identical_support_counts_and_missing_intervals"),
    (lambda value: value["per_sample"].update(timestamp_ns=TIMES[1:]),
     "identical_evaluation_support"),
    (lambda value: value["metrics"].update(orientation_rmse_deg=1.01),
     "orientation_rmse_within_tolerance"),
    (lambda value: value.update(status="failed"), "successful_evaluation"),
])
def test_protocol_support_and_orientation_are_independent_checks(tmp_path, mutate, failed):
    first, second = write_run(tmp_path / "first"), write_run(tmp_path / "second")
    change_evaluation(second, mutate)
    comparison = compare_results(first, second)
    assert not comparison["passed"]
    assert not comparison["checks"][failed]


def test_nonfinite_scores_fail_instead_of_passing_a_nan_comparison(tmp_path):
    first, second = write_run(tmp_path / "first"), write_run(tmp_path / "second")
    change_evaluation(second, lambda value: value["metrics"].update(ate_rmse_m=float("nan")))
    with pytest.raises(ValueError):
        compare_results(first, second)
