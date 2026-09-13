"""Verify simulated specific force, sparse fusion, paired inputs, and exports."""

from dataclasses import replace
import json

import numpy as np
import pytest

from meridian.accel_experiment import FusionConfig, GRAVITY_M_S2, run_experiment, run_trial, main
from meridian.simulation import sample_accelerometer
from meridian.tilt import accel_roll, wrap_angle


def test_simulated_force_matches_known_poses_and_body_translation():
    roll = np.array([0.0, np.pi / 2, -np.pi / 2, np.pi])
    force = sample_accelerometer(roll, noise_std_m_s2=0.0, seed=0, gravity_m_s2=10.0)
    np.testing.assert_allclose(force, [[0, -10], [-10, 0], [10, 0], [0, 10]], atol=1e-14)
    shifted = sample_accelerometer(
        roll, noise_std_m_s2=0.0, seed=0, gravity_m_s2=10.0, translation_yz_m_s2=[2.0, -1.0]
    )
    np.testing.assert_allclose(shifted - force, np.tile([2, -1], (4, 1)), atol=1e-14)


def test_gravity_magnitude_can_hide_a_false_tilt():
    # Level sensor plus translation reproduces the force of a genuinely tilted sensor.
    apparent_angle = np.deg2rad(20.0)
    translation = [-GRAVITY_M_S2 * np.sin(apparent_angle),
                   GRAVITY_M_S2 * (1 - np.cos(apparent_angle))]
    force = sample_accelerometer([0.0], noise_std_m_s2=0.0, seed=1, translation_yz_m_s2=translation)
    assert np.hypot(*force[0]) == pytest.approx(GRAVITY_M_S2)
    assert accel_roll(force)[0] == pytest.approx(apparent_angle)


def test_nominal_angle_noise_matches_first_order_variance():
    expected_variance = (0.2 / GRAVITY_M_S2)**2
    for seed, angle in enumerate((0.0, np.pi / 3)):
        force = sample_accelerometer(np.full(50000, angle), noise_std_m_s2=0.2, seed=seed)
        error = wrap_angle(accel_roll(force) - angle)
        # Seeded statistical check of the approximation, not exact Gaussianity.
        assert np.var(error) == pytest.approx(expected_variance, rel=0.03)
        assert abs(np.mean(error)) < 0.001


def test_accelerometer_rng_is_local_and_input_is_preserved():
    angles = np.array([0.1, -0.2])
    translation = np.array([[1.0, 0.0], [0.0, 0.2]])
    original = translation.copy()
    global_state = np.random.get_state()
    options = dict(noise_std_m_s2=0.2, seed=3, translation_yz_m_s2=translation)
    first = sample_accelerometer(angles, **options)
    np.testing.assert_array_equal(first, sample_accelerometer(angles, **options))
    np.testing.assert_array_equal(translation, original)
    np.testing.assert_array_equal(angles, [0.1, -0.2])
    after = np.random.get_state()
    assert global_state[0] == after[0] and global_state[2:] == after[2:]
    np.testing.assert_array_equal(global_state[1], after[1])
    with pytest.raises(ValueError):
        sample_accelerometer(angles, noise_std_m_s2=0.2, seed=3, translation_yz_m_s2=[1, 2, 3])
    with pytest.raises(ValueError):
        sample_accelerometer([np.nan], noise_std_m_s2=0.2, seed=3)


def test_first_sparse_correction_uses_full_elapsed_time():
    config = FusionConfig(duration_s=1.0, observation_every=20,
                          pulse_start_s=0.4, pulse_end_s=0.6)
    trial = run_trial(config, 2)
    predicted = trial["estimates"]["gyro"][20]
    residual = float(wrap_angle(trial["tilt"][0] - predicted))
    expected = predicted + (1 - np.exp(-0.2)) * residual
    assert trial["estimates"]["complementary"][20] == pytest.approx(expected)
    np.testing.assert_allclose(trial["estimates"]["complementary"][:20], trial["estimates"]["gyro"][:20])
    np.testing.assert_allclose(trial["states"][:20, 0], trial["estimates"]["gyro"][:20])
    assert trial["truth"].time_s[trial["indices"][0]] == pytest.approx(0.2)


def test_paired_trials_change_only_prescribed_accelerometer_force():
    config = FusionConfig(duration_s=1.0, pulse_start_s=0.4, pulse_end_s=0.7)
    nominal, disturbed = run_trial(config, 42), run_trial(config, 42, disturbed=True)
    np.testing.assert_array_equal(nominal["gyro"], disturbed["gyro"])
    np.testing.assert_array_equal(nominal["estimates"]["gyro"], disturbed["estimates"]["gyro"])
    np.testing.assert_allclose(disturbed["force"] - nominal["force"], disturbed["translation"], atol=2e-15)
    np.testing.assert_array_equal(disturbed["force"][:, 1], nominal["force"][:, 1])
    # The pulse starts at .4 and stops before .7; the first future observation is not used early.
    np.testing.assert_allclose(disturbed["translation"][:, 0], [0, 0, 0, 2, 2, 2, 0, 0, 0, 0])
    np.testing.assert_array_equal(nominal["states"][:40], disturbed["states"][:40])
    changed_noise = run_trial(replace(config, accel_noise_std_m_s2=0.4), 42)
    np.testing.assert_array_equal(nominal["gyro"], changed_noise["gyro"])


def test_exports_reproduce_and_preserve_observation_timing(tmp_path):
    config = FusionConfig(duration_s=1.0, pulse_start_s=0.4, pulse_end_s=0.7)
    first, second = tmp_path / "first", tmp_path / "second"
    summary = run_experiment(first, config=config, validation_seeds=2)
    assert run_experiment(second, config=config, validation_seeds=2) == summary
    assert summary["acceptance"]["applied"] is False
    assert summary["acceptance"]["selected_passed"] is None
    for path in first.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (second / path.relative_to(first)).read_bytes()
    assert json.loads((first / "summary.json").read_text()) == summary
    for scenario in ("nominal", "translation_pulse"):
        directory = first / scenario
        accel = np.genfromtxt(directory / "accel_measurements.csv", delimiter=",", names=True)
        estimates = np.genfromtxt(directory / "estimates.csv", delimiter=",", names=True)
        innovations = np.genfromtxt(directory / "innovations.csv", delimiter=",", names=True)
        assert accel.dtype.names == ("time_s", "force_y_m_s2", "force_z_m_s2")
        np.testing.assert_array_equal(accel["time_s"], estimates["time_s"][10::10])
        np.testing.assert_array_equal(accel["time_s"], innovations["time_s"])
    before = (first / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_experiment(first)
    assert (first / "summary.json").read_bytes() == before


def test_nominal_default_meets_predeclared_limits():
    metrics = run_trial(FusionConfig(), 42)["metrics"]
    assert metrics["kalman"]["rmse_deg"] < 0.75
    assert metrics["complementary"]["rmse_deg"] < 1.0
    assert abs(metrics["final_bias_error_deg_s"]) < 0.15


@pytest.mark.parametrize("option,value", [
    ("--seed", "-1"), ("--accel-noise-std-m-s2", "0"),
    ("--complementary-tau-s", "0"), ("--validation-seeds", "0"),
])
def test_invalid_cli_input_leaves_no_results(tmp_path, option, value):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        main([option, value, "--output", str(output)])
    assert failure.value.code == 2
    assert not output.exists()
