"""Check reproducible exports and command-line failure behavior."""

import json

import numpy as np
import pytest

from meridian.gyro_drift import main, run_experiment


def test_exports_reproduce_and_preserve_time_alignment(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    options = dict(duration_s=2.0, sample_rate_hz=50.0, frequency_hz=0.3, seed=13)
    summary = run_experiment(first, **options)
    assert run_experiment(second, **options) == summary
    for name in ("measurements.csv", "truth.csv", "estimates.csv", "summary.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert (first / "overview.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    measurements = np.genfromtxt(first / "measurements.csv", delimiter=",", names=True)
    estimates = np.genfromtxt(first / "estimates.csv", delimiter=",", names=True)
    truth = np.genfromtxt(first / "truth.csv", delimiter=",", names=True)
    np.testing.assert_array_equal(estimates["time_s"], truth["time_s"])
    np.testing.assert_array_equal(estimates["time_s"][:-1], measurements["t_start_s"])
    np.testing.assert_array_equal(estimates["time_s"][1:], measurements["t_end_s"])
    assert "roll_rad" not in measurements.dtype.names
    assert summary["interval_count"] == len(measurements) == 100
    # A half-degree/second bias contributes exactly one degree after two seconds.
    assert summary["metrics"]["bias_only"]["final_error_deg"] == pytest.approx(1.0)
    assert summary["metrics"]["ideal"]["max_abs_error_deg"] < 1e-10
    assert json.loads((first / "summary.json").read_text()) == summary


def test_existing_directory_is_preserved(tmp_path):
    sentinel = tmp_path / "existing.txt"
    sentinel.write_text("Keep this run unchanged.\n")
    with pytest.raises(FileExistsError):
        run_experiment(tmp_path, duration_s=1.0)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "Keep this run unchanged.\n"


def test_invalid_cli_input_creates_no_results(tmp_path, capsys):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as failure:
        main(["--sample-rate-hz", "0", "--output", str(output)])
    assert failure.value.code == 2
    assert "sample_rate_hz" in capsys.readouterr().err
    assert not output.exists()
