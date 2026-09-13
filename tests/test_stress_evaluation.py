"""Check scenario timing, shared inputs, diagnostics, and independent metrics."""

from dataclasses import replace
import math

import numpy as np
import pytest

from meridian import stress_evaluation as evaluation
from meridian.accel_experiment import FusionConfig
from meridian.ekf import AngleBiasEKF, gravity_observation
from meridian.ekf_experiment import run_trial
from meridian.simulation import RollTruth
from meridian.stress_scenarios import SCENARIOS, ScenarioData


CASES = {scenario.name: scenario for scenario in SCENARIOS}


@pytest.fixture(scope="module")
def trials():
    return {name: evaluation.evaluate_scenario(scenario, 42) for name, scenario in CASES.items()}


def test_nominal_reproduces_the_existing_vector_ekf_trial(trials):
    actual = trials["nominal"]
    reference = run_trial(FusionConfig(), 42)
    np.testing.assert_array_equal(actual["data"].gyro_rate_rad_s, reference["gyro"])
    np.testing.assert_array_equal(actual["data"].force_yz_m_s2, reference["force"])
    np.testing.assert_array_equal(actual["observation_indices"], reference["indices"])
    for name, expected in reference["estimates"].items():
        np.testing.assert_array_equal(actual["estimates"][name], expected)
        assert actual["metrics"][name]["angle_rmse_deg"] == reference["metrics"][name]["rmse_deg"]
    np.testing.assert_array_equal(actual["states"]["kalman"], reference["states"])
    np.testing.assert_array_equal(actual["covariance"]["kalman"], reference["covariance"])
    np.testing.assert_array_equal(actual["innovations"]["kalman"][:, 0], reference["innovations"][:, 0])
    np.testing.assert_array_equal(actual["innovation_covariance"]["kalman"][:, 0, 0],
                                  reference["innovations"][:, 1])
    for name in ("states", "covariance", "innovations", "innovation_covariance"):
        np.testing.assert_array_equal(actual[name]["ekf"], reference["ekf"][name])
    np.testing.assert_array_equal(actual["nis"]["ekf"], reference["ekf"]["normalized_innovation_squared"])
    assert actual["metrics"]["ekf"]["angle_rmse_deg"] == reference["ekf"]["metrics"]["rmse_deg"]


@pytest.mark.parametrize("name", CASES)
def test_covariance_diagnostics_and_shared_initialization(name, trials):
    trial = trials[name]
    initial_angle = np.deg2rad(CASES[name].initial_angle_deg)
    for estimate in trial["estimates"].values():
        assert estimate[0] == initial_angle
        assert np.all(np.isfinite(estimate))
    for method in ("kalman", "ekf"):
        assert trial["states"][method][0, 1] == 0.0
        expected_p0 = np.diag([np.deg2rad(CASES[name].initial_angle_std_deg)**2, np.deg2rad(1.0)**2])
        np.testing.assert_array_equal(trial["covariance"][method][0], expected_p0)
        p = trial["covariance"][method]
        np.testing.assert_allclose(p, p.transpose(0, 2, 1), rtol=0.0, atol=1e-16)
        assert np.min(np.linalg.eigvalsh(p)) >= -1e-16
        assert np.all(np.isfinite(trial["nis"][method]))
        assert np.all(trial["nis"][method] >= 0.0)
        assert all(math.isfinite(value) for value in trial["metrics"][method].values())
    # Isotropic vector R preserves the scalar-angle covariance identity even
    # when different state errors change the EKF linearization angle.
    np.testing.assert_allclose(trial["covariance"]["ekf"], trial["covariance"]["kalman"],
                               rtol=2e-12, atol=2e-15)


@pytest.mark.parametrize("name", ["initial_overconfident", "bias_ramp", "accel_noise_mismatch", "accel_delay"])
def test_model_mismatch_does_not_silently_change_filter_tuning(name, trials):
    for method in ("kalman", "ekf"):
        np.testing.assert_allclose(trials[name]["covariance"][method],
                                   trials["nominal"]["covariance"][method], rtol=2e-12, atol=2e-15)


def test_dropout_is_prediction_only_and_preserves_later_noise(trials):
    trial, nominal = trials["accel_dropout"], trials["nominal"]
    data = trial["data"]
    np.testing.assert_array_equal(data.force_yz_m_s2, nominal["data"].force_yz_m_s2)
    np.testing.assert_array_equal(data.gyro_rate_rad_s, nominal["data"].gyro_rate_rad_s)
    assert len(trial["observation_indices"]) == 250
    arrival_times = data.truth.time_s[trial["observation_indices"]]
    assert not np.any((arrival_times >= 12.0) & (arrival_times < 17.0))
    for method in ("kalman", "ekf"):
        np.testing.assert_array_equal(trial["states"][method][:1200], nominal["states"][method][:1200])
        state_before = trial["states"][method][1199]
        p_before = trial["covariance"][method][1199]
        for endpoint in (1200, 1250, 1699):
            dt = np.diff(data.truth.time_s[1199:endpoint + 1])
            elapsed = data.truth.time_s[endpoint] - data.truth.time_s[1199]
            expected_angle = state_before[0] + np.dot(data.gyro_rate_rad_s[1199:endpoint], dt) - state_before[1] * elapsed
            assert trial["states"][method][endpoint, 0] == pytest.approx(expected_angle, abs=2e-14)
            assert trial["states"][method][endpoint, 1] == state_before[1]
            transition = np.array([[1.0, -elapsed], [0.0, 1.0]])
            expected_p = transition @ p_before @ transition.T
            expected_p[0, 0] += np.deg2rad(1.0)**2 * np.dot(dt, dt)
            np.testing.assert_allclose(trial["covariance"][method][endpoint], expected_p, rtol=2e-13, atol=2e-18)


def test_complementary_return_gain_uses_accepted_observation_gap(trials):
    trial = trials["accel_dropout"]
    data = trial["data"]
    times = data.truth.time_s
    returns = np.flatnonzero(trial["observation_indices"] == 1700)[0]
    previous_index = trial["observation_indices"][returns - 1]
    assert times[1700] - times[previous_index] == pytest.approx(5.1)
    predicted_angle = trial["estimates"]["complementary"][1699] + data.gyro_rate_rad_s[1699] * (times[1700] - times[1699])
    observation_row = np.flatnonzero(data.observation_indices == 1700)[0]
    fy, fz = data.force_yz_m_s2[observation_row]
    difference = (math.atan2(-fy, -fz) - predicted_angle + np.pi) % (2 * np.pi) - np.pi
    expected = predicted_angle + (1.0 - math.exp(-5.1)) * difference
    assert trial["estimates"]["complementary"][1700] == pytest.approx(expected, abs=2e-15)
    # Treating the return as a regular 0.1 s correction would change behavior.
    wrong_short_gap = predicted_angle + (1.0 - math.exp(-0.1)) * difference
    assert abs(expected - wrong_short_gap) > 1e-3


def test_delay_correction_uses_arrival_without_hidden_truth(monkeypatch, trials):
    trial = trials["accel_delay"]
    data = trial["data"]
    assert data.sample_time_s[0] == 0.0
    assert data.truth.time_s[trial["observation_indices"][0]] == pytest.approx(0.1)
    oracle = AngleBiasEKF(
        initial_angle_rad=0.0, initial_bias_rad_s=0.0, initial_angle_std_rad=0.0,
        initial_bias_std_rad_s=np.deg2rad(1.0), gyro_noise_std_rad_s=np.deg2rad(1.0),
        accel_noise_std_m_s2=0.2,
    )
    for rate, dt in zip(data.gyro_rate_rad_s[:10], np.diff(data.truth.time_s[:11])):
        oracle.predict(float(rate), float(dt))
    oracle.update(data.force_yz_m_s2[0])
    np.testing.assert_array_equal(trial["states"]["ekf"][10], oracle.state)
    changed_truth = replace(data.truth, angle_rad=data.truth.angle_rad + 10.0,
                            interval_rate_rad_s=data.truth.interval_rate_rad_s + 20.0)
    changed_data = replace(data, sample_time_s=data.sample_time_s + 1000.0,
                           bias_rad_s=data.bias_rad_s + 2.0,
                           interval_bias_rad_s=data.interval_bias_rad_s + 3.0,
                           truth=changed_truth)
    monkeypatch.setattr(evaluation, "generate_scenario", lambda scenario, seed: changed_data)
    changed = evaluation.evaluate_scenario(CASES["accel_delay"], 42)
    for key in ("estimates", "states", "covariance", "innovations", "innovation_covariance", "nis"):
        for method in trial[key]:
            np.testing.assert_array_equal(changed[key][method], trial[key][method])
    assert changed["metrics"]["ekf"]["angle_rmse_deg"] != trial["metrics"]["ekf"]["angle_rmse_deg"]


def test_all_filters_use_actual_unequal_intervals_for_known_motion(monkeypatch):
    times = np.array([0.0, 0.02, 0.07, 0.08, 25.0, 30.0])
    angles = 0.03 * times
    rates = np.diff(angles) / np.diff(times)
    data = ScenarioData(
        truth=RollTruth(times, angles, rates), gyro_rate_rad_s=rates,
        bias_rad_s=np.zeros(len(times)), interval_bias_rad_s=np.zeros(len(rates)),
        observation_indices=np.array([len(times) - 1]), sample_time_s=times[-1:],
        force_yz_m_s2=gravity_observation(angles[-1])[None, :],
        available=np.array([True]), stream_seeds={},
    )
    monkeypatch.setattr(evaluation, "generate_scenario", lambda scenario, seed: data)
    trial = evaluation.evaluate_scenario(CASES["timing_jitter"], 42)
    for estimate in trial["estimates"].values():
        np.testing.assert_allclose(estimate, angles, rtol=0.0, atol=2e-15)
    for method in ("kalman", "ekf"):
        np.testing.assert_allclose(trial["states"][method][:, 1], 0.0, rtol=0.0, atol=2e-16)


def test_reported_metrics_match_independent_hand_calculation():
    metrics = evaluation.summarize_errors([3.0, -4.0, 12.0], [0.0, 25.0, 30.0])
    assert metrics == pytest.approx({
        "angle_rmse_deg": 13.0 / math.sqrt(3.0),
        "angle_time_weighted_rmse_deg": math.sqrt((25.0 * 12.5 + 5.0 * 80.0) / 30.0),
        "late_angle_rmse_deg": math.sqrt(80.0),
        "max_abs_angle_error_deg": 12.0,
        "final_angle_error_deg": 12.0,
    })
    unwrapped = evaluation.summarize_errors([360.0, 0.0, 0.0], [0.0, 25.0, 30.0])
    assert unwrapped["angle_rmse_deg"] == pytest.approx(360.0 / math.sqrt(3.0))


@pytest.mark.parametrize("errors,times", [
    ([1, 2], [0, 0]), ([1], [30]), ([1, 2], [0, 1]),
    ([1, 2], [0, np.nan]), ([1, np.inf], [0, 30]), ([1, 2, 3], [0, 30]),
])
def test_metrics_reject_invalid_shapes_times_and_empty_late_window(errors, times):
    with pytest.raises(ValueError):
        evaluation.summarize_errors(errors, times)


def test_bias_metrics_compare_with_changing_endpoint_truth(trials):
    trial = trials["bias_ramp"]
    times = trial["data"].truth.time_s
    independent_truth_deg_s = 0.5 + 0.1 * np.clip(times - 10.0, 0.0, 10.0)
    for method in ("kalman", "ekf"):
        error = np.rad2deg(trial["states"][method][:, 1]) - independent_truth_deg_s
        metrics = trial["metrics"][method]
        assert metrics["bias_rmse_deg_s"] == pytest.approx(np.sqrt(np.mean(error**2)))
        assert metrics["late_bias_rmse_deg_s"] == pytest.approx(np.sqrt(np.mean(error[times >= 25]**2)))
        assert metrics["max_abs_bias_error_deg_s"] == pytest.approx(np.max(np.abs(error)))
        assert metrics["final_bias_error_deg_s"] == pytest.approx(np.rad2deg(trial["states"][method][-1, 1]) - 1.5)


def test_nis_keeps_distinct_scalar_and_vector_observation_dimensions(trials):
    trial = trials["accel_noise_mismatch"]
    scalar_v = trial["innovations"]["kalman"][:, 0]
    scalar_s = trial["innovation_covariance"]["kalman"][:, 0, 0]
    np.testing.assert_allclose(trial["nis"]["kalman"], scalar_v**2 / scalar_s, rtol=2e-15)
    vy, vz = trial["innovations"]["ekf"].T
    s = trial["innovation_covariance"]["ekf"]
    determinant = s[:, 0, 0] * s[:, 1, 1] - s[:, 0, 1]**2
    expected = (s[:, 1, 1] * vy**2 - 2.0 * s[:, 0, 1] * vy * vz + s[:, 0, 0] * vz**2) / determinant
    np.testing.assert_allclose(trial["nis"]["ekf"], expected, rtol=2e-13)
    assert trial["metrics"]["ekf"]["mean_nis"] == pytest.approx(np.mean(expected))
