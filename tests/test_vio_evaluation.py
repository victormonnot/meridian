"""Independent geometric and missing-data fixtures for VIO evaluation."""

import csv
import json

import numpy as np
import pytest

from meridian.vio_evaluation import (
    TRAJECTORY_COLUMNS,
    Trajectory,
    evaluate_trajectories,
    evaluate_trajectory,
    read_trajectory,
)


POINTS = np.array([[0., 0., 0.], [1., 0., 0.], [1., 2., 0.],
                   [0., 1., 2.], [-1., 0., 1.]])


def trajectory(positions=POINTS, times=None, quaternions=None):
    count = len(positions)
    if times is None:
        times = np.arange(count, dtype=np.int64) * 10_000_000
    if quaternions is None:
        quaternions = np.tile([0., 0., 0., 1.], (count, 1))
    return Trajectory(times, np.asarray(positions, dtype=float).reshape(-1, 3),
                      np.asarray(quaternions, dtype=float).reshape(-1, 4))


def evaluate(estimate, reference, **options):
    expected = options.pop("expected_timestamps_ns", reference.timestamp_ns)
    return evaluate_trajectories(estimate, reference, expected_timestamps_ns=expected, **options)


def write_csv(path, data):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(TRAJECTORY_COLUMNS)
        for time, position, quaternion in zip(data.timestamp_ns, data.position_m, data.quaternion_xyzw):
            writer.writerow([int(time), *position, *quaternion])
    return path


@pytest.mark.parametrize("rotation,quaternion", [
    ([[1, 0, 0], [0, 0, -1], [0, 1, 0]], [2**-.5, 0, 0, 2**-.5]),
    ([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], [0, 2**-.5, 0, 2**-.5]),
    ([[0, -1, 0], [1, 0, 0], [0, 0, 1]], [0, 0, 2**-.5, 2**-.5]),
])
def test_known_global_rotation_translation_removed_for_position_and_orientation(rotation, quaternion):
    rotation = np.array(rotation)
    translation = np.array([4., -2., 7.])
    reference = trajectory(POINTS @ rotation.T + translation,
                           quaternions=np.tile(quaternion, (len(POINTS), 1)))
    result = evaluate(trajectory(), reference)
    assert result["status"] == "ok"
    assert result["metrics"]["ate_rmse_m"] < 1e-14
    assert result["metrics"]["orientation_rmse_deg"] < 1e-12
    np.testing.assert_allclose(result["alignment"]["rotation_estimate_world_to_reference_world"], rotation, atol=1e-14)
    np.testing.assert_allclose(result["alignment"]["translation_m"], translation, atol=1e-14)
    assert result["alignment"]["scale"] == 1.0
    assert result["protocol"]["scale_fitted"] is False
    assert result["protocol"]["time_offset_fitted"] is False
    assert "roll_pitch_yaw" in result["protocol"]["removed_gauge"]


def test_scale_defect_remains_after_best_rigid_alignment():
    result = evaluate(trajectory(2 * POINTS), trajectory())
    # Scaling about the centroid leaves exactly this residual after best SE(3).
    expected = np.sqrt(np.mean(np.sum((POINTS - POINTS.mean(axis=0))**2, axis=1)))
    assert result["metrics"]["ate_rmse_m"] == pytest.approx(expected)
    assert result["alignment"]["scale"] == 1


def test_body_frame_orientation_error_is_not_fixed_by_position_alignment():
    q = np.tile([0., 0., np.sin(np.pi / 12), np.cos(np.pi / 12)], (len(POINTS), 1))
    result = evaluate(trajectory(quaternions=q), trajectory())
    assert result["metrics"]["ate_rmse_m"] < 1e-14
    assert result["metrics"]["orientation_rmse_deg"] == pytest.approx(30)
    assert result["metrics"]["orientation_rmse_rad"] == pytest.approx(np.pi / 6)


def test_body_to_world_inversion_is_detected_on_varying_orientation():
    angles = np.deg2rad([0., 10., 20., 30., 40.])
    quaternion = np.column_stack((np.sin(angles / 2), np.zeros((len(POINTS), 2)), np.cos(angles / 2)))
    inverse = quaternion.copy()
    inverse[:, :3] *= -1
    result = evaluate(trajectory(quaternions=inverse), trajectory(quaternions=quaternion))
    assert result["metrics"]["orientation_rmse_deg"] == pytest.approx(np.sqrt(np.mean((2*np.rad2deg(angles))**2)))


def test_reflection_cannot_be_explained_as_proper_world_rotation():
    reflected = POINTS.copy()
    reflected[:, 0] *= -1
    result = evaluate(trajectory(reflected), trajectory())
    assert np.linalg.det(result["alignment"]["rotation_estimate_world_to_reference_world"]) == pytest.approx(1)
    assert result["metrics"]["ate_rmse_m"] > .1


def test_quaternion_sign_and_pi_rotation_have_correct_geodesic_errors():
    half_turn = np.tile([1., 0., 0., 0.], (len(POINTS), 1))
    sign_variant = half_turn.copy()
    sign_variant[::2] *= -1
    same = evaluate(trajectory(quaternions=sign_variant), trajectory(quaternions=half_turn))
    assert same["metrics"]["orientation_rmse_deg"] < 1e-12
    different = evaluate(trajectory(quaternions=half_turn), trajectory())
    assert different["metrics"]["orientation_rmse_deg"] == pytest.approx(180)


def test_reference_interpolation_uses_shortest_arc_slerp_and_integer_time_differences():
    origin = 1_500_000_000_000_000_000
    times = origin + np.arange(7, dtype=np.int64)
    positions = np.array([[0, 0, 0], [.5, 0, 0], [1, 0, 0], [1, .5, 0],
                          [1, 1, 0], [.5, 1, 0], [0, 1, 0]])
    angles = np.arange(7) * np.pi / 6
    quaternions = np.column_stack((np.zeros((7, 2)), np.sin(angles/2), np.cos(angles/2)))
    reference_q = quaternions[::2].copy()
    reference_q[1::2] *= -1
    reference = trajectory(positions[::2], times[::2], reference_q)
    result = evaluate(trajectory(positions, times, quaternions), reference,
                      expected_timestamps_ns=times, max_reference_gap_ns=2,
                      max_estimate_timestamp_error_ns=0)
    assert result["status"] == "ok"
    assert result["metrics"]["ate_rmse_m"] < 1e-14
    assert result["metrics"]["orientation_rmse_deg"] < 1e-12
    assert result["per_sample"]["timestamp_ns"] == times.tolist()
    assert result["reference_coverage_fraction"] == 1


def test_reference_gaps_and_extrapolation_are_excluded_and_counted():
    times = np.arange(-1, 14, dtype=np.int64)
    positions = np.column_stack((times, times**2, np.zeros(len(times))))
    reference_times = np.array([0, 2, 4, 10, 12], dtype=np.int64)
    reference = trajectory(np.column_stack((reference_times, reference_times**2, np.zeros(5))), reference_times)
    result = evaluate(trajectory(positions, times), reference,
                      expected_timestamps_ns=times, max_reference_gap_ns=2)
    assert result["counts"]["evaluated_camera_samples"] == 8
    assert result["coverage_fraction"] == 1
    assert result["reference_coverage_fraction"] == pytest.approx(8/15)
    assert result["evaluated_reference_fraction"] == 1
    assert [part["sample_count"] for part in result["unsupported_reference_intervals"]] == [1, 5, 1]
    assert result["per_sample"]["timestamp_ns"] == [0, 1, 2, 3, 4, 10, 11, 12]


def test_initialization_interior_and_trailing_loss_stay_in_coverage_denominator():
    times = np.arange(9, dtype=np.int64) * 10_000_000
    positions = np.column_stack((np.arange(9), np.arange(9)**2, np.zeros(9)))
    kept = np.array([2, 3, 5, 6])
    reference = trajectory(positions, times)
    estimate = trajectory(positions[kept], times[kept])
    result = evaluate(estimate, reference)
    assert result["coverage_fraction"] == pytest.approx(4/9)
    assert result["evaluated_fraction"] == pytest.approx(4/9)
    assert result["initialization_delay_s"] == pytest.approx(.02)
    assert result["counts"]["missing_initial_camera_samples"] == 2
    assert result["counts"]["missing_interior_camera_samples"] == 1
    assert result["counts"]["missing_trailing_camera_samples"] == 2
    assert [part["sample_count"] for part in result["missing_estimate_intervals"]] == [2, 1, 2]
    assert result["metrics"]["ate_rmse_m"] < 1e-12


def test_time_error_is_not_optimized_away():
    seconds = np.linspace(0, 2, 101)
    times = np.arange(101, dtype=np.int64) * 20_000_000
    def motion(t):
        return np.column_stack((t, t**2, t**3))
    result = evaluate(trajectory(motion(seconds + .2), times), trajectory(motion(seconds), times))
    assert result["metrics"]["ate_rmse_m"] > .2
    assert result["protocol"]["time_offset_fitted"] is False


def test_timestamp_roundoff_tolerance_does_not_shift_the_evaluation_schedule():
    reference = trajectory()
    shifted = trajectory(times=reference.timestamp_ns + 1000)
    result = evaluate(shifted, reference)
    assert result["timing"]["maximum_estimate_association_error_ns"] == 1000
    assert result["per_sample"]["timestamp_ns"] == reference.timestamp_ns.tolist()
    assert result["metrics"]["ate_rmse_m"] < 1e-14
    with pytest.raises(ValueError, match="schedule"):
        evaluate(trajectory(times=reference.timestamp_ns + 1001), reference)
    with pytest.raises(ValueError, match="schedule"):
        evaluate(shifted, reference, max_estimate_timestamp_error_ns=0)


def test_timestamp_association_is_one_to_one_and_rejects_ties():
    reference = trajectory()
    duplicate_match = trajectory([[0, 0, 0], [0, 0, 0]], times=[0, 1])
    with pytest.raises(ValueError, match="multiple estimates"):
        evaluate(duplicate_match, reference)
    tie = trajectory([[0, 0, 0]], times=[5_000_000])
    with pytest.raises(ValueError, match="ambiguous"):
        evaluate(tie, reference, max_estimate_timestamp_error_ns=5_000_000)


@pytest.mark.parametrize("positions", [POINTS[:0], POINTS[:1], POINTS[:2]])
def test_no_or_insufficient_output_does_not_receive_a_zero_error_score(positions):
    result = evaluate(trajectory(positions), trajectory())
    assert result["status"] == "insufficient_associations"
    assert result["metrics"] is None
    assert result["alignment"] is None
    assert result["coverage_fraction"] == len(positions)/len(POINTS)
    if len(positions) == 0:
        assert result["initialization_delay_s"] is None
        assert result["counts"]["missing_initial_camera_samples"] == len(POINTS)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("positions", [np.zeros((5, 3)), np.column_stack((np.arange(5), np.zeros((5, 2))))])
def test_unobservable_rotation_does_not_produce_an_arbitrary_alignment(positions):
    result = evaluate(trajectory(positions), trajectory(positions))
    assert result["status"] == "degenerate_alignment"
    assert result["metrics"] is None
    assert result["coverage_fraction"] == 1


def test_empty_reference_has_explicit_zero_support():
    result = evaluate(trajectory(), trajectory(POINTS[:0]), expected_timestamps_ns=trajectory().timestamp_ns)
    assert result["status"] == "insufficient_associations"
    assert result["reference_coverage_fraction"] == 0
    assert result["evaluated_reference_fraction"] is None


@pytest.mark.parametrize("times", [[0., 1.], [0, True], [1, 1], [2, 1], [0, 2**63], [[1], [2]]])
def test_timestamps_reject_float_boolean_duplicate_unsorted_and_overflow(times):
    with pytest.raises(ValueError, match="timestamp"):
        trajectory(POINTS[:2], times=times)


@pytest.mark.parametrize("values", [
    [[np.nan, 0, 0]], [[np.inf, 0, 0]], [[0, 0]], [[True, False, False]],
])
def test_invalid_position_arrays_are_rejected(values):
    with pytest.raises(ValueError, match="positions"):
        Trajectory([0], values, [[0., 0., 0., 1.]])


@pytest.mark.parametrize("quaternion", [[0, 0, 0, 0], [0, 0, 0, 2], [0, 0, np.nan, 1], [0, 0, np.inf, 1]])
def test_invalid_quaternions_are_rejected(quaternion):
    with pytest.raises(ValueError, match="quaternion"):
        trajectory([[0, 0, 0]], quaternions=[quaternion])


def test_pose_validation_tolerates_only_small_quaternion_serialization_roundoff():
    value = trajectory([[0, 0, 0]], quaternions=[[0, 0, 0, 1 + 1e-7]])
    assert value.quaternion_xyzw[0, 3] == 1
    with pytest.raises(ValueError, match="unit"):
        trajectory([[0, 0, 0]], quaternions=[[0, 0, 0, 1 + 2e-6]])


def test_trajectory_copies_inputs_to_preserve_validation():
    positions = POINTS.copy()
    value = trajectory(positions)
    positions[0] = np.nan
    assert np.all(np.isfinite(value.position_m))
    with pytest.raises(ValueError):
        value.position_m[0, 0] = 9


@pytest.mark.parametrize("options", [
    {"max_reference_gap_ns": 0}, {"max_reference_gap_ns": 1.5},
    {"max_estimate_timestamp_error_ns": -1}, {"max_estimate_timestamp_error_ns": True},
    {"expected_timestamps_ns": []}, {"expected_timestamps_ns": [0., 1.]},
])
def test_invalid_protocol_options_are_rejected(options):
    with pytest.raises(ValueError):
        evaluate(trajectory(), trajectory(), **options)


def test_csv_entry_point_preserves_epoch_nanoseconds_and_json_contract(tmp_path):
    data = trajectory(times=1_500_000_000_000_000_000 + np.arange(5, dtype=np.int64))
    estimate = write_csv(tmp_path / "estimate.csv", data)
    reference = write_csv(tmp_path / "reference.csv", data)
    result = evaluate_trajectory(estimate, reference, expected_timestamps_ns=data.timestamp_ns)
    assert result["schema_version"] == 1
    assert result["protocol"]["version"] == 1
    assert result["status"] == "ok"
    assert result["timing"]["expected_last_timestamp_ns"] == 1_500_000_000_000_000_004
    assert result["metrics"]["ate_rmse_m"] < 1e-14
    assert result["counts"]["evaluated_camera_samples"] == 5
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("timestamp", ["1.0", "1e9", "nan", "1_000", "True", "9223372036854775808"])
def test_csv_refuses_non_integer_or_overflow_timestamp(tmp_path, timestamp):
    path = tmp_path / "bad.csv"
    path.write_text(",".join(TRAJECTORY_COLUMNS) + f"\n{timestamp},0,0,0,0,0,0,1\n")
    with pytest.raises(ValueError):
        read_trajectory(path)


@pytest.mark.parametrize("contents", [
    "timestamp_ns,px_m,py_m,pz_m,qw,qx,qy,qz\n0,0,0,0,1,0,0,0\n",
    ",".join(TRAJECTORY_COLUMNS) + "\n0,0,0,0,0,0,0,1,extra\n",
    ",".join(TRAJECTORY_COLUMNS) + "\n0,0,0,0,0,0,1\n",
    ",".join(TRAJECTORY_COLUMNS) + "\n\n",
    ",".join(TRAJECTORY_COLUMNS) + "\n0,nan,0,0,0,0,0,1\n",
])
def test_csv_refuses_wrong_schema_and_malformed_rows(tmp_path, contents):
    path = tmp_path / "bad.csv"
    path.write_text(contents)
    with pytest.raises(ValueError):
        read_trajectory(path)
