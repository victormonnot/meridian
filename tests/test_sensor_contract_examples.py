"""Independent checks of the public synthetic descriptor/payload examples."""

import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

import pytest

from meridian.sensor_contract import (
    assess_acquisition, contract_sha256, loads_contract, validate_bundle,
)
from meridian.sensor_conventions import map_constant_clock, timestamp_ns, transform_point


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/sensor-contracts"
EPOCH = 1_520_530_308_199_447_626


def documents():
    return {path.name: loads_contract(path.read_text()) for path in sorted(EXAMPLES.glob("*.json"))}


def rows(path):
    return list(csv.DictReader(io.StringIO(path.read_text())))


def test_all_public_examples_form_one_consistent_bundle():
    values = documents()
    assert len(values) == 8
    validate_bundle(list(values.values()))
    prepared = values["prepared-input.json"]
    calibration = values["calibration.json"]
    assert prepared["acquisition"]["id"] != calibration["acquisition"]["id"]
    assert prepared["acquisition"] == {
        "id": values["replay-acquisition.json"]["id"],
        "sha256": contract_sha256(values["replay-acquisition.json"]),
    }
    for value in values.values():
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        assert contract_sha256(value) == hashlib.sha256(canonical).hexdigest()


def test_fixture_bytes_match_each_declared_inventory_and_stream_bounds():
    for value in documents().values():
        if value["kind"] not in ("acquisition", "prepared_input"):
            continue
        files = {entry["path"]: entry for entry in value["files"]}
        for relative, identity in files.items():
            content = (EXAMPLES / relative).read_bytes()
            assert len(content) == identity["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == identity["sha256"]
        for stream in value.get("streams", []):
            samples = rows(EXAMPLES / stream["index_path"])
            assert [int(row["sample_id"]) for row in samples] == list(range(len(samples)))
            for segment in stream["segments"]:
                group = [row for row in samples if row["segment_id"] == segment["id"]]
                assert len(group) == segment["sample_count"]
                assert group[0]["timestamp"] == segment["first_tick"]
                assert group[-1]["timestamp"] == segment["last_tick"]
                assert all(int(a["timestamp"]) < int(b["timestamp"]) for a, b in zip(group, group[1:]))
            if stream["kind"] == "camera":
                for row in samples:
                    path = (Path(stream["index_path"]).parent / row["filename"]).as_posix()
                    assert files[path]["role"] == "sensor"
                    assert (EXAMPLES / path).is_file()


def test_fixture_png_is_decodable_grayscale_with_declared_shape_and_valid_checksums():
    for path in sorted(EXAMPLES.glob("sensors/cam*/images/*.png")):
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        position, payload, kinds = 8, b"", []
        while position < len(data):
            length = struct.unpack(">I", data[position:position + 4])[0]
            kind = data[position + 4:position + 8]
            content = data[position + 8:position + 8 + length]
            checksum = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
            assert checksum == zlib.crc32(kind + content)
            kinds.append(kind)
            if kind == b"IHDR":
                assert struct.unpack(">IIBBBBB", content) == (8, 6, 8, 0, 0, 0, 0)
            elif kind == b"IDAT":
                payload += content
            position += length + 12
        assert position == len(data)
        assert kinds == [b"IHDR", b"IDAT", b"IEND"]
        pixels = zlib.decompress(payload)
        assert len(pixels) == 6 * (1 + 8)
        assert all(pixels[row * 9] == 0 for row in range(6))


def test_epoch_offset_matches_prepared_payload_without_changing_observations():
    value = documents()["clock-mapping.json"]
    source = rows(EXAMPLES / "sensors/imu0/data.csv")
    prepared = rows(EXAMPLES / "sensors/prepared-imu0/data.csv")
    for before, after in zip(source, prepared, strict=True):
        expected = int(before["timestamp"]) + 2
        mapped = map_constant_clock(
            timestamp_ns(before["timestamp"]),
            source_origin_ns=timestamp_ns(value["source_origin_ns"]),
            target_origin_ns=timestamp_ns(value["target_origin_ns"]),
            offset_ns=timestamp_ns(value["offset_ns"]),
            segment_start_ns=timestamp_ns(value["first_source_ns"]),
            segment_end_ns=timestamp_ns(value["last_source_ns"]),
        )
        assert mapped == expected == int(after["timestamp"])
        assert {k: v for k, v in before.items() if k != "timestamp"} == {
            k: v for k, v in after.items() if k != "timestamp"}
    # Float conversion loses the exact 2 ns distinction at this epoch.
    assert EPOCH + 2 != EPOCH
    assert float(EPOCH + 2) == float(EPOCH)


def test_camera_frame_example_maps_body_forward_to_optical_forward():
    transforms = documents()["calibration.json"]["transforms"]
    assert transform_point([1, 0, 0], transforms[0]["rotation"], transforms[0]["translation_m"]) == (0, 0, 1)
    assert transform_point([1, 0, 0], transforms[1]["rotation"], transforms[1]["translation_m"]) == (-0.1, 0, 1)


def test_reference_absence_partial_support_and_unknown_metadata_stay_distinct():
    values = documents()
    absent = assess_acquisition(values["stereo-no-reference.json"])
    partial = assess_acquisition(values["stereo-partial-reference.json"])
    unknown = assess_acquisition(values["unknown-metadata.json"])
    assert absent["reference_availability"] == "absent"
    assert absent["declared_reference_coverage"] == []
    assert partial["reference_availability"] == "declared"
    assert partial["declared_reference_coverage"] == ["partial"]
    assert unknown["unknown_metadata"]
    for report in (absent, partial, unknown):
        assert report["payload_verification"] == "not_performed"
        assert report["execution_readiness"] == "not_assessed"
        assert report["calibration_validity"] == "not_assessed"
        assert report["reference_coverage_verification"] == "not_performed"
        assert "ate_rmse_m" not in report


def test_snapshot_example_preserves_clock_reset_and_processed_semantics():
    stream = documents()["imu-snapshots.json"]["streams"][0]
    samples = rows(EXAMPLES / stream["index_path"])
    assert int(samples[2]["timestamp"]) < int(samples[1]["timestamp"])
    assert samples[2]["segment_id"] != samples[1]["segment_id"]
    assert stream["measurement_semantics"] == "filtered_snapshot"
    assert stream["timestamp_semantics"] == "message_write"
    assert stream["processing"]["calibration"] == stream["processing"]["filtering"] == "applied"


def test_generator_reproduces_exact_example_bytes_and_refuses_existing_output(tmp_path):
    spec = importlib.util.spec_from_file_location("contract_fixture_generator", EXAMPLES / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "new"
    module.generate(output)
    generated = sorted(path for path in output.rglob("*") if path.is_file())
    assert len(generated) == 18
    for path in generated:
        assert path.read_bytes() == (EXAMPLES / path.relative_to(output)).read_bytes()
    with pytest.raises(FileExistsError):
        module.generate(output)


def test_cli_needs_no_payloads_or_third_party_packages(tmp_path):
    descriptor = tmp_path / "only-descriptor.json"
    descriptor.write_bytes((EXAMPLES / "stereo-no-reference.json").read_bytes())
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from meridian.sensor_contract import main; "
        "result=main([sys.argv[2]]); "
        "assert not any(name in sys.modules for name in ('numpy','django','PIL')); "
        "raise SystemExit(result)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(ROOT / "src"), str(descriptor)],
        text=True, capture_output=True, check=True, timeout=10,
    )
    report = json.loads(result.stdout)
    assert report["descriptor_validity"] == "valid"
    assert report["payload_verification"] == "not_performed"
    assert report["execution_readiness"] == "not_assessed"
    assert not (tmp_path / "sensors").exists()
