"""Check display export provenance, full-resolution metrics, and failure handling."""

import csv
import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from meridian.ekf_experiment import FusionConfig, run_experiment
from meridian.web_export import COLUMNS, build_comparison, export_comparison, main


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    source = tmp_path_factory.mktemp("web-source") / "experiment"
    run_experiment(source, config=FusionConfig(duration_s=1, pulse_start_s=.4, pulse_end_s=.7),
                   validation_seeds=1)
    return source


@pytest.fixture
def mutable_source(experiment, tmp_path):
    return Path(shutil.copytree(experiment, tmp_path / "source"))


def test_units_decimation_endpoints_and_metrics(experiment):
    data = build_comparison(experiment, stride=6)
    summary = json.loads((experiment / "summary.json").read_text())
    assert data["columns"] == COLUMNS
    for name, case in data["scenarios"].items():
        assert case["source_sample_count"] == 101
        rows = np.array(case["rows"])
        assert rows[0, 0] == 0
        assert rows[-1, 0] == 1
        assert np.any(np.isclose(rows[:, 0], .4, rtol=0, atol=1e-12))
        assert np.any(np.isclose(rows[:, 0], .7, rtol=0, atol=1e-12))
        truth = np.genfromtxt(experiment / name / "truth.csv", delimiter=",", names=True)
        ekf = np.genfromtxt(experiment / name / "ekf_estimates.csv", delimiter=",", names=True)
        for row in rows:
            index = np.flatnonzero(truth["time_s"] == row[0]).item()
            assert row[1] == pytest.approx(np.degrees(truth["roll_rad"][index]), abs=5.01e-7)
            assert row[5] == pytest.approx(np.degrees(ekf["ekf_roll_rad"][index]), abs=5.01e-7)
            assert row[8] == pytest.approx(np.degrees(ekf["ekf_bias_rad_s"][index]), abs=5.01e-7)
        metrics = case["metrics"]["rmse_deg"]
        assert metrics["ekf"] == summary["scenarios"][name]["vector_ekf"]["rmse_deg"]
        for method in ("gyro", "complementary", "kalman"):
            assert metrics[method] == summary["scenarios"][name]["baselines"][method]["rmse_deg"]
        # Display RMSE is intentionally not substituted for the source metric.
        display_rmse = float(np.sqrt(np.mean((rows[:, 5] - rows[:, 1]) ** 2)))
        assert abs(display_rmse - metrics["ekf"]) > 1e-4
    nominal, pulse = data["scenarios"].values()
    np.testing.assert_array_equal(np.array(nominal["rows"])[:, [0, 1, 2, 6]],
                                  np.array(pulse["rows"])[:, [0, 1, 2, 6]])
    assert nominal["disturbance"] is None
    assert pulse["disturbance"] == {"axis": "body_y", "start_s": .4, "end_s": .7, "value_m_s2": 2.0}
    assert all(isinstance(value, str) for value in data["provenance"]["stream_seeds"].values())


def test_export_is_reproducible_and_does_not_include_machine_paths(experiment, tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    export_comparison(experiment, first)
    export_comparison(experiment, second)
    assert first.read_bytes() == second.read_bytes()
    assert str(experiment) not in first.read_text()
    data = json.loads(first.read_text())
    assert len(data["provenance"]["source_sha256"]) == 7
    assert all(not Path(key).is_absolute() for key in data["provenance"]["source_sha256"])
    assert len(data["scenarios"]["nominal"]["rows"]) == 21
    with pytest.raises(FileExistsError):
        export_comparison(experiment, first)


@pytest.mark.parametrize("stride", [0, -1, True, 1.5])
def test_invalid_stride_is_rejected(experiment, stride):
    with pytest.raises(ValueError, match="stride"):
        build_comparison(experiment, stride=stride)


@pytest.mark.parametrize("mutation", ["missing_column", "nan", "infinity", "duplicate", "misaligned", "missing_row"])
def test_bad_endpoint_records_are_rejected(mutable_source, tmp_path, mutation):
    path = mutable_source / "nominal" / "ekf_estimates.csv"
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    if mutation == "missing_column":
        rows[0][1] = "unknown_roll"
    elif mutation in ("nan", "infinity"):
        rows[4][1] = "nan" if mutation == "nan" else "inf"
    elif mutation == "duplicate":
        rows[4][0] = rows[3][0]
    elif mutation == "misaligned":
        rows[4][0] = "0.031"
    else:
        rows.pop()
    with path.open("w", newline="") as stream:
        csv.writer(stream).writerows(rows)
    output = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        export_comparison(mutable_source, output)
    assert not output.exists()


@pytest.mark.parametrize("metric", ["rmse_deg", "final_bias_error_deg_s"])
def test_mismatched_summary_is_rejected(mutable_source, metric):
    path = mutable_source / "summary.json"
    summary = json.loads(path.read_text())
    summary["scenarios"]["translation_pulse"]["vector_ekf"][metric] += .1
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="mismatch"):
        build_comparison(mutable_source)


def test_changed_source_timing_is_rejected(mutable_source):
    path = mutable_source / "summary.json"
    summary = json.loads(path.read_text())
    summary["config"]["sample_rate_hz"] = 50
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="schedule"):
        build_comparison(mutable_source)


@pytest.mark.parametrize("field", ["bias_deg_s", "amplitude_deg", "frequency_hz"])
def test_truth_configuration_mismatch_is_rejected(mutable_source, field):
    path = mutable_source / "summary.json"
    summary = json.loads(path.read_text())
    summary["config"][field] += 1
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="configuration"):
        build_comparison(mutable_source)


def test_initialization_mismatch_is_rejected(mutable_source):
    path = mutable_source / "summary.json"
    summary = json.loads(path.read_text())
    summary["initialization"]["bias_rad_s"] = .1
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="initial states"):
        build_comparison(mutable_source)


def test_cli_errors_do_not_create_output(experiment, tmp_path, capsys):
    output = tmp_path / "result.json"
    assert main([str(experiment), "--output", str(output)]) == 0
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        main([str(experiment), "--output", str(output)])
    assert error.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == before
