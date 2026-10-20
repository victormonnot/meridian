"""Closed metadata contracts for sensor acquisition, calibration and timing.

Validation checks declarations and their internal consistency only. It never
opens referenced payloads, verifies their hashes, calibrates sensors or admits
an estimator job. Unknown physical semantics remain explicit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from .sensor_conventions import (
    map_constant_clock, timestamp_ns, timestamp_to_ns, validate_rigid_transform,
)


FORMAT = "meridian-sensor-contract"
SCHEMA_VERSION = 1
MAX_JSON_BYTES = 32 * 1024 * 1024
_COMMON = {"format", "schema_version", "kind", "id"}
_EXTRA = {
    "acquisition": {"purpose", "rig", "provenance", "frames", "clocks", "files", "streams", "stereo_pairing"},
    "calibration": {"acquisition", "state", "method", "cameras", "imu", "transforms", "applicability", "validation"},
    "clock_mapping": {"acquisition", "source_stream_id", "source_segment_id", "target_clock_id",
                      "purpose", "model", "source_origin_ns", "target_origin_ns", "offset_ns",
                      "first_source_ns", "last_source_ns", "evidence"},
    "prepared_input": {"acquisition", "method", "calibration", "clock_mappings", "selections",
                       "operations", "files", "outputs", "target_profile"},
}
_FILE = {"path", "sha256", "size_bytes"}
_STREAM = {"id", "kind", "sensor_instance", "frame_id", "clock_id", "timestamp_semantics",
           "measurement_semantics", "index_path", "segments", "processing"}
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,79}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _fail(message):
    raise ValueError(message)


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail("Object has missing or unsupported fields.")
    return value


def _text(value, maximum=4096):
    if (type(value) is not str or not 1 <= len(value) <= maximum or not value.strip()
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        _fail("Expected bounded nonempty text without control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Text must be valid Unicode.") from error
    return value


def _id(value):
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail("Identifier must be an ASCII slug of at most 80 characters.")
    return value


def _choice(value, choices):
    if type(value) is not str or value not in choices:
        _fail("Unsupported declared value.")
    return value


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("Expected an integer within the declared bounds.")
    return value


def _number(value, minimum=None, positive=False):
    if type(value) not in (int, float):
        _fail("Expected a finite real number.")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError("Number exceeds the finite range.") from error
    if not finite or (minimum is not None and value < minimum) or (positive and value <= 0):
        _fail("Number is nonfinite or outside the declared bounds.")
    return value


def _array(value, minimum=0, maximum=100_000):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        _fail("Expected an array within the declared bounds.")
    return value


def _vector(value, length, *, positive=False):
    for number in _array(value, length, length):
        _number(number, positive=positive)


def _resolution(value):
    for number in _array(value, 2, 2):
        _integer(number, 1, 65536)


def _hash(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _fail("SHA-256 must be 64 lowercase hexadecimal characters.")
    return value


def _path(value):
    _text(value, 512)
    if ("\\" in value or ":" in value or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        _fail("Path must be a normalized relative POSIX path.")
    return value


def _reference(value):
    _object(value, {"id", "sha256"})
    _id(value["id"])
    _hash(value["sha256"])


def _references(value):
    result = {}
    for item in _array(value, maximum=10_000):
        _reference(item)
        if item["id"] in result:
            _fail("Repeated identity reference.")
        result[item["id"]] = item
    return result


def _file(value, role=False):
    _object(value, _FILE | ({"role"} if role else set()))
    _path(value["path"])
    _hash(value["sha256"])
    _integer(value["size_bytes"])
    if role:
        _choice(value["role"], {"original", "sensor", "reference", "evidence"})


def _files(value):
    result = {}
    for item in _array(value, 1):
        _file(item, role=True)
        if item["path"] in result:
            _fail("Repeated file path.")
        result[item["path"]] = item
    return result


def _indexed(value, fields, maximum):
    result = {}
    for item in _array(value, 1, maximum):
        _object(item, fields)
        key = _id(item["id"])
        if key in result:
            _fail("Repeated identifier.")
        result[key] = item
    return result


def _ordered_ticks(first, last, unit="ns"):
    bounds = timestamp_to_ns(first, unit), timestamp_to_ns(last, unit)
    if bounds[0] > bounds[1]:
        _fail("Timestamp bounds must be ordered.")
    return bounds


def _json_graph(value):
    """Reject non-JSON values, cycles, excessive depth and oversized graphs."""
    stack, nodes, encoded_text_bytes = [(value, 0, frozenset())], 0, 0
    while stack:
        item, depth, ancestors = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 2_000_000:
            _fail("Metadata exceeds its structural limits.")
        if type(item) in (dict, list):
            if id(item) in ancestors or len(item) > 100_000:
                _fail("Metadata contains a cycle or oversized collection.")
            children = item.values() if type(item) is dict else item
            if type(item) is dict and any(type(key) is not str or len(key) > 80 for key in item):
                _fail("JSON object keys must be bounded strings.")
            parent = ancestors | {id(item)}
            stack.extend((child, depth + 1, parent) for child in children)
        elif type(item) is str:
            if len(item) > 4096:
                _fail("JSON text exceeds its limit.")
            try:
                item.encode("utf-8")
            except UnicodeError as error:
                raise ValueError("JSON text must be valid Unicode.") from error
            encoded_text_bytes += len(json.dumps(item, ensure_ascii=True))
            if encoded_text_bytes > MAX_JSON_BYTES:
                _fail("Contract text exceeds its encoded size limit.")
        elif type(item) in (int, float):
            _number(item)
        elif item is not None and type(item) is not bool:
            _fail("Metadata must contain only JSON values.")


def _acquisition(value):
    _choice(value["purpose"], {"replay", "calibration", "evaluation", "fixture"})
    rig = _object(value["rig"], {"id", "revision"})
    _id(rig["id"])
    _text(rig["revision"], 80)
    provenance = _object(value["provenance"], {"source", "license", "attribution", "session_id", "synthetic"})
    for key in ("source", "license", "attribution", "session_id"):
        _text(provenance[key])
    if type(provenance["synthetic"]) is not bool:
        _fail("Synthetic provenance must be an explicit boolean.")
    frames = _indexed(value["frames"], {"id", "axes"}, 256)
    for frame in frames.values():
        if frame["axes"] is not None:
            for axis in _array(frame["axes"], 3, 3):
                _text(axis, 128)
    clocks = _indexed(value["clocks"], {"id", "unit", "epoch"}, 128)
    for clock in clocks.values():
        _choice(clock["unit"], {"s", "ms", "us", "ns"})
        _choice(clock["epoch"], {"unix", "boot", "device", "unknown"})
    files, streams = _files(value["files"]), {}
    for stream in _array(value["streams"], 1, 128):
        if type(stream) is not dict:
            _fail("Stream must be an object.")
        kind = _choice(stream.get("kind"), {"camera", "imu", "reference_pose"})
        details = "reference" if kind == "reference_pose" else kind
        _object(stream, _STREAM | {details})
        name = _id(stream["id"])
        if name in streams:
            _fail("Repeated stream identifier.")
        streams[name] = stream
        _text(stream["sensor_instance"], 128)
        if _id(stream["frame_id"]) not in frames or _id(stream["clock_id"]) not in clocks:
            _fail("Stream references an undeclared frame or clock.")
        _choice(stream["timestamp_semantics"], {"sample", "exposure_start", "exposure_midpoint",
                                               "exposure_end", "message_write", "unknown"})
        semantics = {"camera": {"image"}, "imu": {"instantaneous_imu", "filtered_snapshot"},
                     "reference_pose": {"pose"}}[kind]
        _choice(stream["measurement_semantics"], semantics)
        index = _path(stream["index_path"])
        prefix, role = ("references/", "reference") if kind == "reference_pose" else ("sensors/", "sensor")
        if not index.startswith(prefix) or index not in files or files[index]["role"] != role:
            _fail("Stream index requires a declared file in its sensor/reference namespace.")
        segments = _indexed(stream["segments"], {"id", "first_tick", "last_tick", "sample_count"}, 10_000)
        for segment in segments.values():
            _ordered_ticks(segment["first_tick"], segment["last_tick"], clocks[stream["clock_id"]]["unit"])
            _integer(segment["sample_count"], 1)
        processing = _object(stream["processing"], {"calibration", "filtering", "clock_correction", "description"})
        _choice(processing["calibration"], {"applied", "not_applied", "unknown"})
        _choice(processing["filtering"], {"applied", "none", "unknown"})
        _choice(processing["clock_correction"], {"applied", "not_applied", "unknown"})
        _text(processing["description"])
        info = stream[details]
        if kind == "camera":
            _object(info, {"resolution", "encoding", "shutter"})
            _resolution(info["resolution"])
            _choice(info["encoding"], {"mono8", "mono16"})
            _choice(info["shutter"], {"global", "rolling", "unknown"})
        elif kind == "imu":
            _object(info, {"angular_velocity_unit", "specific_force_unit", "local_gravity_m_s2"})
            _choice(info["angular_velocity_unit"], {"rad/s", "deg/s"})
            _choice(info["specific_force_unit"], {"m/s^2", "g"})
            if info["local_gravity_m_s2"] is not None:
                _number(info["local_gravity_m_s2"], positive=True)
            if info["specific_force_unit"] == "g" and info["local_gravity_m_s2"] is None:
                _fail("Local gravity is required for acceleration expressed in g.")
        else:
            _object(info, {"parent_frame_id", "position_unit", "orientation", "use", "coverage",
                           "position_uncertainty_m", "orientation_uncertainty_rad"})
            if _id(info["parent_frame_id"]) not in frames:
                _fail("Reference parent frame is undeclared.")
            _choice(info["position_unit"], {"m"})
            _choice(info["orientation"], {"Hamilton_xyzw_frame_to_parent"})
            _choice(info["use"], {"independent_evaluation_only"})
            _choice(info["coverage"], {"full", "partial", "unknown"})
            for key in ("position_uncertainty_m", "orientation_uncertainty_rad"):
                if info[key] is not None:
                    _number(info[key], minimum=0)
    pairing = value["stereo_pairing"]
    if pairing is not None:
        _object(pairing, {"camera_ids", "policy"})
        cameras = _array(pairing["camera_ids"], 2, 2)
        for camera in cameras:
            if _id(camera) not in streams or streams[camera]["kind"] != "camera":
                _fail("Stereo pairing references an undeclared camera.")
        if cameras[0] == cameras[1]:
            _fail("Stereo pairing requires distinct cameras.")
        _choice(pairing["policy"], {"exact_timestamp_pairs", "unknown"})
        if pairing["policy"] == "exact_timestamp_pairs":
            left, right = (streams[name] for name in cameras)
            ranges = lambda s: [(v["first_tick"], v["last_tick"], v["sample_count"]) for v in s["segments"]]
            if left["clock_id"] != right["clock_id"] or ranges(left) != ranges(right):
                _fail("Declared exact stereo pairing has inconsistent clocks or segment summaries.")


def _calibration(value):
    _reference(value["acquisition"])
    _choice(value["state"], {"supplied", "estimated"})
    method = _object(value["method"], {"name", "version"})
    _text(method["name"])
    _text(method["version"])
    names = set()
    for camera in _array(value["cameras"], maximum=128):
        _object(camera, {"sensor_id", "frame_id", "resolution", "projection", "distortion", "intrinsics", "coefficients"})
        name = _id(camera["sensor_id"])
        if name in names:
            _fail("Repeated calibrated camera.")
        names.add(name)
        _id(camera["frame_id"])
        _resolution(camera["resolution"])
        _choice(camera["projection"], {"pinhole"})
        _choice(camera["distortion"], {"radtan", "equidistant"})
        _vector(camera["intrinsics"], 4)
        for focal in camera["intrinsics"][:2]:
            _number(focal, positive=True)
        _vector(camera["coefficients"], 4)
    imu = value["imu"]
    if imu is not None:
        _object(imu, {"sensor_id", "frame_id", "gyro_bias_rad_s", "accel_bias_m_s2", "gyro_scale", "accel_scale", "noise"})
        if _id(imu["sensor_id"]) in names:
            _fail("IMU and camera sensor identities must differ.")
        _id(imu["frame_id"])
        for field in ("gyro_bias_rad_s", "accel_bias_m_s2", "gyro_scale", "accel_scale"):
            _vector(imu[field], 3, positive=field.endswith("scale"))
        if imu["noise"] is not None:
            noise = _object(imu["noise"], {"model", "gyro_density_rad_s_per_sqrt_hz", "accel_density_m_s2_per_sqrt_hz",
                                        "gyro_bias_diffusion_rad_s_per_sqrt_s", "accel_bias_diffusion_m_s2_per_sqrt_s",
                                        "assumptions"})
            _choice(noise["model"], {"continuous_white_noise_and_bias_diffusion"})
            _text(noise["assumptions"])
            for key in set(noise) - {"model", "assumptions"}:
                _number(noise[key], minimum=0)
    pairs = set()
    for transform in _array(value["transforms"], maximum=1024):
        _object(transform, {"source_frame", "target_frame", "rotation", "translation_m"})
        pair = (_id(transform["source_frame"]), _id(transform["target_frame"]))
        if pair in pairs or pair[0] == pair[1]:
            _fail("Transforms require distinct frames and unique directed pairs.")
        pairs.add(pair)
        validate_rigid_transform(transform["rotation"], transform["translation_m"])
    if not value["cameras"] and imu is None and not value["transforms"]:
        _fail("Calibration must declare at least one supported parameter group.")
    applicability = _object(value["applicability"], {"description", "imu_intrinsics"})
    _text(applicability["description"])
    _choice(applicability["imu_intrinsics"], {"already_applied", "candidate_not_applied", "not_applicable"})
    validation = _object(value["validation"], {"status", "acquisitions", "report"})
    _choice(validation["status"], {"not_provided", "reported"})
    acquisitions = _references(validation["acquisitions"])
    if validation["status"] == "not_provided":
        if acquisitions or validation["report"] is not None:
            _fail("Unprovided validation cannot include validation evidence.")
    else:
        _file(validation["report"])
        if not acquisitions or any(ref["sha256"] == value["acquisition"]["sha256"] for ref in acquisitions.values()):
            _fail("Reported validation needs distinct acquisition identities.")


def _clock_mapping(value):
    _reference(value["acquisition"])
    for key in ("source_stream_id", "source_segment_id", "target_clock_id"):
        _id(value[key])
    _choice(value["purpose"], {"clock_domain_mapping", "sensor_time_offset"})
    _choice(value["model"], {"constant_offset_ns"})
    keys = ("source_origin_ns", "target_origin_ns", "offset_ns", "first_source_ns", "last_source_ns")
    stamps = {key: timestamp_ns(value[key]) for key in keys}
    options = {"source_origin_ns": stamps["source_origin_ns"], "target_origin_ns": stamps["target_origin_ns"],
               "offset_ns": stamps["offset_ns"], "segment_start_ns": stamps["first_source_ns"],
               "segment_end_ns": stamps["last_source_ns"]}
    for key in ("first_source_ns", "last_source_ns"):
        map_constant_clock(stamps[key], **options)
    evidence = _object(value["evidence"], {"status", "description", "report"})
    _choice(evidence["status"], {"declared", "estimated"})
    _text(evidence["description"])
    if evidence["report"] is not None:
        _file(evidence["report"])


def _prepared(value):
    _reference(value["acquisition"])
    method = _object(value["method"], {"name", "version"})
    _text(method["name"])
    _text(method["version"])
    if value["calibration"] is not None:
        _reference(value["calibration"])
    mappings = _references(value["clock_mappings"])
    selections = set()
    for item in _array(value["selections"], 1, 10_000):
        _object(item, {"stream_id", "segment_id", "first_tick", "last_tick"})
        key = (_id(item["stream_id"]), _id(item["segment_id"]))
        _ordered_ticks(item["first_tick"], item["last_tick"])
        if key in selections:
            _fail("Only one contiguous selection per stream segment is supported.")
        selections.add(key)
    operations, phases, transformed_streams = set(), {}, set()
    for item in _array(value["operations"], maximum=10_000):
        _object(item, {"kind", "stream_id", "artifact", "description", "parameters"})
        kind = _choice(item["kind"], {"unit_conversion", "frame_transform", "image_conversion",
                                     "imu_calibration", "clock_mapping"})
        _id(item["stream_id"])
        _text(item["description"])
        params = item["parameters"]
        if kind == "unit_conversion":
            _object(params, {"quantity", "source_unit", "target_unit", "local_gravity_m_s2"})
            quantity = _choice(params["quantity"], {"angular_velocity", "specific_force"})
            source, target = ("deg/s", "rad/s") if quantity == "angular_velocity" else ("g", "m/s^2")
            _choice(params["source_unit"], {source})
            _choice(params["target_unit"], {target})
            if params["local_gravity_m_s2"] is not None:
                _number(params["local_gravity_m_s2"], positive=True)
            if quantity == "specific_force" and params["local_gravity_m_s2"] is None:
                _fail("Conversion from g requires explicit local gravity.")
        elif kind == "frame_transform":
            _object(params, {"source_frame", "target_frame"})
            if _id(params["source_frame"]) == _id(params["target_frame"]):
                _fail("Frame conversion requires distinct frames.")
            if item["stream_id"] in transformed_streams:
                _fail("Only one frame transform per stream is supported.")
            transformed_streams.add(item["stream_id"])
        elif kind == "image_conversion":
            _object(params, {"source_encoding", "target_encoding", "scale", "offset", "rounding"})
            _choice(params["source_encoding"], {"mono16"})
            _choice(params["target_encoding"], {"mono8"})
            _number(params["scale"])
            _number(params["offset"])
            if params["scale"] != 255 / 65535 or params["offset"] != 0:
                _fail("Image conversion must preserve the declared pinned scaling.")
            _choice(params["rounding"], {"opencv_convertTo"})
        elif kind == "imu_calibration":
            _object(params, {"model"})
            _choice(params["model"], {"diagonal_scale_after_bias_subtraction"})
        else:
            _object(params, set())
        if kind in {"unit_conversion", "imu_calibration", "frame_transform"}:
            phase = {"unit_conversion": 0, "imu_calibration": 1, "frame_transform": 2}[kind]
            if phase < phases.get(item["stream_id"], 0):
                _fail("Measurement operations must follow unit conversion, IMU calibration, then frame transformation.")
            phases[item["stream_id"]] = phase
        artifact = item["artifact"]
        if kind in {"unit_conversion", "image_conversion"}:
            if artifact is not None:
                _fail("Unit/image conversion has no calibration artifact.")
        else:
            _reference(artifact)
            expected = mappings.get(artifact["id"]) if kind == "clock_mapping" else value["calibration"]
            if artifact != expected:
                _fail("Operation artifact is not its declared calibration or clock mapping.")
        parameter_identity = (params["quantity"] if kind == "unit_conversion"
                              else json.dumps(params, sort_keys=True, separators=(",", ":")))
        key = (kind, item["stream_id"], artifact["id"] if artifact else None, parameter_identity)
        if key in operations:
            _fail("Repeated preparation operation.")
        operations.add(key)
    files = _files(value["files"])
    outputs = set()
    for output in _array(value["outputs"], 1, 128):
        _object(output, {"stream_id", "index_path", "frame_id", "clock_id", "timestamp_unit"})
        name = _id(output["stream_id"])
        if name in outputs:
            _fail("Prepared output stream identifiers must be unique.")
        outputs.add(name)
        for field in ("frame_id", "clock_id"):
            _id(output[field])
        _choice(output["timestamp_unit"], {"ns"})
        if _path(output["index_path"]) not in files:
            _fail("Prepared output index is missing from its file inventory.")
    if outputs != {item["stream_id"] for item in value["selections"]}:
        _fail("Prepared outputs must identify every selected stream exactly once.")
    _choice(value["target_profile"], {"stereo_imu_candidate_v1", "imu_snapshot_candidate_v1"})


def validate_contract(value) -> None:
    """Validate a declaration, without consulting any referenced payload."""
    _json_graph(value)
    if type(value) is not dict:
        _fail("Contract must be a JSON object.")
    kind = _choice(value.get("kind"), _EXTRA)
    _object(value, _COMMON | _EXTRA[kind])
    _choice(value["format"], {FORMAT})
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        _fail("Unsupported sensor contract schema version.")
    _id(value["id"])
    {"acquisition": _acquisition, "calibration": _calibration,
     "clock_mapping": _clock_mapping, "prepared_input": _prepared}[kind](value)
    if len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)) > MAX_JSON_BYTES:
        _fail("Contract exceeds its encoded size limit.")


def contract_sha256(value) -> str:
    """Hash the exact validated declaration; order of arrays remains significant."""
    validate_contract(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(encoded) > MAX_JSON_BYTES:
        _fail("Contract exceeds its encoded size limit.")
    return hashlib.sha256(encoded).hexdigest()


def loads_contract(text) -> dict:
    """Read bounded JSON metadata, rejecting ambiguous keys and nonfinite data."""
    if type(text) not in (str, bytes) or len(text) > MAX_JSON_BYTES:
        _fail("Contract JSON must be bounded text or bytes.")
    if type(text) is bytes:
        try:
            text = text.decode("utf-8")
        except UnicodeError as error:
            raise ValueError("Contract JSON must be UTF-8.") from error
    if type(text) is str:
        try:
            if len(text.encode("utf-8")) > MAX_JSON_BYTES:
                _fail("Contract exceeds its encoded size limit.")
        except UnicodeError as error:
            raise ValueError("Invalid Unicode JSON.") from error
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("Duplicate JSON object key.")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail("Nonfinite JSON number."))
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid contract JSON.") from error
    validate_contract(value)
    return value


def assess_acquisition(value) -> dict:
    """Describe metadata limitations; this is never an execution admission check."""
    validate_contract(value)
    if value["kind"] != "acquisition":
        _fail("Acquisition assessment requires an acquisition contract.")
    unknown = []
    for frame in value["frames"]:
        if frame["axes"] is None:
            unknown.append("frame_axes:" + frame["id"])
    for clock in value["clocks"]:
        if clock["epoch"] == "unknown":
            unknown.append("clock_epoch:" + clock["id"])
    for stream in value["streams"]:
        if stream["timestamp_semantics"] == "unknown":
            unknown.append("timestamp_semantics:" + stream["id"])
        for key in ("calibration", "filtering", "clock_correction"):
            if stream["processing"][key] == "unknown":
                unknown.append("processing_" + key + ":" + stream["id"])
        if stream["kind"] == "camera" and stream["camera"]["shutter"] == "unknown":
            unknown.append("camera_shutter:" + stream["id"])
    references = [s["reference"]["coverage"] for s in value["streams"] if s["kind"] == "reference_pose"]
    return {"descriptor_validity": "valid", "assessment_scope": "declarations_only",
            "payload_verification": "not_performed", "execution_readiness": "not_assessed",
            "calibration_validity": "not_assessed", "reference_coverage_verification": "not_performed",
            "reference_availability": "declared" if references else "absent",
            "declared_reference_coverage": references, "unknown_metadata": unknown,
            "metadata_blockers": list(unknown)}


def _resolve(reference, records, kind):
    record = records.get(reference["id"])
    if record is None or record["kind"] != kind or contract_sha256(record) != reference["sha256"]:
        _fail("Bundle identity reference is missing, has the wrong kind or has a mismatched hash.")
    return record


def validate_bundle(contracts) -> None:
    """Check cross-record declarations, without opening or applying their files."""
    records = {}
    for contract in _array(contracts, 1, 10_000):
        validate_contract(contract)
        if contract["id"] in records:
            _fail("Bundle identifiers must be unique.")
        records[contract["id"]] = contract
    for contract in contracts:
        if contract["kind"] == "acquisition":
            continue
        acquisition = _resolve(contract["acquisition"], records, "acquisition")
        streams = {s["id"]: s for s in acquisition["streams"]}
        frames = {f["id"] for f in acquisition["frames"]}
        clocks = {c["id"]: c for c in acquisition["clocks"]}
        def stream_segment(stream_id, segment_id):
            stream = streams.get(stream_id)
            segment = next((s for s in stream["segments"] if s["id"] == segment_id), None) if stream else None
            if segment is None:
                _fail("Bundle refers to an unknown stream segment.")
            return stream, segment
        if contract["kind"] == "calibration":
            for declared in contract["cameras"] + ([contract["imu"]] if contract["imu"] else []):
                stream = streams.get(declared["sensor_id"])
                kind = "camera" if "resolution" in declared else "imu"
                if stream is None or stream["kind"] != kind or stream["frame_id"] != declared["frame_id"]:
                    _fail("Calibrated sensor/frame does not match the source acquisition.")
                if kind == "camera" and stream["camera"]["resolution"] != declared["resolution"]:
                    _fail("Camera calibration resolution differs from its acquisition.")
            for transform in contract["transforms"]:
                if {transform["source_frame"], transform["target_frame"]} - frames:
                    _fail("Calibration transform uses undeclared frames.")
            for ref in contract["validation"]["acquisitions"]:
                _resolve(ref, records, "acquisition")
        elif contract["kind"] == "clock_mapping":
            stream, segment = stream_segment(contract["source_stream_id"], contract["source_segment_id"])
            if contract["target_clock_id"] not in clocks:
                _fail("Clock mapping target clock is undeclared.")
            first, last = _ordered_ticks(segment["first_tick"], segment["last_tick"], clocks[stream["clock_id"]]["unit"])
            if not first <= timestamp_ns(contract["first_source_ns"]) <= timestamp_ns(contract["last_source_ns"]) <= last:
                _fail("Clock mapping validity exceeds its source segment.")
        else:
            calibration = _resolve(contract["calibration"], records, "calibration") if contract["calibration"] else None
            mappings = {ref["id"]: _resolve(ref, records, "clock_mapping") for ref in contract["clock_mappings"]}
            for dependency in mappings.values():
                if dependency["acquisition"] != contract["acquisition"]:
                    _fail("Prepared clock mapping refers to a different source acquisition.")
            ranges = {}
            for mapping in mappings.values():
                key = mapping["source_stream_id"], mapping["source_segment_id"]
                bounds = timestamp_ns(mapping["first_source_ns"]), timestamp_ns(mapping["last_source_ns"])
                if any(max(bounds[0], old[0]) <= min(bounds[1], old[1]) for old in ranges.get(key, [])):
                    _fail("Clock mappings overlap within one source stream segment.")
                ranges.setdefault(key, []).append(bounds)
            if calibration:
                source = _resolve(calibration["acquisition"], records, "acquisition")
                if source["rig"] != acquisition["rig"]:
                    _fail("Calibration and replay require the same declared rig revision.")
                source_streams = {item["id"]: item for item in source["streams"]}
                source_axes = {item["id"]: item["axes"] for item in source["frames"]}
                target_axes = {item["id"]: item["axes"] for item in acquisition["frames"]}
                for declared in calibration["cameras"] + ([calibration["imu"]] if calibration["imu"] else []):
                    source_sensor, target = source_streams.get(declared["sensor_id"]), streams.get(declared["sensor_id"])
                    if (source_sensor is None or target is None
                            or any(source_sensor[key] != target[key] for key in ("kind", "sensor_instance", "frame_id"))
                            or target["frame_id"] != declared["frame_id"]
                            or source_axes[target["frame_id"]] != target_axes[target["frame_id"]]):
                        _fail("Calibration sensor identity or frame differs from the replay acquisition.")
                    if target["kind"] == "camera" and target["camera"]["resolution"] != declared["resolution"]:
                        _fail("Calibration resolution differs from the replay acquisition.")
                for transform in calibration["transforms"]:
                    for name in (transform["source_frame"], transform["target_frame"]):
                        if name not in target_axes or source_axes.get(name) != target_axes[name]:
                            _fail("Calibration transform frame differs from the replay acquisition.")
                transform_frames = {entry[key] for entry in calibration["transforms"]
                                    for key in ("source_frame", "target_frame")}
                for sensor_id, sensor in source_streams.items():
                    if sensor["kind"] == "reference_pose" or sensor["frame_id"] not in transform_frames:
                        continue
                    target = streams.get(sensor_id)
                    if (target is None or any(sensor[key] != target[key] for key in ("kind", "sensor_instance", "frame_id"))
                            or (sensor["kind"] == "camera" and sensor["camera"]["resolution"] != target["camera"]["resolution"])):
                        _fail("Extrinsic calibration sensor differs from the replay acquisition.")
            segment_positions = {name: {item["id"]: position for position, item in enumerate(stream["segments"])}
                                 for name, stream in streams.items()}
            selected_positions = {}
            for selection in contract["selections"]:
                stream, segment = stream_segment(selection["stream_id"], selection["segment_id"])
                position = segment_positions[stream["id"]][segment["id"]]
                if position <= selected_positions.get(stream["id"], -1):
                    _fail("Selections must preserve each stream's original segment order.")
                selected_positions[stream["id"]] = position
                unit = clocks[stream["clock_id"]]["unit"]
                first, last = _ordered_ticks(selection["first_tick"], selection["last_tick"], unit)
                source_first, source_last = _ordered_ticks(segment["first_tick"], segment["last_tick"], unit)
                if not source_first <= first <= last <= source_last:
                    _fail("Preparation selection exceeds its source segment.")
            selected = {item["stream_id"]: streams[item["stream_id"]] for item in contract["selections"]}
            cameras = {name for name, item in selected.items() if item["kind"] == "camera"}
            imus = [item for item in selected.values() if item["kind"] == "imu"]
            if contract["target_profile"] == "stereo_imu_candidate_v1":
                pairing = acquisition["stereo_pairing"]
                if (len(cameras) != 2 or len(imus) != 1 or pairing is None
                        or pairing["policy"] != "exact_timestamp_pairs" or cameras != set(pairing["camera_ids"])):
                    _fail("Stereo candidate profile requires the declared exact camera pair and one IMU.")
                paired_intervals = []
                for camera_id in pairing["camera_ids"]:
                    paired_intervals.append([
                        (position, item["first_tick"], item["last_tick"])
                        for position, segment in enumerate(streams[camera_id]["segments"])
                        for item in contract["selections"]
                        if item["stream_id"] == camera_id and item["segment_id"] == segment["id"]
                    ])
                if paired_intervals[0] != paired_intervals[1]:
                    _fail("Stereo candidate selections must match paired segment positions and intervals.")
            elif cameras or len(imus) != 1 or imus[0]["measurement_semantics"] != "filtered_snapshot":
                _fail("Snapshot candidate profile requires one filtered-snapshot IMU and no selected cameras.")
            for operation in contract["operations"]:
                stream = streams.get(operation["stream_id"])
                if stream is None or operation["stream_id"] not in selected:
                    _fail("Preparation operation references an unknown or unselected stream.")
                kind = operation["kind"]
                if kind == "imu_calibration":
                    if (stream["kind"] != "imu" or calibration["imu"] is None
                            or calibration["imu"]["sensor_id"] != stream["id"]
                            or calibration["applicability"]["imu_intrinsics"] != "candidate_not_applied"
                            or stream["processing"]["calibration"] == "applied"):
                        _fail("IMU calibration is inapplicable or already applied.")
                if kind == "clock_mapping":
                    mapping = mappings[operation["artifact"]["id"]]
                    selections = [item for item in contract["selections"] if item["stream_id"] == stream["id"]
                                  and item["segment_id"] == mapping["source_segment_id"]]
                    if mapping["source_stream_id"] != stream["id"] or not selections:
                        _fail("Clock mapping operation requires its selected source stream segment.")
                    unit = clocks[stream["clock_id"]]["unit"]
                    for selection in selections:
                        first, last = _ordered_ticks(selection["first_tick"], selection["last_tick"], unit)
                        if not timestamp_ns(mapping["first_source_ns"]) <= first <= last <= timestamp_ns(mapping["last_source_ns"]):
                            _fail("Clock mapping validity does not cover its selected interval.")
                if kind == "image_conversion" and stream["kind"] != "camera":
                    _fail("Image conversion requires a camera stream.")
                params = operation["parameters"]
                if kind == "image_conversion" and stream["camera"]["encoding"] != params["source_encoding"]:
                    _fail("Image conversion does not match its source encoding.")
                if kind == "unit_conversion":
                    unit_key = "angular_velocity_unit" if params["quantity"] == "angular_velocity" else "specific_force_unit"
                    if stream["kind"] != "imu" or stream["imu"][unit_key] != params["source_unit"]:
                        _fail("Unit conversion does not match its source measurement.")
                    if params["quantity"] == "specific_force" and params["local_gravity_m_s2"] != stream["imu"]["local_gravity_m_s2"]:
                        _fail("Unit conversion uses different local gravity.")
                if kind == "frame_transform":
                    pair = params["source_frame"], params["target_frame"]
                    transform = next((entry for entry in calibration["transforms"]
                                      if (entry["source_frame"], entry["target_frame"]) == pair), None)
                    if pair[0] != stream["frame_id"] or transform is None:
                        _fail("Frame conversion requires the selected direct calibration transform.")
                    if stream["kind"] != "imu" or any(component != 0 for component in transform["translation_m"]):
                        _fail("Frame operations support IMU vector rotation with zero translation only.")
            files = {item["path"]: item for item in contract["files"]}
            for output in contract["outputs"]:
                stream = selected[output["stream_id"]]
                operations = [item for item in contract["operations"] if item["stream_id"] == stream["id"]]
                frame_targets = {item["parameters"]["target_frame"] for item in operations if item["kind"] == "frame_transform"}
                clock_targets = {mappings[item["artifact"]["id"]]["target_clock_id"]
                                 for item in operations if item["kind"] == "clock_mapping"}
                if (len(frame_targets) > 1 or len(clock_targets) > 1
                        or output["frame_id"] != next(iter(frame_targets), stream["frame_id"])
                        or output["clock_id"] != next(iter(clock_targets), stream["clock_id"])):
                    _fail("Prepared output frame or clock does not match its declared operations.")
                if output["clock_id"] != stream["clock_id"]:
                    applied = [mappings[item["artifact"]["id"]] for item in operations if item["kind"] == "clock_mapping"]
                    for selection in contract["selections"]:
                        if selection["stream_id"] != stream["id"]:
                            continue
                        first, last = _ordered_ticks(selection["first_tick"], selection["last_tick"],
                                                     clocks[stream["clock_id"]]["unit"])
                        if not any(mapping["source_segment_id"] == selection["segment_id"]
                                   and timestamp_ns(mapping["first_source_ns"]) <= first <= last
                                   <= timestamp_ns(mapping["last_source_ns"]) for mapping in applied):
                            _fail("Changed output clock requires mapping every selected interval and reset segment.")
                prefix, role = ("references/", "reference") if stream["kind"] == "reference_pose" else ("sensors/", "sensor")
                if not output["index_path"].startswith(prefix) or files[output["index_path"]]["role"] != role:
                    _fail("Prepared output index has the wrong sensor/reference namespace or role.")
                if stream["kind"] == "imu":
                    quantities = {item["parameters"]["quantity"] for item in operations if item["kind"] == "unit_conversion"}
                    for quantity, source_key, unit in (("angular_velocity", "angular_velocity_unit", "rad/s"),
                                                       ("specific_force", "specific_force_unit", "m/s^2")):
                        if stream["imu"][source_key] != unit and quantity not in quantities:
                            _fail("Prepared IMU output requires declared conversion to canonical SI units.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate sensor-contract metadata only; referenced files are never read.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.path.open("rb") as stream:
            value = loads_contract(stream.read(MAX_JSON_BYTES + 1))
        report = {"id": value["id"], "kind": value["kind"], "schema_version": SCHEMA_VERSION,
                  "sha256": contract_sha256(value), "descriptor_validity": "valid",
                  "payload_verification": "not_performed", "execution_readiness": "not_assessed"}
        if value["kind"] == "acquisition":
            report.update(assess_acquisition(value))
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    except (OSError, ValueError) as error:
        print("Invalid sensor contract: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
