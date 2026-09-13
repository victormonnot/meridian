"""Check controlled-scenario exports, disclosure of hidden times, and CLI criteria."""

import json

import numpy as np
import pytest

from meridian.stress_experiment import _nominal_failures, main, run_experiment


def test_complete_export_reproduces_with_installed_entrypoint_contract(tmp_path, capsys):
    first, second = tmp_path / "first", tmp_path / "second"
    summary = run_experiment(first, validation_seeds=1)
    assert main(["--output", str(second), "--validation-seeds", "1"]) == 0
    assert "stress cases have no accuracy pass/fail threshold" in capsys.readouterr().out
    assert json.loads((second / "summary.json").read_text()) == summary
    files = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    assert len(files) == 67  # Eight CSVs per case, summary, and two figures.
    assert files == {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    acceptance = summary["acceptance"]
    for field in ("selected_failed_methods", "failed_validation", "failed_numerical_validation",
                  "selected_failed_numerical_scenarios"):
        assert acceptance[field] == []
    assert acceptance["stress_accuracy_thresholds"] is None
    assert summary["validation"]["seeds"] == [0]
    assert summary["scenarios"]["accel_dropout"]["used_accel_count"] == 250
    assert summary["scenarios"]["accel_dropout"]["maximum_correction_gap_s"] == pytest.approx(5.1)
    for case in summary["scenarios"]:
        path = first / case
        measurements = np.genfromtxt(path / "accel_measurements.csv", delimiter=",", names=True)
        schedule = np.genfromtxt(path / "observation_schedule_truth.csv", delimiter=",", names=True)
        estimates = np.genfromtxt(path / "estimates.csv", delimiter=",", names=True)
        assert measurements.dtype.names == ("arrival_time_s", "force_y_m_s2", "force_z_m_s2")
        assert schedule.dtype.names == ("arrival_time_s", "sample_time_s", "available")
        np.testing.assert_array_equal(measurements["arrival_time_s"], schedule["arrival_time_s"][schedule["available"] == 1])
        assert len(estimates) == 3001
        assert "ekf_p_angle_bias_rad2_s" in estimates.dtype.names
        for method in ("kalman", "ekf"):
            innovations = np.genfromtxt(path / f"{method}_innovations.csv", delimiter=",", names=True)
            np.testing.assert_array_equal(innovations["arrival_time_s"], measurements["arrival_time_s"])
        if case == "accel_delay":
            assert schedule["sample_time_s"][0] == 0
            np.testing.assert_allclose(schedule["arrival_time_s"] - schedule["sample_time_s"], 0.1)
        if case == "accel_dropout":
            assert not np.any((measurements["arrival_time_s"] >= 12) & (measurements["arrival_time_s"] < 17))
    before = (first / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_experiment(first)
    assert (first / "summary.json").read_bytes() == before


def test_nominal_checks_both_filters_use_absolute_bias_and_strict_limits():
    metrics = {name: {"angle_rmse_deg": 0.74, "final_bias_error_deg_s": -0.14}
               for name in ("kalman", "ekf")}
    assert _nominal_failures(metrics) == []
    metrics["ekf"]["final_bias_error_deg_s"] = -0.15
    assert _nominal_failures(metrics) == ["ekf"]
    metrics["kalman"]["angle_rmse_deg"] = 0.75
    assert _nominal_failures(metrics) == ["kalman", "ekf"]


@pytest.mark.parametrize("option,value", [("--seed", "-1"), ("--validation-seeds", "0")])
def test_invalid_cli_input_leaves_no_output(tmp_path, option, value):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        main([option, value, "--output", str(output)])
    assert failure.value.code == 2
    assert not output.exists()
