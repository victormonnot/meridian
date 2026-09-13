"""Verify the native event boundary and every-operation Python/C++ agreement."""

import csv
import io
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from meridian.cpp_parity import (
    CASE_NAMES, TRACE_HEADER, compare_case, compare_traces, python_trace,
    read_cpp_trace, replay_command, run_experiment, scenario_events, serialize_events,
)
from meridian.ekf_experiment import FusionConfig, run_trial


@pytest.fixture(scope="module")
def binary():
    value = os.environ.get("MERIDIAN_CPP_BINARY")
    if not value:
        pytest.skip("C++ integration requires MERIDIAN_CPP_BINARY; see docs/cpp-ekf.md")
    path = Path(value).resolve()
    assert path.is_file(), f"configured C++ executable is missing: {path}"
    return path


def test_serialized_reference_retains_existing_python_endpoint_states():
    config, events = scenario_events("nominal", 42)
    serialized = serialize_events(events)
    operations, values = python_trace(config, serialized)
    rows = list(csv.reader(io.StringIO(serialized)))
    assert rows[0] == ["operation", "value0", "value1"]
    assert all((row[0], float(row[1]), float(row[2])) == event for row, event in zip(rows[1:], events))
    endpoint_rows = [0] + [i for i in range(1, len(operations))
                            if i == len(operations) - 1 or operations[i + 1] != "update"]
    previous = run_trial(FusionConfig(), 42)["ekf"]
    np.testing.assert_array_equal(values[endpoint_rows, :2], previous["states"])
    np.testing.assert_array_equal(values[endpoint_rows, 2:6], previous["covariance"].reshape(-1, 4))
    np.testing.assert_array_equal(values[np.array(operations) == "update", 6:8], previous["innovations"])


@pytest.mark.parametrize("column", range(12))
def test_comparison_detects_errors_in_every_state_and_matrix_entry(column):
    reference = np.zeros((1, 12))
    actual = reference.copy()
    actual[0, column] = 1e-6
    result = compare_traces(reference, actual)
    assert not result["passed"]
    assert sum(not value["passed"] for value in result["quantities"].values()) == 1


@pytest.mark.parametrize("mutation", ["row_count", "step", "operation", "missing_update", "spurious_prediction", "infinity"])
def test_trace_reader_rejects_structural_and_numeric_corruption(tmp_path, mutation):
    rows = [list(TRACE_HEADER), ["0", "init", *(["0"] * 6), *([""] * 6)],
            ["1", "update", *(["0"] * 12)]]
    if mutation == "row_count":
        rows.pop()
    elif mutation == "step":
        rows[2][0] = "2"
    elif mutation == "operation":
        rows[2][1] = "predict"
    elif mutation == "missing_update":
        rows[2][8] = ""
    elif mutation == "spurious_prediction":
        rows[1][8] = "0"
    else:
        rows[2][2] = "inf"
    path = tmp_path / "trace.csv"
    with path.open("w", newline="") as stream:
        csv.writer(stream).writerows(rows)
    with pytest.raises(ValueError):
        read_cpp_trace(path, ["init", "update"])


@pytest.mark.parametrize("case", CASE_NAMES)
def test_complete_scenario_agreement_after_every_operation(binary, tmp_path, case):
    result = compare_case(binary, case, 42, tmp_path / case)
    assert result["passed"]
    assert result["prediction_count"] == 3000
    assert result["correction_count"] == (250 if case == "accel_dropout" else 300)
    assert result["record_count"] == 1 + result["prediction_count"] + result["correction_count"]
    for name, check in result["quantities"].items():
        assert check["compared_values"] == (result["correction_count"] if name.startswith(("v", "s")) else result["record_count"])


def _run(binary, tmp_path, contents, *, config=None, extra=()):
    if config is None:
        config, _ = scenario_events("nominal", 42)
    source, output = tmp_path / "events.csv", tmp_path / "out.csv"
    source.write_bytes(contents.encode("ascii"))
    command = replay_command(binary, config, source, output) + list(extra)
    return subprocess.run(command, capture_output=True, text=True, timeout=30), output


@pytest.mark.parametrize("bad_row", [
    "predict,0,0", "predict,0,-0.1", "predict,nan,0.1", "update,inf,0",
    "predict,0,0.1junk", "predict,0", "update,0,-9.8,extra", "observe,0,-9.8",
    'update,"0",-9.8', "update,0,-9.8,", "update,0,",
])
def test_bad_late_event_does_not_create_output(binary, tmp_path, bad_row):
    process, output = _run(binary, tmp_path, "operation,value0,value1\npredict,0,0.01\n" + bad_row + "\n")
    assert process.returncode != 0 and process.stderr
    assert not output.exists()


def test_crlf_diagnostics_and_existing_output_preservation(binary, tmp_path):
    contents = "operation,value0,value1\r\npredict,0,0.01\r\nupdate,0,-9.80665\r\n"
    process, output = _run(binary, tmp_path, contents)
    assert process.returncode == 0, process.stderr
    values = read_cpp_trace(output, ["init", "predict", "update"])
    np.testing.assert_array_equal(values[:, :2], np.zeros((3, 2)))
    assert np.all(np.isnan(values[:2, 6:]))
    assert np.all(np.isfinite(values[2, 6:]))
    before = output.read_bytes()
    process, _ = _run(binary, tmp_path, contents)
    assert process.returncode != 0
    assert output.read_bytes() == before


def test_header_only_protocol_and_required_options(binary, tmp_path):
    process, output = _run(binary, tmp_path, "operation,value0,value1\n")
    assert process.returncode == 0, process.stderr
    assert read_cpp_trace(output, ["init"]).shape == (1, 12)
    output.unlink()
    config, _ = scenario_events("nominal", 42)
    config.pop("gravity_m_s2")
    process, output = _run(binary, tmp_path, "operation,value0,value1\n", config=config)
    assert process.returncode != 0 and not output.exists()


@pytest.mark.parametrize("extra", [("--unknown", "1"), ("--gravity-m-s2", "9.8")])
def test_unknown_or_duplicate_option_is_refused(binary, tmp_path, extra):
    process, output = _run(binary, tmp_path, "operation,value0,value1\n", extra=extra)
    assert process.returncode != 0 and not output.exists()


def test_full_report_aggregates_and_preserves_existing_directory(binary, tmp_path):
    output = tmp_path / "report"
    summary = run_experiment(binary, output, validation_seeds=1)
    assert summary["passed"]
    assert summary["aggregate"]["trace_count"] == 18
    assert summary["aggregate"]["record_count"] == 2 * (8 * 3301 + 3251)
    assert json.loads((output / "summary.json").read_text()) == summary
    assert len([path for path in output.rglob("*") if path.is_file()]) == 28
    assert summary["binary"]["protocol_version"] == 1
    assert summary["binary"]["cxx_standard"] == 17
    assert summary["binary"]["eigen_version"]
    assert "compiler_version" in summary["binary"]
    before = (output / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_experiment(binary, output, validation_seeds=1)
    assert (output / "summary.json").read_bytes() == before
