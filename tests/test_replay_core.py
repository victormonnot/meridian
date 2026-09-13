"""Verify snapshot timing, causal integration, initialization, and corrections."""

from dataclasses import fields
import math

import numpy as np
import pytest

from meridian.replay import ReplayConfig, replay_snapshots


def _force(angle_rad):
    angles = np.asarray(angle_rad, dtype=np.float64)
    return np.column_stack((
        np.zeros(len(angles)), -9.80665 * np.sin(angles), -9.80665 * np.cos(angles),
    ))


def _stationary(count=3, bias=0.0, angle=0.0):
    times = np.arange(count, dtype=np.int64) * 40_000
    gyro = np.zeros((count, 3))
    gyro[:, 0] = bias
    return times, gyro, _force(np.full(count, angle))


def test_irregular_snapshot_hold_has_the_analytical_rectangular_quadrature_error():
    times = np.array([0, 50_000, 180_000, 250_000, 420_000])
    seconds = times / 1e6
    acceleration, initial_rate, initial_angle = 0.3, 0.2, 0.4
    truth = initial_angle + initial_rate * seconds + 0.5 * acceleration * seconds**2
    gyro = np.zeros((len(times), 3))
    gyro[:, 0] = initial_rate + acceleration * seconds
    result = replay_snapshots(times, gyro, _force(truth))
    # Left rectangular quadrature misses half the rate change over each interval.
    expected_error = np.r_[0.0, -0.5 * acceleration * np.cumsum(np.diff(seconds)**2)]
    np.testing.assert_allclose(result.gyro_roll_rad - truth, expected_error, atol=1e-15)
    assert result.gyro_roll_rad[-1] != pytest.approx(truth[-1], abs=1e-5)


def test_previous_snapshot_is_held_and_final_gyro_cannot_change_any_estimate():
    times, gyro, accel = _stationary(4)
    gyro[:, 0] = [1.0, -2.0, 3.0, 900.0]
    first = replay_snapshots(times, gyro, accel)
    np.testing.assert_allclose(first.gyro_roll_rad, [0.0, 0.04, -0.04, 0.08], atol=1e-15)
    gyro[-1] = [-400.0, 200.0, 700.0]
    second = replay_snapshots(times, gyro, accel)
    for field in fields(first):
        np.testing.assert_array_equal(getattr(first, field.name), getattr(second, field.name))


def test_non_roll_gyro_axes_are_not_used_for_one_axis_propagation():
    times, gyro, accel = _stationary(4, bias=0.03)
    first = replay_snapshots(times, gyro, accel)
    gyro[:, 1:] = [[1.0, 9.0], [2.0, 8.0], [3.0, 7.0], [4.0, 6.0]]
    second = replay_snapshots(times, gyro, accel)
    np.testing.assert_array_equal(first.kalman_state, second.kalman_state)
    np.testing.assert_array_equal(first.gyro_roll_rad, second.gyro_roll_rad)


def test_noiseless_static_pose_recovers_constant_bias_without_truth_input():
    bias = math.radians(0.5)
    times, gyro, accel = _stationary(751, bias=bias, angle=0.35)
    result = replay_snapshots(times, gyro, accel, config=ReplayConfig(gyro_noise_std_deg_s=0.0))
    assert result.gyro_roll_rad[-1] == pytest.approx(0.35 + bias * 30.0)
    assert result.kalman_state[-1, 1] == pytest.approx(bias, abs=1e-6)
    assert result.kalman_state[-1, 0] == pytest.approx(0.35, abs=1e-5)
    # The complementary filter does not estimate the bias; its steady error remains.
    alpha = math.exp(-0.04)
    expected_error = alpha * bias * 0.04 / (1.0 - alpha)
    assert result.complementary_roll_rad[-1] - 0.35 == pytest.approx(expected_error, abs=1e-12)


def test_replay_prefix_is_unchanged_by_later_measurements():
    times, gyro, accel = _stationary(70, bias=0.015)
    prefix = replay_snapshots(times[:31], gyro[:31], accel[:31])
    gyro[31:, 0] = -3.0
    accel[31:] = _force(np.full(39, -0.7))
    full = replay_snapshots(times, gyro, accel)
    for field in fields(prefix):
        value = getattr(prefix, field.name)
        np.testing.assert_array_equal(value, getattr(full, field.name)[:len(value)])


def test_shortest_corrections_cross_pi_without_losing_the_unwrapped_branch():
    times, gyro, _ = _stationary(2)
    angle = np.deg2rad([179.0, -179.0])
    result = replay_snapshots(times, gyro, _force(angle))
    assert np.rad2deg(result.innovation_rad[0]) == pytest.approx(2.0)
    assert 179.0 < np.rad2deg(result.kalman_state[-1, 0]) < 181.0
    expected_complementary = 179.0 + 2.0 * (1.0 - math.exp(-0.04))
    assert np.rad2deg(result.complementary_roll_rad[-1]) == pytest.approx(expected_complementary)


def test_first_tilt_initializes_state_and_uncertainty_without_duplicate_correction():
    times, gyro, accel = _stationary(2, angle=0.4)
    config = ReplayConfig(angle_noise_std_deg=3.0, initial_bias_std_deg_s=2.0)
    result = replay_snapshots(times, gyro, accel, config=config)
    for angle in (result.gyro_roll_rad[0], result.complementary_roll_rad[0], result.kalman_state[0, 0]):
        assert angle == pytest.approx(0.4)
    assert result.kalman_state[0, 1] == 0.0
    np.testing.assert_array_equal(
        result.kalman_covariance[0], np.diag([math.radians(3.0)**2, math.radians(2.0)**2]),
    )
    assert result.innovation_rad.shape == (1,)
    assert result.innovation_variance_rad2.shape == (1,)


def test_covariances_remain_symmetric_positive_semidefinite_over_irregular_times():
    rng = np.random.default_rng(62)
    times = np.r_[0, np.cumsum(rng.integers(10_000, 180_000, size=199))]
    gyro = rng.normal(0.0, 0.02, size=(200, 3))
    force = _force(rng.normal(0.0, 0.01, size=200))
    result = replay_snapshots(times, gyro, force)
    np.testing.assert_allclose(result.kalman_covariance, result.kalman_covariance.transpose(0, 2, 1), atol=1e-18)
    assert np.min(np.linalg.eigvalsh(result.kalman_covariance)) >= -1e-18
    assert np.all(result.innovation_variance_rad2 > 0.0)


def test_subtraction_precedes_float_conversion_at_large_absolute_timestamps():
    times = np.array([np.iinfo(np.int64).max - 2, np.iinfo(np.int64).max - 1, np.iinfo(np.int64).max])
    gyro = np.tile([2.0, 0.0, 0.0], (3, 1))
    result = replay_snapshots(times, gyro, _force(np.zeros(3)))
    np.testing.assert_array_equal(result.elapsed_s, [0.0, 1e-6, 2e-6])
    np.testing.assert_array_equal(result.gyro_roll_rad, [0.0, 2e-6, 4e-6])
    np.testing.assert_array_equal(result.time_us, times)


@pytest.mark.parametrize("times", [
    [], [0], [[0, 1]], [0.0, 40_000.0], [0, True], np.array([False, True]),
    [0, "40000"], [0, None], [-1, 40_000], [0, 2**63],
    np.array([0, 2**63], dtype=np.uint64), [0, 0], [40_000, 0], [0, np.nan],
])
def test_invalid_timestamps_are_rejected_without_coercion_or_reordering(times):
    with pytest.raises(ValueError, match="time_us"):
        replay_snapshots(times, np.zeros((2, 3)), _force(np.zeros(2)))


def test_gap_at_threshold_is_allowed_and_one_microsecond_above_is_rejected():
    gyro, force = np.zeros((2, 3)), _force(np.zeros(2))
    result = replay_snapshots([0, 200_000], gyro, force)
    assert result.elapsed_s[-1] == 0.2
    with pytest.raises(ValueError, match="gap"):
        replay_snapshots([0, 200_001], gyro, force)


@pytest.mark.parametrize("name,value", [
    ("gyro_noise_std_deg_s", -1.0), ("gyro_noise_std_deg_s", np.nan),
    ("gyro_noise_std_deg_s", np.inf), ("gyro_noise_std_deg_s", 1e308),
    ("initial_bias_std_deg_s", -1.0), ("initial_bias_std_deg_s", np.inf),
    ("angle_noise_std_deg", 0.0), ("angle_noise_std_deg", -0.1),
    ("angle_noise_std_deg", 1e-300), ("angle_noise_std_deg", np.inf),
    ("complementary_tau_s", 0.0), ("complementary_tau_s", -1.0),
    ("complementary_tau_s", np.nan), ("max_gap_s", 0.0),
    ("max_gap_s", -0.2), ("max_gap_s", np.inf),
])
def test_invalid_configuration_is_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        ReplayConfig(**{name: value})


@pytest.mark.parametrize("sensor", ["gyro", "accel"])
@pytest.mark.parametrize("values", [np.zeros((2, 3)), np.zeros((3, 2)), [0.0, 0.0, 0.0],
                                    [[0.0, 0.0, -9.81], [np.nan, 0.0, -9.81], [0.0, 0.0, -9.81]],
                                    [[0.0, 0.0, -9.81], [0.0, np.inf, -9.81], [0.0, 0.0, -9.81]]])
def test_sensor_shape_and_all_axis_finiteness_are_required(sensor, values):
    times, gyro, accel = _stationary()
    with pytest.raises(ValueError, match="gyro_rad_s|accel_m_s2"):
        replay_snapshots(times, values if sensor == "gyro" else gyro, values if sensor == "accel" else accel)


def test_degenerate_accelerometer_tilt_rejects_entire_segment():
    times, gyro, accel = _stationary()
    accel[1] = [9.81, 0.0, 0.0]
    with pytest.raises(ValueError, match="norm"):
        replay_snapshots(times, gyro, accel)


def test_replay_preserves_inputs_and_outputs_do_not_share_their_storage():
    inputs = _stationary(5, bias=0.1, angle=0.3)
    before = [value.copy() for value in inputs]
    result = replay_snapshots(*inputs)
    for original, snapshot in zip(inputs, before):
        np.testing.assert_array_equal(original, snapshot)
        for field in fields(result):
            assert not np.shares_memory(original, getattr(result, field.name))
    inputs[0][0] = 8
    inputs[1][:] = 12.0
    inputs[2][:] = 1.0
    assert result.time_us[0] == 0
    assert result.accel_roll_rad[0] == pytest.approx(0.3)


def test_overflow_in_gyro_propagation_is_rejected():
    times, gyro, accel = _stationary(2)
    gyro[0, 0] = 1e308
    with pytest.raises(ValueError, match="finite numerical range"):
        replay_snapshots([0, 20_000_000], gyro, accel, config=ReplayConfig(max_gap_s=20.0))
