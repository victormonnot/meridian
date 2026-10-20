"""Independent import checks on tiny, real DataFlash binary fixtures."""

import copy
import csv
import hashlib
import json
import os

import pytest

from meridian import sensor_dataflash
from meridian.dataflash import ImuRecord, read_dataflash
from meridian.sensor_dataflash import build_dataflash_package
from meridian.sensor_package import validate_package
from test_dataflash_reader import _binary, _imu, _packet, _MSG, _ISBH


def _declaration(instance):
    return {
        "sensor_instance": f"fixture-imu-{instance}",
        "frame_id": f"sensor{instance}",
        "axes": None,
        "calibration": "unknown",
        "filtering": "unknown",
        "clock_correction": "unknown",
        "description": "Synthetic IMU messages; physical frontend processing is not established.",
    }


def _arguments(*instances):
    return {
        "acquisition_id": "dataflash-fixture",
        "rig": {"id": "fixture-rig", "revision": "one"},
        "provenance": {
            "source": "locally generated DataFlash fixture",
            "license": "MIT", "attribution": "Meridian fixture",
            "session_id": "fixture-one", "synthetic": True,
        },
        "instances": {instance: _declaration(instance) for instance in (instances or (0,))},
    }


def _source(tmp_path, data=None):
    source = tmp_path / "private-name.bin"
    source.write_bytes(_binary(_imu()) if data is None else data)
    return source


def _rows(root, stream):
    with (root / stream["index_path"]).open(newline="") as source:
        return list(csv.DictReader(source))


def test_preserves_all_instances_global_row_identity_units_and_reset_boundaries(tmp_path):
    original = _binary(
        _imu(1_000_000, 1), _imu(1_000_000, 0),
        _imu(1_040_000, 1), _imu(1_040_000, 0),
        _imu(1_040_000, 1), _imu(900_000, 1),
        _imu(940_000, 1), _imu(1_140_000, 1),
        _imu(1_340_001, 1), _imu(1_080_000, 0),
    )
    source, output = _source(tmp_path, original), tmp_path / "package"
    acquisition = build_dataflash_package(source, output, **_arguments(0, 1))
    assert source.read_bytes() == original
    assert (output / "originals/source.bin").read_bytes() == original
    assert acquisition == validate_package(output)["acquisition"]
    assert acquisition["clocks"] == [{"id": "dataflash-logger", "unit": "us", "epoch": "unknown"}]
    assert [stream["id"] for stream in acquisition["streams"]] == ["imu0", "imu1"]
    decoded = read_dataflash(source).records
    for stream in acquisition["streams"]:
        instance = int(stream["id"][3:])
        rows = _rows(output, stream)
        expected = [(index, row) for index, row in enumerate(decoded) if row.instance == instance]
        assert [row["sample_id"] for row in rows] == [str(index) for index, _ in expected]
        for row, (_, record) in zip(rows, expected, strict=True):
            assert row["timestamp"] == str(record.time_us)
            assert tuple(float(row[key]) for key in ("wx", "wy", "wz")) == record.gyro_rad_s
            assert tuple(float(row[key]) for key in ("fx", "fy", "fz")) == record.accel_m_s2
        assert stream["timestamp_semantics"] == "message_write"
        assert stream["measurement_semantics"] == "filtered_snapshot"
        assert stream["imu"] == {
            "angular_velocity_unit": "rad/s", "specific_force_unit": "m/s^2", "local_gravity_m_s2": None,
        }
        assert stream["processing"]["calibration"] == "unknown"
    stream1 = acquisition["streams"][1]
    assert [segment["sample_count"] for segment in stream1["segments"]] == [2, 1, 3, 1]
    assert [row["segment_id"] for row in _rows(output, stream1)] == [
        "segment0", "segment0", "segment1", "segment2", "segment2", "segment2", "segment3",
    ]
    evidence = json.loads((output / "evidence/dataflash-import.json").read_text())
    assert [item["reason"] for item in evidence["streams"][1]["segment_starts"]] == [
        "first_record", "duplicate_timestamp", "backward_timestamp", "gap",
    ]
    assert evidence["replay_convention"] == {
        "gyro_interval_rule": "previous snapshot held over the next logged interval",
        "cross_segment_propagation": False,
        "last_snapshot_has_propagation_interval": False,
        "estimator_executed": False,
    }
    assert all(frame["axes"] is None for frame in acquisition["frames"])
    assert all(stream["kind"] == "imu" for stream in acquisition["streams"])


def test_deterministic_outputs_and_allowlisted_evidence_keep_private_original_separate(tmp_path):
    source = _source(tmp_path, _binary(
        _packet(_MSG, "Q64s", 500_000, b"PRIVATE_SERIAL_OR_LOCATION"),
        _packet(_ISBH, "QB", 500_000, 0),
        _imu(errors=(3, 4), health=(0, 0)),
    ))
    for name in ("first", "second"):
        build_dataflash_package(source, tmp_path / name, **_arguments())
    first, second = tmp_path / "first", tmp_path / "second"
    expected = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    assert expected == {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()}
    evidence = json.loads((first / "evidence/dataflash-import.json").read_text())
    assert evidence["original"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert evidence["decoder_metadata"]["imu_health"]["0"]["GH"]["unhealthy_count"] == 1
    assert evidence["decoder_metadata"]["batch_headers"]["accelerometer"] == 1
    assert "PRIVATE_SERIAL_OR_LOCATION" not in json.dumps(evidence)
    assert str(tmp_path) not in json.dumps(evidence)
    assert source.name not in json.dumps(evidence)
    assert "PRIVATE_SERIAL_OR_LOCATION" in (first / "originals/source.bin").read_bytes().decode("latin1")


@pytest.mark.parametrize("instances", [
    {}, {True: _declaration(0)}, {"0": _declaration(0)},
    {1: _declaration(1)}, {0: _declaration(0), 1: _declaration(1)},
    {0: {**_declaration(0), "unexpected": "field"}},
    {0: {**_declaration(0), "calibration": "inferred"}},
    {0: {**_declaration(0), "axes": ["unknown"]}},
])
def test_requires_exact_explicit_instance_metadata_and_removes_failed_output(tmp_path, instances):
    source = _source(tmp_path)
    arguments = _arguments()
    arguments["instances"] = instances
    with pytest.raises(ValueError):
        build_dataflash_package(source, tmp_path / "out", **arguments)
    assert not (tmp_path / "out").exists()


def test_rejects_conflicting_shared_frame_declarations(tmp_path):
    arguments = _arguments(0, 1)
    arguments["instances"][1]["frame_id"] = "sensor0"
    arguments["instances"][1]["axes"] = ["forward", "right", "down"]
    with pytest.raises(ValueError, match="conflicting axes"):
        build_dataflash_package(_source(tmp_path, _binary(_imu(instance=0), _imu(instance=1))),
                                tmp_path / "out", **arguments)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("data", [
    _binary(_imu(gyro=(float("nan"), 0, 0))),
    _binary(_imu(accel=(0, float("inf"), 0))),
    _binary(_imu(), _imu(1_040_000)[:-10]),
    _binary(b"PRIVATE_GARBAGE" * 60, _imu()),
    _binary(_imu(), unit_label=b"deg/s"),
    _binary(_imu(), include_fmtu=False),
])
def test_rejects_nonfinite_corrupt_or_unsupported_decoding_without_partial_package(tmp_path, data, capfd):
    source = _source(tmp_path, data)
    with pytest.raises(ValueError) as caught:
        build_dataflash_package(source, tmp_path / "out", **_arguments())
    assert not (tmp_path / "out").exists()
    assert source.read_bytes() == data
    assert "PRIVATE_GARBAGE" not in str(caught.value)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("threshold", [True, 0, -1, 0.2, 2**63])
def test_rejects_invalid_gap_threshold(tmp_path, threshold):
    with pytest.raises(ValueError, match="gap threshold"):
        build_dataflash_package(_source(tmp_path), tmp_path / "out",
                                gap_threshold_us=threshold, **_arguments())
    assert not (tmp_path / "out").exists()


def test_source_and_record_limits_are_enforced(tmp_path, monkeypatch):
    source = _source(tmp_path, _binary(_imu(), _imu(1_040_000)))
    monkeypatch.setattr(sensor_dataflash, "MAX_SOURCE_BYTES", 4)
    with pytest.raises(ValueError, match="byte limit"):
        build_dataflash_package(source, tmp_path / "out", **_arguments())
    monkeypatch.setattr(sensor_dataflash, "MAX_SOURCE_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(sensor_dataflash, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="record limit"):
        build_dataflash_package(source, tmp_path / "out", **_arguments())
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("record", [
    ImuRecord(True, 0, (0, 0, 0), (0, 0, 0)),
    ImuRecord(2**63 // 1000 + 1, 0, (0, 0, 0), (0, 0, 0)),
    ImuRecord(1, True, (0, 0, 0), (0, 0, 0)),
    ImuRecord(1, 0, (True, 0, 0), (0, 0, 0)),
    ImuRecord(1, 0, (0, 0), (0, 0, 0)),
    ImuRecord(1, 0, (10**1000, 0, 0), (0, 0, 0)),
])
def test_validates_decoded_integer_and_vector_boundaries(tmp_path, monkeypatch, record):
    source = _source(tmp_path)
    decoded = read_dataflash(source)
    decoded.records = [record]
    monkeypatch.setattr(sensor_dataflash, "read_dataflash", lambda _: decoded)
    with pytest.raises(ValueError):
        build_dataflash_package(source, tmp_path / "out", **_arguments())
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("target", ["source", "retained"])
def test_source_or_retained_mutation_during_decode_is_rejected(tmp_path, monkeypatch, target):
    source = _source(tmp_path)
    def mutating_decoder(retained):
        decoded = read_dataflash(retained)
        changed = source if target == "source" else retained
        changed.write_bytes(changed.read_bytes() + b"x")
        return decoded
    monkeypatch.setattr(sensor_dataflash, "read_dataflash", mutating_decoder)
    with pytest.raises(ValueError, match="changed during decoding"):
        build_dataflash_package(source, tmp_path / "out", **_arguments())
    assert not (tmp_path / "out").exists()


def test_existing_output_and_unsafe_sources_are_refused(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    (output / "sentinel").write_bytes(b"preserve")
    with pytest.raises(ValueError, match="new directory"):
        build_dataflash_package(source, output, **_arguments())
    assert (output / "sentinel").read_bytes() == b"preserve"
    for kind in ("symlink", "hardlink", "fifo"):
        candidate = tmp_path / kind
        if kind == "symlink":
            candidate.symlink_to(source)
        elif kind == "hardlink":
            os.link(source, candidate)
        else:
            os.mkfifo(candidate)
        with pytest.raises(ValueError):
            build_dataflash_package(candidate, tmp_path / f"out-{kind}", **_arguments())
        candidate.unlink()
    directory = tmp_path / "linked-parent"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_dataflash_package(directory / source.name, tmp_path / "out-parent", **_arguments())


def test_caller_declarations_are_preserved_without_mutating_input(tmp_path):
    arguments = _arguments()
    arguments["instances"][0].update({
        "axes": ["forward", "right", "down"], "calibration": "applied",
        "filtering": "applied", "clock_correction": "not_applied",
        "description": "Explicit fixture frontend declaration.",
    })
    before = copy.deepcopy(arguments)
    result = build_dataflash_package(_source(tmp_path), tmp_path / "out", **arguments)
    assert arguments == before
    assert result["streams"][0]["processing"] == {
        key: before["instances"][0][key]
        for key in ("calibration", "filtering", "clock_correction", "description")
    }
