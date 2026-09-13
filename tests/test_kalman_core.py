"""Check angle/bias estimation against scalar algebra and batch inference."""

import numpy as np
import pytest

from meridian.kalman import AngleBiasKalman


def make_filter(**overrides):
    parameters = {
        "initial_angle_rad": 0.2,
        "initial_bias_rad_s": 0.1,
        "initial_angle_std_rad": 2.0,
        "initial_bias_std_rad_s": 0.5,
        "gyro_noise_std_rad_s": 0.3,
        "angle_noise_std_rad": 0.5,
    }
    parameters.update(overrides)
    return AngleBiasKalman(**parameters)


def test_prediction_and_correction_match_hand_calculated_example():
    estimator = make_filter()
    estimator.predict(rate_rad_s=1.1, dt_s=2.0)
    # Angle = 0.2 + 2*(1.1-0.1). P00 = 4 + 2²*0.25 + (0.3*2)².
    np.testing.assert_allclose(estimator.state, [2.2, 0.1], atol=1e-14)
    np.testing.assert_allclose(estimator.covariance, [[5.36, -0.5], [-0.5, 0.25]])

    innovation, variance = estimator.update(angle_rad=1.7)
    assert innovation == pytest.approx(-0.5)
    assert variance == pytest.approx(5.61)
    # Scalar conditional-Gaussian result, independently of the Joseph formula.
    np.testing.assert_allclose(estimator.state, [2.2 - 268.0 / 561.0, 0.1 + 25.0 / 561.0])
    np.testing.assert_allclose(estimator.covariance, [
        [134.0 / 561.0, -12.5 / 561.0],
        [-12.5 / 561.0, 0.25 - 25.0 / 561.0],
    ])
    # A lower angle observation corrects a positive gyro bias upwards.
    assert estimator.state[1] > 0.1


def test_sample_rate_noise_variance_scales_with_interval_squared():
    common = dict(initial_angle_std_rad=0.0, initial_bias_std_rad_s=0.0,
                  gyro_noise_std_rad_s=0.4)
    short = make_filter(**common)
    long = make_filter(**common)
    short.predict(0.0, 0.5)
    long.predict(0.0, 1.0)
    np.testing.assert_allclose(short.covariance, [[0.04, 0.0], [0.0, 0.0]])
    np.testing.assert_allclose(long.covariance, [[0.16, 0.0], [0.0, 0.0]])


def test_constant_bias_estimate_matches_independent_batch_posterior():
    estimator = make_filter(initial_angle_rad=0.0, initial_bias_rad_s=0.0,
                            initial_angle_std_rad=0.0, initial_bias_std_rad_s=1.0,
                            gyro_noise_std_rad_s=0.0, angle_noise_std_rad=0.1)
    times = np.cumsum([0.1, 0.3, 0.2, 0.4, 0.5, 0.7])
    bias = 0.3
    for dt in np.diff(np.r_[0.0, times]):
        estimator.predict(bias, dt)
        estimator.update(0.0)

    # Known initial angle and noiseless gyro reduce inference to scalar bias:
    # each observation constrains bias*t, with independent variance R = 0.01.
    information = np.sum(times**2) / 0.01
    bias_variance = 1.0 / (1.0 + information)
    expected_bias = bias * information / (1.0 + information)
    final_time = times[-1]
    np.testing.assert_allclose(estimator.state,
                               [(bias - expected_bias) * final_time, expected_bias],
                               atol=1e-14)
    np.testing.assert_allclose(estimator.covariance, bias_variance * np.array([
        [final_time**2, -final_time], [-final_time, 1.0]
    ]), atol=1e-14)


def test_zero_bias_uncertainty_prevents_bias_learning_without_random_walk():
    estimator = make_filter(initial_bias_std_rad_s=0.0)
    for _ in range(20):
        estimator.predict(1.0, 0.1)
        estimator.update(0.0)
    assert estimator.state[1] == 0.1
    np.testing.assert_array_equal(estimator.covariance[1], [0.0, 0.0])


def test_repeated_irregular_predictions_and_updates_keep_covariance_psd():
    estimator = make_filter(initial_angle_std_rad=0.0)
    for step in range(500):
        estimator.predict(0.3 * np.cos(step / 30.0), 0.01 + (step % 7) * 0.003)
        if step % 4 == 0:
            estimator.update(0.05 * np.sin(step / 20.0))
        covariance = estimator.covariance
        np.testing.assert_array_equal(covariance, covariance.T)
        assert np.linalg.eigvalsh(covariance).min() >= -1e-14
        assert np.all(np.isfinite(estimator.state))


def test_exposed_state_and_covariance_do_not_allow_external_mutation():
    estimator = make_filter()
    state = estimator.state
    covariance = estimator.covariance
    state[:] = -999.0
    covariance[:] = -999.0
    np.testing.assert_array_equal(estimator.state, [0.2, 0.1])
    np.testing.assert_array_equal(estimator.covariance, [[4.0, 0.0], [0.0, 0.25]])
    snapshot = estimator.state
    estimator.predict(1.1, 2.0)
    np.testing.assert_array_equal(snapshot, [0.2, 0.1])


@pytest.mark.parametrize("parameter", [
    "initial_angle_rad", "initial_bias_rad_s", "initial_angle_std_rad",
    "initial_bias_std_rad_s", "gyro_noise_std_rad_s", "angle_noise_std_rad",
])
@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_nonfinite_initialization_is_rejected(parameter, value):
    with pytest.raises(ValueError):
        make_filter(**{parameter: value})


@pytest.mark.parametrize("parameter", [
    "initial_angle_std_rad", "initial_bias_std_rad_s",
    "gyro_noise_std_rad_s", "angle_noise_std_rad",
])
@pytest.mark.parametrize("value", [-0.1, 1e308])
def test_negative_or_unrepresentable_variances_are_rejected(parameter, value):
    with pytest.raises(ValueError):
        make_filter(**{parameter: value})


@pytest.mark.parametrize("value", [0.0, 1e-300])
def test_angle_measurement_variance_must_be_positive(value):
    with pytest.raises(ValueError):
        make_filter(angle_noise_std_rad=value)


@pytest.mark.parametrize("rate,dt", [
    (np.nan, 0.1), (np.inf, 0.1), (0.0, 0.0), (0.0, -0.1),
    (0.0, np.nan), (0.0, np.inf), (1e308, 1e308),
])
def test_invalid_prediction_does_not_change_filter(rate, dt):
    estimator = make_filter()
    original_state, original_covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError):
        estimator.predict(rate, dt)
    np.testing.assert_array_equal(estimator.state, original_state)
    np.testing.assert_array_equal(estimator.covariance, original_covariance)


@pytest.mark.parametrize("angle", [np.nan, np.inf, -np.inf])
def test_invalid_observation_does_not_change_filter(angle):
    estimator = make_filter()
    original_state, original_covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError):
        estimator.update(angle)
    np.testing.assert_array_equal(estimator.state, original_state)
    np.testing.assert_array_equal(estimator.covariance, original_covariance)
