"""Check tilt signs, circular innovations, and complementary dynamics."""

import math

import numpy as np
import pytest

from meridian.tilt import ComplementaryRoll, accel_roll, wrap_angle


def test_known_specific_force_poses_follow_forward_right_down_signs():
    force = np.array([[0.0, -9.81], [-9.81, 0.0], [9.81, 0.0], [0.0, 9.81]])
    expected = np.deg2rad([0.0, 90.0, -90.0, -180.0])
    np.testing.assert_allclose(accel_roll(force), expected, atol=1e-14)
    assert accel_roll([-3.0, -3.0]) == pytest.approx(np.pi / 4.0)


def test_tilt_preserves_leading_dimensions_and_does_not_mutate_input():
    force = np.array([[[0.0, -4.0], [-3.0, 0.0]], [[2.0, 0.0], [0.0, 8.0]]])
    snapshot = force.copy()
    angles = accel_roll(force)
    assert angles.shape == (2, 2)
    np.testing.assert_allclose(angles, [[0.0, np.pi / 2.0], [-np.pi / 2.0, -np.pi]])
    np.testing.assert_array_equal(force, snapshot)


def test_wrapping_maps_both_half_turns_to_negative_pi():
    angles = np.deg2rad([[-540.0, -180.0, 180.0, 540.0], [-181.0, -179.0, 179.0, 181.0]])
    snapshot = angles.copy()
    expected = np.deg2rad([[-180.0] * 4, [179.0, -179.0, 179.0, -179.0]])
    np.testing.assert_allclose(wrap_angle(angles), expected, atol=1e-14)
    np.testing.assert_array_equal(angles, snapshot)


@pytest.mark.parametrize("angles", [np.nan, np.inf, [0.0, -np.inf]])
def test_wrapping_rejects_nonfinite_angles(angles):
    with pytest.raises(ValueError):
        wrap_angle(angles)


@pytest.mark.parametrize("force", [
    [], 1.0, [1.0], [1.0, 2.0, 3.0], [[], []],
    [[0.0, -9.81], [np.nan, -9.81]], [np.inf, 0.0],
    [0.0, 0.0], [0.0, -1e-9], [1e-12, -1e-12],
    [[0.0, -9.81], [0.0, 0.0]], [1.7e308, 1.7e308],
])
def test_tilt_rejects_invalid_or_numerically_degenerate_force(force):
    with pytest.raises(ValueError):
        accel_roll(force)


def test_numerical_threshold_is_not_a_gravity_magnitude_gate():
    assert accel_roll([0.0, -2e-9]) == 0.0
    assert accel_roll([0.0, -98.1]) == 0.0


def test_prediction_uses_each_irregular_interval_and_keeps_unwrapped_state():
    estimator = ComplementaryRoll(initial_angle_rad=3.0, tau_s=0.5)
    for rate, dt, expected in [(2.0, 0.125, 3.25), (-4.0, 0.5, 1.25), (1.0, 0.25, 1.5)]:
        estimator.predict(rate_rad_s=rate, dt_s=dt)
        assert estimator.angle_rad == pytest.approx(expected)


def test_correction_crosses_angle_boundary_along_shortest_innovation():
    estimator = ComplementaryRoll(initial_angle_rad=np.deg2rad(179.0), tau_s=1.0)
    estimator.update(observed_angle_rad=np.deg2rad(-179.0), elapsed_s=math.log(2.0))
    # Half of the +2 degree innovation, rather than half of -358 degrees.
    assert np.rad2deg(estimator.angle_rad) == pytest.approx(180.0)


def test_correction_preserves_the_predicted_revolution_count():
    estimator = ComplementaryRoll(initial_angle_rad=4.0 * np.pi + 0.2, tau_s=1.0)
    estimator.update(observed_angle_rad=0.4, elapsed_s=math.log(2.0))
    assert estimator.angle_rad == pytest.approx(4.0 * np.pi + 0.3)


def test_correction_uses_elapsed_observation_time_after_multiple_predictions():
    estimator = ComplementaryRoll(initial_angle_rad=0.0, tau_s=1.0)
    for _ in range(10):
        estimator.predict(rate_rad_s=1.0, dt_s=0.01)
    estimator.update(observed_angle_rad=0.0, elapsed_s=0.1)
    assert estimator.angle_rad == pytest.approx(0.1 * math.exp(-0.1))


def test_initial_error_decays_exponentially_over_irregular_observations():
    estimator = ComplementaryRoll(initial_angle_rad=0.4, tau_s=0.7)
    elapsed = 0.0
    for interval in [0.03, 0.12, 0.21, 0.08, 0.36]:
        estimator.predict(rate_rad_s=0.0, dt_s=interval)
        estimator.update(observed_angle_rad=0.0, elapsed_s=interval)
        elapsed += interval
        assert estimator.angle_rad == pytest.approx(0.4 * math.exp(-elapsed / 0.7))


def test_constant_bias_has_expected_nonzero_post_correction_steady_error():
    tau, period, bias = 0.8, 0.1, 0.01
    estimator = ComplementaryRoll(initial_angle_rad=0.0, tau_s=tau)
    for _ in range(400):
        estimator.predict(rate_rad_s=bias, dt_s=period)
        estimator.update(observed_angle_rad=0.0, elapsed_s=period)
    # e_next = alpha*(e + bias*period); solve its fixed point analytically.
    alpha = math.exp(-period / tau)
    expected_error = alpha * bias * period / (1.0 - alpha)
    assert estimator.angle_rad == pytest.approx(expected_error, abs=1e-13)
    assert estimator.angle_rad > 0.0


@pytest.mark.parametrize("angle,tau", [
    (np.nan, 1.0), (np.inf, 1.0), (0.0, 0.0), (0.0, -0.1),
    (0.0, np.nan), (0.0, np.inf),
])
def test_invalid_complementary_initialization_is_rejected(angle, tau):
    with pytest.raises(ValueError):
        ComplementaryRoll(initial_angle_rad=angle, tau_s=tau)


@pytest.mark.parametrize("rate,dt", [
    (np.nan, 0.1), (np.inf, 0.1), (0.0, 0.0), (0.0, -0.1),
    (0.0, np.nan), (0.0, np.inf), (1e308, 1e308),
])
def test_invalid_prediction_leaves_state_unchanged(rate, dt):
    estimator = ComplementaryRoll(initial_angle_rad=0.5, tau_s=1.0)
    with pytest.raises(ValueError):
        estimator.predict(rate_rad_s=rate, dt_s=dt)
    assert estimator.angle_rad == 0.5


@pytest.mark.parametrize("observation,elapsed", [
    (np.nan, 0.1), (np.inf, 0.1), (0.0, 0.0), (0.0, -0.1),
    (0.0, np.nan), (0.0, np.inf),
])
def test_invalid_correction_leaves_state_unchanged(observation, elapsed):
    estimator = ComplementaryRoll(initial_angle_rad=0.5, tau_s=1.0)
    with pytest.raises(ValueError):
        estimator.update(observed_angle_rad=observation, elapsed_s=elapsed)
    assert estimator.angle_rad == 0.5


def test_unrepresentable_innovation_leaves_state_unchanged():
    estimator = ComplementaryRoll(initial_angle_rad=-1e308, tau_s=1.0)
    with pytest.raises(ValueError, match="innovation"):
        estimator.update(observed_angle_rad=1e308, elapsed_s=0.1)
    assert estimator.angle_rad == -1e308
