"""Exercise recording isolation and integrity without downloading public data."""

import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import zlib

import pytest

from meridian import vio_dataset as dataset


def _png():
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 512, 512, 16, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(512 * (1 + 512 * 2))))
            + chunk(b"IEND", b""))


def _csv(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


@pytest.fixture
def source(tmp_path):
    times = [1000000001, 1050000001, 1100000001]
    root = dataset.ARCHIVE_ROOT
    files = {}
    for camera in ("cam0", "cam1"):
        files[f"{root}/mav0/{camera}/data.csv"] = _csv(
            dataset.CAMERA_HEADER, [[t, f"{t}.png"] for t in times])
        for timestamp in times:
            files[f"{root}/mav0/{camera}/data/{timestamp}.png"] = _png()
    files[f"{root}/mav0/imu0/data.csv"] = _csv(dataset.IMU_HEADER,
        [[t, 0.1, 0.2, 0.3, 0, 0, 9.81] for t in [990000001, 1040000001, 1110000001]])
    # A 90-degree body-to-world rotation about z, in source wxyz order.
    files[dataset.REFERENCE["source_path"]] = _csv(dataset.MOCAP_HEADER,
        [[t, 1, 2, 3, "0.7071067811865476", 0, 0, "0.7071067811865476"] for t in times])
    files[f"{root}/dso/gt_imu.csv"] = b"unused reference alias\n"
    config = tmp_path / "source-config"
    config.mkdir()
    (config / "estimator_config.yaml").write_text("max_cameras: 2\n")
    return files, config


def _archive(tmp_path, files, extras=(), *, name="recording.tar"):
    path = tmp_path / name
    with tarfile.open(path, "w") as archive:
        for filename, content in files.items():
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for member, content in extras:
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    raw = path.read_bytes()
    pin = {**dataset.ROOM1, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
           "md5": hashlib.md5(raw).hexdigest()}
    return path, pin


def _prepare(tmp_path, source, **kwargs):
    files, config = source
    archive, pin = _archive(tmp_path, files, **kwargs)
    out = tmp_path / "prepared"
    manifest = dataset._prepare_dataset(archive, out, config, pin)
    return out, manifest, pin


def _save_manifest(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_prepare_isolates_reference_preserves_nanoseconds_and_exact_config(tmp_path, source):
    root, manifest, pin = _prepare(tmp_path, source)
    assert dataset._verify_prepared(root, pin) == manifest
    assert dataset.camera_timestamps_ns(root) == [1000000001, 1050000001, 1100000001]
    sensor_files = {p.relative_to(root / "sensors").as_posix()
                    for p in (root / "sensors").rglob("*") if p.is_file()}
    assert len(sensor_files) == 9
    assert all(path.startswith(("mav0/cam0/", "mav0/cam1/", "mav0/imu0/"))
               for path in sensor_files)
    assert not (root / "dso").exists()
    assert not (root / "source-reference.csv").exists()
    with (root / "reference/trajectory.csv").open() as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == dataset.REFERENCE_HEADER
    assert rows[1] == ["1000000001", "1", "2", "3", "0", "0",
                       "0.7071067811865476", "0.7071067811865476"]
    assert (root / "config/estimator_config.yaml").read_bytes() == (
        source[1] / "estimator_config.yaml").read_bytes()
    assert manifest["streams"]["cam0"] == {
        "samples": 3, "first_timestamp_ns": 1000000001, "last_timestamp_ns": 1100000001}
    assert str(tmp_path) not in json.dumps(manifest)
    assert set(manifest["files"]) == {
        p.relative_to(root).as_posix() for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"}


def test_public_entry_points_do_not_accept_fixture_source_identity(tmp_path, source):
    archive, pin = _archive(tmp_path, source[0])
    with pytest.raises(ValueError, match="pinned calibrated room1"):
        dataset.prepare_dataset(archive, tmp_path / "rejected", source[1])
    assert not (tmp_path / "rejected").exists()
    root = tmp_path / "prepared"
    dataset._prepare_dataset(archive, root, source[1], pin)
    with pytest.raises(ValueError, match="manifest"):
        dataset.verify_prepared(root)


def test_prepare_is_deterministic_and_refuses_existing_output(tmp_path, source):
    archive, pin = _archive(tmp_path, source[0])
    first, second = tmp_path / "first", tmp_path / "second"
    dataset._prepare_dataset(archive, first, source[1], pin)
    dataset._prepare_dataset(archive, second, source[1], pin)
    before = (first / "manifest.json").read_bytes()
    assert before == (second / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        dataset._prepare_dataset(archive, first, source[1], pin)
    assert (first / "manifest.json").read_bytes() == before


def test_room1_manifest_bytes_keep_the_original_pre_room2_identity(tmp_path, source):
    root, _, _ = _prepare(tmp_path, source)
    # Recorded using the pre-room2 preparation implementation and this fixture.
    assert hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest() == (
        "93a099b8e3948c69e817a4ec3164cfc7abf79726f87a529ef8495ecfe59694a1")


def test_room2_is_explicit_and_retains_sensor_reference_separation(tmp_path, source, monkeypatch):
    files, config = source
    files = {name.replace(dataset.ARCHIVE_ROOT, dataset.ROOM2_ARCHIVE_ROOT, 1): value
             for name, value in files.items()}
    extras = []
    for camera in ("cam0", "cam1"):
        alias = tarfile.TarInfo(f"{dataset.ROOM2_ARCHIVE_ROOT}/dso/{camera}/images")
        alias.type = tarfile.SYMTYPE
        alias.linkname = f"../../mav0/{camera}/data"
        extras.append((alias, None))
    archive, identity = _archive(tmp_path, files, extras)
    pin = {**dataset.ROOM2, **{key: identity[key] for key in ("sha256", "md5", "size_bytes")}}
    monkeypatch.setattr(dataset, "ROOM2", pin)
    root = tmp_path / "prepared"
    manifest = dataset.prepare_dataset(archive, root, config, dataset_id=dataset.ROOM2_DATASET_ID)
    assert dataset.verify_prepared(root) == manifest
    assert manifest["dataset_id"] == dataset.ROOM2_DATASET_ID
    assert manifest["reference"]["source_path"] == f"{dataset.ROOM2_ARCHIVE_ROOT}/mav0/mocap0/data.csv"
    assert manifest["source"] == pin
    assert not (root / "dso").exists()
    assert all(name.startswith(("mav0/cam0/", "mav0/cam1/", "mav0/imu0/"))
               for name in dataset._inventory(root / "sensors"))
    with pytest.raises(ValueError, match="room1"):
        dataset.prepare_dataset(archive, tmp_path / "room1", config)
    manifest["dataset_id"] = dataset.DATASET_ID
    _save_manifest(root, manifest)
    with pytest.raises(ValueError, match="manifest"):
        dataset.verify_prepared(root)


def test_room2_rejects_a_room1_archive_even_with_fixture_checksum(tmp_path, source, monkeypatch):
    archive, identity = _archive(tmp_path, source[0])
    monkeypatch.setattr(dataset, "ROOM2", {
        **dataset.ROOM2, **{key: identity[key] for key in ("sha256", "md5", "size_bytes")}
    })
    with pytest.raises(ValueError, match="archive root"):
        dataset.prepare_dataset(archive, tmp_path / "prepared", source[1],
                                dataset_id=dataset.ROOM2_DATASET_ID)
    assert not (tmp_path / "prepared").exists()


@pytest.mark.parametrize("identity", [None, True, "tum-vi-room3-512-16", "room2", {}])
def test_recording_allowlist_rejects_unknown_identity_before_io(tmp_path, source, identity):
    with pytest.raises(ValueError, match="unsupported"):
        dataset.download_dataset(tmp_path / "archive", dataset_id=identity)
    with pytest.raises(ValueError, match="unsupported"):
        dataset.prepare_dataset(tmp_path / "absent", tmp_path / "prepared", source[1],
                                dataset_id=identity)
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "prepared").exists()


def test_known_dso_aliases_are_recognized_but_never_extracted(tmp_path, source):
    extras = []
    for camera in ("cam0", "cam1"):
        member = tarfile.TarInfo(f"{dataset.ARCHIVE_ROOT}/dso/{camera}/images")
        member.type = tarfile.SYMTYPE
        member.linkname = f"../../mav0/{camera}/data"
        extras.append((member, None))
    root, _, _ = _prepare(tmp_path, source, extras=extras)
    assert not (root / "dso").exists()
    assert not any(p.is_symlink() for p in root.rglob("*"))


@pytest.mark.parametrize("name,kind,target", [
    ("/escape", tarfile.REGTYPE, ""),
    (f"{dataset.ARCHIVE_ROOT}/../escape", tarfile.REGTYPE, ""),
    (f"{dataset.ARCHIVE_ROOT}/x\\escape", tarfile.REGTYPE, ""),
    ("another-recording/file", tarfile.REGTYPE, ""),
    (f"{dataset.ARCHIVE_ROOT}/mav0/cam0/alias", tarfile.SYMTYPE, "../mocap0/data.csv"),
    (f"{dataset.ARCHIVE_ROOT}/dso/cam0/images", tarfile.SYMTYPE, "../../mav0/mocap0"),
    (f"{dataset.ARCHIVE_ROOT}/unused", tarfile.LNKTYPE, "../mocap0/data.csv"),
    (f"{dataset.ARCHIVE_ROOT}/device", tarfile.CHRTYPE, ""),
])
def test_rejects_unsafe_archive_members_before_creating_output(tmp_path, source, name, kind, target):
    member = tarfile.TarInfo(name)
    member.type, member.linkname = kind, target
    archive, pin = _archive(tmp_path, source[0], [(member, b"" if kind == tarfile.REGTYPE else None)])
    out = tmp_path / "prepared"
    with pytest.raises(ValueError):
        dataset._prepare_dataset(archive, out, source[1], pin)
    assert not out.exists()


def test_duplicate_archive_members_are_rejected(tmp_path, source):
    name = next(iter(source[0]))
    extra = tarfile.TarInfo(name)
    archive, pin = _archive(tmp_path, source[0], [(extra, b"")])
    with pytest.raises(ValueError, match="duplicate"):
        dataset._prepare_dataset(archive, tmp_path / "prepared", source[1], pin)


@pytest.mark.parametrize("fault", ["unpaired", "fractional", "duplicate", "filename", "missing_image",
                                   "extra_image", "imu_nan", "bad_quaternion", "wrong_png"])
def test_rejects_broken_stream_semantics_and_cleans_partial_output(tmp_path, source, fault):
    files, config = source
    root = dataset.ARCHIVE_ROOT
    camera_csv = f"{root}/mav0/cam1/data.csv"
    image = f"{root}/mav0/cam1/data/1000000001.png"
    if fault == "unpaired":
        files[camera_csv] = files[camera_csv].replace(b"1000000001", b"1000000002")
        files[f"{root}/mav0/cam1/data/1000000002.png"] = files.pop(image)
    elif fault == "fractional":
        files[camera_csv] = files[camera_csv].replace(b"1000000001", b"1000000001.5")
    elif fault == "duplicate":
        files[camera_csv] = files[camera_csv].replace(b"1050000001", b"1000000001")
    elif fault == "filename":
        files[camera_csv] = files[camera_csv].replace(b"1000000001.png", b"../../reference.csv")
    elif fault == "missing_image":
        del files[image]
    elif fault == "extra_image":
        files[f"{root}/mav0/cam0/data/999.png"] = _png()
    elif fault == "imu_nan":
        imu = f"{root}/mav0/imu0/data.csv"
        files[imu] = files[imu].replace(b"9.81", b"nan")
    elif fault == "bad_quaternion":
        gt = dataset.REFERENCE["source_path"]
        files[gt] = files[gt].replace(b"0.7071067811865476", b"0.9")
    elif fault == "wrong_png":
        files[image] = b"not a PNG"
    archive, pin = _archive(tmp_path, files)
    out = tmp_path / "prepared"
    with pytest.raises(ValueError):
        dataset._prepare_dataset(archive, out, config, pin)
    assert not out.exists()


@pytest.mark.parametrize("change", ["changed_file", "new_sensor_file", "new_config_file",
                                    "file_symlink", "directory_symlink", "root_symlink"])
def test_verify_rejects_changes_and_links(tmp_path, source, change):
    root, _, pin = _prepare(tmp_path, source)
    if change == "changed_file":
        (root / "sensors/mav0/imu0/data.csv").write_text("changed")
    elif change == "new_sensor_file":
        (root / "sensors/mav0/truth.csv").write_text("truth")
    elif change == "new_config_file":
        (root / "config/additional.yaml").write_text("truth: true")
    elif change == "file_symlink":
        target = root / "config/estimator_config.yaml"
        target.unlink()
        target.symlink_to(source[1] / "estimator_config.yaml")
    elif change == "directory_symlink":
        (root / "sensors/mav0/extra").symlink_to(root / "reference", target_is_directory=True)
    elif change == "root_symlink":
        link = tmp_path / "linked"
        link.symlink_to(root, target_is_directory=True)
        root = link
    with pytest.raises(ValueError):
        dataset._verify_prepared(root, pin)


def test_verify_rejects_reference_in_sensor_tree_even_if_added_to_manifest(tmp_path, source):
    root, manifest, pin = _prepare(tmp_path, source)
    relative = "sensors/mav0/groundtruth.csv"
    target = root / relative
    target.write_text("reference leak")
    manifest["files"][relative] = dataset._file_identity(target)
    _save_manifest(root, manifest)
    with pytest.raises(ValueError, match="unexpected file"):
        dataset._verify_prepared(root, pin)


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 1.0),
                                         ("source", {}), ("streams", {}), ("files", []),
                                         ("extra_reference_payload", [1, 2, 3])])
def test_verify_rejects_bad_manifest_schema(tmp_path, source, field, value):
    root, manifest, pin = _prepare(tmp_path, source)
    manifest[field] = value
    _save_manifest(root, manifest)
    with pytest.raises(ValueError):
        dataset._verify_prepared(root, pin)


def test_symlinked_config_is_rejected_before_output_creation(tmp_path, source):
    (source[1] / "linked.yaml").symlink_to(source[1] / "estimator_config.yaml")
    archive, pin = _archive(tmp_path, source[0])
    with pytest.raises(ValueError):
        dataset._prepare_dataset(archive, tmp_path / "prepared", source[1], pin)
    assert not (tmp_path / "prepared").exists()


def test_download_verifies_identity_and_never_overwrites(tmp_path, monkeypatch):
    content = b"a small checked response"
    pin = {**dataset.ROOM1, "size_bytes": len(content),
           "sha256": hashlib.sha256(content).hexdigest(), "md5": hashlib.md5(content).hexdigest()}
    monkeypatch.setattr(dataset.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(content))
    output = tmp_path / "room1.tar"
    assert dataset._download_dataset(output, pin) == pin
    assert output.read_bytes() == content
    assert not output.with_name("room1.tar.part").exists()
    with pytest.raises(FileExistsError):
        dataset._download_dataset(output, pin)
    assert output.read_bytes() == content


@pytest.mark.parametrize("content", [b"too long for the pinned response", b"wrong", b"tiny"])
def test_bad_download_does_not_publish_an_archive(tmp_path, monkeypatch, content):
    expected = b"right"
    pin = {**dataset.ROOM1, "size_bytes": len(expected),
           "sha256": hashlib.sha256(expected).hexdigest(), "md5": hashlib.md5(expected).hexdigest()}
    monkeypatch.setattr(dataset.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(content))
    output = tmp_path / "room1.tar"
    with pytest.raises(ValueError):
        dataset._download_dataset(output, pin)
    assert not output.exists()
    assert not output.with_name("room1.tar.part").exists()
