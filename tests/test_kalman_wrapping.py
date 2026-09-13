"""Verify angular branch selection without changing the linear correction."""

import numpy as np
import pytest

from meridian.kalman import AngleBiasKalman


def make_filter(**overrides):
    parameters = {
        "initial_angle_rad": np.deg2rad(179.0),
        "initial_bias_rad_s": 0.0,
        "initial_angle_std_rad": 1.0,
        "initial_bias_std_rad_s": 1.0,
        "gyro_noise_std_rad_s": 0.0,
        "angle_noise_std_rad": 1.0,
    }
    parameters.update(overrides)
    return AngleBiasKalman(**parameters)


@pytest.mark.parametrize("predicted_deg, observed_deg, expected_deg", [
    (179.0, -179.0, 2.0),
    (-179.0, 179.0, -2.0),
])
def test_boundary_crossing_selects_short_signed_innovation(
    predicted_deg, observed_deg, expected_deg
):
    estimator = make_filter(initial_angle_rad=np.deg2rad(predicted_deg))
    innovation, variance = estimator.update_wrapped_angle(np.deg2rad(observed_deg))
    assert np.rad2deg(innovation) == pytest.approx(expected_deg)
    assert variance == pytest.approx(2.0)
    assert np.rad2deg(estimator.state[0]) == pytest.approx(
        predicted_deg + 0.5 * expected_deg
    )


def test_wrapped_update_matches_independent_scalar_posterior_with_bias_correction():
    estimator = make_filter(initial_angle_rad=np.pi - 0.02,
                            initial_bias_rad_s=0.1,
                            initial_angle_std_rad=2.0,
                            initial_bias_std_rad_s=0.5,
                            gyro_noise_std_rad_s=0.3,
                            angle_noise_std_rad=0.5)
    estimator.predict(rate_rad_s=0.1, dt_s=2.0)
    # Prediction retains the angle. P00 = 4 + 2²*0.25 + (0.3*2)² = 5.36,
    # P01 = -2*0.25 = -0.5, and the closest observation is pi + 0.03.
    innovation, variance = estimator.update_wrapped_angle(-np.pi + 0.03)
    assert innovation == pytest.approx(0.05)
    assert variance == pytest.approx(5.61)
    np.testing.assert_allclose(estimator.state, [
        np.pi - 0.02 + (5.36 / 5.61) * 0.05,
        0.1 - (0.5 / 5.61) * 0.05,
    ], atol=1e-14)
    # Conditional Gaussian covariance, independent of the Joseph expression.
    np.testing.assert_allclose(estimator.covariance, [
        [5.36 - 5.36**2 / 5.61, -0.5 + 5.36 * 0.5 / 5.61],
        [-0.5 + 5.36 * 0.5 / 5.61, 0.25 - 0.5**2 / 5.61],
    ], atol=1e-14)
    assert estimator.state[1] < 0.1


@pytest.mark.parametrize("turns", [-7, -1, 0, 1, 7])
def test_wrapped_update_preserves_accumulated_turns(turns):
    initial_angle = turns * 2.0 * np.pi + np.deg2rad(179.0)
    estimator = make_filter(initial_angle_rad=initial_angle)
    innovation, _ = estimator.update_wrapped_angle(np.deg2rad(-179.0))
    assert innovation == pytest.approx(np.deg2rad(2.0))
    assert estimator.state[0] == pytest.approx(initial_angle + np.deg2rad(1.0))


def test_unwrapped_update_keeps_original_long_innovation_behavior():
    estimator = make_filter()
    innovation, _ = estimator.update(np.deg2rad(-179.0))
    assert np.rad2deg(innovation) == pytest.approx(-358.0)
    assert estimator.state[0] == pytest.approx(0.0)


def test_wrapped_and_equivalent_unwrapped_updates_have_same_covariance():
    wrapped, unwrapped = make_filter(), make_filter()
    for estimator in (wrapped, unwrapped):
        estimator.predict(rate_rad_s=0.0, dt_s=0.3)
    wrapped_innovation, wrapped_variance = wrapped.update_wrapped_angle(
        np.deg2rad(-179.0)
    )
    unwrapped_innovation, unwrapped_variance = unwrapped.update(np.deg2rad(181.0))
    assert wrapped_innovation == pytest.approx(unwrapped_innovation)
    assert wrapped_variance == unwrapped_variance
    np.testing.assert_array_equal(wrapped.covariance, unwrapped.covariance)
    np.testing.assert_allclose(wrapped.state, unwrapped.state, atol=1e-14)


@pytest.mark.parametrize("observation", [np.pi, -np.pi])
def test_exact_half_turn_uses_negative_pi_tie_break(observation):
    estimator = make_filter(initial_angle_rad=0.0)
    innovation, _ = estimator.update_wrapped_angle(observation)
    assert innovation == pytest.approx(-np.pi)


@pytest.mark.parametrize("observation", [np.nan, np.inf, -np.inf])
def test_nonfinite_wrapped_observation_does_not_change_filter(observation):
    estimator = make_filter()
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="finite"):
        estimator.update_wrapped_angle(observation)
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)


def test_unrepresentable_angular_difference_does_not_change_filter():
    estimator = make_filter(initial_angle_rad=1e308)
    state, covariance = estimator.state, estimator.covariance
    with pytest.raises(ValueError, match="finite"):
        estimator.update_wrapped_angle(-1e308)
    np.testing.assert_array_equal(estimator.state, state)
    np.testing.assert_array_equal(estimator.covariance, covariance)
