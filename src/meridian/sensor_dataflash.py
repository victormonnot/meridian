"""Import supported DataFlash IMU snapshots into a local acquisition package.

The existing decoder checks recorded units. This adapter preserves its scalars,
record order and instance identifiers; it does not calibrate, rotate, resample,
synchronize or replay them. Physical axes and upstream processing are explicit
caller declarations. A bounded input does not make the eager decoder suitable
for an untrusted hosted workload.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat

from .dataflash import read_dataflash
from .sensor_contract import FORMAT, SCHEMA_VERSION, MAX_JSON_BYTES, validate_contract
from .sensor_conventions import timestamp_to_ns


MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_RECORDS = 2_000_000
_INSTANCE_FIELDS = {
    "sensor_instance", "frame_id", "axes", "calibration", "filtering",
    "clock_correction", "description",
}
_CSV_HEADER = ("sample_id", "segment_id", "timestamp", "wx", "wy", "wz", "fx", "fy", "fz")


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _source_identity(path, *, destination=None):
    """Read one bounded regular file, optionally retaining its exact bytes."""
    fd = None
    try:
        absolute = Path(os.path.abspath(path))
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for index, component in enumerate(absolute.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(absolute.parts) - 2:
                flags |= os.O_DIRECTORY
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ValueError("DataFlash source must be an accessible regular file without a symlink.") from error
    writer = None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 1 <= before.st_size <= MAX_SOURCE_BYTES):
            raise ValueError("DataFlash source is empty, nonregular or exceeds the byte limit.")
        if destination is not None:
            writer = destination.open("xb")
            os.chmod(destination, 0o600)
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ValueError("DataFlash source exceeds the byte limit.")
            digest.update(chunk)
            if writer is not None:
                writer.write(chunk)
        if _stamp(before) != _stamp(os.fstat(fd)) or size != before.st_size:
            raise ValueError("DataFlash source changed while being read.")
        return {"sha256": digest.hexdigest(), "size_bytes": size}
    finally:
        if writer is not None:
            writer.close()
        os.close(fd)


def _json_file(path, value):
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("DataFlash descriptor or evidence exceeds the JSON byte limit.")
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _checked_records(data):
    if type(data.records) is not list or not 1 <= len(data.records) <= MAX_RECORDS:
        raise ValueError("DataFlash IMU record count is empty or exceeds the record limit.")
    if type(data.metadata) is not dict:
        raise ValueError("DataFlash decoder metadata is invalid.")
    for key in ("parser_diagnostic_count", "unparsed_tail_bytes"):
        if type(data.metadata.get(key)) is not int or data.metadata[key] != 0:
            raise ValueError("DataFlash import requires zero parser diagnostics and zero unparsed tail bytes.")
    grouped = {}
    for index, record in enumerate(data.records):
        if type(record.instance) is not int or not 0 <= record.instance <= 2**63 - 1:
            raise ValueError("DataFlash instance must be a nonnegative integer.")
        if type(record.time_us) is not int or record.time_us < 0:
            raise ValueError("DataFlash timestamp must be a nonnegative integer.")
        timestamp_to_ns(str(record.time_us), "us")
        for vector in (record.gyro_rad_s, record.accel_m_s2):
            if type(vector) not in (tuple, list) or len(vector) != 3:
                raise ValueError("DataFlash IMU vector must contain three finite scalars.")
            for value in vector:
                if type(value) not in (int, float):
                    raise ValueError("DataFlash IMU vector must contain three finite scalars.")
                try:
                    finite = math.isfinite(value)
                except OverflowError:
                    finite = False
                if not finite:
                    raise ValueError("DataFlash IMU vector must contain three finite scalars.")
        grouped.setdefault(record.instance, []).append((index, record))
    if len(grouped) > 128:
        raise ValueError("DataFlash import exceeds the instance limit.")
    return grouped


def _write_stream(root, instance, rows, declaration, gap_threshold_us):
    stream_id = f"imu{instance}"
    relative = f"sensors/{stream_id}/data.csv"
    index_path = root / relative
    index_path.parent.mkdir(parents=True, mode=0o700)
    segments, boundaries = [], []
    previous_time, previous_index = None, None
    with index_path.open("x", newline="", encoding="utf-8") as output:
        os.chmod(index_path, 0o600)
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(_CSV_HEADER)
        for index, record in rows:
            difference = None if previous_time is None else record.time_us - previous_time
            reason = (
                "first_record" if difference is None else
                "duplicate_timestamp" if difference == 0 else
                "backward_timestamp" if difference < 0 else
                "gap" if difference > gap_threshold_us else None
            )
            if reason is not None:
                if len(segments) >= 10_000:
                    raise ValueError("DataFlash stream exceeds the segment limit.")
                segment_id = f"segment{len(segments)}"
                segments.append({
                    "id": segment_id, "first_tick": str(record.time_us),
                    "last_tick": str(record.time_us), "sample_count": 0,
                })
                boundaries.append({
                    "segment_id": segment_id, "reason": reason, "record_index": index,
                    "timestamp_us": str(record.time_us),
                    "previous_record_index": previous_index,
                    "previous_timestamp_us": None if previous_time is None else str(previous_time),
                })
            segment = segments[-1]
            segment["last_tick"] = str(record.time_us)
            segment["sample_count"] += 1
            writer.writerow((
                str(index), segment["id"], str(record.time_us),
                *(repr(value) for value in record.gyro_rad_s),
                *(repr(value) for value in record.accel_m_s2),
            ))
            previous_time, previous_index = record.time_us, index
    stream = {
        "id": stream_id, "kind": "imu", "sensor_instance": declaration["sensor_instance"],
        "frame_id": declaration["frame_id"], "clock_id": "dataflash-logger",
        "timestamp_semantics": "message_write", "measurement_semantics": "filtered_snapshot",
        "index_path": relative, "segments": segments,
        "processing": {key: declaration[key] for key in (
            "calibration", "filtering", "clock_correction", "description",
        )},
        "imu": {"angular_velocity_unit": "rad/s", "specific_force_unit": "m/s^2",
                "local_gravity_m_s2": None},
    }
    evidence = {
        "stream_id": stream_id, "source_instance": instance, "sample_count": len(rows),
        "first_record_index": rows[0][0], "last_record_index": rows[-1][0],
        "segment_starts": boundaries,
    }
    return stream, evidence


def build_dataflash_package(
    source: Path,
    output: Path,
    *,
    acquisition_id: str,
    rig: dict,
    provenance: dict,
    instances: dict[int, dict],
    gap_threshold_us: int = 200_000,
) -> dict:
    """Retain one binary log and all decoded IMU instances in a new directory.

    ``instances`` must declare every decoded numeric instance exactly once, with
    sensor_instance, frame_id, axes, calibration, filtering, clock_correction and
    description fields. Axes may be null; upstream processing may be unknown.
    No physical frame, calibration status or clock epoch is inferred from a
    firmware banner. The caller is responsible for those declarations.

    A duplicate/regressing timestamp or an interval above ``gap_threshold_us``
    starts a new per-instance segment without deleting a snapshot. The default
    gap threshold is an inspection policy, not an estimator eligibility gate.
    Eager decoding is limited to 128 MiB before decoding and two million records
    afterwards; it can still consume substantially more memory than its input.
    Existing outputs are refused and a failed new output is removed.
    """
    from .sensor_package import file_identity, validate_package

    source, output = Path(source), Path(output)
    if type(gap_threshold_us) is not int or not 1 <= gap_threshold_us <= 2**63 - 1:
        raise ValueError("DataFlash gap threshold must be a positive integer in microseconds.")
    if (type(instances) is not dict or not 1 <= len(instances) <= 128
            or any(type(key) is not int or not 0 <= key <= 2**63 - 1 for key in instances)):
        raise ValueError("DataFlash instance declarations require nonnegative integer keys.")
    for declaration in instances.values():
        if type(declaration) is not dict or set(declaration) != _INSTANCE_FIELDS:
            raise ValueError("DataFlash instance declaration has missing or unsupported fields.")
    # Check before creating an output, then copy through another checked read.
    original_identity = _source_identity(source)
    try:
        output.mkdir(mode=0o700)
    except OSError as error:
        raise ValueError("DataFlash output must be a new directory with an existing parent.") from error
    try:
        retained_path = output / "originals/source.bin"
        retained_path.parent.mkdir(mode=0o700)
        if _source_identity(source, destination=retained_path) != original_identity:
            raise ValueError("DataFlash source changed before decoding.")
        data = read_dataflash(retained_path)
        if (_source_identity(source) != original_identity
                or _source_identity(retained_path) != original_identity):
            raise ValueError("DataFlash source or retained snapshot changed during decoding.")
        grouped = _checked_records(data)
        if set(instances) != set(grouped):
            raise ValueError("DataFlash metadata must declare every decoded instance exactly once.")
        frames, streams, stream_evidence = {}, [], []
        for instance in sorted(grouped):
            declaration = instances[instance]
            frame_id = declaration["frame_id"]
            if type(frame_id) is not str:
                raise ValueError("DataFlash frame identifier must be a string.")
            frame = {"id": frame_id, "axes": declaration["axes"]}
            if frame_id in frames and frames[frame_id] != frame:
                raise ValueError("DataFlash instances declare conflicting axes for one frame.")
            frames[frame_id] = frame
            stream, evidence = _write_stream(output, instance, grouped[instance],
                                             declaration, gap_threshold_us)
            streams.append(stream)
            stream_evidence.append(evidence)
        report = {
            "schema_version": 1,
            "adapter": {"name": "meridian-dataflash-snapshots", "version": "1"},
            "original": {"path": "originals/source.bin", **original_identity},
            "record_count": len(data.records),
            "record_identity": "sample_id is the zero-based global decoded IMU record index",
            "segment_policy": {
                "gap_threshold_us": gap_threshold_us,
                "new_segment_on": ["duplicate_timestamp", "backward_timestamp", "gap"],
                "gap_rule": "positive adjacent per-instance interval strictly above the threshold",
            },
            "streams": stream_evidence, "decoder_metadata": data.metadata,
            "replay_convention": {
                "gyro_interval_rule": "previous snapshot held over the next logged interval",
                "cross_segment_propagation": False,
                "last_snapshot_has_propagation_interval": False,
                "estimator_executed": False,
            },
            "limitations": [
                "Import checks decoded snapshots, not whole-recording completeness or authenticity.",
                "The logger clock epoch is unknown; message write time is not simultaneous sensor sampling.",
                "Axes and upstream calibration/filtering/clock correction are caller declarations.",
                "The original binary is retained in full and may contain sensitive metadata.",
                "Only IMU snapshots are decoded; high-rate batches and other messages remain in the original.",
                "No calibration, clock correction, resampling, estimator or physical validation is performed.",
                "The eager decoder has a source byte limit and a postdecode record limit, not a memory quota.",
            ],
        }
        (output / "evidence").mkdir(mode=0o700)
        _json_file(output / "evidence/dataflash-import.json", report)
        files = []
        paths = [("originals/source.bin", "original"), ("evidence/dataflash-import.json", "evidence")]
        paths.extend((stream["index_path"], "sensor") for stream in streams)
        for relative, role in sorted(paths):
            identity = file_identity(output / relative)
            files.append({"path": relative, "role": role, **identity})
        acquisition = {
            "format": FORMAT, "schema_version": SCHEMA_VERSION, "kind": "acquisition",
            "id": acquisition_id, "purpose": "replay", "rig": rig, "provenance": provenance,
            "frames": list(frames.values()),
            "clocks": [{"id": "dataflash-logger", "unit": "us", "epoch": "unknown"}],
            "files": files, "streams": streams, "stereo_pairing": None,
        }
        validate_contract(acquisition)
        _json_file(output / "acquisition.json", acquisition)
        validate_package(output)
        return acquisition
    except BaseException:
        shutil.rmtree(output)
        raise
