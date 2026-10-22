"""Metadata-only contract for fixed-calibration private stereo/IMU executions.

The maintained room1 profile and numerical evaluator remain unchanged. This
profile admits a bounded subset of prepared inputs, not a physical calibration
qualification. No recordings, images, numerical packages or runtimes are opened.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
from uuid import UUID

from .sensor_contract import contract_sha256, validate_contract
from .sensor_calibration import apply_clock
from .sensor_conventions import timestamp_to_ns, validate_rigid_transform
from .vio_investigation_metrics import timestamp_ns, summarize_interval
from .vio_specification import build_specification

PROFILE_ID = "openvins-private-stereo-v1"
SCHEMA_VERSION = 2
MAX_CAMERA_PAIRS = 10000
MAX_IMU_SAMPLES = 100000
MAX_IMAGE_BYTES = 2 * 1024**2
MAX_IMAGE_PIXELS = 1024 * 1024
_BASELINE = '%YAML:1.0 # need to specify the file type at the top!\n# SPDX-License-Identifier: GPL-3.0-or-later\n# Derived from OpenVINS 69488123ed9362dd44b6f28e7f4680abbff1442b config/tum_vi.\n# Meridian changes threading/verbosity and removes unused ROS outputs; see ../NOTICE.md.\n\nverbosity: "WARNING" # ALL, DEBUG, INFO, WARNING, ERROR, SILENT\n\nuse_fej: true # if first-estimate Jacobians should be used (enable for good consistency)\nintegration: "rk4" # discrete, rk4, analytical (if rk4 or analytical used then analytical covariance propagation is used)\nuse_stereo: true # if we have more than 1 camera, if we should try to track stereo constraints\nmax_cameras: 2 # how many cameras we have 1 = mono, 2 = stereo, >2 = binocular (all mono tracking)\n\ncalib_cam_extrinsics: true\ncalib_cam_intrinsics: true\ncalib_cam_timeoffset: true\ncalib_imu_intrinsics: false\ncalib_imu_g_sensitivity: false\n\nmax_clones: 11\nmax_slam: 50\nmax_slam_in_update: 25\nmax_msckf_in_update: 40\ndt_slam_delay: 2\n\ngravity_mag: 9.80766\n\nfeat_rep_msckf: "GLOBAL_3D"\nfeat_rep_slam: "ANCHORED_MSCKF_INVERSE_DEPTH"\nfeat_rep_aruco: "ANCHORED_MSCKF_INVERSE_DEPTH"\n\n# zero velocity update parameters we can use\n# we support either IMU-based or disparity detection.\ntry_zupt: false\nzupt_chi2_multipler: 0 # set to 0 for only disp-based\nzupt_max_velocity: 0.1\nzupt_noise_multiplier: 50\nzupt_max_disparity: 2.0 # set to 0 for only imu-based\nzupt_only_at_beginning: true\n\n# ==================================================================\n# ==================================================================\n\ninit_window_time: 1.5 # make 2sec if using dynamic...\ninit_imu_thresh: 0.45 # room1-5:0.45, room6:0.25\ninit_max_disparity: 15.0\ninit_max_features: 50\n\ninit_dyn_use: false\ninit_dyn_mle_opt_calib: false\ninit_dyn_mle_max_iter: 50\ninit_dyn_mle_max_time: 0.05\ninit_dyn_mle_max_threads: 1\ninit_dyn_num_pose: 6\ninit_dyn_min_deg: 10.0\n\ninit_dyn_inflation_ori: 10\ninit_dyn_inflation_vel: 100\ninit_dyn_inflation_bg: 10\ninit_dyn_inflation_ba: 100\ninit_dyn_min_rec_cond: 1e-20\n\ninit_dyn_bias_g: [0.0, 0.0, 0.0]\ninit_dyn_bias_a: [0.0, 0.0, 0.0]\n\n# ==================================================================\n# ==================================================================\n\nrecord_timing_information: false\nrecord_timing_filepath: "/tmp/traj_timing.txt"\n\n\n# ==================================================================\n# ==================================================================\n\n# our front-end feature tracking parameters\n# we have a KLT and descriptor based (KLT is better implemented...)\nuse_klt: true\nnum_pts: 200\nfast_threshold: 20\ngrid_x: 5\ngrid_y: 5\nmin_px_dist: 15\nknn_ratio: 0.65\ntrack_frequency: 21.0\ndownsample_cameras: false\nnum_opencv_threads: 1 # -1: auto, 0-1: serial, >1: number of threads\nmulti_threading_pubs: false\nmulti_threading_subs: false\nhistogram_method: "HISTOGRAM" # NONE, HISTOGRAM, CLAHE\n\n# aruco tag tracker for the system\n# DICT_6X6_1000 from https://chev.me/arucogen/\nuse_aruco: false\nnum_aruco: 1024\ndownsize_aruco: true\n\n# ==================================================================\n# ==================================================================\n\n# camera noises and chi-squared threshold multipliers\nup_msckf_sigma_px: 1\nup_msckf_chi2_multipler: 1\nup_slam_sigma_px: 1\nup_slam_chi2_multipler: 1\nup_aruco_sigma_px: 1\nup_aruco_chi2_multipler: 1\n\n# masks for our images\nuse_mask: true\nmask0: "mask_tumvi0.png" #relative to current file\nmask1: "mask_tumvi1.png" #relative to current file\n\n# imu and camera spacial-temporal\n# imu config should also have the correct noise values\nrelative_config_imu: "kalibr_imu_chain.yaml"\nrelative_config_imucam: "kalibr_imucam_chain.yaml"\n'
_LEGACY = build_specification("tumvi-room1-512-16-v1", {"num_pts": 200})
_BINDING = {"input_revision_id", "acquisition_id", "published_attempt", "acquisition_sha256",
            "prepared_input_sha256", "publication_manifest_sha256"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Expected a lowercase SHA256 identity.")
    return value


def _id(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError("Invalid stream/frame/segment identifier.")
    return value


def _path(value):
    from pathlib import PurePosixPath
    if (type(value) is not str or not value or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("Invalid prepared relative path.")
    return value


def _identity(value):
    if type(value) is not dict or set(value) != {"id", "sha256"}:
        raise ValueError("Invalid contract identity.")
    _id(value["id"]); _digest(value["sha256"])


def _numbers(value, count, *, positive=False):
    if (type(value) is not list or len(value) != count or any(type(v) not in (int, float)
            or not math.isfinite(v) or (positive and v <= 0) for v in value)):
        raise ValueError("Invalid finite calibration vector.")


def _binding(value):
    if type(value) is not dict or set(value) != _BINDING:
        raise ValueError("Private input binding requires exact immutable identities.")
    for key in ("input_revision_id", "acquisition_id", "published_attempt"):
        if type(value[key]) is not str or str(UUID(value[key])) != value[key]:
            raise ValueError("Input binding requires canonical UUIDs.")
    for key in _BINDING - {"input_revision_id", "acquisition_id", "published_attempt"}:
        _digest(value[key])


def render_configuration(spec):
    """Render only closed, validated numeric calibration into native YAML."""
    runtime = spec["calibration_parameters"]
    text = _BASELINE
    for old, new in (("calib_cam_extrinsics: true", "calib_cam_extrinsics: false"),
                     ("calib_cam_intrinsics: true", "calib_cam_intrinsics: false"),
                     ("calib_cam_timeoffset: true", "calib_cam_timeoffset: false"),
                     ("use_mask: true", "use_mask: false"),
                     ("num_pts: 200", f"num_pts: {spec['parameters']['num_pts']}")):
        if text.count(old) != 1:
            raise ValueError("Fixed configuration template differs.")
        text = text.replace(old, new)
    text = "\n".join(line for line in text.splitlines() if not line.startswith(("mask0:", "mask1:"))) + "\n"
    identity = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
    lines = ["%YAML:1.0", "imu0:", "  T_i_b:"]
    lines += ["    - " + canonical(row + [0.]) for row in identity] + ["    - [0.0,0.0,0.0,1.0]"]
    for native, key in (("accelerometer_noise_density", "accel_density_m_s2_per_sqrt_hz"),
                        ("accelerometer_random_walk", "accel_bias_diffusion_m_s2_per_sqrt_s"),
                        ("gyroscope_noise_density", "gyro_density_rad_s_per_sqrt_hz"),
                        ("gyroscope_random_walk", "gyro_bias_diffusion_rad_s_per_sqrt_s")):
        lines.append(f"  {native}: {runtime['noise'][key]!r}")
    lines += ['  rostopic: /imu0', '  time_offset: 0.0', '  update_rate: 200.0', '  model: "kalibr"']
    for name in ("Tw", "R_IMUtoGYRO", "Ta", "R_IMUtoACC", "Tg"):
        lines.append(f"  {name}:")
        lines += ["    - " + canonical(row) for row in (identity if name != "Tg" else [[0., 0., 0.]] * 3)]
    cameras = ["%YAML:1.0"]
    for number, camera in enumerate(runtime["cameras"]):
        cameras += [f"cam{number}:", "  T_cam_imu:"]
        cameras += ["    - " + canonical(row + [translation]) for row, translation in
                    zip(camera["rotation"], camera["translation_m"], strict=True)]
        cameras += ["    - [0.0,0.0,0.0,1.0]", f"  cam_overlaps: [{1-number}]", "  camera_model: pinhole",
                    "  distortion_coeffs: " + canonical(camera["coefficients"]),
                    "  distortion_model: " + camera["distortion"], "  intrinsics: " + canonical(camera["intrinsics"]),
                    "  resolution: " + canonical(camera["resolution"]), f"  rostopic: /cam{number}/image_raw"]
    return {"estimator_config.yaml": text, "kalibr_imu_chain.yaml": "\n".join(lines) + "\n",
            "kalibr_imucam_chain.yaml": "\n".join(cameras) + "\n"}


def _fixed():
    evaluation = deepcopy(_LEGACY["evaluation"])
    evaluation.pop("qualification_criteria")
    evaluation["qualification"] = "not_assessed_private_recording"
    return {"schema_version": SCHEMA_VERSION, "profile_id": PROFILE_ID,
            "estimator": deepcopy(_LEGACY["estimator"]), "runtime": deepcopy(_LEGACY["runtime"]),
            "resources": deepcopy(_LEGACY["resources"]), "scheduling": deepcopy(_LEGACY["scheduling"]),
            "evaluation": evaluation, "policy": {
                "calibration": "fixed_supplied_parameters_no_online_refinement", "initial_camera_imu_offset_s": 0.0,
                "imu_intrinsics": "prepared_measurements_identity_runtime_intrinsics", "noise": "supplied_for_prepared_measurement_domain",
                "bias_states": "estimated_residual_bias_states_not_exported_calibration", "masks": "disabled",
                "gravity_m_s2": 9.80766, "initialization": "static_inertial_and_visual_no_reference_initialization",
                "image_conversion": "mono8_unchanged_or_mono16_OpenCV_255_over_65535",
                "reference_use": "independent_evaluation_only", "timestamp_domain": "nonnegative_int64_ns",
                "imu_rate_metadata_hz": 200.0, "propagation_timing": "actual_sample_timestamps",
                "max_camera_pairs": MAX_CAMERA_PAIRS, "max_imu_samples": MAX_IMU_SAMPLES}}


def _configuration(spec):
    return {"rendering_version": 1, "files": {name: {"sha256": hashlib.sha256(text.encode()).hexdigest(),
            "size_bytes": len(text.encode())} for name, text in render_configuration(spec).items()}}


def build_private_specification(binding, prepared, source, calibration, *, parameters=None, interval=None, clock_mappings=()):
    """Assess declarations and freeze a supported interval; never inspect payload."""
    _binding(binding)
    for value, kind in ((prepared, "prepared_input"), (source, "acquisition"), (calibration, "calibration")):
        if type(value) is not dict:
            raise ValueError("Private stereo execution requires a selected supplied calibration.")
        validate_contract(value)
        if value["kind"] != kind:
            raise ValueError("Incorrect input contract kind.")
    if (prepared["target_profile"] != "stereo_imu_candidate_v1" or prepared["acquisition"] != {"id": source["id"], "sha256": contract_sha256(source)}
            or prepared["calibration"] != {"id": calibration["id"], "sha256": contract_sha256(calibration)}
            or binding["acquisition_sha256"] != contract_sha256(source)
            or binding["prepared_input_sha256"] != contract_sha256(prepared)):
        raise ValueError("Prepared input identity or stereo profile differs from its binding.")
    params = {"num_pts": 200} if parameters is None else deepcopy(parameters)
    if type(params) is not dict or set(params) != {"num_pts"} or type(params["num_pts"]) is not int or params["num_pts"] not in (100, 200, 400):
        raise ValueError("Only feature budgets 100, 200 and 400 are supported.")
    mapping_by_key = {}
    identities = []
    for mapping in clock_mappings:
        validate_contract(mapping)
        if mapping["kind"] != "clock_mapping" or mapping["acquisition"] != prepared["acquisition"]:
            raise ValueError("Mapping differs from prepared input source.")
        key = mapping["source_stream_id"], mapping["source_segment_id"]
        if key in mapping_by_key:
            raise ValueError("Clock mapping composition is unsupported.")
        mapping_by_key[key] = mapping
        identities.append({"id": mapping["id"], "sha256": contract_sha256(mapping)})
    if sorted(identities, key=lambda x:x["id"]) != sorted(prepared["clock_mappings"], key=lambda x:x["id"]):
        raise ValueError("Prepared clock mapping closure differs.")
    streams = {s["id"]: s for s in source["streams"]}
    outputs = {s["stream_id"]: s for s in prepared["outputs"]}
    cameras = source.get("stereo_pairing")
    if cameras is None or cameras["policy"] != "exact_timestamp_pairs":
        raise ValueError("Exactly synchronized stereo is required.")
    camera_ids = cameras["camera_ids"]
    imus = [streams[name] for name in outputs if streams[name]["kind"] == "imu"]
    if len(imus) != 1 or imus[0]["measurement_semantics"] != "instantaneous_imu" or imus[0]["timestamp_semantics"] != "sample":
        raise ValueError("Exactly one instantaneous IMU stream is required.")
    imu = imus[0]
    selected = [streams[name] for name in camera_ids] + [imu]
    if any(s["id"] not in outputs for s in selected):
        raise ValueError("Both cameras and the IMU must be prepared.")
    selections = {}
    for stream in selected:
        values = [v for v in prepared["selections"] if v["stream_id"] == stream["id"]]
        if len(values) != 1:
            raise ValueError("The private profile requires one reset segment per sensor stream.")
        selections[stream["id"]] = values[0]
    segments = {s["segment_id"] for s in selections.values()}
    clocks = {outputs[s["id"]]["clock_id"] for s in selected}
    if len(segments) != 1 or len(clocks) != 1:
        raise ValueError("Stereo and IMU require the same declared segment and prepared clock.")
    segment, clock = next(iter(segments)), next(iter(clocks))
    source_clocks = {c["id"]: c for c in source["clocks"]}
    bounds = []
    for stream in selected[:2]:
        selection = selections[stream["id"]]
        mapping = mapping_by_key.get((stream["id"], segment))
        pair = [timestamp_to_ns(selection[k], source_clocks[stream["clock_id"]]["unit"]) for k in ("first_tick", "last_tick")]
        bounds.append([apply_clock(t, mapping) if mapping else t for t in pair])
    if bounds[0] != bounds[1] or bounds[0][0] < 0:
        raise ValueError("Camera timestamps must be nonnegative and exactly paired after preparation.")
    if interval is None:
        interval = {"segment_id": segment, "first_ns": str(bounds[0][0]), "last_ns": str(bounds[0][1])}
    if type(interval) is not dict or set(interval) != {"segment_id", "first_ns", "last_ns"}:
        raise ValueError("Interval requires segment_id, first_ns and last_ns.")
    if interval["segment_id"] != segment or not bounds[0][0] <= timestamp_ns(interval["first_ns"]) < timestamp_ns(interval["last_ns"]) <= bounds[0][1]:
        raise ValueError("The interval must be a nonempty increasing camera range within one segment.")
    calibration_imu = calibration["imu"]
    if calibration_imu is None or calibration_imu["sensor_id"] != imu["id"] or calibration_imu["noise"] is None:
        raise ValueError("Supplied IMU noise parameters are required.")
    if any(calibration_imu["noise"][key] <= 0 for key in calibration_imu["noise"] if key not in {"model", "assumptions"}):
        raise ValueError("The private profile requires positive continuous IMU noise and bias diffusion.")
    if imu["processing"]["calibration"] == "unknown":
        raise ValueError("The source IMU calibration state must be known before execution.")
    corrected = any(op["kind"] == "imu_calibration" and op["stream_id"] == imu["id"] for op in prepared["operations"])
    if corrected and any(v != 1 for key in ("gyro_scale", "accel_scale") for v in calibration_imu[key]):
        raise ValueError("Applied nonidentity IMU gains require a separately supported noise model.")
    if not corrected and calibration["applicability"]["imu_intrinsics"] == "candidate_not_applied":
        raise ValueError("Candidate IMU corrections must be explicitly applied before execution.")
    camera_parameters = []
    camera_metadata = []
    for slot, stream in zip(("cam0", "cam1"), selected[:2], strict=True):
        c = next((c for c in calibration["cameras"] if c["sensor_id"] == stream["id"]), None)
        frame = outputs[stream["id"]]["frame_id"]
        transform = next((t for t in calibration["transforms"] if t["source_frame"] == outputs[imu["id"]]["frame_id"] and t["target_frame"] == frame), None)
        if c is None or c["frame_id"] != frame or c["resolution"] != stream["camera"]["resolution"] or transform is None:
            raise ValueError("Camera intrinsics and direct prepared IMU-to-camera transforms are required.")
        if stream["processing"]["calibration"] != "not_applied":
            raise ValueError("Camera pixels require a known unapplied geometric calibration state.")
        if stream["camera"]["shutter"] != "global" or stream["timestamp_semantics"] != "exposure_midpoint" or math.prod(c["resolution"]) > MAX_IMAGE_PIXELS:
            raise ValueError("Only bounded global-shutter camera images are supported.")
        encoding = "mono8" if any(op["kind"] == "image_conversion" and op["stream_id"] == stream["id"] for op in prepared["operations"]) else stream["camera"]["encoding"]
        camera_parameters.append({key: deepcopy(c[key]) for key in ("resolution", "projection", "distortion", "intrinsics", "coefficients")}
                                 | {key: deepcopy(transform[key]) for key in ("rotation", "translation_m")})
        camera_metadata.append({"slot": slot, "stream_id": stream["id"], "frame_id": frame,
            "resolution": c["resolution"], "encoding": encoding, "index_path": outputs[stream["id"]]["index_path"]})
    references = [streams[name] for name in outputs if streams[name]["kind"] == "reference_pose"]
    reference = None
    reason = "reference_absent"
    if len(references) > 1:
        reason = "multiple_reference_streams_unsupported"
    elif references:
        stream = references[0]
        out = outputs[stream["id"]]
        sel = [v for v in prepared["selections"] if v["stream_id"] == stream["id"]]
        reason = ("reference_clock_mismatch" if out["clock_id"] != clock else
                  "reference_body_frame_mismatch" if out["frame_id"] != outputs[imu["id"]]["frame_id"] else
                  "reference_reset_segments_unsupported" if len(sel) != 1 else None)
        if reason is None:
            reference = {"stream_id": stream["id"], "frame_id": out["frame_id"], "parent_frame_id": stream["reference"]["parent_frame_id"],
                "segment_id": sel[0]["segment_id"], "index_path": out["index_path"]}
    result = _fixed() | {"input": deepcopy(binding) | {"prepared_id": prepared["id"], "acquisition_contract_id": source["id"],
                "calibration": deepcopy(prepared["calibration"]), "clock_mappings": deepcopy(prepared["clock_mappings"])},
        "parameters": params, "interval": deepcopy(interval), "clock_id": clock,
        "cameras": camera_metadata, "imu": {"stream_id": imu["id"], "frame_id": outputs[imu["id"]]["frame_id"], "index_path": outputs[imu["id"]]["index_path"]},
        "reference": {"stream": reference, "unavailable_reason": reason}, "processing": deepcopy(prepared["operations"]),
        "license": source["provenance"]["license"], "calibration_parameters": {"cameras": camera_parameters, "noise": deepcopy(calibration_imu["noise"])}}
    result["configuration"] = _configuration(result)
    validate_private_specification(result)
    return result


def _validate_private_specification(spec):
    """Reject altered fixed policy and malformed runtime parameters without I/O."""
    fixed = _fixed()
    extra = {"input", "parameters", "interval", "clock_id", "cameras", "imu", "reference", "processing", "license", "calibration_parameters", "configuration"}
    if type(spec) is not dict or set(spec) != set(fixed) | extra:
        raise ValueError("Unsupported private specification shape.")
    if any(canonical(spec[k]) != canonical(v) for k, v in fixed.items()):
        raise ValueError("Private runtime or evaluation policy differs from its version.")
    data = spec["input"]
    if type(data) is not dict or set(data) != _BINDING | {"prepared_id", "acquisition_contract_id", "calibration", "clock_mappings"}:
        raise ValueError("Invalid frozen private input identity.")
    _binding({k:data[k] for k in _BINDING})
    _id(data["prepared_id"]); _id(data["acquisition_contract_id"]); _identity(data["calibration"])
    if type(data["clock_mappings"]) is not list or len(data["clock_mappings"]) > 16:
        raise ValueError("Invalid mapping identities.")
    for value in data["clock_mappings"]: _identity(value)
    if (type(spec["parameters"]) is not dict or set(spec["parameters"]) != {"num_pts"}
            or type(spec["parameters"]["num_pts"]) is not int or spec["parameters"]["num_pts"] not in (100,200,400)):
        raise ValueError("Unsupported feature budget.")
    interval = spec["interval"]
    if type(interval) is not dict or set(interval) != {"segment_id", "first_ns", "last_ns"}:
        raise ValueError("Invalid frozen interval.")
    _id(interval["segment_id"]); _id(spec["clock_id"])
    if not 0 <= timestamp_ns(interval["first_ns"]) < timestamp_ns(interval["last_ns"]):
        raise ValueError("Private runtime requires increasing nonnegative nanoseconds.")
    if type(spec["cameras"]) is not list or len(spec["cameras"]) != 2:
        raise ValueError("Exactly two cameras are required.")
    for slot, camera in zip(("cam0", "cam1"), spec["cameras"], strict=True):
        if type(camera) is not dict or set(camera) != {"slot", "stream_id", "frame_id", "resolution", "encoding", "index_path"} or camera["slot"] != slot:
            raise ValueError("Invalid camera metadata.")
        _id(camera["stream_id"]); _id(camera["frame_id"]); _path(camera["index_path"])
        if camera["encoding"] not in {"mono8", "mono16"} or type(camera["resolution"]) is not list or len(camera["resolution"]) != 2 or any(type(v) is not int or v <= 0 for v in camera["resolution"]) or math.prod(camera["resolution"]) > MAX_IMAGE_PIXELS:
            raise ValueError("Invalid camera encoding or dimensions.")
    imu = spec["imu"]
    if type(imu) is not dict or set(imu) != {"stream_id", "frame_id", "index_path"}:
        raise ValueError("Invalid prepared IMU metadata.")
    _id(imu["stream_id"]); _id(imu["frame_id"]); _path(imu["index_path"])
    runtime = spec["calibration_parameters"]
    if type(runtime) is not dict or set(runtime) != {"cameras", "noise"} or len(runtime["cameras"]) != 2:
        raise ValueError("Invalid runtime calibration.")
    for camera, metadata in zip(runtime["cameras"], spec["cameras"], strict=True):
        if type(camera) is not dict or set(camera) != {"resolution", "projection", "distortion", "intrinsics", "coefficients", "rotation", "translation_m"}:
            raise ValueError("Invalid camera parameter shape.")
        if camera["projection"] != "pinhole" or camera["distortion"] not in {"radtan", "equidistant"} or camera["resolution"] != metadata["resolution"]:
            raise ValueError("Unsupported camera model or resolution.")
        _numbers(camera["intrinsics"], 4); _numbers(camera["coefficients"], 4)
        if min(camera["intrinsics"][:2]) <= 0: raise ValueError("Focal lengths must be positive.")
        validate_rigid_transform(camera["rotation"], camera["translation_m"])
    noise = runtime["noise"]
    keys = {"model", "assumptions", "gyro_density_rad_s_per_sqrt_hz", "accel_density_m_s2_per_sqrt_hz", "gyro_bias_diffusion_rad_s_per_sqrt_s", "accel_bias_diffusion_m_s2_per_sqrt_s"}
    if type(noise) is not dict or set(noise) != keys or noise["model"] != "continuous_white_noise_and_bias_diffusion" or type(noise["assumptions"]) is not str:
        raise ValueError("Unsupported noise model.")
    _numbers([noise[k] for k in sorted(keys - {"model", "assumptions"})], 4, positive=True)
    reference = spec["reference"]
    if type(reference) is not dict or set(reference) != {"stream", "unavailable_reason"}:
        raise ValueError("Invalid reference declaration.")
    if reference["stream"] is not None:
        r = reference["stream"]
        if type(r) is not dict or set(r) != {"stream_id", "frame_id", "parent_frame_id", "segment_id", "index_path"} or reference["unavailable_reason"] is not None:
            raise ValueError("Invalid comparable reference.")
        for k in set(r) - {"index_path"}: _id(r[k])
        _path(r["index_path"])
        if r["frame_id"] != imu["frame_id"]: raise ValueError("Reference body frame differs.")
    elif reference["unavailable_reason"] not in {"reference_absent", "multiple_reference_streams_unsupported", "reference_clock_mismatch", "reference_body_frame_mismatch", "reference_reset_segments_unsupported"}:
        raise ValueError("An unavailable reference needs an explicit reason.")
    if type(spec["processing"]) is not list or len(spec["processing"]) > 100 or type(spec["license"]) is not str:
        raise ValueError("Invalid input processing provenance.")
    for operation in spec["processing"]:
        if (type(operation) is not dict or set(operation) != {"kind", "stream_id", "artifact", "description", "parameters"}
                or operation["kind"] not in {"unit_conversion", "frame_transform", "image_conversion", "imu_calibration", "clock_mapping"}
                or type(operation["description"]) is not str or type(operation["parameters"]) is not dict):
            raise ValueError("Invalid prepared operation provenance.")
        _id(operation["stream_id"])
        if operation["artifact"] is not None: _identity(operation["artifact"])
    if canonical(spec["configuration"]) != canonical(_configuration(spec)):
        raise ValueError("Rendered calibration/configuration identity differs.")
    canonical(spec)


def validate_private_specification(spec):
    try:
        _validate_private_specification(spec)
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
        raise ValueError("Malformed private specification.") from error


def specification_sha256(spec):
    validate_private_specification(spec)
    return hashlib.sha256(canonical(spec).encode("ascii")).hexdigest()


def summarize_private_interval(investigation, start_ns, end_ns):
    if investigation.get("schema_version") != 2:
        raise ValueError("Unsupported private investigation version.")
    result = summarize_interval(dict(investigation, schema_version=1), start_ns, end_ns)
    availability = investigation["evaluation"]
    if result["metrics"] is None:
        availability = {"status": "unavailable", "reason": availability["reason"] or result["status"]}
    elif result["reference_coverage_fraction"] < 1:
        availability = {"status": "partial", "reason": "partial_reference_coverage"}
    else:
        availability = {"status": "available", "reason": None}
    return result | {"evaluation": availability}


def configuration_profile():
    from .vio_specification import configuration_profile as legacy_profile
    return {"schema_version": 2, "id": PROFILE_ID, "estimator": "OpenVINS",
        "parameters": legacy_profile()["parameters"], "private_input": True,
        "limitations": [
            "Uses one immutable prepared stereo/IMU revision and one exact nonnegative camera interval.",
            "Requires global-shutter exposure-midpoint stereo, instantaneous sample-time IMU, one shared segment and clock, supplied camera geometry and positive continuous IMU noise.",
            "Calibration is fixed; camera/IMU time offset starts at zero after explicit preparation. Generic masks are disabled.",
            "Static initialization and fixed gravity 9.80766 m/s² retain the declared profile assumptions; initialization is not guaranteed.",
            "References are evaluation-only and optional. No room1 qualification or independent physical validation is implied.",
            "Private comparisons and portable exports are unavailable in this profile version."]}


def validate_private_investigation(artifact, *, specification, input_manifest, evaluation, trajectory_sha256):
    """Validate retained child identities/support with no numerical dependencies."""
    digest = specification_sha256(specification)
    if (type(artifact) is not dict or type(artifact.get("schema_version")) is not int or artifact["schema_version"] != 2
            or artifact.get("run_state") not in {"complete", "partial"}
            or artifact.get("specification_sha256") != digest
            or artifact.get("dataset_manifest_sha256") != specification["input"]["publication_manifest_sha256"]):
        raise ValueError("Private investigation identity differs from its frozen input.")
    expected_input = deepcopy(specification["input"]) | {"profile_id": PROFILE_ID, "clock_id": specification["clock_id"],
        "imu_frame_id": specification["imu"]["frame_id"], "processing": specification["processing"]}
    if artifact.get("input") != expected_input or artifact.get("cameras") != specification["cameras"]:
        raise ValueError("Private investigation input processing/frame identity differs.")
    def safe_times(value, key=""):
        if key == "timestamp_ns" or key.endswith("_timestamp_ns"):
            return None if value is None else [str(v) for v in value] if isinstance(value, list) else str(value)
        if isinstance(value, dict): return {k:safe_times(v,k) for k,v in value.items()}
        if isinstance(value, list): return [safe_times(v,key) for v in value]
        return value
    summary = {k:safe_times(v,k) for k,v in evaluation.items() if k not in {"per_sample", "protocol", "alignment", "schema_version"}}
    if canonical(artifact.get("summary")) != canonical(summary):
        raise ValueError("Private investigation changes its numerical summary.")
    if artifact.get("evaluation") != evaluation.get("availability") or artifact.get("alignment") != evaluation.get("alignment") or artifact.get("protocol") != evaluation.get("protocol"):
        raise ValueError("Private investigation changes its independent evaluation.")
    provenance = artifact.get("provenance", {})
    reference_hash = input_manifest["reference_sha256"] if input_manifest["reference_available"] else None
    if (provenance.get("trajectory_sha256") != trajectory_sha256
            or provenance.get("reference_sha256") != reference_hash
            or provenance.get("imu_sha256") != input_manifest["imu_sha256"]
            or provenance.get("evaluator_sha256") != specification["evaluation"]["evaluator_sha256"]
            or provenance.get("evaluation_sha256") != hashlib.sha256(canonical(evaluation).encode()).hexdigest()
            or provenance.get("display_downsampling") is not False):
        raise ValueError("Private investigation payload provenance differs.")
    expected = input_manifest["camera_timestamps_ns"]
    frames = artifact.get("frames")
    if type(frames) is not list or len(frames) != len(expected) or not 1 < len(expected) <= MAX_CAMERA_PAIRS:
        raise ValueError("Investigation must retain the full selected camera schedule.")
    origin = timestamp_ns(expected[0])
    errors = {}
    if evaluation.get("per_sample") is not None:
        samples = evaluation["per_sample"]
        errors = dict(zip(map(str, samples["timestamp_ns"]), zip(samples["position_error_m"], samples["orientation_error_deg"]), strict=True))
    for index, (stamp, frame) in enumerate(zip(expected, frames, strict=True)):
        if type(frame) is not dict or type(frame.get("index")) is not int or frame["index"] != index or frame.get("timestamp_ns") != stamp or frame.get("time_s") != (timestamp_ns(stamp)-origin)*1e-9:
            raise ValueError("Investigation camera timing differs from its frozen interval.")
        error = errors.get(stamp)
        if (frame.get("estimated") is not (frame.get("estimate_raw") is not None)
                or frame.get("reference_supported") is not (frame.get("reference") is not None)
                or frame.get("associated") is not (frame.get("estimated") and frame.get("reference_supported"))
                or frame.get("evaluated") is not (error is not None)
                or (frame.get("position_error_m"), frame.get("orientation_error_deg")) != (error if error else (None, None))
                or (frame.get("estimated") and evaluation["alignment"] is not None) != (frame.get("estimate") is not None)):
            raise ValueError("Private investigation support/errors differ from evaluation.")
        for key in ("estimate_raw", "estimate", "reference"):
            pose = frame.get(key)
            if pose is not None:
                if type(pose) is not dict or set(pose) != {"position_m", "quaternion_xyzw", "rpy_deg"}:
                    raise ValueError("Invalid retained pose.")
                _numbers(pose["position_m"], 3); _numbers(pose["quaternion_xyzw"], 4); _numbers(pose["rpy_deg"], 3)
                if abs(math.sqrt(sum(v*v for v in pose["quaternion_xyzw"])) - 1) > 1e-6: raise ValueError("Retained quaternion is not unit length.")
    counts = evaluation["counts"]
    if sum(f["estimated"] for f in frames) != counts["estimate_samples"] or sum(f["reference_supported"] for f in frames) != counts["reference_supported_camera_samples"]:
        raise ValueError("Private investigation coverage differs.")
    imu = artifact.get("imu", {})
    corrected = any(op["kind"] == "imu_calibration" and op["stream_id"] == specification["imu"]["stream_id"] for op in specification["processing"])
    if imu.get("source") != "prepared_input" or imu.get("resampled") is not False or imu.get("bias_corrected") is not corrected:
        raise ValueError("IMU processing provenance differs.")
    samples = imu.get("samples")
    if type(samples) is not list or len(samples) != input_manifest["imu_samples"] or not 0 < len(samples) <= MAX_IMU_SAMPLES:
        raise ValueError("IMU investigation must retain all staged input samples.")
    previous = None
    for sample in samples:
        stamp = timestamp_ns(sample["timestamp_ns"])
        if stamp < 0 or (previous is not None and stamp <= previous) or sample["time_s"] != (stamp-origin)*1e-9:
            raise ValueError("Invalid prepared IMU investigation timing.")
        _numbers(sample["gyro_rad_s"], 3); _numbers(sample["accel_m_s2"], 3)
        previous = stamp
    if artifact.get("full_interval") != summarize_private_interval(artifact, expected[0], expected[-1]):
        raise ValueError("Retained interval summary differs from full-resolution evidence.")
    canonical(artifact)
