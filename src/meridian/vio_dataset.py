"""Acquire the pinned room1 benchmark and separate sensor inputs from references.

Preparation is intentionally specific to the calibrated TUM-VI EuRoC export.
It is not a general recording importer. Manifests identify files; they are not
signatures or an authorization boundary against a party who can replace both
the manifest and its files.
"""

import csv
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tarfile
import urllib.request


DATASET_ID = "tum-vi-room1-512-16"
ARCHIVE_ROOT = "dataset-room1_512_16"
ROOM1 = {
    "url": "https://vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room1_512_16.tar",
    "archive_name": "dataset-room1_512_16.tar",
    "sha256": "20354392eab5cc82c9770ed29cf0130c5acce0db54a257571499008fb34c398f",
    "md5": "51d2b8d93ee4ada47ec0b2d10115dcf8",
    "size_bytes": 1707110400,
}
LICENSE = {
    "spdx": "CC-BY-4.0",
    "url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "TUM VI Benchmark: David Schubert, Thore Goll, Nikolaus Demmel, "
                   "Vladyslav Usenko, Joerg Stueckler, Daniel Cremers (2018)",
    "dataset_url": "https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset",
}
REFERENCE = {
    "path": "reference/trajectory.csv",
    "source_path": f"{ARCHIVE_ROOT}/mav0/mocap0/data.csv",
    "quaternion": "xyzw",
    "rotation": "body_to_world",
    "body_frame": "imu",
    "timestamp_unit": "ns",
    "transformation": "Reorder q_RS from wxyz to xyzw; no time, frame, or scale correction.",
}
CAMERA_HEADER = ["#timestamp [ns]", "filename"]
IMU_HEADER = ["#timestamp [ns]", *[f"w_RS_S_{axis} [rad s^-1]" for axis in "xyz"],
              *[f"a_RS_S_{axis} [m s^-2]" for axis in "xyz"]]
MOCAP_HEADER = ["#timestamp [ns]", *[f"p_RS_R_{axis} [m]" for axis in "xyz"],
                *[f"q_RS_{axis} []" for axis in "wxyz"]]
REFERENCE_HEADER = ["timestamp_ns", "px_m", "py_m", "pz_m", "qx", "qy", "qz", "qw"]
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PNG_PATH = re.compile(r"sensors/mav0/cam[01]/data/[0-9]+\.png\Z")
_CONFIG_PATH = re.compile(r"config/[A-Za-z0-9_-]+\.(?:yaml|yml|png)\Z")
_SENSOR_CSV = {f"sensors/mav0/{name}/data.csv" for name in ("cam0", "cam1", "imu0")}
_MANIFEST_KEYS = {"schema_version", "dataset_id", "source", "license", "sensor_root",
                  "reference", "streams", "files"}
_IGNORED_DSO_LINKS = {
    f"{ARCHIVE_ROOT}/dso/{camera}/images": f"../../mav0/{camera}/data"
    for camera in ("cam0", "cam1")
}


def _plain_file(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"expected a regular file: {path.name}")


def _file_identity(path: Path) -> dict:
    _plain_file(path)
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            sha.update(block)
            size += len(block)
    return {"sha256": sha.hexdigest(), "size_bytes": size}


def _archive_identity(path: Path) -> dict:
    _plain_file(path)
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            sha.update(block)
            md5.update(block)
            size += len(block)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "size_bytes": size}


def _check_archive_identity(identity: dict, pin: dict) -> None:
    if any(identity[key] != pin[key] for key in ("sha256", "md5", "size_bytes")):
        raise ValueError("archive does not match the pinned calibrated room1 recording")


def download_dataset(output_archive: Path) -> dict:
    """Download only the pinned room1 archive; refuse an existing destination."""
    return _download_dataset(Path(output_archive), ROOM1)


def _download_dataset(output_archive: Path, pin: dict) -> dict:
    if output_archive.exists() or output_archive.is_symlink():
        raise FileExistsError(output_archive)
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_archive.with_name(output_archive.name + ".part")
    created = False
    try:
        with temporary.open("xb") as target:
            created = True
            sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
            with urllib.request.urlopen(pin["url"], timeout=120) as response:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > pin["size_bytes"]:
                        raise ValueError("download exceeds the pinned archive size")
                    sha.update(block)
                    md5.update(block)
                    target.write(block)
        _check_archive_identity({"sha256": sha.hexdigest(), "md5": md5.hexdigest(),
                                 "size_bytes": size}, pin)
        # A same-directory hard link publishes without replacing an existing path.
        os.link(temporary, output_archive)
        return dict(pin)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _relative_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("invalid relative file path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("unsafe relative file path")
    return path


def _inventory(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("expected a regular directory")
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_dir():
                raise ValueError("symlink or special directory is not supported")
        for name in filenames:
            path = Path(directory) / name
            _plain_file(path)
            files[path.relative_to(root).as_posix()] = path
    return dict(sorted(files.items()))


def _rows(path: Path, header: list[str]) -> list[list[str]]:
    _plain_file(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        actual = next(reader, None)
        if actual is None or [part.strip() for part in actual] != header:
            raise ValueError(f"unexpected CSV header: {path.name}")
        rows = []
        previous = -1
        for row in reader:
            row = [value.strip() for value in row]
            if len(row) != len(header):
                raise ValueError(f"incorrect CSV field count: {path.name}")
            timestamp = _timestamp(row[0])
            if timestamp <= previous:
                raise ValueError(f"timestamps must strictly increase: {path.name}")
            previous = timestamp
            rows.append(row)
    if len(rows) < 2:
        raise ValueError(f"at least two samples are required: {path.name}")
    return rows


def _timestamp(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError("timestamp must be an integer number of nanoseconds")
    number = int(value)
    if number > 2**63 - 1:
        raise ValueError("timestamp exceeds the signed 64-bit range")
    return number


def _finite(values: list[str]) -> list[float]:
    numbers = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("non-finite sensor or reference value")
    return numbers


def _stream_summary(rows: list[list[str]]) -> dict:
    return {"samples": len(rows), "first_timestamp_ns": int(rows[0][0]),
            "last_timestamp_ns": int(rows[-1][0])}


def _normalize_reference(source: Path, target: Path) -> None:
    rows = _rows(source, MOCAP_HEADER)
    target.parent.mkdir(parents=True)
    with target.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(REFERENCE_HEADER)
        for row in rows:
            values = _finite(row[1:])
            if not math.isclose(sum(value * value for value in values[3:]), 1.,
                                abs_tol=1e-6, rel_tol=0):
                raise ValueError("reference quaternion is not unit length")
            # q_RS maps the IMU/body frame into the reference/world frame.
            # Retain source decimal values; only change the component order.
            writer.writerow([*row[:4], *row[5:8], row[4]])


def _png_header(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(33)
    if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[8:16] != b"\x00\x00\x00\rIHDR"
            or struct.unpack(">IIBBBBB", header[16:29]) != (512, 512, 16, 0, 0, 0, 0)):
        raise ValueError("expected a 512x512 16-bit grayscale PNG")


def _streams(root: Path, file_names: set[str]) -> dict:
    cameras = {}
    expected_images = set()
    for name in ("cam0", "cam1"):
        rows = _rows(root / f"sensors/mav0/{name}/data.csv", CAMERA_HEADER)
        for row in rows:
            if row[1] != f"{row[0]}.png":
                raise ValueError("camera filename must match its timestamp")
            image = f"sensors/mav0/{name}/data/{row[1]}"
            if image not in file_names:
                raise ValueError("camera CSV refers to a missing image")
            _png_header(root / image)
            expected_images.add(image)
        cameras[name] = rows
    if [row[0] for row in cameras["cam0"]] != [row[0] for row in cameras["cam1"]]:
        raise ValueError("the reference requires exactly paired stereo timestamps")
    actual_images = {name for name in file_names if _PNG_PATH.fullmatch(name)}
    if actual_images != expected_images:
        raise ValueError("unreferenced camera images are not supported")
    imu = _rows(root / "sensors/mav0/imu0/data.csv", IMU_HEADER)
    for row in imu:
        _finite(row[1:])
    reference = _rows(root / REFERENCE["path"], REFERENCE_HEADER)
    for row in reference:
        values = _finite(row[1:])
        if not math.isclose(sum(value * value for value in values[3:]), 1.,
                            abs_tol=1e-6, rel_tol=0):
            raise ValueError("reference quaternion is not unit length")
    return {**{name: _stream_summary(rows) for name, rows in cameras.items()},
            "imu0": _stream_summary(imu), "reference": _stream_summary(reference)}


def prepare_dataset(archive: Path, output: Path, config_source: Path) -> dict:
    """Prepare room1 with sensor-only input, separate truth, and exact config copies."""
    return _prepare_dataset(Path(archive), Path(output), Path(config_source), ROOM1)


def _prepare_dataset(archive: Path, output: Path, config_source: Path, pin: dict) -> dict:
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    _check_archive_identity(_archive_identity(archive), pin)
    configs = _inventory(config_source)
    if not configs or any(not _CONFIG_PATH.fullmatch(f"config/{name}") for name in configs):
        raise ValueError("configuration must contain only top-level YAML and PNG files")
    with tarfile.open(archive, "r:") as source:
        members = {}
        for member in source:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = _relative_name(name)
            if path.parts[0] != ARCHIVE_ROOT:
                raise ValueError("unexpected archive root")
            if name in members:
                raise ValueError("duplicate archive member")
            # The official export also contains two unused DSO image aliases.
            # Recognize only those exact metadata entries, never extract them.
            ignored_alias = (member.issym() and name in _IGNORED_DSO_LINKS
                             and member.linkname == _IGNORED_DSO_LINKS[name])
            if not member.isfile() and not member.isdir() and not ignored_alias:
                raise ValueError("archive links and special files are not supported")
            members[name] = member
        selected = {}
        mocap = None
        for name, member in members.items():
            if not member.isfile():
                continue
            relative = name.removeprefix(ARCHIVE_ROOT + "/")
            target = "sensors/" + relative
            if target in _SENSOR_CSV or _PNG_PATH.fullmatch(target):
                selected[target] = member
            elif name == REFERENCE["source_path"]:
                mocap = member
        if not _SENSOR_CSV.issubset(selected) or mocap is None:
            raise ValueError("archive is missing a required sensor or reference stream")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        try:
            for name, member in selected.items():
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as content, target.open("xb") as out:
                    shutil.copyfileobj(content, out)
            # The temporary reference is never written inside the sensor tree.
            raw_reference = output / "source-reference.csv"
            with source.extractfile(mocap) as content, raw_reference.open("xb") as out:
                shutil.copyfileobj(content, out)
            _normalize_reference(raw_reference, output / REFERENCE["path"])
            raw_reference.unlink()
            (output / "config").mkdir()
            for name, original in configs.items():
                shutil.copyfile(original, output / "config" / name, follow_symlinks=False)
            files = _inventory(output)
            streams = _streams(output, set(files))
            manifest = {
                "schema_version": 1, "dataset_id": DATASET_ID, "source": dict(pin),
                "license": dict(LICENSE), "sensor_root": "sensors/mav0",
                "reference": dict(REFERENCE), "streams": streams,
                "files": {name: _file_identity(path) for name, path in files.items()},
            }
            (output / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return _verify_prepared(output, pin)
        except BaseException:
            shutil.rmtree(output)
            raise


def verify_prepared(path: Path) -> dict:
    """Verify the pinned source, exact file inventory, hashes, and stream semantics."""
    return _verify_prepared(Path(path), ROOM1)


def _verify_prepared(path: Path, pin: dict) -> dict:
    files = _inventory(path)
    if "manifest.json" not in files:
        raise ValueError("prepared dataset has no manifest")
    manifest = json.loads(files.pop("manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("dataset_id") != DATASET_ID
            or manifest.get("source") != pin or manifest.get("license") != LICENSE
            or manifest.get("sensor_root") != "sensors/mav0"
            or manifest.get("reference") != REFERENCE):
        raise ValueError("unsupported or inconsistent prepared dataset manifest")
    recorded = manifest.get("files")
    if not isinstance(recorded, dict) or set(recorded) != set(files):
        raise ValueError("prepared file inventory differs from the manifest")
    for name, identity in recorded.items():
        _relative_name(name)
        if (name not in _SENSOR_CSV and name != REFERENCE["path"]
                and not _PNG_PATH.fullmatch(name) and not _CONFIG_PATH.fullmatch(name)):
            raise ValueError("unexpected file in prepared dataset")
        if (not isinstance(identity, dict) or set(identity) != {"sha256", "size_bytes"}
                or not isinstance(identity["sha256"], str)
                or not _DIGEST.fullmatch(identity["sha256"])
                or type(identity["size_bytes"]) is not int or identity["size_bytes"] < 0):
            raise ValueError("invalid file identity in prepared manifest")
        if _file_identity(files[name]) != identity:
            raise ValueError(f"prepared file differs from manifest: {name}")
    if not _SENSOR_CSV.issubset(files) or REFERENCE["path"] not in files:
        raise ValueError("prepared dataset is missing required streams")
    if not any(name.startswith("config/") for name in files):
        raise ValueError("prepared dataset has no configuration")
    if _streams(path, set(files)) != manifest.get("streams"):
        raise ValueError("prepared stream summaries differ from the manifest")
    return manifest


def camera_timestamps_ns(prepared: Path) -> list[int]:
    """Read exact integer camera timestamps; call verify_prepared before execution."""
    rows = _rows(Path(prepared) / "sensors/mav0/cam0/data.csv", CAMERA_HEADER)
    return [int(row[0]) for row in rows]
