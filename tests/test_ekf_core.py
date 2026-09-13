"""Check gravity geometry, local Gaussian inference, and EKF limitations."""

import numpy as np
import pytest

from meridian.ekf import AngleBiasEKF, gravity_jacobian, gravity_observation
from meridian.kalman import AngleBiasKalman


def make_filter(**overrides):
    parameters = {
        "initial_angle_rad": 0.2,
        "initial_bias_rad_s": 0.1,
        "initial_angle_std_rad": 0.4,
        "initial_bias_std_rad_s": 0.5,
        "gyro_noise_std_rad_s": 0.3,
        "accel_noise_std_m_s2": 0.2,
    }
    parameters.update(overrides)
    return AngleBiasEKF(**parameters)


@pytest.mark.parametrize("angle, expected_force, expected_derivative", [
    (0.0, [0.0, -10.0], [-10.0, 0.0]),
    (np.pi / 2.0, [-10.0, 0.0], [0.0, 10.0]),
    (-np.pi / 2.0, [10.0, 0.0], [0.0, -10.0]),
    (np.pi, [0.0, 10.0], [10.0, 0.0]),
])
def test_static_poses_fix_body_frame_signs(angle, expected_force, expected_derivative):
    np.testing.assert_allclose(gravity_observation(angle, 10.0), expected_force, atol=2e-15)
    jacobian = gravity_jacobian(angle, 10.0)
    np.testing.assert_allclose(jacobian[:, 0], expected_derivative, atol=2e-15)
    np.testing.assert_array_equal(jacobian[:, 1], [0.0, 0.0])


@pytest.mark.parametrize("angle", [-7.2, -np.pi, -0.8, 0.0, 0.4, np.pi, 14.0])
@pytest.mark.parametrize("step", [1e-4, 1e-6])
def test_jacobian_matches_centered_differences_in_both_state_columns(angle, step):
    state = np.array([angle, 0.13])
    numerical = np.empty((2, 2))
    for column in range(2):
        offset = np.zeros(2)
        offset[column] = step
        numerical[:, column] = (
            gravity_observation((state + offset)[0])
            - gravity_observation((state - offset)[0])
        ) / (2.0 * step)
    np.testing.assert_allclose(gravity_jacobian(angle), numerical, rtol=3e-9, atol=1e-8)


def test_prediction_matches_linear_filter_for_irregular_intervals():
    common = dict(initial_angle_rad=0.2, initial_bias_rad_s=0.1,
                  initial_angle_std_rad=0.4, initial_bias_std_rad_s=0.5,
                  gyro_noise_std_rad_s=0.3)
    ekf = AngleBiasEKF(**common, accel_noise_std_m_s2=0.2)
    linear = AngleBiasKalman(**common, angle_noise_std_rad=0.2)
    for rate, dt in [(0.4, 0.01), (-0.2, 0.5), (1.1, 2.0), (0.0, 0.037)]:
        ekf.predict(rate, dt)
        linear.predict(rate, dt)
        np.testing.assert_array_equal(ekf.state, linear.state)
        np.testing.assert_array_equal(ekf.covariance, linear.covariance)


def test_joint_update_matches_independent_whitened_least_squares():
    estimator = make_filter(gravity_m_s2=10.0)
    estimator.predict(0.7, 0.3)
    prior_state, prior_covariance = estimator.state, estimator.covariance
    theta, _ = prior_state
    # Build a linear Gaussian inference problem around the frozen prior.
    # The least-squares unknown is the state increment, not the posterior state.
    observation = np.array([-2.9, -9.3])
    expected = np.array([-10.0 * np.sin(theta), -10.0 * np.cos(theta)])
    observation_matrix = np.array([[-10.0 * np.cos(theta), 0.0],
                                   [10.0 * np.sin(theta), 0.0]])
    whiten_prior = np.linalg.solve(np.linalg.cholesky(prior_covariance), np.eye(2))
    design = np.vstack([whiten_prior, observation_matrix / 0.2])
    target = np.r_[np.zeros(2), (observation - expected) / 0.2]
    expected_increment = np.linalg.lstsq(design, target, rcond=None)[0]
    expected_covariance = np.linalg.solve(design.T @ design, np.eye(2))

    innovation, innovation_covariance = estimator.update(observation)
    np.testing.assert_allclose(estimator.state, prior_state + expected_increment, atol=1e-13)
    np.testing.assert_allclose(estimator.covariance, expected_covariance, atol=1e-13)
    np.testing.assert_allclose(innovation, observation - expected, atol=1e-14)
    np.testing.assert_allclose(innovation_covariance,
                               observation_matrix @ prior_covariance @ observation_matrix.T
                               + np.eye(2) * 0.04, atol=1e-14)


def test_vector_correction_matches_scalar_tangent_projection():
    estimator = make_filter()
    estimator.predict(0.7, 0.3)
    prior_state, prior_covariance = estimator.state, estimator.covariance
    observed_angle, observed_norm = -0.3, 12.5
    observation = observed_norm * np.array([-np.sin(observed_angle), -np.cos(observed_angle)])
    # Isotropic component noise makes the tangential residual equivalent to
    # a scalar angular innovation scaled by the measured force magnitude.
    effective_innovation = (observed_norm / 9.80665) * np.sin(observed_angle - prior_state[0])
    angular_variance = (0.2 / 9.80665)**2
    scalar_gain = prior_covariance[:, 0] / (prior_covariance[0, 0] + angular_variance)
    expected_state = prior_state + scalar_gain * effective_innovation
    expected_covariance = prior_covariance - np.outer(
        prior_covariance[:, 0], prior_covariance[0]
    ) / (prior_covariance[0, 0] + angular_variance)
    estimator.update(observation)
    np.testing.assert_allclose(estimator.state, expected_state, atol=1e-13)
    np.testing.assert_allclose(estimator.covariance, expected_covariance, atol=1e-13)


def test_isotropic_vector_covariance_matches_angle_filter_despite_different_states():
    common = dict(initial_angle_rad=0.2, initial_bias_rad_s=0.1,
                  initial_angle_std_rad=0.4, initial_bias_std_rad_s=0.5,
                  gyro_noise_std_rad_s=0.3)
    ekf = AngleBiasEKF(**common, accel_noise_std_m_s2=0.2)
    linear = AngleBiasKalman(**common, angle_noise_std_rad=0.2 / 9.80665)
    for step in range(40):
        dt = 0.01 + (step % 7) * 0.003
        ekf.predict(0.2, dt)
        linear.predict(0.2, dt)
        # Large innovations and varying magnitudes make the state paths differ,
        # but H's fixed norm and isotropic R leave the same covariance recursion.
        angle = 1.5 * np.sin(step / 3.0)
        force = (1.0 + step / 20.0) * gravity_observation(angle)
        ekf.update(force)
        linear.update_wrapped_angle(angle)
        np.testing.assert_allclose(ekf.covariance, linear.covariance, atol=1e-13, rtol=1e-12)
    assert not np.allclose(ekf.state, linear.state)


def test_bias_correction_requires_prediction_cross_covariance():
    estimator = make_filter(initial_angle_rad=0.0, initial_bias_rad_s=0.0,
                            initial_angle_std_rad=0.1)
    estimator.update([0.0, -9.80665])
    assert estimator.state[1] == 0.0
    assert estimator.covariance[1, 1] == 0.25
    estimator.predict(0.2, 0.1)
    assert estimator.covariance[0, 1] < 0.0
    estimator.update([0.0, -9.80665])
    assert 0.0 < estimator.state[1] < 0.2
    assert estimator.covariance[1, 1] < 0.25


def test_zero_bias_uncertainty_prevents_learning_without_random_walk():
    estimator = make_filter(initial_bias_std_rad_s=0.0)
    for _ in range(20):
        estimator.predict(0.2, 0.1)
        estimator.update([0.0, -9.80665])
    assert estimator.state[1] == 0.1
    np.testing.assert_array_equal(estimator.covariance[1], [0.0, 0.0])


@pytest.mark.parametrize("vertical_force", [0.0, -15.0, 9.80665])
def test_radial_innovation_can_shrink_uncertainty_without_correcting_state(vertical_force):
    # Includes zero force and an exactly upside-down observation. At level,
    # the local derivative has no z component; these errors cannot rotate it.
    estimator = make_filter(initial_angle_rad=0.0)
    state, covariance = estimator.state, estimator.covariance
    innovation, _ = estimator.update([0.0, vertical_force])
    assert abs(innovation[1]) > 1.0
    np.testing.assert_array_equal(estimator.state, state)
    assert estimator.covariance[0, 0] < covariance[0, 0]


@pytest.mark.parametrize("turns", [-3, 0, 4])
def test_vector_correction_crosses_pi_without_wrapping_the_state(turns):
    initial_angle = turns * 2.0 * np.pi + np.deg2rad(179.0)
    estimator = make_filter(initial_angle_rad=initial_angle, initial_bias_rad_s=0.0)
    # Same physical pose as +181 degrees; the state preserves accumulated turns.
    estimator.update(gravity_observation(np.deg2rad(-179.0)))
    assert np.deg2rad(1.9) < estimator.state[0] - initial_angle < np.deg2rad(2.0)
    assert estimator.state[0] > turns * 2.0 * np.pi + np.pi


def test_repeated_predictions_and_updates_keep_covariance_symmetric_and_psd():
    estimator = make_filter(initial_angle_rad=0.0, initial_angle_std_rad=0.0)
    for step in range(500):
        estimator.predict(0.3 * np.cos(step / 30.0), 0.01 + (step % 7) * 0.003)
        if step % 4 == 0:
            estimator.update(gravity_observation(0.05 * np.sin(step / 20.0)))
        covariance = estimator.covariance
        np.testing.assert_array_equal(covariance, covariance.T)
        assert np.linalg.eigvalsh(covariance).min() >= -1e-14
        assert np.all(np.isfinite(estimator.state))


def test_properties_diagnostics_and_observation_do_not_allow_storage_mutation():
    estimator = make_filter()
    observation = np.array([-0.3, -9.8])
    original_observation = observation.copy()
    innovation, innovation_covariance = estimator.update(observation)
    state, covariance = estimator.state, estimator.covariance
    estimator.state[:] = -999.0
    estimator.covariance[:] = -999.0
    innovation[:] = -999.0
    innovation_covariance[:] = -999.0
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)
    np.testing.assert_array_equal(observation, original_observation)
    estimator.predict(0.2, 0.01)
    assert not np.array_equal(estimator.state, state)


@pytest.mark.parametrize("function", [gravity_observation, gravity_jacobian])
@pytest.mark.parametrize("angle, gravity", [
    (np.nan, 9.8), (np.inf, 9.8), (-np.inf, 9.8), (0.0, np.nan),
    (0.0, np.inf), (0.0, 0.0), (0.0, -9.8),
])
def test_model_helpers_reject_invalid_arguments(function, angle, gravity):
    with pytest.raises(ValueError):
        function(angle, gravity)


@pytest.mark.parametrize("parameter, value", [
    ("initial_angle_rad", np.nan), ("initial_bias_rad_s", np.inf),
    ("initial_angle_std_rad", -0.1), ("initial_angle_std_rad", 1e308),
    ("initial_bias_std_rad_s", np.nan), ("initial_bias_std_rad_s", -0.1),
    ("gyro_noise_std_rad_s", np.inf), ("gyro_noise_std_rad_s", -0.1),
    ("accel_noise_std_m_s2", np.nan), ("accel_noise_std_m_s2", -0.1),
    ("accel_noise_std_m_s2", 0.0), ("accel_noise_std_m_s2", 1e-300),
    ("accel_noise_std_m_s2", 1e308), ("gravity_m_s2", np.inf),
    ("gravity_m_s2", 0.0), ("gravity_m_s2", -9.8),
])
def test_invalid_initialization_is_rejected(parameter, value):
    with pytest.raises(ValueError):
        make_filter(**{parameter: value})


@pytest.mark.parametrize("rate, dt", [
    (np.nan, 0.1), (np.inf, 0.1), (0.0, 0.0), (0.0, -0.1),
    (0.0, np.nan), (0.0, np.inf), (1e308, 1e308),
])
def test_invalid_prediction_does_not_change_filter(rate, dt):
    estimator = make_filter()
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError):
        estimator.predict(rate, dt)
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)


@pytest.mark.parametrize("observation", [
    [], [1.0], [1.0, 2.0, 3.0], [[1.0, 2.0]], [[1.0], [2.0]],
    0.0, [np.nan, 0.0], [0.0, np.inf], [-np.inf, 0.0],
])
def test_invalid_observation_does_not_change_filter(observation):
    estimator = make_filter()
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError):
        estimator.update(observation)
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)


def test_unrepresentable_innovation_does_not_change_filter():
    estimator = make_filter(initial_angle_rad=0.0, gravity_m_s2=1e308)
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="finite"):
        estimator.update([0.0, 1e308])
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)


def test_unrepresentable_correction_does_not_change_filter():
    estimator = make_filter(initial_angle_rad=0.0, gravity_m_s2=1e-100,
                            accel_noise_std_m_s2=1e-110)
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="correction.*finite"):
        estimator.update([1e300, 0.0])
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)


def test_numerically_singular_innovation_covariance_does_not_change_filter():
    # Finite R can be lost when added to a much larger rank-one H P H.T.
    estimator = make_filter(initial_angle_rad=np.pi / 4.0, gravity_m_s2=1.0,
                            initial_angle_std_rad=1.0, accel_noise_std_m_s2=1e-100)
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="singular"):
        estimator.update([0.0, -9.80665])
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)
