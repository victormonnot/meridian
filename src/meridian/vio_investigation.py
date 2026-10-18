"""Full-resolution investigation artifacts derived from the qualified VIO evaluator.

No new alignment or numerical qualification protocol is introduced here. Camera
epochs retain missing estimates and unsupported reference brackets explicitly.
Raw IMU input is displayed in its own frame and timestamp basis, without bias
correction, resampling, interpolation, or a claim that every input was processed.
"""

import csv
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from meridian import vio_evaluation as evaluator
from meridian.vio_investigation_metrics import summarize_interval, timestamp_ns


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IMU_HEADER = ["#timestamp [ns]", *[f"w_RS_S_{axis} [rad s^-1]" for axis in "xyz"],
               *[f"a_RS_S_{axis} [m s^-2]" for axis in "xyz"]]
_REQUEST_KEYS = {"schema_version", "specification_sha256", "dataset_manifest_sha256",
                 "dataset_id", "expected_timestamps_ns", "evaluator_sha256",
                 "reference_sha256", "imu_sha256", "license", "run_state"}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError("Expected a lowercase SHA256 digest.")
    return value


def _json_safe_times(value, key=""):
    if key == "timestamp_ns" or key.endswith("_timestamp_ns"):
        if value is None:
            return None
        return [str(item) for item in value] if isinstance(value, list) else str(value)
    if isinstance(value, dict):
        return {name: _json_safe_times(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_times(item) for item in value]
    return value


def build_image_index(manifest: dict, expected_timestamps_ns, *,
                      specification_sha256: str, dataset_manifest_sha256: str) -> dict:
    """Build a backend-only, exact camera allowlist from a verified manifest."""
    expected = evaluator._timestamps(expected_timestamps_ns, "expected camera timestamps")
    if not len(expected):
        raise ValueError("Camera schedule is empty.")
    index = {"schema_version": 1,
             "specification_sha256": _digest(specification_sha256),
             "dataset_manifest_sha256": _digest(dataset_manifest_sha256),
             "image": {"width": 512, "height": 512, "source_encoding": "mono16",
                       "served_encoding": "original_png", "transformation": "none"},
             "cameras": {}}
    for camera in ("cam0", "cam1"):
        rows = []
        for number, stamp in enumerate(expected):
            relative = f"sensors/mav0/{camera}/data/{int(stamp)}.png"
            identity = manifest["files"][relative]
            if (set(identity) != {"sha256", "size_bytes"}
                    or type(identity["size_bytes"]) is not int or identity["size_bytes"] <= 0):
                raise ValueError("Invalid camera file identity.")
            rows.append({"index": number, "timestamp_ns": str(int(stamp)),
                         "relative_path": relative, "sha256": _digest(identity["sha256"]),
                         "size_bytes": identity["size_bytes"]})
        index["cameras"][camera] = rows
    return index


def _imu_samples(path, origin):
    result, previous = [], None
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        if next(reader, None) != _IMU_HEADER:
            raise ValueError("IMU CSV does not match the maintained input schema.")
        for row in reader:
            if len(row) != 7:
                raise ValueError("IMU row must contain a timestamp and six measurements.")
            stamp = timestamp_ns(row[0])
            if previous is not None and stamp <= previous:
                raise ValueError("IMU timestamps must increase strictly.")
            values = [float(item) for item in row[1:]]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("IMU measurements must be finite.")
            result.append({"timestamp_ns": str(stamp), "time_s": (stamp-origin)*1e-9,
                           "gyro_rad_s": values[:3], "accel_m_s2": values[3:]})
            previous = stamp
    if not result:
        raise ValueError("The IMU input must not be empty.")
    return result


def _quaternion(rotation):
    # A symmetric 4x4 eigenproblem avoids a trace division at 180 degree rotations.
    r = rotation
    matrix = np.array([
        [r[0,0]-r[1,1]-r[2,2], r[0,1]+r[1,0], r[0,2]+r[2,0], r[2,1]-r[1,2]],
        [r[0,1]+r[1,0], r[1,1]-r[0,0]-r[2,2], r[1,2]+r[2,1], r[0,2]-r[2,0]],
        [r[0,2]+r[2,0], r[1,2]+r[2,1], r[2,2]-r[0,0]-r[1,1], r[1,0]-r[0,1]],
        [r[2,1]-r[1,2], r[0,2]-r[2,0], r[1,0]-r[0,1], r.trace()],
    ])
    _, vectors = np.linalg.eigh(matrix)
    result = vectors[:, -1]
    return result if result[3] >= 0 else -result


def _pose(position, quaternion):
    rotation = evaluator._rotation_matrices(np.asarray([quaternion]))[0]
    cosine_pitch = math.hypot(rotation[0, 0], rotation[1, 0])
    pitch = math.atan2(-rotation[2, 0], cosine_pitch)
    if cosine_pitch < 1e-8:
        roll, yaw = 0.0, math.atan2(-rotation[0, 1], rotation[1, 1])
    else:
        roll, yaw = math.atan2(rotation[2, 1], rotation[2, 2]), math.atan2(rotation[1, 0], rotation[0, 0])
    return {"position_m": list(map(float, position)),
            "quaternion_xyzw": list(map(float, quaternion)),
            "rpy_deg": [math.degrees(value) for value in (roll, pitch, yaw)]}


def _events(expected, estimated, supported, native_status):
    events = []
    origin = int(expected[0])
    found = np.flatnonzero(estimated)
    if len(found):
        stamp = int(expected[found[0]])
        events.append({"kind": "first_estimated_pose", "timestamp_ns": str(stamp),
                       "time_s": (stamp-origin)*1e-9, "source": "trajectory_output"})
    if native_status is not None:
        stamp = native_status.get("initialized_timestamp_ns")
        # A negative native sentinel is not an initialization event.
        if type(stamp) is int and stamp >= 0:
            events.append({"kind": "native_initialization", "timestamp_ns": str(stamp),
                           "time_s": (stamp-origin)*1e-9, "source": "native_status"})
    for kind, missing in (("missing_estimate", ~estimated), ("unsupported_reference", ~supported)):
        for interval in evaluator._missing_intervals(expected, missing):
            events.append({"kind": kind, **_json_safe_times(interval),
                           "first_time_s": (interval["first_timestamp_ns"]-origin)*1e-9,
                           "last_time_s": (interval["last_timestamp_ns"]-origin)*1e-9,
                           "source": "camera_schedule_support"})
    return events


def _native_status(status):
    if status is None:
        return None
    # A failed native exception may contain its internal filenames. Preserve
    # structured measurements, not arbitrary exception text or unknown fields.
    keys = {"schema_version", "state", "upstream_revision", "counts",
            "first_camera_timestamp_ns", "last_camera_timestamp_ns",
            "first_estimate_timestamp_ns", "last_estimate_timestamp_ns",
            "initialized_timestamp_ns", "initialization_delay_s", "timing"}
    result = {key: value for key, value in status.items() if key in keys}
    result["error_reported"] = status.get("error") is not None
    return _json_safe_times(result)


def build_investigation(estimate_path: Path, reference_path: Path, imu_path: Path, *,
                        request: dict, evaluation: dict, native_status: dict | None = None) -> dict:
    """Validate identities and derive synchronized evidence without changing scores."""
    if (type(request) is not dict or set(request) != _REQUEST_KEYS
            or type(request["schema_version"]) is not int or request["schema_version"] != 1
            or request["run_state"] not in {"complete", "partial"}):
        raise ValueError("Unsupported investigation request.")
    for key in ("specification_sha256", "dataset_manifest_sha256", "evaluator_sha256",
                "reference_sha256", "imu_sha256"):
        _digest(request[key])
    if (_sha(evaluator.__file__) != request["evaluator_sha256"]
            or _sha(reference_path) != request["reference_sha256"]
            or _sha(imu_path) != request["imu_sha256"]):
        raise ValueError("Investigation input or evaluator identity differs from its request.")
    expected = evaluator._timestamps(request["expected_timestamps_ns"], "expected camera timestamps")
    if not len(expected):
        raise ValueError("Camera schedule must not be empty.")
    estimate, reference = evaluator.read_trajectory(estimate_path), evaluator.read_trajectory(reference_path)
    protocol = evaluation["protocol"]
    calculated = evaluator.evaluate_trajectories(
        estimate, reference, expected_timestamps_ns=expected,
        max_reference_gap_ns=protocol["max_reference_gap_ns"],
        max_estimate_timestamp_error_ns=protocol["max_estimate_timestamp_error_ns"])
    if json.dumps(calculated, sort_keys=True, allow_nan=False) != json.dumps(evaluation, sort_keys=True, allow_nan=False):
        raise ValueError("Saved evaluation differs from the qualified full-resolution evaluator.")
    selected, _ = evaluator._estimate_indices(estimate, expected, protocol["max_estimate_timestamp_error_ns"])
    supported, positions, quaternions = evaluator._reference_at(reference, expected, protocol["max_reference_gap_ns"])
    estimated = np.zeros(len(expected), dtype=bool)
    estimated[selected] = True
    estimates = {int(index): number for number, index in enumerate(selected)}
    first, last = (int(selected[0]), int(selected[-1])) if len(selected) else (None, None)
    errors = {}
    if evaluation["per_sample"] is not None:
        errors = {stamp: (position, orientation) for stamp, position, orientation in zip(
            evaluation["per_sample"]["timestamp_ns"], evaluation["per_sample"]["position_error_m"],
            evaluation["per_sample"]["orientation_error_deg"], strict=True)}
    alignment = evaluation["alignment"]
    rotation = np.asarray(alignment["rotation_estimate_world_to_reference_world"]) if alignment else None
    translation = np.asarray(alignment["translation_m"]) if alignment else None
    origin = int(expected[0])
    frames = []
    for index, stamp in enumerate(expected):
        raw, aligned, source_stamp = None, None, None
        if index in estimates:
            number = estimates[index]
            source_stamp = str(int(estimate.timestamp_ns[number]))
            raw = _pose(estimate.position_m[number], estimate.quaternion_xyzw[number])
            if alignment is not None:
                orientation = rotation @ evaluator._rotation_matrices(estimate.quaternion_xyzw[number:number+1])[0]
                aligned = _pose(rotation @ estimate.position_m[number] + translation, _quaternion(orientation))
        reason = (None if raw is not None else "no_estimates" if first is None else
                  "initialization" if index < first else "trailing" if index > last else "interior")
        error = errors.get(int(stamp))
        frames.append({"index": index, "timestamp_ns": str(int(stamp)), "time_s": (int(stamp)-origin)*1e-9,
                       "estimated": bool(estimated[index]), "reference_supported": bool(supported[index]),
                       "associated": bool(estimated[index] and supported[index]), "evaluated": error is not None,
                       "missing_estimate_reason": reason, "estimate_timestamp_ns": source_stamp,
                       "estimate_raw": raw, "estimate": aligned,
                       "reference": _pose(positions[index], quaternions[index]) if supported[index] else None,
                       "position_error_m": error[0] if error else None,
                       "orientation_error_deg": error[1] if error else None})
    summary = {key: _json_safe_times(value, key) for key, value in evaluation.items()
               if key not in {"per_sample", "protocol", "alignment", "schema_version"}}
    unavailable = {"available": False, "reason": "The pinned native adapter does not export this diagnostic."}
    output = {
        "schema_version": 1, "run_state": request["run_state"],
        "specification_sha256": request["specification_sha256"],
        "dataset_manifest_sha256": request["dataset_manifest_sha256"],
        "time": {"origin_timestamp_ns": str(origin), "first_timestamp_ns": str(origin),
                 "last_timestamp_ns": str(int(expected[-1])), "duration_s": (int(expected[-1])-origin)*1e-9,
                 "basis": "recording_sensor_timestamps", "offset_fitted": False},
        "coordinate_frames": {
            "estimate_raw": "IMU body to estimator local world",
            "estimate": "IMU body to reference world after the full-recording global SE3 alignment",
            "reference": "IMU body to motion-capture reference world", "imu": "IMU sensor/body axes",
            "quaternion": "Hamilton_xyzw_body_to_world",
            "euler": "R=Rz(yaw)Ry(pitch)Rx(roll); principal values; at gimbal lock roll=0; display only",
        },
        "units": {"position": "m", "orientation_error": "deg", "euler": "deg",
                  "gyro": "rad/s", "acceleration": "m/s^2", "relative_time": "s", "absolute_time": "ns string"},
        "protocol": protocol, "alignment": alignment, "summary": summary, "frames": frames,
        "imu": {"source": "raw_input", "bias_corrected": False, "resampled": False,
                "samples": _imu_samples(imu_path, origin)},
        "events": _events(expected, estimated, supported, native_status),
        "diagnostics": {name: dict(unavailable) for name in
                        ("features", "biases", "covariance", "innovations", "tracking_status")},
        "provenance": {"dataset_id": request["dataset_id"], "license": request["license"],
                       "trajectory_sha256": _sha(estimate_path), "reference_sha256": request["reference_sha256"],
                       "imu_sha256": request["imu_sha256"], "evaluator_sha256": request["evaluator_sha256"],
                       "builder_sha256": _sha(__file__), "interval_scorer_sha256": _sha(Path(__file__).with_name("vio_investigation_metrics.py")),
                       "evaluation_sha256": hashlib.sha256(json.dumps(evaluation, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
                       "evaluation_hash_encoding": "canonical_json_sorted_keys_compact_utf8",
                       "display_downsampling": False, "native_status": _native_status(native_status)},
    }
    # Check the JSON boundary now; never serialize NaN or an imprecise absolute timestamp.
    json.dumps(output, allow_nan=False)
    output["full_interval"] = summarize_interval(output, str(origin), str(int(expected[-1])))
    return output


def validate_investigation(artifact: dict, *, request: dict, evaluation: dict,
                           trajectory_sha256: str) -> None:
    """Check isolated-child output before allowing it into a published manifest."""
    if (type(artifact.get("schema_version")) is not int or artifact["schema_version"] != 1
            or artifact.get("run_state") != request["run_state"]
            or artifact.get("specification_sha256") != request["specification_sha256"]
            or artifact.get("dataset_manifest_sha256") != request["dataset_manifest_sha256"]):
        raise ValueError("Investigation identity does not match this attempt.")
    provenance = artifact["provenance"]
    for key, expected in (("trajectory_sha256", trajectory_sha256),
                          ("reference_sha256", request["reference_sha256"]),
                          ("imu_sha256", request["imu_sha256"]),
                          ("evaluator_sha256", request["evaluator_sha256"]),
                          ("builder_sha256", _sha(__file__)),
                          ("interval_scorer_sha256", _sha(Path(__file__).with_name("vio_investigation_metrics.py")))):
        if provenance.get(key) != expected:
            raise ValueError("Investigation provenance does not match its validated sources.")
    summary = {key: _json_safe_times(value, key) for key, value in evaluation.items()
               if key not in {"per_sample", "protocol", "alignment", "schema_version"}}
    for key, value in (("summary", summary), ("protocol", evaluation["protocol"]),
                       ("alignment", evaluation["alignment"])):
        if json.dumps(artifact[key], sort_keys=True, allow_nan=False) != json.dumps(value, sort_keys=True, allow_nan=False):
            raise ValueError("Investigation changes the independently evaluated result.")
    expected = request["expected_timestamps_ns"]
    frames = artifact["frames"]
    if len(frames) != len(expected):
        raise ValueError("Investigation must retain the complete camera timeline.")
    errors = {}
    if evaluation["per_sample"] is not None:
        samples = evaluation["per_sample"]
        errors = dict(zip(samples["timestamp_ns"], zip(samples["position_error_m"], samples["orientation_error_deg"]), strict=True))
    for index, (stamp, frame) in enumerate(zip(expected, frames, strict=True)):
        if (type(frame["index"]) is not int or frame["index"] != index
                or frame["timestamp_ns"] != str(stamp)
                or frame["time_s"] != (stamp-expected[0])*1e-9):
            raise ValueError("Investigation camera timing differs from the selected recording.")
        error = errors.get(stamp)
        if ((frame["position_error_m"], frame["orientation_error_deg"]) != (error if error else (None, None))
                or frame["evaluated"] is not (error is not None)
                or frame["estimated"] is not (frame["estimate_raw"] is not None)
                or frame["reference_supported"] is not (frame["reference"] is not None)
                or (frame["estimated"] and evaluation["alignment"] is not None) != (frame["estimate"] is not None)):
            raise ValueError("Investigation support or errors differ from the evaluated samples.")
        for key in ("estimate_raw", "estimate", "reference"):
            pose = frame[key]
            if pose is not None:
                evaluator.Trajectory([stamp], [pose["position_m"]], [pose["quaternion_xyzw"]])
                if len(pose["rpy_deg"]) != 3 or not all(type(value) in (int,float) and math.isfinite(value) for value in pose["rpy_deg"]):
                    raise ValueError("Attitude display angles must be finite triples.")
    if (sum(frame["estimated"] for frame in frames) != evaluation["counts"]["estimate_samples"]
            or sum(frame["reference_supported"] for frame in frames) != evaluation["counts"]["reference_supported_camera_samples"]):
        raise ValueError("Investigation coverage differs from the evaluated support.")
    if artifact["full_interval"] != summarize_interval(artifact, str(expected[0]), str(expected[-1])):
        raise ValueError("Investigation interval scores differ from its full-resolution errors.")
    imu = artifact["imu"]
    if imu.get("source") != "raw_input" or imu.get("resampled") is not False or imu.get("bias_corrected") is not False:
        raise ValueError("Investigation must identify unmodified IMU input.")
    previous = None
    for sample in imu["samples"]:
        stamp = timestamp_ns(sample["timestamp_ns"])
        if ((previous is not None and stamp <= previous) or sample["time_s"] != (stamp-expected[0])*1e-9):
            raise ValueError("Invalid IMU sample timing.")
        for key in ("gyro_rad_s", "accel_m_s2"):
            if len(sample[key]) != 3 or not all(type(value) in (int,float) and math.isfinite(value) for value in sample[key]):
                raise ValueError("Invalid IMU sample measurements.")
        previous = stamp
    if not imu["samples"]:
        raise ValueError("Investigation IMU input is empty.")
    if any(value.get("available") is not False for value in artifact["diagnostics"].values()):
        raise ValueError("Unsupported native diagnostics must remain unavailable.")
    json.dumps(artifact, allow_nan=False)
