"""Check timing discontinuities and exports without requiring private recordings."""

import csv
import hashlib
import json
import math

import pytest

from meridian import dataflash_audit
from meridian.dataflash import DataFlashData, ImuRecord
from meridian.dataflash_audit import analyze_records, audit_file


def record(time_us, instance=0, *, gyro=(0.0, 0.0, 0.0), accel=(0.0, 0.0, -9.80665)):
    return ImuRecord(time_us, instance, gyro, accel)


def test_interleaved_instances_keep_order_and_do_not_bridge_discontinuities():
    records = [record(1_000_000), record(100, 1), record(1_040_000), record(200_100, 1),
               record(2_000_000), record(2_000_000), record(1_900_000),
               record(1_940_000, gyro=(math.nan, 0, 0)), record(1_980_000), record(2_020_000)]
    report, labels = analyze_records(records)
    assert labels == [0, 0, 0, 0, 1, 2, 3, None, 4, 4]
    assert report["0"]["duplicate_intervals"] == 1
    assert report["0"]["backward_intervals"] == 1
    assert report["0"]["nonfinite_sensor_records"] == 1
    assert report["0"]["gaps"] == [{"previous_record_index": 2, "next_record_index": 4,
                                   "previous_time_us": 1_040_000, "next_time_us": 2_000_000,
                                   "duration_s": 0.96}]
    assert report["0"]["within_segment_duration_s"] == pytest.approx(0.08)
    assert report["1"]["within_segment_duration_s"] == pytest.approx(0.2)
    assert report["1"]["gaps"] == []  # Equality with the threshold stays in the same run.
    assert [s["samples"] for s in report["0"]["segments"]] == [2, 1, 1, 1, 2]


def test_timestamp_differences_preserve_integer_precision():
    start = 2**60
    report, labels = analyze_records([record(start), record(start + 1)])
    assert report["0"]["median_positive_dt_s"] == 1e-6
    assert report["0"]["within_segment_duration_s"] == 1e-6
    assert labels == [0, 0]


def test_undefined_tilt_and_invalid_values_are_counted_without_nonstandard_json():
    report, labels = analyze_records([record(0, accel=(0, 0, 0)),
                                     record(40_000, accel=(math.inf, math.nan, 0))])
    stream = report["0"]
    assert stream["apparent_roll_principal_deg"]["count"] == 0
    assert stream["apparent_roll_principal_deg"]["min"] is None
    assert stream["accel_norm_m_s2"]["count"] == 1
    assert labels == [0, None]
    json.dumps(report, allow_nan=False)


def test_principal_roll_bounds_do_not_report_a_false_arithmetic_mean():
    report, _ = analyze_records([record(0, accel=(0, -0.1, 9.8)), record(40_000, accel=(0, 0.1, 9.8))])
    bounds = report["0"]["apparent_roll_principal_deg"]
    assert bounds["min"] < -179 and bounds["max"] > 179
    assert "mean" not in bounds and "std" not in bounds


@pytest.mark.parametrize("threshold", [0, -1, math.inf, math.nan])
def test_invalid_gap_threshold(threshold):
    with pytest.raises(ValueError, match="gap_threshold"):
        analyze_records([record(0)], gap_threshold_s=threshold)


def test_empty_input_is_rejected():
    with pytest.raises(ValueError, match="no IMU"):
        analyze_records([])


def test_audit_exports_original_order_and_source_hash_reproducibly(tmp_path, monkeypatch):
    source = tmp_path / "private-user-name.bin"
    source.write_bytes(b"fixture source; decoding tested independently")
    original = source.read_bytes()
    records = [record(1_000_000), record(1_040_000), record(3_000_000), record(2_900_000, 1)]
    metadata = {"pymavlink_version": "fixture", "parser_diagnostic_count": 0}
    monkeypatch.setattr(dataflash_audit, "read_dataflash", lambda _: DataFlashData(records, metadata))
    first, second = tmp_path / "first", tmp_path / "second"
    summary = audit_file(source, first)
    assert audit_file(source, second) == summary
    assert summary["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    assert source.name not in (first / "summary.json").read_text()
    assert str(source.parent) not in (first / "summary.json").read_text()
    for name in ("summary.json", "imu.csv", "overview.png"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    with (first / "imu.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [int(row["time_us"]) for row in rows] == [r.time_us for r in records]
    assert [row["segment_id"] for row in rows] == ["0", "0", "1", "0"]
    with pytest.raises(FileExistsError):
        audit_file(source, first)


def test_decode_failure_leaves_no_output_directory(tmp_path, monkeypatch):
    source, output = tmp_path / "recording.bin", tmp_path / "out"
    source.write_bytes(b"unsupported")
    def fail(_):
        raise ValueError("unsupported IMU units")
    monkeypatch.setattr(dataflash_audit, "read_dataflash", fail)
    with pytest.raises(ValueError, match="unsupported"):
        audit_file(source, output)
    assert not output.exists()


def test_source_change_during_read_is_rejected(tmp_path, monkeypatch):
    source, output = tmp_path / "recording.bin", tmp_path / "out"
    source.write_bytes(b"before")
    def changed(path):
        path.write_bytes(b"after")
        return DataFlashData([record(0)], {})
    monkeypatch.setattr(dataflash_audit, "read_dataflash", changed)
    with pytest.raises(ValueError, match="changed"):
        audit_file(source, output)
    assert not output.exists()
