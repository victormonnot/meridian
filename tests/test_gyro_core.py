"""Check the gyro model against analytical behavior and input contracts."""

import numpy as np
import pytest

from meridian.integration import integrate_gyro
from meridian.simulation import sample_gyro, simulate_roll


@pytest.mark.parametrize("rate", [2.0, -0.3, 0.0])
def test_constant_rate_uses_actual_intervals_and_initial_angle(rate):
    times = np.array([10.0, 10.01, 10.1, 10.35, 11.0])
    angles = integrate_gyro(times, np.full(times.size - 1, rate), initial_angle_rad=0.4)
    np.testing.assert_allclose(angles, 0.4 + rate * (times - times[0]), atol=1e-14)


def test_each_rate_applies_to_its_corresponding_interval():
    angles = integrate_gyro([10.0, 10.125, 10.625, 11.125], [2.0, -4.0, 1.0],
                            initial_angle_rad=0.3)
    np.testing.assert_allclose(angles, [0.3, 0.55, -1.45, -0.95], atol=1e-14)


def test_ideal_gyro_reconstructs_analytic_sinusoid_at_endpoints():
    truth = simulate_roll(duration_s=3.5, sample_rate_hz=80.0,
                          amplitude_rad=0.2, frequency_hz=0.4)
    expected_times = np.arange(281) / 80.0
    expected_angles = 0.2 * np.sin(2.0 * np.pi * 0.4 * expected_times)
    np.testing.assert_allclose(truth.time_s, expected_times, atol=1e-14)
    np.testing.assert_allclose(truth.angle_rad, expected_angles, atol=1e-14)
    measured_rates = sample_gyro(truth.interval_rate_rad_s)
    np.testing.assert_allclose(integrate_gyro(truth.time_s, measured_rates),
                               expected_angles, atol=1e-14)


def test_stationary_constant_bias_produces_linear_drift():
    truth = simulate_roll(duration_s=20.0, sample_rate_hz=50.0, amplitude_rad=0.0)
    bias = np.deg2rad(0.5)
    measured_rates = sample_gyro(truth.interval_rate_rad_s, bias_rad_s=bias)
    angles = integrate_gyro(truth.time_s, measured_rates)
    np.testing.assert_allclose(angles, bias * truth.time_s, atol=1e-14)
    assert np.rad2deg(angles[-1]) == pytest.approx(10.0)


def test_noise_is_repeatable_for_a_seed_without_touching_global_random_state():
    rates = np.linspace(-1.0, 1.0, 32)
    state_before = np.random.get_state()
    first = sample_gyro(rates, bias_rad_s=0.1, noise_std_rad_s=0.02, seed=17)
    second = sample_gyro(rates, bias_rad_s=0.1, noise_std_rad_s=0.02, seed=17)
    different = sample_gyro(rates, bias_rad_s=0.1, noise_std_rad_s=0.02, seed=18)
    state_after = np.random.get_state()
    np.testing.assert_array_equal(first, second)
    assert not np.array_equal(first, different)
    assert state_before[0] == state_after[0]
    np.testing.assert_array_equal(state_before[1], state_after[1])
    assert state_before[2:] == state_after[2:]


def test_sensor_sampling_and_integration_do_not_mutate_inputs():
    times = np.array([0.0, 0.1, 0.3])
    rates = np.array([0.5, -0.2])
    original_times = times.copy()
    original_rates = rates.copy()
    samples = sample_gyro(rates, bias_rad_s=0.1, noise_std_rad_s=0.01)
    samples_before = samples.copy()
    angles = integrate_gyro(times, samples)
    np.testing.assert_array_equal(times, original_times)
    np.testing.assert_array_equal(rates, original_rates)
    np.testing.assert_array_equal(samples, samples_before)
    assert not np.shares_memory(samples, rates)
    assert not np.shares_memory(angles, samples)


@pytest.mark.parametrize("kwargs", [
    {"duration_s": 0.0}, {"duration_s": -1.0}, {"duration_s": np.nan},
    {"sample_rate_hz": 0.0}, {"sample_rate_hz": np.inf},
    {"amplitude_rad": np.nan}, {"frequency_hz": -0.1},
    {"frequency_hz": 50.0}, {"frequency_hz": np.inf},
    {"duration_s": 0.015}, {"duration_s": 0.001},
])
def test_simulation_rejects_invalid_parameters_and_partial_intervals(kwargs):
    with pytest.raises(ValueError):
        simulate_roll(**kwargs)


def test_simulation_accepts_a_single_interval_and_decimal_grid_roundoff():
    single = simulate_roll(duration_s=0.01, sample_rate_hz=100.0, frequency_hz=0.0)
    np.testing.assert_array_equal(single.time_s, [0.0, 0.01])
    np.testing.assert_array_equal(single.interval_rate_rad_s, [0.0])
    rounded = simulate_roll(duration_s=0.14, sample_rate_hz=100.0)
    assert rounded.interval_rate_rad_s.size == 14
    assert rounded.time_s[-1] == 0.14


@pytest.mark.parametrize("rates", [[], [[1.0]], [np.nan], [np.inf]])
def test_sensor_rejects_invalid_rate_arrays(rates):
    with pytest.raises(ValueError):
        sample_gyro(rates)


@pytest.mark.parametrize("kwargs", [
    {"bias_rad_s": np.inf}, {"noise_std_rad_s": -0.1},
    {"noise_std_rad_s": np.nan}, {"seed": -1}, {"seed": 1.5}, {"seed": True},
])
def test_sensor_rejects_invalid_bias_noise_and_seed(kwargs):
    with pytest.raises(ValueError):
        sample_gyro([0.0], **kwargs)


@pytest.mark.parametrize("times,rates", [
    ([], []), ([0.0], []), ([[0.0, 1.0]], [0.0]),
    ([0.0, 0.0], [0.0]), ([1.0, 0.0], [0.0]), ([0.0, np.nan], [0.0]),
    ([0.0, np.inf], [0.0]), ([0.0, 1.0], []), ([0.0, 1.0], [0.0, 0.0]),
    ([0.0, 1.0], [[0.0]]), ([0.0, 1.0], [np.nan]), ([0.0, 1.0], [np.inf]),
])
def test_integration_rejects_invalid_timestamps_and_rates(times, rates):
    with pytest.raises(ValueError):
        integrate_gyro(times, rates)


@pytest.mark.parametrize("angle", [np.nan, np.inf, -np.inf])
def test_integration_rejects_nonfinite_initial_angle(angle):
    with pytest.raises(ValueError):
        integrate_gyro([0.0, 1.0], [0.0], initial_angle_rad=angle)
