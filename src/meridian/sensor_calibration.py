"""Bounded supplied-calibration application plans, using only the standard library.

This module validates declarations and arithmetic; it never reads sensor files,
fits parameters, decodes images or establishes physical calibration accuracy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from .sensor_contract import contract_sha256, validate_bundle, validate_contract
from .sensor_conventions import (
    map_constant_clock, timestamp_ns, timestamp_to_ns, transform_point,
)


APPLICATION_METHOD = {"name": "meridian-sensor-preparation", "version": "2"}


def identity(document):
    return {"id": document["id"], "sha256": contract_sha256(document)}


def selected_streams(acquisition, imu_stream, include_reference):
    imus = [s for s in acquisition["streams"] if s["kind"] == "imu"]
    if imu_stream is None:
        if len(imus) != 1:
            raise ValueError("Select one IMU stream explicitly when the recording has multiple IMUs.")
        imu = imus[0]
    else:
        matches = [s for s in imus if s["id"] == imu_stream]
        if len(matches) != 1:
            raise ValueError("Selected IMU stream does not exist.")
        imu = matches[0]
    cameras = [s for s in acquisition["streams"] if s["kind"] == "camera"]
    if cameras:
        pairing = acquisition["stereo_pairing"]
        if len(cameras) != 2 or pairing is None or pairing["policy"] != "exact_timestamp_pairs":
            raise ValueError("Preparation requires exactly one declared synchronized stereo pair.")
        if imu["measurement_semantics"] != "instantaneous_imu":
            raise ValueError("Stereo preparation requires instantaneous IMU semantics.")
        profile = "stereo_imu_candidate_v1"
    else:
        if imu["measurement_semantics"] != "filtered_snapshot":
            raise ValueError("IMU-only preparation currently supports filtered snapshots.")
        profile = "imu_snapshot_candidate_v1"
    return [s for s in acquisition["streams"] if s is imu or s["kind"] == "camera"
            or (include_reference and s["kind"] == "reference_pose")], profile


def _operation(kind, stream_id, parameters, description, artifact=None):
    return {"kind": kind, "stream_id": stream_id, "artifact": artifact,
            "parameters": parameters, "description": description}


def apply_clock(stamp, mapping):
    return map_constant_clock(stamp,
        source_origin_ns=timestamp_ns(mapping["source_origin_ns"]),
        target_origin_ns=timestamp_ns(mapping["target_origin_ns"]),
        offset_ns=timestamp_ns(mapping["offset_ns"]),
        segment_start_ns=timestamp_ns(mapping["first_source_ns"]),
        segment_end_ns=timestamp_ns(mapping["last_source_ns"]))


def apply_imu(values, *, intrinsics=None, rotation=None):
    """SI vectors: diagonal gain after bias subtraction, then a direct rotation."""
    result = list(values)
    if len(result) != 6 or any(type(v) not in (int, float) or not math.isfinite(v) for v in result):
        raise ValueError("IMU correction requires six finite SI measurements.")
    if intrinsics is not None:
        bias = intrinsics["gyro_bias_rad_s"] + intrinsics["accel_bias_m_s2"]
        scale = intrinsics["gyro_scale"] + intrinsics["accel_scale"]
        result = [(value - offset) * gain for value, offset, gain in zip(result, bias, scale, strict=True)]
        if not all(math.isfinite(v) for v in result):
            raise ValueError("Corrected IMU measurement exceeds the finite numerical range.")
    if rotation is not None:
        result = list(transform_point(result[:3], rotation, [0, 0, 0])) + list(
            transform_point(result[3:], rotation, [0, 0, 0]))
    return result


def validate_application_request(acquisition, *, calibration=None, clock_mappings=(),
        dependency_acquisitions=(), apply_imu_intrinsics=False, imu_target_frame=None,
        imu_stream=None, include_reference=True, image_encoding="preserve"):
    """Return a deterministic, payload-free plan for an immutable full-source revision.

    Calibration parameters may be selected without changing observations. Numeric
    operations are explicit and require known unapplied source corrections.
    One constant mapping per stream/reset segment is supported; composition is
    rejected rather than inferred from the order of separate clock artifacts.
    """
    validate_contract(acquisition)
    if acquisition["kind"] != "acquisition":
        raise ValueError("Application requires the original acquisition descriptor.")
    if (type(apply_imu_intrinsics) is not bool or type(include_reference) is not bool
            or type(image_encoding) is not str or image_encoding not in {"preserve", "mono8"}
            or (imu_target_frame is not None and type(imu_target_frame) is not str)
            or (imu_stream is not None and type(imu_stream) is not str)):
        raise ValueError("Unsupported calibration application option.")
    if type(clock_mappings) not in (list, tuple) or type(dependency_acquisitions) not in (list, tuple):
        raise ValueError("Application dependencies require bounded lists.")
    if len(clock_mappings) > 1280 or len(dependency_acquisitions) > 128:
        raise ValueError("Too many application dependencies.")
    if calibration is None and not clock_mappings:
        raise ValueError("Select a calibration or a clock mapping for a derived revision.")
    calibration, mappings, dependencies = deepcopy(calibration), deepcopy(list(clock_mappings)), deepcopy(list(dependency_acquisitions))
    records = [acquisition]
    by_id = {acquisition["id"]: acquisition}
    for document, expected in [(d, "acquisition") for d in dependencies] + (
            [(calibration, "calibration")] if calibration is not None else []) + [(d, "clock_mapping") for d in mappings]:
        validate_contract(document)
        if document["kind"] != expected:
            raise ValueError("Application dependency has the wrong contract kind.")
        previous = by_id.get(document["id"])
        if previous is not None:
            if expected != "acquisition" or identity(previous) != identity(document):
                raise ValueError("Application dependency identifiers must be unique.")
        else:
            by_id[document["id"]] = document
            records.append(document)
    dependencies = sorted((d for d in records if d["kind"] == "acquisition" and d["id"] != acquisition["id"]), key=lambda d: d["id"])
    mappings.sort(key=lambda d: (d["source_stream_id"], d["source_segment_id"], d["id"]))
    streams, profile = selected_streams(acquisition, imu_stream, include_reference)
    imu = next(s for s in streams if s["kind"] == "imu")
    selected = {s["id"]: s for s in streams}
    clocks = {c["id"]: c for c in acquisition["clocks"]}
    mapping_index = {}
    for mapping in mappings:
        stream = selected.get(mapping["source_stream_id"])
        if stream is None:
            raise ValueError("Clock mapping refers to an unselected source stream.")
        if stream["processing"]["clock_correction"] != "not_applied":
            raise ValueError("Clock correction is already applied or its source status is unknown.")
        key = stream["id"], mapping["source_segment_id"]
        if key in mapping_index:
            raise ValueError("Clock mapping composition within one source segment is unsupported.")
        mapping_index[key] = mapping
        if mapping["purpose"] == "clock_domain_mapping":
            if mapping["target_clock_id"] == stream["clock_id"]:
                raise ValueError("A clock-domain mapping requires a distinct target clock.")
        elif (stream["kind"] == "reference_pose" or mapping["target_clock_id"] != stream["clock_id"]
                or mapping["source_origin_ns"] != mapping["target_origin_ns"]):
            raise ValueError("A sensor time offset requires one unchanged clock and equal origins.")
    if apply_imu_intrinsics:
        if (calibration is None or calibration["imu"] is None
                or calibration["imu"]["sensor_id"] != imu["id"]
                or calibration["applicability"]["imu_intrinsics"] != "candidate_not_applied"
                or imu["processing"]["calibration"] != "not_applied"):
            raise ValueError("IMU correction requires explicit unapplied compatible intrinsics.")
    rotation = None
    if imu_target_frame is not None:
        rotation = next((t for t in calibration["transforms"]
                         if t["source_frame"] == imu["frame_id"] and t["target_frame"] == imu_target_frame), None) if calibration else None
        if rotation is None or any(v != 0 for v in rotation["translation_m"]):
            raise ValueError("IMU frame change requires a direct zero-translation transform.")
    operations, outputs, selections = [], [], []
    for stream in streams:
        name = stream["id"]
        if stream["kind"] == "camera" and image_encoding == "mono8" and stream["camera"]["encoding"] == "mono16":
            operations.append(_operation("image_conversion", name,
                {"source_encoding": "mono16", "target_encoding": "mono8", "scale": 255 / 65535,
                 "offset": 0, "rounding": "opencv_convertTo"}, "Pinned grayscale representation conversion."))
        if stream["kind"] == "imu":
            for quantity, field, unit in (("angular_velocity", "angular_velocity_unit", "rad/s"),
                                           ("specific_force", "specific_force_unit", "m/s^2")):
                if stream["imu"][field] != unit:
                    operations.append(_operation("unit_conversion", name,
                        {"quantity": quantity, "source_unit": stream["imu"][field], "target_unit": unit,
                         "local_gravity_m_s2": stream["imu"]["local_gravity_m_s2"] if quantity == "specific_force" else None},
                        "Convert source measurements to SI before calibration."))
            if apply_imu_intrinsics:
                operations.append(_operation("imu_calibration", name,
                    {"model": "diagonal_scale_after_bias_subtraction"}, "Apply supplied diagonal gain after SI bias subtraction.", identity(calibration)))
            if rotation is not None:
                operations.append(_operation("frame_transform", name,
                    {"source_frame": stream["frame_id"], "target_frame": imu_target_frame},
                    "Rotate IMU vectors without translation or lever-arm compensation.", identity(calibration)))
        targets = set()
        for segment in stream["segments"]:
            selections.append({"stream_id": name, "segment_id": segment["id"],
                               "first_tick": segment["first_tick"], "last_tick": segment["last_tick"]})
            mapping = mapping_index.get((name, segment["id"]))
            if mapping is not None:
                operations.append(_operation("clock_mapping", name, {},
                    "Apply one declared integer-nanosecond correction to this source segment.", identity(mapping)))
                targets.add(mapping["target_clock_id"])
        if len(targets) > 1:
            raise ValueError("One output stream cannot mix clock domains.")
        branch = "references" if stream["kind"] == "reference_pose" else "sensors"
        outputs.append({"stream_id": name, "index_path": f"{branch}/{name}/data.csv",
            "frame_id": imu_target_frame if stream["kind"] == "imu" and rotation else stream["frame_id"],
            "clock_id": next(iter(targets), stream["clock_id"]), "timestamp_unit": "ns"})
    request = {"calibration": calibration, "clock_mappings": mappings, "dependency_acquisitions": dependencies,
        "apply_imu_intrinsics": apply_imu_intrinsics, "imu_target_frame": imu_target_frame,
        "imu_stream": imu["id"], "include_reference": include_reference, "image_encoding": image_encoding}
    digest = hashlib.sha256(json.dumps({"acquisition": identity(acquisition), "method": APPLICATION_METHOD,
        "request": request}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    prepared_id = "prepared-" + digest[:24]
    # Placeholder inventory is declaration-only, never reported as output bytes.
    files = [{"path": out["index_path"], "sha256": "0" * 64, "size_bytes": 0,
              "role": "reference" if selected[out["stream_id"]]["kind"] == "reference_pose" else "sensor"} for out in outputs]
    prepared = {"format": "meridian-sensor-contract", "schema_version": 1, "kind": "prepared_input", "id": prepared_id,
        "acquisition": identity(acquisition), "method": dict(APPLICATION_METHOD),
        "calibration": identity(calibration) if calibration else None, "clock_mappings": [identity(m) for m in mappings],
        "selections": selections, "operations": operations, "files": files, "outputs": outputs, "target_profile": profile}
    validate_bundle([*records, prepared])
    if profile == "stereo_imu_candidate_v1":
        camera_ids = acquisition["stereo_pairing"]["camera_ids"]
        corrected = []
        for camera_id in camera_ids:
            stream = selected[camera_id]
            bounds = []
            for segment in stream["segments"]:
                mapping = mapping_index.get((camera_id, segment["id"]))
                stamps = [timestamp_to_ns(segment[k], clocks[stream["clock_id"]]["unit"]) for k in ("first_tick", "last_tick")]
                bounds.append(tuple(apply_clock(t, mapping) if mapping else t for t in stamps))
            corrected.append((next(o["clock_id"] for o in outputs if o["stream_id"] == camera_id), bounds))
        if corrected[0] != corrected[1]:
            raise ValueError("Clock corrections would break the declared exact stereo pairing.")
    return {"method": dict(APPLICATION_METHOD), "prepared_id": prepared_id, "request_sha256": digest,
        "request": request, "operations": operations, "outputs": outputs,
        "assessment": {"application_status": "planned", "calibration_parameters": "selected" if calibration else "absent",
            "imu_intrinsics": "selected_for_application" if apply_imu_intrinsics else "not_applied_by_preparation",
            "imu_frame_rotation": "selected_for_application" if rotation else "not_applied_by_preparation",
            "clock_corrections": len(mappings), "camera_pixels": "geometry_unchanged",
            "noise_parameters": "retained_unconsumed", "physical_accuracy": "not_assessed",
            "independent_validation": "reported_not_verified" if calibration and calibration["validation"]["status"] == "reported" else "not_provided"}}
