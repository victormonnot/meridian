"""Check timing, independent simulation streams, exports, and declared criteria."""

from dataclasses import replace
import json

import numpy as np
import pytest

from meridian.kalman_experiment import (
    ACCEPTANCE_LIMITS, ExperimentConfig, main, run_experiment, run_trial,
)
from meridian.simulation import sample_angle


def test_sparse_observation_is_applied_after_its_gyro_intervals():
    config = ExperimentConfig(duration_s=0.2, sample_rate_hz=10.0, observation_every=2)
    trial = run_trial(config, 8)
    assert trial["observation_indices"].tolist() == [2]
    # Before the first observation there is no bias correction.
    np.testing.assert_allclose(trial["states"][:2, 0], trial["gyro_roll"][:2])
    np.testing.assert_array_equal(trial["states"][:2, 1], [0.0, 0.0])
    # Independent closed-form prior after T=0.2 s and two noisy rate intervals.
    bias_variance = np.deg2rad(1.0)**2
    angle_variance = 0.2**2 * bias_variance + 2 * (np.deg2rad(1.0) * 0.1)**2
    cross_covariance = -0.2 * bias_variance
    predicted_angle = np.sum(trial["gyro"]) * 0.1
    innovation = trial["observations"][0] - predicted_angle
    variance = angle_variance + np.deg2rad(2.0)**2
    expected = [predicted_angle + angle_variance / variance * innovation,
                cross_covariance / variance * innovation]
    np.testing.assert_allclose(trial["states"][-1], expected, atol=1e-14)
    assert trial["innovations"][0] == pytest.approx(innovation)
    assert trial["innovation_variances"][0] == pytest.approx(variance)


def test_angle_settings_do_not_change_shared_gyro_measurements():
    config = ExperimentConfig(duration_s=1.0)
    first = run_trial(config, 12)
    second = run_trial(replace(config, observation_every=5, angle_noise_std_deg=4.0), 12)
    np.testing.assert_array_equal(first["gyro"], second["gyro"])
    np.testing.assert_array_equal(first["gyro_roll"], second["gyro_roll"])
    assert first["stream_seeds"]["gyro"] != first["stream_seeds"]["angle"]
    np.testing.assert_array_equal(first["states"], run_trial(config, 12)["states"])


def test_angle_sampler_preserves_input_and_global_rng():
    angles = np.array([-0.3, 0.0, 0.8])
    original = angles.copy()
    state = np.random.get_state()
    noisy = sample_angle(angles, noise_std_rad=0.1, seed=5)
    np.testing.assert_array_equal(sample_angle(angles, noise_std_rad=0.1, seed=5), noisy)
    np.testing.assert_array_equal(sample_angle(angles, noise_std_rad=0.0, seed=5), angles)
    np.testing.assert_array_equal(angles, original)
    after = np.random.get_state()
    assert state[0] == after[0] and state[2:] == after[2:]
    np.testing.assert_array_equal(state[1], after[1])
    with pytest.raises(ValueError):
        sample_angle([np.nan], noise_std_rad=0.1, seed=5)
    with pytest.raises(ValueError):
        sample_angle(angles, noise_std_rad=-0.1, seed=5)


def test_exports_reproduce_and_keep_truth_separate(tmp_path):
    config = ExperimentConfig(duration_s=0.5, observation_every=5)
    first, second = tmp_path / "first", tmp_path / "second"
    summary = run_experiment(first, config=config, validation_seeds=2)
    assert run_experiment(second, config=config, validation_seeds=2) == summary
    for path in first.iterdir():
        assert path.read_bytes() == (second / path.name).read_bytes()
    assert summary["acceptance"]["applied"] is False
    assert summary["acceptance"]["selected_passed"] is None
    assert summary["validation"]["seeds"] == [0, 1]
    assert json.loads((first / "summary.json").read_text()) == summary
    gyro = np.genfromtxt(first / "gyro_measurements.csv", delimiter=",", names=True)
    angle = np.genfromtxt(first / "angle_measurements.csv", delimiter=",", names=True)
    estimates = np.genfromtxt(first / "estimates.csv", delimiter=",", names=True)
    innovations = np.genfromtxt(first / "innovations.csv", delimiter=",", names=True)
    np.testing.assert_array_equal(gyro["t_end_s"], estimates["time_s"][1:])
    np.testing.assert_array_equal(angle["time_s"], estimates["time_s"][5::5])
    np.testing.assert_array_equal(angle["time_s"], innovations["time_s"])
    assert gyro.dtype.names == ("t_start_s", "t_end_s", "rate_rad_s")
    assert angle.dtype.names == ("time_s", "angle_rad")
    before = (first / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_experiment(first, config=config, validation_seeds=2)
    assert (first / "summary.json").read_bytes() == before


def test_default_scenario_meets_predeclared_criteria():
    metrics = run_trial(ExperimentConfig(), 42)["metrics"]
    assert metrics["kalman_angle_rmse_deg"] < ACCEPTANCE_LIMITS["angle_rmse_deg"]
    assert abs(metrics["final_bias_error_deg_s"]) < ACCEPTANCE_LIMITS["final_bias_abs_error_deg_s"]
    assert metrics["kalman_observation_epoch_rmse_deg"] < metrics["raw_angle_observation_rmse_deg"]


@pytest.mark.parametrize("option,value", [
    ("--observation-every", "0"), ("--duration-s", "0.01"),
    ("--angle-noise-std-deg", "0"), ("--seed", "-1"),
    ("--validation-seeds", "0"),
])
def test_invalid_cli_input_leaves_no_output(tmp_path, option, value):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        main([option, value, "--output", str(output)])
    assert failure.value.code == 2
    assert not output.exists()
