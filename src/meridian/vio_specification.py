"""Pure, versioned contract for the maintained OpenVINS room1 experiment profile.

Only the Python standard library is required. Pins come from the public 1.1
qualification; building metadata neither opens recordings nor starts a runtime.
"""

from copy import deepcopy
import hashlib
import json


SPECIFICATION_SCHEMA_VERSION = 1
PROFILE_ID = "openvins-tumvi-room1-v1"
DATASET_ID = "tumvi-room1-512-16-v1"
_CHOICES = (100, 200, 400)
_CONFIGURATION_FILES = {
    "estimator_config.yaml": {
        "sha256": "10c37c00c52a07ba5a86ce3d6d2846c2430397d6fa6a9e190dfdc47aa10c2045", "size_bytes": 3689,
    },
    "kalibr_imu_chain.yaml": {
        "sha256": "cb598952ee3766492a332eaae8567fe2b63a9bf6d63b9c0a7b9d988471600a3d", "size_bytes": 1351,
    },
    "kalibr_imucam_chain.yaml": {
        "sha256": "072d26e3dd20844ae7402f508c51cf7e899a401634ba6b0db0e08cd2880b7dab", "size_bytes": 1530,
    },
    "mask_tumvi0.png": {
        "sha256": "62dfc1053f336d1adb4ae01519579532370d1b211894566a7a4ec167dd8c3f4f", "size_bytes": 12277,
    },
    "mask_tumvi1.png": {
        "sha256": "67ff644599f862f2437026d9c43ef0a0887907470179a53e34447fb95cbd35ba", "size_bytes": 11930,
    },
}
_RENDERED_ESTIMATOR_HASHES = {
    100: "b20864246ac6c7d8d0b23b430568ba8860249fdc2207142f3a1095ca2a1f79f5",
    200: "10c37c00c52a07ba5a86ce3d6d2846c2430397d6fa6a9e190dfdc47aa10c2045",
    400: "3bfe690b59058cd7bb5ef9315e1867372e0cd9ef01e3ad1d4bdfc2df2ab7f452",
}
_PINNED = {
    "schema_version": SPECIFICATION_SCHEMA_VERSION,
    "profile_id": PROFILE_ID,
    "dataset": {
        "id": DATASET_ID,
        "prepared_dataset_id": "tum-vi-room1-512-16",
        "version": "512_16-calibrated-v1",
        "archive_sha256": "20354392eab5cc82c9770ed29cf0130c5acce0db54a257571499008fb34c398f",
        "prepared_manifest_sha256": "e55f0480533611fc1e198d16b181814a8d34752cb748becbfeec7bcb4908ba6d",
        "reference_sha256": "69f3dcb4c5453fc8bafb0e6214172bbfe0f176e42e119953862b120208ad22ba",
        "reference_use": "independent_evaluation_only",
    },
    "interval": {
        "mode": "full_recording",
        "first_camera_timestamp_ns": "1520530308199447626",
        "last_camera_timestamp_ns": "1520530449203911100",
        "camera_pairs": 2821,
        "imu_samples": 28122,
    },
    "estimator": {
        "name": "OpenVINS",
        "upstream_revision": "69488123ed9362dd44b6f28e7f4680abbff1442b",
        "adapter": "meridian-openvins",
        "adapter_sha256": "a577f358c893ac9171cad6a71bc2a65a49b9d90ab3dc426865d89b6ad2c8b782",
        "adapter_protocol_version": 1,
    },
    "runtime": {
        "image_id": "sha256:73e1213d073ce52a9a509decb15628a7c043e0690609171c4b9382ed0c2730da",
        "version": {
            "schema_version": 1, "protocol_version": 1, "adapter": "meridian-openvins",
            "adapter_sha256": "a577f358c893ac9171cad6a71bc2a65a49b9d90ab3dc426865d89b6ad2c8b782",
            "opencv_version": "4.6.0",
            "upstream_revision": "69488123ed9362dd44b6f28e7f4680abbff1442b",
        },
        "provenance_sha256": {
            "binary-sha256.txt": "7b66a083c7d5c69c6e85721e3357def42fbc1a125a5b13735f3e55ee3ad28070",
            "dependency-versions.txt": "92d76ee986a1ff4508a96fb3f2c26439d99cb446a546f8e0d795f56d69e7023e",
        },
    },
    "configuration": {
        "rendering_version": 1,
        "baseline_files": _CONFIGURATION_FILES,
        "rendered_files": _CONFIGURATION_FILES,
        "operation": "replace_exact_num_pts_line_in_pinned_baseline",
    },
    "scheduling": {
        "version": 1,
        "stereo_pairing": "exact_timestamp_pairs",
        "camera_policy": "every_pair_with_available_imu_bracket",
        "imu_policy": "feed_through_first_sample_strictly_after_camera_plus_current_estimated_offset",
        "time_origin": "subtract_first_sensor_timestamp_as_integer_before_seconds_conversion",
        "wall_clock_pacing": False,
        "callback_frequency_throttle": False,
    },
    "conventions": {
        "timestamp_unit": "ns",
        "absolute_timestamp_encoding": "decimal_integer_string",
        "position_unit": "m",
        "gyro_unit": "rad/s",
        "accelerometer_unit": "m/s^2",
        "body_frame": "imu",
        "orientation": "unit_Hamilton_xyzw_body_to_world",
        "camera_extrinsics": "T_cam_imu_maps_imu_to_camera",
        "image_conversion": "uint16_to_uint8_OpenCV_convertTo_scale_255_over_65535",
        "dataset_preprocessing": "already_calibrated_imu_and_synchronized_clocks_no_second_correction",
        "initial_camera_imu_offset_s": 0.0,
        "initialization": "static_inertial_and_visual_no_reference_initialization",
    },
    "resources": {
        "cpus": 2.0, "memory_mb": 4096, "timeout_s": 900.0,
        "pids": 128, "tmp_mb": 128, "network": "none", "rootfs": "read-only",
    },
    "evaluation": {
        "protocol_version": 1,
        "evaluator_sha256": "539f07a0e3dbabe5fd3b4874b42218db1938e5f3733d8ac3bd9c714900a8cca6",
        "maximum_estimate_timestamp_error_ns": 1000,
        "maximum_reference_bracket_ns": 20000000,
        "reference_position_interpolation": "linear",
        "reference_orientation_interpolation": "shortest_arc_slerp",
        "estimate_interpolation": False,
        "reference_extrapolation": False,
        "alignment": "global_SE3_over_all_evaluated_positions_scale_fixed_to_one",
        "time_offset_fitting": False,
        "qualification_criteria": {
            "minimum_evaluated_samples": 100,
            "minimum_coverage_fraction": 0.9,
            "minimum_evaluated_fraction": 0.9,
            "minimum_reference_coverage_fraction": 0.95,
            "maximum_initialization_delay_s": 10.0,
            "maximum_ate_rmse_m": 0.5,
            "maximum_orientation_rmse_deg": 15.0,
        },
    },
    "reproducibility": {
        "opencv_threads": 1,
        "omp_threads": 1,
        "openblas_threads": 1,
        "seed": None,
        "seed_policy": "no_random_seed_control_exposed_by_this_qualified_adapter",
        "claim": "same_runtime_and_inputs_only_no_cross_hardware_bit_identity_guarantee",
    },
}


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Specification must contain finite JSON values with string object keys.") from exc


def _parameters(parameters) -> dict:
    if type(parameters) is not dict or set(parameters) - {"num_pts"}:
        raise ValueError("Parameters must be an object containing only num_pts.")
    points = parameters.get("num_pts", 200)
    if type(points) is not int or points not in _CHOICES:
        raise ValueError("num_pts must be an integer choice: 100, 200, or 400.")
    return {"num_pts": points}


def build_specification(dataset_id, parameters, *, schema_version=1) -> dict:
    """Build a fresh pinned specification from the single supported parameter.

    An omitted num_pts key selects 200 only at construction. Validation of an
    already stored specification never fills defaults or upgrades its version.
    """
    if type(schema_version) is not int or schema_version != SPECIFICATION_SCHEMA_VERSION:
        raise ValueError("Unsupported specification schema_version; expected integer 1.")
    if type(dataset_id) is not str or dataset_id != DATASET_ID:
        raise ValueError("The selected dataset version is not supported by this configuration profile.")
    normalized = _parameters(parameters)
    spec = deepcopy(_PINNED)
    spec["parameters"] = normalized
    # Separate copies are important: changing the rendered hash must not alter
    # the baseline identity or another caller's specification.
    spec["configuration"]["rendered_files"] = deepcopy(_CONFIGURATION_FILES)
    spec["configuration"]["rendered_files"]["estimator_config.yaml"]["sha256"] = _RENDERED_ESTIMATOR_HASHES[normalized["num_pts"]]
    return spec


def validate_specification(spec) -> None:
    """Reject changed pins, missing/defaulted fields, extra keys, and wrong types."""
    if type(spec) is not dict:
        raise ValueError("Specification must be an object.")
    if type(spec.get("parameters")) is not dict or set(spec["parameters"]) != {"num_pts"}:
        raise ValueError("An immutable specification must explicitly contain num_pts.")
    dataset = spec.get("dataset")
    if type(dataset) is not dict:
        raise ValueError("Specification must contain a supported dataset identity.")
    expected = build_specification(dataset.get("id"), spec["parameters"], schema_version=spec.get("schema_version"))
    if _canonical(spec) != _canonical(expected):
        raise ValueError("Specification differs from the pinned configuration profile.")


def specification_sha256(spec) -> str:
    """Hash the validated immutable object using sorted compact ASCII JSON."""
    validate_specification(spec)
    return hashlib.sha256(_canonical(spec).encode("ascii")).hexdigest()


def configuration_profile() -> dict:
    """Return server-maintained controls without opening a recording or runtime."""
    return {
        "schema_version": 1,
        "id": PROFILE_ID,
        "dataset_id": DATASET_ID,
        "estimator": "OpenVINS",
        "parameters": {
            "num_pts": {
                "label": "Feature budget",
                "type": "integer",
                "default": 200,
                "minimum": 100,
                "maximum": 400,
                "choices": list(_CHOICES),
                "unit": "features (total across two cameras after initialization)",
                "description": "Target tracking budget divided between the two cameras after initialization; initialization uses its separate fixed feature settings. A larger budget can trade computation for visual support.",
            },
        },
        "limitations": [
            "Only the num_pts feature budget is configurable; every other runtime, data, calibration and evaluation setting is pinned.",
            "The full handheld room1 recording is used; custom intervals and other recordings are unsupported.",
            "A valid specification describes a supported configuration, not a completed run or a numerical qualification result.",
            "The original qualification evaluated num_pts=200; other choices require their own recorded executions.",
        ],
    }
