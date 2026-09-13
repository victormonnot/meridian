"""Check independent scenario truth, paired noise, and timestamp semantics."""

from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from meridian.accel_experiment import FusionConfig, run_trial
from meridian.stress_scenarios import (
    SCENARIOS, Scenario, bias_at_time, bias_interval_mean, generate_scenario,
)


CASES = {scenario.name: scenario for scenario in SCENARIOS}


def _gravity_at(times):
    angle = np.deg2rad(20.0) * np.sin(2 * np.pi * 0.1 * times)
    return -9.80665 * np.column_stack((np.sin(angle), np.cos(angle)))


def test_suite_is_fixed_and_changes_one_condition_at_a_time():
    assert tuple(CASES) == (
        "nominal", "initial_offset", "initial_overconfident", "bias_ramp",
        "accel_dropout", "timing_jitter", "accel_noise_mismatch", "accel_delay",
    )
    assert len(SCENARIOS) == 8
    assert CASES["initial_offset"].initial_angle_deg == 60
    assert CASES["initial_offset"].initial_angle_std_deg == 30
    assert CASES["initial_overconfident"].initial_angle_deg == 60
    assert CASES["initial_overconfident"].initial_angle_std_deg == 0
    assert CASES["accel_noise_mismatch"].accel_noise_std_m_s2 == 0.6
    with pytest.raises(FrozenInstanceError):
        CASES["nominal"].initial_angle_deg = 4


def test_nominal_exactly_matches_existing_sensor_experiment():
    baseline = run_trial(FusionConfig(), 42)
    data = generate_scenario(CASES["nominal"], 42)
    for name in ("time_s", "angle_rad", "interval_rate_rad_s"):
        np.testing.assert_array_equal(getattr(data.truth, name), getattr(baseline["truth"], name))
    np.testing.assert_array_equal(data.gyro_rate_rad_s, baseline["gyro"])
    np.testing.assert_array_equal(data.force_yz_m_s2, baseline["force"])
    np.testing.assert_array_equal(data.observation_indices, baseline["indices"])
    np.testing.assert_array_equal(data.sample_time_s, baseline["truth"].time_s[baseline["indices"]])
    assert data.stream_seeds["gyro"] == baseline["stream_seeds"]["gyro"]
    assert data.stream_seeds["accelerometer"] == baseline["stream_seeds"]["accelerometer"]
    assert len(set(data.stream_seeds.values())) == 3


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_shapes_endpoint_truth_and_local_reproducibility(scenario):
    state = np.random.get_state()
    first, second = generate_scenario(scenario, 5), generate_scenario(scenario, 5)
    assert first.truth.time_s.shape == first.truth.angle_rad.shape == first.bias_rad_s.shape == (3001,)
    assert first.truth.interval_rate_rad_s.shape == first.gyro_rate_rad_s.shape == (3000,)
    assert first.interval_bias_rad_s.shape == (3000,)
    assert first.observation_indices.shape == first.sample_time_s.shape == first.available.shape == (300,)
    assert first.force_yz_m_s2.shape == (300, 2)
    assert first.available.dtype == np.bool_
    assert first.truth.time_s[0] == 0
    assert first.truth.time_s[-1] == 30
    for name in ("gyro_rate_rad_s", "bias_rad_s", "interval_bias_rad_s", "observation_indices",
                 "sample_time_s", "force_yz_m_s2", "available"):
        np.testing.assert_array_equal(getattr(first, name), getattr(second, name))
    np.testing.assert_array_equal(first.truth.time_s, second.truth.time_s)
    assert first.stream_seeds == second.stream_seeds
    after = np.random.get_state()
    assert state[0] == after[0] and state[2:] == after[2:]
    np.testing.assert_array_equal(state[1], after[1])


@pytest.mark.parametrize("name", ("initial_offset", "initial_overconfident", "accel_dropout"))
def test_initialization_and_dropout_preserve_all_generated_measurements(name):
    nominal = generate_scenario(CASES["nominal"], 42)
    changed = generate_scenario(CASES[name], 42)
    np.testing.assert_array_equal(changed.gyro_rate_rad_s, nominal.gyro_rate_rad_s)
    np.testing.assert_array_equal(changed.force_yz_m_s2, nominal.force_yz_m_s2)
    np.testing.assert_array_equal(changed.bias_rad_s, nominal.bias_rad_s)
    np.testing.assert_array_equal(changed.truth.angle_rad, nominal.truth.angle_rad)
    np.testing.assert_array_equal(changed.sample_time_s, nominal.sample_time_s)
    assert changed.stream_seeds == nominal.stream_seeds


def test_dropout_mask_has_exact_arrival_boundaries_without_removing_rows():
    data = generate_scenario(CASES["accel_dropout"], 3)
    arrivals = data.truth.time_s[data.observation_indices]
    missing_times = arrivals[~data.available]
    assert len(missing_times) == 50
    assert missing_times[0] == pytest.approx(12)
    assert missing_times[-1] == pytest.approx(16.9)
    assert data.available[np.flatnonzero(arrivals == 17)[0]]
    assert np.all(np.isfinite(data.force_yz_m_s2))


def test_bias_truth_and_exact_integral_across_ramp_boundaries():
    times = np.array([0, 9, 10, 11, 15, 19, 20, 21, 30], dtype=float)
    np.testing.assert_allclose(
        np.rad2deg(bias_at_time(times, bias_ramp=True)),
        [0.5, 0.5, 0.5, 0.6, 1, 1.4, 1.5, 1.5, 1.5], atol=1e-15,
    )
    # Independent areas: constant rectangles plus linear-ramp trapezoids.
    starts = [0, 9, 10, 11, 19, 20, 9, 0]
    ends = [10, 11, 20, 13, 21, 30, 21, 30]
    expected = [0.5, (0.5 + 0.55) / 2, 1, 0.7, (1.45 + 1.5) / 2, 1.5, 1, 1]
    means = bias_interval_mean(starts, ends, bias_ramp=True)
    np.testing.assert_allclose(np.rad2deg(means), expected, rtol=1e-13, atol=1e-13)
    np.testing.assert_allclose(np.rad2deg(bias_at_time(times)), 0.5)
    np.testing.assert_allclose(np.rad2deg(bias_interval_mean(starts, ends)), 0.5)


def test_ramp_only_changes_gyro_bias_and_is_causal():
    nominal = generate_scenario(CASES["nominal"], 8)
    ramp = generate_scenario(CASES["bias_ramp"], 8)
    pre_ramp = ramp.truth.time_s[1:] <= 10
    np.testing.assert_array_equal(ramp.gyro_rate_rad_s[pre_ramp], nominal.gyro_rate_rad_s[pre_ramp])
    np.testing.assert_array_equal(ramp.force_yz_m_s2, nominal.force_yz_m_s2)
    np.testing.assert_allclose(
        ramp.gyro_rate_rad_s - nominal.gyro_rate_rad_s,
        ramp.interval_bias_rad_s - nominal.interval_bias_rad_s, atol=6e-17,
    )
    np.testing.assert_allclose(
        np.sum(ramp.interval_bias_rad_s * np.diff(ramp.truth.time_s)), np.deg2rad(30), atol=1e-15,
    )
    assert ramp.bias_rad_s[-1] == pytest.approx(np.deg2rad(1.5))


def test_jitter_uses_actual_intervals_and_an_independent_paired_noise_stream():
    nominal = generate_scenario(CASES["nominal"], 42)
    jitter = generate_scenario(CASES["timing_jitter"], 42)
    times = jitter.truth.time_s
    dt = np.diff(times)
    assert np.all(dt > 0)
    assert np.ptp(dt) > 0.009
    assert np.sum(dt) == pytest.approx(30, abs=1e-13)
    # The integral identity is independent of the exact random durations.
    np.testing.assert_allclose(
        jitter.truth.interval_rate_rad_s * dt,
        np.diff(jitter.truth.angle_rad), atol=1e-18,
    )
    np.testing.assert_allclose(
        jitter.gyro_rate_rad_s - jitter.truth.interval_rate_rad_s - jitter.interval_bias_rad_s,
        nominal.gyro_rate_rad_s - nominal.truth.interval_rate_rad_s - nominal.interval_bias_rad_s,
        atol=6e-17,
    )
    np.testing.assert_array_equal(jitter.sample_time_s, times[jitter.observation_indices])
    np.testing.assert_allclose(
        jitter.force_yz_m_s2 - _gravity_at(jitter.sample_time_s),
        nominal.force_yz_m_s2 - _gravity_at(nominal.sample_time_s), atol=2e-15,
    )
    assert jitter.stream_seeds == nominal.stream_seeds


def test_delay_changes_acquisition_signal_without_shifting_the_noise_sequence():
    nominal = generate_scenario(CASES["nominal"], 12)
    delayed = generate_scenario(CASES["accel_delay"], 12)
    arrival = delayed.truth.time_s[delayed.observation_indices]
    assert delayed.sample_time_s[0] == 0
    np.testing.assert_array_equal(delayed.sample_time_s, arrival - 0.1)
    np.testing.assert_array_equal(delayed.gyro_rate_rad_s, nominal.gyro_rate_rad_s)
    np.testing.assert_allclose(
        delayed.force_yz_m_s2 - nominal.force_yz_m_s2,
        _gravity_at(delayed.sample_time_s) - _gravity_at(nominal.sample_time_s), atol=2e-15,
    )
    assert np.all(delayed.available)


def test_noise_mismatch_scales_the_same_standardized_acceleration_noise_only():
    nominal = generate_scenario(CASES["nominal"], 7)
    changed = generate_scenario(CASES["accel_noise_mismatch"], 7)
    signal = _gravity_at(nominal.sample_time_s)
    np.testing.assert_allclose((changed.force_yz_m_s2 - signal) / 3, nominal.force_yz_m_s2 - signal,
                               atol=2e-15)
    np.testing.assert_array_equal(changed.gyro_rate_rad_s, nominal.gyro_rate_rad_s)


@pytest.mark.parametrize("seed", (-1, True, np.bool_(True), 0.5, "42", None))
def test_invalid_seed_is_rejected(seed):
    with pytest.raises(ValueError, match="seed"):
        generate_scenario(CASES["nominal"], seed)


@pytest.mark.parametrize("field,value", [
    ("initial_angle_deg", np.inf), ("initial_angle_deg", True),
    ("initial_angle_std_deg", -1), ("accel_noise_std_m_s2", np.nan),
    ("accel_noise_std_m_s2", -1), ("accel_delay_s", -0.1),
    ("bias_ramp", 1), ("accel_dropout", "yes"), ("timing_jitter", None),
    ("name", ""), ("description", ""),
])
def test_invalid_scenario_settings_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        replace(CASES["nominal"], **{field: value})


def test_delay_cannot_invent_data_before_simulation_start():
    scenario = replace(CASES["nominal"], accel_delay_s=0.2)
    with pytest.raises(ValueError, match="before the simulation"):
        generate_scenario(scenario, 0)


@pytest.mark.parametrize("start,end", [(1, 1), (2, 1), (-1, 1), (0, np.inf), ([], [])])
def test_invalid_bias_intervals_are_rejected(start, end):
    with pytest.raises(ValueError):
        bias_interval_mean(start, end, bias_ramp=True)
