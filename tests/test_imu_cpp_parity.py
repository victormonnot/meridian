"""Check the recorded-snapshot event bridge with synthetic, public test inputs."""

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from meridian import imu_cpp_parity, imu_replay
from meridian.cpp_parity import python_trace, serialize_events
from meridian.dataflash import DataFlashData, ImuRecord
from meridian.imu_cpp_parity import check_snapshot_link, replay_events, run_comparison
from meridian.replay import REPLAY_GRAVITY_M_S2, ReplayConfig


def _source(tmp_path, monkeypatch, *, start=2_000_000):
    """Keep instance/segment choice observable through nonconsecutive row indices."""
    def row(time, rate, angle, *, instance=0, scale=1.0):
        gravity = REPLAY_GRAVITY_M_S2 * scale
        return ImuRecord(time, instance, (rate, 5.0, -8.0),
                         (3.2, -gravity * math.sin(angle), -gravity * math.cos(angle)))

    selected = [row(start, .3, .25), row(start + 40_001, -.4, .26, scale=1.015),
                row(start + 130_003, .12, .23, scale=.985),
                row(start + 180_007, 10_000.0, .29)]
    records = [row(start - 500_000, 90.0, -.4), selected[0],
               row(start + 100, 88.0, -.3, instance=1), selected[1],
               row(start + 50_000, 77.0, -.2, instance=1), *selected[2:]]
    source = tmp_path / "source-fixture.bin"
    source.write_bytes(b"synthetic snapshot source; decoder tested separately")
    metadata = {"pymavlink_version": "fixture", "parser_diagnostic_count": 0,
                "unparsed_tail_bytes": 0}
    monkeypatch.setattr(imu_replay, "read_dataflash", lambda _: DataFlashData(records, metadata))
    # Plot generation is covered by the replay tests; retain a deterministic artifact here.
    monkeypatch.setattr(imu_replay, "_plot", lambda result, path: path.write_bytes(b"plot fixture"))
    return source, selected


def _replay(tmp_path, monkeypatch, *, start=2_000_000, config=ReplayConfig()):
    source, records = _source(tmp_path, monkeypatch, start=start)
    directory = tmp_path / "python_replay"
    summary = imu_replay.run_replay(source, directory, instance=0, segment=1, config=config)
    return directory, summary, records


def _rewrite_cell(path, row_index, column, value):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames, rows = reader.fieldnames, list(reader)
    rows[row_index][column] = str(value)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(scope="module")
def binary():
    value = os.environ.get("MERIDIAN_CPP_BINARY")
    if not value:
        pytest.skip("C++ integration requires MERIDIAN_CPP_BINARY; see docs/cpp-ekf.md")
    path = Path(value).resolve()
    assert path.is_file(), f"configured C++ executable is missing: {path}"
    return path


@pytest.mark.parametrize("start", [2_000_000, np.iinfo(np.int64).max - 300_000])
def test_events_preserve_integer_times_previous_hold_and_single_initialization(tmp_path, monkeypatch, start):
    config = ReplayConfig(gyro_noise_std_deg_s=.7, angle_noise_std_deg=3.0,
                          initial_bias_std_deg_s=.4, complementary_tau_s=2.0)
    directory, summary, records = _replay(tmp_path, monkeypatch, start=int(start), config=config)
    settings, events, timeline = replay_events(directory, summary)
    assert settings == {
        "initial_angle_rad": pytest.approx(
            math.atan2(-records[0].accel_m_s2[1], -records[0].accel_m_s2[2]), rel=0, abs=1e-15),
        "initial_bias_rad_s": 0.0,
        "initial_angle_std_rad": math.radians(3.0),
        "initial_bias_std_rad_s": math.radians(.4),
        "gyro_noise_std_rad_s": math.radians(.7),
        "accel_noise_std_m_s2": REPLAY_GRAVITY_M_S2 * math.radians(3.0),
        "gravity_m_s2": REPLAY_GRAVITY_M_S2,
    }
    assert events == [
        ("predict", .3, .040001), ("update", *records[1].accel_m_s2[1:]),
        ("predict", -.4, .090002), ("update", *records[2].accel_m_s2[1:]),
        ("predict", .12, .050004), ("update", *records[3].accel_m_s2[1:]),
    ]
    record_indices = [1, 3, 5, 6]
    expected = [{"step": 0, "operation": "init", "endpoint_record_index": 1,
                 "endpoint_time_us": int(start), "input_record_index": 1,
                 "input_time_us": int(start)}]
    for k in range(1, len(records)):
        for step, operation, input_k in ((2 * k - 1, "predict", k - 1), (2 * k, "update", k)):
            expected.append({"step": step, "operation": operation,
                             "endpoint_record_index": record_indices[k],
                             "endpoint_time_us": records[k].time_us,
                             "input_record_index": record_indices[input_k],
                             "input_time_us": records[input_k].time_us})
    assert timeline == expected
    operations, values = python_trace(settings, serialize_events(events))
    assert operations == ["init", "predict", "update", "predict", "update", "predict", "update"]
    assert values[0, 0] == settings["initial_angle_rad"]
    np.testing.assert_array_equal(values[0, 2:6],
                                  [math.radians(3.0)**2, 0, 0, math.radians(.4)**2])
    linked = check_snapshot_link(directory, operations, values)
    assert linked["passed"] and linked["record_count"] == 4
    assert linked["quantities"]["angle_rad"]["compared_values"] == 4
    assert linked["quantities"]["v0"]["compared_values"] == 3


@pytest.mark.parametrize("column", [0, 3, 6, 9])
def test_snapshot_link_detects_state_covariance_innovation_and_s_changes(tmp_path, monkeypatch, column):
    directory, summary, _ = _replay(tmp_path, monkeypatch)
    settings, events, _ = replay_events(directory, summary)
    operations, values = python_trace(settings, serialize_events(events))
    values[2, column] += 1e-4
    assert not check_snapshot_link(directory, operations, values)["passed"]


@pytest.mark.parametrize("filename,column,value", [
    ("measurements.csv", "time_us", "2040001.0"),
    ("measurements.csv", "time_us", "2000000"),
    ("measurements.csv", "time_us", "1999999"),
    ("measurements.csv", "record_index", "1"),
    ("measurements.csv", "instance", "1"),
    ("measurements.csv", "segment_id", "0"),
])
def test_event_bridge_refuses_corrupted_selection_or_integer_timeline(tmp_path, monkeypatch, filename, column, value):
    directory, summary, _ = _replay(tmp_path, monkeypatch)
    _rewrite_cell(directory / filename, 1, column, value)
    with pytest.raises(ValueError):
        replay_events(directory, summary)


@pytest.mark.parametrize("mutation", ["operation", "record_count", "state_timestamp", "innovation_timestamp"])
def test_snapshot_link_refuses_structural_mismatch(tmp_path, monkeypatch, mutation):
    directory, summary, _ = _replay(tmp_path, monkeypatch)
    settings, events, _ = replay_events(directory, summary)
    operations, values = python_trace(settings, serialize_events(events))
    if mutation == "operation":
        operations[1] = "update"
    elif mutation == "record_count":
        operations, values = operations[:-2], values[:-2]
    elif mutation == "state_timestamp":
        _rewrite_cell(directory / "ekf_estimates.csv", 1, "time_us", 2_040_002)
    else:
        _rewrite_cell(directory / "ekf_innovations.csv", 0, "time_us", 2_040_002)
    with pytest.raises(ValueError):
        check_snapshot_link(directory, operations, values)


def test_existing_output_is_preserved_before_any_replay(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"retain existing results")
    with pytest.raises(FileExistsError):
        run_comparison(tmp_path / "missing.bin", tmp_path / "missing-executable", output,
                       instance=0, segment=1)
    assert [path.name for path in output.iterdir()] == ["keep.txt"]
    assert sentinel.read_bytes() == b"retain existing results"


def test_native_recorded_replay_matches_each_operation_and_retains_provenance(binary, tmp_path, monkeypatch):
    source, _ = _source(tmp_path, monkeypatch)
    original = source.read_bytes()
    output, repeat = tmp_path / "first", tmp_path / "second"
    summary = run_comparison(source, binary, output, instance=0, segment=1)
    assert summary["passed"] and summary["comparison"]["passed"] and summary["snapshot_link"]["passed"]
    assert summary["prediction_count"] == summary["correction_count"] == 3
    assert summary["comparison"]["record_count"] == 7
    assert summary["snapshot_link"]["record_count"] == 4
    assert summary["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert summary["binary"]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert summary["binary"]["protocol_version"] == 1
    assert str(tmp_path) not in json.dumps(summary)
    assert json.loads((output / "summary.json").read_text()) == summary
    for name, digest in summary["artifact_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert run_comparison(source, binary, repeat, instance=0, segment=1) == summary
    first_files = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    repeat_files = {path.relative_to(repeat): path.read_bytes() for path in repeat.rglob("*") if path.is_file()}
    assert first_files == repeat_files
    assert source.read_bytes() == original


def test_numerical_mismatch_is_reported_and_cli_returns_failure(binary, tmp_path, monkeypatch):
    source, _ = _source(tmp_path, monkeypatch)
    original_reader = imu_cpp_parity.read_cpp_trace

    def changed_trace(path, operations):
        values = original_reader(path, operations)
        values[1, 0] += 1e-4
        return values

    monkeypatch.setattr(imu_cpp_parity, "read_cpp_trace", changed_trace)
    output = tmp_path / "mismatch"
    assert imu_cpp_parity.main([str(source), "--binary", str(binary), "--instance", "0",
                                "--segment", "1", "--output", str(output)]) == 1
    summary = json.loads((output / "summary.json").read_text())
    assert not summary["passed"] and not summary["comparison"]["passed"]
    assert summary["snapshot_link"]["passed"]
    assert not summary["comparison"]["quantities"]["angle_rad"]["passed"]


@pytest.mark.parametrize("protocol", [2, True])
def test_unsupported_or_boolean_protocol_does_not_create_results(tmp_path, monkeypatch, protocol):
    source, _ = _source(tmp_path, monkeypatch)
    binary = tmp_path / "native-fixture"
    binary.write_bytes(b"mock executable")

    def version(command, **kwargs):
        assert command[1:] == ["--version"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"protocol_version": protocol}), "")

    monkeypatch.setattr(imu_cpp_parity.subprocess, "run", version)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="protocol"):
        run_comparison(source, binary, output, instance=0, segment=1)
    assert not output.exists()


def test_failed_native_replay_does_not_write_a_success_summary(tmp_path, monkeypatch):
    source, _ = _source(tmp_path, monkeypatch)
    binary = tmp_path / "native-fixture"
    binary.write_bytes(b"mock executable")

    def native(command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, '{"protocol_version":1}', "")
        raise subprocess.CalledProcessError(7, command, stderr="synthetic native failure")

    monkeypatch.setattr(imu_cpp_parity.subprocess, "run", native)
    output = tmp_path / "failed"
    with pytest.raises(SystemExit) as caught:
        imu_cpp_parity.main([str(source), "--binary", str(binary), "--instance", "0",
                            "--segment", "1", "--output", str(output)])
    assert caught.value.code == 2
    assert not (output / "summary.json").exists()
    assert not (output / "cpp_trace.csv").exists()


@pytest.mark.parametrize("changed", [
    "source", "binary", "events", "snapshot", "timeline", "python_trace", "replay_summary",
])
def test_changes_during_native_execution_are_detected(binary, tmp_path, monkeypatch, changed):
    source, _ = _source(tmp_path, monkeypatch)
    local_binary = tmp_path / "native-copy"
    shutil.copy2(binary, local_binary)
    output = tmp_path / "changed"
    original_run = subprocess.run

    def native(command, **kwargs):
        completed = original_run(command, **kwargs)
        if "--input" in command:
            target = {"source": source, "binary": local_binary,
                      "events": output / "events.csv",
                      "snapshot": output / "python_replay" / "ekf_estimates.csv",
                      "timeline": output / "timeline.csv",
                      "python_trace": output / "python_trace.csv",
                      "replay_summary": output / "python_replay" / "summary.json"}[changed]
            with target.open("ab") as stream:
                stream.write(b"changed after native replay")
        return completed

    monkeypatch.setattr(imu_cpp_parity.subprocess, "run", native)
    with pytest.raises(ValueError, match="changed"):
        run_comparison(source, local_binary, output, instance=0, segment=1)
    assert not (output / "summary.json").exists()


@pytest.mark.parametrize("error", [ValueError("unsupported event protocol"),
                                   subprocess.TimeoutExpired("native replay", 30)])
def test_cli_reports_runtime_errors_with_exit_two(tmp_path, monkeypatch, error):
    def failed(*args, **kwargs):
        raise error

    monkeypatch.setattr(imu_cpp_parity, "run_comparison", failed)
    with pytest.raises(SystemExit) as caught:
        imu_cpp_parity.main([str(tmp_path / "input.bin"), "--binary", str(tmp_path / "native"),
                            "--instance", "0", "--segment", "0", "--output", str(tmp_path / "out")])
    assert caught.value.code == 2
