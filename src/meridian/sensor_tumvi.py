"""Adapt a local calibrated TUM-VI EuRoC export into a sensor package.

Selecting this adapter declares the calibrated 512_16 source profile; names do
not authenticate a recording. Original bytes are retained, images are copied,
and CSV numbers keep their source decimal text. No calibration, synchronization,
image conversion, estimator execution or network acquisition occurs here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile

from .sensor_contract import FORMAT, SCHEMA_VERSION, validate_contract
from .sensor_conventions import timestamp_ns, validate_quaternion_xyzw
from .sensor_package import (
    DEFAULT_LIMITS, file_identity, materialize_source, validate_package,
)


CAMERA_HEADER = ["#timestamp [ns]", "filename"]
IMU_HEADER = [
    "#timestamp [ns]",
    *[f"w_RS_S_{axis} [rad s^-1]" for axis in "xyz"],
    *[f"a_RS_S_{axis} [m s^-2]" for axis in "xyz"],
]
MOCAP_HEADER = [
    "#timestamp [ns]",
    *[f"p_RS_R_{axis} [m]" for axis in "xyz"],
    *[f"q_RS_{axis} []" for axis in "wxyz"],
]
_SEQUENCE = re.compile(
    r"(?:room[1-6]|corridor[1-5]|magistrale[1-6]|outdoors[1-8]|slides[1-3]"
    r"|calib-cam[1-8]|calib-imu[1-4])\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}\Z")
_LICENSE = "CC-BY-4.0"
_ATTRIBUTION = (
    "TUM VI Benchmark: David Schubert, Thore Goll, Nikolaus Demmel, "
    "Vladyslav Usenko, Joerg Stueckler, Daniel Cremers (2018)"
)
_SOURCE = "https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _copy_regular(source, destination, *, limits):
    """Copy a regular file through a non-following descriptor, checking its bytes."""
    before = file_identity(source, limits=limits)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise ValueError("TUM-VI source must be a regular file.")
            sha, size = hashlib.sha256(), 0
            while block := incoming.read(1024 * 1024):
                size += len(block)
                if size > limits.max_file_bytes:
                    raise ValueError("TUM-VI source exceeds the file limit.")
                sha.update(block)
                outgoing.write(block)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if {"sha256": sha.hexdigest(), "size_bytes": size} != before:
        raise ValueError("TUM-VI source changed while copying.")
    if file_identity(source, limits=limits) != before:
        raise ValueError("TUM-VI source changed while copying.")
    return before


def _csv_rows(path, header, *, limits):
    if path.stat().st_size > limits.max_csv_bytes:
        raise ValueError("TUM-VI CSV exceeds its byte limit.")
    previous = None
    rows = []
    def bounded_lines(handle):
        while line := handle.readline(32 * 1024 + 1):
            if len(line) > 32 * 1024:
                raise ValueError("TUM-VI CSV physical lines exceed 32768 characters.")
            yield line
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(bounded_lines(handle), strict=True)
            actual = next(reader, None)
            if actual is None or [value.strip() for value in actual] != header:
                raise ValueError("Unexpected calibrated TUM-VI CSV header.")
            for row in reader:
                if len(rows) >= limits.max_csv_rows:
                    raise ValueError("TUM-VI CSV exceeds its row limit.")
                row = [value.strip() for value in row]
                if len(row) != len(header):
                    raise ValueError("TUM-VI CSV has an incorrect field count.")
                stamp = timestamp_ns(row[0])
                if stamp < 0 or (previous is not None and stamp <= previous):
                    raise ValueError("TUM-VI timestamps must increase without reordering.")
                previous = stamp
                rows.append(row)
    except (csv.Error, UnicodeError) as error:
        raise ValueError("TUM-VI CSV must contain valid bounded UTF-8 rows.") from error
    if not rows:
        raise ValueError("TUM-VI CSV has no observations.")
    return rows


def _finite(values):
    try:
        numbers = [float(value) for value in values]
    except (ValueError, OverflowError) as error:
        raise ValueError("TUM-VI measurement must be a finite number.") from error
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("TUM-VI measurement must be a finite number.")
    return numbers


def _csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _segment(rows):
    return [{
        "id": "segment0",
        "first_tick": rows[0][0],
        "last_tick": rows[-1][0],
        "sample_count": len(rows),
    }]


def _stream(name, kind, rows, description):
    return {
        "id": name,
        "kind": kind,
        "sensor_instance": "tumvi-" + name,
        "frame_id": name if kind == "camera" else "imu",
        "clock_id": "tumvi-clock",
        "timestamp_semantics": "exposure_midpoint" if kind == "camera" else "sample",
        "measurement_semantics": {
            "camera": "image", "imu": "instantaneous_imu", "reference_pose": "pose",
        }[kind],
        "index_path": (
            f"references/{name}/data.csv"
            if kind == "reference_pose" else f"sensors/{name}/data.csv"
        ),
        "segments": _segment(rows),
        "processing": {
            "calibration": "not_applied" if kind == "camera" else "applied",
            "filtering": "unknown" if kind == "imu" else "applied",
            "clock_correction": "applied",
            "description": description,
        },
    }


def _source_root(materialized, inventory, sequence):
    expected = f"dataset-{sequence}_512_16"
    if (materialized / "mav0").is_dir():
        root = materialized
    elif (materialized / expected / "mav0").is_dir():
        if any(path.split("/", 1)[0] != expected for path in inventory):
            raise ValueError("TUM-VI input contains another archive root.")
        root = materialized / expected
    else:
        raise ValueError("TUM-VI input does not match the declared calibrated sequence.")
    allowed = {"cam0", "cam1", "imu0", "mocap0"}
    if any(path.name not in allowed for path in (root / "mav0").iterdir()):
        raise ValueError("Unsupported additional TUM-VI sensor stream.")
    return root


def _allowed_aliases(sequence):
    prefix = f"dataset-{sequence}_512_16"
    return {
        f"{prefix}/dso/{camera}/images": f"../../mav0/{camera}/data"
        for camera in ("cam0", "cam1")
    }


def _archive_aliases(archive, allowed):
    """Record exact skipped aliases after bounded materialization succeeds."""
    observed = []
    try:
        with tarfile.open(archive, "r:") as handle:
            for member in handle:
                if member.issym() and allowed.get(member.name) == member.linkname:
                    observed.append({"path": member.name, "target": member.linkname})
    except tarfile.TarError as error:
        raise ValueError("TUM-VI archive must be an uncompressed tar.") from error
    return sorted(observed, key=lambda item: item["path"])


def _convert(root, package, *, include_reference, limits):
    streams, camera_rows = [], {}
    for name in ("cam0", "cam1"):
        rows = _csv_rows(root / f"mav0/{name}/data.csv", CAMERA_HEADER, limits=limits)
        expected = set()
        for row in rows:
            if row[1] != f"{row[0]}.png":
                raise ValueError("TUM-VI image filename must equal its timestamp.")
            expected.add(row[1])
            _copy_regular(
                root / f"mav0/{name}/data/{row[1]}",
                package / f"sensors/{name}/data/{row[1]}",
                limits=limits,
            )
        if {path.name for path in (root / f"mav0/{name}/data").iterdir()} != expected:
            raise ValueError("TUM-VI camera has unreferenced or missing images.")
        _csv(
            package / f"sensors/{name}/data.csv",
            ["sample_id", "segment_id", "timestamp", "filename"],
            ([str(i), "segment0", row[0], "data/" + row[1]] for i, row in enumerate(rows)),
        )
        stream = _stream(
            name, "camera", rows,
            "Publisher calibrated 512_16 export: downsampled unrectified image bytes; "
            "published clock corrections already represented. No pixel or camera "
            "calibration is applied by this adapter.",
        )
        stream["camera"] = {
            "resolution": [512, 512], "encoding": "mono16", "shutter": "global",
        }
        streams.append(stream)
        camera_rows[name] = rows
    if [row[0] for row in camera_rows["cam0"]] != [row[0] for row in camera_rows["cam1"]]:
        raise ValueError("TUM-VI cameras require exact timestamp pairs.")
    rows = _csv_rows(root / "mav0/imu0/data.csv", IMU_HEADER, limits=limits)
    for row in rows:
        _finite(row[1:])
    _csv(
        package / "sensors/imu0/data.csv",
        ["sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz"],
        ([str(i), "segment0", *row] for i, row in enumerate(rows)),
    )
    stream = _stream(
        "imu0", "imu", rows,
        "Publisher calibrated IMU axes, scale and approximate bias corrections "
        "and time corrections already represented; filtering is not inferred. "
        "No corrections, resampling or noise identification applied here.",
    )
    stream["imu"] = {
        "angular_velocity_unit": "rad/s", "specific_force_unit": "m/s^2",
        "local_gravity_m_s2": None,
    }
    streams.append(stream)
    mocap = root / "mav0/mocap0/data.csv"
    if include_reference and mocap.exists():
        rows = _csv_rows(mocap, MOCAP_HEADER, limits=limits)
        for row in rows:
            numbers = _finite(row[1:])
            validate_quaternion_xyzw([*numbers[4:7], numbers[3]])
        _csv(
            package / "references/mocap0/data.csv",
            ["sample_id", "segment_id", "timestamp", "px", "py", "pz", "qx", "qy", "qz", "qw"],
            ([str(i), "segment0", *row[:4], *row[5:8], row[4]] for i, row in enumerate(rows)),
        )
        stream = _stream(
            "mocap0", "reference_pose", rows,
            "Publisher poses already map the calibrated IMU frame into the "
            "reference world and use corrected time/outlier processing. Only "
            "quaternion component order changes wxyz to xyzw; no normalization.",
        )
        stream["reference"] = {
            "parent_frame_id": "world", "position_unit": "m",
            "orientation": "Hamilton_xyzw_frame_to_parent",
            "use": "independent_evaluation_only", "coverage": "unknown",
            "position_uncertainty_m": None, "orientation_uncertainty_rad": None,
        }
        streams.append(stream)
    return streams, "included" if len(streams) == 4 else (
        "excluded_by_request" if not include_reference else "absent"
    )


def adapt_tumvi(source: Path, output: Path, *, acquisition_id: str,
                sequence: str, include_reference: bool = True,
                limits=DEFAULT_LIMITS) -> dict:
    """Create a new acquisition package from a declared calibrated 512_16 export.

    Only local uncompressed tar archives and regular directory trees are
    supported. Original archive bytes or the complete original directory tree
    remain outside ``sensors/``. Publication occurs only after package validation;
    failures remove staging data and never replace an existing output. Readers
    must use only the completed result after this function returns; local
    publication is not atomic. Concurrent mutation of source or output files is
    unsupported.
    """
    if type(sequence) is not str or _SEQUENCE.fullmatch(sequence) is None:
        raise ValueError("Unsupported TUM-VI sequence identity.")
    if type(acquisition_id) is not str or _IDENTIFIER.fullmatch(acquisition_id) is None:
        raise ValueError("Invalid acquisition identity.")
    if type(include_reference) is not bool:
        raise ValueError("include_reference must be a boolean.")
    source, output = Path(source), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if source.is_symlink():
        raise ValueError("TUM-VI source may not be a symlink.")
    if not source.exists():
        raise FileNotFoundError(source)
    source_resolved, output_resolved = source.resolve(), output.resolve()
    if source.is_dir() and (
            output_resolved == source_resolved or source_resolved in output_resolved.parents):
        raise ValueError("TUM-VI output cannot be inside its source directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tumvi-import-", dir=output.parent) as temporary:
        scratch = Path(temporary)
        package = scratch / "package"
        package.mkdir()
        aliases, archive_identity = [], None
        if source.is_dir():
            materialized = package / "originals/source"
            inventory = materialize_source(source, materialized, limits=limits)
            source_kind = "directory"
        else:
            archive = package / "originals/source.tar"
            archive_identity = _copy_regular(source, archive, limits=limits)
            allowed = _allowed_aliases(sequence)
            materialized = scratch / "extracted"
            inventory = materialize_source(
                archive, materialized, limits=limits, ignored_aliases=allowed,
            )
            aliases = _archive_aliases(archive, allowed)
            source_kind = "uncompressed_tar"
        root = _source_root(materialized, inventory, sequence)
        streams, reference_status = _convert(
            root, package, include_reference=include_reference, limits=limits,
        )
        _write_json(package / "evidence/tumvi-source.json", {
            "schema_version": 1,
            "adapter": "meridian.sensor_tumvi",
            "adapter_version": "1",
            "declared_profile": "tumvi-calibrated-euroc-512-16",
            "sequence": sequence,
            "source_kind": source_kind,
            "archive_identity": archive_identity,
            "source_files": inventory,
            "ignored_archive_aliases": aliases,
            "reference_policy": reference_status,
            "publisher_source": _SOURCE,
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_authenticity": "not_established_by_adapter_selection",
            "transformations": [
                "Add stream-local sample IDs and segment0 without sorting or gap filling.",
                "Preserve timestamp integers and stripped numeric decimal lexemes.",
                "Copy PNG bytes without conversion or calibration.",
                "Reorder included reference quaternion wxyz to xyzw without normalization.",
            ],
        })
        files = []
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            name = path.relative_to(package).as_posix()
            role = ("original" if name.startswith("originals/") else
                    "reference" if name.startswith("references/") else
                    "evidence" if name.startswith("evidence/") else "sensor")
            files.append({"path": name, **file_identity(path, limits=limits), "role": role})
        acquisition = {
            "format": FORMAT, "schema_version": SCHEMA_VERSION,
            "kind": "acquisition", "id": acquisition_id,
            "purpose": "calibration" if sequence.startswith("calib-") else "replay",
            "rig": {"id": "tumvi-stereo-bmi160", "revision": "publisher-calibrated-512-16"},
            "provenance": {
                "source": _SOURCE + "; declared calibrated 512_16 profile; see evidence/tumvi-source.json",
                "license": _LICENSE, "attribution": _ATTRIBUTION,
                "session_id": "tumvi-" + sequence, "synthetic": False,
            },
            "frames": [
                {"id": "cam0", "axes": ["right", "down", "forward"]},
                {"id": "cam1", "axes": ["right", "down", "forward"]},
                {"id": "imu", "axes": None},
                {"id": "world", "axes": None},
            ],
            "clocks": [{"id": "tumvi-clock", "unit": "ns", "epoch": "unknown"}],
            "files": files, "streams": streams,
            "stereo_pairing": {
                "camera_ids": ["cam0", "cam1"], "policy": "exact_timestamp_pairs",
            },
        }
        validate_contract(acquisition)
        _write_json(package / "acquisition.json", acquisition)
        validate_package(package, limits=limits)
        # Reserve the destination exclusively: rename of a directory can replace
        # an empty directory created by another caller between a check and move.
        output.mkdir(mode=0o700)
        try:
            for path in sorted(package.iterdir()):
                path.rename(output / path.name)
        except BaseException:
            shutil.rmtree(output)
            raise
        return acquisition
