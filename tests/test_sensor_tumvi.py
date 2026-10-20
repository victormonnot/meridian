"""Check the calibrated TUM-VI adapter without public downloads or holdouts."""

import csv
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import zlib

import pytest

from meridian import sensor_tumvi as tumvi
from meridian.sensor_contract import validate_contract
from meridian.sensor_package import DEFAULT_LIMITS, validate_package
from meridian import vio_dataset


EPOCH = 1_520_530_308_199_447_626
ROOT = "dataset-room1_512_16"


def png():
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 512, 16, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(512 * (1 + 512 * 2))))
        + chunk(b"IEND", b"")
    )


def csv_bytes(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


def rows(path):
    with path.open(newline="") as handle:
        return list(csv.reader(handle))


@pytest.fixture
def files():
    stamps = [EPOCH, EPOCH + 50_000_000]
    result = {}
    for name in ("cam0", "cam1"):
        result[f"mav0/{name}/data.csv"] = csv_bytes(
            tumvi.CAMERA_HEADER, [[str(t), f"{t}.png"] for t in stamps],
        )
        for stamp in stamps:
            result[f"mav0/{name}/data/{stamp}.png"] = png()
    result["mav0/imu0/data.csv"] = csv_bytes(
        tumvi.IMU_HEADER,
        [[str(t), "0.1045577128", "-0.0792420275", "0.0178416359",
          "-0.3522450410", "0.0000321145", "9.9237603029"]
         for t in [EPOCH - 1, EPOCH + 20_000_000, EPOCH + 50_000_001]],
    )
    result["mav0/mocap0/data.csv"] = csv_bytes(
        tumvi.MOCAP_HEADER,
        [[str(t), "1.0000000000", "-2.0000000000", "0.0000000001",
          "0.7071067811865476", "0.0000000000", "0.0000000000",
          "0.7071067811865476"] for t in stamps],
    )
    result["dso/camchain.yaml"] = b"cam0:\n  camera_model: pinhole\n"
    result["dso/imu_config.yaml"] = b"update_rate: 200.0\n"
    return result


def directory(tmp_path, files, name="source"):
    root = tmp_path / name
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def archive(tmp_path, files, *, aliases=(), root=ROOT, extras=(), name="source.tar"):
    path = tmp_path / name
    with tarfile.open(path, "w") as handle:
        for relative, content in files.items():
            member = tarfile.TarInfo(f"{root}/{relative}")
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))
        for member in aliases:
            handle.addfile(member)
        for member, content in extras:
            handle.addfile(member, io.BytesIO(content))
    return path


def adapt(source, output, **kwargs):
    return tumvi.adapt_tumvi(
        source, output, acquisition_id="fixture-tumvi", sequence="room1", **kwargs,
    )


def identities(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_directory_preserves_source_and_exact_numeric_lexemes(tmp_path, files):
    source = directory(tmp_path, files)
    before = identities(source)
    output = tmp_path / "package"
    acquisition = adapt(source, output)
    validate_contract(acquisition)
    assert validate_package(output)["acquisition"] == acquisition
    assert identities(source) == before
    for relative, content in files.items():
        assert (output / "originals/source" / relative).read_bytes() == content
    for camera in ("cam0", "cam1"):
        for stamp in (EPOCH, EPOCH + 50_000_000):
            assert (output / f"sensors/{camera}/data/{stamp}.png").read_bytes() == (
                source / f"mav0/{camera}/data/{stamp}.png"
            ).read_bytes()
    original = rows(source / "mav0/imu0/data.csv")
    imported = rows(output / "sensors/imu0/data.csv")
    assert [row[2:] for row in imported[1:]] == original[1:]
    assert [row[:2] for row in imported[1:]] == [
        ["0", "segment0"], ["1", "segment0"], ["2", "segment0"],
    ]
    reference = rows(output / "references/mocap0/data.csv")
    assert reference[1] == [
        "0", "segment0", str(EPOCH), "1.0000000000", "-2.0000000000",
        "0.0000000001", "0.0000000000", "0.0000000000",
        "0.7071067811865476", "0.7071067811865476",
    ]
    assert all("mocap" not in path for path in identities(output / "sensors"))
    assert str(tmp_path) not in json.dumps(acquisition)
    assert acquisition["frames"][2]["axes"] is None
    assert acquisition["clocks"][0]["epoch"] == "unknown"
    reference_stream = acquisition["streams"][-1]
    assert reference_stream["reference"]["coverage"] == "unknown"
    assert reference_stream["reference"]["position_uncertainty_m"] is None
    imu = next(stream for stream in acquisition["streams"] if stream["kind"] == "imu")
    assert imu["processing"]["calibration"] == imu["processing"]["clock_correction"] == "applied"
    assert imu["imu"]["angular_velocity_unit"] == "rad/s"
    assert imu["imu"]["specific_force_unit"] == "m/s^2"


def test_tar_and_directory_produce_identical_sensor_and_reference_payloads(tmp_path, files):
    source = directory(tmp_path, files)
    packed = archive(tmp_path, files)
    original = packed.read_bytes()
    first, second = tmp_path / "directory-package", tmp_path / "tar-package"
    adapt(source, first)
    result = adapt(packed, second)
    assert packed.read_bytes() == original
    assert (second / "originals/source.tar").read_bytes() == original
    assert identities(first / "sensors") == identities(second / "sensors")
    assert identities(first / "references") == identities(second / "references")
    evidence = json.loads((second / "evidence/tumvi-source.json").read_text())
    assert evidence["archive_identity"] == {
        "size_bytes": len(original), "sha256": hashlib.sha256(original).hexdigest(),
    }
    assert set(evidence["source_files"]) == {f"{ROOT}/{name}" for name in files}
    assert {entry["path"] for entry in result["files"]} == set(identities(second)) - {"acquisition.json"}


def test_repeat_import_is_deterministic_and_never_replaces_output(tmp_path, files):
    source = directory(tmp_path, files)
    first, second = tmp_path / "first", tmp_path / "second"
    assert adapt(source, first) == adapt(source, second)
    assert identities(first) == identities(second)
    before = identities(first)
    with pytest.raises(FileExistsError):
        adapt(source, first)
    assert identities(first) == before


def test_output_created_at_publication_is_preserved(tmp_path, files, monkeypatch):
    source = directory(tmp_path, files)
    output = tmp_path / "package"
    original_mkdir = Path.mkdir
    competing_content = b"another caller owns this directory\n"

    def create_competing_directory(path, *args, **kwargs):
        if path == output:
            original_mkdir(path)
            (path / "owner").write_bytes(competing_content)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_competing_directory)
    with pytest.raises(FileExistsError):
        adapt(source, output)
    assert identities(output) == {
        "owner": hashlib.sha256(competing_content).hexdigest(),
    }
    assert not list(tmp_path.glob(".tumvi-import-*"))


def test_failed_publication_removes_only_owned_output(tmp_path, files, monkeypatch):
    source = directory(tmp_path, files)
    before = identities(source)
    output = tmp_path / "package"
    original_rename = Path.rename

    def reject_sensor_move(path, target):
        if path.name == "sensors":
            raise OSError("fixture publication failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", reject_sensor_move)
    with pytest.raises(OSError, match="fixture publication failure"):
        adapt(source, output)
    assert not output.exists()
    assert identities(source) == before
    assert not list(tmp_path.glob(".tumvi-import-*"))


@pytest.mark.parametrize("remove_reference", [False, True])
def test_reference_is_optional_and_remains_separate(tmp_path, files, remove_reference):
    if remove_reference:
        del files["mav0/mocap0/data.csv"]
    source = directory(tmp_path, files)
    output = tmp_path / "package"
    result = adapt(source, output, include_reference=remove_reference)
    assert {stream["kind"] for stream in result["streams"]} == {"camera", "imu"}
    assert not (output / "references").exists()
    assert (output / "originals/source/mav0/mocap0/data.csv").exists() is not remove_reference
    report = json.loads((output / "evidence/tumvi-source.json").read_text())
    assert report["reference_policy"] == ("absent" if remove_reference else "excluded_by_request")


def test_numerical_payloads_match_frozen_legacy_preparation(tmp_path, files):
    source = archive(tmp_path, files)
    content = source.read_bytes()
    pin = {
        **vio_dataset.ROOM1, "sha256": hashlib.sha256(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(), "size_bytes": len(content),
    }
    config = tmp_path / "config"
    config.mkdir()
    (config / "estimator_config.yaml").write_text("max_cameras: 2\n")
    legacy = tmp_path / "legacy"
    legacy_manifest = vio_dataset._prepare_dataset(source, legacy, config, pin)
    legacy_before = identities(legacy)
    output = tmp_path / "package"
    adapt(source, output)
    for name in ("cam0", "cam1", "imu0"):
        old = rows(legacy / f"sensors/mav0/{name}/data.csv")[1:]
        new = rows(output / f"sensors/{name}/data.csv")[1:]
        if name.startswith("cam"):
            assert [row[2] for row in new] == [row[0] for row in old]
            for row in old:
                assert (legacy / f"sensors/mav0/{name}/data/{row[1]}").read_bytes() == (
                    output / f"sensors/{name}/data/{row[1]}"
                ).read_bytes()
        else:
            assert [row[2:] for row in new] == old
    assert [row[2:] for row in rows(output / "references/mocap0/data.csv")[1:]] == (
        rows(legacy / "reference/trajectory.csv")[1:]
    )
    assert identities(legacy) == legacy_before
    assert vio_dataset._verify_prepared(legacy, pin) == legacy_manifest


def test_only_exact_official_dso_aliases_are_skipped_and_recorded(tmp_path, files):
    aliases = []
    for camera in ("cam0", "cam1"):
        member = tarfile.TarInfo(f"{ROOT}/dso/{camera}/images")
        member.type = tarfile.SYMTYPE
        member.linkname = f"../../mav0/{camera}/data"
        aliases.append(member)
    source = archive(tmp_path, files, aliases=aliases)
    output = tmp_path / "package"
    adapt(source, output)
    report = json.loads((output / "evidence/tumvi-source.json").read_text())
    assert report["ignored_archive_aliases"] == [
        {"path": alias.name, "target": alias.linkname} for alias in aliases
    ]
    assert not any(path.is_symlink() for path in output.rglob("*"))
    assert (output / "originals/source.tar").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("alias_name,link", [
    (f"{ROOT}/dso/cam0/images", "../../mav0/mocap0"),
    (f"{ROOT}/mav0/cam0/alias", "../mocap0/data.csv"),
    (f"{ROOT}/other", "/etc/passwd"),
])
def test_unrecognized_archive_links_are_rejected(tmp_path, files, alias_name, link):
    alias = tarfile.TarInfo(alias_name)
    alias.type, alias.linkname = tarfile.SYMTYPE, link
    source = archive(tmp_path, files, aliases=[alias])
    output = tmp_path / "package"
    with pytest.raises(ValueError):
        adapt(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".tumvi-import-*"))


@pytest.mark.parametrize("case", [
    "duplicate_time", "backward_time", "nonfinite", "bad_header", "bad_filename",
    "unpaired", "missing_image", "unreferenced_image", "broken_png",
    "wrong_dimensions", "nonunit_quaternion", "extra_stream", "empty_imu",
    "oversized_imu_line",
])
def test_invalid_source_is_rejected_without_publishing(tmp_path, files, case):
    if case in {"duplicate_time", "backward_time", "nonfinite", "bad_header", "empty_imu"}:
        parsed = list(csv.reader(io.StringIO(files["mav0/imu0/data.csv"].decode())))
        if case == "duplicate_time":
            parsed[2][0] = parsed[1][0]
        elif case == "backward_time":
            parsed[2][0] = str(EPOCH - 2)
        elif case == "nonfinite":
            parsed[1][1] = "nan"
        elif case == "bad_header":
            parsed[0][1] = "gyro_x"
        else:
            parsed = parsed[:1]
        files["mav0/imu0/data.csv"] = csv_bytes(parsed[0], parsed[1:])
    elif case in {"bad_filename", "unpaired"}:
        parsed = list(csv.reader(io.StringIO(files["mav0/cam1/data.csv"].decode())))
        if case == "bad_filename":
            parsed[1][1] = "../mocap0/data.csv"
        else:
            parsed[1] = [str(EPOCH + 1), f"{EPOCH + 1}.png"]
            files[f"mav0/cam1/data/{EPOCH + 1}.png"] = files.pop(f"mav0/cam1/data/{EPOCH}.png")
        files["mav0/cam1/data.csv"] = csv_bytes(parsed[0], parsed[1:])
    elif case == "missing_image":
        del files[f"mav0/cam0/data/{EPOCH}.png"]
    elif case == "unreferenced_image":
        files["mav0/cam0/data/0.png"] = png()
    elif case in {"broken_png", "wrong_dimensions"}:
        content = files[f"mav0/cam0/data/{EPOCH}.png"]
        if case == "broken_png":
            content = content[:-10]
        else:
            content = (content[:16] + struct.pack(">I", 511) + content[20:29])
            content += struct.pack(">I", zlib.crc32(content[12:29]))
            content += files[f"mav0/cam0/data/{EPOCH}.png"][33:]
        files[f"mav0/cam0/data/{EPOCH}.png"] = content
    elif case == "nonunit_quaternion":
        parsed = list(csv.reader(io.StringIO(files["mav0/mocap0/data.csv"].decode())))
        parsed[1][4] = "2"
        files["mav0/mocap0/data.csv"] = csv_bytes(parsed[0], parsed[1:])
    elif case == "oversized_imu_line":
        files["mav0/imu0/data.csv"] += b"," * 32769 + b"\n"
    else:
        files["mav0/cam2/data.csv"] = b"unsupported\n"
    source = directory(tmp_path, files)
    before = identities(source)
    output = tmp_path / "package"
    with pytest.raises((ValueError, FileNotFoundError)):
        adapt(source, output)
    assert not output.exists()
    assert identities(source) == before
    assert not list(tmp_path.glob(".tumvi-import-*"))


def test_directory_symlinks_and_nested_output_are_refused(tmp_path, files):
    source = directory(tmp_path, files)
    with pytest.raises(ValueError, match="inside"):
        adapt(source, source / "new-output")
    assert not (source / "new-output").exists()
    (source / "alias").symlink_to(source / "mav0/imu0/data.csv")
    with pytest.raises(ValueError):
        adapt(source, tmp_path / "package")
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize("sequence", ["../room1", "room1_512_16", "room0", "room99", "", True])
def test_unsupported_sequence_is_rejected_before_source_access(tmp_path, sequence):
    with pytest.raises(ValueError, match="sequence"):
        tumvi.adapt_tumvi(tmp_path / "absent", tmp_path / "out",
                         acquisition_id="test", sequence=sequence)
    assert not (tmp_path / "out").exists()


def test_archive_root_must_match_explicit_sequence(tmp_path, files):
    source = archive(tmp_path, files, root="dataset-room2_512_16")
    with pytest.raises(ValueError, match="sequence"):
        adapt(source, tmp_path / "package")
    assert not (tmp_path / "package").exists()


def test_limits_apply_to_materialized_source_and_final_package(tmp_path, files):
    source = directory(tmp_path, files)
    source_size = sum(len(content) for content in files.values())
    limits = replace(DEFAULT_LIMITS, max_total_bytes=source_size + 100)
    with pytest.raises(ValueError):
        adapt(source, tmp_path / "package", limits=limits)
    assert not (tmp_path / "package").exists()
    limits = replace(DEFAULT_LIMITS, max_csv_rows=1)
    with pytest.raises(ValueError):
        adapt(source, tmp_path / "row-limit", limits=limits)
    assert not (tmp_path / "row-limit").exists()


def test_single_dataset_wrapper_directory_is_supported(tmp_path, files):
    source = directory(tmp_path, {f"{ROOT}/{path}": value for path, value in files.items()})
    output = tmp_path / "package"
    adapt(source, output)
    assert (output / "originals/source" / ROOT / "dso/camchain.yaml").read_bytes() == (
        files["dso/camchain.yaml"]
    )
