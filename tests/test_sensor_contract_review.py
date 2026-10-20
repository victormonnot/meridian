"""Independent scientific regressions using the public contract examples."""

import json
from pathlib import Path

import pytest

from meridian.sensor_contract import contract_sha256, validate_bundle


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "sensor-contracts"


def identity(value):
    return {"id": value["id"], "sha256": contract_sha256(value)}


def bundle():
    names = ("stereo-no-reference.json", "replay-acquisition.json",
             "calibration.json", "prepared-input.json")
    source, replay, calibration, prepared = [
        json.loads((EXAMPLES / name).read_text(encoding="utf-8")) for name in names
    ]
    prepared["clock_mappings"] = []
    prepared["operations"] = []
    streams = {stream["id"]: stream for stream in replay["streams"]}
    for output in prepared["outputs"]:
        output["clock_id"] = streams[output["stream_id"]]["clock_id"]
    return source, replay, calibration, prepared


def operation(kind, parameters, artifact=None):
    return {"kind": kind, "stream_id": "imu0", "artifact": artifact,
            "description": "Independent declaration-only regression.",
            "parameters": parameters}


def non_si_calibration_bundle():
    source, replay, calibration, prepared = bundle()
    imu = next(stream for stream in replay["streams"] if stream["kind"] == "imu")
    imu["imu"].update(angular_velocity_unit="deg/s", specific_force_unit="g",
                      local_gravity_m_s2=9.81)
    calibration["imu"] = {
        "sensor_id": "imu0", "frame_id": "imu",
        "gyro_bias_rad_s": [1, 0, 0], "accel_bias_m_s2": [1, 0, 0],
        "gyro_scale": [2, 1, 1], "accel_scale": [2, 1, 1], "noise": None,
    }
    calibration["applicability"]["imu_intrinsics"] = "candidate_not_applied"
    prepared["acquisition"] = identity(replay)
    prepared["calibration"] = identity(calibration)
    gyro_conversion = operation("unit_conversion", {
        "quantity": "angular_velocity", "source_unit": "deg/s",
        "target_unit": "rad/s", "local_gravity_m_s2": None,
    })
    accel_conversion = operation("unit_conversion", {
        "quantity": "specific_force", "source_unit": "g",
        "target_unit": "m/s^2", "local_gravity_m_s2": 9.81,
    })
    correction = operation("imu_calibration", {
        "model": "diagonal_scale_after_bias_subtraction",
    }, identity(calibration))
    return [source, replay, calibration, prepared], gyro_conversion, accel_conversion, correction


@pytest.mark.parametrize("conversion_position", ["after", "missing"])
def test_si_bias_calibration_requires_prior_unit_conversion(conversion_position):
    values, gyro, accel, correction = non_si_calibration_bundle()
    values[-1]["operations"] = [gyro, accel, correction]
    validate_bundle(values)
    # A 180 deg/s sample must become pi rad/s before subtracting a 1 rad/s bias.
    # Applying the correction first would mix unlike units.
    values[-1]["operations"] = (
        [correction, gyro, accel] if conversion_position == "after" else [correction]
    )
    with pytest.raises(ValueError):
        validate_bundle(values)


def test_repeated_unit_conversion_cannot_hide_behind_unused_parameter_change():
    values, gyro, accel, _ = non_si_calibration_bundle()
    values[-1]["operations"] = [gyro, accel]
    validate_bundle(values)
    repeated = json.loads(json.dumps(gyro))
    repeated["parameters"]["local_gravity_m_s2"] = 9.81
    values[-1]["operations"].append(repeated)
    with pytest.raises(ValueError):
        validate_bundle(values)


def test_frame_operations_must_consume_the_preceding_output_frame():
    source, replay, calibration, prepared = bundle()
    calibration["transforms"][1]["translation_m"] = [0, 0, 0]
    prepared["calibration"] = identity(calibration)
    prepared["operations"] = [
        operation("frame_transform", {"source_frame": "imu", "target_frame": "cam0"},
                  identity(calibration)),
    ]
    next(output for output in prepared["outputs"] if output["stream_id"] == "imu0")[
        "frame_id"
    ] = "cam0"
    validate_bundle([source, replay, calibration, prepared])
    prepared["operations"].append(
        operation("frame_transform", {"source_frame": "imu", "target_frame": "cam1"},
                  identity(calibration)),
    )
    # After the first operation the values are in cam0 axes, not original IMU axes.
    with pytest.raises(ValueError):
        validate_bundle([source, replay, calibration, prepared])


def test_extrinsics_only_calibration_binds_participating_sensor_instances():
    source, replay, calibration, prepared = bundle()
    calibration["cameras"] = []
    calibration["imu"] = None
    prepared["calibration"] = identity(calibration)
    validate_bundle([source, replay, calibration, prepared])
    next(stream for stream in replay["streams"] if stream["id"] == "cam1")[
        "sensor_instance"
    ] = "replacement-camera"
    prepared["acquisition"] = identity(replay)
    # Unchanged textual frame/rig labels cannot override a changed camera instance.
    with pytest.raises(ValueError):
        validate_bundle([source, replay, calibration, prepared])


def test_changed_output_clock_requires_mapping_every_selected_reset_segment():
    names = ("stereo-no-reference.json", "replay-acquisition.json",
             "calibration.json", "clock-mapping.json", "prepared-input.json")
    source, replay, calibration, mapping, prepared = [
        json.loads((EXAMPLES / name).read_text(encoding="utf-8")) for name in names
    ]
    validate_bundle([source, replay, calibration, mapping, prepared])
    next(stream for stream in replay["streams"] if stream["id"] == "imu0")[
        "segments"
    ].append({"id": "after-reset", "first_tick": "0", "last_tick": "10000000",
              "sample_count": 3})
    prepared["selections"].append({
        "stream_id": "imu0", "segment_id": "after-reset",
        "first_tick": "0", "last_tick": "10000000",
    })
    mapping["acquisition"] = identity(replay)
    prepared["acquisition"] = identity(replay)
    prepared["clock_mappings"] = [identity(mapping)]
    prepared["operations"][0]["artifact"] = identity(mapping)
    # One mapped pre-reset segment cannot relabel every subsequent source segment.
    with pytest.raises(ValueError):
        validate_bundle([source, replay, calibration, mapping, prepared])
