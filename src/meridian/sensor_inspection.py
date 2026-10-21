"""Sensor evidence before estimation, with exact clocks and bounded query artifacts.

Reading/querying uses only the standard library. Package validation and Pillow
are imported lazily by the isolated writer. No calibration, clock fitting,
resampling, estimator execution or universal quality classification occurs.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil

from .sensor_contract import contract_sha256, validate_contract
from .sensor_conventions import accel_to_m_s2, gyro_to_rad_s, timestamp_ns, timestamp_to_ns


METHOD_VERSION = "sensor-inspection-v1"
MAX_SUMMARY_BYTES = 1024**2
MAX_CHUNK_BYTES = 1024**2
MAX_CHUNK_ROWS = 256
MAX_INSPECTION_BYTES = 512 * 1024**2
MAX_PREVIEW_BYTES = 128 * 1024
MAX_WINDOW_BYTES = 2 * 1024**2 - 4096  # Leave room for the authenticated HTTP envelope.
MAX_DISPLAY_POINTS = 1200
MAX_LISTED_GAPS = 1200
MAX_ROWS = 100_000
POLICY = {"gap_interval_multiplier": 3, "reference_max_bracket_ns": "20000000",
          "stereo_nearest_tolerance_ns": "5000000", "display_point_limit": MAX_DISPLAY_POINTS}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
_SAMPLE = re.compile(r"0|[1-9][0-9]{0,18}\Z")
_CHUNK_PATH = re.compile(r"inspection/streams/[0-9]{3}/[0-9]{5}/[0-9]{6}\.json\Z")
_PREVIEW_PATH = re.compile(r"inspection/previews/[0-9]{6}\.png\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_AXES = ("wx", "wy", "wz", "fx", "fy", "fz")


class InspectionLimitError(ValueError):
    """Valid inputs exceed a separately bounded inspection artifact budget."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _integer(value, minimum=0, maximum=MAX_ROWS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid bounded inspection count.")
    return value


def _sample(value):
    if type(value) is not str or _SAMPLE.fullmatch(value) is None:
        raise ValueError("Sample identifiers must remain canonical decimal strings.")
    return int(value)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _quantile(ordered, numerator, denominator=100):
    return ordered[max(0, (len(ordered) * numerator + denominator - 1) // denominator - 1)]


def _timing(stamps, *, gap_threshold=None, gap_count=None, gap_policy="cadence_median_multiplier"):
    """Positive within-segment intervals; absolute timestamps never become floats."""
    intervals = [b - a for a, b in zip(stamps, stamps[1:])]
    if any(value <= 0 for value in intervals):
        raise ValueError("Inspection cannot repair duplicate or backward timestamps.")
    ordered = sorted(intervals)
    quantiles = {key: str(_quantile(ordered, percentile)) if ordered else None
                 for key, percentile in (("minimum", 0), ("p05", 5), ("p50", 50),
                                         ("p95", 95), ("maximum", 100))}
    threshold = gap_threshold if gap_threshold is not None else (3 * _quantile(ordered, 50) if ordered else None)
    histogram = []
    if ordered:
        width = max(1, (ordered[-1] - ordered[0] + 16) // 16)
        buckets = Counter((value - ordered[0]) // width for value in ordered)
        histogram = [{"lower_ns": str(ordered[0] + index * width),
                      "upper_ns": str(min(ordered[-1], ordered[0] + (index + 1) * width - 1)),
                      "count": buckets[index]}
                     for index in range((ordered[-1] - ordered[0]) // width + 1)]
    span = stamps[-1] - stamps[0] if stamps else 0
    return {"interval_count": len(intervals), "elapsed_ns": str(span),
            "rate_hz": len(intervals) * 1e9 / span if span else None,
            "interval_ns": quantiles, "quantile_method": "nearest_rank",
            "histogram": histogram, "gap_threshold_ns": str(threshold) if threshold is not None else None,
            "gap_policy": gap_policy,
            "gap_count": (sum(value > threshold for value in intervals) if threshold is not None else 0)
                         if gap_count is None else gap_count,
            "duplicate_count": 0, "backward_count": 0, "ordering_evidence": "enforced_by_import"}


class _Moments:
    def __init__(self):
        self.n, self.mean, self.m2 = 0, 0.0, 0.0
        self.minimum, self.maximum, self.bad = None, None, False
        self.scale, self.squares = 0.0, 0.0

    def add(self, value):
        self.n += 1
        if not _finite(value):
            self.bad = True
            return
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        absolute = abs(value)
        if absolute > self.scale:
            self.squares = self.squares * (self.scale / absolute) ** 2 + 1
            self.scale = absolute
        elif self.scale:
            self.squares += (absolute / self.scale) ** 2
        delta = value - self.mean
        mean = self.mean * ((self.n - 1) / self.n) + value / self.n
        m2 = self.m2 + delta * (value - mean)
        if not math.isfinite(mean) or not math.isfinite(m2):
            self.bad = True
        else:
            self.mean, self.m2 = mean, m2

    def result(self):
        return {"minimum": self.minimum, "maximum": self.maximum,
                "mean": self.mean if self.n and not self.bad else None,
                "stddev": math.sqrt(max(0.0, self.m2) / self.n) if self.n and not self.bad else None,
                "status": "numerical_range_exceeded" if self.bad else "available" if self.n else "no_samples"}

    def rms(self):
        result = self.scale * math.sqrt(self.squares / self.n) if self.n and not self.bad else None
        return result if result is None or math.isfinite(result) else None


def _imu_result(axes, gyro_norm, force_norm):
    force = force_norm.result()
    return {"axes": {key: value.result() for key, value in zip(_AXES, axes)},
            "activity_evidence": {
                "status": "numerical_range_exceeded" if gyro_norm.bad or force_norm.bad else
                          "available" if gyro_norm.n else "no_samples",
                "gyro_norm_rms_rad_s": gyro_norm.rms(),
                "specific_force_norm_mean_m_s2": force["mean"],
                "specific_force_norm_stddev_m_s2": force["stddev"],
                "interpretation": "Apparent motion evidence only; low variation does not prove physical stationarity."},
            "saturation": {"status": "unknown", "reason": "sensor_limits_not_declared"}}


def _monotonic_segments(stream):
    return all(timestamp_ns(b["first_ns"]) > timestamp_ns(a["last_ns"])
               for a, b in zip(stream["segments"], stream["segments"][1:]))


def _comparable(left, right):
    if left["clock_id"] != right["clock_id"]:
        return "different_clock_domains"
    if not _monotonic_segments(left) or not _monotonic_segments(right):
        return "ambiguous_reset_eras"
    return None


def _nearest_unique(stamps, stamp, tolerance):
    index = bisect_left(stamps, stamp)
    candidates = [i for i in (index - 1, index) if 0 <= i < len(stamps)]
    if not candidates:
        return None
    distances = [(abs(stamps[i] - stamp), i) for i in candidates]
    distances.sort()
    if distances[0][0] > tolerance or (len(distances) == 2 and distances[0][0] == distances[1][0]):
        return None
    return distances[0][1]


def _support(stamp, stream, timestamp_index):
    for segment in stream["segments"]:
        if not timestamp_ns(segment["first_ns"]) <= stamp <= timestamp_ns(segment["last_ns"]):
            continue
        stamps = timestamp_index[(stream["id"], segment["id"])]
        index = bisect_left(stamps, stamp)
        if index < len(stamps) and stamps[index] == stamp:
            return "supported", segment["id"]
        if index == 0 or index == len(stamps):
            return "outside", segment["id"]
        maximum = (int(POLICY["reference_max_bracket_ns"]) if stream["kind"] == "reference_pose"
                   else int(segment["timing"]["gap_threshold_ns"]))
        return ("supported" if stamps[index] - stamps[index - 1] <= maximum else "gap"), segment["id"]
    return "outside", None


def _comparisons(acquisition, streams, rows):
    comparisons = []
    timestamp_index = {key: [timestamp_ns(row["timestamp_ns"]) for row in samples]
                       for key, samples in rows.items()}
    cameras = [stream for stream in streams if stream["kind"] == "camera"]
    for camera in cameras:
        for target in (stream for stream in streams if stream["kind"] in {"imu", "reference_pose"}):
            reason, counts = _comparable(camera, target), Counter()
            for segment in camera["segments"]:
                for row in rows[(camera["id"], segment["id"])]:
                    status, target_segment = ("not_comparable", None) if reason else _support(
                        timestamp_ns(row["timestamp_ns"]), target, timestamp_index)
                    row["support"].append({"stream_id": target["id"], "kind": target["kind"],
                                           "status": status, "segment_id": target_segment})
                    counts[status] += 1
            comparisons.append({"kind": "reference_support" if target["kind"] == "reference_pose" else "imu_support",
                "stream_ids": [camera["id"], target["id"]], "status": "not_comparable" if reason else "available",
                "reason": reason, "supported_samples": counts["supported"],
                "total_samples": camera["sample_count"], "outside_samples": counts["outside"],
                "gap_samples": counts["gap"], "not_comparable_samples": counts["not_comparable"],
                "fraction": counts["supported"] / camera["sample_count"] if not reason else None,
                "maximum_bracket_ns": POLICY["reference_max_bracket_ns"] if target["kind"] == "reference_pose" else None})
    pairing = acquisition["stereo_pairing"]
    if pairing:
        left, right = [next(stream for stream in streams if stream["id"] == name) for name in pairing["camera_ids"]]
        exact = pairing["policy"] == "exact_timestamp_pairs"
        reason = None if exact else _comparable(left, right)
        matched = 0
        if not reason:
            if exact:
                groups = [(rows[(left["id"], a["id"])], a["id"], rows[(right["id"], b["id"])], b["id"])
                          for a, b in zip(left["segments"], right["segments"], strict=True)]
            else:
                groups = [([(a, s["id"]) for s in left["segments"] for a in rows[(left["id"], s["id"])]], None,
                           [(b, s["id"]) for s in right["segments"] for b in rows[(right["id"], s["id"])]], None)]
            for aa, aseg, bb, bseg in groups:
                arows = [(x, aseg) for x in aa] if exact else aa
                brows = [(x, bseg) for x in bb] if exact else bb
                astamps = [timestamp_ns(x[0]["timestamp_ns"]) for x in arows]
                bstamps = [timestamp_ns(x[0]["timestamp_ns"]) for x in brows]
                if exact and astamps != bstamps:
                    raise ValueError("Exact stereo declaration differs from inspected timestamps.")
                tolerance = 0 if exact else int(POLICY["stereo_nearest_tolerance_ns"])
                for index, (a, a_segment) in enumerate(arows):
                    other = _nearest_unique(bstamps, astamps[index], tolerance)
                    if other is None or _nearest_unique(astamps, bstamps[other], tolerance) != index:
                        continue
                    b, b_segment = brows[other]
                    for source, target, target_stream, target_segment in (
                            (a, b, right["id"], b_segment), (b, a, left["id"], a_segment)):
                        source["stereo_match"] = {"stream_id": target_stream, "segment_id": target_segment,
                            "sample_id": target["sample_id"], "timestamp_ns": target["timestamp_ns"],
                            "delta_ns": str(timestamp_ns(target["timestamp_ns"]) - timestamp_ns(source["timestamp_ns"]))}
                    matched += 1
        comparisons.append({"kind": "stereo_pairing", "stream_ids": [left["id"], right["id"]],
            "status": "not_comparable" if reason else "available", "reason": reason,
            "policy": "declared_exact" if exact else "mutual_unique_nearest",
            "tolerance_ns": "0" if exact else POLICY["stereo_nearest_tolerance_ns"],
            "matched_samples": matched, "unmatched_samples": {
                left["id"]: left["sample_count"] - matched, right["id"]: right["sample_count"] - matched}})
    return comparisons


@dataclass
class _Writer:
    root: Path
    maximum: int
    total: int = 0
    files: int = 0

    def write(self, relative, content, maximum):
        if len(content) > maximum or self.total + len(content) > self.maximum or self.files >= 59_998:
            raise InspectionLimitError("Inspection artifact exceeds its byte or file budget.")
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as handle:
            handle.write(content)
        self.total += len(content)
        self.files += 1


def _preview(content, camera):
    from PIL import Image, ImageFilter, ImageStat
    with Image.open(io.BytesIO(content)) as source:
        source.load()
        original = source.convert("I") if camera["encoding"] == "mono16" else source.convert("L")
        width, height = original.size
        factor = min(1.0, 320 / max(width, height))
        size = (max(1, round(width * factor)), max(1, round(height * factor)))
        scaled = original.resize(size, Image.Resampling.BOX)
        preview = scaled.point(lambda pixel: pixel / 257).convert("L") if camera["encoding"] == "mono16" else scaled
        histogram = preview.histogram()
        pixels = size[0] * size[1]
        edges = preview.filter(ImageFilter.FIND_EDGES)
        if size[0] > 2 and size[1] > 2:
            edges = edges.crop((1, 1, size[0] - 1, size[1] - 1))
        indicators = {"method": "resized_mono8_evidence_v1", "status": "available",
            "width": size[0], "height": size[1],
            "brightness_mean_0_1": ImageStat.Stat(preview).mean[0] / 255,
            "dark_endpoint_fraction": histogram[0] / pixels,
            "bright_endpoint_fraction": histogram[255] / pixels,
            "edge_contrast_mean_0_1": ImageStat.Stat(edges).mean[0] / 255}
        output = io.BytesIO()
        preview.save(output, format="PNG", compress_level=6)
        return output.getvalue(), size, indicators


def write_inspection(acquisition_root, output_root, *, validated=None, limits=None,
                     max_bytes=MAX_INSPECTION_BYTES):
    """Write a new, immutable inspection tree from an unchanged validated package."""
    from .sensor_package import DEFAULT_LIMITS, _read_bytes, file_identity, iter_stream_rows, validate_package
    limits = limits or DEFAULT_LIMITS
    root, output = Path(acquisition_root), Path(output_root)
    if (output.exists() or output.is_symlink() or output.resolve().is_relative_to(root.resolve())
            or any(path.is_symlink() for path in output.parents)):
        raise ValueError("Inspection requires a new output outside the source package.")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_INSPECTION_BYTES:
        raise ValueError("Invalid inspection byte budget.")
    validated = validated if validated is not None else validate_package(root, limits=limits)
    acquisition = validated["acquisition"]
    validate_contract(acquisition)
    if (acquisition["kind"] != "acquisition" or validated["report"]["status"] != "validated"
            or validated["report"]["acquisition_sha256"] != contract_sha256(acquisition)):
        raise ValueError("Inspection requires the exact validated acquisition identity.")
    clocks = {clock["id"]: clock for clock in acquisition["clocks"]}
    inventory = {item["path"]: item for item in acquisition["files"]}
    output.mkdir(mode=0o700)
    writer, streams, rows, total_rows, image_number = _Writer(output, max_bytes), [], {}, 0, 0
    try:
        for source in acquisition["streams"]:
            stream = {key: source[key] for key in ("id", "kind", "frame_id", "clock_id", "timestamp_semantics",
                                                   "measurement_semantics", "processing")}
            stream["sample_count"], stream["segments"] = 0, []
            if source["kind"] == "imu":
                stream["source_units"] = {"angular_velocity": source["imu"]["angular_velocity_unit"],
                                          "specific_force": source["imu"]["specific_force_unit"]}
                stream["units"] = {"angular_velocity": "rad/s", "specific_force": "m/s^2"}
                stream["saturation"] = {"status": "unknown", "reason": "sensor_limits_not_declared"}
            else:
                detail = "camera" if source["kind"] == "camera" else "reference"
                stream[detail] = source[detail]
            for segment in source["segments"]:
                rows[(source["id"], segment["id"])] = []
            expected = {key: inventory[source["index_path"]][key] for key in ("sha256", "size_bytes")}
            if file_identity(root / source["index_path"], limits=limits) != expected:
                raise ValueError("Validated source index changed before inspection.")
            for raw in iter_stream_rows(root, source, limits=limits):
                total_rows += 1
                if total_rows > min(MAX_ROWS, limits.max_csv_rows):
                    raise InspectionLimitError("Inspection sample budget exceeded.")
                row = {"sample_id": raw[0], "timestamp_ns": str(timestamp_to_ns(raw[2], clocks[source["clock_id"]]["unit"])),
                       "gap_before": None}
                if source["kind"] == "imu":
                    values = list(map(float, raw[3:]))
                    row["gyro_rad_s"] = [gyro_to_rad_s(x, source["imu"]["angular_velocity_unit"]) for x in values[:3]]
                    row["specific_force_m_s2"] = [accel_to_m_s2(x, source["imu"]["specific_force_unit"],
                        local_gravity_m_s2=source["imu"]["local_gravity_m_s2"]) for x in values[3:]]
                elif source["kind"] == "reference_pose":
                    row["position_m"], row["quaternion_xyzw"] = list(map(float, raw[3:6])), list(map(float, raw[6:]))
                else:
                    name = (PurePosixPath(source["index_path"]).parent / raw[3]).as_posix()
                    content = _read_bytes(root / name, min(limits.max_png_bytes, limits.max_file_bytes))
                    identity = inventory[name]
                    if len(content) != identity["size_bytes"] or hashlib.sha256(content).hexdigest() != identity["sha256"]:
                        raise ValueError("Validated image changed before inspection.")
                    preview, size, indicators = _preview(content, source["camera"])
                    relative = f"previews/{image_number:06d}.png"
                    writer.write(relative, preview, MAX_PREVIEW_BYTES)
                    image_number += 1
                    row["image"] = {"preview_path": "inspection/" + relative, "source_path": name,
                        "source_sha256": identity["sha256"], "source_size_bytes": identity["size_bytes"],
                        "width": source["camera"]["resolution"][0], "height": source["camera"]["resolution"][1],
                        "preview_width": size[0], "preview_height": size[1], "indicators": indicators}
                    row["support"], row["stereo_match"] = [], None
                rows[(source["id"], raw[1])].append(row)
            if file_identity(root / source["index_path"], limits=limits) != expected:
                raise ValueError("Source index changed during inspection.")
            for source_segment in source["segments"]:
                segment_rows = rows[(source["id"], source_segment["id"])]
                stamps = [timestamp_ns(row["timestamp_ns"]) for row in segment_rows]
                reference = source["kind"] == "reference_pose"
                timing = _timing(stamps,
                    gap_threshold=int(POLICY["reference_max_bracket_ns"]) if reference else None,
                    gap_policy="reference_bracket_limit" if reference else "cadence_median_multiplier")
                threshold = int(timing["gap_threshold_ns"]) if timing["gap_threshold_ns"] is not None else None
                for a, b in zip(segment_rows, segment_rows[1:]):
                    interval = timestamp_ns(b["timestamp_ns"]) - timestamp_ns(a["timestamp_ns"])
                    if threshold is not None and interval > threshold:
                        b["gap_before"] = {"previous_sample_id": a["sample_id"],
                                           "previous_timestamp_ns": a["timestamp_ns"], "interval_ns": str(interval)}
                previous = stream["segments"][-1] if stream["segments"] else None
                boundary = None if previous is None else {
                    "kind": "reset_or_overlap" if stamps[0] <= timestamp_ns(previous["last_ns"]) else "forward_discontinuity",
                    "previous_last_ns": previous["last_ns"], "first_ns": str(stamps[0])}
                stream["segments"].append({"id": source_segment["id"], "sample_count": len(stamps),
                    "first_ns": str(stamps[0]), "last_ns": str(stamps[-1]), "gap_count": timing["gap_count"],
                    "timing": timing, "boundary_from_previous": boundary, "chunks": []})
                stream["sample_count"] += len(stamps)
            streams.append(stream)
        comparisons = _comparisons(acquisition, streams, rows)
        for stream_index, stream in enumerate(streams):
            for segment_index, segment in enumerate(stream["segments"]):
                segment_rows = rows[(stream["id"], segment["id"])]
                offset = 0
                while offset < len(segment_rows):
                    selected = segment_rows[offset:offset + MAX_CHUNK_ROWS]
                    while True:
                        chunk = {"schema_version": 1, "method_version": METHOD_VERSION, "stream_id": stream["id"],
                                 "segment_id": segment["id"], "rows": selected}
                        content = _json(chunk) + b"\n"
                        if len(content) <= MAX_CHUNK_BYTES:
                            break
                        if len(selected) == 1:
                            raise InspectionLimitError("One inspection row exceeds the chunk byte budget.")
                        selected = selected[:max(1, len(selected) // 2)]
                    relative = f"streams/{stream_index:03d}/{segment_index:05d}/{len(segment['chunks']):06d}.json"
                    writer.write(relative, content, MAX_CHUNK_BYTES)
                    segment["chunks"].append({"path": "inspection/" + relative, "rows": len(selected),
                        "first_ns": selected[0]["timestamp_ns"], "last_ns": selected[-1]["timestamp_ns"],
                        "first_sample_id": selected[0]["sample_id"], "last_sample_id": selected[-1]["sample_id"]})
                    offset += len(selected)
        summary = {"schema_version": 1, "kind": "sensor_inspection", "method_version": METHOD_VERSION,
            "status": "available", "acquisition": {"id": acquisition["id"], "sha256": contract_sha256(acquisition)},
            "policy": dict(POLICY), "evidence": {"import_validity": "validated", "inspection_availability": "available",
                "calibration_suitability": "not_assessed", "estimator_compatibility": "not_assessed",
                "estimate_support": "not_generated", "evaluation_availability": "not_assessed"},
            "clocks": acquisition["clocks"], "frames": acquisition["frames"], "streams": streams,
            "comparisons": comparisons, "reference_availability": validated["report"]["reference_availability"],
            "limitations": ["Clock labels and timestamp meaning are declarations, not measured synchronization.",
                "Duplicate/backward timestamps within a segment are rejected by import, not repaired by inspection.",
                "Gap thresholds and image/IMU indicators describe observations, not universal quality requirements.",
                "Reference support is separate from sensor support; no estimate or evaluation is generated."]}
        validate_summary(summary)
        writer.write("summary.json", _json(summary) + b"\n", MAX_SUMMARY_BYTES)
        return summary
    except Exception:
        shutil.rmtree(output)
        raise


def _validate_summary(summary):
    """Validate the bounded index before a callback may read its chunk paths."""
    if (type(summary) is not dict or type(summary.get("schema_version")) is not int or summary["schema_version"] != 1
            or summary.get("kind") != "sensor_inspection" or summary.get("method_version") != METHOD_VERSION
            or summary.get("status") != "available" or summary.get("policy") != POLICY):
        raise ValueError("Unsupported inspection summary or method.")
    if len(_json(summary)) > MAX_SUMMARY_BYTES:
        raise InspectionLimitError("Inspection summary exceeds its byte budget.")
    identity = summary.get("acquisition", {})
    if (type(identity) is not dict or type(identity.get("id")) is not str
            or not _ID.fullmatch(identity["id"]) or type(identity.get("sha256")) is not str
            or not _SHA.fullmatch(identity["sha256"])):
        raise ValueError("Inspection identity is invalid.")
    streams = summary.get("streams")
    if type(streams) is not list or not 1 <= len(streams) <= 128:
        raise ValueError("Invalid inspection stream list.")
    names, paths, count = set(), set(), 0
    for stream in streams:
        name = stream.get("id")
        if type(name) is not str or not _ID.fullmatch(name) or name in names or stream.get("kind") not in {"camera", "imu", "reference_pose"}:
            raise ValueError("Invalid or repeated inspection stream.")
        names.add(name)
        segments = stream.get("segments")
        if type(segments) is not list or not 1 <= len(segments) <= 10_000:
            raise ValueError("Invalid segment list.")
        segment_names, stream_count, previous_sample = set(), 0, -1
        for segment in segments:
            identifier = segment.get("id")
            if type(identifier) is not str or not _ID.fullmatch(identifier) or identifier in segment_names:
                raise ValueError("Invalid or repeated inspection segment.")
            segment_names.add(identifier)
            first, last = timestamp_ns(segment["first_ns"]), timestamp_ns(segment["last_ns"])
            if first > last:
                raise ValueError("Segment bounds must be ordered.")
            chunks, sample_count = segment["chunks"], 0
            if type(chunks) is not list or not chunks:
                raise ValueError("Each segment requires indexed chunks.")
            previous_time = None
            for chunk in chunks:
                path = chunk["path"]
                if type(path) is not str or not _CHUNK_PATH.fullmatch(path) or path in paths:
                    raise ValueError("Invalid or repeated inspection chunk path.")
                paths.add(path)
                a, b = timestamp_ns(chunk["first_ns"]), timestamp_ns(chunk["last_ns"])
                i, j = _sample(chunk["first_sample_id"]), _sample(chunk["last_sample_id"])
                if (not first <= a <= b <= last or (previous_time is not None and a <= previous_time)
                        or i > j or i <= previous_sample):
                    raise ValueError("Chunk bounds do not preserve source order.")
                previous_time, previous_sample = b, j
                sample_count += _integer(chunk["rows"], 1, MAX_CHUNK_ROWS)
            if chunks[0]["first_ns"] != segment["first_ns"] or chunks[-1]["last_ns"] != segment["last_ns"]:
                raise ValueError("Chunk index omits segment endpoints.")
            if sample_count != _integer(segment["sample_count"], 1):
                raise ValueError("Chunk sample totals differ from the segment.")
            stream_count += sample_count
        if stream_count != _integer(stream["sample_count"], 1):
            raise ValueError("Segment sample totals differ from the stream.")
        count += stream_count
    _integer(count, 1)


def validate_summary(summary):
    """Reject malformed nested index structures with a stable ValueError API."""
    try:
        _validate_summary(summary)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError, RecursionError) as error:
        raise ValueError("Malformed inspection summary.") from error


def _selected(summary, stream_id, segment_id=None):
    validate_summary(summary)
    stream = next((item for item in summary["streams"] if item["id"] == stream_id), None)
    if stream is None:
        raise ValueError("Unknown inspection stream.")
    if segment_id is None:
        return stream, None
    segment = next((item for item in stream["segments"] if item["id"] == segment_id), None)
    if segment is None:
        raise ValueError("Unknown inspection segment.")
    return stream, segment


def _read_rows(read_chunk, stream, segment, index):
    chunk = read_chunk(index["path"])
    if (type(chunk) is not dict or type(chunk.get("schema_version")) is not int or chunk["schema_version"] != 1
            or chunk.get("method_version") != METHOD_VERSION
            or chunk.get("stream_id") != stream["id"] or chunk.get("segment_id") != segment["id"]
            or type(chunk.get("rows")) is not list or len(chunk["rows"]) != index["rows"]
            or len(_json(chunk)) > MAX_CHUNK_BYTES):
        raise ValueError("Inspection chunk does not match its index.")
    previous, previous_id = None, -1
    for row in chunk["rows"]:
        stamp, identifier = timestamp_ns(row["timestamp_ns"]), _sample(row["sample_id"])
        if previous is not None and stamp <= previous or identifier <= previous_id:
            raise ValueError("Inspection rows do not preserve source order.")
        previous, previous_id = stamp, identifier
        if stream["kind"] == "imu":
            for key in ("gyro_rad_s", "specific_force_m_s2"):
                if type(row.get(key)) is not list or len(row[key]) != 3 or not all(_finite(x) for x in row[key]):
                    raise ValueError("Inspection IMU vectors require finite SI scalars.")
        gap = row.get("gap_before")
        if gap is not None:
            if (_sample(gap["previous_sample_id"]) >= identifier
                    or timestamp_ns(gap["previous_timestamp_ns"]) >= stamp
                    or int(gap["interval_ns"]) != stamp - timestamp_ns(gap["previous_timestamp_ns"])):
                raise ValueError("Invalid inspection gap endpoints.")
    first, last = chunk["rows"][0], chunk["rows"][-1]
    if any(first[a] != index[b] for a, b in (("timestamp_ns", "first_ns"), ("sample_id", "first_sample_id"))) or any(
            last[a] != index[b] for a, b in (("timestamp_ns", "last_ns"), ("sample_id", "last_sample_id"))):
        raise ValueError("Inspection row endpoints differ from the chunk index.")
    return chunk["rows"]


def _window_rows(read_chunk, stream, segment, first, last):
    following = False
    for index in segment["chunks"]:
        if timestamp_ns(index["last_ns"]) < first:
            continue
        if timestamp_ns(index["first_ns"]) > last:
            if following:
                break
            following = True
        for row in _read_rows(read_chunk, stream, segment, index):
            yield row
        if following:
            break


def _support_signature(row):
    return tuple((item["stream_id"], item["status"], item["segment_id"]) for item in row.get("support", []))


def inspection_window(summary, read_chunk, *, stream_id, segment_id, first_ns, last_ns,
                      max_points=MAX_DISPLAY_POINTS):
    """Full-resolution interval evidence followed by bounded gap-preserving display."""
    stream, segment = _selected(summary, stream_id, segment_id)
    first, last = timestamp_ns(first_ns), timestamp_ns(last_ns)
    if not timestamp_ns(segment["first_ns"]) <= first <= last <= timestamp_ns(segment["last_ns"]):
        raise ValueError("The closed interval must lie within one declared segment.")
    _integer(max_points, 2, MAX_DISPLAY_POINTS)
    stamps, gaps, gap_count, support, mandatory = [], [], 0, {}, {}
    axes, gyro_norm, force_norm = [_Moments() for _ in _AXES], _Moments(), _Moments()
    extrema, previous, last_selected, selected_count, too_many = {}, None, None, 0, False

    def retain(row):
        nonlocal too_many
        if row and first <= timestamp_ns(row["timestamp_ns"]) <= last:
            if len(mandatory) >= max_points and row["sample_id"] not in mandatory:
                too_many = True
            else:
                mandatory[row["sample_id"]] = row

    for row in _window_rows(read_chunk, stream, segment, first, last):
        stamp, gap = timestamp_ns(row["timestamp_ns"]), row.get("gap_before")
        if gap is not None and timestamp_ns(gap["previous_timestamp_ns"]) < last and stamp > first:
            gap_count += 1
            if len(gaps) < MAX_LISTED_GAPS:
                gaps.append({**gap, "sample_id": row["sample_id"], "timestamp_ns": row["timestamp_ns"]})
            retain(previous)
            retain(row)
        if not first <= stamp <= last:
            previous = row
            continue
        selected_count += 1
        last_selected = row
        stamps.append(stamp)
        if selected_count == 1:
            retain(row)
        if previous and _support_signature(previous) != _support_signature(row):
            retain(previous)
            retain(row)
        for item in row.get("support", []):
            value = support.setdefault(item["stream_id"], {"stream_id": item["stream_id"], "kind": item["kind"],
                "supported_samples": 0, "not_comparable_samples": 0, "total_samples": 0})
            value["total_samples"] += 1
            value["supported_samples"] += item["status"] == "supported"
            value["not_comparable_samples"] += item["status"] == "not_comparable"
        if stream["kind"] == "imu":
            values = row["gyro_rad_s"] + row["specific_force_m_s2"]
            for index, value in enumerate(values):
                axes[index].add(value)
                for label, prefer in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
                    key = index, label
                    if key not in extrema or prefer(value, extrema[key][0]):
                        extrema[key] = (value, row)
            gyro_norm.add(math.hypot(*row["gyro_rad_s"]))
            force_norm.add(math.hypot(*row["specific_force_m_s2"]))
        previous = row
    retain(last_selected)
    for _, row in extrema.values():
        retain(row)
    for value in support.values():
        value["fraction"] = (value["supported_samples"] / value["total_samples"]
                             if value["total_samples"] and not value["not_comparable_samples"] else None)
    threshold = segment["timing"]["gap_threshold_ns"]
    result = {"schema_version": 1, "method_version": METHOD_VERSION, "stream_id": stream_id,
        "segment_id": segment_id, "clock_id": stream["clock_id"],
        "interval": {"first_ns": first_ns, "last_ns": last_ns, "boundary": "closed"},
        "sample_count": selected_count,
        "timing": _timing(stamps, gap_threshold=int(threshold) if threshold is not None else None,
            gap_count=gap_count, gap_policy=segment["timing"].get("gap_policy", "cadence_median_multiplier")),
        "gap_count": gap_count, "gaps": gaps, "gaps_truncated": gap_count > len(gaps),
        "support": list(support.values()),
        "imu": _imu_result(axes, gyro_norm, force_norm) if stream["kind"] == "imu" else None}
    capacity = max_points
    while True:
        points = dict(mandatory)
        unavailable = too_many or len(points) > capacity
        if not unavailable:
            remaining = capacity - len(points)
            wanted = (set(range(selected_count)) if selected_count <= capacity else
                      {i * (selected_count - 1) // max(1, remaining - 1) for i in range(remaining)}) if selected_count else set()
            position = 0
            for row in _window_rows(read_chunk, stream, segment, first, last):
                if first <= timestamp_ns(row["timestamp_ns"]) <= last:
                    if position in wanted:
                        points[row["sample_id"]] = row
                    position += 1
            display = sorted(points.values(), key=lambda row: timestamp_ns(row["timestamp_ns"]))
        else:
            display = []
        result["display"] = {"status": "narrower_interval_required" if unavailable else "available",
            "source_samples": selected_count, "point_count": len(display), "point_limit": max_points,
            "reduced": len(display) < selected_count, "points": display}
        if len(_json(result)) <= MAX_WINDOW_BYTES:
            return result
        if unavailable:
            raise InspectionLimitError("Interval metadata exceeds the bounded response budget.")
        if capacity <= len(mandatory):
            too_many = True
            continue
        capacity = max(len(mandatory), capacity // 2)


def inspection_image(summary, read_chunk, *, stream_id, sample_id):
    stream, _ = _selected(summary, stream_id)
    identifier = _sample(sample_id)
    if stream["kind"] != "camera":
        raise ValueError("Only camera streams have inspection previews.")
    for segment in stream["segments"]:
        for index in segment["chunks"]:
            if _sample(index["first_sample_id"]) <= identifier <= _sample(index["last_sample_id"]):
                for row in _read_rows(read_chunk, stream, segment, index):
                    if row["sample_id"] == sample_id:
                        image = row["image"]
                        if not _PREVIEW_PATH.fullmatch(image["preview_path"]):
                            raise ValueError("Invalid generated preview path.")
                        return {**image, "stream_id": stream_id, "segment_id": segment["id"],
                                "sample_id": sample_id, "timestamp_ns": row["timestamp_ns"]}
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("acquisition", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    summary = write_inspection(args.acquisition, args.output)
    print(json.dumps({"schema_version": 1, "status": summary["status"],
                      "acquisition": summary["acquisition"], "streams": len(summary["streams"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
