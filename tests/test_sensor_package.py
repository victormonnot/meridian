"""Adversarial local-package checks, independent of any estimator behavior."""

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tarfile
import zipfile
import zlib

from PIL import Image
import pytest

import meridian.sensor_package as package_module
from meridian.sensor_package import (
    DEFAULT_LIMITS, PackageLimits, PackageValidationError, copy_regular,
    file_identity, iter_stream_rows, materialize_package, materialize_source,
    validate_package,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/sensor-contracts"


def package(tmp_path, name="stereo-no-reference"):
    root = tmp_path / "package"
    root.mkdir()
    acquisition = json.loads((EXAMPLES / f"{name}.json").read_text())
    for entry in acquisition["files"]:
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / entry["path"], target)
    save(root, acquisition)
    return root, acquisition


def save(root, acquisition):
    (root / "acquisition.json").write_text(json.dumps(acquisition) + "\n", encoding="utf-8")


def refresh(root, acquisition):
    for entry in acquisition["files"]:
        content = (root / entry["path"]).read_bytes()
        entry.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    save(root, acquisition)


def contents(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def failure(code, function, *args, **kwargs):
    with pytest.raises(PackageValidationError) as error:
        function(*args, **kwargs)
    assert error.value.finding["code"] == code
    assert str(error.value) == error.value.finding["message"]
    assert not str(error.value.finding.get("path", "")).startswith("/")
    return error.value.finding


def tar_bytes(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, payload, kind, link in entries:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, link
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def png_chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png(*, width=8, height=6, bits=8, color=0, interlace=0, pixels=None, extra=b""):
    if pixels is None:
        pixels = bytes(height * (1 + width * (bits // 8)))
    header = struct.pack(">IIBBBBB", width, height, bits, color, 0, 0, interlace)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", zlib.compress(pixels)) + extra + png_chunk(b"IEND", b""))


@pytest.mark.parametrize("name,rows,images,reference", [
    ("stereo-no-reference", 7, 4, "absent"),
    ("stereo-partial-reference", 8, 4, "present"),
    ("imu-snapshots", 4, 0, "absent"),
    ("unknown-metadata", 7, 4, "absent"),
])
def test_real_payload_validation_is_deterministic_and_does_not_claim_execution(tmp_path, name, rows, images, reference):
    root, acquisition = package(tmp_path, name)
    before = contents(root)
    result = validate_package(root)
    assert result == validate_package(root)
    assert result["acquisition"] == acquisition
    report = result["report"]
    assert (report["rows"], report["images"], report["reference_availability"]) == (rows, images, reference)
    assert report["payload_verification"] == "verified"
    assert report["execution_readiness"] == report["calibration_validity"] == "not_assessed"
    assert report["metadata_assessment"]["payload_verification"] == "not_performed"
    assert contents(root) == before
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("container", ["directory", "tar", "tar.gz", "zip"])
def test_materialization_preserves_every_byte_and_validates_the_same_content(tmp_path, container):
    root, _ = package(tmp_path)
    before = contents(root)
    source = root
    if container.startswith("tar"):
        source = tmp_path / ("source." + container)
        with tarfile.open(source, "w:gz" if container.endswith("gz") else "w") as archive:
            for name in sorted(before):
                archive.add(root / name, arcname=name)
    elif container == "zip":
        source = tmp_path / "source.zip"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(before):
                archive.write(root / name, arcname=name)
    output = tmp_path / "output"
    assert materialize_package(source, output) == validate_package(root)
    assert contents(root) == contents(output) == before
    with pytest.raises(FileExistsError):
        materialize_source(source, output)
    assert contents(output) == before


@pytest.mark.parametrize("mutation,code", [
    ("missing", "inventory_mismatch"), ("extra", "inventory_mismatch"),
    ("empty_directory", "inventory_mismatch"), ("bytes", "file_identity"),
    ("size", "file_identity"), ("self_inventory", "reserved_path"),
    ("duplicate_json", "descriptor_invalid"), ("utf16_json", "descriptor_invalid"),
    ("missing_descriptor", "descriptor_missing"),
])
def test_exact_inventory_and_descriptor_identity(tmp_path, mutation, code):
    root, acquisition = package(tmp_path)
    path = root / acquisition["files"][0]["path"]
    if mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        (root / "unlisted.txt").write_text("extra")
    elif mutation == "empty_directory":
        (root / "empty").mkdir()
    elif mutation == "bytes":
        path.write_bytes(path.read_bytes().replace(b"0", b"1", 1))
    elif mutation == "size":
        acquisition["files"][0]["size_bytes"] += 1
        save(root, acquisition)
    elif mutation == "self_inventory":
        acquisition["files"].append({"path": "acquisition.json", "sha256": "0" * 64, "size_bytes": 0, "role": "evidence"})
        save(root, acquisition)
    elif mutation == "duplicate_json":
        (root / "acquisition.json").write_text('{"format":"x","format":"y"}')
    elif mutation == "utf16_json":
        (root / "acquisition.json").write_bytes(json.dumps(acquisition).encode("utf-16"))
    else:
        (root / "acquisition.json").unlink()
    failure(code, validate_package, root)
    output = tmp_path / "output"
    failure(code, materialize_package, root, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo"])
def test_links_and_special_directory_members_are_never_followed(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    if kind == "symlink":
        (source / "link").symlink_to(outside)
    elif kind == "directory_symlink":
        (source / "link").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, source / "link")
    else:
        os.mkfifo(source / "pipe")
    output = tmp_path / "output"
    failure("special_file", materialize_source, source, output)
    assert not output.exists()
    assert outside.read_bytes() == b"secret"


def test_source_and_destination_symlink_ancestors_and_recursive_outputs_are_refused(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source")
    link = tmp_path / "alias"
    link.symlink_to(source, target_is_directory=True)
    failure("unsafe_source", materialize_source, link, tmp_path / "out")
    failure("unsafe_source", file_identity, link / "data")
    failure("unsafe_source", copy_regular, link / "data", tmp_path / "copy")
    failure("unsafe_source", materialize_source, source, link / "nested")
    failure("destination_error", materialize_source, source, source / "nested")
    with pytest.raises(FileExistsError):
        materialize_source(source, source)
    assert sorted(path.name for path in source.iterdir()) == ["data"]


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../escape", "a//b", "a\\b", "a:b", "./file", "a/\x01b"])
@pytest.mark.parametrize("container", ["tar", "zip"])
def test_unsafe_archive_names_are_rejected_without_outputs(tmp_path, name, container):
    source = tmp_path / ("source." + container)
    if container == "tar":
        tar_bytes(source, [(name, b"payload", tarfile.REGTYPE, "")])
    else:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(name, b"payload")
    output = tmp_path / "output"
    failure("unsafe_path", materialize_source, source, output)
    assert not output.exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("container", ["tar", "zip"])
def test_duplicate_archive_members_are_rejected(tmp_path, container):
    source = tmp_path / ("source." + container)
    if container == "tar":
        tar_bytes(source, [("file", b"one", tarfile.REGTYPE, ""), ("file", b"two", tarfile.REGTYPE, "")])
    else:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("file", b"one")
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("file", b"two")
    failure("duplicate_member", materialize_source, source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.GNUTYPE_SPARSE])
def test_tar_links_and_special_types_are_rejected(tmp_path, kind):
    source = tmp_path / "source.tar"
    tar_bytes(source, [("file", b"", kind, "elsewhere")])
    failure("archive_member", materialize_source, source, tmp_path / "output")


def test_only_explicit_exact_upstream_archive_alias_can_be_ignored(tmp_path):
    source = tmp_path / "source.tar"
    alias = "dataset/dso/cam0/images"
    target = "../../mav0/cam0/data"
    tar_bytes(source, [(alias, b"", tarfile.SYMTYPE, target), ("dataset/mav0/data", b"original", tarfile.REGTYPE, "")])
    failure("archive_member", materialize_source, source, tmp_path / "refused")
    failure("archive_member", materialize_source, source, tmp_path / "wrong", ignored_aliases={alias: "other"})
    inventory = materialize_source(source, tmp_path / "accepted", ignored_aliases={alias: target})
    assert list(inventory) == ["dataset/mav0/data"]
    assert not (tmp_path / "accepted" / alias).exists()


def test_zip_symlink_and_unsupported_compression(tmp_path):
    for label, mode, compression in [("link", stat.S_IFLNK, zipfile.ZIP_STORED), ("bzip", stat.S_IFREG, zipfile.ZIP_BZIP2)]:
        source = tmp_path / f"{label}.zip"
        member = zipfile.ZipInfo("file")
        member.create_system, member.external_attr, member.compress_type = 3, mode << 16, compression
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(member, b"elsewhere")
        failure("archive_member", materialize_source, source, tmp_path / label)


def test_zip_directory_metadata_and_payload_corruption_are_bounded(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("file", b"payload")
    failure("archive_limit", materialize_source, source, tmp_path / "limited", limits=replace(DEFAULT_LIMITS, max_archive_metadata_bytes=20))
    content = source.read_bytes()
    source.write_bytes(content.replace(b"payload", b"payloae", 1))
    failure("source_invalid", materialize_source, source, tmp_path / "corrupt")
    source.write_bytes(content[:-12])
    failure("archive_format", materialize_source, source, tmp_path / "truncated")


def test_forced_zip64_is_explicitly_unsupported_even_for_a_small_member(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        with archive.open("file", "w", force_zip64=True) as target:
            target.write(b"small")
    failure("archive_member", materialize_source, source, tmp_path / "output")


@pytest.mark.parametrize("container", ["tar", "zip"])
def test_archive_file_directory_collisions_and_unlisted_empty_directories(tmp_path, container):
    source = tmp_path / ("source." + container)
    if container == "tar":
        tar_bytes(source, [("parent", b"file", tarfile.REGTYPE, ""),
                           ("parent/child", b"file", tarfile.REGTYPE, "")])
    else:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("parent", b"file")
            archive.writestr("parent/child", b"file")
    failure("source_invalid", materialize_source, source, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    root, _ = package(tmp_path)
    with tarfile.open(tmp_path / "empty.tar", "w") as archive:
        for name in contents(root):
            archive.add(root / name, arcname=name)
        member = tarfile.TarInfo("unlisted-empty")
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
    failure("inventory_mismatch", materialize_package, tmp_path / "empty.tar", tmp_path / "empty")


def test_tar_extended_headers_are_bounded_before_their_payload_is_read(tmp_path):
    source = tmp_path / "source.tar"
    with tarfile.open(source, "w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("file")
        info.size = 1
        info.pax_headers = {"comment": "x" * 1000}
        archive.addfile(info, io.BytesIO(b"x"))
    failure("archive_limit", materialize_source, source, tmp_path / "output", limits=replace(DEFAULT_LIMITS, max_archive_metadata_bytes=100))
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("limit,value,code", [
    ("max_total_bytes", 100, "total_limit"), ("max_file_bytes", 100, "file_limit"),
    ("max_members", 2, "member_limit"), ("max_descriptor_bytes", 10, "file_limit"),
    ("max_csv_bytes", 20, "file_limit"), ("max_csv_rows", 3, "row_limit"),
    ("max_image_pixels", 10, "image_dimensions"), ("max_png_bytes", 50, "file_limit"),
])
def test_explicit_limits_apply_before_large_payload_work(tmp_path, limit, value, code):
    root, _ = package(tmp_path)
    failure(code, validate_package, root, limits=replace(DEFAULT_LIMITS, **{limit: value}))


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_resource_limits_are_rejected(value):
    with pytest.raises(ValueError):
        PackageLimits(max_csv_rows=value)


@pytest.mark.parametrize("column,value,code", [
    (0, "00", "sample_identity"), (0, "-1", "sample_identity"), (0, "true", "sample_identity"),
    (2, "1.0", "timestamp"), (2, " 1", "timestamp"), (2, "+1", "timestamp"),
    (2, "9223372036854775808", "timestamp"),
    (3, "NaN", "numeric_value"), (3, "inf", "numeric_value"), (3, "true", "numeric_value"),
    (3, "1e999", "numeric_value"), (3, " 1", "numeric_value"),
])
def test_csv_rejects_ambiguous_ids_ticks_and_nonfinite_measurements(tmp_path, column, value, code):
    root, acquisition = package(tmp_path, "imu-snapshots")
    path = root / acquisition["streams"][0]["index_path"]
    lines = path.read_text().splitlines()
    row = lines[1].split(",")
    row[column] = value
    lines[1] = ",".join(row)
    path.write_text("\n".join(lines) + "\n")
    refresh(root, acquisition)
    finding = failure(code, validate_package, root)
    assert finding["stream_id"] == "imu0" and finding["row"] == 2


@pytest.mark.parametrize("mutation,code", [
    ("header", "csv_header"), ("columns", "csv_columns"), ("longline", "csv_columns"),
    ("utf8", "csv_invalid"), ("quotes", "csv_invalid"),
    ("duplicate_id", "sample_identity"), ("duplicate_tick", "timestamp_order"),
    ("backwards_tick", "timestamp_order"), ("segment_reorder", "segment_summary"),
    ("missing_row", "segment_summary"), ("endpoint", "segment_summary"),
])
def test_csv_headers_order_segments_and_actual_counts(tmp_path, mutation, code):
    root, acquisition = package(tmp_path, "imu-snapshots")
    path = root / acquisition["streams"][0]["index_path"]
    lines = path.read_text().splitlines()
    if mutation == "header":
        lines[0] = lines[0].replace("wx", "gx")
    elif mutation == "columns":
        lines[1] += ",extra"
    elif mutation == "longline":
        lines[1] += ",x" * 20000
    elif mutation == "utf8":
        path.write_bytes(b"\xff")
    elif mutation == "quotes":
        lines[1] = '"unterminated'
    elif mutation in {"duplicate_id", "duplicate_tick", "backwards_tick"}:
        row1, row2 = lines[1].split(","), lines[2].split(",")
        index = 0 if mutation == "duplicate_id" else 2
        row2[index] = row1[index] if mutation != "backwards_tick" else str(int(row1[index]) - 1)
        lines[2] = ",".join(row2)
    elif mutation == "segment_reorder":
        lines[1], lines[3] = lines[3], lines[1]
    elif mutation == "missing_row":
        del lines[-1]
    else:
        acquisition["streams"][0]["segments"][0]["last_tick"] = "1040001"
    if mutation != "utf8":
        path.write_text("\n".join(lines) + "\n")
    refresh(root, acquisition)
    failure(code, validate_package, root)


def test_sample_id_holes_reset_segments_and_singletons_preserve_original_rows(tmp_path):
    root, acquisition = package(tmp_path, "imu-snapshots")
    stream = acquisition["streams"][0]
    path = root / stream["index_path"]
    lines = path.read_text().splitlines()
    kept = [lines[1].split(","), lines[3].split(",")]
    for row, sample, segment in zip(kept, (4, 99), stream["segments"], strict=True):
        row[0] = str(sample)
        segment.update(last_tick=row[2], sample_count=1)
    path.write_text(lines[0] + "\n" + "\n".join(",".join(row) for row in kept) + "\n")
    refresh(root, acquisition)
    assert validate_package(root)["report"]["rows"] == 2
    assert list(iter_stream_rows(root, stream)) == kept
    partial = iter_stream_rows(root, stream)
    assert next(partial) == kept[0]
    partial.close()


@pytest.mark.parametrize("mutation,code", [
    ("crc", "image_crc"), ("truncated", "image_truncated"), ("trailing", "image_structure"),
    ("dimensions", "image_dimensions"), ("encoding", "image_encoding"),
    ("interlace", "image_encoding"), ("expansion", "image_expansion"),
    ("filter", "image_decode"), ("critical", "image_encoding"),
    ("reserved_chunk", "image_structure"),
])
def test_png_requires_complete_bounded_decode_and_valid_structure(tmp_path, mutation, code):
    root, acquisition = package(tmp_path)
    path = root / "sensors/cam0/images/0.png"
    content = png()
    if mutation == "crc":
        content = content[:29] + bytes([content[29] ^ 1]) + content[30:]
    elif mutation == "truncated":
        content = content[:-6]
    elif mutation == "trailing":
        content += b"extra"
    elif mutation == "dimensions":
        content = png(width=9)
    elif mutation == "encoding":
        content = png(bits=16)
    elif mutation == "interlace":
        content = png(interlace=1)
    elif mutation == "expansion":
        content = png(pixels=b"x" * 100000)
    elif mutation == "filter":
        content = png(pixels=(b"\xff" + bytes(8)) * 6)
    elif mutation == "critical":
        content = png(extra=png_chunk(b"TEST", b""))
    else:
        content = png(extra=png_chunk(b"test", b""))
    path.write_bytes(content)
    refresh(root, acquisition)
    failure(code, validate_package, root)


def test_valid_animated_png_cannot_be_silently_reduced_to_its_first_frame(tmp_path):
    root, acquisition = package(tmp_path)
    path = root / "sensors/cam0/images/0.png"
    first, second = Image.new("L", (8, 6), 10), Image.new("L", (8, 6), 200)
    first.save(path, save_all=True, append_images=[second], duration=20, loop=0)
    with Image.open(path) as animation:
        assert animation.is_animated and animation.n_frames == 2
        animation.seek(1)
        animation.load()
        assert animation.getpixel((0, 0)) == 200
    refresh(root, acquisition)
    failure("image_encoding", validate_package, root)


def test_image_paths_roles_and_unreferenced_sensor_files_are_checked(tmp_path):
    root, acquisition = package(tmp_path)
    image = next(item for item in acquisition["files"] if item["path"].endswith("0.png"))
    image["role"] = "evidence"
    save(root, acquisition)
    failure("image_inventory", validate_package, root)
    image["role"] = "sensor"
    extra = root / "sensors/extra.bin"
    extra.write_bytes(b"unused")
    acquisition["files"].append({"path": "sensors/extra.bin", "role": "sensor", **file_identity(extra)})
    save(root, acquisition)
    failure("unreferenced_payload", validate_package, root)


def test_reference_quaternion_is_validated_without_requiring_full_coverage(tmp_path):
    root, acquisition = package(tmp_path, "stereo-partial-reference")
    assert validate_package(root)["report"]["reference_availability"] == "present"
    stream = next(item for item in acquisition["streams"] if item["kind"] == "reference_pose")
    path = root / stream["index_path"]
    lines = path.read_text().splitlines()
    row = lines[1].split(",")
    row[-1] = "2"
    lines[1] = ",".join(row)
    path.write_text("\n".join(lines) + "\n")
    refresh(root, acquisition)
    failure("quaternion", validate_package, root)


def test_actual_stereo_interior_timestamps_are_checked_beyond_metadata_endpoints(tmp_path):
    root, acquisition = package(tmp_path)
    for offset, stream in enumerate(acquisition["streams"][:2]):
        path = root / stream["index_path"]
        lines = path.read_text().splitlines()
        row = lines[1].split(",")
        row[0], row[2], row[3] = "1", str(int(row[2]) + 100 + offset), "images/middle.png"
        last = lines[2].split(",")
        last[0] = "2"
        path.write_text("\n".join([lines[0], lines[1], ",".join(row), ",".join(last)]) + "\n")
        image = path.parent / row[3]
        image.write_bytes(png())
        acquisition["files"].append({"path": image.relative_to(root).as_posix(), "role": "sensor", **file_identity(image)})
        stream["segments"][0]["sample_count"] = 3
    refresh(root, acquisition)
    failure("stereo_pairing", validate_package, root)


def test_originals_and_evidence_are_preserved_and_hashed_but_not_interpreted(tmp_path):
    root, acquisition = package(tmp_path)
    for role in ("original", "evidence"):
        path = root / role / "opaque.bin"
        path.parent.mkdir()
        path.write_bytes(b"\x00\xffopaque original bytes")
        acquisition["files"].append({"path": path.relative_to(root).as_posix(), "role": role, **file_identity(path)})
    save(root, acquisition)
    before = contents(root)
    validate_package(root)
    assert contents(root) == before


def test_copy_regular_is_exact_nonoverwriting_and_rejects_hardlinks(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(bytes(range(256)))
    output = tmp_path / "new" / "copy"
    assert copy_regular(source, output) == file_identity(source)
    assert output.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        copy_regular(source, output)
    os.link(source, tmp_path / "alias")
    failure("special_file", copy_regular, source, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()


def test_copy_detects_source_mutation_and_removes_its_partial_output(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(bytes(range(128)))
    sha256 = hashlib.sha256

    class MutatingDigest:
        def __init__(self):
            self.digest, self.changed = sha256(), False

        def update(self, chunk):
            if not self.changed:
                self.changed = True
                with source.open("ab") as stream:
                    stream.write(b"changed during copy")
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(package_module, "_CHUNK", 8)
    monkeypatch.setattr(package_module.hashlib, "sha256", MutatingDigest)
    failure("source_changed", copy_regular, source, output)
    assert not output.exists()
