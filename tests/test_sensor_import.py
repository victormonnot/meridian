"""End-to-end checks of local import, preparation and exact source preservation."""

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import numpy as np
from PIL import Image
import pytest

from meridian.sensor_contract import contract_sha256, validate_bundle
from meridian.sensor_import import convert_mono16, import_package, prepare_package
from meridian.sensor_package import validate_package


EXAMPLES = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"


def save(root, value):
    (root / "acquisition.json").write_text(json.dumps(value) + "\n", encoding="utf-8")


def refresh(root, value):
    for entry in value["files"]:
        data = (root / entry["path"]).read_bytes()
        entry.update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    save(root, value)


def package(tmp_path, name="stereo-no-reference"):
    root = tmp_path / name
    root.mkdir()
    value = json.loads((EXAMPLES / f"{name}.json").read_text())
    for entry in value["files"]:
        destination = root / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / entry["path"], destination)
    save(root, value)
    return root, value


def contents(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def rows(path):
    with path.open(newline="") as handle:
        return list(csv.reader(handle))[1:]


@pytest.mark.parametrize("container", ["directory", "tar", "zip"])
def test_repeat_import_preserves_package_bytes_and_semantic_identity(tmp_path, container):
    root, acquisition = package(tmp_path)
    source = root
    original = contents(root)
    if container == "tar":
        source = tmp_path / "source.tar"
        with tarfile.open(source, "w") as archive:
            for name in sorted(original):
                archive.add(root / name, arcname=name)
    elif container == "zip":
        source = tmp_path / "source.zip"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(original):
                archive.write(root / name, arcname=name)
    first = import_package(source, tmp_path / "first")
    second = import_package(source, tmp_path / "second")
    assert first == second
    assert first["acquisition"]["sha256"] == contract_sha256(acquisition)
    assert contents(tmp_path / "first/acquisition") == original
    assert contents(tmp_path / "first") == contents(tmp_path / "second")
    assert contents(root) == original
    if container != "directory":
        assert (tmp_path / "first/original-transfer").read_bytes() == source.read_bytes()


def test_preparation_repeats_preserving_observations_and_optional_reference(tmp_path):
    source, acquisition = package(tmp_path, "stereo-partial-reference")
    original = contents(source)
    first = prepare_package(source, tmp_path / "one")
    second = prepare_package(source, tmp_path / "two")
    assert first == second
    assert contents(tmp_path / "one") == contents(tmp_path / "two")
    assert contents(source) == original
    prepared = first["prepared_input"]
    validate_bundle([acquisition, prepared])
    assert prepared["operations"] == []
    for entry in prepared["files"]:
        payload = (tmp_path / "one" / entry["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert len(payload) == entry["size_bytes"]
    assert rows(tmp_path / "one/sensors/imu0/data.csv") == rows(source / "sensors/imu0/data.csv")
    assert rows(tmp_path / "one/references/pose0/data.csv") == rows(source / "references/pose0/data.csv")
    without = prepare_package(source, tmp_path / "without", include_reference=False)
    assert all(entry["role"] == "sensor" for entry in without["prepared_input"]["files"])
    assert not (tmp_path / "without/references").exists()
    assert without["prepared_input"]["acquisition"] == prepared["acquisition"]
    assert first["report"]["execution_readiness"] == "not_assessed"


def test_snapshots_scale_microseconds_exactly_and_keep_reset_and_identity(tmp_path):
    source, _ = package(tmp_path, "imu-snapshots")
    result = prepare_package(source, tmp_path / "prepared")
    raw = rows(source / "sensors/snapshots/data.csv")
    output = rows(tmp_path / "prepared/sensors/imu0/data.csv")
    for before, after in zip(raw, output, strict=True):
        assert after[:2] == before[:2]
        assert after[3:] == before[3:]
        assert int(after[2]) == 1000 * int(before[2])
    assert int(output[2][2]) < int(output[1][2])
    assert output[2][1] != output[1][1]
    assert result["prepared_input"]["target_profile"] == "imu_snapshot_candidate_v1"
    assert result["report"]["streams"][0]["measurement_semantics"] == "filtered_snapshot"


def test_explicit_si_conversion_is_analytic_and_never_calibration(tmp_path):
    source, acquisition = package(tmp_path, "imu-snapshots")
    imu = acquisition["streams"][0]["imu"]
    imu.update(angular_velocity_unit="deg/s", specific_force_unit="g", local_gravity_m_s2=9.8)
    path = source / "sensors/snapshots/data.csv"
    lines = path.read_text().splitlines()
    lines[1:] = [",".join(line.split(",")[:3] + ["180", "-90", "0", "0", "0", "-1"])
                 for line in lines[1:]]
    path.write_text("\n".join(lines) + "\n")
    refresh(source, acquisition)
    result = prepare_package(source, tmp_path / "prepared")
    output = rows(tmp_path / "prepared/sensors/imu0/data.csv")
    assert [float(value) for value in output[0][3:]] == pytest.approx([np.pi, -np.pi / 2, 0, 0, 0, -9.8])
    assert [op["kind"] for op in result["prepared_input"]["operations"]] == ["unit_conversion"] * 2
    assert result["prepared_input"]["calibration"] is None
    assert result["prepared_input"]["clock_mappings"] == []


def test_mono16_conversion_matches_nearest_scaling_for_every_possible_input(tmp_path):
    original = np.arange(65536, dtype=np.uint16).reshape(256, 256)
    source = tmp_path / "source.png"
    Image.fromarray(original).save(source)
    before = source.read_bytes()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    convert_mono16(source, a)
    convert_mono16(source, b)
    assert a.read_bytes() == b.read_bytes()
    assert source.read_bytes() == before
    with Image.open(a) as result:
        assert result.mode == "L" and result.size == (256, 256)
        expected = np.floor(original.astype(np.float64) * (255 / 65535) + 0.5).astype(np.uint8)
        np.testing.assert_array_equal(np.asarray(result), expected)


def test_prepared_image_conversion_records_source_identity_and_dimensions(tmp_path):
    source, acquisition = package(tmp_path)
    pixels = np.arange(48, dtype=np.uint16).reshape(6, 8) * 1394
    for stream in acquisition["streams"]:
        if stream["kind"] == "camera":
            stream["camera"]["encoding"] = "mono16"
    for path in source.glob("sensors/cam*/images/*.png"):
        Image.fromarray(pixels).save(path)
    refresh(source, acquisition)
    result = prepare_package(source, tmp_path / "prepared", image_encoding="mono8")
    assert len(result["report"]["image_conversions"]) == 4
    for conversion in result["report"]["image_conversions"]:
        assert conversion["resolution"] == [8, 6]
        original = (source / conversion["source"]["path"]).read_bytes()
        assert hashlib.sha256(original).hexdigest() == conversion["source"]["sha256"]
        with Image.open(tmp_path / "prepared" / conversion["output"]["path"]) as image:
            assert image.mode == "L"
            np.testing.assert_array_equal(np.asarray(image), np.floor(pixels / 257 + 0.5))
    assert {op["kind"] for op in result["prepared_input"]["operations"]} == {"image_conversion"}


def test_multiple_imu_instances_require_an_explicit_preparation_selection(tmp_path):
    source, acquisition = package(tmp_path, "imu-snapshots")
    duplicate = deepcopy(acquisition["streams"][0])
    duplicate.update(id="imu1", sensor_instance="imu1", index_path="sensors/imu1/data.csv")
    destination = source / duplicate["index_path"]
    destination.parent.mkdir()
    shutil.copyfile(source / acquisition["streams"][0]["index_path"], destination)
    acquisition["streams"].append(duplicate)
    entry = deepcopy(acquisition["files"][0])
    entry["path"] = duplicate["index_path"]
    acquisition["files"].append(entry)
    save(source, acquisition)
    validate_package(source)
    with pytest.raises(ValueError, match="Select one IMU"):
        prepare_package(source, tmp_path / "ambiguous")
    assert not (tmp_path / "ambiguous").exists()
    prepared = prepare_package(source, tmp_path / "selected", imu_stream="imu1")
    assert {output["stream_id"] for output in prepared["prepared_input"]["outputs"]} == {"imu1"}


@pytest.mark.parametrize("operation", [import_package, prepare_package])
def test_existing_outputs_and_source_tree_are_never_overwritten(tmp_path, operation):
    source, _ = package(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep"
    sentinel.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        operation(source, existing)
    assert sentinel.read_bytes() == b"keep"
    with pytest.raises(ValueError, match="outside"):
        operation(source, source / "nested")
    assert not (source / "nested").exists()


def test_rejected_payload_leaves_no_new_output_and_cli_returns_structured_error(tmp_path):
    source, _ = package(tmp_path)
    (source / "sensors/cam0/images/0.png").write_bytes(b"broken")
    with pytest.raises(ValueError):
        import_package(source, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    run = subprocess.run([sys.executable, "-m", "meridian.sensor_import", "validate", str(source)],
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 2
    result = json.loads(run.stderr)
    assert result["status"] == "rejected"
    assert result["findings"][0]["code"]
    assert str(tmp_path) not in run.stderr


def test_cli_prepares_real_files_without_starting_an_estimator(tmp_path):
    source, _ = package(tmp_path)
    run = subprocess.run([sys.executable, "-m", "meridian.sensor_import", "prepare",
                          str(source), str(tmp_path / "cli")],
                         text=True, capture_output=True, timeout=20, check=True)
    report = json.loads(run.stdout)
    assert report["status"] == "accepted"
    assert report["execution_readiness"] == "not_assessed"
    assert (tmp_path / "cli/prepared-input.json").is_file()
    assert not any("trajectory" in name for name in contents(tmp_path / "cli"))


def test_descriptor_changed_during_final_copy_cannot_publish_inconsistent_lineage(tmp_path, monkeypatch):
    from meridian import sensor_import
    source, acquisition = package(tmp_path)
    original_copy = sensor_import._copy
    def mutate_copy(incoming, outgoing, *args, **kwargs):
        if incoming.name == "acquisition.json":
            changed = deepcopy(acquisition)
            changed["id"] = "changed-during-copy"
            save(source, changed)
        return original_copy(incoming, outgoing, *args, **kwargs)
    monkeypatch.setattr(sensor_import, "_copy", mutate_copy)
    with pytest.raises(ValueError, match="descriptor changed"):
        prepare_package(source, tmp_path / "inconsistent")
    assert not (tmp_path / "inconsistent").exists()


def test_prepared_png_expansion_obeys_output_budget(tmp_path):
    from dataclasses import replace
    from meridian.sensor_package import DEFAULT_LIMITS
    source, acquisition = package(tmp_path)
    for stream in acquisition["streams"]:
        if stream["kind"] == "camera":
            stream["camera"].update(encoding="mono16", resolution=[128, 128])
    for path in source.glob("sensors/cam*/images/*.png"):
        Image.fromarray(np.zeros((128, 128), dtype=np.uint16)).save(path)
    refresh(source, acquisition)
    limits = replace(DEFAULT_LIMITS, max_total_bytes=10000)
    validate_package(source, limits=limits)
    with pytest.raises(ValueError, match="byte limits"):
        prepare_package(source, tmp_path / "expanded", image_encoding="mono8", limits=limits)
    assert not (tmp_path / "expanded").exists()
