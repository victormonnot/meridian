"""Verify vector snapshot replay against analytical and physical contracts."""

from dataclasses import fields
import math

import numpy as np
import pytest

from meridian.replay import REPLAY_GRAVITY_M_S2, ReplayConfig, replay_snapshots


def _force(angles, magnitude=REPLAY_GRAVITY_M_S2):
    angles = np.asarray(angles, dtype=np.float64)
    return np.column_stack((
        np.zeros(len(angles)),
        -magnitude * np.sin(angles),
        -magnitude * np.cos(angles),
    ))


@pytest.mark.parametrize("angle_std_deg", [0.1, 2.0, 7.5])
def test_component_noise_is_the_declared_local_angle_noise_mapping(angle_std_deg):
    config = ReplayConfig(angle_noise_std_deg=angle_std_deg)
    assert REPLAY_GRAVITY_M_S2 == 9.80665
    assert config.accel_noise_std_m_s2 == pytest.approx(
        9.80665 * math.radians(angle_std_deg), rel=1e-15,
    )


def test_finite_angle_variance_cannot_overflow_derived_component_variance():
    # Its angular variance is finite, but multiplication by g squared overflows.
    angle_std_deg = 1e155
    assert math.isfinite(math.radians(angle_std_deg)**2)
    with pytest.raises(ValueError):
        ReplayConfig(angle_noise_std_deg=angle_std_deg)


def test_rotated_first_correction_matches_tangent_radial_and_information_solutions():
    config = ReplayConfig(
        gyro_noise_std_deg_s=1.7,
        angle_noise_std_deg=3.0,
        initial_bias_std_deg_s=2.0,
    )
    dt, initial_angle, rate = 0.137003, math.radians(40.0), 0.41
    prior_angle = initial_angle + rate * dt
    force = _force([initial_angle, prior_angle + 0.16])
    force[1, 1:] *= (REPLAY_GRAVITY_M_S2 + 0.8) / REPLAY_GRAVITY_M_S2
    result = replay_snapshots(
        [2_000_000, 2_137_003], [[rate, 0.0, 0.0], [900.0, 0.0, 0.0]],
        force, config=config,
    )

    angle_variance = math.radians(3.0)**2
    bias_variance = math.radians(2.0)**2
    prior_covariance = np.array([
        [angle_variance + dt**2 * (bias_variance + math.radians(1.7)**2),
         -dt * bias_variance],
        [-dt * bias_variance, bias_variance],
    ])
    g = REPLAY_GRAVITY_M_S2
    tangent = np.array([-math.cos(prior_angle), math.sin(prior_angle)])
    radial = np.array([-math.sin(prior_angle), -math.cos(prior_angle)])
    innovation = force[1, 1:] - g * radial
    component_variance = (g * math.radians(3.0))**2
    tangent_variance = g**2 * prior_covariance[0, 0] + component_variance
    expected_s = (
        tangent_variance * np.outer(tangent, tangent)
        + component_variance * np.outer(radial, radial)
    )
    # A scalar tangent observation gives an independent joint-update solution.
    expected_state = np.array([prior_angle, 0.0]) + (
        g * prior_covariance[:, 0] * (tangent @ innovation) / tangent_variance
    )
    # Information addition is independent of the core's Joseph-form update.
    expected_covariance = np.linalg.inv(
        np.linalg.inv(prior_covariance)
        + np.diag([g**2 / component_variance, 0.0])
    )
    expected_nis = (
        (tangent @ innovation)**2 / tangent_variance
        + (radial @ innovation)**2 / component_variance
    )

    np.testing.assert_allclose(result.ekf_innovation_m_s2[0], innovation, atol=2e-15)
    np.testing.assert_allclose(result.ekf_innovation_covariance_m2_s4[0], expected_s, atol=2e-15)
    np.testing.assert_allclose(result.ekf_state[1], expected_state, atol=2e-15)
    np.testing.assert_allclose(result.ekf_covariance[1], expected_covariance, atol=2e-18)
    assert result.ekf_nis[0] == pytest.approx(expected_nis, rel=2e-14)
    assert abs(expected_s[0, 1]) > 0.01
    diagonal_only_nis = np.sum(innovation**2 / np.diag(expected_s))
    assert abs(diagonal_only_nis - expected_nis) > 0.01


def test_initial_tilt_sets_shared_state_and_covariance_without_a_vector_correction():
    config = ReplayConfig(angle_noise_std_deg=3.0, initial_bias_std_deg_s=2.0)
    force = _force([0.4, 0.4, 0.4])
    force[0, 1:] *= 1.4
    result = replay_snapshots([0, 20_000, 70_000], np.zeros((3, 3)), force, config=config)
    np.testing.assert_array_equal(result.ekf_state[0], result.kalman_state[0])
    assert result.ekf_state[0, 0] == pytest.approx(0.4)
    assert result.ekf_state[0, 1] == 0.0
    np.testing.assert_array_equal(
        result.ekf_covariance[0], np.diag([math.radians(3.0)**2, math.radians(2.0)**2]),
    )
    assert result.ekf_state.shape == (3, 2)
    assert result.ekf_covariance.shape == (3, 2, 2)
    assert result.ekf_innovation_m_s2.shape == (2, 2)
    assert result.ekf_innovation_covariance_m2_s4.shape == (2, 2, 2)
    assert result.ekf_nis.shape == (2,)
    # The radial discrepancy on row zero does not appear as an extra correction.
    np.testing.assert_allclose(result.ekf_innovation_m_s2, 0.0, atol=2e-15)
    np.testing.assert_allclose(result.ekf_nis, 0.0, atol=1e-26)


def test_radial_force_mismatch_is_not_discarded_by_normalizing_equal_tilts():
    config = ReplayConfig()
    force = _force([0.7, 0.7])
    matched = replay_snapshots([0, 40_000], np.zeros((2, 3)), force, config=config)
    radial_excess = 1.5
    force[1, 1:] *= (REPLAY_GRAVITY_M_S2 + radial_excess) / REPLAY_GRAVITY_M_S2
    mismatched = replay_snapshots([0, 40_000], np.zeros((2, 3)), force, config=config)
    np.testing.assert_allclose(mismatched.accel_roll_rad, matched.accel_roll_rad, atol=1e-15)
    np.testing.assert_allclose(mismatched.ekf_state, matched.ekf_state, atol=1e-15)
    np.testing.assert_allclose(mismatched.ekf_covariance, matched.ekf_covariance, atol=1e-18)
    np.testing.assert_allclose(mismatched.innovation_rad, 0.0, atol=1e-15)
    radial = np.array([-math.sin(0.7), -math.cos(0.7)])
    np.testing.assert_allclose(mismatched.ekf_innovation_m_s2[0], radial_excess * radial, atol=2e-15)
    assert mismatched.ekf_nis[0] == pytest.approx(
        (radial_excess / config.accel_noise_std_m_s2)**2, rel=2e-14,
    )
    assert matched.ekf_nis[0] < 1e-26


def test_matched_isotropic_tuning_gives_kalman_covariance_even_with_irregular_mismatch():
    rng = np.random.default_rng(71)
    times = np.r_[0, np.cumsum(rng.integers(7_000, 190_000, 199))]
    gyro = rng.normal(0.0, 0.2, size=(len(times), 3))
    force = _force(0.5 + 0.2 * np.sin(times / 1e6))
    force[25:95, 1] += 3.0
    force[115:160, 1:] *= 1.3
    result = replay_snapshots(times, gyro, force)
    # With |dh/dtheta| = g, the angle information is g² / sigma_accel².
    np.testing.assert_allclose(result.ekf_covariance, result.kalman_covariance, rtol=2e-13, atol=1e-18)
    np.testing.assert_allclose(result.ekf_covariance, result.ekf_covariance.transpose(0, 2, 1), atol=1e-18)
    assert np.min(np.linalg.eigvalsh(result.ekf_covariance)) >= -1e-18
    assert np.min(np.linalg.eigvalsh(result.ekf_innovation_covariance_m2_s4)) > 0.0
    assert np.all(np.isfinite(result.ekf_nis)) and np.all(result.ekf_nis >= 0.0)
    assert np.max(np.abs(result.ekf_state[:, 0] - result.kalman_state[:, 0])) > 1e-3


def test_static_pose_recovers_known_gyro_bias_from_measurements_only():
    count, angle, bias = 1501, -0.45, math.radians(0.8)
    times = np.arange(count, dtype=np.int64) * 40_000
    gyro = np.tile([bias, 0.0, 0.0], (count, 1))
    result = replay_snapshots(
        times, gyro, _force(np.full(count, angle)),
        config=ReplayConfig(gyro_noise_std_deg_s=0.0),
    )
    assert result.gyro_roll_rad[-1] - angle == pytest.approx(bias * 60.0)
    assert result.ekf_state[-1, 0] == pytest.approx(angle, abs=1e-5)
    assert result.ekf_state[-1, 1] == pytest.approx(bias, abs=1e-6)


def test_one_axis_replay_ignores_finite_off_axis_measurements():
    times = np.array([0, 10_000, 45_000, 170_000])
    gyro = np.tile([0.17, 0.0, 0.0], (4, 1))
    force = _force([0.2, 0.22, 0.23, 0.19])
    reference = replay_snapshots(times, gyro, force)
    gyro[:, 1:] = [[20.0, -40.0], [70.0, 90.0], [4.0, -9.0], [3.0, 12.0]]
    force[:, 0] = [50.0, -20.0, 45.0, -15.0]
    changed = replay_snapshots(times, gyro, force)
    for field in fields(reference):
        np.testing.assert_array_equal(getattr(changed, field.name), getattr(reference, field.name))


def test_ekf_timing_is_invariant_to_a_large_integer_timestamp_origin():
    times = np.array([0, 1, 5, 100_003], dtype=np.int64)
    gyro = np.array([[0.4, 0.0, 0.0], [-0.1, 0.0, 0.0], [0.7, 0.0, 0.0], [99.0, 0.0, 0.0]])
    force = _force([0.3, 0.31, 0.28, 0.37])
    reference = replay_snapshots(times, gyro, force)
    shifted = replay_snapshots(times + (np.iinfo(np.int64).max - times[-1]), gyro, force)
    for field in fields(reference):
        if field.name != "time_us":
            np.testing.assert_array_equal(getattr(shifted, field.name), getattr(reference, field.name))
