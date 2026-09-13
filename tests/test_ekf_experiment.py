"""Check shared inputs, scheduling, diagnostics, and reproducible EKF exports."""

import json

import numpy as np
import pytest

from meridian import accel_experiment as baseline
from meridian.ekf_experiment import ACCEPTANCE_LIMITS, FusionConfig, main, run_experiment, run_trial


SHORT_CONFIG = FusionConfig(duration_s=1.0, pulse_start_s=0.4, pulse_end_s=0.7)


@pytest.mark.parametrize("disturbed", [False, True])
def test_ekf_uses_unchanged_baseline_measurements_and_results(disturbed):
    reference = baseline.run_trial(SHORT_CONFIG, 42, disturbed=disturbed)
    trial = run_trial(SHORT_CONFIG, 42, disturbed=disturbed)
    for name in ("gyro", "indices", "force", "tilt", "translation", "states", "covariance", "innovations"):
        np.testing.assert_array_equal(trial[name], reference[name])
    for name in reference["estimates"]:
        np.testing.assert_array_equal(trial["estimates"][name], reference["estimates"][name])
    assert trial["metrics"] == reference["metrics"]
    assert trial["stream_seeds"] == reference["stream_seeds"]
    # Isotropic vector noise yields the same covariance as the scalar-angle KF,
    # even when translation changes the state correction.
    np.testing.assert_allclose(trial["ekf"]["covariance"], reference["covariance"], atol=2e-18, rtol=2e-13)


def test_sparse_updates_and_paired_noise_preserve_causality():
    nominal = run_trial(SHORT_CONFIG, 42)
    disturbed = run_trial(SHORT_CONFIG, 42, disturbed=True)
    np.testing.assert_array_equal(nominal["gyro"], disturbed["gyro"])
    np.testing.assert_allclose(disturbed["force"] - nominal["force"], disturbed["translation"], atol=2e-15)
    np.testing.assert_array_equal(nominal["ekf"]["states"][:40], disturbed["ekf"]["states"][:40])
    assert not np.allclose(nominal["ekf"]["states"][40:], disturbed["ekf"]["states"][40:])
    ekf = nominal["ekf"]
    np.testing.assert_allclose(ekf["states"][:10, 0], nominal["estimates"]["gyro"][:10])
    np.testing.assert_array_equal(ekf["states"][:10, 1], np.zeros(10))
    assert nominal["truth"].time_s[nominal["indices"][0]] == pytest.approx(0.1)
    assert ekf["innovations"].shape == (10, 2)
    assert ekf["innovation_covariance"].shape == (10, 2, 2)


def test_joint_nis_matches_independent_two_by_two_quadratic_form():
    ekf = run_trial(SHORT_CONFIG, 42, disturbed=True)["ekf"]
    vy, vz = ekf["innovations"].T
    s = ekf["innovation_covariance"]
    determinant = s[:, 0, 0] * s[:, 1, 1] - s[:, 0, 1]**2
    expected = (s[:, 1, 1] * vy**2 - 2 * s[:, 0, 1] * vy * vz + s[:, 0, 0] * vz**2) / determinant
    np.testing.assert_allclose(ekf["normalized_innovation_squared"], expected, rtol=1e-13)
    assert ekf["metrics"]["mean_normalized_innovation_squared"] == pytest.approx(np.mean(expected))


def test_default_nominal_meets_predeclared_accuracy_limits():
    trial = run_trial(FusionConfig(), 42)
    metrics = trial["ekf"]["metrics"]
    assert metrics["rmse_deg"] < ACCEPTANCE_LIMITS["ekf_angle_rmse_deg"]
    assert abs(metrics["final_bias_error_deg_s"]) < ACCEPTANCE_LIMITS["ekf_final_bias_abs_error_deg_s"]
    # There is deliberately no requirement to outperform the angle KF.


def test_exports_are_reproducible_and_keep_vector_units_and_timestamps(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    summary = run_experiment(first, config=SHORT_CONFIG, validation_seeds=2)
    assert run_experiment(second, config=SHORT_CONFIG, validation_seeds=2) == summary
    files = [path for path in first.rglob("*") if path.is_file()]
    assert len(files) == 20  # Nine CSVs per scenario, one summary, one plot.
    for path in files:
        assert path.read_bytes() == (second / path.relative_to(first)).read_bytes()
    assert json.loads((first / "summary.json").read_text()) == summary
    assert summary["acceptance"]["applied"] is False
    assert summary["acceptance"]["selected_passed"] is None
    assert summary["acceptance"]["failed_validation_seeds"] is None
    assert summary["innovation_dimensions"] == {"angle_kalman": 1, "vector_ekf": 2}
    assert summary["validation"]["seeds"] == [0, 1]
    for scenario in ("nominal", "translation_pulse"):
        directory = first / scenario
        measurements = np.genfromtxt(directory / "accel_measurements.csv", delimiter=",", names=True)
        estimates = np.genfromtxt(directory / "ekf_estimates.csv", delimiter=",", names=True)
        innovations = np.genfromtxt(directory / "ekf_innovations.csv", delimiter=",", names=True)
        assert estimates.dtype.names == ("time_s", "ekf_roll_rad", "ekf_bias_rad_s", "p_angle_rad2",
                                         "p_angle_bias_rad2_s", "p_bias_rad2_s2")
        assert innovations.dtype.names == ("time_s", "innovation_y_m_s2", "innovation_z_m_s2",
                                           "s_yy_m2_s4", "s_yz_m2_s4", "s_zz_m2_s4", "normalized_innovation_squared")
        np.testing.assert_array_equal(innovations["time_s"], measurements["time_s"])
        np.testing.assert_array_equal(innovations["time_s"], estimates["time_s"][10::10])
        assert measurements.dtype.names == ("time_s", "force_y_m_s2", "force_z_m_s2")
        assert (directory / "truth.csv").exists()
    before = (first / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_experiment(first)
    assert (first / "summary.json").read_bytes() == before


@pytest.mark.parametrize("option,value", [
    ("--seed", "-1"), ("--accel-noise-std-m-s2", "0"),
    ("--accel-noise-std-m-s2", "nan"), ("--complementary-tau-s", "0"),
    ("--validation-seeds", "0"),
])
def test_invalid_cli_input_leaves_no_output(tmp_path, option, value):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        main([option, value, "--output", str(output)])
    assert failure.value.code == 2
    assert not output.exists()
