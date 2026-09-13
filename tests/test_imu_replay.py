"""Verify segment selection and complete local exports without private log data."""

import csv
import hashlib
import json
import math

import numpy as np
import pytest

from meridian import imu_replay
from meridian.dataflash import DataFlashData, ImuRecord
from meridian.imu_replay import main, run_replay, select_segment
from meridian.replay import ReplayConfig


def records():
    def row(time, instance=0, gyro=0.05):
        return ImuRecord(time, instance, (gyro, 0.0, 0.0), (0.0, 0.0, -9.81))
    return [row(1_000_000, gyro=5), row(1_020_000, 1), row(1_040_000, gyro=5),
            row(2_000_000), row(2_020_000, 1), row(2_040_000, gyro=99)]


def source_fixture(tmp_path, monkeypatch):
    source = tmp_path / "private-source-name.bin"
    source.write_bytes(b"a source fixture; binary decoding is tested separately")
    metadata = {"pymavlink_version": "fixture", "parser_diagnostic_count": 0, "unparsed_tail_bytes": 0}
    monkeypatch.setattr(imu_replay, "read_dataflash", lambda _: DataFlashData(records(), metadata))
    return source


def test_explicit_selection_keeps_instance_and_gap_boundaries():
    indices, selected = select_segment(records(), instance=0, segment=1, max_gap_s=0.2)
    assert indices == [3, 5]
    assert selected["start_time_us"] == 2_000_000
    assert selected["duration_s"] == 0.04
    with pytest.raises(ValueError, match="two IMU"):
        select_segment(records(), instance=1, segment=0, max_gap_s=0.2)


@pytest.mark.parametrize("instance,segment", [(9, 0), (0, 9), (-1, 0), (0, -1), (True, 0), (0, 1.5)])
def test_selection_requires_existing_integer_ids(instance, segment):
    with pytest.raises(ValueError):
        select_segment(records(), instance=instance, segment=segment, max_gap_s=0.2)


def test_exports_reproduce_selection_and_preserve_source(tmp_path, monkeypatch):
    source = source_fixture(tmp_path, monkeypatch)
    original = source.read_bytes()
    first, second = tmp_path / "first", tmp_path / "second"
    summary = run_replay(source, first, instance=0, segment=1)
    assert run_replay(source, second, instance=0, segment=1) == summary
    assert source.read_bytes() == original
    assert summary["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert summary["sampling"]["propagations_and_corrections"] == 1
    assert summary["initialization"]["covariance"][0][0] == pytest.approx(np.deg2rad(2)**2)
    assert source.name not in json.dumps(summary) and str(source.parent) not in json.dumps(summary)
    for name in ("summary.json", "measurements.csv", "estimates.csv", "innovations.csv", "overview.png"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        if name != "summary.json":
            assert summary["artifact_sha256"][name] == hashlib.sha256((first / name).read_bytes()).hexdigest()
    with (first / "measurements.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [int(row["record_index"]) for row in rows] == [3, 5]
    assert [int(row["time_us"]) for row in rows] == [2_000_000, 2_040_000]
    assert [float(row["gyro_x_rad_s"]) for row in rows] == [0.05, 99]
    with (first / "estimates.csv").open(newline="") as stream:
        estimates = list(csv.DictReader(stream))
    assert float(estimates[-1]["gyro_roll_rad"]) == pytest.approx(0.05 * 0.04)
    with (first / "innovations.csv").open(newline="") as stream:
        innovations = list(csv.DictReader(stream))
    assert len(innovations) == 1 and innovations[0]["time_us"] == "2040000"
    with pytest.raises(FileExistsError):
        run_replay(source, first, instance=0, segment=1)


def test_selection_failure_does_not_create_output(tmp_path, monkeypatch):
    source = source_fixture(tmp_path, monkeypatch)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="does not exist"):
        run_replay(source, output, instance=0, segment=8)
    assert not output.exists()


def test_source_change_during_read_is_refused(tmp_path, monkeypatch):
    source = source_fixture(tmp_path, monkeypatch)
    def changed(path):
        path.write_bytes(b"changed")
        return DataFlashData(records(), {})
    monkeypatch.setattr(imu_replay, "read_dataflash", changed)
    with pytest.raises(ValueError, match="source file changed"):
        run_replay(source, tmp_path / "out", instance=0, segment=1)
    assert not (tmp_path / "out").exists()


def test_cli_exposes_tuning_and_refuses_bad_arguments(tmp_path, monkeypatch, capsys):
    source = source_fixture(tmp_path, monkeypatch)
    output = tmp_path / "out"
    argv = [str(source), "--instance", "0", "--segment", "1", "--output", str(output),
            "--angle-noise-std-deg", "3", "--complementary-tau-s", "2"]
    assert main(argv) == 0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["config"]["angle_noise_std_deg"] == 3
    assert summary["config"]["complementary_tau_s"] == 2
    assert "no independent ground truth" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main([*argv, "--max-gap-s", "nan"])
    assert error.value.code == 2


def test_small_real_binary_fixture_runs_through_decoder_and_replay(tmp_path):
    pytest.importorskip("pymavlink")
    from test_dataflash_reader import _binary, _imu
    source = tmp_path / "synthetic.bin"
    source.write_bytes(_binary(*[_imu(1_000_000 + k * 40_000, gyro=(0.01, 0, 0)) for k in range(30)]))
    summary = run_replay(source, tmp_path / "out", instance=0, segment=0)
    assert summary["selection"]["samples"] == 30
    assert summary["metadata"]["imu_format"]["units"]["GyrX"] == "rad/s"
    assert summary["diagnostics"]["gyro_angle_change_deg"] == pytest.approx(math.degrees(0.01 * 29 * 0.04))
